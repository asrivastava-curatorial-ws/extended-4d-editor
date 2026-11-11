"""Per-platform runtime: timeline execution, fault handling and the real-time control loop."""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from . import __version__
from .clocksync import ClockSync
from .content_cache import MotionCache
from .drivers import ActuatorCommand, ActuatorDriver, LinkStatus, create_driver
from .esp32_protocol import CommandState
from .interpolation import MotionTrack
from .pipeline import PlatformPipeline
from .platform_config import PlatformConfig
from .protocol import Timeline, TimelineState
from .sources import BlendSource, ConstantSource, TrajectorySource

log = logging.getLogger(__name__)


class Phase(StrEnum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    SCHEDULED = "SCHEDULED"
    ENGAGING = "ENGAGING"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"


REPORTED_STATE = {
    Phase.STARTING: "READY",
    Phase.IDLE: "READY",
    Phase.SCHEDULED: "READY",
    Phase.ENGAGING: "RUNNING",
    Phase.PLAYING: "RUNNING",
    Phase.PAUSED: "RUNNING",
    Phase.STOPPING: "STOPPED",
    Phase.STOPPED: "STOPPED",
    Phase.FAULT: "ERROR",
}


@dataclass
class LoadedProgramme:
    programme_id: int
    sha256: str
    track: MotionTrack | None  # None: programme without motion (platform stays neutral)
    duration_s: float


class PlatformRuntime:
    def __init__(
        self,
        cfg: PlatformConfig,
        control_rate_hz: float,
        cache: MotionCache,
        latency_compensation_s: float = 0.0,
        server_loss_timeout_s: float = 10.0,
        driver: ActuatorDriver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.mono = clock
        self.rate = control_rate_hz
        self.cache = cache
        self.clock = ClockSync()
        self.pipeline = PlatformPipeline(cfg, control_rate_hz, latency_compensation_s)
        self.driver = driver or create_driver(cfg)
        self.server_loss_timeout_s = server_loss_timeout_s
        self.phase = Phase.STARTING
        self.lock = threading.Lock()
        self._posted: list[Callable[[float], None]] = []
        self._events: list[tuple[float, int, Callable[[float], None]]] = []
        self._counter = itertools.count()
        self.programmes: dict[int, LoadedProgramme] = {}
        self.timeline = Timeline()
        self.applied_timeline_id = 0
        self.trajectory: TrajectorySource | None = None
        self.server_connected = False
        self._server_lost_at: float | None = clock()
        self._ever_connected = False
        self.errors: deque[str] = deque(maxlen=10)
        self.last_request_id: str | None = None
        self.fault_reason = ""
        self._reset_ticks = 0
        self._link_ok_since: float | None = None
        self.link: LinkStatus | None = None
        self.loop_overruns = 0
        self.ticks = 0
        self._master = 0.0
        self._started = clock()
        self._fallback_offset = 0.0
        self.run_counter = 0  # incremented by every PLAY / RESTART (a new show run)
        # optional hooks set by the application; never required for motion
        self.on_programme: Callable[[LoadedProgramme], None] | None = None
        self.effects_status: Callable[[PlatformRuntime], dict[str, Any] | None] | None = None

    # ================================================== called from the network (asyncio) thread
    def post(self, action: Callable[[float], None]) -> None:
        with self.lock:
            self._posted.append(action)

    def add_error(self, text: str) -> None:
        with self.lock:
            self.errors.append(f"{time.strftime('%H:%M:%S')} {text}"[:300])

    def on_server_connected(self) -> None:
        with self.lock:
            self.server_connected = True
            self._server_lost_at = None
            self._ever_connected = True

    def on_server_disconnected(self) -> None:
        with self.lock:
            if self.server_connected:
                self._server_lost_at = self.mono()
            self.server_connected = False

    def register_programme(self, programme_id: int, sha256: str, duration_s: float) -> LoadedProgramme:
        """Load (from the local cache) the motion profile of a programme; raises on invalid data."""
        if sha256:
            track, mf = self.cache.load(sha256)
            if mf.programme_id != programme_id:
                raise ValueError(f"motion file is for programme {mf.programme_id}, not {programme_id}")
            problems = self._profile_problems(mf.samples)
            if problems:
                self.add_error(f"programme {programme_id}: {problems} (runtime limits will clip)")
            loaded = LoadedProgramme(programme_id, sha256, track, mf.duration_s)
        else:
            loaded = LoadedProgramme(programme_id, "", None, duration_s)
        with self.lock:
            self.programmes[programme_id] = loaded
        hook = self.on_programme
        if hook is not None:
            try:
                hook(loaded)
            except Exception as exc:  # noqa: BLE001 - optional extras must never fail a PREPARE
                self.add_error(f"programme {programme_id}: effects not prepared: {exc}")
        return loaded

    def _profile_problems(self, samples: np.ndarray) -> str:
        lim = self.cfg.limits
        over = (samples < lim.position_min - 1e-6) | (samples > lim.position_max + 1e-6)
        if np.any(over):
            axes = sorted({int(i) for i in np.nonzero(over)[1]})
            return "profile exceeds platform position limits on axes " + ",".join(str(a) for a in axes)
        return ""

    # timeline commands (scheduled on master-clock instants, executed by the control thread)
    def cmd_play(self, programme_id: int, timeline_id: int, start_at: float, duration_s: float, loop: bool) -> None:
        def arm(_t: float) -> None:
            if timeline_id < self.applied_timeline_id:
                return
            self.applied_timeline_id = timeline_id
            self.run_counter += 1
            self.timeline = Timeline(
                timeline_id=timeline_id,
                state=TimelineState.PLAYING,
                programme_id=programme_id,
                epoch=start_at,
                start_at=start_at,
                duration_s=duration_s,
                loop=loop,
            )
            if self.phase != Phase.FAULT:
                self.phase = Phase.SCHEDULED
            self._schedule(
                start_at, lambda t: self._start_trajectory(programme_id, timeline_id, start_at, duration_s, loop, t)
            )

        self.post(arm)

    def cmd_pause(self, programme_id: int, timeline_id: int, pause_at: float, position_s: float) -> None:
        def arm(_t: float) -> None:
            if timeline_id < self.applied_timeline_id:
                return
            self.applied_timeline_id = timeline_id
            self.timeline = Timeline(
                timeline_id=timeline_id,
                state=TimelineState.PAUSED,
                programme_id=programme_id,
                position_s=position_s,
                duration_s=self.timeline.duration_s,
                loop=self.timeline.loop,
            )
            self._schedule(pause_at, lambda t: self._pause(programme_id, position_s, t))

        self.post(arm)

    def cmd_resume(self, programme_id: int, timeline_id: int, resume_at: float, position_s: float) -> None:
        def arm(_t: float) -> None:
            if timeline_id < self.applied_timeline_id:
                return
            self.applied_timeline_id = timeline_id
            duration, loop = self.timeline.duration_s, self.timeline.loop
            prog = self.programmes.get(programme_id)
            if prog is not None:
                duration = prog.duration_s
            epoch = resume_at - position_s
            self.timeline = Timeline(
                timeline_id=timeline_id,
                state=TimelineState.PLAYING,
                programme_id=programme_id,
                epoch=epoch,
                start_at=resume_at,
                duration_s=duration,
                loop=loop,
            )
            self._schedule(
                resume_at, lambda t: self._start_trajectory(programme_id, timeline_id, epoch, duration, loop, t)
            )

        self.post(arm)

    def cmd_stop(self, timeline_id: int, stop_at: float, operator_ack: bool = True) -> None:
        def arm(_t: float) -> None:
            if timeline_id < self.applied_timeline_id:
                return
            self.applied_timeline_id = timeline_id
            self.timeline = Timeline(
                timeline_id=timeline_id,
                state=TimelineState.STOPPED,
                programme_id=self.timeline.programme_id,
                duration_s=self.timeline.duration_s,
            )
            if operator_ack:
                self._request_fault_reset()
            self._schedule(stop_at, self._stop)

        self.post(arm)

    def cmd_sync(self, timeline: Timeline, force: bool) -> None:
        def arm(t: float) -> None:
            if timeline.timeline_id < self.applied_timeline_id:
                return
            if force:
                self._request_fault_reset()
            same = timeline.timeline_id == self.applied_timeline_id and not force
            self.applied_timeline_id = timeline.timeline_id
            self.timeline = timeline
            if same and self.phase not in (Phase.STARTING, Phase.FAULT):
                return
            self._apply_timeline(t)

        self.post(arm)

    # ======================================================== control thread (single owner)
    def _schedule(self, when: float, action: Callable[[float], None]) -> None:
        heapq.heappush(self._events, (when, next(self._counter), action))

    def _master_now(self, now_local: float) -> float:
        if self.clock.synced:
            return self.clock.master_time(now_local)
        return now_local + self._fallback_offset

    def tick(self, now_local: float) -> None:
        self.ticks += 1
        t = self._master_now(now_local)
        self._master = t
        with self.lock:
            posted, self._posted = self._posted, []
        if self.clock.synced:  # timeline actions need a synchronised clock
            for action in posted:
                action(t)
            while self._events and self._events[0][0] <= t:
                _when, _n, action = heapq.heappop(self._events)
                action(t)
        else:
            with self.lock:
                self._posted = posted + self._posted

        link = self.driver.poll(now_local)
        self.link = link
        if not self.pipeline.initialised:
            self._initialise(link, t)
            return

        self._supervise(link, now_local, t)
        strokes = self.pipeline.step(t)
        if self.phase == Phase.FAULT:
            state = CommandState.PARK
        elif self._reset_ticks > 0:
            state = CommandState.FAULT_RESET
            self._reset_ticks -= 1
        else:
            state = CommandState.RUN
        self.driver.send(ActuatorCommand(state, strokes, t))
        self._advance_phase(t)

    def _initialise(self, link: LinkStatus, t: float) -> None:
        """Wait for the first status from the actuator controller, then start from its reported pose."""
        if link.positions_m is not None:
            try:
                pose = self.pipeline.pose_from_strokes(link.positions_m)
            except ValueError:
                pose = self.cfg.calibration.park_pose.copy()
                self.add_error("could not solve forward kinematics from reported positions; assuming park pose")
            self.pipeline.reset(pose)
            self.phase = Phase.IDLE
            log.info("platform link up", extra={"platform": self.cfg.platform_id, "pose": np.round(pose, 4).tolist()})
            if link.faults:
                self._request_fault_reset()
            return
        self.driver.send(ActuatorCommand(CommandState.IDLE, self.pipeline.strokes, t))
        if self.mono() - self._started > 3.0 and self.phase != Phase.FAULT:
            self._enter_fault("no reply from actuator controller")

    def _supervise(self, link: LinkStatus, now_local: float, t: float) -> None:
        healthy = link.connected and not link.faults
        if healthy:
            if self._link_ok_since is None:
                self._link_ok_since = now_local
        else:
            self._link_ok_since = None
        if self.phase != Phase.FAULT and not healthy and not (self._reset_ticks > 0 and link.connected):
            if not link.connected:
                self._enter_fault("communication with actuator controller lost")
            elif link.faults:
                self._enter_fault("actuator controller fault: " + ",".join(link.faults))
        if self.phase == Phase.FAULT and healthy and self.cfg.motion.fault_recovery == "auto":
            if self._link_ok_since is not None and now_local - self._link_ok_since > 2.0:
                self._clear_fault(t)
        # server connection supervision
        lost_at = self._server_lost_at
        if (
            not self.server_connected
            and self._ever_connected
            and lost_at is not None
            and now_local - lost_at > self.server_loss_timeout_s
            and self.phase in (Phase.ENGAGING, Phase.PLAYING, Phase.PAUSED, Phase.SCHEDULED)
        ):
            self.add_error(f"server unreachable for {self.server_loss_timeout_s:.0f}s: returning to neutral")
            self._events.clear()
            self._stop(t)

    def _enter_fault(self, reason: str) -> None:
        log.error("platform fault", extra={"platform": self.cfg.platform_id, "reason": reason})
        self.fault_reason = reason
        self.add_error(reason)
        self.phase = Phase.FAULT
        self.trajectory = None

    def _request_fault_reset(self) -> None:
        if self.phase == Phase.FAULT or (self.link is not None and self.link.faults):
            self._reset_ticks = max(self._reset_ticks, 3)
            if self.phase == Phase.FAULT and self.link is not None and self.link.connected:
                self._clear_fault(self._master)

    def _clear_fault(self, t: float) -> None:
        link = self.link
        if link is None or link.positions_m is None:
            return
        try:
            pose = self.pipeline.pose_from_strokes(link.positions_m)
        except ValueError:
            pose = self.cfg.calibration.park_pose.copy()
        self.pipeline.reset(pose)
        self._reset_ticks = max(self._reset_ticks, 3)
        self.phase = Phase.STOPPED
        self.fault_reason = ""
        log.info("platform fault cleared", extra={"platform": self.cfg.platform_id})

    def _apply_timeline(self, t: float) -> None:
        tl = self.timeline
        if tl.state == TimelineState.PLAYING and tl.programme_id is not None and tl.epoch is not None:
            if tl.epoch > t:
                if self.phase != Phase.FAULT:
                    self.phase = Phase.SCHEDULED
                epoch = tl.epoch
                self._schedule(
                    epoch,
                    lambda tt: self._start_trajectory(
                        tl.programme_id or 0, tl.timeline_id, epoch, tl.duration_s, tl.loop, tt
                    ),
                )
            else:
                self._start_trajectory(tl.programme_id, tl.timeline_id, tl.epoch, tl.duration_s, tl.loop, t)
        elif tl.state == TimelineState.PAUSED and tl.programme_id is not None:
            self._pause(tl.programme_id, tl.position_s, t)
        elif tl.state in (TimelineState.STOPPED, TimelineState.IDLE) and self.phase not in (
            Phase.IDLE,
            Phase.STOPPED,
            Phase.STOPPING,
            Phase.STARTING,
        ):
            self._stop(t)

    def _start_trajectory(
        self, programme_id: int, timeline_id: int, epoch: float, duration: float, loop: bool, t: float
    ) -> None:
        if timeline_id < self.applied_timeline_id or self.phase == Phase.FAULT or not self.pipeline.initialised:
            return
        prog = self.programmes.get(programme_id)
        if prog is None:
            self.add_error(f"programme {programme_id} not prepared; staying at neutral")
            self.pipeline.transition(self.pipeline.neutral, t, gentle=True)
            self.phase = Phase.STOPPING
            return
        if prog.track is None:
            self.trajectory = None
            self.pipeline.transition(self.pipeline.neutral, t, gentle=True)
            self.phase = Phase.PLAYING
            return
        source = TrajectorySource(prog.track, epoch, duration or prog.duration_s, loop, programme_id, timeline_id)
        self.trajectory = source
        late = t - epoch > 0.25
        self.pipeline.transition(source, t, gentle=late)
        self.phase = Phase.ENGAGING

    def _pause(self, programme_id: int, position_s: float, t: float) -> None:
        if self.phase == Phase.FAULT or not self.pipeline.initialised:
            return
        prog = self.programmes.get(programme_id)
        if self.cfg.motion.pause_behaviour == "neutral" or prog is None or prog.track is None:
            hold = self.pipeline.neutral
        else:
            hold = ConstantSource(prog.track.sample(position_s), "pause-hold")
        self.pipeline.transition(hold, t, gentle=False, minimum=0.8)
        self.trajectory = None
        self.phase = Phase.PAUSED

    def _stop(self, t: float) -> None:
        if not self.pipeline.initialised:
            return
        self.trajectory = None
        if self.phase == Phase.FAULT:
            return
        self.pipeline.transition(self.pipeline.neutral, t, gentle=True)
        self.phase = Phase.STOPPING

    def _advance_phase(self, t: float) -> None:
        in_blend = isinstance(self.pipeline.selector.current, BlendSource)
        if self.phase == Phase.ENGAGING and not in_blend:
            self.phase = Phase.PLAYING
        elif self.phase == Phase.STOPPING and not in_blend:
            self.phase = Phase.STOPPED

    # ================================================================== effects (read-only view)
    def show_position(self, now_local: float) -> tuple[bool, LoadedProgramme | None, float, float, bool]:
        """(playing, programme, position_s, duration_s, loop) for optional effects playback.

        `playing` is True only while the platform really follows the programme timeline (ENGAGING or
        PLAYING, before the end of a non-looping programme): never while scheduled, paused, stopping,
        stopped or in fault. Safe to call from another thread; it only reads.
        """
        tl = self.timeline
        phase = self.phase
        if phase not in (Phase.ENGAGING, Phase.PLAYING) or tl.state != TimelineState.PLAYING or tl.programme_id is None:
            return False, None, 0.0, 0.0, False
        prog = self.programmes.get(tl.programme_id)
        duration = tl.duration_s or (prog.duration_s if prog is not None else 0.0)
        position = tl.position_at(self._master_now(now_local))
        playing = position >= 0.0 and (tl.loop or duration <= 0.0 or position < duration)
        return playing, prog, position, duration, tl.loop

    # ================================================================== reporting
    def expected_position(self, t: float) -> float | None:
        tl = self.timeline
        if tl.programme_id is None or tl.state in (TimelineState.IDLE, TimelineState.STOPPED):
            return None
        return tl.position_at(t)

    def status(self) -> dict[str, Any]:
        t = self._master
        expected = self.expected_position(t)
        traj = self.trajectory
        position = (
            traj.position(t) if traj is not None else (self.timeline.position_s if self.phase == Phase.PAUSED else None)
        )
        drift = None
        if (
            traj is not None
            and expected is not None
            and expected >= 0
            and traj.timeline_id == self.timeline.timeline_id
        ):
            drift = position - expected if position is not None else None
            if drift is not None and traj.duration > 0:  # wrap-aware difference
                drift = (drift + traj.duration / 2) % traj.duration - traj.duration / 2
        link = self.link
        pipe = self.pipeline
        with self.lock:
            errors = list(self.errors)
        st: dict[str, Any] = {
            "state": REPORTED_STATE[self.phase],
            "phase": str(self.phase),
            "programme_id": self.timeline.programme_id,
            "timeline_id": self.timeline.timeline_id,
            "position_s": None if position is None else round(position, 4),
            "expected_position_s": None if expected is None else round(expected, 4),
            "drift_s": None if drift is None else round(drift, 5),
            "clock_offset_s": round(self.clock.offset, 6) if self.clock.synced else None,
            "rtt_s": round(self.clock.rtt, 6) if self.clock.synced else None,
            "clock_drift_ppm": round(self.clock.drift * 1e6, 2),
            "pose": [round(float(v), 5) for v in pipe.pose],
            "target_pose": [round(float(v), 5) for v in pipe.target],
            "actuator_lengths_m": [round(float(v), 5) for v in pipe.lengths],
            "stroke_utilisation": round(pipe.stroke_utilisation(), 3),
            "link": None if link is None else link.as_dict(),
            "limit_events": self.pipeline.counters.as_dict(),
            "driver": self.driver.kind,
            "control_rate_hz": self.rate,
            "loop_overruns": self.loop_overruns,
            "fault": self.fault_reason or None,
            "server_connected": self.server_connected,
            "errors": errors,
            "last_request_id": self.last_request_id,
            "app_version": __version__,
            "profile": self.cfg.to_summary(),
        }
        effects = self._effects_report()
        if effects is not None:   # only present when the controller has an effects patch
            st["effects"] = effects
        return st

    def _effects_report(self) -> dict[str, Any] | None:
        hook = self.effects_status
        if hook is None:
            return None  # effects not configured: motion-only controller
        try:
            return hook(self)
        except Exception as exc:  # noqa: BLE001 - reporting extras must never break STATUS
            return {"error": f"{type(exc).__name__}: {exc}"}


class ControlLoop:
    """Real-time loop thread running every platform runtime at the configured rate."""

    def __init__(self, runtimes: list[PlatformRuntime], rate_hz: float) -> None:
        self.runtimes = runtimes
        self.period = 1.0 / rate_hz
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="motion-control-loop", daemon=True)
        self.max_tick_s = 0.0

    def start(self) -> None:
        for rt in self.runtimes:
            rt.driver.open()
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
        for rt in self.runtimes:
            rt.driver.close()

    def _run(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            # Tick at the *scheduled* instant, not at the (jittered) wake-up time: the motion track is
            # then sampled on a uniform grid, as in the offline validation. Wake-up latency (normally
            # well below one period) only delays the command; it no longer shows up as velocity/jerk
            # noise that the limiter would have to clamp.
            tick_t = next_t
            started = time.monotonic()
            for rt in self.runtimes:
                try:
                    rt.tick(tick_t)
                except Exception as exc:
                    log.exception("control tick failed", extra={"platform": rt.cfg.platform_id})
                    rt.add_error(f"internal error: {exc}")
            elapsed = time.monotonic() - started
            self.max_tick_s = max(self.max_tick_s, elapsed)
            next_t += self.period
            delay = next_t - time.monotonic()
            if delay < -self.period:
                for rt in self.runtimes:
                    rt.loop_overruns += 1
                next_t = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)
