"""From a recorded live session to a programme (``.motion`` with effects and bass).

Live, the cueing can only use what has already happened. Afterwards the whole session is known, so
the programme is made with the Editor's own offline stages: the perceptual cueing of the extractor
(same filters, plus zero-phase smoothing and a gain chosen from the whole recording), the constraint
stage that fits the motion into one platform's limits, and the validation. The session's effect
levels become effect tracks, the shakers' sound becomes the programme's bass track.

A programme for the theatre belongs to a **video** - the Control Server only accepts a motion file
that was made for exactly the video of its programme, and of the same length. ``video=`` binds the
programme to a recording of the session (a screen or 360 capture made at the same time):
``video_offset_s`` says at which second of the session that video begins, and ``find_offset`` finds
it by comparing how the picture turns with how the telemetry says the vehicle turned.

Needs the workstation packages (SciPy, OpenCV), like everything that makes programmes.
"""

from __future__ import annotations

import datetime as dt
import math
import platform as host_platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from fourdvr_extractor.constraints import ConstraintSettings, apply_constraints
from fourdvr_extractor.cueing import CueingSettings, compute_cues
from fourdvr_extractor.events import CLASSES, MotionEvents
from fourdvr_extractor.pipeline import DEFAULT_PROFILE
from fourdvr_extractor.soundtrack import default_bass_rate
from fourdvr_extractor.validation import ValidationReport, validate
from fourdvr_fx import BassTrack, Cue, EffectsDocument, effect_type
from fourdvr_fx.embed import attach_effects
from fourdvr_motion import motionfile
from fourdvr_motion.platform_config import PlatformConfig, load_platform_config

from . import __version__
from .sample import AXES, LiveSample
from .session import Session
from .shaker import ShakerSettings, SynthFeed

ProgressFn = Callable[[float, str], None]
KIND_CLASS = {"car": "vehicle", "aircraft": "flight", "ride": "rapid", "any": "rapid"}
ON = 0.02                 # an effect level above this counts as "on"


@dataclass
class ConvertOptions:
    profile: str | Path | PlatformConfig | None = None     # None: the bundled example platform
    programme_id: int = 1
    gain: float = 1.0
    kind: str = "auto"                 # auto | car | aircraft | ride
    effects: bool = True
    bass: bool = True
    start_s: float = 0.0
    end_s: float = 0.0                 # 0: to the end
    sample_rate_hz: float = 100.0
    allow_violations: bool = False
    cueing: CueingSettings | None = None
    video: str | Path | None = None    # the video the programme is for (sets its length and its video reference)
    video_offset_s: float = 0.0        # the second of the session at which that video begins
    projection: str | None = None      # of that video, where the guess from its shape is wrong
    stereo: str | None = None


@dataclass
class ConvertResult:
    path: Path | None
    duration_s: float
    passed: bool
    stroke_utilisation: float
    peaks: str
    effects: list[str]
    bass: bool
    report: ValidationReport
    motion: np.ndarray
    document: EffectsDocument | None
    sha256: str = ""


def trimmed(session: Session, start_s: float = 0.0, end_s: float = 0.0) -> Session:
    """The part of a session between two times (seconds from its start)."""
    n = session.samples
    i0 = max(0, min(n, round(start_s * session.rate_hz)))
    i1 = n if end_s <= 0.0 else max(i0, min(n, round(end_s * session.rate_hz) + 1))
    if i0 == 0 and i1 == n:
        return session
    header = {**session.header, "samples": i1 - i0, "duration_s": round(max(0, i1 - i0 - 1) / session.rate_hz, 4)}
    return Session(header, session.accel[i0:i1], session.rates[i0:i1], session.attitude[i0:i1],
                   {k: v[i0:i1] for k, v in session.scalars.items()}, session.pose[i0:i1], session.effects[i0:i1],
                   session.path)


