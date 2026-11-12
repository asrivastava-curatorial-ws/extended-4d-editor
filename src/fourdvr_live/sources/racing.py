"""Telemetry of driving games, decoded from the formats their makers publish.

Every decoder checks the size (and, where the format has them, the markers) of each packet against
the published layout and refuses anything else. What the publishers mostly do **not** say is which
way a positive value points. The directions below are the best reading of each document; they are
marked in docs/LIVE.md and must be checked once per game with ``fourdvr live monitor`` - a wrong one
is turned round in the configuration (``[axes]``), not in this file.

Layouts and where they are published: docs/TELEMETRY_FORMATS.md.
"""

from __future__ import annotations

import math
import socket
import struct

from ..sample import G, LiveSample
from .base import Decoder, Hits, Roughness, Slope, SourceInfo, UdpSource, lateral_from_yaw, yaw_from_lateral

Vec = tuple[float, float, float]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _unit(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-9 else (0.0, 0.0, 0.0)


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


# =========================================================================== Forza (Data Out)
FORZA = SourceInfo(
    "forza", "Forza Data Out", "Forza Motorsport (2023), Forza Motorsport 7, Forza Horizon 4 / 5 / 6", "car", 5300,
    setup="Settings > Gameplay & HUD > Data Out: On, IP of this computer, a port; any packet format",
    published="support.forza.net: 'Forza Motorsport Data Out Documentation', 'Forza Horizon 6 Data Out Documentation'",
)
FORZA_SIZES = {232: 0, 311: 0, 331: 0, 324: 12}       # packet size -> shift of the "dash" part (Horizon: 12 bytes more)


class ForzaDecoder(Decoder):
    """Car space: X right, Y up, Z forward; AngularVelocity X pitch, Y yaw, Z roll (rad/s)."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        shift = FORZA_SIZES.get(len(data))
        if shift is None:
            self.bad()
            return None
        race_on, stamp = struct.unpack_from("<iI", data, 0)
        rpm_max, rpm_idle, rpm = struct.unpack_from("<3f", data, 8)
        ax, ay, az, vx, vy, vz, wx, wy, wz, _yaw, pitch, roll = struct.unpack_from("<12f", data, 20)
        strips = struct.unpack_from("<4i", data, 116)
        puddles = struct.unpack_from("<4f", data, 132)
        surface = struct.unpack_from("<4f", data, 148)
        combined = struct.unpack_from("<4f", data, 180)
        accel = (az, -ax, ay)
        sample = LiveSample(
            accel=accel,
            rates=(-wz, wx, -wy),
            attitude=(-roll, pitch),
            speed=math.sqrt(vx * vx + vy * vy + vz * vz),
            rpm=rpm, rpm_max=rpm_max, rpm_idle=rpm_idle,
            rumble=max(min(1.0, max(abs(s) for s in surface)), 0.8 if any(strips) else 0.0),
            slip=min(1.0, max(0.0, max(combined) - 1.0)),
            active=race_on != 0,
            time=stamp / 1000.0,
        )
        if len(data) > 232:
            (speed,) = struct.unpack_from("<f", data, 244 + shift)
            throttle, brake, _clutch, _hand, gear = struct.unpack_from("<5B", data, 303 + shift)
            sample.speed, sample.throttle, sample.brake = speed, throttle / 255.0, brake / 255.0
            sample.gear = -1 if gear == 0 else int(gear)
        depth = max((p for p in puddles if 0.0 < p <= 1.0), default=0.0)
        if depth > 0.0:
            sample.effects["splash"] = depth
        sample.impact = self._hits.step(accel) if sample.active else 0.0
        return sample


# =========================================================================== EA SPORTS F1 23 / 24 / 25
F1 = SourceInfo(
    "f1", "EA SPORTS F1 UDP", "F1 23, F1 24, F1 25 (UDP format 2023, 2024 or 2025)", "car", 20777,
    setup="Settings > Telemetry Settings: UDP Telemetry On, IP of this computer, port 20777, send rate 60 Hz",
    published="EA forums: 'F1 25 UDP Specification' (and the F1 24 / F1 23 documents)",
)
F1_HEADER = 29
F1_CARS = 22
F1_MOTION, F1_SESSION, F1_TELEMETRY, F1_STATUS, F1_MOTION_EX = 0, 1, 6, 7, 13
F1_ROUGH = {1: 0.8, 3: 0.9, 4: 0.7, 5: 0.5, 6: 0.5, 7: 0.5, 9: 0.6, 11: 0.8}     # surface type -> rumble
F1_RAIN = {3: 0.4, 4: 0.8, 5: 1.0}                                                # weather -> rain


class F1Decoder(Decoder):
    """Header 29 bytes; motion (id 0, 1349 bytes) carries the g-forces of the player's car, motion-ex
    (id 13) its rotation rates, car telemetry (id 6) speed, pedals, revs and the surface under each
    wheel, session (id 1) the weather. A sample is made at every motion packet."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rates = (0.0, 0.0, 0.0)
        self._speed = self._rpm = self._throttle = self._brake = self._rumble = 0.0
        self._rpm_max, self._rpm_idle = 13000.0, 3500.0
        self._gear = 0
        self._rain = 0.0
        self._splash = 0.0

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) < F1_HEADER:
            self.bad()
            return None
        fmt, _year, _major, _minor, _version, kind = struct.unpack_from("<HBBBBB", data, 0)
        (stamp,) = struct.unpack_from("<f", data, 15)
        player = data[27]
        if fmt not in (2023, 2024, 2025) or player >= F1_CARS:
            self.bad()
            return None
        if kind == F1_MOTION:
            if len(data) != 1349:
                self.bad()
                return None
            g_lat, g_long, g_vert, _yaw, pitch, roll = struct.unpack_from("<6f", data, F1_HEADER + 60 * player + 36)
            accel = (g_long * G, -g_lat * G, g_vert * G)
            sample = LiveSample(
                accel=accel, rates=self._rates, attitude=(-roll, pitch), speed=self._speed, rpm=self._rpm,
                rpm_max=self._rpm_max, rpm_idle=self._rpm_idle, throttle=self._throttle, brake=self._brake,
                gear=self._gear, rumble=self._rumble if self._speed > 2.0 else 0.0, time=stamp,
            )
            sample.impact = self._hits.step(accel)
            if self._rain > 0.0:
                sample.effects["rain"] = self._rain
            if self._splash > 0.0 and self._speed > 5.0:
                sample.effects["splash"] = self._splash
            return sample
        if kind == F1_TELEMETRY and len(data) == 1352:
            base = F1_HEADER + 60 * player
            speed, throttle, _steer, brake, _clutch, gear, rpm = struct.unpack_from("<HfffBbH", data, base)
            surfaces = data[base + 56 : base + 60]
            self._speed, self._throttle, self._brake = speed / 3.6, throttle, brake
            self._gear, self._rpm = int(gear), float(rpm)
            self._rumble = max(F1_ROUGH.get(s, 0.0) for s in surfaces)
            self._splash = 0.5 if 8 in surfaces else 0.0
        elif kind == F1_MOTION_EX and len(data) in (217, 237, 273):
            wx, wy, wz = struct.unpack_from("<3f", data, 173)
            self._rates = (-wz, wx, -wy)
        elif kind == F1_SESSION and len(data) > F1_HEADER:
            self._rain = F1_RAIN.get(data[F1_HEADER], 0.0)
        elif kind == F1_STATUS and len(data) == 1239:
            rpm_max, rpm_idle = struct.unpack_from("<HH", data, F1_HEADER + 55 * player + 17)
            if rpm_max > rpm_idle > 0:
                self._rpm_max, self._rpm_idle = float(rpm_max), float(rpm_idle)
        else:
            self.ignored += 1
        return None


