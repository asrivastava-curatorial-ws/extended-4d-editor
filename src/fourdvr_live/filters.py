"""Small real-time filters (NumPy only): the transfer functions of the offline cueing, run one sample
at a time. ``bilinear`` is the Tustin transform, so a filter here is the same discrete filter that
``scipy.signal.cont2discrete(..., method="bilinear")`` gives the offline stage (tested against it)."""

from __future__ import annotations

import math

import numpy as np


def bilinear(num: list[float], den: list[float], dt: float) -> tuple[np.ndarray, np.ndarray]:
    """Continuous num(s)/den(s) (highest power first) -> (b, a) in powers of z^-1, a[0] = 1."""
    n = len(den) - 1
    if n < 1 or len(num) > len(den):
        raise ValueError("need a proper transfer function of order >= 1")
    nm = [0.0] * (len(den) - len(num)) + [float(v) for v in num]
    k = 2.0 / dt
    b = np.zeros(n + 1)
    a = np.zeros(n + 1)
    for i in range(n + 1):                      # term s^(n-i)
        poly = np.array([1.0])
        for _ in range(n - i):
            poly = np.convolve(poly, [1.0, -1.0])
        for _ in range(i):
            poly = np.convolve(poly, [1.0, 1.0])
        scale = k ** (n - i)
        b += nm[i] * scale * poly
        a += float(den[i]) * scale * poly
    return b / a[0], a / a[0]


class Filter:
    """Linear filter stepped sample by sample over `channels` parallel signals (direct form II transposed)."""

    def __init__(self, b: np.ndarray, a: np.ndarray, channels: int = 1) -> None:
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self.order = len(self.a) - 1
        self.z = np.zeros((self.order, channels))

    @classmethod
    def analog(cls, num: list[float], den: list[float], dt: float, channels: int = 1) -> Filter:
        return cls(*bilinear(num, den, dt), channels=channels)

    def reset(self) -> None:
        self.z[:] = 0.0

    def step(self, x: np.ndarray) -> np.ndarray:
        b, a, z = self.b, self.a, self.z
        y = b[0] * x + z[0]
        for i in range(self.order - 1):
            z[i] = b[i + 1] * x + z[i + 1] - a[i + 1] * y
        z[self.order - 1] = b[self.order] * x - a[self.order] * y
        return y


def lowpass2(cutoff_hz: float, dt: float, channels: int = 1, zeta: float = 0.7071) -> Filter:
    w = 2.0 * math.pi * cutoff_hz
    return Filter.analog([w * w], [1.0, 2.0 * zeta * w, w * w], dt, channels)


def washout_position(hp_hz: float, washout_hz: float, dt: float, channels: int = 1, zeta: float = 1.0) -> Filter:
    """Acceleration -> platform position: s / ((s^2 + 2 zeta w s + w^2)(s + wb)) - as offline."""
    w = 2.0 * math.pi * hp_hz
    wb = 2.0 * math.pi * washout_hz
    den = np.polymul([1.0, 2.0 * zeta * w, w * w], [1.0, wb])
    return Filter.analog([1.0, 0.0], [float(v) for v in den], dt, channels)


def washout_angle(hp_hz: float, dt: float, channels: int = 1, zeta: float = 1.0) -> Filter:
    """Rotation rate -> platform angle: s / (s^2 + 2 zeta w s + w^2) - as offline."""
    w = 2.0 * math.pi * hp_hz
    return Filter.analog([1.0, 0.0], [1.0, 2.0 * zeta * w, w * w], dt, channels)


class Smoother:
    """One-pole low-pass with a time constant (also used for meters and effect levels)."""

    def __init__(self, tau_s: float, value: float = 0.0) -> None:
        self.tau = max(tau_s, 1e-6)
        self.value = value

    def step(self, target: float, dt: float) -> float:
        self.value += (target - self.value) * (1.0 - math.exp(-dt / self.tau))
        return self.value


def smootherstep(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * u * (u * (u * 6.0 - 15.0) + 10.0)
