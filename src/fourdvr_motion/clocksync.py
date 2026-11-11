"""Client-side estimate of the Control Server master clock (offset, round-trip time, drift).

NTP-style exchange: the client sends t0 (local), the server answers t1 (receive) and t2 (send) in
master time, the client notes t3 (local). offset = ((t1 - t0) + (t2 - t3)) / 2,
rtt = (t3 - t0) - (t2 - t1). Samples with the lowest RTT are the least disturbed by queuing, so
the estimate is the median offset of the best 30 % of recent samples; drift (ppm) is the slope of
offset against local time (least squares) and is applied between exchanges.
The VR Player implements the same algorithm in scripts/net/clock_sync.gd.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ClockSample:
    local: float
    offset: float
    rtt: float


class ClockSync:
    def __init__(self, window: int = 64, best_fraction: float = 0.3) -> None:
        self.samples: deque[ClockSample] = deque(maxlen=window)
        self.best_fraction = best_fraction
        self.offset = 0.0
        self.rtt = 0.0
        self.drift = 0.0  # seconds per second
        self._ref_local = 0.0

    @property
    def synced(self) -> bool:
        return len(self.samples) >= 3

    def add(self, t0: float, t1: float, t2: float, t3: float) -> ClockSample:
        rtt = max(0.0, (t3 - t0) - (t2 - t1))
        sample = ClockSample(local=(t0 + t3) / 2.0, offset=((t1 - t0) + (t2 - t3)) / 2.0, rtt=rtt)
        self.samples.append(sample)
        self._recompute()
        return sample

    def _recompute(self) -> None:
        ordered = sorted(self.samples, key=lambda s: s.rtt)
        count = max(3, int(len(ordered) * self.best_fraction))
        best = ordered[:count]
        self.offset = statistics.median(s.offset for s in best)
        self.rtt = statistics.median(s.rtt for s in best)
        self._ref_local = statistics.median(s.local for s in best)
        span = max(s.local for s in best) - min(s.local for s in best)
        if len(best) >= 5 and span > 5.0:
            mx = statistics.fmean(s.local for s in best)
            my = statistics.fmean(s.offset for s in best)
            sxx = sum((s.local - mx) ** 2 for s in best)
            sxy = sum((s.local - mx) * (s.offset - my) for s in best)
            slope = sxy / sxx if sxx > 0 else 0.0
            self.drift = max(-500e-6, min(500e-6, slope))  # quartz clocks are within a few hundred ppm
            self.offset = my
            self._ref_local = mx
        else:
            self.drift = 0.0

    def master_time(self, local: float | None = None) -> float:
        loc = time.monotonic() if local is None else local
        return loc + self.offset + self.drift * (loc - self._ref_local)

    def local_time(self, master: float) -> float:
        """Inverse mapping (used to schedule local actions at a master-clock instant)."""
        return (master - self.offset + self.drift * self._ref_local) / (1.0 + self.drift)
