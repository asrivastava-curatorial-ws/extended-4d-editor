"""Fixture library: the profiles of real DMX devices, taken from the Open Fixture Library.

The Open Fixture Library (https://open-fixture-library.org, MIT licence) describes DMX devices: the
modes of each, the channels of a mode, and what the value ranges of every channel do. A snapshot of
all of it is inside this package (data/ofl-fixtures.json.xz; data/OFL-LICENSE.txt). Fixture files of
your own - the same JSON format; the library's online "Fixture Editor" writes it - go into
:func:`fixtures_dir`, or into any folder named in the environment variable FOURDVR_FIXTURES.

The show does not read the library. :func:`plan` turns one mode of a fixture into the channel list of a
venue patch entry - which channel the effect drives, over which DMX values, and which channels keep a
fixed value - and that list is written into the patch. A patch stays complete in itself, and what a
device does can be read, and corrected, in the patch.

Not read: pixel matrices (a mode that repeats channels for every pixel) and modes of more than 32
channels - neither is an effect device.
"""

from __future__ import annotations

import json
import lzma
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .patch import STROBE_DEFAULT_MAX_HZ, STROBE_HARD_MAX_HZ

FIXTURES_ENV = "FOURDVR_FIXTURES"
SNAPSHOT = "ofl-fixtures.json.xz"
MAX_CHANNELS = 32
RATE_MIN_HZ, RATE_MAX_HZ = 0.1, 50.0      # the flash rates a patch's rate channel can express
NAME_MAX = 40

# effect types whose device is not a light: colour channels of such a fixture stay dark
_NOT_A_LIGHT = ("fog", "wind", "water", "rain", "air", "bubbles", "snow", "scent")
# the channel an effect drives: capability types in the order they are looked for
_KINDS: dict[str, tuple[str, ...]] = {
    "fog": ("Fog", "FogOutput"),
    "speed": ("Speed", "Rotation"),
    "intensity": ("Intensity",),
    "generic": ("Generic",),
}
_ORDER: dict[str, tuple[str, ...]] = {
    "fog": ("fog", "intensity", "generic", "speed"),
    "wind": ("speed", "intensity", "generic", "fog"),
}
_DEFAULT_ORDER = ("intensity", "generic", "fog", "speed")
_PROPERTY = {"Fog": "fogOutput", "FogOutput": "fogOutput", "Speed": "speed", "Rotation": "speed", "Intensity": "brightness"}
_WORDS = {
    "off": 0.0, "stop": 0.0, "closed": 0.0,
    "dark": 0.01, "weak": 0.01, "slow": 0.01, "low": 0.01, "slow cw": 0.01, "slow ccw": 0.01, "slow reverse": 0.01,
    "bright": 1.0, "strong": 1.0, "fast": 1.0, "high": 1.0, "fast cw": 1.0, "fast ccw": 1.0, "fast reverse": 1.0,
}
_NUMBER = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([A-Za-z%^0-9/]*)$")


class FixtureError(ValueError):
    pass


def fixtures_dir() -> Path:
    """Where fixture files of the user are looked for (it need not exist)."""
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "extended-4d-editor" / "fixtures"


@dataclass
class Capability:
    lo: int                     # DMX values of the channel itself (8 bit), both ends included
    hi: int
    type: str
    props: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        extra = self.props.get("shutterEffect") or self.props.get("comment") or self.props.get("color") or ""
        for name in ("brightness", "fogOutput", "speed"):
            if f"{name}Start" in self.props:
                extra = f"{self.props[f'{name}Start']} … {self.props[f'{name}End']}"
            elif name in self.props and not extra:
                extra = str(self.props[name])
        return f"{self.lo}–{self.hi} {self.type}" + (f" ({extra})" if extra else "")


