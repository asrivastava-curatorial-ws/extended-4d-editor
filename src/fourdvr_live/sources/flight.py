"""Telemetry of flight simulators.

Rotation rates follow the aeronautical convention that both simulators document (roll rate P
positive right wing down, pitch rate Q positive nose up, yaw rate R positive nose right); they are
turned into the platform's directions here. The directions of the sideways and lengthways g-loads of
X-Plane are not stated by its maker - check them with ``fourdvr live monitor`` (docs/LIVE.md).
"""

from __future__ import annotations

import math
import struct

from ..sample import G, LiveSample
from .base import Decoder, Hits, Roughness, SourceInfo

KNOT = 0.514444
FOOT = 0.3048

# =========================================================================== X-Plane "DATA" output
XPLANE = SourceInfo(
    "xplane", "X-Plane data output", "X-Plane 10, 11, 12", "aircraft", 49005,
    setup="Settings > Data Output: tick 'Network via UDP' for rows 3, 4, 16, 17 (and 37), send to this computer's IP "
          "and port, rate 50-99 per second",
    published="Laminar Research: X-Plane's Data Output screen and 'Exchanging Data with X-Plane'",
)
XPLANE_ROWS = {3: "speeds", 4: "Mach, VVI, g-load", 16: "angular velocities", 17: "pitch, roll, headings", 37: "engine RPM"}


