"""A made-up drive, and packets of every supported format that describe it.

Two uses:

* **tests** - each format's packets are built here from the published layout and must come out of
  the decoder as the drive that went in. That proves that sizes, offsets and the arithmetic of both
  directions agree with each other; it cannot prove that a game means the same by a sign as this
  program assumes (docs/LIVE.md, "What was not tested").
* **the bench** - ``fourdvr live sim --format forza --to 127.0.0.1:5300`` plays the game's part, so a
  venue can try platforms, effects and shakers before the real game is there.

The drive is kinematically consistent: sideways acceleration is speed x turning rate and so on, and
the vehicle's attitude is the integral of its rotation rates - formats that carry only speed or only
direction vectors must give the same answer as formats that carry accelerations.
"""

from __future__ import annotations

import math
import socket
import struct
import time
from dataclasses import dataclass, field

import numpy as np

from .sample import G, LiveSample
from .sources import flight, openformat, racing

LAP_S = 40.0
RATE = 200.0          # the drive is worked out on this grid


@dataclass
class SimState:
    """The drive at one instant: what the vehicle does (sample) and where it is (world: x east,
    y north, z up; heading anticlockwise from east; all in the platform's sign conventions)."""

    t: float
    sample: LiveSample
    heading: float
    pitch: float              # + nose down
    roll: float               # + right side down
    velocity: tuple[float, float, float]
    position: tuple[float, float, float]
    frame: int = 0


def _ramp(t: np.ndarray, t0: float, t1: float, width: float = 0.6) -> np.ndarray:
    """1 between t0 and t1 with smooth shoulders, 0 elsewhere."""
    up = np.clip((t - t0) / width, 0.0, 1.0)
    down = np.clip((t1 - t) / width, 0.0, 1.0)
    s = np.minimum(up, down)
    return s * s * (3.0 - 2.0 * s)


