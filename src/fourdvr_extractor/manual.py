"""Manual motion editing: what a person adds to (or takes away from) the automatically extracted motion.

Two kinds of edit, both drawn as curves on the Editor timeline:

* **intensity** - a gain envelope (0 .. 2, default 1) that scales the automatic motion over time:
  calm a passage down, switch the platform off during a title sequence, emphasise a finale;
* **axis offsets** - six curves (-1 .. +1, default 0), one per degree of freedom, that are *added*
  to the motion: a jolt, a drop, a long bank, a hand-made ride for footage the analysis cannot read.
  1.0 is the axis' travel limit in the platform profile, before the soft limiting of the constraint
  stage.

Edits are applied to the raw cues *before* the constraint stage, so everything a person draws still
passes the same workspace, velocity, acceleration and jerk limits and the same validation as the
automatic motion. Curves ease between their points (quintic "smootherstep": no velocity or
acceleration step at a point), which is what a platform can follow.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fourdvr_motion.kinematics import AXES
from fourdvr_motion.platform_config import PlatformConfig

from .signals import fade_envelope

Point = tuple[float, float]
GAIN_RANGE = (0.0, 2.0)
OFFSET_RANGE = (-1.0, 1.0)
MAX_POINTS = 20000


def ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * u * (u * (u * 6.0 - 15.0) + 10.0)


def curve_at(points: list[Point], t: float, default: float) -> float:
    """Eased interpolation through `points` (sorted by time); the ends are held."""
    if not points:
        return default
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    i = bisect.bisect_right(points, (t, math.inf))
    (t0, v0), (t1, v1) = points[i - 1], points[i]
    return v0 + (v1 - v0) * ease((t - t0) / (t1 - t0))


def sample_curve(points: list[Point], times: np.ndarray, default: float) -> np.ndarray:
    """Vectorised curve_at for a rising time grid."""
    if not points:
        return np.full(len(times), default)
    pt = np.array([p[0] for p in points])
    pv = np.array([p[1] for p in points])
    if len(pt) == 1:
        return np.full(len(times), pv[0])
    i = np.clip(np.searchsorted(pt, times, side="right"), 1, len(pt) - 1)
    t0, t1, v0, v1 = pt[i - 1], pt[i], pv[i - 1], pv[i]
    u = np.clip((times - t0) / np.maximum(t1 - t0, 1e-12), 0.0, 1.0)
    return v0 + (v1 - v0) * (u * u * u * (u * (u * 6.0 - 15.0) + 10.0))


@dataclass
class MotionEdits:
    gain: list[Point] = field(default_factory=list)
    offsets: dict[str, list[Point]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return all(abs(v - 1.0) < 1e-9 for _t, v in self.gain) and all(
            abs(v) < 1e-9 for pts in self.offsets.values() for _t, v in pts
        )

    def lane(self, name: str) -> list[Point]:
        """The point list of 'gain' or of an axis (created on demand) - the timeline edits it in place."""
        if name == "gain":
            return self.gain
        if name not in AXES:
            raise ValueError(f"unknown motion lane {name!r}")
        return self.offsets.setdefault(name, [])

    def sort(self) -> None:
        self.gain.sort(key=lambda p: p[0])
        for pts in self.offsets.values():
            pts.sort(key=lambda p: p[0])

    def copy(self) -> MotionEdits:
        return MotionEdits(list(self.gain), {k: list(v) for k, v in self.offsets.items()})

    def to_dict(self) -> dict[str, Any]:
        def pts(points: list[Point]) -> list[list[float]]:
            return [[round(t, 4), round(v, 4)] for t, v in points]

        return {"gain": pts(self.gain), "offsets": {k: pts(v) for k, v in self.offsets.items() if v}}

    @classmethod
    def from_dict(cls, data: Any) -> MotionEdits:
        if not isinstance(data, dict):
            raise ValueError("manual motion edits must be an object")

        def pts(raw: Any, what: str, lo: float, hi: float) -> list[Point]:
            if not isinstance(raw, list) or len(raw) > MAX_POINTS:
                raise ValueError(f"{what}: expected a list of at most {MAX_POINTS} [time, value] points")
            out: list[Point] = []
            for item in raw:
                if not (isinstance(item, list | tuple) and len(item) == 2):
                    raise ValueError(f"{what}: every point is [time, value]")
                t, v = float(item[0]), float(item[1])
                if not (math.isfinite(t) and math.isfinite(v)) or t < 0 or not lo <= v <= hi:
                    raise ValueError(f"{what}: point [{t}, {v}] outside time >= 0, value {lo}..{hi}")
                out.append((t, v))
            return sorted(out, key=lambda p: p[0])

        offsets_in = data.get("offsets", {})
        if not isinstance(offsets_in, dict) or set(offsets_in) - set(AXES):
            raise ValueError(f"offsets: keys must be axes {AXES}")
        return cls(
            pts(data.get("gain", []), "gain", *GAIN_RANGE),
            {k: pts(v, f"offsets.{k}", *OFFSET_RANGE) for k, v in offsets_in.items()},
        )

    def apply(self, raw: np.ndarray, fs: float, profile: PlatformConfig, fade_s: float = 1.5) -> np.ndarray:
        """Raw cues with the edits applied (the input is not modified). Still to be constrained."""
        n = len(raw)
        times = np.arange(n) / fs
        out = np.asarray(raw, dtype=np.float64) * sample_curve(self.gain, times, 1.0)[:, None]
        fade = fade_envelope(n, fs, fade_s)       # manual motion, too, starts and ends at neutral
        lim = profile.limits
        for k, axis in enumerate(AXES):
            pts = self.offsets.get(axis)
            if not pts:
                continue
            travel = min(abs(float(lim.position_min[k])), abs(float(lim.position_max[k])))
            out[:, k] += sample_curve(pts, times, 0.0) * travel * fade
        return out


# ------------------------------------------------------------------------------ ready-made shapes
@dataclass(frozen=True)
class Gesture:
    name: str
    label: str
    axis: str
    shape: tuple[Point, ...]      # (seconds after the start, offset added) - starts and ends at 0


def _hold(amount: float, rise: float, hold: float, fall: float) -> tuple[Point, ...]:
    return ((0.0, 0.0), (rise, amount), (rise + hold, amount), (rise + hold + fall, 0.0))


# Shapes are sized so that a typical electric 6-DoF platform can follow them almost unchanged (the rise
# times respect a jerk limit around 40 m/s^3); the constraint stage scales down whatever a particular
# platform cannot do. Fast vibration belongs on the seat-vibration effect, not on the platform.
GESTURES: dict[str, Gesture] = {
    g.name: g
    for g in (
        Gesture("jolt", "Jolt (up)", "heave", ((0.0, 0.0), (0.3, 0.15), (0.9, 0.0))),
        Gesture("drop", "Drop", "heave", ((0.0, 0.0), (0.45, -0.5), (1.5, 0.0))),
        Gesture("lift", "Lift and hold", "heave", _hold(0.6, 0.8, 1.5, 1.2)),
        Gesture("bank-left", "Bank left", "roll", _hold(-0.6, 0.7, 1.2, 0.9)),
        Gesture("bank-right", "Bank right", "roll", _hold(0.6, 0.7, 1.2, 0.9)),
        Gesture("nose-up", "Nose up (climb)", "pitch", _hold(-0.6, 0.8, 1.2, 1.0)),
        Gesture("nose-down", "Nose down (dive)", "pitch", _hold(0.6, 0.8, 1.2, 1.0)),
        Gesture("launch", "Launch (push back in the seat)", "surge", ((0.0, 0.0), (0.6, 0.6), (2.0, 0.0))),
        Gesture("brake", "Brake", "surge", ((0.0, 0.0), (0.6, -0.6), (2.0, 0.0))),
        Gesture("swerve-left", "Swerve left", "sway", ((0.0, 0.0), (0.5, 0.5), (1.5, 0.0))),
        Gesture("swerve-right", "Swerve right", "sway", ((0.0, 0.0), (0.5, -0.5), (1.5, 0.0))),
        Gesture("turn-left", "Turn left", "yaw", _hold(0.6, 0.8, 1.0, 1.0)),
        Gesture("turn-right", "Turn right", "yaw", _hold(-0.6, 0.8, 1.0, 1.0)),
        Gesture(
            "shake", "Shake (gentle)", "heave",
            ((0.0, 0.0), (0.2, 0.03), (0.4, -0.03), (0.6, 0.03), (0.8, -0.03), (1.0, 0.02), (1.2, -0.02), (1.5, 0.0)),
        ),
    )
}


def insert_shape(points: list[Point], start: float, shape: tuple[Point, ...], default: float,
                 scale: float = 1.0, lo: float = -1.0, hi: float = 1.0) -> list[Point]:
    """New point list with `shape` added on top of the curve from `start` on.

    Points of the old curve inside the shape's span are replaced; the curve outside is unchanged
    because the old values at both ends of the span are pinned.
    """
    end = start + shape[-1][0]
    base = [(start + dt, curve_at(points, start + dt, default)) for dt, _dv in shape]
    kept = [p for p in points if p[0] < start - 1e-9 or p[0] > end + 1e-9]
    new = [(round(t, 4), min(hi, max(lo, b + dv * scale))) for (t, b), (_dt, dv) in zip(base, shape, strict=True)]
    return sorted(kept + new, key=lambda p: p[0])
