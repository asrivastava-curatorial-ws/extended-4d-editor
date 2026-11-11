"""Effects engine: turns the effect tracks of the running programme(s) into DMX frames.

For every patched fixture the engine takes the level of its effect type at the show position (plus the
fixture's lead time), applies the type master, the fixture's safety limiter and - for strobes - the
flash-rate ceiling, and renders the fixture's DMX channels. Theatre fixtures follow the theatre show;
a platform fixture follows its own platform and is off whenever that platform is not playing
(paused, stopped, faulted, not started).

The engine is deterministic and free of I/O: ``render()`` returns the frames, the caller sends them.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .dmx import DMX_CHANNELS
from .model import ON_THRESHOLD, RAW_DMX, CompiledDocument
from .patch import FixtureInstance, Limits, Patch

THEATRE = "theatre"


@dataclass
class ShowState:
    """Where one timeline is. `active` is False whenever effects must be off."""

    active: bool = False
    doc: CompiledDocument | None = None
    position_s: float = 0.0
    duration_s: float = 0.0
    loop: bool = False
    run_id: int = 0          # changes when a new run starts: per-show totals are reset


class Limiter:
    """Per-fixture activation limits (see patch.Limits). All times in seconds on the caller's clock."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.out = 0.0
        self.reason = ""
        self.total_on = 0.0
        self.trips = 0
        self._on_since: float | None = None
        self._blocked_until = -1e18
        self._wait_release = False
        self._window: deque[tuple[float, float]] = deque()
        self._window_on = 0.0

    def reset_show(self) -> None:
        self.total_on = 0.0

    def force_off(self) -> None:
        self.out = 0.0
        self._on_since = None

    def step(self, requested: float, now: float, dt: float) -> float:
        lim = self.limits
        want = min(max(requested, 0.0), lim.max_level)
        if want <= ON_THRESHOLD:
            want = 0.0        # below 1 % is "off": no trickle through a level channel, nothing for the limits to miss
        reason = ""
        if lim.duty_window_s:
            while self._window and self._window[0][0] < now - lim.duty_window_s:
                self._window_on -= self._window.popleft()[1]
        if want > ON_THRESHOLD:
            if self._wait_release or now < self._blocked_until:
                want, reason = 0.0, "cool-down"
            elif lim.max_total_on_s and self.total_on >= lim.max_total_on_s:
                want, reason = 0.0, "show total reached"
            elif lim.duty_window_s and self._window_on + dt > lim.max_duty * lim.duty_window_s + 1e-9:
                want, reason = 0.0, "duty limit"
                # pause long enough that the effect does not chatter at the edge of the window
                self._blocked_until = now + max(lim.min_off_s, 0.1 * lim.duty_window_s)
                self.trips += 1
            elif lim.max_on_s and self._on_since is not None and now - self._on_since >= lim.max_on_s - 1e-9:
                want, reason = 0.0, "maximum on-time"
                self._blocked_until = now + lim.min_off_s
                self._wait_release = lim.min_off_s <= 0.0   # without a cool-down: stay off until the cue ends
                self.trips += 1
        else:
            self._wait_release = False
        # slew limiting (soft start / stop); forced-off states drop at once
        if reason:
            self.out = 0.0
        elif want > self.out and lim.ramp_up_s > 0.0:
            self.out = min(want, self.out + dt / lim.ramp_up_s)
        elif want < self.out and lim.ramp_down_s > 0.0:
            self.out = max(want, self.out - dt / lim.ramp_down_s)
        else:
            self.out = want
        # accounting on what is actually output
        if self.out > ON_THRESHOLD:
            if self._on_since is None:
                self._on_since = now
            self.total_on += dt
            if lim.duty_window_s:
                self._window.append((now, dt))
                self._window_on += dt
        elif self._on_since is not None:
            self._on_since = None
            if not reason:
                self._blocked_until = max(self._blocked_until, now + lim.min_off_s)
        self.reason = reason
        return self.out


class _Instance:
    def __init__(self, inst: FixtureInstance) -> None:
        self.inst = inst
        self.limiter = Limiter(inst.fixture.limits)
        self.requested = 0.0
        self.level = 0.0
        self.rate_hz = 0.0
        self.values: list[int] = [c.off for c in inst.fixture.channels]
        self.run_id: int | None = None
        self.last_position = 0.0
        # software flash of a strobe
        self.last_flash: tuple[float, int] | None = None   # (cue start, flash number) of the last flash shown
        self.flash_lit = False                               # lit in the current frame
        self.flash_at = -1e18                                # when the last flash started (caller's clock)