class XPlaneDecoder(Decoder):
    """"DATA" + one byte, then rows of 36 bytes: a row number and eight floats. Used rows: 3 (speeds),
    4 (g-loads: normal, axial, side), 16 (Q, P, R in rad/s), 17 (pitch, roll in degrees), 37 (RPM)."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rough = Roughness()
        self._last: float | None = None
        self._speed = self._rpm = 0.0
        self._accel = (0.0, 0.0, 0.0)
        self._rates = (0.0, 0.0, 0.0)
        self._attitude: tuple[float, float] | None = None
        self.rows: set[int] = set()

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        if len(data) < 41 or data[0:4] != b"DATA" or (len(data) - 5) % 36:
            self.bad()
            return None
        motion = False
        for offset in range(5, len(data), 36):
            (row,) = struct.unpack_from("<i", data, offset)
            v = struct.unpack_from("<8f", data, offset + 4)
            self.rows.add(row)
            if row == 3:
                self._speed = max(0.0, v[0]) * KNOT
            elif row == 4:
                self._accel = (v[5] * G, -v[6] * G, (v[4] - 1.0) * G)
                motion = True
            elif row == 16:
                self._rates = (v[1], -v[0], -v[2])
                motion = True
            elif row == 17:
                self._attitude = (math.radians(v[1]), -math.radians(v[0]))
            elif row == 37:
                self._rpm = max(0.0, v[0])
        if not motion:
            self.ignored += 1
            return None
        dt = now - self._last if self._last is not None else 0.0
        self._last = now
        sample = LiveSample(accel=self._accel, rates=self._rates, attitude=self._attitude, speed=self._speed,
                            rpm=self._rpm, rpm_max=2700.0 if self._rpm > 0.0 else 0.0, throttle=0.6,
                            rumble=self._rough.step(self._accel[2], dt))
        sample.impact = self._hits.step(self._accel)
        return sample


# =========================================================================== FlightGear generic protocol
FLIGHTGEAR = SourceInfo(
    "flightgear", "FlightGear generic protocol", "FlightGear", "aircraft", 47011,
    setup="copy fourdvr.xml ('fourdvr live flightgear-xml') into FlightGear's Protocol folder and start it with "
          "--generic=socket,out,60,<this computer>,47011,udp,fourdvr",
    published="FlightGear: generic protocol (README.protocol; wiki 'Generic protocol')",
)
FG_MAGIC = "4DFG1"
FG_CHUNKS = (
    # name, property, type, format
    ("x accel (forward, ft/s2)", "/accelerations/pilot/x-accel-fps_sec", "float", "%.4f"),
    ("y accel (right, ft/s2)", "/accelerations/pilot/y-accel-fps_sec", "float", "%.4f"),
    ("z accel (down, ft/s2)", "/accelerations/pilot/z-accel-fps_sec", "float", "%.4f"),
    ("roll rate (deg/s)", "/orientation/roll-rate-degps", "float", "%.4f"),
    ("pitch rate (deg/s)", "/orientation/pitch-rate-degps", "float", "%.4f"),
    ("yaw rate (deg/s)", "/orientation/yaw-rate-degps", "float", "%.4f"),
    ("roll (deg)", "/orientation/roll-deg", "float", "%.3f"),
    ("pitch (deg)", "/orientation/pitch-deg", "float", "%.3f"),
    ("airspeed (kt)", "/velocities/airspeed-kt", "float", "%.2f"),
    ("ground speed (kt)", "/velocities/groundspeed-kt", "float", "%.2f"),
    ("engine rpm", "/engines/engine/rpm", "float", "%.1f"),
    ("weight on wheels", "/gear/gear/wow", "bool", "%d"),
    ("paused", "/sim/freeze/master", "bool", "%d"),
    ("rain", "/environment/rain-norm", "float", "%.3f"),
    ("snow", "/environment/snow-norm", "float", "%.3f"),
)


def flightgear_protocol_xml() -> str:
    """The protocol file FlightGear needs (Protocol/fourdvr.xml): one comma-separated line per frame."""
    chunks = [f"   <chunk>\n    <name>format</name>\n    <format>{FG_MAGIC}</format>\n   </chunk>"]
    for name, node, kind, fmt in FG_CHUNKS:
        chunks.append(f"   <chunk>\n    <name>{name}</name>\n    <type>{kind}</type>\n    <format>{fmt}</format>\n"
                      f"    <node>{node}</node>\n   </chunk>")
    # (an XML comment must not contain two hyphens in a row, so the option is described, not quoted)
    return ('<?xml version="1.0"?>\n<!-- Extended 4D Editor: live telemetry for the motion platform, effects and '
            'shakers.\n     Start FlightGear with the "generic" option: socket,out,60,HOST,47011,udp,fourdvr -->\n'
            "<PropertyList>\n <generic>\n  <output>\n   <line_separator>newline</line_separator>\n"
            "   <var_separator>,</var_separator>\n" + "\n".join(chunks) + "\n  </output>\n </generic>\n</PropertyList>\n")


class FlightGearDecoder(Decoder):
    """One text line per frame, as written by our protocol file: "4DFG1" and fifteen numbers. The
    pilot accelerations are in body axes (X forward, Y right, Z down) and include the reaction to
    gravity, so 1 g is taken out of the vertical one."""

    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._rough = Roughness()
        self._last: float | None = None
        self._on_ground = False

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        try:
            parts = data.decode("ascii").strip().split(",")
            if parts[0] != FG_MAGIC or len(parts) != len(FG_CHUNKS) + 1:
                raise ValueError("not a line of the fourdvr protocol")
            v = [float(p) for p in parts[1:]]
        except (UnicodeDecodeError, ValueError):
            self.bad()
            return None
        first = self._last is None
        dt = 0.0 if self._last is None else now - self._last
        self._last = now
        accel = (v[0] * FOOT, -v[1] * FOOT, -v[2] * FOOT - G)
        on_ground = v[11] != 0.0
        rolling = on_ground and v[9] * KNOT > 3.0
        sample = LiveSample(
            accel=accel, rates=(math.radians(v[3]), -math.radians(v[4]), -math.radians(v[5])),
            attitude=(math.radians(v[6]), -math.radians(v[7])), speed=max(0.0, v[8]) * KNOT, rpm=max(0.0, v[10]),
            rpm_max=2700.0 if v[10] > 0.0 else 0.0, throttle=0.6,
            rumble=max(self._rough.step(accel[2], dt), 0.25 if rolling else 0.0), airborne=not on_ground,
            active=v[12] == 0.0,
        )
        touchdown = on_ground and not self._on_ground and not first
        sample.impact = max(self._hits.step(accel), 0.6 if touchdown else 0.0)
        self._on_ground = on_ground
        if v[13] > 0.02:
            sample.effects["rain"] = min(1.0, v[13])
        if v[14] > 0.02:
            sample.effects["snow"] = min(1.0, v[14])
        return sample
