"""Preflight: run a programme's effects through a venue patch offline and report what the venue
will actually output - which effect types have no fixture, and where a fixture's safety limits
(longest activation, cool-down, duty cycle, total per show, flash rate) will shorten or drop a cue.

The run uses the same engine as the show service on a simulated clock, so the result is what the
theatre does, not an approximation of it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from .engine import THEATRE, EffectsEngine, ShowState
from .model import ON_THRESHOLD, RAW_DMX, CompiledDocument, EffectsDocument
from .patch import TYPE_LIMITS, AudioConfig, FixtureDef, OutputConfig, Patch, default_limits

MAX_INTERVALS = 12


@dataclass
class FixtureReport:
    key: str
    effect: str
    requested_s: float = 0.0     # time the programme asks this fixture to be on
    output_s: float = 0.0        # time it is on after the limits
    held_s: float = 0.0          # requested, but held off by a limit
    peak: float = 0.0
    reasons: dict[str, float] = field(default_factory=dict)             # seconds held, per reason
    intervals: list[tuple[float, float, str]] = field(default_factory=list)  # timeline time, first few


@dataclass
class PreflightReport:
    duration_s: float
    fixtures: list[FixtureReport] = field(default_factory=list)
    unpatched: list[str] = field(default_factory=list)   # effect types of the programme without a fixture
    disabled: list[str] = field(default_factory=list)    # types switched off in the patch
    notes: list[str] = field(default_factory=list)

    @property
    def limited(self) -> list[FixtureReport]:
        return [f for f in self.fixtures if f.held_s > 0.02]

    def clean(self) -> bool:
        return not (self.limited or self.unpatched or self.disabled or self.notes)

    def lines(self) -> list[str]:
        out: list[str] = []
        for f in self.fixtures:
            if f.requested_s <= 0.0:
                continue
            text = f"{f.key:24s} {f.effect:10s} on {f.output_s:7.1f} s of {f.requested_s:7.1f} s requested, peak {f.peak * 100:3.0f} %"
            if f.held_s > 0.02:
                why = ", ".join(f"{k} {v:.1f} s" for k, v in sorted(f.reasons.items(), key=lambda kv: -kv[1]))
                text += f"  - LIMITED: {why}"
            out.append(text)
            for t0, t1, reason in f.intervals:
                out.append(f"{'':24s}   {_clock(t0)} - {_clock(t1)}  held off: {reason}")
        idle = [f.key for f in self.fixtures if f.requested_s <= 0.0]
        if idle:
            out.append("not used by this programme: " + ", ".join(idle))
        for name in self.unpatched:
            out.append(f"no fixture for effect type '{name}': its cues are ignored at this venue")
        for name in self.disabled:
            out.append(f"effect type '{name}' is switched off in the patch")
        out.extend(self.notes)
        return out


def _clock(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


DEFAULT_SOURCE = "default limits (no venue patch)"


def default_patch(types: Iterable[str]) -> Patch:
    """A venue that exists only on paper: one fixture with the default limits for every effect type
    given that has default limits. The editor checks a programme against it while no venue patch is
    loaded, so that cues the default limits would shorten are seen while editing. What the theatre
    really does is decided by its own patch."""
    names = sorted(n for n in set(types) if n in TYPE_LIMITS)
    fixtures = [
        FixtureDef(id=name, effect=name, address=1 + k, limits=default_limits(name), flash="software" if name == "strobe" else "fixture")
        for k, name in enumerate(names)
    ]
    return Patch(output=OutputConfig(protocol="none"), audio=AudioConfig(enabled=True, vibration_channels=[1, 2]),
                 fixtures=fixtures, source=DEFAULT_SOURCE)


def preflight(doc: EffectsDocument, patch: Patch, platform_ids: list[str]) -> PreflightReport:
    """Simulate one complete (non-looping) show run of `doc` on `patch`."""
    now = [1000.0]
    engine = EffectsEngine(patch, platform_ids, lambda: now[0])
    compiled: CompiledDocument = doc.compile()
    report = PreflightReport(doc.duration_s)
    used = {t.type for t in doc.tracks if t.enabled and not t.is_empty()}
    patched = set(patch.effects())
    boards = patch.output.esp32_audio if patch.output.protocol == "esp32" else None     # sound through the ESP32 boards
    board_sound = boards is not None and boards.enabled
    for name in sorted(used - {RAW_DMX}):
        if name == "vibration" and ((patch.audio.enabled and patch.audio.vibration_channels)
                                    or (boards is not None and board_sound and boards.vibration_channels)):
            continue
        if name not in patched:
            report.unpatched.append(name)
        elif not patch.type_config(name).enabled:
            report.disabled.append(name)
    if doc.bass is not None and not patch.audio.enabled and not board_sound:
        report.notes.append("the programme has a bass track, but [audio] is not enabled in the patch")
    raw = [t for t in doc.tracks if t.type == RAW_DMX and t.enabled]
    if raw and not patch.allow_raw_dmx:
        report.notes.append("raw DMX tracks are not allowed by the patch (allow_raw_dmx = false): they are ignored")
    elif raw:
        taken = {(i.universe, ch): i.key for i in patch.instances(platform_ids) for ch in i.channels()}
        for tr in raw:
            owner = taken.get((tr.universe, tr.channel))
            if owner:
                report.notes.append(f"raw DMX track {tr.id} ({tr.universe}/{tr.channel}) is overridden by fixture {owner}")
    wanted_hz = max((c.params.get("rate_hz", 0.0) for t in doc.tracks if t.type == "strobe" for c in t.cues), default=0.0)
    for f in patch.fixtures:
        if f.effect == "strobe" and wanted_hz > f.limits.max_rate_hz + 1e-9:
            report.notes.append(
                f"strobe cues ask for up to {wanted_hz:g} flashes/s; fixture {f.id} is limited to {f.limits.max_rate_hz:g}"
            )

    state = ShowState(True, compiled, 0.0, doc.duration_s, False, 1)
    shows = {THEATRE: state, **{pid: state for pid in platform_ids}}
    dt = engine.frame_s
    rows = {it.inst.key: FixtureReport(it.inst.key, it.inst.fixture.effect) for it in engine.instances}
    open_since: dict[str, tuple[float, str]] = {}
    steps = math.ceil(doc.duration_s / dt)
    for k in range(steps + 1):
        t = min(k * dt, doc.duration_s)
        now[0] = 1000.0 + k * dt
        state.position_s = t
        state.active = k < steps            # the last frame is the stop: everything is released
        engine.render(shows, now[0])
        for it in engine.instances:
            row = rows[it.inst.key]
            asked = it.requested > ON_THRESHOLD
            on = it.level > ON_THRESHOLD
            if asked:
                row.requested_s += dt
            if on:
                row.output_s += dt
                row.peak = max(row.peak, it.level)
            reason = it.limiter.reason if asked and not on else ""
            at = t + it.inst.fixture.lead_s      # where this is on the programme timeline
            started = open_since.get(row.key)
            if reason:
                if started is None:
                    started = open_since[row.key] = (at, reason)
                row.held_s += dt                 # the whole gap is charged to the limit that opened it
                row.reasons[started[1]] = row.reasons.get(started[1], 0.0) + dt
            elif started is not None:
                if len(row.intervals) < MAX_INTERVALS:
                    row.intervals.append((started[0], at, started[1]))
                del open_since[row.key]
    for key, (t0, reason) in open_since.items():
        if len(rows[key].intervals) < MAX_INTERVALS:
            rows[key].intervals.append((t0, doc.duration_s, reason))
    report.fixtures = list(rows.values())
    return report
