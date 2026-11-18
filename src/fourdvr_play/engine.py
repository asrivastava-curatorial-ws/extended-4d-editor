"""The interactive show's engine: the live engine, with a channel of its own for every seat.

    shared source ([source]) ------------------------------.
    seat telemetry (game, per seat) -> guard -> cueing ----+--> the seat's platform (its own limits)
    reactions (blocks: "push this seat") -> pulse cueing --'
    effect levels: shared, the seat's own, reactions -> the seat's fixtures and shakers

Nothing new protects the hardware, because nothing has to: a reaction is an *acceleration pulse* that
goes through a cueing filter (it always returns to neutral), then the same soft limit and the same
platform runtime - limiter, kinematics, actuator guard, link supervision - as a programme or a live
drive. A block can therefore ask for anything; the seat does what its platform allows.

Reactions happen only while the session runs and is not held; otherwise they fade out and new ones
are left out (counted as "suppressed").
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

import numpy as np

from fourdvr_fx.bridge import BridgeOutput
from fourdvr_fx.dmx import DmxOutput
from fourdvr_fx.engine import THEATRE, ShowState
from fourdvr_live.cueing import LiveCueing, LiveCueingSettings, soft_limit
from fourdvr_live.effects import LiveDocument, RuleEngine
from fourdvr_live.engine import LiveEngine, LivePoseSource
from fourdvr_live.filters import smootherstep
from fourdvr_live.safety import AxisMap, InputGuard
from fourdvr_live.sources.base import LiveSource, SourceInfo
from fourdvr_live.sources.openformat import OpenDecoder
from fourdvr_motion.drivers import ActuatorDriver
from fourdvr_motion.platform_config import PlatformConfig

from .config import MOTION, PlayConfig, SeatConfig
from .logic import Limits, Reaction

GATE_IN_S = 0.5              # reactions come in this fast when a session starts (they are short themselves)
MAX_PULSES = 12              # pushes in force on one seat at a time; more are left out
NONE = SourceInfo("none", "No shared source", "only what the seats and the blocks do", "any", setup="nothing")


class IdleSource(LiveSource):
    """``[source]`` left out: nothing is shared; the seats' own channels and the blocks do everything."""

    info = NONE

    def run(self) -> None:
        self._stop.wait()


def kick_settings(base: LiveCueingSettings, limits: Limits) -> LiveCueingSettings:
    """The cueing of the reaction channel: what a block asks for is what the seat tries to show (no
    scaling down as for a racing car's g), it comes back quickly and it never leans the seat for long."""
    return LiveCueingSettings(
        gain=base.gain, axis_gains=dict(base.axis_gains), accel_scale=1.0, peak_specific_force=limits.max_accel,
        rotation_gain=1.0, peak_rate=limits.max_rate, input_lp_hz=12.0, translation_hp_hz=0.8,
        translation_washout_hz=0.25, rotation_hp_hz=0.5, yaw_hp_hz=0.5, tilt_coordination=False, comfort=base.comfort,
    )


class _Pulse:
    __slots__ = ("accel", "rates", "seconds", "start")

    def __init__(self, start: float, reaction: Reaction) -> None:
        self.start = start
        self.seconds = max(reaction.seconds, 1e-3)
        self.accel = np.asarray(reaction.accel, dtype=np.float64)
        self.rates = np.asarray(reaction.rates, dtype=np.float64)

    def weight(self, t: float) -> float | None:
        """0..1 (a smooth bump: no step at either end); None once it is over."""
        u = (t - self.start) / self.seconds
        if u >= 1.0:
            return None
        return math.sin(math.pi * u) ** 2 if u > 0.0 else 0.0


