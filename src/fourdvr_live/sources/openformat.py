"""The open live format of this program ("4DLIVE"): one small JSON object per UDP datagram.

For anything that has no telemetry of its own - a VR experience made in Godot, Unity or Unreal, a
simulator of your own, a show controller: a dozen lines in the experience send what the platform,
the effects and the shakers should feel. ``fourdvr live sender godot|unity|python`` prints a ready
sender. The reference is docs/LIVE_FORMAT.md.

    {"4dlive": 1, "accel": [1.2, -0.4, 0.0], "rates": [0.0, 0.02, 0.31], "speed": 14.0,
     "rpm": 3200, "rpm_max": 7000, "throttle": 0.8, "rumble": 0.2, "effects": {"rain": 0.6}}

Directions and units are those of the platform (see fourdvr_live.sample). Every key but "4dlive"
is optional. Instead of "accel" a sender may give "velocity" (m/s, same axes): the acceleration is
then worked out here, including what turning adds.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..sample import G, LiveSample
from .base import Decoder, Hits, SourceInfo

OPEN = SourceInfo(
    "open", "Open live format (4DLIVE)", "your own VR experience or simulator (Godot, Unity, Unreal, anything "
    "that can send UDP)", "any", 47010,
    setup="add the sender ('fourdvr live sender godot' / 'unity' / 'python') to the experience",
    published="docs/LIVE_FORMAT.md (this program)",
)
FORMAT_KEY = "4dlive"
FORMAT_VERSION = 1
MAX_BYTES = 4096


def _triple(value: Any, what: str) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"{what} must be three numbers")
    return (float(value[0]), float(value[1]), float(value[2]))


class OpenDecoder(Decoder):
    def __init__(self) -> None:
        super().__init__()
        self._hits = Hits()
        self._vel: tuple[float, float, float] | None = None
        self._t: float | None = None
        self.last_problem = ""

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        try:
            if len(data) > MAX_BYTES:
                raise ValueError("datagram too long")
            msg = json.loads(data.decode("utf-8"))
            if not isinstance(msg, dict) or int(msg.get(FORMAT_KEY, 0)) != FORMAT_VERSION:
                raise ValueError(f'"{FORMAT_KEY}": {FORMAT_VERSION} is missing')
            return self._sample(msg, now)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self.last_problem = str(exc)
            self.bad()
            return None

    def _sample(self, msg: dict[str, Any], now: float) -> LiveSample:
        t = float(msg["t"]) if "t" in msg else None
        clock = t if t is not None else now
        dt = clock - self._t if self._t is not None else 0.0
        self._t = clock
        if "rates_deg" in msg:
            r = _triple(msg["rates_deg"], "rates_deg")
            rates = (math.radians(r[0]), math.radians(r[1]), math.radians(r[2]))
        else:
            rates = _triple(msg.get("rates", (0.0, 0.0, 0.0)), "rates")
        if "accel" in msg:
            accel = _triple(msg["accel"], "accel")
            self._vel = None
        elif "accel_g" in msg:
            a = _triple(msg["accel_g"], "accel_g")
            accel = (a[0] * G, a[1] * G, a[2] * G)
            self._vel = None
        elif "velocity" in msg:
            v = _triple(msg["velocity"], "velocity")
            last, self._vel = self._vel, v
            if last is not None and 1e-4 < dt < 0.25:
                # a = dv/dt + omega x v in the vehicle's own axes (x forward, y left, z up): the platform's
                # roll, pitch and yaw are the right-handed rotations about exactly these axes
                wx, wy, wz = rates
                accel = ((v[0] - last[0]) / dt + wy * v[2] - wz * v[1],
                         (v[1] - last[1]) / dt + wz * v[0] - wx * v[2],
                         (v[2] - last[2]) / dt + wx * v[1] - wy * v[0])
            else:
                accel = (0.0, 0.0, 0.0)
        else:
            accel = (0.0, 0.0, 0.0)
        speed = float(msg["speed"]) if "speed" in msg else (math.sqrt(sum(c * c for c in self._vel)) if self._vel else 0.0)
        sample = LiveSample(
            accel=accel, rates=rates, speed=abs(speed), rpm=float(msg.get("rpm", 0.0)),
            rpm_max=float(msg.get("rpm_max", 0.0)), rpm_idle=float(msg.get("rpm_idle", 0.0)),
            throttle=float(msg.get("throttle", 0.0)), brake=float(msg.get("brake", 0.0)), gear=int(msg.get("gear", 0)),
            rumble=float(msg.get("rumble", 0.0)), slip=float(msg.get("slip", 0.0)), airborne=bool(msg.get("airborne", False)),
            active=bool(msg.get("active", True)), time=t,
        )
        if "attitude" in msg:
            a2 = msg["attitude"]
            if not isinstance(a2, list | tuple) or len(a2) != 2:
                raise ValueError("attitude must be two numbers (roll, pitch)")
            sample.attitude = (float(a2[0]), float(a2[1]))
        sample.impact = float(msg["impact"]) if "impact" in msg else self._hits.step(accel)
        effects = msg.get("effects", {})
        if not isinstance(effects, dict):
            raise ValueError("effects must be an object of levels")
        for name, value in effects.items():
            sample.effects[str(name)] = float(value)
        if "scent" in msg:                         # which cartridge a stated "scent" level means
            sample.params["scent"] = {"slot": float(int(msg["scent"]))}
        if "strobe_hz" in msg:
            sample.params["strobe"] = {"rate_hz": float(msg["strobe_hz"])}
        return sample


def encode(sample: LiveSample) -> bytes:
    """A sample as a datagram of the open format (used by the simulator, the tests and the Python sender)."""
    msg: dict[str, Any] = {
        FORMAT_KEY: FORMAT_VERSION,
        "accel": [round(v, 4) for v in sample.accel], "rates": [round(v, 5) for v in sample.rates],
        "speed": round(sample.speed, 3), "rpm": round(sample.rpm, 1), "rpm_max": round(sample.rpm_max, 1),
        "rpm_idle": round(sample.rpm_idle, 1), "throttle": round(sample.throttle, 3), "brake": round(sample.brake, 3),
        "gear": sample.gear, "rumble": round(sample.rumble, 3), "slip": round(sample.slip, 3),
        "impact": round(sample.impact, 3), "active": sample.active,
    }
    if sample.time is not None:
        msg["t"] = round(sample.time, 4)
    if sample.attitude is not None:
        msg["attitude"] = [round(v, 5) for v in sample.attitude]
    if sample.effects:
        msg["effects"] = {k: round(v, 3) for k, v in sample.effects.items()}
    if "scent" in sample.params:
        msg["scent"] = int(sample.params["scent"].get("slot", 1))
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")