# =========================================================================== Codemasters "extradata=3"
CODEMASTERS = SourceInfo(
    "dirt", "Codemasters telemetry (extradata 3)", "DiRT Rally, DiRT Rally 2.0, DiRT 4, GRID (2019) and other "
    "Codemasters titles with <udp ... extradata=\"3\" />", "car", 20777,
    setup="hardware_settings_config.xml: <udp enabled=\"true\" extradata=\"3\" ip=\"this computer\" port=\"20777\" delay=\"1\" />",
    published="Codemasters' telemetry field list (66 values), as used by the open dr2_logger project",
)


class CodemastersDecoder(Decoder):
    """66 little-endian floats. There are no rotation rates in this format: they are worked out from
    how the car's "right" and "forward" direction vectors move between two packets."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rough = Roughness()
        self._t: float | None = None
        self._r: Vec = (1.0, 0.0, 0.0)
        self._f: Vec = (0.0, 0.0, 1.0)
        self._v: Vec = (0.0, 0.0, 0.0)
        self._stopped = 0

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) != 264:
            self.bad()
            return None
        v = struct.unpack("<66f", data)
        t = v[0]
        vel: Vec = (v[8], v[9], v[10])
        right, forward = _unit((v[11], v[12], v[13])), _unit((v[14], v[15], v[16]))
        up = _unit(_cross(forward, right))
        dt = t - self._t if self._t is not None else 0.0
        rates, heave = (0.0, 0.0, 0.0), 0.0
        if 1e-4 < dt < 0.25:
            df = (_sub(forward, self._f)[0] / dt, _sub(forward, self._f)[1] / dt, _sub(forward, self._f)[2] / dt)
            dr = (_sub(right, self._r)[0] / dt, _sub(right, self._r)[1] / dt, _sub(right, self._r)[2] / dt)
            dv = _sub(vel, self._v)
            rates = (-_dot(dr, up), -_dot(df, up), -_dot(df, right))
            heave = _dot(dv, up) / dt
            self._stopped = 0
        else:
            self._stopped += 1            # the game's clock stands still: paused, or the stage has not begun
        self._t, self._r, self._f, self._v = t, right, forward, vel
        accel = (v[35] * G, -v[34] * G, heave)
        gear = round(v[33])
        sample = LiveSample(
            accel=accel, rates=rates, speed=max(0.0, v[7]), rpm=v[37] * 10.0, rpm_max=v[63] * 10.0,
            rpm_idle=v[64] * 10.0, throttle=v[29], brake=v[31], gear=-1 if gear in (-1, 10) else gear,
            rumble=self._rough.step(heave, dt) if v[7] > 2.0 else 0.0, active=self._stopped < 3, time=t,
        )
        sample.impact = self._hits.step(accel) if sample.active else 0.0
        return sample


# =========================================================================== Project CARS 2 / Automobilista 2
PCARS2 = SourceInfo(
    "pcars2", "Project CARS 2 UDP", "Project CARS 2, Project CARS 3, Automobilista 2 (UDP protocol 'Project CARS 2')",
    "car", 5606,
    setup="Options > System: UDP Protocol Version 'Project CARS 2', UDP Frequency 1 (fastest)",
    published="Slightly Mad Studios: SMS_UDP_Definitions.hpp (Project CARS 2 API)",
)
PCARS2_ROUGH = {2: 0.4, 3: 0.6, 4: 0.8, 5: 0.3, 6: 0.5, 7: 0.5, 8: 0.7, 9: 0.9, 10: 0.8, 11: 0.6, 15: 0.5, 16: 0.7,
                17: 0.5, 18: 0.8, 19: 0.4, 20: 0.7, 22: 0.5, 25: 0.8, 26: 0.4, 27: 0.5, 28: 0.5, 29: 0.6, 30: 0.4,
                35: 0.5, 36: 0.6, 37: 0.8, 40: 0.8, 41: 0.8, 42: 0.6, 43: 0.8}


class Pcars2Decoder(Decoder):
    """Packed structs with a 12-byte base; packet type at byte 10. Telemetry (type 0, 559 bytes) has
    the local acceleration and rotation rate; game state (type 4, 24 bytes) says whether the game is
    being played and how hard it rains or snows. Local axes read as X right, Y up, Z backwards."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._state: int | None = None
        self._rain = self._snow = 0.0

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) < 12:
            self.bad()
            return None
        kind = data[10]
        if kind == 4 and len(data) == 24:
            self._state = data[14] & 0x07
            self._rain, self._snow = data[17] / 255.0, data[18] / 255.0
            return None
        if kind != 0:
            self.ignored += 1
            return None
        if len(data) != 559:
            self.bad()
            return None
        brake, throttle = data[29], data[30]
        (speed,) = struct.unpack_from("<f", data, 36)
        rpm, rpm_max = struct.unpack_from("<HH", data, 40)
        gear = data[45] & 0x0F
        w = struct.unpack_from("<3f", data, 88)
        a = struct.unpack_from("<3f", data, 100)
        accel = (-a[2], -a[0], a[1])
        sample = LiveSample(
            accel=accel, rates=(-w[2], -w[0], w[1]), speed=max(0.0, speed), rpm=float(rpm), rpm_max=float(rpm_max),
            throttle=throttle / 255.0, brake=brake / 255.0, gear=-1 if gear == 15 else gear,
            rumble=max(PCARS2_ROUGH.get(t, 0.0) for t in data[140:144]) if speed > 2.0 else 0.0,
            active=self._state in (None, 2),
        )
        sample.impact = self._hits.step(accel) if sample.active else 0.0
        if self._rain > 0.02:
            sample.effects["rain"] = self._rain
        if self._snow > 0.02:
            sample.effects["snow"] = self._snow
        return sample


