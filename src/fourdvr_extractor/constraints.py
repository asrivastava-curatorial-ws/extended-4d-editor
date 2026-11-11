"""Workspace, velocity, acceleration and jerk constraints for a platform profile (offline).

Order (MOTION_ALGORITHM.md): smoothing to the platform bandwidth -> per-axis soft workspace
compression -> iterative, *local* gain reduction wherever velocity, acceleration or jerk of an
axis exceed a margin of the limit -> combined workspace (inverse kinematics: every actuator inside
its usable stroke) -> actuator velocity/acceleration -> repeat until everything passes.
Local gain envelopes are smooth, so the result stays smooth and keeps its timing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from fourdvr_motion.platform_config import PlatformConfig

from .signals import derivatives, fade_envelope, lowpass, smooth_envelope


@dataclass
class ConstraintSettings:
    bandwidth_hz: float = 6.0  # content above this cannot be reproduced well by the platform
    position_margin: float = 0.92  # fraction of the position limits used
    derivative_margin: float = 0.85  # fraction of velocity/acceleration/jerk limits used
    actuator_margin: float = 0.85
    max_iterations: int = 25

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ConstraintResult:
    motion: np.ndarray
    iterations: int
    converged: bool
    axis_gain_min: list[float]


def soft_limit(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Smooth compression into (lo, hi): identity for small values, tanh-like near the bounds."""
    out = np.empty_like(x)
    pos = x >= 0
    out[pos] = hi * np.tanh(x[pos] / hi)
    out[~pos] = (-lo) * np.tanh(x[~pos] / (-lo))
    return out


def _feasible_scale(profile: PlatformConfig, pose: np.ndarray) -> float:
    act = profile.actuators
    lo, hi = 0.0, 1.0
    zero = profile.calibration.zero_offsets
    for _ in range(16):
        mid = (lo + hi) / 2
        lengths = profile.geometry.leg_lengths(pose * mid + zero)
        if np.all(lengths >= act.usable_min()) and np.all(lengths <= act.usable_max()):
            lo = mid
        else:
            hi = mid
    return lo


def apply_constraints(
    motion: np.ndarray, fs: float, profile: PlatformConfig, settings: ConstraintSettings | None = None
) -> ConstraintResult:
    s = settings or ConstraintSettings()
    lim = profile.limits
    act = profile.actuators
    x = lowpass(np.asarray(motion, dtype=np.float64), s.bandwidth_hz, fs, order=4)
    for k in range(6):
        x[:, k] = soft_limit(x[:, k], lim.position_min[k] * s.position_margin, lim.position_max[k] * s.position_margin)
    gains_min = np.ones(6)
    zero = profile.calibration.zero_offsets
    converged = False
    iterations = 0
    for _ in range(s.max_iterations):
        iterations += 1
        changed = False
        # per-axis derivatives
        v, a, j = derivatives(x, fs)
        for k in range(6):
            ratio = np.maximum.reduce(
                [
                    np.abs(v[:, k]) / (lim.velocity[k] * s.derivative_margin),
                    np.abs(a[:, k]) / (lim.acceleration[k] * s.derivative_margin),
                    np.abs(j[:, k]) / (lim.jerk[k] * s.derivative_margin),
                ]
            )
            if np.any(ratio > 1.0):
                env = smooth_envelope(ratio, fs)
                x[:, k] *= env
                gains_min[k] = min(gains_min[k], float(env.min()))
                changed = True
        # combined workspace via inverse kinematics
        lengths = profile.geometry.leg_lengths_many(x + zero)
        bad = np.any((lengths < act.usable_min()) | (lengths > act.usable_max()), axis=1)
        if np.any(bad):
            ratio = np.ones(len(x))
            for i in np.nonzero(bad)[0]:
                ratio[i] = 1.0 / max(_feasible_scale(profile, x[i]) * 0.98, 1e-3)
            x *= smooth_envelope(ratio, fs, hold_s=0.4, sigma_s=0.15)[:, None]
            changed = True
            lengths = profile.geometry.leg_lengths_many(x + zero)
        # actuator velocity / acceleration
        lv = np.gradient(lengths, 1.0 / fs, axis=0)
        la = np.gradient(lv, 1.0 / fs, axis=0)
        ratio = np.maximum(
            np.max(np.abs(lv), axis=1) / (act.max_velocity * s.actuator_margin),
            np.max(np.abs(la), axis=1) / (act.max_acceleration * s.actuator_margin),
        )
        if np.any(ratio > 1.0):
            x *= smooth_envelope(ratio, fs)[:, None]
            changed = True
        if not changed:
            converged = True
            break
    # hard guarantees at the very end (no-ops when converged) and exact neutral at both ends
    x = np.clip(x, lim.position_min, lim.position_max)
    edge = fade_envelope(len(x), fs, 2.0 / fs)
    x *= edge[:, None]
    return ConstraintResult(x, iterations, converged, [round(float(g), 4) for g in gains_min])
