"""Motion/event analysis and motion classification (deterministic, rule-based).

From the cleaned ego-motion signals this module derives forward speed, longitudinal, lateral
and vertical accelerations (including the centripetal terms of the rotating vehicle frame:
forward speed x yaw rate sideways, forward speed x pitch rate vertically), banking, vibration
level and impact-like events, and labels every instant with a motion class:

  static    - no usable camera motion (tripod shot, or too uncertain to cue)
  cut       - around a scene cut (no motion is synthesised across edits)
  handheld  - small irregular camera motion (walking, handheld): strongly attenuated
  vehicle   - sustained forward motion, yaw-dominant turning, road vibration
  flight    - sustained forward motion with banking/pitching and little vibration
  rapid     - fast rotations / strong accelerations (rides, roller coasters, action)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import maximum_filter1d, median_filter, percentile_filter, uniform_filter1d

from .analysis import Analysis
from .signals import bandpass, hampel, lowpass, resample, robust_scale

CLASSES = ("static", "cut", "handheld", "vehicle", "flight", "rapid")
RIDE_CLASSES = ("vehicle", "flight", "rapid")
BRIDGE_HANDHELD_S = 10.0  # a shorter "handheld" stretch between ride classes is the same ride
IMPACT_CONTEXT_S = 10.0  # window for the local robust statistics of impact detection


@dataclass
class MotionEvents:
    times: np.ndarray  # (N,) motion-rate grid
    angular_velocity: np.ndarray  # (N, 3) rad/s, platform axes
    velocity: np.ndarray  # (N, 3) scale-free
    acceleration: np.ndarray  # (N, 3) scale-free proxy incl. centripetal
    confidence: np.ndarray  # (N,)
    forward_speed: np.ndarray  # (N,)
    banking: np.ndarray  # (N,) leaky-integrated roll (rad)
    vibration: np.ndarray  # (N,) 0..1 normalised level
    labels: np.ndarray  # (N,) indices into CLASSES
    impacts: list[tuple[float, float]] = field(default_factory=list)  # (time, relative magnitude)
    events: list[dict[str, object]] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)

    def segments(self) -> list[tuple[float, float, str]]:
        segs: list[tuple[float, float, str]] = []
        if not len(self.labels):
            return segs
        start = 0
        for i in range(1, len(self.labels) + 1):
            if i == len(self.labels) or self.labels[i] != self.labels[start]:
                segs.append((float(self.times[start]), float(self.times[i - 1]), CLASSES[int(self.labels[start])]))
                start = i
        return segs

    def class_fractions(self) -> dict[str, float]:
        n = max(len(self.labels), 1)
        return {c: round(float(np.sum(self.labels == i)) / n, 3) for i, c in enumerate(CLASSES)}


def analyse_events(analysis: Analysis, duration_s: float, rate_hz: float) -> MotionEvents:
    n = math.floor(duration_s * rate_hz + 1e-9) + 1
    grid = np.arange(n) / rate_hz
    fa = analysis.frame_rate
    hw = max(1, round(0.25 * fa))

    conf = analysis.confidence.copy()
    omega = analysis.angular_velocity.copy()
    vel = analysis.velocity.copy()
    low = conf < 0.15
    omega[low] = 0.0
    vel[low] = 0.0
    omega = hampel(omega, hw)
    vel = hampel(vel, hw)
    # never carry motion across an edit: zero a short window around each cut
    cut_mask_a = np.zeros(len(analysis.times), dtype=bool)
    for tc in analysis.cuts:
        cut_mask_a |= np.abs(analysis.times - tc) < 0.25
    omega[cut_mask_a] = 0.0
    vel[cut_mask_a] = 0.0

    times = analysis.times
    if len(times) < 2:
        times = np.array([0.0, max(duration_s, 1e-3)])
        omega = np.zeros((2, 3))
        vel = np.zeros((2, 3))
        conf = np.zeros(2)
    omega_g = lowpass(resample(times, omega, grid), min(8.0, fa / 2.5), rate_hz)
    vel_g = lowpass(resample(times, vel, grid), min(4.0, fa / 3.0), rate_hz)
    conf_g = resample(times, conf, grid)
    cut_g = np.zeros(n, dtype=bool)
    for tc in analysis.cuts:
        cut_g |= np.abs(grid - tc) < 0.5

    # accelerations (scale-free) in the vehicle frame: a = dv/dt + omega x v with v ~ (forward, 0, 0):
    #   lateral  a_y = +forward * yaw rate     (turns: towards the inside of the curve)
    #   vertical a_z = -forward * pitch rate   (+pitch = nose down: dips, loops and pull-ups push
    #                                           the rider into the seat, crests and drops lift them)
    vel_s = lowpass(vel_g, 1.5, rate_hz)
    acc = np.gradient(vel_s, 1.0 / rate_hz, axis=0)
    forward = vel_s[:, 0]
    omega_s = lowpass(omega_g, 1.5, rate_hz)
    acc[:, 1] += forward * omega_s[:, 2]
    acc[:, 2] -= forward * omega_s[:, 1]
    acc = lowpass(acc, 2.0, rate_hz)

    # banking (leaky integral of roll rate, time constant 3 s)
    leak = np.exp(-1.0 / (3.0 * rate_hz))
    bank = np.zeros(n)
    for i in range(1, n):
        bank[i] = leak * bank[i - 1] + omega_g[i, 0] / rate_hz

    # vibration: 3-12 Hz content of rotation and translation, RMS over 0.5 s, normalised
    hf = np.concatenate([bandpass(omega_g, 3.0, 12.0, rate_hz), bandpass(vel_g, 3.0, 12.0, rate_hz)], axis=1)
    vib_raw = _rms(np.sum(hf * hf, axis=1), max(1, int(0.5 * rate_hz)))
    vib_scale = robust_scale(vib_raw, 95.0)
    vibration = np.clip(vib_raw / vib_scale, 0.0, 1.5) / 1.5 if vib_scale > 0 else np.zeros(n)

    # impacts: sharp, *isolated* peaks in the change of motion - robust z-score > 6 against the whole
    # programme AND > 5 against the preceding, the surrounding and the following IMPACT_CONTEXT_S
    # (a continuously fast or rough section, or its onset, is not a series of impacts); local
    # maxima at least 0.3 s apart
    change = np.linalg.norm(np.gradient(np.concatenate([omega_g, vel_g], axis=1), axis=0), axis=1) * rate_hz
    med = float(np.median(change))
    mad = float(np.median(np.abs(change - med))) * 1.4826 + 1e-9
    z = (change - med) / mad
    z_loc = _local_z(change, rate_hz, IMPACT_CONTEXT_S)
    peaks = (z > 6.0) & (z_loc > 5.0) & (change == maximum_filter1d(change, size=max(3, int(0.3 * rate_hz))))
    peaks &= ~cut_g
    impacts = [(float(grid[i]), float(min(z_loc[i] / 10.0, 1.0))) for i in np.nonzero(peaks)[0]]

    labels = _classify(grid, omega_g, vel_s, acc, vibration, conf_g, cut_g, rate_hz)
    events = _events(grid, acc, omega_g, impacts, analysis.cuts, rate_hz)
    stats = {
        "speed_p90": robust_scale(forward, 90.0),
        "accel_p95": robust_scale(np.linalg.norm(acc, axis=1), 95.0),
        "rotation_p95_rad_s": robust_scale(np.linalg.norm(omega_g, axis=1), 95.0),
    }
    return MotionEvents(
        times=grid,
        angular_velocity=omega_g,
        velocity=vel_g,
        acceleration=acc,
        confidence=conf_g,
        forward_speed=forward,
        banking=bank,
        vibration=vibration,
        labels=labels,
        impacts=impacts,
        events=events,
        stats=stats,
    )


def _rms(power: np.ndarray, size: int) -> np.ndarray:
    """Moving RMS of a non-negative power signal. The running-sum filter can return values a few ulp
    below zero next to exact zeros; clamp them so the square root never produces NaN."""
    return np.sqrt(np.maximum(uniform_filter1d(power, size=size, mode="nearest"), 0.0))


def _classify(
    t: np.ndarray,
    omega: np.ndarray,
    vel: np.ndarray,
    acc: np.ndarray,
    vib: np.ndarray,
    conf: np.ndarray,
    cut: np.ndarray,
    fs: float,
) -> np.ndarray:
    win = max(1, int(fs))  # 1 s windows

    def avg(x: np.ndarray) -> np.ndarray:
        return uniform_filter1d(x, size=win, axis=0, mode="nearest")

    rot_rms = _rms(np.sum(omega * omega, axis=1), win)
    roll_rms = _rms(omega[:, 0] ** 2, win)
    pitch_rms = _rms(omega[:, 1] ** 2, win)
    yaw_rms = _rms(omega[:, 2] ** 2, win)
    speed = np.linalg.norm(vel, axis=1)
    fwd_frac = avg(((vel[:, 0] > 0.7 * speed) & (speed > 1e-6)).astype(float))
    speed_ref = robust_scale(speed, 90.0)
    moving = avg(speed) > 0.3 * speed_ref if speed_ref > 0 else np.zeros(len(t), dtype=bool)
    acc_mag = avg(np.linalg.norm(acc, axis=1))
    acc_ref = float(np.median(acc_mag[acc_mag > 0])) if np.any(acc_mag > 0) else 0.0
    conf_w = avg(conf)

    labels = np.full(len(t), CLASSES.index("handheld"), dtype=np.int64)
    sustained = moving & (fwd_frac > 0.6)
    bank_ratio = (roll_rms + 0.5 * pitch_rms) / np.maximum(rot_rms, 1e-9)
    labels[sustained & (yaw_rms >= roll_rms)] = CLASSES.index("vehicle")
    labels[sustained & (bank_ratio > 0.55) & (avg(vib) < 0.35)] = CLASSES.index("flight")
    rapid = (rot_rms > 0.8) | ((acc_ref > 0) & (acc_mag > 3.0 * acc_ref) & moving)
    labels[rapid] = CLASSES.index("rapid")
    still = (rot_rms < 0.02) & ~moving
    labels[still | (conf_w < 0.15)] = CLASSES.index("static")
    labels[cut] = CLASSES.index("cut")
    smoothed = median_filter(labels, size=max(1, win | 1), mode="nearest")
    smoothed[cut] = CLASSES.index("cut")
    return bridge_handheld(smoothed, fs, BRIDGE_HANDHELD_S)


def bridge_handheld(labels: np.ndarray, fs: float, max_s: float) -> np.ndarray:
    """The camera mounting does not change for a few seconds without an edit. A "handheld" stretch of
    at most `max_s` between two ride-class stretches (vehicle, flight, rapid) is a calmer part of
    the same ride - e.g. distant scenery makes the monocular speed proxy small - and becomes
    "vehicle". Static stretches and cuts are kept: they can be real stops and edits."""
    out = labels.copy()
    hh = CLASSES.index("handheld")
    ride = {CLASSES.index(c) for c in RIDE_CLASSES}
    n = len(out)
    i = 0
    while i < n:
        if out[i] != hh:
            i += 1
            continue
        j = i
        while j < n and out[j] == hh:
            j += 1
        if i > 0 and j < n and out[i - 1] in ride and out[j] in ride and (j - i) <= max_s * fs:
            out[i:j] = CLASSES.index("vehicle")
        i = j
    return out


def _local_z(x: np.ndarray, fs: float, window_s: float) -> np.ndarray:
    """Smallest robust z-score of each sample against the windows of `window_s` before, around and
    after it: (x - median) / (median - 16th percentile), i.e. one sigma from the lower half of the
    window, which stays valid when up to half of the window is a rough section. Moving percentiles
    are evaluated at about 10 Hz."""
    step = max(1, round(fs / 10.0))
    xd = x[::step]
    half = max(1, int(window_s * fs / step) // 2)
    size = 2 * half + 1
    pos = np.arange(len(x)) / step
    grid = np.arange(len(xd))
    z = np.full(len(x), np.inf)
    for origin in (half, 0, -half):  # past, centred, future window (scipy: +origin shifts left)
        q16, q50 = (
            np.interp(pos, grid, percentile_filter(xd, q, size=size, mode="nearest", origin=origin)) for q in (16, 50)
        )
        z = np.minimum(z, (x - q50) / (q50 - q16 + 1e-9))
    return z


def _events(
    t: np.ndarray,
    acc: np.ndarray,
    omega: np.ndarray,
    impacts: list[tuple[float, float]],
    cuts: list[float],
    fs: float,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    ref = robust_scale(acc[:, 0], 90.0)

    def onsets(mask: np.ndarray, kind: str, values: np.ndarray) -> None:
        idx = np.nonzero(mask & ~np.roll(mask, 1))[0]
        for i in idx:
            out.append({"time": round(float(t[i]), 3), "type": kind, "magnitude": round(float(abs(values[i])), 4)})

    if ref > 0:
        onsets(acc[:, 0] > 0.8 * ref, "acceleration", acc[:, 0])
        onsets(acc[:, 0] < -0.8 * ref, "deceleration", acc[:, 0])
    vert_ref = robust_scale(acc[:, 2], 90.0)
    if vert_ref > 0:
        onsets(acc[:, 2] > 0.8 * vert_ref, "dip", acc[:, 2])  # pushed into the seat (valley, loop)
        onsets(acc[:, 2] < -0.8 * vert_ref, "crest", acc[:, 2])  # lifted out of the seat (hill top, drop)
    yaw_ref = robust_scale(omega[:, 2], 90.0)
    if yaw_ref > 0.05:
        onsets(np.abs(omega[:, 2]) > 0.8 * yaw_ref, "turn", omega[:, 2])
    roll_ref = robust_scale(omega[:, 0], 90.0)
    if roll_ref > 0.05:
        onsets(np.abs(omega[:, 0]) > 0.8 * roll_ref, "banking", omega[:, 0])
    out += [{"time": round(ti, 3), "type": "impact", "magnitude": round(m, 3)} for ti, m in impacts]
    out += [{"time": round(c, 3), "type": "cut", "magnitude": 0.0} for c in cuts]
    out.sort(key=lambda e: float(e["time"]))  # type: ignore[arg-type]
    return out
