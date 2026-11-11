"""Effects document: effect tracks (cues and curves) on the programme timeline.

Format ``4D-VR-THEATRE-EFFECTS`` 1.0 (docs/EFFECTS_FORMAT.md). The document describes WHAT should happen
and when (wind at 60 % from 12.0 s, a 0.4 s water burst at 31.2 s ...), never HOW a venue produces it:
DMX addresses, fixture types, lead times and safety limits live in the venue patch (patch.py).

A track's level at time t (0..1) is the maximum of its curve (linear interpolation between points,
holding the first/last value outside them) and of every cue active at t (trapezoid with fades).
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import math
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

FORMAT_NAME = "4D-VR-THEATRE-EFFECTS"
FORMAT_MAJOR = 1
FORMAT_VERSION = "1.0.0"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_TRACKS = 64
MAX_ITEMS_PER_TRACK = 20000
ON_THRESHOLD = 0.01          # a level above this counts as "on"
BASS_ENCODING = "s16le-zlib-base64"
BASS_MAX_SECONDS = 4 * 3600      # longest bass track a reader unpacks
BASS_MIN_RATE_HZ, BASS_MAX_RATE_HZ = 500, 8000
RAW_DMX = "dmx"


class EffectsError(ValueError):
    """The effects document is malformed."""


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    default: float
    minimum: float
    maximum: float
    step: float = 1.0
    unit: str = ""


@dataclass(frozen=True)
class EffectType:
    name: str
    label: str
    colour: str                 # lane colour in the editor
    editing: str = "cues"       # "cues" (bursts) or "curve" (continuous level)
    default_duration_s: float = 1.0
    default_fade_s: float = 0.0
    params: tuple[ParamSpec, ...] = ()
    description: str = ""


BUILTIN_TYPES: dict[str, EffectType] = {
    t.name: t
    for t in (
        EffectType("wind", "Wind", "#58a6ff", "curve", 3.0, 0.5, (), "fans: continuous level, follows speed"),
        EffectType("water", "Water spray", "#39c5cf", "cues", 0.4, 0.0, (), "short mist / spray bursts"),
        EffectType("rain", "Rain", "#1f6feb", "cues", 4.0, 0.5, (), "overhead rain: drops for as long as it rains"),
        EffectType("air", "Air blast", "#a5d6ff", "cues", 0.25, 0.0, (), "face / neck air jets"),
        EffectType("fog", "Fog / smoke", "#8b949e", "cues", 3.0, 0.0, (), "fog, smoke or haze bursts"),
        EffectType("heat", "Heat (fire)", "#f0883e", "cues", 3.0, 0.3, (), "warm air or heat lamp for fire scenes"),
        EffectType("bubbles", "Bubbles", "#7ee787", "cues", 5.0, 0.0, (), "bubble machine: underwater and bubble scenes"),
        EffectType("snow", "Snow", "#e6edf3", "cues", 5.0, 0.0, (), "snow machine: falling snow"),
        EffectType(
            "strobe", "Strobe / lightning", "#e3b341", "cues", 1.0, 0.0,
            (ParamSpec("rate_hz", "Flash rate", 3.0, 0.5, 25.0, 0.5, "Hz"),),
            "flashes; the venue patch limits the flash rate",
        ),
        EffectType(
            "scent", "Scent", "#bc8cff", "cues", 4.0, 0.0,
            (ParamSpec("slot", "Cartridge", 1, 1, 16, 1),),
            "scent release from one cartridge (slot)",
        ),
        EffectType("vibration", "Seat vibration", "#f778ba", "curve", 1.0, 0.1, (), "bass shakers: synthesised rumble level"),
        EffectType("dmx", "DMX channel", "#3fb950", "curve", 2.0, 0.0, (), "raw DMX channel (lighting, auxiliaries)"),
    )
}


def effect_type(name: str) -> EffectType:
    """Built-in type or a generic level effect for any other (venue-defined) name, e.g. "bubbles"."""
    return BUILTIN_TYPES.get(name) or EffectType(name, name.replace("-", " ").replace("_", " ").title(), "#d29922")


def _num(value: Any, what: str, lo: float = -math.inf, hi: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EffectsError(f"{what} must be a number")
    v = float(value)
    if not math.isfinite(v) or v < lo or v > hi:
        raise EffectsError(f"{what} = {value!r} outside {lo:g}..{hi:g}")
    return v


def _params(value: Any, what: str) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EffectsError(f"{what} must be an object")
    out: dict[str, float] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not ID_RE.match(k):
            raise EffectsError(f"{what}: bad parameter name {k!r}")
        out[k] = _num(v, f"{what}.{k}", -1e6, 1e6)
    return out


def _r(v: float) -> float:
    r = round(float(v), 4)
    return int(r) if r == int(r) else r


@dataclass
class Cue:
    t: float
    duration: float
    level: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    params: dict[str, float] = field(default_factory=dict)
    label: str = ""

    @property
    def end(self) -> float:
        return self.t + self.duration

    def value_at(self, t: float) -> float:
        dt = t - self.t
        if dt < 0.0 or dt >= self.duration:
            return 0.0
        k = 1.0
        if self.fade_in > 0.0 and dt < self.fade_in:
            k = dt / self.fade_in
        remaining = self.duration - dt
        if self.fade_out > 0.0 and remaining < self.fade_out:
            k = min(k, remaining / self.fade_out)
        return self.level * k

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"t": _r(self.t), "d": _r(self.duration), "level": _r(self.level)}
        if self.fade_in:
            d["fade_in"] = _r(self.fade_in)
        if self.fade_out:
            d["fade_out"] = _r(self.fade_out)
        if self.params:
            d["params"] = {k: _r(v) for k, v in sorted(self.params.items())}
        if self.label:
            d["label"] = self.label
        return d

    @classmethod
    def from_dict(cls, d: Any, what: str = "cue") -> Cue:
        if not isinstance(d, dict):
            raise EffectsError(f"{what} must be an object")
        duration = _num(d.get("d"), f"{what}.d", 0.001, 86400.0)
        cue = cls(
            t=_num(d.get("t"), f"{what}.t", 0.0, 86400.0),
            duration=duration,
            level=_num(d.get("level", 1.0), f"{what}.level", 0.0, 1.0),
            fade_in=_num(d.get("fade_in", 0.0), f"{what}.fade_in", 0.0, duration),
            fade_out=_num(d.get("fade_out", 0.0), f"{what}.fade_out", 0.0, duration),
            params=_params(d.get("params"), f"{what}.params"),
            label=str(d.get("label", ""))[:80],
        )
        if cue.fade_in + cue.fade_out > cue.duration + 1e-9:
            raise EffectsError(f"{what}: fades longer than the cue")
        return cue


@dataclass
class Track:
    id: str
    type: str
    label: str = ""
    enabled: bool = True
    cues: list[Cue] = field(default_factory=list)
    points: list[tuple[float, float]] = field(default_factory=list)   # (t, level), sorted by t
    params: dict[str, float] = field(default_factory=dict)            # defaults for cues, e.g. {"slot": 2}
    universe: int = 0     # raw DMX tracks (type "dmx") only: DMX universe (1-based) and channel 1..512
    channel: int = 0

    # ------------------------------------------------------------------ evaluation (editor / reference)
    def curve_at(self, t: float) -> float:
        pts = self.points
        if not pts:
            return 0.0
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        i = bisect.bisect_right(pts, (t, math.inf))   # first point later than t
        (t0, v0), (t1, v1) = pts[i - 1], pts[i]
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    def value_at(self, t: float) -> float:
        v = self.curve_at(t)
        for cue in self.cues:
            c = cue.value_at(t)
            if c > v:
                v = c
        return min(1.0, max(0.0, v))

    def params_at(self, t: float) -> dict[str, float]:
        """Parameters in force at t: those of the strongest active cue over the track defaults."""
        best: Cue | None = None
        best_v = 0.0
        for cue in self.cues:
            v = cue.value_at(t)
            if v > best_v or (best is not None and v == best_v and v > 0 and cue.t >= best.t):
                best, best_v = cue, v
        if best is None or not best.params:
            return dict(self.params)
        return {**self.params, **best.params}

    def sort(self) -> None:
        self.cues.sort(key=lambda c: (c.t, c.duration))
        self.points.sort(key=lambda p: p[0])

    def is_empty(self) -> bool:
        return not self.cues and all(v <= 0.0 for _t, v in self.points)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.label:
            d["label"] = self.label
        if not self.enabled:
            d["enabled"] = False
        if self.params:
            d["params"] = {k: _r(v) for k, v in sorted(self.params.items())}
        if self.type == RAW_DMX:
            d["universe"], d["channel"] = self.universe, self.channel
        if self.points:
            d["points"] = [[_r(t), _r(v)] for t, v in self.points]
        if self.cues:
            d["cues"] = [c.to_dict() for c in self.cues]
        return d

    @classmethod
    def from_dict(cls, d: Any, index: int = 0) -> Track:
        what = f"tracks[{index}]"
        if not isinstance(d, dict):
            raise EffectsError(f"{what} must be an object")
        tid, typ = d.get("id"), d.get("type")
        if not isinstance(tid, str) or not ID_RE.match(tid):
            raise EffectsError(f"{what}.id must match {ID_RE.pattern}")
        if not isinstance(typ, str) or not ID_RE.match(typ):
            raise EffectsError(f"{what}.type must match {ID_RE.pattern}")
        cues_in, points_in = d.get("cues", []), d.get("points", [])
        if not isinstance(cues_in, list) or not isinstance(points_in, list):
            raise EffectsError(f"{what}: cues and points must be arrays")
        if len(cues_in) > MAX_ITEMS_PER_TRACK or len(points_in) > MAX_ITEMS_PER_TRACK:
            raise EffectsError(f"{what}: more than {MAX_ITEMS_PER_TRACK} cues or points")
        points: list[tuple[float, float]] = []
        for k, p in enumerate(points_in):
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise EffectsError(f"{what}.points[{k}] must be [t, level]")
            points.append((_num(p[0], f"{what}.points[{k}].t", 0.0, 86400.0), _num(p[1], f"{what}.points[{k}].level", 0.0, 1.0)))
        if any(points[k][0] < points[k - 1][0] for k in range(1, len(points))):
            raise EffectsError(f"{what}.points must be sorted by time")
        track = cls(
            id=tid,
            type=typ,
            label=str(d.get("label", ""))[:80],
            enabled=bool(d.get("enabled", True)),
            cues=[Cue.from_dict(c, f"{what}.cues[{k}]") for k, c in enumerate(cues_in)],
            points=points,
            params=_params(d.get("params"), f"{what}.params"),
        )
        if typ == RAW_DMX:
            track.universe = int(_num(d.get("universe"), f"{what}.universe", 1, 63999))
            track.channel = int(_num(d.get("channel"), f"{what}.channel", 1, 512))
        track.cues.sort(key=lambda c: (c.t, c.duration))
        return track


@dataclass
class BassTrack:
    """Low-frequency audio for subwoofers / bass shakers, stored mono at a low sample rate."""

    sample_rate_hz: int
    samples: np.ndarray                 # int16, mono
    gain_db: float = 0.0
    cutoff_hz: float = 120.0
    source: str = ""

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate_hz)

    def as_float(self) -> np.ndarray:
        return self.samples.astype(np.float32) / 32767.0

    @classmethod
    def from_float(cls, x: np.ndarray, sample_rate_hz: int, **kw: Any) -> BassTrack:
        arr = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
        return cls(int(sample_rate_hz), np.round(arr * 32767.0).astype("<i2"), **kw)

    def to_dict(self) -> dict[str, Any]:
        raw = np.ascontiguousarray(self.samples, dtype="<i2").tobytes()
        return {
            "encoding": BASS_ENCODING,
            "sample_rate_hz": int(self.sample_rate_hz),
            "sample_count": len(self.samples),
            "gain_db": _r(self.gain_db),
            "cutoff_hz": _r(self.cutoff_hz),
            "source": self.source,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data": base64.b64encode(zlib.compress(raw, 9)).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, d: Any) -> BassTrack:
        if not isinstance(d, dict):
            raise EffectsError("bass must be an object")
        if d.get("encoding") != BASS_ENCODING:
            raise EffectsError(f"bass.encoding must be {BASS_ENCODING!r}")
        rate = int(_num(d.get("sample_rate_hz"), "bass.sample_rate_hz", BASS_MIN_RATE_HZ, BASS_MAX_RATE_HZ))
        data = d.get("data")
        if not isinstance(data, str):
            raise EffectsError("bass.data must be a base64 string")
        count = int(_num(d.get("sample_count"), "bass.sample_count", 1, rate * BASS_MAX_SECONDS))
        try:
            unpack = zlib.decompressobj()
            raw = unpack.decompress(base64.b64decode(data, validate=True), 2 * count + 1)   # never more than declared
        except (ValueError, zlib.error) as exc:
            raise EffectsError(f"bass.data is corrupt: {exc}") from exc
        if len(raw) != 2 * count or not unpack.eof:
            raise EffectsError(f"bass.data does not hold the {count} samples the header declares")
        if d.get("sha256") and hashlib.sha256(raw).hexdigest() != d["sha256"]:
            raise EffectsError("bass.data checksum mismatch")
        return cls(
            rate,
            np.frombuffer(raw, dtype="<i2").copy(),
            gain_db=_num(d.get("gain_db", 0.0), "bass.gain_db", -60.0, 24.0),
            cutoff_hz=_num(d.get("cutoff_hz", 120.0), "bass.cutoff_hz", 10.0, 1000.0),
            source=str(d.get("source", ""))[:200],
        )


@dataclass
class EffectsDocument:
    duration_s: float
    tracks: list[Track] = field(default_factory=list)
    bass: BassTrack | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ content
    def is_empty(self) -> bool:
        return self.bass is None and all(t.is_empty() for t in self.tracks)

    def track(self, track_id: str) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def types(self) -> list[str]:
        return sorted({t.type for t in self.tracks})

    def new_track(self, type_name: str, label: str = "", **kw: Any) -> Track:
        if not ID_RE.match(type_name):
            raise EffectsError(f"bad effect type name {type_name!r}")
        if len(self.tracks) >= MAX_TRACKS:
            raise EffectsError(f"at most {MAX_TRACKS} tracks")
        used = {t.id for t in self.tracks}
        tid, n = type_name, 1
        while tid in used:
            n += 1
            tid = f"{type_name}-{n}"[:32]
        et = effect_type(type_name)
        track = Track(id=tid, type=type_name, label=label or (et.label if n == 1 else f"{et.label} {n}"), **kw)
        if et.editing == "curve" and not track.points and not track.cues:
            track.points = [(0.0, 0.0), (round(self.duration_s, 4), 0.0)]
        if type_name == RAW_DMX and not track.universe:
            track.universe, track.channel = 1, 1
        self.tracks.append(track)
        return track

    def problems(self) -> list[str]:
        """Content checks that do not make the document unreadable (shown as warnings)."""
        out: list[str] = []
        seen: set[tuple[int, int]] = set()
        for tr in self.tracks:
            for c in tr.cues:
                if c.end > self.duration_s + 0.05:
                    out.append(f"track {tr.id}: cue at {c.t:.2f} s ends after the programme ({self.duration_s:.2f} s)")
                    break
            if tr.points and tr.points[-1][0] > self.duration_s + 0.05:
                out.append(f"track {tr.id}: curve points after the end of the programme")
            if tr.type == RAW_DMX:
                key = (tr.universe, tr.channel)
                if key in seen:
                    out.append(f"track {tr.id}: DMX {tr.universe}/{tr.channel} is used by more than one track (highest wins)")
                seen.add(key)
        if self.bass is not None and abs(self.bass.duration_s - self.duration_s) > 1.0:
            out.append(f"bass track is {self.bass.duration_s:.1f} s, programme {self.duration_s:.1f} s")
        return out

    # ------------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "duration_s": _r(self.duration_s),
            "meta": dict(self.meta),
            "tracks": [t.to_dict() for t in self.tracks],
        }
        if self.bass is not None:
            d["bass"] = self.bass.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Any) -> EffectsDocument:
        if not isinstance(d, dict):
            raise EffectsError("effects document must be a JSON object")
        if d.get("format") != FORMAT_NAME:
            raise EffectsError(f"not a {FORMAT_NAME} document")
        version = str(d.get("version", ""))
        if not version.split(".")[0].isdigit() or int(version.split(".")[0]) != FORMAT_MAJOR:
            raise EffectsError(f"unsupported effects format version {version!r} (supported: {FORMAT_MAJOR}.x)")
        tracks_in = d.get("tracks", [])
        if not isinstance(tracks_in, list) or len(tracks_in) > MAX_TRACKS:
            raise EffectsError(f"tracks must be an array of at most {MAX_TRACKS} tracks")
        tracks = [Track.from_dict(t, i) for i, t in enumerate(tracks_in)]
        ids = [t.id for t in tracks]
        if len(set(ids)) != len(ids):
            raise EffectsError("track ids must be unique")
        meta = d.get("meta", {})
        return cls(
            duration_s=_num(d.get("duration_s"), "duration_s", 0.01, 86400.0),
            tracks=tracks,
            bass=BassTrack.from_dict(d["bass"]) if d.get("bass") is not None else None,
            meta=dict(meta) if isinstance(meta, dict) else {},
        )

    def copy(self) -> EffectsDocument:
        """Deep copy that shares the (immutable in practice) bass sample array."""
        doc = EffectsDocument(self.duration_s, [], self.bass, dict(self.meta))
        for t in self.tracks:
            doc.tracks.append(
                Track(t.id, t.type, t.label, t.enabled,
                      [Cue(c.t, c.duration, c.level, c.fade_in, c.fade_out, dict(c.params), c.label) for c in t.cues],
                      list(t.points), dict(t.params), t.universe, t.channel)
            )
        return doc

    def compile(self) -> CompiledDocument:
        return CompiledDocument(self)


class CompiledTrack:
    """Read-only, fast evaluation of one track (numpy curve, bisected cues)."""

    def __init__(self, track: Track) -> None:
        self.id, self.type, self.params = track.id, track.type, dict(track.params)
        self.universe, self.channel = track.universe, track.channel
        pts = sorted(track.points, key=lambda p: p[0])
        self._pt = np.array([p[0] for p in pts], dtype=np.float64)
        self._pv = np.array([p[1] for p in pts], dtype=np.float64)
        self._cues = sorted(track.cues, key=lambda c: (c.t, c.duration))
        self._starts = [c.t for c in self._cues]
        self._longest = max((c.duration for c in self._cues), default=0.0)

    def _active(self, t: float) -> list[Cue]:
        if not self._cues:
            return []
        hi = bisect.bisect_right(self._starts, t)
        lo = bisect.bisect_left(self._starts, t - self._longest)
        return [c for c in self._cues[lo:hi] if t < c.end]

    def curve_at(self, t: float) -> float:
        pt, pv = self._pt, self._pv
        if not len(pt):
            return 0.0
        if t <= pt[0]:
            return float(pv[0])
        if t >= pt[-1]:
            return float(pv[-1])
        i = int(np.searchsorted(pt, t, side="right"))  # same rule as Track.curve_at
        t0, t1, v0, v1 = float(pt[i - 1]), float(pt[i]), float(pv[i - 1]), float(pv[i])
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    def value_at(self, t: float, match: dict[str, float] | None = None) -> float:
        """Level at t. With `match`, only cues (or the track defaults) carrying those parameter values count."""
        v = self.curve_at(t) if _matches(self.params, match) else 0.0
        for cue in self._active(t):
            if match and not _matches({**self.params, **cue.params}, match):
                continue
            c = cue.value_at(t)
            if c > v:
                v = c
        return min(1.0, max(0.0, v))

    def _strongest(self, t: float) -> Cue | None:
        best: Cue | None = None
        best_v = 0.0
        for cue in self._active(t):
            v = cue.value_at(t)
            if v > best_v or (best is not None and v == best_v and v > 0 and cue.t >= best.t):
                best, best_v = cue, v
        return best

    def params_at(self, t: float) -> dict[str, float]:
        best = self._strongest(t)
        if best is None or not best.params:
            return dict(self.params)
        return {**self.params, **best.params}

    def onset_at(self, t: float) -> float | None:
        """Start time of the strongest cue active at t; None while only the curve (or nothing) is active."""
        best = self._strongest(t)
        return None if best is None else best.t


def _matches(params: dict[str, float], match: dict[str, float] | None) -> bool:
    if not match:
        return True
    return all(abs(params.get(k, math.nan) - v) < 1e-6 for k, v in match.items())


class CompiledDocument:
    def __init__(self, doc: EffectsDocument) -> None:
        self.duration_s = doc.duration_s
        self.bass = doc.bass
        self.by_type: dict[str, list[CompiledTrack]] = {}
        for t in doc.tracks:
            if t.enabled:
                self.by_type.setdefault(t.type, []).append(CompiledTrack(t))

    def level(self, type_name: str, t: float, match: dict[str, float] | None = None) -> float:
        return max((tr.value_at(t, match) for tr in self.by_type.get(type_name, ())), default=0.0)

    def params(self, type_name: str, t: float) -> dict[str, float]:
        """Parameters of the strongest track of this type at t."""
        best: dict[str, float] = {}
        best_v = -1.0
        for tr in self.by_type.get(type_name, ()):
            v = tr.value_at(t)
            if v > best_v:
                best, best_v = tr.params_at(t), v
        return best

    def onset(self, type_name: str, t: float) -> float | None:
        """Start of the cue that sets the level of this type at t (strobe: its flashes count from here)."""
        best: float | None = None
        best_v = 0.0
        for tr in self.by_type.get(type_name, ()):
            v = tr.value_at(t)
            if v > best_v:
                best, best_v = tr.onset_at(t), v
        return best

    def raw_dmx(self) -> list[CompiledTrack]:
        return self.by_type.get(RAW_DMX, [])