class Drive:
    """A 40-second lap that ends as it begins: pull away, a left bend, a rough stretch, a right
    bend, a crest and a dip, rain, hard braking with a bump at the end."""

    def __init__(self) -> None:
        n = int(LAP_S * RATE)
        t = np.arange(n) / RATE
        self.hit_at, self.hit_s, self.hit_ms2 = 29.0, 0.025, 45.0     # a collision: 45 m/s^2 for 25 ms
        surge = (3.2 * _ramp(t, 0.5, 6.5) - 6.0 * _ramp(t, 30.0, 33.2)
                 + 1.5 * _ramp(t, 20.0, 23.0) - 1.5 * _ramp(t, 25.0, 28.0)
                 - self.hit_ms2 * ((t >= self.hit_at) & (t < self.hit_at + self.hit_s)))
        speed = np.cumsum(surge) / RATE
        speed -= np.linspace(0.0, speed[-1], n)          # close the lap exactly at rest
        speed = np.maximum(speed, 0.0)
        surge = np.gradient(speed, 1.0 / RATE)
        yaw = 0.32 * _ramp(t, 8.0, 12.0) - 0.36 * _ramp(t, 16.0, 19.5)
        pitch = -0.10 * _ramp(t, 22.0, 23.2, 0.4) + 0.20 * _ramp(t, 23.4, 24.6, 0.4) - 0.10 * _ramp(t, 24.8, 26.0, 0.4)
        roll = np.gradient(0.03 * yaw / 0.36, 1.0 / RATE)         # the body leans a little in the bends
        rough = _ramp(t, 12.5, 15.5, 0.3)
        bump = 2.2 * rough * np.sin(2.0 * math.pi * 6.0 * t)
        bump_v = np.cumsum(bump) / RATE
        self.t = t
        self.speed, self.surge = speed, surge
        self.rates = np.stack([roll, pitch, yaw], axis=1)
        self.sway = speed * yaw
        self.heave = -speed * pitch + bump
        self.bump_v = bump_v
        self.rough = rough
        self.rain = 0.6 * (np.abs(t - 26.0) < 2.0)
        # attitude: integral of the body rates (Z-Y-X Euler angles)
        att = np.zeros((n, 3))                 # heading, pitch, roll
        pos = np.zeros((n, 3))
        for i in range(1, n):
            h, th, ph = att[i - 1]
            p, q, r = self.rates[i - 1]
            ph_d = p + (q * math.sin(ph) + r * math.cos(ph)) * math.tan(th)
            th_d = q * math.cos(ph) - r * math.sin(ph)
            h_d = (q * math.sin(ph) + r * math.cos(ph)) / math.cos(th)
            att[i] = (h + h_d / RATE, th + th_d / RATE, ph + ph_d / RATE)
        self.att = att
        self.vel = np.zeros((n, 3))
        for i in range(n):
            fwd, _left, up = body_axes(att[i, 0], att[i, 1], att[i, 2])
            self.vel[i] = speed[i] * np.asarray(fwd) + bump_v[i] * np.asarray(up)
        pos[1:] = np.cumsum(self.vel[:-1], axis=0) / RATE
        self.pos = pos

    def at(self, t: float, loop: bool = True) -> SimState:
        tt = t % LAP_S if loop else min(t, LAP_S - 1.0 / RATE)
        i = min(int(tt * RATE), len(self.t) - 1)
        speed = float(self.speed[i])
        rpm_max, rpm_idle = 7200.0, 900.0
        band = (speed % 14.0) / 14.0                     # revs climb through each "gear"
        moving = speed > 0.3
        rpm = rpm_idle + (rpm_max - rpm_idle) * (0.25 + 0.7 * band) if moving else rpm_idle
        surge = float(self.surge[i])
        sample = LiveSample(
            accel=(surge, float(self.sway[i]), float(self.heave[i])),
            rates=(float(self.rates[i, 0]), float(self.rates[i, 1]), float(self.rates[i, 2])),
            attitude=(float(self.att[i, 2]), float(self.att[i, 1])),
            speed=speed, rpm=rpm, rpm_max=rpm_max, rpm_idle=rpm_idle,
            throttle=min(1.0, max(0.0, surge / 3.2)) if moving else 0.0, brake=min(1.0, max(0.0, -surge / 6.0)),
            gear=1 + int(speed // 14.0) if moving else 0,
            rumble=0.85 * float(self.rough[i]),
            impact=0.64 if self.hit_at <= tt < self.hit_at + self.hit_s else 0.0, time=t,
        )
        if self.rain[i] > 0.0:
            sample.effects["rain"] = float(self.rain[i])
        return SimState(t, sample, float(self.att[i, 0]), float(self.att[i, 1]), float(self.att[i, 2]),
                        (float(self.vel[i, 0]), float(self.vel[i, 1]), float(self.vel[i, 2])),
                        (float(self.pos[i, 0]), float(self.pos[i, 1]), float(self.pos[i, 2])),
                        frame=round(t * 60.0))


def body_axes(heading: float, pitch: float, roll: float) -> tuple[tuple[float, float, float], ...]:
    """Forward, left and up of the vehicle in the world (x east, y north, z up), for a heading
    (anticlockwise from east), a pitch (+ nose down) and a roll (+ right side down)."""
    ch, sh = math.cos(heading), math.sin(heading)
    ct, st = math.cos(pitch), math.sin(pitch)
    cp, sp = math.cos(roll), math.sin(roll)
    fwd = (ch * ct, sh * ct, -st)
    left = (ch * st * sp - sh * cp, sh * st * sp + ch * cp, ct * sp)
    up = (ch * st * cp + sh * sp, sh * st * cp - ch * sp, ct * cp)
    return fwd, left, up


# =========================================================================== encoders (one frame -> packets)
def encode_open(s: SimState) -> list[bytes]:
    return [openformat.encode(s.sample)]


def encode_forza(s: SimState, size: int = 311) -> list[bytes]:
    a, w, x = s.sample.accel, s.sample.rates, s.sample
    shift = racing.FORZA_SIZES[size]
    buf = bytearray(size)
    struct.pack_into("<iI", buf, 0, 1 if x.active else 0, int(s.t * 1000.0) & 0xFFFFFFFF)
    struct.pack_into("<3f", buf, 8, x.rpm_max, x.rpm_idle, x.rpm)
    struct.pack_into("<12f", buf, 20, -a[1], a[2], a[0], 0.0, 0.0, x.speed, w[1], -w[2], -w[0],
                     -s.heading, s.pitch, -s.roll)
    struct.pack_into("<4i", buf, 116, 0, 0, 0, 0)
    splash = x.effects.get("splash", 0.0)
    struct.pack_into("<4f", buf, 132, splash, 0.0, 0.0, 0.0)
    struct.pack_into("<4f", buf, 148, x.rumble, x.rumble, x.rumble, x.rumble)
    struct.pack_into("<4f", buf, 180, 1.0 + x.slip, 0.0, 0.0, 0.0)
    struct.pack_into("<5i", buf, 212, 2001, 4, 700, 1, 6)
    if size > 232:
        struct.pack_into("<f", buf, 244 + shift, x.speed)
        struct.pack_into("<5B", buf, 303 + shift, round(x.throttle * 255), round(x.brake * 255), 0, 0,
                         0 if x.gear < 0 else x.gear)
    return [bytes(buf)]


def _f1_header(fmt: int, kind: int, size: int, s: SimState, player: int) -> bytearray:
    buf = bytearray(size)
    struct.pack_into("<HBBBBBQfIIBB", buf, 0, fmt, fmt % 100, 1, 0, 1, kind, 0x4D4F54494F4E, s.t, s.frame, s.frame,
                     player, 255)
    return buf


def encode_f1(s: SimState, fmt: int = 2025, player: int = 19) -> list[bytes]:
    x, a, w = s.sample, s.sample.accel, s.sample.rates
    out: list[bytes] = []
    if s.frame % 30 == 0:
        session = _f1_header(fmt, racing.F1_SESSION, 644 if fmt == 2023 else 753, s, player)
        rain = x.effects.get("rain", 0.0)
        session[29] = 5 if rain >= 0.9 else 4 if rain >= 0.6 else 3 if rain > 0.0 else 0
        status = _f1_header(fmt, racing.F1_STATUS, 1239, s, player)
        struct.pack_into("<HH", status, 29 + 55 * player + 17, int(x.rpm_max), int(x.rpm_idle))
        out += [bytes(session), bytes(status)]
    ex = _f1_header(fmt, racing.F1_MOTION_EX, {2023: 217, 2024: 237, 2025: 273}[fmt], s, player)
    struct.pack_into("<3f", ex, 173, w[1], -w[2], -w[0])
    tel = _f1_header(fmt, racing.F1_TELEMETRY, 1352, s, player)
    base = 29 + 60 * player
    struct.pack_into("<HfffBbH", tel, base, round(x.speed * 3.6), x.throttle, 0.0, x.brake, 0, x.gear, round(x.rpm))
    tel[base + 56 : base + 60] = bytes([1 if x.rumble >= 0.75 else 0] * 4)
    motion = _f1_header(fmt, racing.F1_MOTION, 1349, s, player)
    struct.pack_into("<6f", motion, base + 36, -a[1] / G, a[0] / G, a[2] / G, s.heading, s.pitch, -s.roll)
    return [*out, bytes(ex), bytes(tel), bytes(motion)]


def _yup(v: tuple[float, float, float]) -> tuple[float, float, float]:
    """World vector (east, north, up) in a Y-up, left-handed game world (x east, y up, z north)."""
    return (v[0], v[2], v[1])


def encode_dirt(s: SimState) -> list[bytes]:
    x = s.sample
    fwd, left, _up = body_axes(s.heading, s.pitch, s.roll)
    right = (-left[0], -left[1], -left[2])
    v = [0.0] * 66
    v[0] = s.t if x.active else 0.0
    v[4:7] = _yup(s.position)
    v[7] = x.speed
    v[8:11] = _yup(s.velocity)
    v[11:14] = _yup(right)
    v[14:17] = _yup(fwd)
    v[29], v[31], v[33] = x.throttle, x.brake, 10.0 if x.gear < 0 else float(x.gear)
    v[34], v[35] = -x.accel[1] / G, x.accel[0] / G
    v[37], v[63], v[64], v[65] = x.rpm / 10.0, x.rpm_max / 10.0, x.rpm_idle / 10.0, 6.0
    return [struct.pack("<66f", *v)]


def encode_pcars2(s: SimState) -> list[bytes]:
    x, a, w = s.sample, s.sample.accel, s.sample.rates
    out: list[bytes] = []
    if s.frame % 30 == 0:
        state = bytearray(24)
        struct.pack_into("<IIBBBB", state, 0, s.frame, s.frame, 1, 1, 4, 2)
        state[14] = 2 if x.active else 3
        state[17], state[18] = round(x.effects.get("rain", 0.0) * 255), round(x.effects.get("snow", 0.0) * 255)
        out.append(bytes(state))
    buf = bytearray(559)
    struct.pack_into("<IIBBBB", buf, 0, s.frame, s.frame, 1, 1, 0, 3)
    buf[29], buf[30] = round(x.brake * 255), round(x.throttle * 255)
    struct.pack_into("<f", buf, 36, x.speed)
    struct.pack_into("<HH", buf, 40, round(x.rpm), round(x.rpm_max))
    buf[45] = (6 << 4) | (15 if x.gear < 0 else x.gear & 0x0F)
    struct.pack_into("<3f", buf, 88, -w[1], w[2], -w[0])
    struct.pack_into("<3f", buf, 100, -a[1], a[2], -a[0])
    buf[140:144] = bytes([10 if x.rumble >= 0.75 else 0] * 4)
    return [*out, bytes(buf)]


def encode_assetto(s: SimState) -> list[bytes]:
    x, a = s.sample, s.sample.accel
    buf = bytearray(328)
    buf[0:1] = b"a"
    struct.pack_into("<i", buf, 4, 328)
    struct.pack_into("<3f", buf, 8, x.speed * 3.6, x.speed * 2.23694, x.speed)
    struct.pack_into("<3f", buf, 28, a[2] / G, -a[1] / G, a[0] / G)
    struct.pack_into("<5f", buf, 56, x.throttle, x.brake, 0.0, x.rpm, 0.0)
    struct.pack_into("<i", buf, 76, x.gear + 1)
    struct.pack_into("<4f", buf, 132, x.slip, x.slip, x.slip, x.slip)
    return [bytes(buf)]


def assetto_greeting() -> bytes:
    """The game's answer to a handshake: car, driver, identifier, version, track, layout (408 bytes)."""
    def name(text: str) -> bytes:
        return text.encode("utf-16-le").ljust(100, b"\0")
    return name("simulated car") + name("driver") + struct.pack("<ii", 4242, 1) + name("test track") + name("lap")


def _outgauge(s: SimState) -> bytes:
    x = s.sample
    buf = bytearray(92)
    struct.pack_into("<I4sHBB", buf, 0, int(s.t * 1000.0) & 0xFFFFFFFF, b"sim\0", 0, x.gear + 1, 0)
    struct.pack_into("<2f", buf, 12, x.speed, x.rpm)
    struct.pack_into("<2f", buf, 48, x.throttle, x.brake)
    return bytes(buf)


def encode_outsim(s: SimState, with_id: bool = False) -> list[bytes]:
    fwd, left, up = body_axes(s.heading, s.pitch, s.roll)
    a = s.sample.accel
    world = tuple(a[0] * fwd[k] + a[1] * left[k] + a[2] * up[k] for k in range(3))
    buf = bytearray(68 if with_id else 64)
    struct.pack_into("<I", buf, 0, round(s.t * 1000.0) & 0xFFFFFFFF)
    struct.pack_into("<3f", buf, 16, s.heading - math.pi / 2.0, -s.pitch, s.roll)
    struct.pack_into("<6f", buf, 28, *world, *s.velocity)
    struct.pack_into("<3i", buf, 52, *(int(p * 65536.0) for p in s.position))
    return [_outgauge(s), bytes(buf)]


def encode_beamng(s: SimState) -> list[bytes]:
    a, w = s.sample.accel, s.sample.rates
    _fwd, _left, up = body_axes(s.heading, s.pitch, s.roll)
    values = (*s.position, *s.velocity, -a[1], a[0], a[2], *up, s.roll, -s.pitch, s.heading, w[0], -w[1], w[2],
              0.0, 0.0, 0.0)
    return [_outgauge(s), b"BNG1" + struct.pack("<21f", *values)]


def encode_xplane(s: SimState) -> list[bytes]:
    x, a, w = s.sample, s.sample.accel, s.sample.rates
    none = -999.0

    def row(index: int, *values: float) -> bytes:
        return struct.pack("<i8f", index, *(list(values) + [none] * (8 - len(values))))

    return [b"DATA*" + row(3, x.speed / flight.KNOT, none, none, x.speed / flight.KNOT)
            + row(4, 0.1, none, 0.0, none, a[2] / G + 1.0, a[0] / G, -a[1] / G)
            + row(16, -w[1], w[0], -w[2]) + row(17, -math.degrees(s.pitch), math.degrees(s.roll), 0.0, 0.0)
            + row(37, x.rpm)]


def encode_flightgear(s: SimState) -> list[bytes]:
    x, a, w = s.sample, s.sample.accel, s.sample.rates
    values = (a[0] / flight.FOOT, -a[1] / flight.FOOT, -(a[2] + G) / flight.FOOT, math.degrees(w[0]),
              -math.degrees(w[1]), -math.degrees(w[2]), math.degrees(s.roll), -math.degrees(s.pitch),
              x.speed / flight.KNOT, x.speed / flight.KNOT, x.rpm, 1, 0 if x.active else 1,
              x.effects.get("rain", 0.0), x.effects.get("snow", 0.0))
    return [(flight.FG_MAGIC + "," + ",".join(f"{v:.4f}" if isinstance(v, float) else str(v) for v in values) + "\n").encode()]


def encode_gt7(s: SimState) -> list[bytes]:
    x, w = s.sample, s.sample.rates
    clear = bytearray(racing.GT7_SIZE)
    struct.pack_into("<I", clear, 0, racing.GT7_MAGIC)
    struct.pack_into("<3f", clear, 0x10, *_yup(s.velocity))
    struct.pack_into("<3f", clear, 0x2C, w[1], -w[2], -w[0])
    struct.pack_into("<f", clear, 0x3C, x.rpm)
    struct.pack_into("<f", clear, 0x4C, x.speed)
    struct.pack_into("<i", clear, 0x70, s.frame)
    struct.pack_into("<H", clear, 0x8A, round(x.rpm_max))
    clear[0x8E] = 1 if x.active else 3
    clear[0x90] = 0 if x.gear < 0 else x.gear & 0x0F
    clear[0x91], clear[0x92] = round(x.throttle * 255), round(x.brake * 255)
    seed = (0x12345678 + s.frame * 2654435761) & 0xFFFFFFFF
    nonce = struct.pack("<II", seed ^ 0xDEADBEAF, seed)
    out = bytearray()
    for i in range(0, racing.GT7_SIZE, 64):
        stream = racing.salsa20_block(racing._GT7_KEY, nonce, i // 64)
        out += bytes(c ^ k for c, k in zip(clear[i : i + 64], stream, strict=False))
    struct.pack_into("<I", out, 0x40, seed)
    return [bytes(out)]


ENCODERS = {
    "open": encode_open, "forza": encode_forza, "f1": encode_f1, "dirt": encode_dirt, "pcars2": encode_pcars2,
    "assetto": encode_assetto, "outsim": encode_outsim, "beamng": encode_beamng, "xplane": encode_xplane,
    "flightgear": encode_flightgear, "gt7": encode_gt7,
}


# =========================================================================== playing the game's part
@dataclass
class SimSender:
    """Sends the drive as packets of one format, in real time, like the game would."""

    format: str
    host: str = "127.0.0.1"
    port: int = 0
    rate_hz: float = 60.0
    drive: Drive = field(default_factory=Drive)
    sent: int = 0

    def run(self, seconds: float = 0.0, stop: object = None) -> int:
        """Send for `seconds` (0: until `stop.is_set()` or Ctrl+C). Returns the number of packets."""
        from .sources import INFOS, canonical

        name = canonical(self.format)
        if name not in ENCODERS:
            raise ValueError(f"no simulator for source {name!r}")
        encode = ENCODERS[name]
        port = self.port or INFOS[name].port
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target: tuple[str, int] | None = (self.host, port)
        if name == "assetto":             # the game waits to be asked
            sock.bind(("0.0.0.0", racing.AC_PORT))
            target = None
        elif name == "gt7":
            sock.bind(("0.0.0.0", racing.GT7_SEND_PORT))
            target = None
        sock.settimeout(0.0)
        start = time.monotonic()
        frame = 0
        try:
            while True:
                now = time.monotonic()
                t = now - start
                if (seconds and t >= seconds) or (stop is not None and stop.is_set()):  # type: ignore[attr-defined]
                    break
                if name in ("assetto", "gt7"):
                    target = self._requests(sock, name, target)
                state = self.drive.at(frame / self.rate_hz)
                state.frame = frame
                if target is not None:
                    for packet in encode(state):
                        sock.sendto(packet, target)
                        self.sent += 1
                frame += 1
                delay = start + frame / self.rate_hz - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except KeyboardInterrupt:
            pass
        finally:
            sock.close()
        return self.sent

    def _requests(self, sock: socket.socket, name: str, target: tuple[str, int] | None) -> tuple[str, int] | None:
        while True:
            try:
                data, addr = sock.recvfrom(64)
            except (BlockingIOError, TimeoutError):
                return target
            if name == "gt7" and data[:1] == b"A":
                target = (addr[0], self.port or racing.GT7.port)
            elif name == "assetto" and len(data) == 12:
                operation = struct.unpack("<iii", data)[2]
                if operation == racing.AC_HANDSHAKE:
                    sock.sendto(assetto_greeting(), addr)
                elif operation == racing.AC_SUBSCRIBE_UPDATE:
                    target = addr
                elif operation == racing.AC_DISMISS:
                    target = None
