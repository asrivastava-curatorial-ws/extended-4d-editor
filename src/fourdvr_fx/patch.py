"""Venue effects patch: which fixtures exist, where they are on DMX, and their limits.

The patch (TOML, config/effects/patch.example.toml) belongs to the venue, not to the content: the same
programme plays in a theatre with one fog machine or with four, with fans on each platform or none.
Effect types without a patched fixture are simply not played.

    [output]            protocol and refresh rate, [output.sacn] / [output.artnet] / [output.enttec]
    [audio]             bass (subwoofer / bass shaker) output, optional
    [types.<effect>]    enabled, master level
    [[fixtures]]        id, effect, scope (theatre | platform), universe, address, channels, lead_s, limits
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dmx import ArtNetOutput, CaptureOutput, DmxOutput, EnttecProOutput, NullOutput, SacnOutput
from .model import ID_RE, RAW_DMX

PROTOCOLS = ("sacn", "artnet", "enttec-pro", "esp32", "capture", "none")
ESP32_PORT = 47002
ESP32_THEATRE_UNIVERSES = (1, 2)     # esp32 output: theatre fixtures are on the theatre ESP32's DMX line 1 (or 2)
ESP32_PLATFORM_UNIVERSE = 100        # esp32 output: the k-th platform's own DMX line counts as universe 101 + k
SCOPES = ("theatre", "platform")
ROLES = ("level", "switch", "rate", "static")
STROBE_HARD_MAX_HZ = 25.0
# Flash-rate ceiling applied unless the venue sets its own after its photosensitivity assessment.
STROBE_DEFAULT_MAX_HZ = 3.0


class PatchError(ValueError):
    pass


@dataclass
class Limits:
    max_level: float = 1.0
    max_on_s: float = 0.0          # longest continuous activation (0 = unlimited)
    min_off_s: float = 0.0         # pause enforced after every activation
    duty_window_s: float = 0.0     # sliding window for max_duty (0 = off)
    max_duty: float = 1.0          # largest fraction of the window the effect may be on
    max_total_on_s: float = 0.0    # per show run (0 = unlimited)
    ramp_up_s: float = 0.0         # time for 0 -> 1 (slew limit)
    ramp_down_s: float = 0.0
    max_rate_hz: float = STROBE_DEFAULT_MAX_HZ   # strobe flash-rate ceiling

    def describe(self) -> str:
        parts = [f"level ≤ {self.max_level * 100:g} %"] if self.max_level < 1.0 else []
        if self.ramp_up_s or self.ramp_down_s:
            parts.append(f"ramp {self.ramp_up_s:g}/{self.ramp_down_s:g} s")
        if self.max_on_s:
            parts.append(f"on ≤ {self.max_on_s:g} s")
        if self.min_off_s:
            parts.append(f"off ≥ {self.min_off_s:g} s")
        if self.duty_window_s:
            parts.append(f"duty ≤ {self.max_duty * 100:g} % / {self.duty_window_s:g} s")
        if self.max_total_on_s:
            parts.append(f"total ≤ {self.max_total_on_s:g} s per show")
        return ", ".join(parts) or "no limits"


# Conservative defaults per effect type; a fixture's [fixtures.limits] overrides single values.
TYPE_LIMITS: dict[str, dict[str, float]] = {
    "wind": {"ramp_up_s": 0.5, "ramp_down_s": 0.5},
    "water": {"max_on_s": 2.0, "min_off_s": 1.0, "max_total_on_s": 30.0},
    "rain": {"max_on_s": 5.0, "min_off_s": 5.0, "max_total_on_s": 40.0},
    "heat": {"max_on_s": 6.0, "min_off_s": 10.0, "duty_window_s": 60.0, "max_duty": 0.25},
    "bubbles": {"max_on_s": 10.0, "min_off_s": 5.0, "max_total_on_s": 60.0},
    "snow": {"max_on_s": 10.0, "min_off_s": 5.0, "max_total_on_s": 60.0},
    "air": {"max_on_s": 1.0, "min_off_s": 0.3},
    "fog": {"max_on_s": 5.0, "min_off_s": 10.0, "duty_window_s": 60.0, "max_duty": 0.25},
    "strobe": {"max_on_s": 5.0, "min_off_s": 1.0, "max_rate_hz": STROBE_DEFAULT_MAX_HZ},
    "scent": {"max_on_s": 6.0, "min_off_s": 15.0},
}


# effect types that put something into the room: a fixture of one of them without any activation limit earns a warning
LIMITED_BY_DEFAULT = ("fog", "water", "rain", "scent", "strobe", "heat", "bubbles", "snow")


@dataclass
class ChannelDef:
    role: str = "level"
    offset: int = 0          # channel offset from the fixture address
    min: int = 0             # DMX value at the smallest non-zero level
    max: int = 255           # DMX value at level 1
    off: int = 0             # DMX value while the effect is off
    gamma: float = 1.0
    on: int = 255            # switch: value when on
    threshold: float = 0.01  # switch: level above which it is on
    value: int = 0           # static: fixed value
    when: str = "active"     # static: "active" (only while the effect is on) or "always"
    hz_min: float = 1.0      # rate: flash rate mapped to min .. max
    hz_max: float = 20.0

    def render(self, level: float, rate_hz: float) -> int:
        if self.role == "level":
            if level <= 0.0:
                return self.off
            return round(self.min + (self.max - self.min) * (min(1.0, level) ** self.gamma))
        if self.role == "switch":
            return self.on if level > self.threshold else self.off
        if self.role == "rate":
            if level <= 0.0:
                return self.off
            k = (min(max(rate_hz, self.hz_min), self.hz_max) - self.hz_min) / max(self.hz_max - self.hz_min, 1e-9)
            return round(self.min + (self.max - self.min) * k)
        return self.value if (self.when == "always" or level > 0.0) else self.off   # static


PROFILES: dict[str, list[dict[str, Any]]] = {
    "dimmer": [{"role": "level"}],
    "switch": [{"role": "switch"}],
    "level-rate": [{"role": "level"}, {"role": "rate", "offset": 1}],
}


@dataclass
class FixtureDef:
    id: str
    effect: str
    scope: str = "theatre"
    universe: int = 1
    address: int = 1
    channels: list[ChannelDef] = field(default_factory=lambda: [ChannelDef()])
    lead_s: float = 0.0                 # send this much earlier than the timeline (device latency)
    limits: Limits = field(default_factory=Limits)
    match: dict[str, float] = field(default_factory=dict)   # e.g. {slot = 2}: only cues with these parameters
    flash: str = "fixture"              # strobe: "fixture" (device flashes itself) or "software" (we pulse the level)
    flash_on_s: float = 0.03
    platforms: list[str] = field(default_factory=list)      # platform scope: which platforms (default: all)
    address_step: int = 0               # platform scope: address of platform k = address + k * step
    universe_step: int = 0
    addresses: dict[str, int] = field(default_factory=dict)  # platform scope: explicit address per platform
    label: str = ""

    @property
    def footprint(self) -> int:
        return max(c.offset for c in self.channels) + 1


@dataclass
class FixtureInstance:
    """One physical fixture: a theatre fixture, or one platform's copy of a platform fixture."""

    fixture: FixtureDef
    platform: str | None
    universe: int
    address: int

    @property
    def key(self) -> str:
        return self.fixture.id if self.platform is None else f"{self.fixture.id}@{self.platform}"

    def channels(self) -> list[int]:
        return [self.address + c.offset for c in self.fixture.channels]