class SeatChannel:
    """One seat: its own telemetry, the pushes it was given, its effect levels."""

    def __init__(self, seat: SeatConfig, rate_hz: float, cfg: PlayConfig, feed: Any) -> None:
        live = cfg.live
        self.id = seat.id
        self.platform = seat.platform
        self.guard = InputGuard(replace(live.safety), AxisMap(live.axes))
        self.cueing = LiveCueing(rate_hz, replace(live.cueing, axis_gains=dict(live.cueing.axis_gains)))
        self.kick = LiveCueing(rate_hz, kick_settings(live.cueing, cfg.limits))
        self.rules = RuleEngine(live.rules)
        self.doc = LiveDocument()
        self.doc.feed = feed
        self.decoder = OpenDecoder()
        self.lock = threading.Lock()
        self.enabled = True
        self.scale = MOTION[seat.motion]
        self.blocked = frozenset(seat.blocked)
        self.pulses: list[_Pulse] = []
        self.held: dict[str, tuple[float, float]] = {}       # effect -> (level, until)
        self.levels: dict[str, float] = {}
        self.reactions = 0
        self.left_out = 0
        self._pose = (np.zeros(6), np.zeros(6))
        self._times = (0.0, 0.0)

    def offer(self, msg: Any, now: float) -> bool:
        """A telemetry message of this seat (the open live format, as an object)."""
        with self.lock:
            sample = self.decoder.feed_message(msg, now)
        return sample is not None and self.guard.offer(sample, now)

    def push(self, reaction: Reaction, now: float) -> None:
        self.reactions += 1
        if reaction.moves:
            if len(self.pulses) >= MAX_PULSES:
                self.left_out += 1
            else:
                self.pulses.append(_Pulse(now, reaction))
        for name, level in reaction.effects.items():
            if level > 0.0:
                old = self.held.get(name, (0.0, 0.0))
                self.held[name] = (max(level, old[0] if old[1] > now else 0.0), max(old[1], now + reaction.effect_seconds))

    def step(self, t: float, dt: float, gate: float, gain: float, shared: np.ndarray, shared_levels: dict[str, float],
             shared_params: dict[str, dict[str, float]], position_s: float) -> None:
        sample, envelope, reset = self.guard.step(t)
        if reset:
            self.cueing.reset()
            self.rules.reset()
        self.cueing.s.gain = self.kick.s.gain = gain
        if sample is not None:
            own = self.cueing.step(np.asarray(sample.accel), np.asarray(sample.rates)) * envelope
        else:
            own = self.cueing.step(np.zeros(3), np.zeros(3)) * envelope
        accel, rates = np.zeros(3), np.zeros(3)
        if self.pulses:
            alive: list[_Pulse] = []
            for pulse in self.pulses:
                w = pulse.weight(t)
                if w is not None:
                    alive.append(pulse)
                    accel += pulse.accel * w
                    rates += pulse.rates * w
            self.pulses = alive
        kick = self.kick.step(accel, rates) * gate
        pose = (shared + own + kick) * (self.scale if self.enabled else 0.0)
        self._pose = (self._pose[1], pose)
        self._times = (t, t + dt)

        levels = dict(shared_levels)
        for name, level in self.rules.step(sample, envelope, t, dt).items():
            levels[name] = max(levels.get(name, 0.0), level)
        params = {**shared_params, **self.rules.params}
        for name in [n for n, (_level, until) in self.held.items() if t >= until]:
            del self.held[name]
        for name, (level, _until) in self.held.items():
            levels[name] = max(levels.get(name, 0.0), round(level * gate, 4))
        if "strobe" in levels:
            params.setdefault("strobe", {}).setdefault("rate_hz", 3.0)
        levels = {k: v for k, v in levels.items() if v > 0.0 and k not in self.blocked} if self.enabled else {}
        self.levels = levels
        self.doc.set(levels, params, position_s)

    def pose_at(self, t: float) -> np.ndarray:
        (p0, p1), (t0, t1) = self._pose, self._times
        if t >= t1 or t1 <= t0:
            return p1
        if t <= t0:
            return p0
        return p0 + (p1 - p0) * ((t - t0) / (t1 - t0))

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "platform": self.platform or None, "telemetry": self.guard.state,
                "envelope": round(self.guard.envelope, 3), "enabled": self.enabled, "motion": self.scale,
                "levels": dict(self.levels), "pushes": len(self.pulses), "reactions": self.reactions,
                "left_out": self.left_out, "rejected": self.guard.rejected + self.decoder.invalid,
                "pose": [round(float(v), 5) for v in self._pose[1]]}


class SeatPoseSource(LivePoseSource):
    """What a platform follows in an interactive show: its seat's pose (see LivePoseSource)."""

    name = "play"

    def __init__(self, engine: PlayEngine, cfg: PlatformConfig, lookahead: float = 0.0) -> None:
        super().__init__(engine, cfg, lookahead)
        self.play = engine
        self.platform_id = cfg.platform_id

    def pose(self, t: float) -> np.ndarray:
        offset = self.play.pose_for(self.platform_id, t - self.lookahead)
        return self.neutral + np.where(offset >= 0.0, soft_limit(offset, self.up), soft_limit(offset, self.down))


