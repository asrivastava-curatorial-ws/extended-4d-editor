"""Per-platform motion processing: source -> hard limits -> kinematics -> calibration -> strokes.

The same pipeline feeds every driver (servo/electric hardware and the simulation driver), so the
simulation exercises exactly the code that commands real actuators.
"""

from __future__ import annotations

import numpy as np

from .limits import ActuatorGuard, PoseLimiter, blend_time
from .platform_config import PlatformConfig
from .sources import ConstantSource, PoseSource, SourceSelector


class PlatformPipeline:
    def __init__(self, cfg: PlatformConfig, control_rate_hz: float, latency_compensation_s: float = 0.0) -> None:
        if not 50.0 <= control_rate_hz <= 200.0:
            raise ValueError("control rate must be 50-200 Hz")
        self.cfg = cfg
        self.dt = 1.0 / control_rate_hz
        self.lookahead = latency_compensation_s
        self.limiter = PoseLimiter(cfg.limits, self.dt)
        self.guard = ActuatorGuard(cfg.geometry, cfg.actuators, self.dt)
        self.counters = self.limiter.counters
        self.guard.counters = self.counters
        cal = cfg.calibration
        self.neutral = ConstantSource(cal.neutral_pose, "neutral")
        self.park = ConstantSource(cal.park_pose, "park")
        self.selector = SourceSelector(self.park)
        self.speed_scale = 1.0
        self.pose = cal.park_pose.copy()  # logical pose (without zero offsets)
        self.target = cal.park_pose.copy()
        self.lengths = cfg.geometry.leg_lengths(cal.park_pose + cal.zero_offsets)
        self.strokes = self._strokes(self.lengths)
        self.initialised = False

    # ------------------------------------------------------------------ setup
    def reset(self, pose: np.ndarray) -> None:
        """Start from a known platform pose (e.g. derived from the controller's reported positions)."""
        pose = np.asarray(pose, dtype=np.float64)
        self.pose = pose.copy()
        self.target = pose.copy()
        self.limiter.reset(pose)
        self.guard.reset(pose + self.cfg.calibration.zero_offsets)
        self.selector = SourceSelector(ConstantSource(pose, "hold"))
        self.lengths = self.guard.lengths.copy()
        self.strokes = self._strokes(self.lengths)
        self.initialised = True

    def pose_from_strokes(self, strokes_m: np.ndarray) -> np.ndarray:
        """Forward kinematics from stroke positions (inverse of the calibration mapping)."""
        cal = self.cfg.calibration
        lengths = np.asarray(strokes_m, dtype=np.float64) + self.cfg.actuators.min_length - cal.actuator_offsets
        pose = self.cfg.geometry.forward(lengths, guess=cal.park_pose + cal.zero_offsets)
        return pose - cal.zero_offsets

    # ------------------------------------------------------------ transitions
    def transition(self, source: PoseSource, t: float, gentle: bool = False, minimum: float | None = None) -> float:
        """Blend from the current target to ``source`` starting at master time t; returns the duration."""
        delta = source.pose(t) - self.selector.pose(t)
        scale = self.cfg.motion.park_speed_scale if gentle else 0.8
        duration = blend_time(
            delta, self.cfg.limits, scale, self.cfg.motion.min_blend_s if minimum is None else minimum
        )
        self.selector.transition(source, t, duration)
        self.speed_scale = 1.0
        return duration

    # ------------------------------------------------------------------- step
    def step(self, t: float) -> np.ndarray:
        """Compute the stroke targets (m) to command now for master time ``t``."""
        r_now = self.selector.pose(t + self.lookahead)
        r_next = self.selector.pose(t + self.lookahead + self.dt)
        self.target = r_next
        x = self.limiter.step(r_now, r_next, self.speed_scale)
        zero = self.cfg.calibration.zero_offsets
        lim = self.cfg.limits
        pose, lengths = self.guard.apply(
            x + zero, self.cfg.calibration.neutral_pose + zero, lim.position_min + zero, lim.position_max + zero
        )
        logical = pose - zero
        if not np.allclose(logical, x, atol=1e-9):
            self.limiter.sync(logical)
        self.pose = logical
        self.lengths = lengths
        self.strokes = self._strokes(lengths)
        return self.strokes

    def _strokes(self, lengths: np.ndarray) -> np.ndarray:
        act = self.cfg.actuators
        strokes = lengths - act.min_length + self.cfg.calibration.actuator_offsets
        return np.clip(strokes, 0.0, act.stroke)

    def stroke_utilisation(self) -> float:
        act = self.cfg.actuators
        mid = act.stroke / 2.0
        return float(np.max(np.abs(self.strokes - mid) / mid))