@dataclass
class OutputConfig:
    protocol: str = "none"
    refresh_hz: float = 40.0
    sacn: dict[str, Any] = field(default_factory=dict)
    artnet: dict[str, Any] = field(default_factory=dict)
    enttec: dict[str, Any] = field(default_factory=dict)
    esp32: dict[str, Any] = field(default_factory=dict)       # port, theatre, theatre_platform, platforms {id: host}
    esp32_audio: AudioConfig | None = None                     # bass / vibration through the ESP32s' audio outputs

    def create(self) -> DmxOutput:
        """The output of this protocol. "esp32" needs the platforms as well: fourdvr_fx.bridge.create_output()."""
        if self.protocol == "sacn":
            s = self.sacn
            return SacnOutput(
                source_name=str(s.get("source_name", "4D VR Theatre")),
                priority=int(s.get("priority", 100)),
                multicast=bool(s.get("multicast", True)),
                unicast=[str(x) for x in s.get("unicast", [])],
                interface_ip=str(s.get("interface_ip", "")),
                port=int(s.get("port", 5568)),
            )
        if self.protocol == "artnet":
            a = self.artnet
            return ArtNetOutput(
                targets=[str(x) for x in a.get("targets", ["255.255.255.255"])],
                universe_base=int(a.get("universe_base", 0)),
                port=int(a.get("port", 6454)),
                interface_ip=str(a.get("interface_ip", "")),
            )
        if self.protocol == "enttec-pro":
            e = self.enttec
            if not e.get("port"):
                raise PatchError('[output.enttec] needs port = "/dev/ttyUSB0" (or "COM3")')
            return EnttecProOutput(str(e["port"]), int(e.get("universe", 1)))
        if self.protocol == "capture":
            return CaptureOutput()
        return NullOutput()