class EffectsEngine:
    def __init__(
        self,
        patch: Patch,
        platform_ids: list[str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        problems = patch.problems(platform_ids)
        if problems:
            raise ValueError("effects patch: " + "; ".join(problems))
        self.patch = patch
        self.platform_ids = list(platform_ids)
        self.clock = clock
        self.frame_s = 1.0 / patch.output.refresh_hz
        self.instances = [_Instance(i) for i in patch.instances(platform_ids)]
        self.type_enabled = {name: cfg.enabled for name, cfg in patch.types.items()}
        self.type_master = {name: cfg.master for name, cfg in patch.types.items()}
        self.test_enabled = False
        self._overrides: dict[str, tuple[float, float]] = {}
        self._universes: set[int] = {i.inst.universe for i in self.instances}
        self._last: float | None = None
        self.frames = 0
        self.type_levels: dict[str, float] = {}

    # ------------------------------------------------------------------ operator / test controls
    def enabled(self, effect: str) -> bool:
        return self.type_enabled.get(effect, True)

    def master(self, effect: str) -> float:
        return self.type_master.get(effect, 1.0)

    def set_enabled(self, effect: str, on: bool) -> None:
        self.type_enabled[effect] = bool(on)

    def set_master(self, effect: str, level: float) -> None:
        self.type_master[effect] = min(1.0, max(0.0, float(level)))

    def fire(self, key: str, level: float = 1.0, seconds: float = 1.0) -> bool:
        """Manual test of one fixture instance (studio live mode / commissioning tool). The fixture's
        limits still apply. Returns False when testing is not enabled or the key is unknown."""
        if not self.test_enabled or not any(i.inst.key == key for i in self.instances):
            return False
        self._overrides[key] = (min(1.0, max(0.0, level)), self.clock() + min(max(seconds, 0.0), 30.0))
        return True

    def clear_overrides(self) -> None:
        self._overrides.clear()

    # ------------------------------------------------------------------ rendering
    def _buffers(self) -> dict[int, bytearray]:
        return {u: bytearray(DMX_CHANNELS) for u in sorted(self._universes)}

    def blackout(self) -> dict[int, bytes]:
        """All known universes at zero; limiter outputs reset (used on shutdown and on internal errors)."""
        for it in self.instances:
            it.limiter.force_off()
            it.level = it.requested = 0.0
            it.flash_lit = False
            it.values = [0] * len(it.values)
        self.type_levels = {}
        return {u: bytes(b) for u, b in self._buffers().items()}

    @staticmethod
    def _eval_time(show: ShowState, lead_s: float) -> float | None:
        t = show.position_s + lead_s
        if show.duration_s > 0.0 and t >= show.duration_s:
            if not show.loop:
                return None
            t -= show.duration_s
        return t

    def render(self, shows: Mapping[str, ShowState], now: float | None = None) -> dict[int, bytes]:
        now = self.clock() if now is None else now
        dt = self.frame_s if self._last is None else min(max(now - self._last, 0.0), 1.0)
        self._last = now
        self.frames += 1
        theatre = shows.get(THEATRE)
        # raw DMX tracks first, patched fixtures on top
        if (
            theatre is not None and theatre.active and theatre.doc is not None
            and self.patch.allow_raw_dmx and self.enabled(RAW_DMX)
        ):
            raws = theatre.doc.raw_dmx()
            self._universes.update(tr.universe for tr in raws)
        else:
            raws = []
        buffers = self._buffers()
        if raws and theatre is not None:
            t = self._eval_time(theatre, 0.0)
            if t is not None:
                master = self.master(RAW_DMX)
                for tr in raws:
                    value = round(255 * tr.value_at(t) * master)
                    buf = buffers[tr.universe]
                    if value > buf[tr.channel - 1]:
                        buf[tr.channel - 1] = value
        levels: dict[str, float] = {}
        for it in self.instances:
            fx = it.inst.fixture
            show = shows.get(it.inst.platform or THEATRE)
            requested, rate_hz, position, cue_start = 0.0, 0.0, 0.0, -1.0
            if show is not None and show.active and show.doc is not None and self.enabled(fx.effect):
                if it.run_id != show.run_id or show.position_s < it.last_position - 1.0:
                    it.limiter.reset_show()          # new run or loop restart
                it.run_id, it.last_position = show.run_id, show.position_s
                t = self._eval_time(show, fx.lead_s)
                if t is not None:
                    position = t
                    requested = show.doc.level(fx.effect, t, fx.match or None) * self.master(fx.effect)
                    if fx.effect == "strobe":
                        rate_hz = min(show.doc.params("strobe", t).get("rate_hz", 3.0), fx.limits.max_rate_hz)
                        onset = show.doc.onset("strobe", t)
                        if onset is not None:
                            position, cue_start = t - onset, onset   # the first flash is at the start of the cue
            override = self._overrides.get(it.inst.key)
            if override is not None:
                if now < override[1] and self.test_enabled:
                    requested = max(requested, override[0])
                    if fx.effect == "strobe" and rate_hz <= 0.0:
                        rate_hz = min(3.0, fx.limits.max_rate_hz)
                        position = now
                else:
                    del self._overrides[it.inst.key]
            it.requested = requested
            level = it.limiter.step(requested, now, dt)
            it.level, it.rate_hz = level, rate_hz
            shown = level
            if fx.effect == "strobe" and fx.flash == "software":
                shown = level if self._software_flash(it, level, rate_hz, position, cue_start, now) else 0.0
            it.values = [c.render(shown, rate_hz) for c in fx.channels]
            buf = buffers[it.inst.universe]
            for c, value in zip(fx.channels, it.values, strict=True):
                buf[it.inst.address + c.offset - 1] = value
            if level > levels.get(fx.effect, 0.0):
                levels[fx.effect] = level
        self.type_levels = levels
        return {u: bytes(b) for u, b in buffers.items()}

    def _software_flash(
        self, it: _Instance, level: float, rate_hz: float, position: float, cue_start: float, now: float
    ) -> bool:
        """Whether a strobe that is flashed by this software is lit in this frame.

        Flashes are counted along the programme (`position`: time since the start of the cue), so that
        every strobe of a venue is in step. Each flash is shown once, for its length:

        * a frame that comes too late for a flash's window still shows it - a late flash, not a lost one;
        * a flash never starts sooner after the previous one than the fixture's rate ceiling allows
          (less one and a half output frames for the timing of the frames), wherever the position
          comes from: play, a seek, scrubbing in the Editor, a loop, one cue straight after another;
        * two flashes are always separated by a dark frame, and a flash ends after its length even if
          the position stands still.
        """
        fx = it.inst.fixture
        lit = False
        if level > 0.0 and rate_hz > 0.0:
            period = 1.0 / rate_hz
            on_s = min(max(fx.flash_on_s, self.frame_s), 0.5 * period)
            flash = (cue_start, int(position // period))
            since = now - it.flash_at
            if flash != it.last_flash:
                min_gap = 1.0 / fx.limits.max_rate_hz - 1.5 * self.frame_s
                if not it.flash_lit and not 0.0 <= since < min_gap:
                    lit, it.last_flash, it.flash_at = True, flash, now
            else:
                lit = it.flash_lit and (position % period) < on_s and 0.0 <= since < on_s + self.frame_s
        elif level <= 0.0:
            it.last_flash = None
        it.flash_lit = lit
        return lit

    # ------------------------------------------------------------------ reporting
    def status(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "fixtures": [
                {
                    "key": it.inst.key,
                    "effect": it.inst.fixture.effect,
                    "universe": it.inst.universe,
                    "address": it.inst.address,
                    "requested": round(it.requested, 3),
                    "level": round(it.level, 3),
                    "dmx": list(it.values),
                    "limit": it.limiter.reason or None,
                    "trips": it.limiter.trips,
                }
                for it in self.instances
            ],
            "types": {name: {"enabled": self.enabled(name), "master": self.master(name)} for name in self.patch.effects()},
        }

    def limited(self) -> list[str]:
        """Keys of the fixtures currently held off by a limit."""
        return [it.inst.key for it in self.instances if it.limiter.reason]