def fitted(session: Session, start_s: float, duration_s: float) -> Session:
    """The session from `start_s` on, exactly `duration_s` long: what lies outside the recording is rest."""
    rate = session.rate_hz
    n = math.floor(duration_s * rate + 1e-9) + 1
    i0 = round(start_s * rate)
    lo, hi = max(0, i0), min(session.samples, i0 + n)

    def cut(a: np.ndarray, fill: float = 0.0) -> np.ndarray:
        out = np.full((n, *a.shape[1:]), fill, dtype=a.dtype)
        if hi > lo:
            out[lo - i0 : hi - i0] = a[lo:hi]
        return out

    header = {**session.header, "samples": n, "duration_s": round(duration_s, 4)}
    return Session(header, cut(session.accel), cut(session.rates), cut(session.attitude, np.nan),
                   {k: cut(v) for k, v in session.scalars.items()}, cut(session.pose), cut(session.effects), session.path)


def find_offset(session: Session, video: str | Path, seconds: float = 90.0, progress: ProgressFn | None = None,
                projection: str | None = None, stereo: str | None = None) -> tuple[float, float]:
    """At which second of the session does this video begin? Returns (offset, confidence 0..1).

    The first `seconds` of the video are analysed for how the camera turns (the extractor's own
    analysis), and that is slid along the session's rotation rates until the two agree best. A
    negative offset means the video began before the recording. A confidence below about 0.4 means
    "not found": the picture does not turn the way the telemetry says (another session, a third-
    person or replay camera, or hardly any turning in that part)."""
    from fourdvr_extractor.analysis import AnalysisSettings, analyse
    from fourdvr_extractor.video import probe

    info = probe(video, with_hash=False)
    if projection:
        info.projection = projection
    if stereo:
        info.stereo = stereo
    limit = max(10.0, seconds)
    frames = [0]

    def enough() -> bool:
        frames[0] += 1
        return frames[0] / max(info.fps, 1.0) > limit

    analysis = analyse(info, AnalysisSettings(max_width=480, max_fps=15.0), progress, enough)
    fs = 20.0
    span = float(analysis.times[-1])
    if span < 5.0:
        raise ValueError("the video is too short to find where it belongs in the session (less than 5 s analysed)")
    grid = np.arange(0.0, span, 1.0 / fs)
    seen = np.stack([np.interp(grid, analysis.times, analysis.angular_velocity[:, k]) for k in range(3)], axis=1)
    seen *= np.interp(grid, analysis.times, analysis.confidence)[:, None] > 0.15
    t = np.arange(session.samples) / session.rate_hz
    told_grid = np.arange(0.0, session.duration_s, 1.0 / fs)
    told = np.stack([np.interp(told_grid, t, session.rates[:, k]) for k in range(3)], axis=1)

    def smooth(x: np.ndarray) -> np.ndarray:
        kernel = np.hanning(int(fs * 0.6) | 1)
        kernel /= kernel.sum()
        return np.stack([np.convolve(x[:, k], kernel, mode="same") for k in range(x.shape[1])], axis=1)

    seen, told = smooth(seen), smooth(told)
    pad = len(seen)                                  # the video may begin up to its analysed length before the session
    told = np.concatenate([np.zeros((pad, 3)), told, np.zeros((pad, 3))])
    best, best_lag = -1.0, 0
    energy_seen = float(np.sum(seen * seen))
    if energy_seen <= 1e-9 or float(np.sum(told * told)) <= 1e-9:
        return 0.0, 0.0
    cumulative = np.concatenate([[0.0], np.cumsum(np.sum(told * told, axis=1))])
    cross = np.zeros(len(told) - len(seen) + 1)
    for k in range(3):
        cross += np.correlate(told[:, k], seen[:, k], mode="valid")
    for lag in range(len(cross)):
        energy = cumulative[lag + len(seen)] - cumulative[lag]
        if energy <= 0.05 * energy_seen:
            continue
        score = float(cross[lag]) / math.sqrt(energy * energy_seen)
        if score > best:
            best, best_lag = score, lag
    return (best_lag - pad) / fs, max(0.0, best)