@dataclass
class Channel:
    key: str
    name: str
    capabilities: list[Capability] = field(default_factory=list)
    default: int = 0
    fine_of: str = ""           # this is the fine (16 bit, 24 bit) half of that channel

    def types(self) -> set[str]:
        return {c.type for c in self.capabilities}

    def of(self, types: Iterable[str]) -> list[Capability]:
        wanted = set(types)
        return [c for c in self.capabilities if c.type in wanted]

    def describe(self) -> str:
        if self.fine_of:
            return f"fine part of {self.fine_of}"
        text = ", ".join(c.describe() for c in self.capabilities[:4])
        return text + (f", … ({len(self.capabilities)} ranges)" if len(self.capabilities) > 4 else "")


@dataclass
class Mode:
    name: str
    short: str
    channels: list[Channel | None] = field(default_factory=list)     # None: a slot the mode leaves unused
    unsupported: str = ""       # why this mode cannot be patched ("" = it can)

    @property
    def footprint(self) -> int:
        return len(self.channels)


@dataclass
class Model:
    key: str                    # "manufacturer/fixture"
    name: str
    manufacturer: str
    categories: list[str] = field(default_factory=list)
    modes: list[Mode] = field(default_factory=list)
    comment: str = ""
    source: str = "Open Fixture Library"

    @property
    def title(self) -> str:
        return f"{self.manufacturer} {self.name}".strip()

    def mode(self, name: str = "") -> Mode:
        """The mode with this name or short name (without a name: the first that can be patched)."""
        if not name:
            for m in self.modes:
                if not m.unsupported:
                    return m
            raise FixtureError(f"{self.key}: none of its modes can be patched ({self.modes[0].unsupported if self.modes else 'no modes'})")
        wanted = name.strip().lower()
        for m in self.modes:
            if wanted in (m.short.lower(), m.name.lower()):
                return m
        raise FixtureError(f"{self.key} has no mode '{name}' (it has: {', '.join(m.short for m in self.modes)})")


# ---------------------------------------------------------------------------------------------- reading a fixture
def _coarse(value: Any, shift: int, what: str) -> int:
    """A DMX value of the fixture file as a value of the channel itself: files give values of 16 and 24 bit
    channels in the full resolution; a percentage is of the whole range."""
    if isinstance(value, str) and value.endswith("%"):
        try:
            return max(0, min(255, int(float(value[:-1]) / 100.0 * 255.0 + 0.5)))
        except ValueError as exc:
            raise FixtureError(f"{what}: '{value}' is not a value") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureError(f"{what}: '{value}' is not a value")
    return max(0, min(255, int(value) >> shift))


def _channel(key: str, raw: dict[str, Any], name: str = "") -> Channel:
    fine = raw.get("fineChannelAliases") or []
    stated = {"8bit": 1, "16bit": 2, "24bit": 3}.get(str(raw.get("dmxValueResolution", "")), 1 + len(fine))
    shift = 8 * (stated - 1)
    caps: list[Capability] = []
    if isinstance(raw.get("capability"), dict):
        c = raw["capability"]
        caps.append(Capability(0, 255, str(c.get("type", "Generic")), {k: v for k, v in c.items() if k != "type"}))
    for c in raw.get("capabilities") or []:
        if not isinstance(c, dict):
            continue
        rng = c.get("dmxRange")
        if not (isinstance(rng, list) and len(rng) == 2):
            raise FixtureError(f"channel {key}: a capability without a value range")
        lo, hi = _coarse(rng[0], shift, f"channel {key}"), _coarse(rng[1], shift, f"channel {key}")
        caps.append(Capability(min(lo, hi), max(lo, hi), str(c.get("type", "Generic")),
                               {k: v for k, v in c.items() if k not in ("type", "dmxRange")}))
    default = _coarse(raw["defaultValue"], shift, f"channel {key}") if "defaultValue" in raw else 0
    return Channel(key, (name or str(raw.get("name") or key))[:NAME_MAX], caps, default)