class PlayEngine(LiveEngine):
    def __init__(self, cfg: PlayConfig, source: LiveSource | None = None, drivers: dict[str, ActuatorDriver] | None = None,
                 effects_output: DmxOutput | None = None, audio_backend: str = "", effects: bool = True) -> None:
        self.play = cfg
        self.channels: dict[str, SeatChannel] = {}
        self.by_platform: dict[str, SeatChannel] = {}
        self.house_doc = LiveDocument()
        self.house: dict[str, tuple[float, float]] = {}
        self.house_levels: dict[str, float] = {}
        self._inbox: deque[tuple[tuple[str, ...], Reaction, bool]] = deque()
        self.live_on = False
        self.gate = 0.0
        self._gate_ramp = 0.0
        self.suppressed = 0
        self.reactions = 0
        if source is None and str(cfg.live.source.get("type", "")) == "none":
            source = IdleSource()
        super().__init__(cfg.live, source, drivers, effects_output, audio_backend, effects)
        self.house_doc.feed = self.doc.feed
        for seat in cfg.seats:
            channel = SeatChannel(seat, self.rate, cfg, self.doc.feed)
            self.channels[seat.id] = channel
            if seat.platform:
                self.by_platform[seat.platform] = channel
        for rt in self.runtimes:
            rt.live_source = SeatPoseSource(self, rt.cfg, rt.pipeline.lookahead)
        # the buttons of a seat's board have changed: (platform id, buttons pressed as a bit mask). The hub
        # sets this; it is called from the effects service's thread.
        self.on_seat_inputs: Callable[[str, int], None] | None = None
        if self.service is not None and isinstance(self.service.output, BridgeOutput):
            self.service.output.on_input = self._board_inputs

    def _board_inputs(self, platform: str | None, bits: int) -> None:
        handler = self.on_seat_inputs
        if handler is not None and platform is not None:
            handler(platform, bits)

    @property
    def seat_boards(self) -> bool:
        """True if the seats' boards are reached over the effects link, so that their buttons can be read."""
        return self.service is not None and isinstance(self.service.output, BridgeOutput)

    # ================================================================== from the hub (any thread)
    def set_live(self, on: bool) -> None:
        """The session runs (reactions allowed) or does not."""
        self.live_on = bool(on)

    def react(self, seats: Iterable[str], reaction: Reaction, house: bool = False) -> None:
        self._inbox.append((tuple(seats), reaction, house))

    def offer(self, seat: str, msg: Any, now: float) -> bool:
        channel = self.channels.get(seat)
        return channel is not None and channel.offer(msg, now)

    def configure(self, seat: str, enabled: bool, motion: str, blocked: Iterable[str]) -> None:
        channel = self.channels.get(seat)
        if channel is not None:
            channel.enabled = bool(enabled)
            channel.scale = MOTION.get(motion, 1.0)
            channel.blocked = frozenset(blocked)

    # ================================================================== control thread
    def pose_for(self, platform_id: str, t: float) -> np.ndarray:
        channel = self.by_platform.get(platform_id)
        if channel is not None:
            return channel.pose_at(t)
        return self.pose_at(t) if self.play.shared_motion else np.zeros(6)

    def tick(self, t: float) -> None:
        super().tick(t)
        dt = self.dt
        allowed = self.live_on and not self.guard.hold
        if allowed:
            self._gate_ramp = min(1.0, self._gate_ramp + dt / GATE_IN_S)
        else:
            self._gate_ramp = max(0.0, self._gate_ramp - dt / self.cfg.safety.fade_out_s)
        self.gate = smootherstep(self._gate_ramp)
        while self._inbox:
            seats, reaction, house = self._inbox.popleft()
            if not allowed:
                self.suppressed += 1
                continue
            self.reactions += 1
            for seat in seats:
                channel = self.channels.get(seat)
                if channel is not None and channel.enabled:
                    channel.push(reaction, t)
            if house:
                for name, level in reaction.effects.items():
                    old = self.house.get(name, (0.0, 0.0))
                    self.house[name] = (max(level, old[0] if old[1] > t else 0.0), max(old[1], t + reaction.effect_seconds))
        shared = self._pose[1] if self.play.shared_motion else np.zeros(6)
        position = t - (self._t0 if self._t0 is not None else t)
        levels, params = self._levels, self.rules.params
        for channel in self.channels.values():
            channel.step(t, dt, self.gate, self.cueing.s.gain, shared, levels, params, position)
        room = dict(levels)
        for name in [n for n, (_level, until) in self.house.items() if t >= until]:
            del self.house[name]
        for name, (level, _until) in self.house.items():
            room[name] = max(room.get(name, 0.0), round(level * self.gate, 4))
        room = {k: v for k, v in room.items() if v > 0.0}
        room_params = dict(params)
        if "strobe" in room:
            room_params.setdefault("strobe", {}).setdefault("rate_hz", 3.0)
        self.house_levels = room
        self.house_doc.set(room, room_params, position)

    # ================================================================== effects service (its own thread)
    def _states(self, now: float) -> dict[str, ShowState]:
        position = max(0.0, now - (self._t0 if self._t0 is not None else now))
        run = int(position // (self.cfg.rules.show_minutes * 60.0))
        moving = self.guard.envelope > 0.0 or self.gate > 0.0
        states: dict[str, ShowState] = {}
        any_following = not self.runtimes
        for rt in self.runtimes:
            following = rt.following
            any_following = any_following or following
            channel = self.by_platform.get(rt.cfg.platform_id)
            on = moving or (channel is not None and channel.guard.envelope > 0.0)
            states[rt.cfg.platform_id] = ShowState(on and following, channel.doc if channel is not None else self.doc,
                                                   position, 0.0, False, run)
        states[THEATRE] = ShowState(moving and any_following, self.house_doc, position, 0.0, False, run)
        return states

    # ================================================================== lifecycle, status
    def stop(self, settle_s: float | None = None) -> None:
        self.live_on = False
        super().stop(settle_s)

    def status(self) -> dict[str, Any]:
        st = super().status()
        st["play"] = {"live": self.live_on, "gate": round(self.gate, 3), "reactions": self.reactions,
                      "suppressed": self.suppressed, "room": dict(self.house_levels),
                      "seats": [c.status() for c in self.channels.values()]}
        return st
