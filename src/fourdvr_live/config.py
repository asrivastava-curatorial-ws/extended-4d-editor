"""Configuration of a live show (``live.toml``): which source, which platforms and effects, how strong.

The platforms and the venue patch are best taken from the show service's own configuration
(``controller = "controller.toml"``): the same profiles, addresses and limits, nothing to keep in
step by hand. Without it, platforms and a patch can be listed here. Everything is optional but the
source: with no platform and no patch, ``fourdvr live run`` still shows what the source delivers and
plays the shakers' sound on this computer.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fourdvr_fx.patch import Patch, PatchError, load_patch
from fourdvr_motion.config import ControllerConfigError, load_controller_config
from fourdvr_motion.platform_config import PlatformConfig, PlatformConfigError, load_platform_config

from .cueing import LiveCueingSettings
from .effects import LiveRules
from .safety import AxisMap, SafetySettings
from .shaker import ShakerSettings

SECTIONS = ("live", "platforms", "effects", "source", "axes", "cueing", "safety", "rules", "shaker")


class LiveConfigError(ValueError):
    pass


@dataclass
class LiveConfig:
    source: dict[str, Any] = field(default_factory=lambda: {"type": "demo"})
    rate_hz: float = 100.0
    platforms: list[PlatformConfig] = field(default_factory=list)
    patch: Patch | None = None
    reference_platform: str = ""
    latency_compensation_s: float = 0.0
    cache_dir: Path | None = None
    axes: dict[str, float] = field(default_factory=dict)
    cueing: LiveCueingSettings = field(default_factory=LiveCueingSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    rules: LiveRules = field(default_factory=LiveRules)
    shaker: ShakerSettings = field(default_factory=ShakerSettings)
    record_dir: Path | None = None       # every run is recorded into this folder
    hold: bool = False                   # start held: nothing moves until the operator releases it
    sound: bool = True                   # without a patch: shaker sound on this computer's default output
    origin: str = ""
    notes: list[str] = field(default_factory=list)

    def check(self) -> None:
        if not 50.0 <= self.rate_hz <= 200.0:
            raise LiveConfigError("rate_hz must be between 50 and 200")
        ids = [p.platform_id for p in self.platforms]
        if len(set(ids)) != len(ids):
            raise LiveConfigError("two platforms have the same id")
        try:
            AxisMap(self.axes)
            self.cueing.check()
            self.safety.check()
        except ValueError as exc:
            raise LiveConfigError(str(exc)) from exc
        if self.patch is not None:
            problems = self.patch.problems(ids)
            if problems:
                raise LiveConfigError("effects patch: " + "; ".join(problems))

    def describe(self) -> list[str]:
        from .sources import info

        src = info(str(self.source.get("type", "open")))
        lines = [f"source      {src.title}" + (f" (UDP port {self.source.get('port', src.port)})" if src.port else "")]
        if self.platforms:
            lines += [f"platform    {p.platform_id}  {p.driver}  {p.address or '-'}" for p in self.platforms]
        else:
            lines.append("platform    none (effects and sound only)")
        if self.patch is not None:
            lines.append(f"effects     {self.patch.source or 'patch'}: output {self.patch.output.protocol}, "
                         f"{len(self.patch.fixtures)} fixture type(s)" + (", bass output" if self.patch.audio.enabled else ""))
        else:
            lines.append("effects     no venue patch" + (": shaker sound on this computer" if self.sound else ""))
        turned = {k: v for k, v in self.axes.items() if v != 1.0}
        if turned:
            lines.append("directions  " + ", ".join(f"{k} x {v:g}" for k, v in turned.items()))
        lines.append(f"strength    {self.cueing.gain:g}" + ("   (starts held)" if self.hold else ""))
        return lines


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise LiveConfigError(f"[{name}] must be a table")
    return value


def _platform(entry: dict[str, Any], base: Path) -> PlatformConfig:
    if "config" not in entry:
        raise LiveConfigError('each [[platforms]] entry needs config = "platforms/<profile>.toml"')
    unknown = sorted(set(entry) - {"config", "id", "name", "link_platform_id", "address", "driver"})
    if unknown:
        raise LiveConfigError(f"[[platforms]]: unknown key(s) {', '.join(unknown)}")
    pc = load_platform_config(base / str(entry["config"]))
    return replace(
        pc,
        platform_id=str(entry.get("id", pc.platform_id)),
        name=str(entry.get("name", entry.get("id", pc.name))),
        link_platform_id=int(entry.get("link_platform_id", pc.link_platform_id)),
        address=str(entry.get("address", pc.address)),
        driver=str(entry.get("driver", pc.driver)),
    )


def parse_live_config(data: dict[str, Any], base: Path, origin: str = "") -> LiveConfig:
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise LiveConfigError(f"unknown section(s): {', '.join('[' + u + ']' for u in unknown)}")
    live = _table(data, "live")
    bad = sorted(set(live) - {"controller", "rate_hz", "record", "hold", "sound", "latency_compensation_s"})
    if bad:
        raise LiveConfigError(f"[live]: unknown key(s) {', '.join(bad)}")
    cfg = LiveConfig(origin=origin)
    try:
        if live.get("controller"):
            ctl = load_controller_config(base / str(live["controller"]))
            cfg.platforms = [entry.config for entry in ctl.platforms]
            cfg.rate_hz = ctl.control_rate_hz
            cfg.latency_compensation_s = ctl.latency_compensation_s
            cfg.cache_dir = ctl.cache_dir
            if ctl.effects is not None and ctl.effects.enabled:
                cfg.patch = load_patch(ctl.effects.patch_path)
                cfg.reference_platform = ctl.effects.reference_platform
            cfg.notes.append(f"platforms and effects from {ctl.source}")
            if "platforms" in data or "effects" in data:
                raise LiveConfigError("with [live] controller = ..., leave out [[platforms]] and [effects]: they come "
                                      "from that file")
        else:
            entries = data.get("platforms", [])
            if not isinstance(entries, list):
                raise LiveConfigError("[[platforms]] must be a list of tables")
            cfg.platforms = [_platform(e, base) for e in entries]
            fx = _table(data, "effects")
            bad = sorted(set(fx) - {"patch", "reference_platform"})
            if bad:
                raise LiveConfigError(f"[effects]: unknown key(s) {', '.join(bad)}")
            if fx.get("patch"):
                cfg.patch = load_patch(base / str(fx["patch"]))
            cfg.reference_platform = str(fx.get("reference_platform", ""))
        cfg.rate_hz = float(live.get("rate_hz", cfg.rate_hz))
        cfg.latency_compensation_s = float(live.get("latency_compensation_s", cfg.latency_compensation_s))
        cfg.hold = bool(live.get("hold", False))
        cfg.sound = bool(live.get("sound", True))
        if live.get("record"):
            cfg.record_dir = (base / str(live["record"])).resolve()
        source = _table(data, "source")
        if source:
            cfg.source = dict(source)
        if "file" in cfg.source:          # a session or video file, relative to the configuration
            cfg.source["file"] = str((base / str(cfg.source["file"])).resolve())
        cfg.axes = {str(k): float(v) for k, v in _table(data, "axes").items()}
        cfg.cueing = LiveCueingSettings.from_dict(_table(data, "cueing"))
        safety = _table(data, "safety")
        bad = sorted(set(safety) - set(SafetySettings.__dataclass_fields__))
        if bad:
            raise LiveConfigError(f"[safety]: unknown key(s) {', '.join(bad)}")
        cfg.safety = SafetySettings(**{k: float(v) for k, v in safety.items()})
        cfg.rules = LiveRules.from_dict(_table(data, "rules"))
        cfg.shaker = ShakerSettings.from_dict(_table(data, "shaker"))
    except (ControllerConfigError, PlatformConfigError, PatchError) as exc:
        raise LiveConfigError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, LiveConfigError):
            raise
        raise LiveConfigError(str(exc)) from exc
    cfg.check()
    return cfg


def load_live_config(path: str | Path) -> LiveConfig:
    p = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LiveConfigError(f"{p}: {exc}") from exc
    try:
        return parse_live_config(data, p.parent, str(p))
    except LiveConfigError as exc:
        raise LiveConfigError(f"{p}: {exc}") from exc


EXAMPLE = '''# Extended 4D Editor - a live show: motion, effects and bass from a running game or a live picture.
#   fourdvr live check -c live.toml          what this file says, and whether it can run
#   fourdvr live monitor -c live.toml        what the source delivers, in plain words (check the directions)
#   fourdvr live run -c live.toml            the show
# docs/LIVE.md explains every setting.

[live]
controller = "controller.toml"   # platforms, their addresses and limits, and the venue's effects patch:
                                 # the same file the show service uses. Leave out for a bench test.
# record = "sessions"            # record every run into this folder (.4dlive; open it in the Editor)
# hold = true                    # start held: nothing moves until the operator releases it (Space)

[source]
type = "forza"                   # 'fourdvr live sources' lists them: open, forza, f1, dirt, pcars2, assetto,
port = 5300                      # outsim, beamng, xplane, flightgear, gt7, screen, sound, replay, demo
# host = "192.168.10.60"         # the game's / console's address, where the format needs it (assetto, gt7)

[axes]                           # one factor per direction, applied to what the source delivers:
# yaw = -1.0                     # -1 turns a direction round, 0 switches it off - see "Checking the directions"

[cueing]
gain = 0.7                       # overall strength, 0..2 (start low)
# accel_scale = 0.2              # how much of the vehicle's real acceleration the seat shows
# max_tilt_deg = 7.0             # the lean that stands for sustained braking / cornering
# tilt_rate_deg_s = 5.0

[rules]                          # effects made from the telemetry
# wind_full_m_s = 55.0           # full wind from this speed
# strobe = false                 # a source may not flash the strobe unless this is true
# show_minutes = 15              # the fixtures' "per show" totals start again after this long

[shaker]
# engine_level = 0.5
# road_level = 0.6
'''
