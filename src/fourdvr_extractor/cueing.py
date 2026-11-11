"""Perceptual motion cueing (classical washout with tilt coordination).

Camera motion is NOT copied to the platform. Instead the vestibular cues a passenger would feel
are synthesised within the platform's small workspace (see MOTION_ALGORITHM.md):

* onset of linear acceleration  -> short translational motion (high-pass + double integration +
  washout, so the platform always drifts back to neutral);
* sustained acceleration / turning -> slow tilt of the seat so gravity supplies the sustained
  specific force (tilt coordination), rate-limited below the rotation-perception threshold;
* rotation onsets (turns, banking, pitching) -> high-pass filtered rotation, washed out;
* vibration and impacts -> small high-frequency texture on heave/pitch/roll.

Every channel is scaled by configurable gains and by a per-class gain schedule (static shots and
handheld wobble are suppressed; vehicles, flights and rides are cued).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.ndimage import gaussian_filter1d, uniform_filter1d

from .events import CLASSES, MotionEvents
from .signals import bandpass, fade_envelope, lowpass, robust_scale, slew_limit, washout_angle, washout_position

AXES = ("surge", "sway", "heave", "roll", "pitch", "yaw")
G = 9.81

DEFAULT_CLASS_GAINS: dict[str, tuple[float, float, float]] = {
    # class: (translation/tilt, rotation, vibration+impacts)
    "static": (0.0, 0.0, 0.0),
    "cut": (0.0, 0.0, 0.0),
    "handheld": (0.15, 0.25, 0.2),
    "vehicle": (1.0, 0.8, 1.0),
    "flight": (0.8, 1.0, 0.3),
    "rapid": (1.0, 1.0, 1.0),
}


@dataclass
class CueingSettings:
    global_gain: float = 1.0
    axis_gains: dict[str, float] = field(default_factory=lambda: dict.fromkeys(AXES, 1.0))
    peak_specific_force: float = 1.5  # m/s^2 cue for the 95th percentile of the acceleration proxy
    rotation_gain: float = 0.6  # platform rotation rate per camera rotation rate (before washout)
    translation_hp_hz: float = 0.5
    translation_washout_hz: float = 0.12
    rotation_hp_hz: float = 0.25
    yaw_hp_hz: float = 0.35
    tilt_coordination: bool = True
    tilt_lp_hz: float = 0.35
    max_tilt_deg: float = 7.0
    tilt_rate_deg_s: float = 3.0  # below the vestibular rotation threshold
    vibration_gain: float = 1.0
    vibration_heave_m: float = 0.002
    vibration_rotation_rad: float = 0.004
    impact_gain: float = 1.0
    impact_heave_m: float = 0.012
    impact_duration_s: float = 0.15
    fade_s: float = 1.5  # start and end at neutral (seamless programme looping)
    class_gains: dict[str, tuple[float, float, float]] = field(default_factory=lambda: dict(DEFAULT_CLASS_GAINS))

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["class_gains"] = {k: list(v) for k, v in self.class_gains.items()}
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CueingSettings:
        base = cls()
        for key, value in data.items():
            if not hasattr(base, key):
                raise ValueError(f"unknown cueing setting {key!r}")
            if key == "class_gains":
                value = {k: tuple(v) for k, v in dict(value).items()}  # type: ignore[call-overload]
                merged = dict(DEFAULT_CLASS_GAINS)
                merged.update(value)  # type: ignore[arg-type]
                value = merged
            elif key == "axis_gains":
                merged_axes = dict.fromkeys(AXES, 1.0)
                merged_axes.update(dict(value))  # type: ignore[call-overload]
                value = merged_axes
            setattr(base, key, value)
        return base


@dataclass
class CueChannels:
    """Individual channels (for inspection in the GUI and tests), all (N, 6) or (N,)."""

    translational: np.ndarray
    tilt: np.ndarray
    rotational: np.ndarray
    texture: np.ndarray
    gain_schedule: np.ndarray  # (N, 3) translation/rotation/texture multipliers


def _schedule(events: MotionEvents, settings: CueingSettings, fs: float) -> np.ndarray:
    gains = np.array([settings.class_gains.get(c, (1.0, 1.0, 1.0)) for c in CLASSES], dtype=np.float64)
    sched = gains[events.labels]
    # smooth class changes over ~1 s so gains never step
    return uniform_filter1d(uniform_filter1d(sched, size=max(1, int(fs)), axis=0), size=max(1, int(0.5 * fs)), axis=0)


def compute_cues(
    events: MotionEvents, fs: float, settings: CueingSettings | None = None
) -> tuple[np.ndarray, CueChannels]:
    s = settings or CueingSettings()
    n = len(events.times)
    sched = _schedule(events, s, fs)
    g_t, g_r, g_x = sched[:, 0], sched[:, 1], sched[:, 2]

    # specific-force proxy -> perceptual units (m/s^2)
    acc = events.acceleration
    ref = robust_scale(np.linalg.norm(acc, axis=1), 95.0)
    sf = (acc / ref * s.peak_specific_force) if ref > 0 else np.zeros_like(acc)
    sf = sf * g_t[:, None]

    # 1) translational high-frequency channel
    trans = np.zeros((n, 6))
    for k in range(3):
        hp = s.translation_hp_hz * (1.5 if k == 2 else 1.0)
        trans[:, k] = washout_position(sf[:, k], fs, hp, s.translation_washout_hz)

    # 2) tilt coordination (sustained surge/sway specific force rendered by gravity)
    tilt = np.zeros((n, 6))
    if s.tilt_coordination:
        lp = lowpass(sf[:, :2], s.tilt_lp_hz, fs, order=2)
        max_tilt = math.radians(s.max_tilt_deg)
        pitch = -np.arcsin(np.clip(lp[:, 0] / G, -math.sin(max_tilt), math.sin(max_tilt)))
        roll = np.arcsin(np.clip(lp[:, 1] / G, -math.sin(max_tilt), math.sin(max_tilt)))
        rate = math.radians(s.tilt_rate_deg_s)
        # Gaussian smoothing removes the slew limiter's corners without overshoot (stays within max tilt)
        tilt[:, 4] = gaussian_filter1d(slew_limit(pitch, rate, fs), sigma=0.15 * fs, mode="nearest")
        tilt[:, 3] = gaussian_filter1d(slew_limit(roll, rate, fs), sigma=0.15 * fs, mode="nearest")

    # 3) rotational channel (onsets of turning, banking, pitching), washed out
    rot = np.zeros((n, 6))
    rates = events.angular_velocity * s.rotation_gain * g_r[:, None]
    rot[:, 3] = washout_angle(rates[:, 0], fs, s.rotation_hp_hz)
    rot[:, 4] = washout_angle(rates[:, 1], fs, s.rotation_hp_hz)
    rot[:, 5] = washout_angle(rates[:, 2], fs, s.yaw_hp_hz)

    # 4) texture: vibration and impacts
    tex = np.zeros((n, 6))
    hf = bandpass(np.concatenate([events.angular_velocity, events.velocity], axis=1), 3.0, 12.0, fs)
    if len(hf):
        heave_src = hf[:, 3:].sum(axis=1)
        sc = robust_scale(heave_src, 98.0)
        if sc > 0:
            tex[:, 2] = heave_src / sc * s.vibration_heave_m
        for k, axis in ((0, 3), (1, 4)):
            sc = robust_scale(hf[:, k], 98.0)
            if sc > 0:
                tex[:, axis] = hf[:, k] / sc * s.vibration_rotation_rad
        tex *= (s.vibration_gain * np.clip(events.vibration, 0.0, 1.0))[:, None]
    width = max(2, int(s.impact_duration_s * fs))
    pulse = 0.5 * (1 - np.cos(2 * np.pi * np.arange(width) / (width - 1)))
    for t_imp, mag in events.impacts:
        i0 = int(t_imp * fs)
        seg = slice(i0, min(n, i0 + width))
        m = pulse[: seg.stop - seg.start] * mag * s.impact_gain
        tex[seg, 2] -= m * s.impact_heave_m
        tex[seg, 4] += m * s.impact_heave_m * 0.5
    tex *= g_x[:, None]

    cues = trans + tilt + rot + tex
    gains = np.array([s.axis_gains.get(a, 1.0) for a in AXES]) * s.global_gain
    cues = cues * gains[None, :]
    cues *= fade_envelope(n, fs, s.fade_s)[:, None]
    return cues, CueChannels(trans, tilt, rot, tex, sched)
