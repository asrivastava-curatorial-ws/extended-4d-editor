"""Signal-processing helpers (SciPy): resampling, robust cleaning, zero-phase and washout filters."""

from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.ndimage import gaussian_filter1d, minimum_filter1d


def hampel(x: np.ndarray, window: int, n_sigma: float = 4.0) -> np.ndarray:
    """Replace outliers (> n_sigma robust deviations from the running median) by the running median."""
    if window < 1 or len(x) < 3:
        return x.copy()
    from scipy.ndimage import median_filter

    med = median_filter(x, size=(2 * window + 1,) + (1,) * (x.ndim - 1), mode="nearest")
    dev = np.abs(x - med)
    mad = median_filter(dev, size=(2 * window + 1,) + (1,) * (x.ndim - 1), mode="nearest") * 1.4826
    out = x.copy()
    bad = dev > n_sigma * np.maximum(mad, 1e-9)
    out[bad] = med[bad]
    return out


def resample(times: np.ndarray, values: np.ndarray, grid: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim == 1:
        return np.interp(grid, times, values)
    return np.stack([np.interp(grid, times, values[:, k]) for k in range(values.shape[1])], axis=1)


def lowpass(x: np.ndarray, cutoff_hz: float, fs: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth low-pass (no delay relative to the video)."""
    nyq = fs / 2.0
    if cutoff_hz >= 0.95 * nyq or len(x) < 3 * (order + 1):
        return x.copy()
    sos = signal.butter(order, cutoff_hz / nyq, btype="low", output="sos")
    return signal.sosfiltfilt(sos, x, axis=0)


def highpass(x: np.ndarray, cutoff_hz: float, fs: float, order: int = 2) -> np.ndarray:
    nyq = fs / 2.0
    if len(x) < 3 * (order + 1):
        return np.zeros_like(x)
    sos = signal.butter(order, min(cutoff_hz / nyq, 0.95), btype="high", output="sos")
    return signal.sosfiltfilt(sos, x, axis=0)


def bandpass(x: np.ndarray, lo_hz: float, hi_hz: float, fs: float, order: int = 2) -> np.ndarray:
    nyq = fs / 2.0
    hi = min(hi_hz, 0.9 * nyq)
    if lo_hz >= hi or len(x) < 3 * (2 * order + 1):
        return np.zeros_like(x)
    sos = signal.butter(order, [lo_hz / nyq, hi / nyq], btype="band", output="sos")
    return signal.sosfiltfilt(sos, x, axis=0)


def analog_filter(num: list[float], den: list[float], x: np.ndarray, fs: float) -> np.ndarray:
    """Causal discrete version (bilinear transform) of a continuous transfer function num/den."""
    b, a, _dt = signal.cont2discrete((num, den), 1.0 / fs, method="bilinear")
    return signal.lfilter(np.ravel(b), a, x, axis=0)


def washout_position(accel: np.ndarray, fs: float, hp_hz: float, washout_hz: float, zeta: float = 1.0) -> np.ndarray:
    """Classical translational washout: x/a = s / ((s^2 + 2 zeta w s + w^2)(s + wb)).

    High-pass filtered acceleration, double-integrated, with a further first-order washout so the
    platform always drifts back to neutral (stable for any bounded input).
    """
    w = 2 * np.pi * hp_hz
    wb = 2 * np.pi * washout_hz
    den = np.polymul([1.0, 2 * zeta * w, w * w], [1.0, wb])
    return analog_filter([1.0, 0.0], list(den), accel, fs)


def washout_angle(rate: np.ndarray, fs: float, hp_hz: float, zeta: float = 1.0) -> np.ndarray:
    """Rotational washout: angle/rate = s / (s^2 + 2 zeta w s + w^2) (high-passed, integrated)."""
    w = 2 * np.pi * hp_hz
    return analog_filter([1.0, 0.0], [1.0, 2 * zeta * w, w * w], rate, fs)


def slew_limit(x: np.ndarray, max_rate: float, fs: float) -> np.ndarray:
    out = np.empty_like(x)
    step = max_rate / fs
    acc = x[0]
    for i, v in enumerate(x):
        acc = acc + np.clip(v - acc, -step, step)
        out[i] = acc
    return out


def smooth_envelope(ratio: np.ndarray, fs: float, hold_s: float = 0.25, sigma_s: float = 0.12) -> np.ndarray:
    """Gain envelope <= 1 that removes excess (ratio > 1) smoothly: held minimum, then Gaussian smoothing."""
    gain = 1.0 / np.maximum(ratio, 1.0)
    held = minimum_filter1d(gain, size=max(1, int(2 * hold_s * fs) + 1), mode="nearest")
    smooth = gaussian_filter1d(held, sigma=max(1.0, sigma_s * fs), mode="nearest")
    return np.minimum(smooth, 1.0)  # slightly shallower than `held`; callers iterate until satisfied


def smootherstep(u: np.ndarray) -> np.ndarray:
    u = np.clip(u, 0.0, 1.0)
    return u * u * u * (u * (u * 6.0 - 15.0) + 10.0)


def fade_envelope(n: int, fs: float, fade_s: float) -> np.ndarray:
    t = np.arange(n) / fs
    total = (n - 1) / fs
    if fade_s <= 0:
        return np.ones(n)
    return smootherstep(t / fade_s) * smootherstep((total - t) / fade_s)


def derivatives(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    v = np.gradient(x, 1.0 / fs, axis=0)
    a = np.gradient(v, 1.0 / fs, axis=0)
    j = np.gradient(a, 1.0 / fs, axis=0)
    return v, a, j


def robust_scale(x: np.ndarray, q: float = 95.0) -> float:
    v = float(np.percentile(np.abs(x), q)) if len(x) else 0.0
    return v if v > 1e-9 else 0.0