# =========================================================================== Assetto Corsa (remote telemetry)
ASSETTO = SourceInfo(
    "assetto", "Assetto Corsa remote telemetry", "Assetto Corsa (the original; not Competizione)", "car", 9997,
    needs_host=True,
    setup="nothing to switch on: this computer asks the game (UDP port 9996 of the game PC) for its data",
    published="Kunos Simulazioni: 'AC UDP Remote Telemetry' document",
)
AC_PORT = 9996
AC_HANDSHAKE, AC_SUBSCRIBE_UPDATE, AC_DISMISS = 0, 1, 3


def ac_request(operation: int) -> bytes:
    return struct.pack("<iii", 1, 1, operation)


class AssettoDecoder(Decoder):
    """RTCarInfo, 328 bytes, begins with 'a' and its own size. The format has g-forces but no
    rotation: the turning rate is worked out from sideways g and speed."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rough = Roughness()
        self._last: float | None = None
        self.greeted = False              # the game answered the handshake
        self.last_car = -1e18

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) == 408:              # the answer to the handshake: car, driver, track names
            self.greeted = True
            return None
        if len(data) != 328 or data[0:1] != b"a" or struct.unpack_from("<i", data, 4)[0] != 328:
            self.bad()
            return None
        (speed,) = struct.unpack_from("<f", data, 16)
        in_pit = bool(data[24])
        g_vert, g_side, g_front = struct.unpack_from("<3f", data, 28)
        gas, brake, _clutch, rpm, _steer = struct.unpack_from("<5f", data, 56)
        (gear,) = struct.unpack_from("<i", data, 76)
        slip = struct.unpack_from("<4f", data, 132)
        dt = now - self._last if self._last is not None else 0.0
        self._last = self.last_car = now
        accel = (g_front * G, -g_side * G, g_vert * G)
        sample = LiveSample(
            accel=accel, rates=(0.0, 0.0, yaw_from_lateral(speed, accel[1])), speed=max(0.0, speed), rpm=rpm,
            throttle=gas, brake=brake, gear=gear - 1, slip=min(1.0, max(abs(s) for s in slip)),
            rumble=self._rough.step(accel[2], dt) if speed > 2.0 else 0.0, active=not in_pit or speed > 1.0,
        )
        sample.impact = self._hits.step(accel)
        return sample


class AssettoSource(UdpSource):
    info = ASSETTO

    def __init__(self, port: int = 9997, bind: str = "0.0.0.0", host: str = "") -> None:
        self.ac = AssettoDecoder()
        super().__init__(self.ac, port, bind, host)
        self._asked = -1e18
        self._subscribed = False

    def idle(self, sock: socket.socket, now: float) -> None:
        if now - self.ac.last_car < 2.0:
            return
        if self.ac.greeted and not self._subscribed:
            sock.sendto(ac_request(AC_SUBSCRIBE_UPDATE), (self.host, AC_PORT))
            self._subscribed = True
            self._asked = now
        elif now - self._asked > 2.0:           # nothing yet (or the game was restarted): ask again
            self.ac.greeted = self._subscribed = False
            sock.sendto(ac_request(AC_HANDSHAKE), (self.host, AC_PORT))
            self._asked = now

    def goodbye(self, sock: socket.socket) -> None:
        if self.host:
            sock.sendto(ac_request(AC_DISMISS), (self.host, AC_PORT))


# =========================================================================== Live for Speed OutSim / OutGauge, BeamNG
OUTSIM = SourceInfo(
    "outsim", "OutSim / OutGauge", "Live for Speed; other programs that send LFS OutSim / OutGauge packets", "car", 4123,
    setup="cfg.txt: OutSim Mode 1, OutSim IP / Port = this computer, OutSim Delay 1; the same for OutGauge (same port)",
    published="Live for Speed: docs/InSim.txt, 'OutSim' and 'OutGauge'",
)
BEAMNG = SourceInfo(
    "beamng", "BeamNG MotionSim / OutGauge", "BeamNG.drive, BeamNG.tech", "car", 4444,
    setup="Options > Other > Protocols: MotionSim enabled and OutGauge enabled, IP of this computer, the same port",
    published="documentation.beamng.com: Modding > Protocols (MotionSim 'BNG1', OutGauge)",
)


class _Gauges:
    """OutGauge (92 bytes, 96 with an id): revs, pedals and gear - shared by LFS and BeamNG."""

    def __init__(self) -> None:
        self.rpm = self.throttle = self.brake = 0.0
        self.gear = 0

    def take(self, data: bytes) -> None:
        self.gear = data[10] - 1
        _speed, self.rpm = struct.unpack_from("<2f", data, 12)
        self.throttle, self.brake = struct.unpack_from("<2f", data, 48)


class OutSimDecoder(Decoder):
    """OutSimPack, 64 bytes (68 with an id): time (ms), angular velocity, heading / pitch / roll,
    acceleration, velocity, position. Acceleration and velocity are in the world's axes (X and Y on
    the ground, Z up) and are turned into the car's with the heading; the rotation rates are the
    changes of heading ("anticlockwise from above"), pitch ("from the right") and roll ("from the
    front"), which is how the document defines their directions."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rough = Roughness()
        self._gauges = _Gauges()
        self._t: int | None = None
        self._att = (0.0, 0.0, 0.0)

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) in (92, 96):
            self._gauges.take(data)
            return None
        if len(data) not in (64, 68):
            self.bad()
            return None
        (stamp,) = struct.unpack_from("<I", data, 0)
        heading, pitch, roll = struct.unpack_from("<3f", data, 16)
        ax, ay, az, vx, vy, vz = struct.unpack_from("<6f", data, 28)
        dt = ((stamp - self._t) & 0xFFFFFFFF) / 1000.0 if self._t is not None else 0.0
        rates = (0.0, 0.0, 0.0)
        if 1e-4 < dt < 0.25:
            rates = (_wrap(roll - self._att[2]) / dt, -_wrap(pitch - self._att[1]) / dt, _wrap(heading - self._att[0]) / dt)
        self._t, self._att = stamp, (heading, pitch, roll)
        sin_h, cos_h = math.sin(heading), math.cos(heading)
        accel = (-ax * sin_h + ay * cos_h, -(ax * cos_h + ay * sin_h), az)
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        g = self._gauges
        sample = LiveSample(
            accel=accel, rates=rates, attitude=(roll, -pitch), speed=speed, rpm=g.rpm, throttle=g.throttle,
            brake=g.brake, gear=g.gear, rumble=self._rough.step(az, dt) if speed > 2.0 else 0.0, time=stamp / 1000.0,
        )
        sample.impact = self._hits.step(accel)
        return sample


