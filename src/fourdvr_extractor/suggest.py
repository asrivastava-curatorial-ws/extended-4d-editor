"""Suggested effects: a first draft of the effect tracks from what the analysis knows.

From the camera motion and the sound level:

  wind       follows the forward speed of the camera (ride, vehicle and flight passages)
  vibration  follows the camera shake and the low-frequency energy of the sound track
  air        a short blast at every impact-like motion event
  strobe     a flash where the picture flashes (lightning, explosions)

From scene detection (scene.py: a picture model and a sound model; only where they are installed):

  rain       while it rains in the picture - also on weaker picture evidence if rain is heard
  fog        a burst when a foggy or smoky scene begins, repeated while it lasts
  heat       while fire, an explosion or lava is in the picture
  bubbles    during underwater scenes and scenes with bubbles
  snow       while it snows or the scene is covered in snow
  water      a short spray where water splashes (seen, and heard where there is sound)
  scent      one release when a forest, sea or flower scene begins (cartridges 1, 2, 3)

Scene detection is evidence, not knowledge: it misses scenes and it sees some that are not there.
Suggestions are a starting point for the timeline editor - every cue and curve point can be moved,
changed or deleted, and what a venue's devices finally do is bounded by the venue's limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import median_filter

from fourdvr_fx import Cue, EffectsDocument

from .analysis import Analysis
from .events import CLASSES, MotionEvents
from .scene import Scene
from .signals import lowpass
from .soundtrack import FEATURE_RATE, Soundtrack

Point = tuple[float, float]
MOTION_SUGGESTABLE = ("wind", "vibration", "air", "strobe")
SCENE_SUGGESTABLE = ("rain", "fog", "heat", "bubbles", "snow", "water", "scent")
SUGGESTABLE = (*MOTION_SUGGESTABLE, *SCENE_SUGGESTABLE)
SEEN = 0.8                   # picture score from which a concept counts as seen
SEEN_WEAK = 0.35             # ... as possibly seen: counts only if the sound agrees
HEARD = 0.25                 # sound probability from which a cue counts as heard
CUT_SNAP_S = 0.6             # a scene that starts or ends this close to a cut starts or ends at the cut
FOG_BURST_S, FOG_REPEAT_S = 3.0, 20.0
SPRAY_S, SPRAY_REPEAT_S = 0.5, 4.0
SCENT_S, SCENT_REPEAT_S = 3.0, 90.0
SCENTS = (("forest", 1), ("sea", 2), ("flowers", 3))      # scene -> cartridge (slot) by convention
CURVE_RATE_HZ = 10.0
MAX_POINTS = 1500
FLASH_GROUP_S = 1.5          # flashes closer than this form one strobe cue
FLASH_RATE_HZ = 3.0


@dataclass
class Suggestion:
    type: str
    cues: list[Cue] = field(default_factory=list)
    points: list[Point] = field(default_factory=list)
    note: str = ""


def simplify(times: np.ndarray, values: np.ndarray, tolerance: float, max_points: int = MAX_POINTS) -> list[Point]:
    """Fewest points whose straight-line interpolation stays within `tolerance` of the curve."""
    n = len(times)
    if n == 0:
        return []
    if n <= 2:
        return [(round(float(t), 3), round(float(v), 3)) for t, v in zip(times, values, strict=True)]
    tol = tolerance
    while True:
        keep = np.zeros(n, dtype=bool)
        keep[0] = keep[-1] = True
        stack = [(0, n - 1)]
        while stack:
            a, b = stack.pop()
            if b <= a + 1:
                continue
            seg = times[a + 1 : b]
            line = values[a] + (values[b] - values[a]) * (seg - times[a]) / (times[b] - times[a])
            err = np.abs(values[a + 1 : b] - line)
            i = int(np.argmax(err))
            if err[i] > tol:
                k = a + 1 + i
                keep[k] = True
                stack.extend(((a, k), (k, b)))
        if int(keep.sum()) <= max_points:
            idx = np.nonzero(keep)[0]
            return [(round(float(times[i]), 3), round(float(values[i]), 3)) for i in idx]
        tol *= 1.6


def _curve(times: np.ndarray, level: np.ndarray, fs: float, tolerance: float) -> list[Point]:
    step = max(1, round(fs / CURVE_RATE_HZ))
    t, v = times[::step], np.clip(level[::step], 0.0, 1.0)
    if len(t) and t[-1] < times[-1]:
        t, v = np.append(t, times[-1]), np.append(v, min(1.0, max(0.0, float(level[-1]))))
    return simplify(t, v, tolerance)


def suggest_wind(ev: MotionEvents) -> Suggestion | None:
    if len(ev.times) < 3:
        return None
    fs = 1.0 / float(ev.times[1] - ev.times[0])
    speed = np.clip(ev.forward_speed, 0.0, None)
    moving = speed[speed > 0]
    scale = float(np.percentile(moving, 90)) if len(moving) > fs else 0.0
    if scale <= 1e-9:
        return None
    still = np.isin(ev.labels, [CLASSES.index("static"), CLASSES.index("cut")])
    gate = np.clip(lowpass((~still).astype(np.float64), 0.5, fs), 0.0, 1.0)
    level = np.clip(lowpass(np.clip(speed / scale, 0.0, 1.0) ** 0.7 * gate, 0.4, fs), 0.0, 1.0)
    level[level < 0.06] = 0.0
    if float(level.max()) < 0.15:
        return None
    return Suggestion("wind", points=_curve(ev.times, level, fs, 0.04), note="follows the camera's forward speed")


def suggest_vibration(ev: MotionEvents | None, st: Soundtrack | None, duration_s: float) -> Suggestion | None:
    if ev is not None and len(ev.times) >= 3:
        times = ev.times
        fs = 1.0 / float(times[1] - times[0])
        shake = np.clip(ev.vibration, 0.0, 1.0)
    else:
        fs = CURVE_RATE_HZ
        times = np.arange(int(duration_s * fs) + 1) / fs
        shake = np.zeros(len(times))
    rumble = np.zeros(len(times))
    if st is not None and len(st.bass):
        rumble = np.interp(times, np.arange(len(st.bass)) / FEATURE_RATE, st.bass, right=0.0)
    level = 1.0 - (1.0 - 0.8 * shake) * (1.0 - 0.7 * rumble)        # either source is enough
    level = np.clip(lowpass(level, 2.0, fs), 0.0, 1.0)
    level[level < 0.08] = 0.0
    if float(level.max()) < 0.15:
        return None
    sources = " and ".join(s for s, used in (("camera shake", shake.any()), ("bass in the sound track", rumble.any())) if used)
    return Suggestion("vibration", points=_curve(times, level, fs, 0.05), note=f"follows {sources}")


def suggest_air(ev: MotionEvents, min_gap_s: float = 1.0) -> Suggestion | None:
    if not ev.impacts:
        return None
    top = max(m for _t, m in ev.impacts) or 1.0
    cues: list[Cue] = []
    for t, magnitude in sorted(ev.impacts):
        level = round(min(1.0, 0.55 + 0.45 * magnitude / top), 2)
        if cues and t - cues[-1].t < min_gap_s:
            if level > cues[-1].level:
                cues[-1] = Cue(round(t, 3), 0.25, level)
            continue
        cues.append(Cue(round(t, 3), 0.25, level))
    return Suggestion("air", cues=cues, note="one blast per impact-like motion event")


def find_flashes(analysis: Analysis) -> list[float]:
    """Times at which the picture gets suddenly and briefly much brighter.

    Brightness is compared with its running median over about one second: a flash (shorter than half
    the window) stands out, while a cut or fade to a brighter shot moves the median with it and does not.
    """
    luma = analysis.luma
    if len(luma) < 8 or len(luma) != len(analysis.times):
        return []
    size = max(3, round(analysis.frame_rate) | 1)                     # about one second, odd
    excess = luma - median_filter(luma, size=size, mode="nearest")
    spread = 1.4826 * float(np.median(np.abs(excess - np.median(excess))))
    threshold = max(0.10, 6.0 * spread)
    out: list[float] = []
    for i in range(1, len(luma) - 1):
        if excess[i] > threshold and excess[i - 1] <= threshold:      # the frame on which it lights up
            out.append(float(analysis.times[i]))
    return out


def suggest_strobe(analysis: Analysis) -> Suggestion | None:
    flashes = find_flashes(analysis)
    if not flashes:
        return None
    cues: list[Cue] = []
    start = last = flashes[0]
    for t in [*flashes[1:], None]:
        if t is not None and t - last < FLASH_GROUP_S:
            last = t
            continue
        cues.append(Cue(round(start, 3), round(max(0.15, last - start + 0.15), 3), 1.0, params={"rate_hz": FLASH_RATE_HZ}))
        if t is not None:
            start = last = t
    return Suggestion("strobe", cues=cues, note="where the picture flashes")


def scene_segments(
    times: np.ndarray, active: np.ndarray, duration_s: float, min_len_s: float, max_gap_s: float, cuts: list[float] | None = None
) -> list[tuple[float, float]]:
    """Passages (start, end) in which `active` holds. One sample stands for the time around it; passages
    closer than `max_gap_s` are joined, shorter ones than `min_len_s` dropped, and ends near a scene
    cut are moved onto the cut."""
    if not len(times) or not np.any(active):
        return []
    step = float(np.median(np.diff(times))) if len(times) > 1 else 1.0
    raw: list[list[float]] = []
    for t, on in zip(times, active, strict=True):
        if not on:
            continue
        a, b = float(t) - step / 2, float(t) + step / 2
        if raw and a - raw[-1][1] <= max_gap_s + 1e-6:
            raw[-1][1] = b
        else:
            raw.append([a, b])
    out = []
    for a, b in raw:
        for cut in cuts or []:
            if abs(cut - a) <= CUT_SNAP_S:
                a = cut
            if abs(cut - b) <= CUT_SNAP_S:
                b = cut
        a, b = max(0.0, a), min(duration_s, b)
        if b - a >= min_len_s - 1e-6:
            out.append((round(a, 2), round(b, 2)))
    return out


def _span_cues(segments: list[tuple[float, float]], level: float, fade_in: float, fade_out: float) -> list[Cue]:
    return [Cue(a, round(b - a, 2), level, min(fade_in, (b - a) / 3), min(fade_out, (b - a) / 3)) for a, b in segments]


def _repeated(segments: list[tuple[float, float]], length: float, every: float, level: float = 1.0) -> list[Cue]:
    cues = []
    for a, b in segments:
        t = a
        while t < b - 1e-6 and (t == a or b - t >= length):
            cues.append(Cue(round(t, 2), round(min(length, b - t), 2), level))
            t += every
    return cues


def suggest_scene(scene: Scene, duration_s: float, cuts: list[float] | None = None) -> list[Suggestion]:
    """Rain, fog, heat, bubbles, snow, water spray and scent from the scene scores."""
    t = scene.times
    if not len(t):
        return []
    pic = {name: scene.picture.get(name, np.zeros(len(t))) for name in ("rain", "fog", "fire", "bubbles", "snow", "splash", "forest", "sea", "flowers")}
    snd = {cue: scene.sound_at(cue, t) for cue in ("rain", "fire", "explosion", "splash")}
    out: list[Suggestion] = []

    def seg(active: np.ndarray, min_len: float, gap: float) -> list[tuple[float, float]]:
        return scene_segments(t, active, duration_s, min_len, gap, cuts)

    rain = seg((pic["rain"] >= SEEN) | ((pic["rain"] >= SEEN_WEAK) & (snd["rain"] >= HEARD)), 2.0, 2.0)
    if rain:
        cues = []
        for a, b in rain:
            heard = float(np.mean(snd["rain"][(t >= a) & (t <= b)])) if scene.has_sound and np.any((t >= a) & (t <= b)) else 0.0
            level = round((0.6 + 0.4 * min(1.0, heard / 0.6)) * 20) / 20 if scene.has_sound else 0.7
            cues += _span_cues([(a, b)], level, 0.5, 0.5)
        out.append(Suggestion("rain", cues=cues, note="where it rains in the picture" + (" and in the sound" if scene.has_sound else "")))
    fog = seg(pic["fog"] >= SEEN, 3.0, 3.0)
    if fog:
        out.append(Suggestion("fog", cues=_repeated(fog, FOG_BURST_S, FOG_REPEAT_S), note="foggy or smoky scenes: a burst at the start, repeated while they last"))
    fire = seg((pic["fire"] >= SEEN) | ((pic["fire"] >= SEEN_WEAK) & (np.maximum(snd["fire"], snd["explosion"]) >= HEARD)), 1.0, 2.0)
    if fire:
        out.append(Suggestion("heat", cues=_span_cues(fire, 1.0, 0.3, 0.5), note="fire, explosions or lava in the picture"))
    bubbles = seg(pic["bubbles"] >= SEEN, 3.0, 3.0)
    if bubbles:
        out.append(Suggestion("bubbles", cues=_span_cues(bubbles, 0.8, 0.0, 0.0), note="underwater scenes and scenes with bubbles"))
    snow = seg(pic["snow"] >= SEEN, 3.0, 3.0)
    if snow:
        out.append(Suggestion("snow", cues=_span_cues(snow, 0.7, 0.0, 0.0), note="snowfall and scenes covered in snow"))
    splash = seg((pic["splash"] >= SEEN) & ((snd["splash"] >= 0.15) | (not scene.has_sound)), 1.0, 1.0)
    if splash:
        out.append(Suggestion("water", cues=_repeated(splash, SPRAY_S, SPRAY_REPEAT_S), note="where water splashes"))
    scents: list[Cue] = []
    for name, slot in SCENTS:
        last = -1e9
        for a, _b in seg(pic[name] >= SEEN, 5.0, 3.0):
            if a - last >= SCENT_REPEAT_S:
                scents.append(Cue(a, SCENT_S, 1.0, params={"slot": float(slot)}, label=name))
                last = a
    if scents:
        out.append(Suggestion("scent", cues=sorted(scents, key=lambda c: c.t), note="when a forest, sea or flower scene begins (cartridges 1, 2, 3)"))
    return out


def suggest_all(
    analysis: Analysis | None, events: MotionEvents | None, soundtrack: Soundtrack | None, duration_s: float,
    scene: Scene | None = None,
) -> list[Suggestion]:
    found = [
        suggest_wind(events) if events is not None else None,
        suggest_vibration(events, soundtrack, duration_s) if (events is not None or soundtrack is not None) else None,
        suggest_air(events) if events is not None else None,
        suggest_strobe(analysis) if analysis is not None else None,
    ]
    if scene is not None:
        found += suggest_scene(scene, duration_s, analysis.cuts if analysis is not None else None)
    return [s for s in found if s is not None]


def apply_suggestions(doc: EffectsDocument, suggestions: list[Suggestion], types: list[str] | None = None) -> list[str]:
    """Put suggestions into the tracks named after their type, replacing what those tracks held.

    Other tracks (and hand-made second tracks of the same type) are left alone. Returns the types written.
    """
    written: list[str] = []
    for s in suggestions:
        if types is not None and s.type not in types:
            continue
        track = doc.track(s.type) or doc.new_track(s.type)
        if track.id != s.type:
            continue
        track.cues = [Cue(c.t, c.duration, c.level, c.fade_in, c.fade_out, dict(c.params), c.label) for c in s.cues]
        track.points = [p for p in s.points if p[0] <= doc.duration_s + 1e-6]
        if track.points and track.points[-1][0] < doc.duration_s:
            track.points.append((round(doc.duration_s, 4), track.points[-1][1]))
        track.cues = [c for c in track.cues if c.t < doc.duration_s]
        written.append(s.type)
    if written:
        doc.meta["suggested"] = sorted(set(doc.meta.get("suggested", [])) | set(written))
    return written