@dataclass
class AudioConfig:
    enabled: bool = False
    device: str = ""                    # substring of the output device name; empty = system default
    sample_rate_hz: int = 48000
    channels: int = 2
    bass_channels: list[int] = field(default_factory=lambda: [1, 2])     # 1-based output channels
    vibration_channels: list[int] = field(default_factory=lambda: [1, 2])
    vibration_frequency_hz: float = 45.0
    bass_gain_db: float = 0.0
    vibration_gain_db: float = -6.0
    latency_offset_s: float = 0.0       # positive: play earlier (compensates output latency)
    buffer_ms: int = 40


@dataclass
class TypeConfig:
    enabled: bool = True
    master: float = 1.0


@dataclass
class Patch:
    output: OutputConfig = field(default_factory=OutputConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    types: dict[str, TypeConfig] = field(default_factory=dict)
    fixtures: list[FixtureDef] = field(default_factory=list)
    allow_raw_dmx: bool = True
    source: str = ""

    def type_config(self, name: str) -> TypeConfig:
        return self.types.get(name) or TypeConfig()

    def effects(self) -> list[str]:
        return sorted({f.effect for f in self.fixtures})

    def instances(self, platform_ids: list[str]) -> list[FixtureInstance]:
        out: list[FixtureInstance] = []
        esp32 = self.output.protocol == "esp32"
        for f in self.fixtures:
            if f.scope == "theatre":
                out.append(FixtureInstance(f, None, f.universe, f.address))
                continue
            targets = f.platforms or list(f.addresses) or platform_ids
            for k, pid in enumerate(targets):
                if pid not in platform_ids:
                    continue    # a platform hosted by another controller process
                address = f.addresses.get(pid, f.address + k * f.address_step)
                # through ESP32s every platform has a DMX line of its own, counted as a universe of its own
                universe = ESP32_PLATFORM_UNIVERSE + 1 + platform_ids.index(pid) if esp32 else f.universe + k * f.universe_step
                out.append(FixtureInstance(f, pid, universe, address))
        return out

    def esp32_lines(self, platform_ids: list[str], default_hosts: dict[str, str] | None = None) -> dict[int, tuple[str, int]]:
        """esp32 output: universe -> (host of the ESP32, its DMX port), for the lines that have an ESP32.

        A platform's ESP32 is the one named in [output.esp32.platforms], else `default_hosts` (the show
        service passes the hosts of the motion links). The theatre devices are on the ESP32 with the
        address [output.esp32] theatre, or on the ESP32 of the platform named by theatre_platform - if
        that platform's ESP32 is known here (a controller process that does not serve it sends nothing).
        """
        cfg = self.output.esp32
        hosts = {**(default_hosts or {}), **{str(k): str(v) for k, v in cfg.get("platforms", {}).items()}}
        lines: dict[int, tuple[str, int]] = {}
        theatre = str(cfg.get("theatre", "")) or hosts.get(str(cfg.get("theatre_platform", "")), "")
        if theatre:
            for port, universe in enumerate(ESP32_THEATRE_UNIVERSES):
                lines[universe] = (theatre, port)
        for k, pid in enumerate(platform_ids):
            if hosts.get(pid):
                lines[ESP32_PLATFORM_UNIVERSE + 1 + k] = (hosts[pid], 0)
        return lines

    def problems(self, platform_ids: list[str]) -> list[str]:
        """Configuration errors that must be fixed before the patch is used."""
        out: list[str] = []
        used: dict[tuple[Any, int], str] = {}
        esp32 = self.output.protocol == "esp32"
        lines = self.esp32_lines(platform_ids) if esp32 else {}
        for inst in self.instances(platform_ids):
            f = inst.fixture
            if esp32 and inst.platform is None and inst.universe not in ESP32_THEATRE_UNIVERSES:
                out.append(f"fixture {inst.key}: with the esp32 output a theatre fixture is on universe 1 "
                           "(the theatre ESP32's DMX line) or 2 (its second line)")
            # two fixtures collide if they are on the same wire: the same universe - or, through ESP32s,
            # the same DMX port of the same ESP32 (theatre devices on a platform's ESP32, two platforms on one)
            wire: Any = lines.get(inst.universe, inst.universe) if esp32 else inst.universe
            where = f"universe {inst.universe}" if not esp32 or inst.universe not in lines else f"DMX line {wire[1] + 1} of {wire[0]}"
            for ch in inst.channels():
                if not 1 <= ch <= 512:
                    out.append(f"fixture {inst.key}: DMX channel {ch} outside 1..512")
                    continue
                other = used.get((wire, ch))
                if other is not None and other != inst.key:
                    out.append(f"fixture {inst.key}: {where} channel {ch} already used by {other}")
                used[(wire, ch)] = inst.key
            if (not esp32 and f.scope == "platform" and not f.address_step and not f.universe_step and not f.addresses
                    and len(platform_ids) > 1):
                out.append(f"fixture {f.id}: platform scope needs address_step, universe_step or addresses")
        if self.output.protocol == "enttec-pro":
            wanted = {i.universe for i in self.instances(platform_ids)} - {int(self.output.enttec.get("universe", 1))}
            if wanted:
                out.append(f"USB DMX output carries one universe; fixtures also use universes {sorted(wanted)}")
        if self.audio.enabled:
            a = self.audio
            for ch in [*a.bass_channels, *a.vibration_channels]:
                if not 1 <= ch <= a.channels:
                    out.append(f"[audio] channel {ch} outside 1..{a.channels}")
        return out

    def warnings(self) -> list[str]:
        out: list[str] = []
        e = self.output.esp32
        if (self.output.protocol == "esp32" and not e.get("theatre") and not e.get("theatre_platform")
                and any(f.scope == "theatre" for f in self.fixtures)):
            out.append('theatre fixtures have no ESP32: set [output.esp32] theatre = "<address>" or theatre_platform = "<platform id>"; '
                       "until then they stay off")
        for f in self.fixtures:
            if f.effect == "strobe" and f.limits.max_rate_hz > STROBE_DEFAULT_MAX_HZ:
                out.append(
                    f"fixture {f.id}: flash rate limit {f.limits.max_rate_hz:g} Hz is above {STROBE_DEFAULT_MAX_HZ:g} Hz - "
                    "only with a photosensitivity risk assessment and visitor warnings"
                )
            if f.effect == "strobe" and f.flash == "software" and f.limits.max_rate_hz > self.output.refresh_hz / 4.0:
                out.append(
                    f"fixture {f.id}: software flashes of up to {f.limits.max_rate_hz:g} per second are uneven at "
                    f"{self.output.refresh_hz:g} DMX frames per second - let the device flash itself (flash = \"fixture\")"
                )
            if f.effect in LIMITED_BY_DEFAULT and not (f.limits.max_on_s or f.limits.duty_window_s):
                out.append(f"fixture {f.id}: no activation limit configured for a {f.effect} fixture")
        return out


def _int(d: dict[str, Any], key: str, default: int, lo: int, hi: int, what: str) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise PatchError(f"{what}.{key} must be an integer {lo}..{hi}")
    return v


def _float(d: dict[str, Any], key: str, default: float, lo: float, hi: float, what: str) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise PatchError(f"{what}.{key} must be a number {lo:g}..{hi:g}")
    return float(v)


def _known(d: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise PatchError(f"{what}: unknown key(s) {', '.join(unknown)}")


def _channel(d: Any, what: str) -> ChannelDef:
    if not isinstance(d, dict):
        raise PatchError(f"{what} must be a table")
    _known(d, {"role", "offset", "min", "max", "off", "gamma", "on", "threshold", "value", "when", "hz_min", "hz_max"}, what)
    role = str(d.get("role", "level"))
    if role not in ROLES:
        raise PatchError(f"{what}.role must be one of {', '.join(ROLES)}")
    when = str(d.get("when", "active"))
    if when not in ("active", "always"):
        raise PatchError(f'{what}.when must be "active" or "always"')
    ch = ChannelDef(
        role=role,
        offset=_int(d, "offset", 0, 0, 511, what),
        min=_int(d, "min", 0, 0, 255, what),
        max=_int(d, "max", 255, 0, 255, what),
        off=_int(d, "off", 0, 0, 255, what),
        gamma=_float(d, "gamma", 1.0, 0.1, 5.0, what),
        on=_int(d, "on", 255, 0, 255, what),
        threshold=_float(d, "threshold", 0.01, 0.0, 1.0, what),
        value=_int(d, "value", 0, 0, 255, what),
        when=when,
        hz_min=_float(d, "hz_min", 1.0, 0.1, STROBE_HARD_MAX_HZ, what),
        hz_max=_float(d, "hz_max", 20.0, 0.1, 50.0, what),
    )
    if ch.hz_max <= ch.hz_min:
        raise PatchError(f"{what}: hz_max must be above hz_min")
    return ch


def _limits(effect: str, d: Any, what: str) -> Limits:
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise PatchError(f"{what} must be a table")
    _known(d, set(Limits.__dataclass_fields__), what)
    merged = {**TYPE_LIMITS.get(effect, {}), **d}
    lim = Limits(
        max_level=_float(merged, "max_level", 1.0, 0.0, 1.0, what),
        max_on_s=_float(merged, "max_on_s", 0.0, 0.0, 3600.0, what),
        min_off_s=_float(merged, "min_off_s", 0.0, 0.0, 3600.0, what),
        duty_window_s=_float(merged, "duty_window_s", 0.0, 0.0, 3600.0, what),
        max_duty=_float(merged, "max_duty", 1.0, 0.0, 1.0, what),
        max_total_on_s=_float(merged, "max_total_on_s", 0.0, 0.0, 86400.0, what),
        ramp_up_s=_float(merged, "ramp_up_s", 0.0, 0.0, 60.0, what),
        ramp_down_s=_float(merged, "ramp_down_s", 0.0, 0.0, 60.0, what),
        max_rate_hz=_float(merged, "max_rate_hz", STROBE_DEFAULT_MAX_HZ, 0.1, STROBE_HARD_MAX_HZ, what),
    )
    return lim


def default_limits(effect: str) -> Limits:
    """The limits a fixture of this effect type gets when its patch entry sets none."""
    return _limits(effect, None, effect)


def _fixture(d: Any, index: int) -> FixtureDef:
    what = f"fixtures[{index}]"
    if not isinstance(d, dict):
        raise PatchError(f"{what} must be a table")
    _known(d, {"id", "effect", "scope", "universe", "address", "channels", "profile", "lead_s", "limits", "match", "flash",
               "flash_on_s", "platforms", "address_step", "universe_step", "addresses", "label"}, what)
    fid, effect = d.get("id"), d.get("effect")
    if not isinstance(fid, str) or not ID_RE.match(fid):
        raise PatchError(f"{what}.id must match {ID_RE.pattern}")
    what = f"fixture {fid}"
    if not isinstance(effect, str) or not ID_RE.match(effect) or effect == RAW_DMX:
        raise PatchError(f'{what}.effect must be an effect type name such as "wind" (not "{RAW_DMX}")')
    scope = str(d.get("scope", "theatre"))
    if scope not in SCOPES:
        raise PatchError(f"{what}.scope must be theatre or platform")
    if "channels" in d and "profile" in d:
        raise PatchError(f"{what}: give channels or profile, not both")
    profile = str(d.get("profile", "dimmer"))
    if "channels" not in d and profile not in PROFILES:
        raise PatchError(f"{what}.profile must be one of {', '.join(PROFILES)}")
    raw_channels = d.get("channels", PROFILES.get(profile, PROFILES["dimmer"]))
    if not isinstance(raw_channels, list) or not raw_channels or len(raw_channels) > 32:
        raise PatchError(f"{what}.channels must be a list of 1..32 channel tables")
    channels = [_channel(c, f"{what}.channels[{k}]") for k, c in enumerate(raw_channels)]
    if len({c.offset for c in channels}) != len(channels):
        raise PatchError(f"{what}: two channels share an offset")
    match = d.get("match", {})
    if not isinstance(match, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in match.values()):
        raise PatchError(f"{what}.match must be a table of numbers, e.g. {{ slot = 2 }}")
    flash = str(d.get("flash", "fixture"))
    if flash not in ("fixture", "software"):
        raise PatchError(f'{what}.flash must be "fixture" or "software"')
    platforms = d.get("platforms", [])
    addresses = d.get("addresses", {})
    if not isinstance(platforms, list) or not isinstance(addresses, dict):
        raise PatchError(f"{what}: platforms must be a list and addresses a table")
    for pid, addr in addresses.items():
        if isinstance(addr, bool) or not isinstance(addr, int) or not 1 <= addr <= 512:
            raise PatchError(f"{what}.addresses.{pid} must be 1..512")
    return FixtureDef(
        id=fid,
        effect=effect,
        scope=scope,
        universe=_int(d, "universe", 1, 1, 63999, what),
        address=_int(d, "address", 1, 1, 512, what),
        channels=channels,
        lead_s=_float(d, "lead_s", 0.0, 0.0, 30.0, what),
        limits=_limits(effect, d.get("limits"), f"{what}.limits"),
        match={str(k): float(v) for k, v in match.items()},
        flash=flash,
        flash_on_s=_float(d, "flash_on_s", 0.03, 0.01, 0.5, what),
        platforms=[str(p) for p in platforms],
        address_step=_int(d, "address_step", 0, 0, 512, what),
        universe_step=_int(d, "universe_step", 0, 0, 100, what),
        addresses={str(k): int(v) for k, v in addresses.items()},
        label=str(d.get("label", ""))[:80],
    )


def patch_from_dict(data: dict[str, Any], source: str = "") -> Patch:
    _known(data, {"output", "audio", "types", "fixtures", "allow_raw_dmx"}, "patch")
    out_d = data.get("output", {})
    if not isinstance(out_d, dict):
        raise PatchError("[output] must be a table")
    _known(out_d, {"protocol", "refresh_hz", "sacn", "artnet", "enttec", "esp32"}, "[output]")
    protocol = str(out_d.get("protocol", "none"))
    if protocol not in PROTOCOLS:
        raise PatchError(f"[output].protocol must be one of {', '.join(PROTOCOLS)}")
    for key, allowed in (("sacn", {"source_name", "priority", "multicast", "unicast", "interface_ip", "port"}),
                         ("artnet", {"targets", "universe_base", "port", "interface_ip"}),
                         ("enttec", {"port", "universe"}),
                         ("esp32", {"port", "theatre", "theatre_platform", "platforms", "audio"})):
        section = out_d.get(key, {})
        if not isinstance(section, dict):
            raise PatchError(f"[output.{key}] must be a table")
        _known(section, allowed, f"[output.{key}]")
    output = OutputConfig(
        protocol=protocol,
        refresh_hz=_float(out_d, "refresh_hz", 40.0, 1.0, 44.0, "[output]"),
        sacn=dict(out_d.get("sacn", {})),
        artnet=dict(out_d.get("artnet", {})),
        enttec=dict(out_d.get("enttec", {})),
    )
    if protocol == "esp32":
        e = dict(out_d.get("esp32", {}))
        platforms = e.get("platforms", {})
        if not isinstance(platforms, dict) or not all(isinstance(v, str) and v for v in platforms.values()):
            raise PatchError('[output.esp32.platforms] must be a table of platform id = "address of its ESP32"')
        ea = e.pop("audio", {})
        if not isinstance(ea, dict):
            raise PatchError("[output.esp32.audio] must be a table")
        _known(ea, {"enabled", "bass_channels", "vibration_channels", "vibration_frequency_hz", "bass_gain_db",
                    "vibration_gain_db", "latency_offset_s"}, "[output.esp32.audio]")
        if e.get("theatre") and e.get("theatre_platform"):
            raise PatchError("[output.esp32]: give theatre (an address) or theatre_platform (a platform id), not both")
        output.esp32 = {"port": _int(e, "port", ESP32_PORT, 1, 65535, "[output.esp32]"), "theatre": str(e.get("theatre", "")),
                        "theatre_platform": str(e.get("theatre_platform", "")),
                        "platforms": {str(k): str(v) for k, v in platforms.items()}}
        output.esp32_audio = AudioConfig(
            enabled=bool(ea.get("enabled", True)), sample_rate_hz=4000, channels=2,
            bass_channels=[int(c) for c in ea.get("bass_channels", [1, 2])],
            vibration_channels=[int(c) for c in ea.get("vibration_channels", [1, 2])],
            vibration_frequency_hz=_float(ea, "vibration_frequency_hz", 45.0, 10.0, 200.0, "[output.esp32.audio]"),
            bass_gain_db=_float(ea, "bass_gain_db", 0.0, -60.0, 24.0, "[output.esp32.audio]"),
            vibration_gain_db=_float(ea, "vibration_gain_db", -6.0, -60.0, 24.0, "[output.esp32.audio]"),
            latency_offset_s=_float(ea, "latency_offset_s", 0.0, -1.0, 1.0, "[output.esp32.audio]"),
        )
        if any(c not in (1, 2) for c in [*output.esp32_audio.bass_channels, *output.esp32_audio.vibration_channels]):
            raise PatchError("[output.esp32.audio] channels are 1 (left) and 2 (right)")
    a = data.get("audio", {})
    if not isinstance(a, dict):
        raise PatchError("[audio] must be a table")
    _known(a, set(AudioConfig.__dataclass_fields__), "[audio]")
    channels = _int(a, "channels", 2, 1, 32, "[audio]")
    audio = AudioConfig(
        enabled=bool(a.get("enabled", False)),
        device=str(a.get("device", "")),
        sample_rate_hz=_int(a, "sample_rate_hz", 48000, 8000, 192000, "[audio]"),
        channels=channels,
        bass_channels=[int(c) for c in a.get("bass_channels", list(range(1, channels + 1)))],
        vibration_channels=[int(c) for c in a.get("vibration_channels", list(range(1, channels + 1)))],
        vibration_frequency_hz=_float(a, "vibration_frequency_hz", 45.0, 10.0, 200.0, "[audio]"),
        bass_gain_db=_float(a, "bass_gain_db", 0.0, -60.0, 24.0, "[audio]"),
        vibration_gain_db=_float(a, "vibration_gain_db", -6.0, -60.0, 24.0, "[audio]"),
        latency_offset_s=_float(a, "latency_offset_s", 0.0, -1.0, 1.0, "[audio]"),
        buffer_ms=_int(a, "buffer_ms", 40, 5, 500, "[audio]"),
    )
    types: dict[str, TypeConfig] = {}
    for name, td in (data.get("types") or {}).items():
        if not isinstance(td, dict) or not ID_RE.match(str(name)):
            raise PatchError(f"[types.{name}] must be a table with a valid effect type name")
        _known(td, {"enabled", "master"}, f"[types.{name}]")
        types[str(name)] = TypeConfig(bool(td.get("enabled", True)), _float(td, "master", 1.0, 0.0, 1.0, f"[types.{name}]"))
    fixtures_in = data.get("fixtures", [])
    if not isinstance(fixtures_in, list):
        raise PatchError("[[fixtures]] must be an array of tables")
    fixtures = [_fixture(f, i) for i, f in enumerate(fixtures_in)]
    ids = [f.id for f in fixtures]
    if len(set(ids)) != len(ids):
        raise PatchError("fixture ids must be unique")
    return Patch(output, audio, types, fixtures, bool(data.get("allow_raw_dmx", True)), source)


def load_patch(path: str | Path) -> Patch:
    p = Path(path).expanduser()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PatchError(f"{p}: {exc}") from exc
    return patch_from_dict(data, str(p))