class BeamNGDecoder(Decoder):
    """MotionSim: "BNG1" and 21 floats (88 bytes) - position, velocity, acceleration (without gravity),
    the vehicle's up vector, roll / pitch / yaw angles, their rates and accelerations."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rough = Roughness()
        self._gauges = _Gauges()
        self._last: float | None = None

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) in (92, 96):
            self._gauges.take(data)
            return None
        if len(data) != 88 or data[0:4] != b"BNG1":
            self.bad()
            return None
        v = struct.unpack_from("<21f", data, 4)
        dt = now - self._last if self._last is not None else 0.0
        self._last = now
        accel = (v[7], -v[6], v[8])
        speed = math.sqrt(v[3] * v[3] + v[4] * v[4] + v[5] * v[5])
        g = self._gauges
        sample = LiveSample(
            accel=accel, rates=(v[15], -v[16], v[17]), attitude=(v[12], -v[13]), speed=speed, rpm=g.rpm,
            throttle=g.throttle, brake=g.brake, gear=g.gear,
            rumble=self._rough.step(v[8], dt) if speed > 2.0 else 0.0,
        )
        sample.impact = self._hits.step(accel)
        return sample


# =========================================================================== Gran Turismo 7 (experimental)
GT7 = SourceInfo(
    "gt7", "Gran Turismo 7 (experimental)", "Gran Turismo 7 on PlayStation 4 / 5", "car", 33740, needs_host=True,
    setup="nothing to switch on: this computer asks the console (its address is needed) and the game answers",
    published="not published by the game's maker; worked out by the community (GTPlanet; the open gt7telemetry script)",
    experimental=True,
)
GT7_SEND_PORT = 33739
GT7_SIZE = 296
GT7_MAGIC = 0x47375330
_GT7_KEY = b"Simulator Interface Packet GT7 ver 0.0"[:32]
_MASK = 0xFFFFFFFF


def _rotl(v: int, n: int) -> int:
    return ((v << n) & _MASK) | (v >> (32 - n))


def salsa20_block(key: bytes, nonce: bytes, counter: int) -> bytes:
    """One 64-byte block of the Salsa20/20 key stream (256-bit key, 64-bit nonce)."""
    k = struct.unpack("<8I", key)
    n = struct.unpack("<2I", nonce)
    c = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)          # "expand 32-byte k"
    start = [c[0], k[0], k[1], k[2], k[3], c[1], n[0], n[1],
             counter & _MASK, (counter >> 32) & _MASK, c[2], k[4], k[5], k[6], k[7], c[3]]
    x = list(start)
    quarters = ((0, 4, 8, 12), (5, 9, 13, 1), (10, 14, 2, 6), (15, 3, 7, 11),
                (0, 1, 2, 3), (5, 6, 7, 4), (10, 11, 8, 9), (15, 12, 13, 14))
    for _ in range(10):
        for a, b, cc, d in quarters:
            x[b] ^= _rotl((x[a] + x[d]) & _MASK, 7)
            x[cc] ^= _rotl((x[b] + x[a]) & _MASK, 9)
            x[d] ^= _rotl((x[cc] + x[b]) & _MASK, 13)
            x[a] ^= _rotl((x[d] + x[cc]) & _MASK, 18)
    return struct.pack("<16I", *((x[i] + start[i]) & _MASK for i in range(16)))


def gt7_decrypt(data: bytes, blocks: int = 3) -> bytes:
    """The first `blocks` x 64 bytes of a packet in clear (the values used here all lie in them)."""
    (seed,) = struct.unpack_from("<I", data, 0x40)
    nonce = struct.pack("<II", seed ^ 0xDEADBEAF, seed)
    out = bytearray()
    for i in range(blocks):
        stream = salsa20_block(_GT7_KEY, nonce, i)
        out += bytes(a ^ b for a, b in zip(data[64 * i : 64 * i + 64], stream, strict=True))
    return bytes(out)


class GT7Decoder(Decoder):
    """296 encrypted bytes per frame. There is no acceleration in the packet: forwards it is worked
    out from the change of speed, sideways from speed and turning rate."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._slope = Slope()
        self._id: int | None = None

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) != GT7_SIZE:
            self.bad()
            return None
        clear = gt7_decrypt(data)
        if struct.unpack_from("<I", clear, 0)[0] != GT7_MAGIC:
            self.bad()
            return None
        wx, wy, wz = struct.unpack_from("<3f", clear, 0x2C)
        (rpm,) = struct.unpack_from("<f", clear, 0x3C)
        (speed,) = struct.unpack_from("<f", clear, 0x4C)
        (frame,) = struct.unpack_from("<i", clear, 0x70)
        (rpm_max,) = struct.unpack_from("<H", clear, 0x8A)
        flags, gear, throttle, brake = clear[0x8E], clear[0x90] & 0x0F, clear[0x91], clear[0x92]
        dt = (frame - self._id) / 60.0 if self._id is not None else 0.0
        self._id = frame
        if not 0.0 < dt < 0.25:
            dt = 0.0
        yaw = -wy
        accel = (self._slope.step(speed, dt), lateral_from_yaw(speed, yaw), 0.0)
        sample = LiveSample(
            accel=accel, rates=(-wz, wx, yaw), speed=max(0.0, speed), rpm=rpm, rpm_max=float(rpm_max),
            throttle=throttle / 255.0, brake=brake / 255.0, gear=-1 if gear == 0 else gear,
            active=bool(flags & 1) and not flags & 2,
        )
        sample.impact = self._hits.step(accel) if sample.active else 0.0
        return sample


class GT7Source(UdpSource):
    info = GT7

    def __init__(self, port: int = 33740, bind: str = "0.0.0.0", host: str = "") -> None:
        super().__init__(GT7Decoder(), port, bind, host)
        self._beat = -1e18

    def idle(self, sock: socket.socket, now: float) -> None:
        if now - self._beat >= 1.0:       # the game sends for a while after each "A" and then stops
            self._beat = now
            sock.sendto(b"A", (self.host, GT7_SEND_PORT))
