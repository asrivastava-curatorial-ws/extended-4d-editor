"""Pose sources and smooth transitions between them.

Every target pose comes from a *source*: neutral/park/hold poses, or a trajectory that follows the
shared programme timeline. Changes of source (start, pause, resume, stop, programme switch, late
join, fault) are C2-continuous blends (smootherstep), whose duration is chosen from the platform
limits, so transitions never demand steps in velocity or acceleration.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

import numpy as np

from .interpolation import MotionTrack
from .limits import smootherstep


class PoseSource(ABC):
    name = "source"

    @abstractmethod
    def pose(self, t: float) -> np.ndarray:
        """Target pose (6,) at master-clock time t."""


class ConstantSource(PoseSource):
    def __init__(self, pose: np.ndarray, name: str = "hold") -> None:
        self._pose = np.asarray(pose, dtype=np.float64).copy()
        self.name = name

    def pose(self, t: float) -> np.ndarray:
        return self._pose.copy()


class TrajectorySource(PoseSource):
    """Follows a programme timeline: position = (t - epoch) [mod duration]."""

    name = "trajectory"

    def __init__(
        self,
        track: MotionTrack,
        epoch: float,
        duration_s: float,
        loop: bool,
        programme_id: int,
        timeline_id: int,
    ) -> None:
        self.track = track
        self.epoch = epoch
        self.duration = duration_s
        self.loop = loop
        self.programme_id = programme_id
        self.timeline_id = timeline_id

    def position(self, t: float) -> float:
        p = t - self.epoch
        if p < 0:
            return 0.0
        if self.duration > 0:
            return math.fmod(p, self.duration) if self.loop else min(p, self.duration)
        return p

    def pose(self, t: float) -> np.ndarray:
        return self.track.sample(self.position(t))


class BlendSource(PoseSource):
    name = "blend"

    def __init__(self, a: PoseSource, b: PoseSource, t0: float, duration: float) -> None:
        self.a = a
        self.b = b
        self.t0 = t0
        self.duration = max(duration, 1e-3)

    def weight(self, t: float) -> float:
        return smootherstep((t - self.t0) / self.duration)

    def done(self, t: float) -> bool:
        return t >= self.t0 + self.duration

    def pose(self, t: float) -> np.ndarray:
        w = self.weight(t)
        if w <= 0.0:
            return self.a.pose(t)
        if w >= 1.0:
            return self.b.pose(t)
        pa = self.a.pose(t)
        return pa + w * (self.b.pose(t) - pa)


class SourceSelector:
    """Holds the active source and performs blended transitions."""

    def __init__(self, initial: PoseSource) -> None:
        self.current: PoseSource = initial

    @property
    def target(self) -> PoseSource:
        """The source being transitioned to (or the current one)."""
        cur = self.current
        return cur.b if isinstance(cur, BlendSource) else cur

    def transition(self, new: PoseSource, t: float, duration: float) -> None:
        self.current = BlendSource(self.current, new, t, duration)

    def pose(self, t: float) -> np.ndarray:
        cur = self.current
        while isinstance(cur, BlendSource) and cur.done(t):
            cur = cur.b
        # collapse finished inner blends so nesting depth stays bounded
        if isinstance(cur, BlendSource) and isinstance(cur.a, BlendSource) and cur.a.done(t):
            cur.a = cur.a.b
        self.current = cur
        return cur.pose(t)

    def in_transition(self) -> bool:
        return isinstance(self.current, BlendSource)
