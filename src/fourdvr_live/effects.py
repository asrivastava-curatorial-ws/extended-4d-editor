"""Effects from a live source.

``LiveRules`` turn each sample into effect levels - wind from speed, seat vibration from engine and
road, an air blast at a hit, and whatever the source states itself (rain, snow, fog ...).
``LiveDocument`` hands those levels to the unchanged effects engine in the place of a programme's
effect tracks, so the venue patch, its masters and every fixture's limits apply exactly as in a show.

Two things differ from a programme: a fixture's lead time cannot be honoured (nobody knows what the
game will do next), and the "per show" totals of the limits start again every ``show_minutes``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from fourdvr_fx.model import BUILTIN_TYPES, ON_THRESHOLD, CompiledDocument, EffectsDocument

from .filters import Smoother
from .sample import LiveSample

STATED = ("wind", "water", "rain", "air", "fog", "heat", "bubbles", "snow", "scent", "vibration", "strobe")


@dataclass
class LiveRules:
    wind: bool = True
    wind_start_m_s: float = 3.0           # below this speed: no wind
    wind_full_m_s: float = 55.0           # full wind from this speed (about 200 km/h)
    vibration: bool = True
    vibration_engine: float = 0.35        # level at full revs and full throttle
    vibration_road: float = 0.8           # level on the roughest surface
    air_on_impact: bool = True
    impact_threshold: float = 0.35        # a hit at least this strong fires the air blast
    air_seconds: float = 0.3
    water_on_splash: bool = True          # sources that report puddles / spray
    stated: bool = True                   # levels the source states itself (weather, own format)
    strobe: bool = False                  # a stated strobe is ignored unless this is switched on
    show_minutes: float = 15.0            # the limits' "per show" totals start again after this long

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LiveRules:
        r = cls()
        for key, value in data.items():
            if not hasattr(r, key):
                raise ValueError(f"unknown effects rule {key!r}")
            setattr(r, key, bool(value) if isinstance(getattr(r, key), bool) else float(value))
        if r.wind_full_m_s <= r.wind_start_m_s:
            raise ValueError("wind_full_m_s must be above wind_start_m_s")
        if not 1.0 <= r.show_minutes <= 24 * 60:
            raise ValueError("show_minutes must be 1-1440")
        return r


class LiveDocument(CompiledDocument):
    """Looks like a compiled effects document to the engine; its levels are whatever was set last."""

    def __init__(self) -> None:
        super().__init__(EffectsDocument(duration_s=0.0))
        self._levels: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._onsets: dict[str, float] = {}
        self.feed: Any = None            # live sound for the bass outputs (see fourdvr_live.shaker)

    def set(self, levels: dict[str, float], params: dict[str, dict[str, float]], position_s: float) -> None:
        onsets = {k: self._onsets.get(k, position_s) for k, v in levels.items() if v > ON_THRESHOLD}
        self._levels, self._params, self._onsets = dict(levels), dict(params), onsets

    def clear(self) -> None:
        self._levels, self._params, self._onsets = {}, {}, {}

    @property
    def levels(self) -> dict[str, float]:
        return dict(self._levels)

    def level(self, type_name: str, t: float, match: dict[str, float] | None = None) -> float:
        value = self._levels.get(type_name, 0.0)
        if value and match:
            params = self._params.get(type_name, {})
            if any(abs(params.get(k, math.nan) - v) > 1e-6 for k, v in match.items()):
                return 0.0
        return value

    def params(self, type_name: str, t: float) -> dict[str, float]:
        return dict(self._params.get(type_name, {}))

    def onset(self, type_name: str, t: float) -> float | None:
        return self._onsets.get(type_name)


class RuleEngine:
    """LiveRules applied sample by sample (called once per control period)."""

    def __init__(self, rules: LiveRules | None = None) -> None:
        self.rules = rules or LiveRules()
        self._wind = Smoother(0.6)
        self._vib = Smoother(0.08)
        self._air_until = -1e18
        self.levels: dict[str, float] = {}
        self.params: dict[str, dict[str, float]] = {}

    def reset(self) -> None:
        self._wind.value = self._vib.value = 0.0
        self._air_until = -1e18
        self.levels, self.params = {}, {}

    def step(self, sample: LiveSample | None, envelope: float, now: float, dt: float) -> dict[str, float]:
        r = self.rules
        levels: dict[str, float] = {}
        params: dict[str, dict[str, float]] = {}
        if sample is None or envelope <= 0.0:
            self._wind.step(0.0, dt)
            self._vib.step(0.0, dt)
            self.levels, self.params = {}, {}
            return {}
        if r.wind:
            want = (sample.speed - r.wind_start_m_s) / (r.wind_full_m_s - r.wind_start_m_s)
            levels["wind"] = self._wind.step(min(1.0, max(0.0, want)), dt)
        if r.vibration:
            engine = 0.0
            if sample.rpm > 0.0:
                top = sample.rpm_max if sample.rpm_max > 0.0 else 8000.0
                engine = r.vibration_engine * min(1.0, sample.rpm / top) * (0.3 + 0.7 * sample.throttle)
            road = r.vibration_road * max(sample.rumble, 0.6 * sample.slip)
            levels["vibration"] = self._vib.step(min(1.0, max(engine, road, sample.impact)), dt)
        if r.air_on_impact and sample.impact >= r.impact_threshold:
            self._air_until = now + r.air_seconds
        if now < self._air_until:
            levels["air"] = 1.0
        if r.stated:
            for name, value in sample.effects.items():
                if name == "splash":
                    if r.water_on_splash:
                        levels["water"] = max(levels.get("water", 0.0), value)
                    continue
                if name not in STATED and name in BUILTIN_TYPES:
                    continue                        # "dmx" and the like are not for a game to set
                if name == "strobe" and not r.strobe:
                    continue
                levels[name] = max(levels.get(name, 0.0), value)
            for name, p in sample.params.items():
                params[name] = {k: float(v) for k, v in p.items()}
        if "strobe" in levels:
            params.setdefault("strobe", {}).setdefault("rate_hz", 3.0)
        levels = {k: round(min(1.0, max(0.0, v)) * envelope, 4) for k, v in levels.items()}
        levels = {k: v for k, v in levels.items() if v > 0.0}
        self.levels, self.params = levels, params
        return levels