def _resample(times: np.ndarray, values: np.ndarray, grid: np.ndarray) -> np.ndarray:
    if values.ndim == 1:
        return np.interp(grid, times, values)
    return np.stack([np.interp(grid, times, values[:, k]) for k in range(values.shape[1])], axis=1)


def session_kind(session: Session, kind: str = "auto") -> str:
    if kind != "auto":
        return kind
    return str(session.header.get("source", {}).get("kind", "any"))


def events_from_session(session: Session, rate_hz: float, kind: str = "auto", duration_s: float | None = None) -> MotionEvents:
    """The session as the extractor's "motion events": what a video analysis would have had to guess
    (accelerations, rotation rates, vibration, impacts) is measured here."""
    if session.samples < 2:
        raise ValueError("the session is too short to make a programme of (less than two samples)")
    n = motionfile.expected_sample_count(session.duration_s if duration_s is None else duration_s, rate_hz)
    grid = np.arange(n) / rate_hz
    times = np.arange(session.samples) / session.rate_hz
    active = _resample(times, session.active.astype(np.float64), grid) > 0.5
    accel = _resample(times, session.accel, grid) * active[:, None]
    rates = _resample(times, session.rates, grid) * active[:, None]
    speed = _resample(times, session.scalars["speed"], grid)
    velocity = np.zeros((n, 3))
    velocity[:, 2] = np.cumsum(accel[:, 2]) / rate_hz        # only its 3-12 Hz content is used (road texture)
    rumble = np.clip(_resample(times, session.scalars["rumble"], grid), 0.0, 1.0) * active
    labels = np.full(n, CLASSES.index("static"), dtype=np.int64)
    labels[active] = CLASSES.index(KIND_CLASS.get(session_kind(session, kind), "rapid"))
    hits = session.scalars["impact"] * session.active
    impacts: list[tuple[float, float]] = []
    gap = max(1, round(0.3 * session.rate_hz))
    i = 0
    while i < len(hits):
        if hits[i] > 0.2:
            j = min(len(hits), i + gap)
            k = i + int(np.argmax(hits[i:j]))
            impacts.append((k / session.rate_hz, float(hits[k])))
            i = j
        else:
            i += 1
    bank = np.zeros(n)
    att = session.attitude[:, 0]
    if np.all(np.isfinite(att)):
        bank = _resample(times, att, grid)
    return MotionEvents(
        times=grid, angular_velocity=rates, velocity=velocity, acceleration=accel, confidence=active.astype(np.float64),
        forward_speed=speed, banking=bank, vibration=rumble, labels=labels, impacts=impacts,
        events=[{"t": round(t, 3), "type": "impact", "magnitude": round(m, 3)} for t, m in impacts][:2000],
        stats={"speed_p90": float(np.percentile(speed, 90.0)),
               "accel_p95": float(np.percentile(np.linalg.norm(accel, axis=1), 95.0)),
               "rotation_p95_rad_s": float(np.percentile(np.linalg.norm(rates, axis=1), 95.0))},
    )


