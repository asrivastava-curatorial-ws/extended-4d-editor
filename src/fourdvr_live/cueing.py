"""Motion cueing in real time (classical washout with tilt coordination).

The same four ideas as the offline cueing of the extractor (docs/MOTION_ALGORITHM.md), with the same
transfer functions, but strictly causal - only what has already happened is known:

* the *onset* of an acceleration becomes a short movement of the platform that washes out again;
* a *sustained* acceleration becomes a slow tilt, so that gravity supplies the feeling;
* the *onset* of a rotation becomes a rotation that washes out;
* everything is scaled, softly saturated, and finally limited by the platform's own limiter.

``LiveCueing.step()`` is called once per control period and returns the pose offset from neutral.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from .filters import Filter, lowpass2, washout_angle, washout_position
from .sample import AXES, G


@dataclass
class LiveCueingSettings:
    gain: float = 1.0                     # everything (0..2); the operator's strength control
    axis_gains: dict[str, float] = field(default_factory=lambda: dict.fromkeys(AXES, 1.0))
    accel_scale: float = 0.2              # cue per unit of real acceleration (a racing car pulls far more than a seat can show)
    peak_specific_force: float = 2.5      # m/s^2: the cue saturates softly towards this
    rotation_gain: float = 0.6            # platform rotation rate per vehicle rotation rate, before washout
    peak_rate: float = 1.0                # rad/s: rotation rates saturate softly towards this
    input_lp_hz: float = 8.0              # smoothing of the incoming values (steps between packets, sensor noise)
    translation_hp_hz: float = 0.5
    translation_washout_hz: float = 0.12
    rotation_hp_hz: float = 0.25
    yaw_hp_hz: float = 0.35
    tilt_coordination: bool = True
    tilt_lp_hz: float = 0.5
    max_tilt_deg: float = 7.0
    tilt_rate_deg_s: float = 5.0
    comfort: float = 1.0                  # 0..1 on the fast channels (onsets); lowered when the picture cannot be delayed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LiveCueingSettings:
        s = cls()
        for key, value in data.items():
            if not hasattr(s, key):
                raise ValueError(f"unknown cueing setting {key!r}")
            if key == "axis_gains":
                unknown = sorted(set(value) - set(AXES))
                if unknown:
                    raise ValueError(f"unknown axis {unknown[0]!r} (axes: {', '.join(AXES)})")
                s.axis_gains = {**dict.fromkeys(AXES, 1.0), **{k: float(v) for k, v in dict(value).items()}}
            elif isinstance(getattr(s, key), bool):
                setattr(s, key, bool(value))
            else:
                setattr(s, key, float(value))
        s.check()
        return s

    def check(self) -> None:
        if not 0.0 <= self.gain <= 2.0:
            raise ValueError("cueing gain must be 0-2")
        if not 0.0 <= self.comfort <= 1.0:
            raise ValueError("comfort must be 0-1")
        if not 0.5 <= self.max_tilt_deg <= 20.0:
            raise ValueError("max_tilt_deg must be 0.5-20")
        for name in ("accel_scale", "peak_specific_force", "peak_rate", "input_lp_hz", "translation_hp_hz",
                     "translation_washout_hz", "rotation_hp_hz", "yaw_hp_hz", "tilt_lp_hz", "tilt_rate_deg_s"):
            if not getattr(self, name) > 0.0:
                raise ValueError(f"{name} must be positive")


def soft_limit(x: np.ndarray, limit: np.ndarray | float) -> np.ndarray:
    """Smooth saturation: follows x for small values and approaches +/-limit (never beyond)."""
    lim = np.maximum(np.asarray(limit, dtype=np.float64), 1e-9)
    return lim * np.tanh(np.asarray(x, dtype=np.float64) / lim)


class LiveCueing:
    def __init__(self, rate_hz: float, settings: LiveCueingSettings | None = None) -> None:
        self.s = settings or LiveCueingSettings()
        self.s.check()
        self.dt = 1.0 / rate_hz
        s, dt = self.s, self.dt
        self._in = lowpass2(min(s.input_lp_hz, 0.4 * rate_hz), dt, channels=6)
        self._trans = [
            washout_position(s.translation_hp_hz * (1.5 if k == 2 else 1.0), s.translation_washout_hz, dt)
            for k in range(3)
        ]
        self._tilt_lp = lowpass2(s.tilt_lp_hz, dt, channels=2)
        self._tilt_smooth = lowpass2(2.0, dt, channels=2, zeta=1.0)
        self._rot = [
            washout_angle(s.rotation_hp_hz, dt), washout_angle(s.rotation_hp_hz, dt), washout_angle(s.yaw_hp_hz, dt)
        ]
        self._tilt = np.zeros(2)          # slew-limited roll, pitch targets (rad)
        self.channels: dict[str, np.ndarray] = {}
        self.pose = np.zeros(6)

    def filters(self) -> list[Filter]:
        return [self._in, *self._trans, self._tilt_lp, self._tilt_smooth, *self._rot]

    def reset(self) -> None:
        """Forget everything (called while the output is faded out, so nothing jumps)."""
        for f in self.filters():
            f.reset()
        self._tilt[:] = 0.0
        self.pose = np.zeros(6)

    def step(self, accel: np.ndarray, rates: np.ndarray) -> np.ndarray:
        """One control period: vehicle acceleration (3) and rotation rates (3) in, pose offset (6) out."""
        s = self.s
        x = self._in.step(np.concatenate([np.asarray(accel, dtype=np.float64), np.asarray(rates, dtype=np.float64)]))
        sf = soft_limit(x[:3] * s.accel_scale, s.peak_specific_force)
        om = soft_limit(x[3:] * s.rotation_gain, s.peak_rate)

        trans = np.zeros(6)
        for k in range(3):
            trans[k] = float(self._trans[k].step(sf[k : k + 1])[0])

        tilt = np.zeros(6)
        if s.tilt_coordination:
            lp = self._tilt_lp.step(sf[:2])
            limit = math.sin(math.radians(s.max_tilt_deg))
            want = np.array([
                math.asin(max(-limit, min(limit, lp[1] / G))),        # roll: right side down for a push to the left
                -math.asin(max(-limit, min(limit, lp[0] / G))),       # pitch: nose up for a push forwards
            ])
            step = math.radians(s.tilt_rate_deg_s) * self.dt
            self._tilt += np.clip(want - self._tilt, -step, step)
            tilt[3:5] = self._tilt_smooth.step(self._tilt)

        rot = np.zeros(6)
        for k in range(3):
            rot[3 + k] = float(self._rot[k].step(om[k : k + 1])[0])

        gains = np.array([s.axis_gains.get(a, 1.0) for a in AXES]) * s.gain
        pose = (trans * s.comfort + tilt + rot * s.comfort) * gains
        self.channels = {"translational": trans, "tilt": tilt, "rotational": rot}
        self.pose = pose
        return pose