def parse_fixture(data: dict[str, Any], key: str, manufacturer: str = "", source: str = "Open Fixture Library") -> Model:
    """A fixture file of the Open Fixture Library format as a Model."""
    if not isinstance(data, dict) or not isinstance(data.get("modes"), list) or not data.get("name"):
        raise FixtureError(f"{key}: not a fixture file (no name, or no modes)")
    available: dict[str, Any] = data.get("availableChannels") or {}
    templates: dict[str, Any] = data.get("templateChannels") or {}
    patterns = [(re.compile("^" + re.escape(k).replace(re.escape("$pixelKey"), "(.+)") + "$"), k, v) for k, v in templates.items()]
    fine_of: dict[str, str] = {}
    switching: dict[str, tuple[str, list[tuple[Any, Any]]]] = {}       # alias -> (channel that decides, [(range, key)])
    for ckey, raw in {**templates, **available}.items():
        if not isinstance(raw, dict):
            continue
        for alias in raw.get("fineChannelAliases") or []:
            fine_of[str(alias)] = ckey
        for cap in ([raw["capability"]] if isinstance(raw.get("capability"), dict) else raw.get("capabilities") or []):
            for alias, target in (cap.get("switchChannels") or {}).items():
                switching.setdefault(str(alias), (ckey, []))[1].append((cap.get("dmxRange"), target))
    cache: dict[str, Channel | None] = {}

    def resolve(ckey: str, depth: int = 0) -> Channel | None:
        if ckey in cache:
            return cache[ckey]
        found: Channel | None
        if ckey in available:
            found = _channel(ckey, available[ckey])
        elif ckey in fine_of:
            found = Channel(ckey, ckey[:NAME_MAX], fine_of=fine_of[ckey])
        elif ckey in switching and depth < 4:
            decider, cases = switching[ckey]
            base = resolve(decider, depth + 1)
            at = base.default if base is not None else 0
            raw = available.get(decider) or templates.get(decider) or {}
            shift = 8 * ({"8bit": 1, "16bit": 2, "24bit": 3}.get(str(raw.get("dmxValueResolution", "")),
                                                                 1 + len(raw.get("fineChannelAliases") or [])) - 1)
            target: Any = cases[0][1]
            for rng, key_then in cases:
                if isinstance(rng, list) and len(rng) == 2 and (int(rng[0]) >> shift) <= at <= (int(rng[1]) >> shift):
                    target = key_then
                    break
            inner = resolve(str(target), depth + 1) if target else None
            found = None if inner is None else Channel(ckey, inner.name, inner.capabilities, inner.default, inner.fine_of)
        else:
            found = None
            for pattern, tkey, raw in patterns:
                m = pattern.match(ckey)
                if m and isinstance(raw, dict):
                    found = _channel(ckey, raw, str(raw.get("name") or tkey).replace("$pixelKey", m.group(1)))
                    break
            else:
                raise FixtureError(f"channel '{ckey}' is not described")
        cache[ckey] = found
        return found

    modes: list[Mode] = []
    for m in data["modes"]:
        if not isinstance(m, dict) or not isinstance(m.get("channels"), list):
            continue
        mode = Mode(str(m.get("name") or m.get("shortName") or "mode"), str(m.get("shortName") or m.get("name") or "mode"))
        try:
            for entry in m["channels"]:
                if entry is None:
                    mode.channels.append(None)
                elif isinstance(entry, str):
                    mode.channels.append(resolve(entry))
                else:
                    raise FixtureError("a pixel matrix")
            if len(mode.channels) > MAX_CHANNELS:
                raise FixtureError(f"{len(mode.channels)} channels (at most {MAX_CHANNELS})")
            if not any(c is not None and not c.fine_of for c in mode.channels):
                raise FixtureError("no channel")
        except FixtureError as exc:
            mode.unsupported = str(exc)
        modes.append(mode)
    if not modes:
        raise FixtureError(f"{key}: no modes")
    return Model(key, str(data["name"]), manufacturer or key.split("/")[0], [str(c) for c in data.get("categories") or []],
                 modes, str(data.get("comment") or ""), source)


# ---------------------------------------------------------------------------------------------- the library
@dataclass
class Entry:
    key: str
    name: str
    manufacturer: str
    categories: list[str]
    modes: list[str]
    source: str

    @property
    def title(self) -> str:
        return f"{self.manufacturer} {self.name}".strip()