def _simplify(times: np.ndarray, values: np.ndarray, tolerance: float = 0.03, limit: int = 4000) -> list[tuple[float, float]]:
    """A level curve as few points as describe it to within `tolerance` (Ramer-Douglas-Peucker)."""
    keep = np.zeros(len(values), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(values) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        line = values[a] + (values[b] - values[a]) * (times[a + 1 : b] - times[a]) / (times[b] - times[a])
        err = np.abs(values[a + 1 : b] - line)
        k = int(np.argmax(err))
        if err[k] > tolerance:
            mid = a + 1 + k
            keep[mid] = True
            stack += [(a, mid), (mid, b)]
    idx = np.nonzero(keep)[0]
    if len(idx) > limit:
        idx = idx[np.linspace(0, len(idx) - 1, limit).astype(int)]
    return [(round(float(times[i]), 3), round(float(min(1.0, max(0.0, values[i]))), 3)) for i in idx]


def effects_from_session(session: Session, bass: bool = True, duration_s: float | None = None) -> EffectsDocument:
    """Effect tracks from the levels the live rules asked for: continuous effects (wind, vibration)
    as curves, the others as one cue per stretch in which the effect was on."""
    duration = session.duration_s if duration_s is None else duration_s
    doc = EffectsDocument(duration_s=duration, meta={"drafted_from": "live session"})
    times = np.arange(session.samples) / session.rate_hz
    for name in session.effect_names:
        level = np.asarray(session.level(name), dtype=np.float64)
        if session.samples < 2 or float(np.max(level)) <= ON:
            continue
        if effect_type(name).editing == "curve":
            smooth = np.convolve(level, np.ones(9) / 9.0, mode="same")
            track = doc.new_track(name)
            track.points = [(min(tp, round(duration, 3)), v) for tp, v in _simplify(times, smooth)]
            if track.points[0][0] > 0.0:
                track.points.insert(0, (0.0, 0.0))
            continue
        track = doc.new_track(name)
        on = level > ON
        gap = round(0.4 * session.rate_hz)
        i = 0
        while i < len(on):
            if not on[i]:
                i += 1
                continue
            j = i
            last = i
            while j < len(on) and (on[j] or j - last <= gap):
                if on[j]:
                    last = j
                j += 1
            start, end = times[i], min(times[last] + 1.0 / session.rate_hz, duration)
            track.cues.append(Cue(t=round(float(start), 3), duration=round(float(max(end - start, 0.1)), 3),
                                  level=round(float(np.max(level[i : last + 1])), 3)))
            i = last + 1
    if bass:
        doc.bass = bass_from_session(session, duration_s=duration)
    return doc


def bass_from_session(session: Session, settings: ShakerSettings | None = None, duration_s: float | None = None) -> BassTrack | None:
    """The shakers' sound of the session (engine tone, road rumble, thumps), as the bass track of the
    programme - the same generator as live, run over the recording."""
    total = session.duration_s if duration_s is None else duration_s
    rate = default_bass_rate(total)
    feed = SynthFeed(settings)
    sc = session.scalars
    per = rate / session.rate_hz
    out = np.zeros(math.ceil(session.samples * per) + 2, dtype=np.float32)
    done = 0.0
    position = 0
    for i in range(session.samples):
        sample = LiveSample(rpm=float(sc["rpm"][i]), rpm_max=float(sc["rpm_max"][i]), rpm_idle=float(sc["rpm_idle"][i]),
                            throttle=float(sc["throttle"][i]), rumble=float(sc["rumble"][i]), slip=float(sc["slip"][i]),
                            impact=float(sc["impact"][i]))
        feed.update(sample, float(sc["envelope"][i]))
        done += per
        frames = int(done) - position
        if frames > 0:
            out[position : position + frames] = feed.read(0, frames, rate, 0.0)
            position += frames
    want = round(total * rate)
    y = out[:want] if position >= want else np.concatenate([out[:position], np.zeros(want - position, dtype=np.float32)])
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak < 0.01:
        return None
    gain = min(0.9 / peak, 4.0)
    return BassTrack.from_float(y * gain, rate, cutoff_hz=120.0,
                                source=f"live session: engine, road and impacts, level {20 * math.log10(gain):+.1f} dB")


def _profile(value: str | Path | PlatformConfig | None) -> PlatformConfig:
    if isinstance(value, PlatformConfig):
        return value
    return load_platform_config(value if value else DEFAULT_PROFILE)


def convert_session(
    session: Session,
    output: str | Path | None,
    options: ConvertOptions | None = None,
    progress: ProgressFn | None = None,
) -> ConvertResult:
    """Make the programme; write it to `output` (None: only work it out)."""
    o = options or ConvertOptions()
    say = progress or (lambda _f, _t: None)
    profile = _profile(o.profile)
    fs = o.sample_rate_hz
    video: dict[str, Any] = {"filename": "", "sha256": "", "width": 0, "height": 0, "frame_rate": 0.0,
                             "projection": "flat", "stereo": "mono",
                             "note": "made from a recorded live session, not from a video"}
    if o.video is not None:
        from fourdvr_extractor.video import probe

        say(0.02, f"reading {Path(o.video).name}")
        info = probe(o.video)
        if o.projection:
            info.projection = o.projection
        if o.stereo:
            info.stereo = o.stereo
        part = fitted(session, o.video_offset_s, info.duration_s)
        duration = float(info.duration_s)
        video = {**info.to_dict(), "session_offset_s": round(o.video_offset_s, 3)}
        if not part.active.any():
            raise ValueError(f"with the video beginning at second {o.video_offset_s:g} of the session, nothing of the "
                             f"session falls into the video's {duration:.1f} s")
    else:
        part = trimmed(session, o.start_s, o.end_s)
        duration = part.duration_s
    video["duration_s"] = round(duration, 4)
    say(0.1, f"session: {part.duration_s:.1f} s from {part.summary()['source']}")
    events = events_from_session(part, fs, o.kind, duration)
    settings = CueingSettings.from_dict((o.cueing or CueingSettings()).to_dict())
    settings.global_gain *= o.gain
    raw, _channels = compute_cues(events, fs, settings)
    say(0.4, "motion cues made; fitting them to the platform")
    constrained = apply_constraints(raw, fs, profile, ConstraintSettings())
    motion = constrained.motion
    report = validate(motion, fs, profile)
    say(0.7, f"validation {'passed' if report.passed else 'failed: ' + '; '.join(report.violations[:3])}")
    doc = effects_from_session(part, o.bass, duration) if (o.effects or o.bass) else None
    if doc is not None and not o.effects:
        doc.tracks = []
    peaks = ", ".join(f"{a} {np.max(np.abs(motion[:, k])) * (1000.0 if k < 3 else 57.2958):.0f}"
                      f"{' mm' if k < 3 else '°'}" for k, a in enumerate(AXES)
                      if np.max(np.abs(motion[:, k])) > (0.001 if k < 3 else 0.002))
    result = ConvertResult(None, duration, report.passed, float(report.actuators.get("stroke_utilisation", 0.0)), peaks,
                           [] if doc is None else [t.type for t in doc.tracks], bool(doc is not None and doc.bass is not None),
                           report, motion, doc)
    if output is None:
        return result
    if not report.passed and not o.allow_violations:
        raise ValueError("the motion does not pass the validation for this platform: " + "; ".join(report.violations[:3])
                         + " (--allow-violations writes it anyway)")
    src = part.header.get("source", {})
    header = motionfile.build_header(
        programme_id=o.programme_id, duration_s=duration, sample_rate_hz=fs,
        video=video,
        generation={
            "generator": "Extended-4D-Editor", "generator_version": __version__,
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": host_platform.platform(terse=True),
            "live_session": {"file": part.path.name if part.path else "", "source": src.get("title") or src.get("source"),
                             "recorded_at": part.header.get("created_at"),
                             "start_s": o.video_offset_s if o.video is not None else o.start_s,
                             "end_s": 0.0 if o.video is not None else o.end_s,
                             "kind": session_kind(part, o.kind), "axes": part.header.get("axes", {})},
            "settings": {"sample_rate_hz": fs, "cueing": settings.to_dict()},
            "classes": events.class_fractions(), "events": events.events[:2000],
            "constraints": {"iterations": constrained.iterations, "converged": constrained.converged,
                            "axis_gain_min": constrained.axis_gain_min},
        },
        validation={
            "passed": report.passed, "profile": report.profile, "profile_sha256": report.profile_sha256,
            "violations": report.violations, "workspace_violations": report.workspace_violations,
            "stroke_utilisation": round(float(report.actuators.get("stroke_utilisation", 0.0)), 4),
            "disclaimer": "numerical limit check only; not a passenger-safety certification",
        },
    )
    target = Path(output)
    result.sha256 = motionfile.write_motion(target, attach_effects(header, doc), motion)
    result.path = target
    say(1.0, f"written: {target.name}")
    return result

