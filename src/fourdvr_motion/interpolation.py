"""Shape-preserving interpolation of .motion samples (independent of video frame rate).

Monotone piecewise-cubic Hermite interpolation (PCHIP, Fritsch-Carlson). Between two samples the
curve never leaves the range spanned by them, so interpolation cannot overshoot configured limits
that the samples already respect, and the velocity is continuous (no steps at sample instants).
"""

from __future__ import annotations

import math

import numpy as np


def pchip_slopes(y: np.ndarray, h: float) -> np.ndarray:
    """Derivatives (per second) at each sample for uniformly spaced data, shape (N, C)."""
    y = np.asarray(y, dtype=np.float64)
    n = y.shape[0]
    d = np.zeros_like(y)
    if n < 2:
        return d
    delta = np.diff(y, axis=0) / h
    if n == 2:
        d[:] = delta[0]
        return d
    a, b = delta[:-1], delta[1:]
    same_sign = (a * b) > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        harmonic = np.where(same_sign, 2.0 / (1.0 / np.where(a == 0, 1, a) + 1.0 / np.where(b == 0, 1, b)), 0.0)
    d[1:-1] = np.where(same_sign, harmonic, 0.0)
    d[0] = _edge(delta[0], delta[1])
    d[-1] = _edge(delta[-1], delta[-2])
    return d


def _edge(d0: np.ndarray, d1: np.ndarray) -> np.ndarray:
    m = (3.0 * d0 - d1) / 2.0
    m = np.where(np.sign(m) != np.sign(d0), 0.0, m)
    m = np.where((np.sign(d0) != np.sign(d1)) & (np.abs(m) > np.abs(3.0 * d0)), 3.0 * d0, m)
    return m


class MotionTrack:
    """A 6-DoF trajectory sampled at a fixed rate, evaluated at arbitrary programme times."""

    def __init__(self, samples: np.ndarray, sample_rate_hz: float, duration_s: float | None = None) -> None:
        self.samples = np.ascontiguousarray(np.asarray(samples, dtype=np.float64))
        if self.samples.ndim != 2 or self.samples.shape[1] != 6 or self.samples.shape[0] < 2:
            raise ValueError("samples must have shape (N>=2, 6)")
        self.rate = float(sample_rate_hz)
        self.h = 1.0 / self.rate
        self.last_time = (self.samples.shape[0] - 1) * self.h
        self.duration = float(duration_s) if duration_s is not None else self.last_time
        self.slopes = pchip_slopes(self.samples, self.h)

    def sample(self, t: float) -> np.ndarray:
        if not math.isfinite(t) or t <= 0.0:
            return self.samples[0].copy()
        if t >= self.last_time:
            return self.samples[-1].copy()
        x = t * self.rate
        i = int(x)
        u = x - i
        u2 = u * u
        u3 = u2 * u
        h00 = 2 * u3 - 3 * u2 + 1
        h10 = u3 - 2 * u2 + u
        h01 = -2 * u3 + 3 * u2
        h11 = u3 - u2
        p0, p1 = self.samples[i], self.samples[i + 1]
        m0, m1 = self.slopes[i] * self.h, self.slopes[i + 1] * self.h
        return h00 * p0 + h10 * m0 + h01 * p1 + h11 * m1

    def sample_many(self, times: np.ndarray) -> np.ndarray:
        return np.array([self.sample(float(t)) for t in np.asarray(times, dtype=np.float64)])