class Library:
    """The snapshot inside the package, and the user's own fixture files on top of it."""

    def __init__(self, folders: Iterable[str | Path] | None = None, snapshot: bool = True) -> None:
        self._raw: dict[str, tuple[dict[str, Any], str, str]] = {}      # key -> (fixture file, manufacturer, source)
        self.snapshot: dict[str, Any] = {}
        self.problems: list[str] = []
        if snapshot:
            self._load_snapshot()
        if folders is None:
            named = [p for p in os.environ.get(FIXTURES_ENV, "").split(os.pathsep) if p]
            folders = [fixtures_dir(), *named]
        self.folders = [Path(f).expanduser() for f in folders]
        for folder in self.folders:
            self._load_folder(folder)

    def _load_snapshot(self) -> None:
        try:
            blob = resources.files("fourdvr_fx").joinpath("data", SNAPSHOT).read_bytes()
            data = json.loads(lzma.decompress(blob))
        except (OSError, lzma.LZMAError, ValueError) as exc:
            self.problems.append(f"the library inside the package cannot be read: {exc}")
            return
        self.snapshot = {k: data.get(k) for k in ("source", "commit", "date", "licence")}
        names = data.get("manufacturers", {})
        for key, fixture in data.get("fixtures", {}).items():
            self._raw[key] = (fixture, str(names.get(key.split("/")[0], key.split("/")[0])), "Open Fixture Library")

    def _load_folder(self, folder: Path) -> None:
        if not folder.is_dir():
            return
        names: dict[str, str] = {}
        try:
            listed = json.loads((folder / "manufacturers.json").read_text(encoding="utf-8"))
            names = {k: str(v.get("name", k)) for k, v in listed.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        for path in sorted(folder.rglob("*.json")):
            if path.name == "manufacturers.json" or len(path.relative_to(folder).parts) > 2:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.problems.append(f"{path}: {exc}")
                continue
            if not isinstance(data, dict) or "modes" not in data:
                continue                                   # a redirect, or not a fixture at all
            group = path.parent.name if path.parent != folder else "user"
            self._raw[f"{group}/{path.stem}"] = (data, names.get(group, group), str(path))

    def __len__(self) -> int:
        return len(self._raw)

    def keys(self) -> list[str]:
        return sorted(self._raw)

    def get(self, key: str) -> Model:
        wanted = key.strip()
        if wanted not in self._raw:                       # any case will do
            lower = wanted.lower()
            same = [k for k in self._raw if k.lower() == lower]
            if not same:
                near = [k for k in self._raw if k.lower().endswith("/" + lower) or lower in k.lower()][:6]
                raise FixtureError(f"no fixture '{key}' in the library" + (f" - did you mean {', '.join(near)}?" if near else
                                                                           " (fourdvr fx fixtures search …)"))
            wanted = same[0]
        data, manufacturer, source = self._raw[wanted]
        return parse_fixture(data, wanted, manufacturer, source)

    def search(self, text: str = "", category: str = "") -> list[Entry]:
        """Fixtures whose manufacturer, name or key contain every word of `text`, of this category (any case)."""
        words = text.lower().split()
        cat = category.strip().lower()
        out: list[Entry] = []
        for key in sorted(self._raw):
            data, manufacturer, source = self._raw[key]
            cats = [str(c) for c in data.get("categories") or []]
            if cat and cat not in (c.lower() for c in cats):
                continue
            hay = f"{key} {manufacturer} {data.get('name', '')}".lower()
            if all(w in hay for w in words):
                modes = [str(m.get("shortName") or m.get("name") or "") for m in data.get("modes") or [] if isinstance(m, dict)]
                out.append(Entry(key, str(data.get("name", key)), manufacturer, cats, modes, source))
        return out

    def categories(self) -> dict[str, int]:
        count: dict[str, int] = {}
        for data, _m, _s in self._raw.values():
            for c in data.get("categories") or []:
                count[str(c)] = count.get(str(c), 0) + 1
        return dict(sorted(count.items(), key=lambda kv: (-kv[1], kv[0])))

    def info(self) -> dict[str, Any]:
        own = sum(1 for _d, _m, source in self._raw.values() if source != "Open Fixture Library")
        return {"fixtures": len(self._raw), "from_the_library": len(self._raw) - own, "own": own, "snapshot": self.snapshot,
                "folders": [str(f) for f in self.folders], "problems": list(self.problems)}


_shared: Library | None = None


def library() -> Library:
    """The library, read once."""
    global _shared
    if _shared is None:
        _shared = Library()
    return _shared


def forget_library() -> None:
    """Read the folders again at the next use (after a file was added)."""
    global _shared
    _shared = None


# ---------------------------------------------------------------------------------------------- a patch entry
def _amount(value: Any) -> float | None:
    """How much a capability's start or end value is: 0 for off, larger for more. None: not known."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return abs(float(value))
    text = str(value).strip().lower()
    if text in _WORDS:
        return _WORDS[text]
    m = _NUMBER.match(text)
    if not m:
        return None
    number = abs(float(m.group(1)))
    return number / 100.0 if m.group(2) == "%" else number


def _ends(cap: Capability) -> tuple[float | None, float | None]:
    name = _PROPERTY.get(cap.type, "")
    if name and f"{name}Start" in cap.props:
        return _amount(cap.props[f"{name}Start"]), _amount(cap.props.get(f"{name}End"))
    if name and name in cap.props:
        a = _amount(cap.props[name])
        return a, a
    return None, None


def level_range(channel: Channel, types: Iterable[str]) -> tuple[int, int, int] | None:
    """(value while off, value at the smallest level, value at full level) of the ranges of these types -
    or None if the channel has none that does something."""
    caps = channel.of(types)
    if not caps:
        return None
    idle = [c for c in caps if _ends(c) == (0.0, 0.0)]
    active = sorted((c for c in caps if c not in idle), key=lambda c: c.lo)
    if not active:
        return None
    nothing = idle or channel.of(("NoFunction",))
    off = min(c.lo for c in nothing) if nothing else 0
    if len(active) > 1:
        amounts = [_ends(c) for c in active]
        known = all(a is not None and b is not None for a, b in amounts)
        rising = known and all(amounts[i][1] <= amounts[i + 1][0] for i in range(len(active) - 1))      # type: ignore[operator]
        falling = known and all(amounts[i][1] >= amounts[i + 1][0] for i in range(len(active) - 1))     # type: ignore[operator]
        if rising and amounts[0][0] != amounts[-1][1]:
            return off, active[0].lo, active[-1].hi
        if falling and amounts[0][0] != amounts[-1][1]:
            return off, active[-1].hi, active[0].lo
        active = [max(active, key=lambda c: c.hi - c.lo)]
    cap = active[0]
    a, b = _ends(cap)
    if a is not None and b is not None and a > b:
        return off, cap.hi, cap.lo
    return off, cap.lo, cap.hi


def _hz(value: Any) -> float | None:
    m = _NUMBER.match(str(value).strip())
    if not m:
        return None
    unit = m.group(2).lower()
    if unit == "hz":
        return float(m.group(1))
    if unit == "bpm":
        return float(m.group(1)) / 60.0
    return None


def strobe_range(channel: Channel) -> tuple[int, int, float, float, bool] | None:
    """(value at the slowest flashes, value at the fastest, slowest rate, fastest rate, rates known) of
    the channel's widest strobe range - None if it has none. Rates are cut to what a patch can express
    (0.1 to 50 flashes a second), and the values with them."""
    caps = [c for c in channel.capabilities
            if (c.type == "ShutterStrobe" and c.props.get("shutterEffect") == "Strobe" and not c.props.get("randomTiming"))
            or c.type == "StrobeSpeed"]
    if not caps:
        return None
    rated = [c for c in caps if _hz(c.props.get("speedStart")) is not None and _hz(c.props.get("speedEnd")) is not None]
    cap = max(rated or caps, key=lambda c: c.hi - c.lo)
    if cap in rated:
        a, b = float(_hz(cap.props["speedStart"]) or 0.0), float(_hz(cap.props["speedEnd"]) or 0.0)
        if a != b:
            slow, fast, hz_lo, hz_hi = (cap.lo, cap.hi, a, b) if a < b else (cap.hi, cap.lo, b, a)
            lo, hi = max(hz_lo, RATE_MIN_HZ), min(hz_hi, RATE_MAX_HZ)
            if hi > lo:
                def at(hz: float) -> int:
                    return round(slow + (fast - slow) * (hz - hz_lo) / (hz_hi - hz_lo))
                return at(lo), at(hi), round(lo, 3), round(hi, 3), True
    start, end = _amount(cap.props.get("speedStart", "")), _amount(cap.props.get("speedEnd", ""))
    if start is not None and end is not None and start > end:
        return cap.hi, cap.lo, 1.0, 20.0, False
    return cap.lo, cap.hi, 1.0, 20.0, False


def _shutter(channel: Channel, effect: str) -> int | None:
    """The middle of the channel's range with this shutter effect ("Open", "Closed")."""
    for c in channel.capabilities:
        if c.type == "ShutterStrobe" and c.props.get("shutterEffect") == effect:
            return (c.lo + c.hi) // 2
    return None


@dataclass
class Plan:
    """One mode of a fixture as a venue patch entry."""

    model: Model
    mode: Mode
    effect: str
    channels: list[dict[str, Any]] = field(default_factory=list)
    flash: str = ""                 # strobe: "fixture" or "software"
    outputs: int = 1                # a pack of independent outputs: how many it has
    output: int = 1                 # ... and which of them this entry is
    driven: str = ""                # name of the channel the effect drives
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        text = f"{self.model.title} ({self.mode.short})"
        return (text if self.outputs == 1 else f"{text} output {self.output}")[:80]

    def entry(self, fixture_id: str, universe: int = 1, address: int = 1, scope: str = "theatre") -> dict[str, Any]:
        """The table of a [[fixtures]] entry."""
        out: dict[str, Any] = {"id": fixture_id, "effect": self.effect, "label": self.label, "model": self.model.key,
                               "mode": self.mode.short}
        if scope != "theatre":
            out["scope"] = scope
        out |= {"universe": universe, "address": address}
        if self.flash:
            out["flash"] = self.flash
        out["channels"] = [dict(c) for c in self.channels]
        return out


def plan(model: Model, mode: Mode, effect: str, output: int = 1, max_rate_hz: float = STROBE_DEFAULT_MAX_HZ) -> Plan:
    """Decide what every channel of `mode` does while this fixture plays `effect`.

    The effect drives one channel (its "level"): the fog output of a fog machine, the speed of a fan, the
    dimmer of anything else. A strobe that can flash as slowly as the venue allows flashes itself (its
    rate channel follows the cue); one that cannot is switched by us. The other channels keep a value:
    colours full while a light is on, a shutter open, the rest at the value the fixture file names.
    """
    if mode.unsupported:
        raise FixtureError(f"{model.key}, mode {mode.short}: {mode.unsupported}")
    slots = [(k, c) for k, c in enumerate(mode.channels) if c is not None]
    real = [(k, c) for k, c in slots if not c.fine_of]
    result = Plan(model, mode, effect)
    used: dict[int, dict[str, Any]] = {}

    def put(offset: int, channel: Channel, **values: Any) -> None:
        used[offset] = {"offset": offset, **values, "name": channel.name}

    # a pack of independent outputs (dimmer pack, relay pack): one entry is one of its outputs
    for kind in ("intensity", "generic"):
        same = [(k, c) for k, c in real if level_range(c, _KINDS[kind]) is not None and len(c.types() - {"NoFunction"}) == 1]
        if len(real) >= 2 and len(same) == len(real):
            result.outputs = len(same)
            if not 1 <= output <= len(same):
                raise FixtureError(f"{model.key}, mode {mode.short} has outputs 1..{len(same)}, not {output}")
            k, channel = same[output - 1]
            off, lo, hi = level_range(channel, _KINDS[kind]) or (0, 0, 255)
            result.output, result.driven = output, channel.name
            result.channels = [_tidy({"role": "level", "offset": k, "min": lo, "max": hi, "off": off, "name": channel.name})]
            if effect == "strobe":
                result.flash = "software"       # a lamp on a dimmer does not flash by itself
            result.notes.append(f"a device with {len(same)} independent outputs: this entry is output {output} "
                                f"(DMX address + {k}); the others stay free for other fixtures")
            return result
    if output != 1:
        raise FixtureError(f"{model.key}, mode {mode.short} is one device, not a pack of outputs")

    light = effect not in _NOT_A_LIGHT
    colours = [k for k, c in real if "ColorIntensity" in c.types()] if light else []
    levels: list[int] = []          # the channels the effect drives
    order = _ORDER.get(effect, _DEFAULT_ORDER)

    def find_levels(skip: int | None = None) -> list[int]:
        for kind in order:
            hit = next((k for k, c in real if k != skip and level_range(c, _KINDS[kind]) is not None), None)
            if hit is not None:
                return [hit]
        return [k for k in colours if k != skip]      # a light without a dimmer: its colours are its brightness

    if effect == "strobe":
        found = next(((k, c, r) for k, c in real if (r := strobe_range(c)) is not None), None)
        if found is not None:
            k, channel, (lo, hi, hz_lo, hz_hi, known) = found
            if known and hz_lo <= max_rate_hz + 1e-9 and hz_lo <= STROBE_HARD_MAX_HZ:
                result.flash = "fixture"            # it flashes itself, as slowly as the venue asks
                levels = find_levels(skip=k)
                closed = _shutter(channel, "Closed")
                nothing = channel.of(("NoFunction",))
                off = 0 if levels else closed if closed is not None else min((c.lo for c in nothing), default=0)
                put(k, channel, role="rate", min=lo, max=hi, off=off, hz_min=hz_lo, hz_max=hz_hi)
                result.driven = channel.name
            else:
                levels = find_levels()              # also a dimmer that shares the channel with the strobe
                why = (f"its own strobe cannot flash slower than {hz_lo:g} times a second, above the limit of {max_rate_hz:g}"
                       if known else "the fixture file does not give its flash rates in Hz")
                if not levels:
                    raise FixtureError(f"{model.key}, mode {mode.short} cannot be patched as a strobe: {why}, and it has no "
                                       "dimmer that could be switched instead - measure its rates and write its channels by hand")
                result.flash = "software"
                result.notes.append(f'{why}: the flashes are made by switching its brightness (flash = "software"), '
                                    "which keeps to the venue's limit")
        else:
            levels = find_levels()
            result.flash = "software"
            result.notes.append('no strobe channel: the flashes are made by switching its brightness (flash = "software")')
    else:
        levels = find_levels()
    if not levels and not result.driven:
        levels = [real[0][0]]
        result.notes.append(f"none of its channels is a dimmer, an output or a speed: channel '{real[0][1].name}' is driven - "
                            "check that this is what the device needs")
    resting: list[str] = []
    for k, channel in slots:
        if k in used:
            continue
        if channel.fine_of:
            put(k, channel, role="static", value=0)
        elif k in levels:
            found3 = next((lr for kind in order if (lr := level_range(channel, _KINDS[kind])) is not None), None)
            off, lo, hi = found3 or level_range(channel, ("ColorIntensity",)) or (0, 0, 255)
            put(k, channel, role="level", min=lo, max=hi, off=off)
        elif k in colours:
            put(k, channel, role="static", value=255, when="active")
        elif light and "ShutterStrobe" in channel.types() and _shutter(channel, "Open") is not None:
            put(k, channel, role="static", value=_shutter(channel, "Open"), when="active", off=channel.default)
        elif channel.default:
            put(k, channel, role="static", value=channel.default, when="always")
        else:
            put(k, channel, role="static", value=0)
            if any(level_range(channel, _KINDS[kind]) is not None for kind in ("fog", "speed", "intensity")):
                resting.append(channel.name)
    if resting:
        result.notes.append(f"left at 0: {', '.join(resting)} - give {'it' if len(resting) == 1 else 'them'} a value in the patch "
                            "if the device needs one there (the fan of a hazer, a second dimmer)")
    if levels:
        names = [used[k]["name"] for k in levels]
        result.driven = names[0] if len(names) == 1 else "the colours (" + ", ".join(names) + ")"
    result.channels = [_tidy(used[k]) for k in sorted(used)]
    return result


_DEFAULTS = {"min": 0, "max": 255, "off": 0, "value": 0, "when": "active", "hz_min": 1.0, "hz_max": 20.0}
_ROLE_KEYS = {"level": ("min", "max", "off"), "rate": ("min", "max", "off", "hz_min", "hz_max"), "static": ("value", "when", "off")}


def _tidy(channel: dict[str, Any]) -> dict[str, Any]:
    """Only what differs from the defaults, in a fixed order."""
    out: dict[str, Any] = {"role": channel["role"], "offset": channel["offset"]}
    for key in _ROLE_KEYS[channel["role"]]:
        if key in channel and channel[key] != _DEFAULTS[key]:
            out[key] = channel[key]
    out["name"] = channel["name"]
    return out


def describe(model: Model, mode: Mode, result: Plan | None = None) -> list[str]:
    """Lines for a person: the channels of a mode, and (with a plan) what each does in the patch."""
    lines: list[str] = []
    roles = {c["offset"]: c for c in result.channels} if result is not None else {}
    for k, channel in enumerate(mode.channels):
        if channel is None:
            lines.append(f"  {k + 1:3d}  (not used)")
            continue
        text = f"  {k + 1:3d}  {channel.name:24s} {channel.describe()}"
        role = roles.get(k)
        if role is not None:
            if role["role"] == "level":
                what = f"driven by the effect: off {role.get('off', 0)}, {role.get('min', 0)} … {role.get('max', 255)}"
            elif role["role"] == "rate":
                what = (f"flash rate: {role.get('hz_min', 1.0):g} Hz at {role.get('min', 0)} … "
                        f"{role.get('hz_max', 20.0):g} Hz at {role.get('max', 255)}, off {role.get('off', 0)}")
            elif role.get("when") == "always":
                what = f"always {role.get('value', 0)}"
            elif role.get("value", 0):
                what = f"{role['value']} while the effect is on"
            else:
                what = "0"
            text += f"\n       -> {what}"
        elif result is not None:
            text += "\n       -> left free"
        lines.append(text)
    return lines


def build_snapshot(folder: str | Path, commit: str = "", date: str = "") -> bytes:
    """The content of data/ofl-fixtures.json.xz from a copy of the Open Fixture Library (its fixtures/
    folder): every fixture file as it is, without the parts no patch needs (pictures of gobos, links,
    physical data, the editing history)."""
    root = Path(folder)
    root = root / "fixtures" if (root / "fixtures").is_dir() else root
    names = json.loads((root / "manufacturers.json").read_text(encoding="utf-8"))
    fixtures: dict[str, Any] = {}
    for path in sorted(root.glob("*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "modes" not in data:
            continue
        for drop in ("$schema", "meta", "links", "physical", "wheels", "rdm", "helpWanted", "matrix"):
            data.pop(drop, None)
        for mode in data.get("modes", []):
            if isinstance(mode, dict):
                for drop in ("physical", "rdmPersonalityIndex"):
                    mode.pop(drop, None)
        fixtures[f"{path.parent.name}/{path.stem}"] = data
    doc = {"source": "https://github.com/OpenLightingProject/open-fixture-library", "commit": commit, "date": date,
           "licence": "MIT - see OFL-LICENSE.txt", "manufacturers": {k: v.get("name", k) for k, v in names.items() if isinstance(v, dict)},
           "fixtures": fixtures}
    return lzma.compress(json.dumps(doc, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8"), preset=9)
