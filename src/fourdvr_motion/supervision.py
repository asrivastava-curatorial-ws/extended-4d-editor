"""Watching that actuators follow their commands (needs measured positions from the actuator controller).

An actuator lags behind its command: a servo drive by milliseconds, a hydraulic cylinder by tens of
milliseconds, a pneumatic one by more, a cylinder on a plain valve by its travel time. So a measured
position is compared with every command of the last ``lag_s``: it may lie anywhere between the lowest
and the highest of them. Beyond that by more than ``following_error`` for longer than
``following_error_s`` - a jammed or leaking cylinder, a pump that stopped, a broken sensor, a drive
that switched off - the actuator is reported and the platform parks.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from .platform_config import SupervisionSettings


class FollowingErrorMonitor:
    def __init__(self, settings: SupervisionSettings, legs: int) -> None:
        self.s = settings
        self.legs = legs
        self.history: deque[tuple[float, np.ndarray]] = deque()
        self.over_since: list[float | None] = [None] * legs
        self.worst = np.zeros(legs)              # the largest error seen (m), per actuator
        self.error = np.zeros(legs)              # the error now (m)

    @property
    def enabled(self) -> bool:
        return self.s.following_error > 0.0

    def reset(self) -> None:
        self.history.clear()
        self.over_since = [None] * self.legs
        self.error[:] = 0.0

    def command(self, now: float, strokes: np.ndarray) -> None:
        self.history.append((now, np.asarray(strokes, dtype=np.float64).copy()))
        keep = now - max(self.s.lag_s, 0.0) - 0.05
        while len(self.history) > 1 and self.history[1][0] <= keep:
            self.history.popleft()

    def check(self, now: float, measured: np.ndarray | None) -> str:
        """A fault message, or "" (also while there is nothing to compare)."""
        if not self.enabled or measured is None or not self.history:
            return ""
        start = now - self.s.lag_s
        window = [s for t, s in self.history if t >= start - 1e-9] or [self.history[-1][1]]
        lo = np.min(window, axis=0)
        hi = np.max(window, axis=0)
        pos = np.asarray(measured, dtype=np.float64)
        self.error = np.maximum(np.maximum(lo - pos, pos - hi), 0.0)
        self.worst = np.maximum(self.worst, self.error)
        for i in range(self.legs):
            if self.error[i] > self.s.following_error:
                if self.over_since[i] is None:
                    self.over_since[i] = now
                elif now - self.over_since[i] >= self.s.following_error_s:   # type: ignore[operator]
                    return (f"actuator {i + 1} does not follow its command: {self.error[i] * 1000:.0f} mm off for "
                            f"{now - self.over_since[i]:.1f} s (jammed, no pressure or supply, sensor or drive fault?)")  # type: ignore[operator]
            else:
                self.over_since[i] = None
        return ""
