"""The live show: one source in; platforms, effects and bass out.

Per control period (the same 50-200 Hz loop that plays a programme):

    source -> InputGuard (checks, stall watchdog, fades) -> LiveCueing -> pose offset
                                                         -> RuleEngine -> effect levels -> LiveDocument
                                                         -> SynthFeed  -> shaker sound

Each platform is a :class:`LiveRuntime` - the show service's own platform runtime (link supervision,
fault handling, limiter, kinematics, actuator guard) that follows the live pose instead of a
programme's trajectory. The effects go through the unchanged effects service with the venue patch.
No Control Server is involved: this computer's clock is the master clock.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from fourdvr_fx.dmx import DmxOutput
from fourdvr_fx.engine import THEATRE, ShowState
from fourdvr_fx.patch import AudioConfig, OutputConfig, Patch
from fourdvr_fx.service import EffectsService
from fourdvr_motion.content_cache import MotionCache
from fourdvr_motion.drivers import ActuatorDriver
from fourdvr_motion.effects_link import link_hosts
from fourdvr_motion.platform_config import PlatformConfig
from fourdvr_motion.runtime import Phase, PlatformRuntime
from fourdvr_motion.sources import PoseSource

from .config import LiveConfig
from .cueing import LiveCueing, LiveCueingSettings, soft_limit
from .effects import LiveDocument, RuleEngine
from .safety import AxisMap, InputGuard
from .sample import AXES, LiveSample, describe
from .session import SUFFIX, SessionRecorder
from .shaker import MixFeed, SynthFeed
from .sources import LiveSource, create

log = logging.getLogger(__name__)
SOFT_RANGE = 0.9          # the live pose stays within this part of each axis' range; the limiter guards the rest
LATE_S = 0.05             # a control period that starts this late is counted (a board's time-out is 0.1-0.25 s)


def sound_only_patch() -> Patch:
    """No venue patch: no DMX, the shakers' sound on this computer's default sound output."""
    return Patch(output=OutputConfig(protocol="none"), audio=AudioConfig(enabled=True, vibration_channels=[1, 2]),
                 source="built-in (sound only)")


class LivePoseSource(PoseSource):
    """What a platform follows in live mode: its neutral pose plus the live cue, bent softly into the
    platform's own range so that the limiter underneath rarely has anything to do."""

    name = "live"

    def __init__(self, engine: LiveEngine, cfg: PlatformConfig, lookahead: float = 0.0) -> None:
        self.engine = engine
        self.neutral = cfg.calibration.neutral_pose.copy()
        self.up = np.maximum(cfg.limits.position_max - self.neutral, 0.0) * SOFT_RANGE
        self.down = np.maximum(self.neutral - cfg.limits.position_min, 0.0) * SOFT_RANGE
        self.lookahead = lookahead

    def pose(self, t: float) -> np.ndarray:
        offset = self.engine.pose_at(t - self.lookahead)
        return self.neutral + np.where(offset >= 0.0, soft_limit(offset, self.up), soft_limit(offset, self.down))


class LiveRuntime(PlatformRuntime):
    """A platform of the show service, following the live pose. Everything that protects the hardware
    is the parent class, unchanged; only "which pose to follow" differs."""

    def __init__(self, cfg: PlatformConfig, rate_hz: float, cache: MotionCache, latency_compensation_s: float = 0.0,
                 driver: ActuatorDriver | None = None) -> None:
        super().__init__(cfg, rate_hz, cache, latency_compensation_s=latency_compensation_s, driver=driver)
        now = self.mono()
        for _ in range(3):                    # no Control Server: this computer's clock is the master clock
            self.clock.add(now, now, now, now)
        self.live_source: PoseSource | None = None
        self.want = True                      # False after a fault until the operator engages again

    def tick(self, now_local: float) -> None:
        super().tick(now_local)
        if (self.want and self.live_source is not None and self.pipeline.initialised
                and self.phase in (Phase.IDLE, Phase.STOPPED)):
            self.pipeline.transition(self.live_source, self._master, gentle=True)
            self.run_counter += 1
            self.phase = Phase.ENGAGING

    def _enter_fault(self, reason: str) -> None:
        super()._enter_fault(reason)
        if self.cfg.motion.fault_recovery != "auto":
            self.want = False                 # like a show: after a fault the operator decides when to go on

    def engage(self) -> None:
        """The operator's "go on": acknowledge a fault and follow the live pose again."""
        def act(_t: float) -> None:
            self._request_fault_reset()
            self.want = True

        self.post(act)

    def release(self) -> None:
        """Back to neutral and stay there (shutdown)."""
        def act(t: float) -> None:
            self.want = False
            self._stop(t)

        self.post(act)

    @property
    def following(self) -> bool:
        return self.phase in (Phase.ENGAGING, Phase.PLAYING)


class LiveEngine:
    def __init__(
        self,
        cfg: LiveConfig,
        source: LiveSource | None = None,
        drivers: dict[str, ActuatorDriver] | None = None,
        effects_output: DmxOutput | None = None,
        audio_backend: str = "",
        effects: bool = True,
    ) -> None:
        cfg.check()
        self.cfg = cfg
        self.rate = cfg.rate_hz
        self.dt = 1.0 / self.rate
        self.guard = InputGuard(cfg.safety, AxisMap(cfg.axes))
        self.guard.hold = cfg.hold
        self.rules = RuleEngine(cfg.rules)
        self.doc = LiveDocument()
        self.synth = SynthFeed(cfg.shaker)
        self.source = source if source is not None else create(cfg.source)
        defaults = LiveCueingSettings()
        comfort = getattr(self.source, "suggested_comfort", None)
        if comfort is not None and cfg.cueing.comfort == defaults.comfort:
            cfg.cueing.comfort = comfort          # a picture that cannot be delayed: the fast cues are turned down
        smoothing = getattr(self.source, "suggested_input_lp_hz", None)
        if smoothing is not None and cfg.cueing.input_lp_hz == defaults.input_lp_hz:
            cfg.cueing.input_lp_hz = smoothing    # motion estimated from a picture is rougher than telemetry
        self.cueing = LiveCueing(self.rate, cfg.cueing)
        self.doc.feed = MixFeed(self.synth, getattr(self.source, "feed", None))
        cache_dir = cfg.cache_dir or Path(tempfile.gettempdir()) / "extended-4d-editor-live"
        self.cache = MotionCache(cache_dir)
        self.runtimes: list[LiveRuntime] = []
        for pc in cfg.platforms:
            rt = LiveRuntime(pc, self.rate, self.cache, cfg.latency_compensation_s, (drivers or {}).get(pc.platform_id))
            rt.live_source = LivePoseSource(self, pc, rt.pipeline.lookahead)
            self.runtimes.append(rt)
        self.service: EffectsService | None = None
        self.effects_error = ""
        patch = cfg.patch if cfg.patch is not None else (sound_only_patch() if cfg.sound else None)
        if effects and patch is not None:
            try:
                self.service = EffectsService(patch, [rt.cfg.platform_id for rt in self.runtimes], self._states,
                                              output=effects_output, audio_backend=audio_backend,
                                              hosts=link_hosts(list(self.runtimes)))
            except (ValueError, OSError) as exc:
                self.effects_error = str(exc)
                log.error("effects disabled: %s", exc)
        self.recorder: SessionRecorder | None = None
        self.max_tick_s = 0.0
        self.max_late_s = 0.0          # how late the loop has ever started a tick (a busy computer)
        self.late_ticks = 0            # ticks that started more than LATE_S late
        self._switch_interval: float | None = None
        self.ticks = 0
        self.overruns = 0
        self._t0: float | None = None
        self._pose = (np.zeros(6), np.zeros(6))        # the cue at the last tick and at the one after
        self._times = (0.0, 0.0)
        self._sample: LiveSample | None = None
        self._levels: dict[str, float] = {}
        self._delay_s: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = False

    # ================================================================== control thread
    def pose_at(self, t: float) -> np.ndarray:
        (p0, p1), (t0, t1) = self._pose, self._times
        if t >= t1 or t1 <= t0:
            return p1
        if t <= t0:
            return p0
        return p0 + (p1 - p0) * ((t - t0) / (t1 - t0))

    def tick(self, t: float) -> None:
        """One control period at (monotonic) time t: the cue for t + dt, the effect levels, the sound."""
        if self._t0 is None:
            self._t0 = t
        self.ticks += 1
        sample, envelope, reset = self.guard.step(t)
        if reset:
            self.cueing.reset()
            self.rules.reset()
        if sample is not None:
            pose = self.cueing.step(np.asarray(sample.accel), np.asarray(sample.rates)) * envelope
            if sample.captured is not None:
                self._delay_s = t - sample.captured
        else:
            pose = self.cueing.step(np.zeros(3), np.zeros(3)) * envelope
        self._pose = (self._pose[1], pose)
        self._times = (t, t + self.dt)
        levels = self.rules.step(sample, envelope, t, self.dt)
        self.doc.set(levels, self.rules.params, t - self._t0)
        self.synth.update(sample, envelope)
        self._sample, self._levels = sample, levels
        rec = self.recorder
        if rec is not None:
            rec.add(sample, envelope, pose, levels)

    def step_all(self, t: float) -> None:
        """The engine and every platform, once (the loop's body; also how tests drive the show)."""
        self.tick(t)
        for rt in self.runtimes:
            try:
                rt.tick(t)
            except Exception as exc:
                log.exception("control tick failed", extra={"platform": rt.cfg.platform_id})
                rt.add_error(f"internal error: {exc}")

    def _run(self) -> None:
        period = self.dt
        next_t = time.monotonic()
        while not self._stop.is_set():
            tick_t = next_t                       # tick at the scheduled instant (see ControlLoop._run)
            started = time.monotonic()
            late = started - tick_t
            if late > self.max_late_s:
                self.max_late_s = late
            if late > LATE_S:
                self.late_ticks += 1
            self.step_all(tick_t)
            self.max_tick_s = max(self.max_tick_s, time.monotonic() - started)
            next_t += period
            delay = next_t - time.monotonic()
            if delay < -period:
                self.overruns += 1
                next_t = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    # ================================================================== effects service (its own thread)
    def _states(self, now: float) -> dict[str, ShowState]:
        position = max(0.0, now - (self._t0 if self._t0 is not None else now))
        run = int(position // (self.cfg.rules.show_minutes * 60.0))
        moving = self.guard.envelope > 0.0
        states: dict[str, ShowState] = {}
        any_following = not self.runtimes
        for rt in self.runtimes:
            following = rt.following
            any_following = any_following or following
            states[rt.cfg.platform_id] = ShowState(moving and following, self.doc, position, 0.0, False, run)
        states[THEATRE] = ShowState(moving and any_following, self.doc, position, 0.0, False, run)
        return states

    # ================================================================== lifecycle
    def start(self) -> None:
        """Open the source, the platforms' links and the effects outputs and begin. Raises SourceError
        if the source cannot start; nothing else is opened then."""
        self.source.start(self.guard.offer)
        if self.service is not None:
            try:
                self.service.start()
            except Exception as exc:  # noqa: BLE001 - as in the show service: effects never stop motion
                self.effects_error = f"{type(exc).__name__}: {exc}"
                self.service = None
        if self._switch_interval is None:  # the control loop gets its turn sooner when other threads are busy
            self._switch_interval = sys.getswitchinterval()
            sys.setswitchinterval(min(self._switch_interval, 0.001))
        for rt in self.runtimes:           # the links last: from here on the boards expect a command every period
            rt.driver.open()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-control-loop", daemon=True)
        self._thread.start()
        self.started = True

    def stop(self, settle_s: float | None = None) -> None:
        """Fade to rest, bring the platforms to neutral, then close everything."""
        if self._thread is not None and self._thread.is_alive():
            self.guard.hold = True
            for rt in self.runtimes:
                rt.release()
            limit = settle_s if settle_s is not None else self.cfg.safety.fade_out_s + (5.0 if self.runtimes else 0.3)
            deadline = time.monotonic() + limit
            moving = (Phase.ENGAGING, Phase.PLAYING, Phase.STOPPING, Phase.PAUSED, Phase.SCHEDULED)
            while time.monotonic() < deadline:
                if self.guard.envelope <= 0.0 and not any(rt.phase in moving for rt in self.runtimes):
                    break
                time.sleep(0.02)
            self._stop.set()
            self._thread.join(timeout=2.0)
        self._thread = None
        self.source.stop()
        if self.service is not None:
            self.service.stop()
        for rt in self.runtimes:
            rt.driver.close()
        if self._switch_interval is not None:
            sys.setswitchinterval(self._switch_interval)
            self._switch_interval = None
        self.started = False

    # ================================================================== the operator
    def hold(self, on: bool = True) -> None:
        self.guard.hold = on

    def engage(self) -> None:
        """Release a hold and acknowledge platform faults."""
        self.guard.hold = False
        for rt in self.runtimes:
            rt.engage()

    def set_gain(self, gain: float) -> float:
        self.cueing.s.gain = min(2.0, max(0.0, float(gain)))
        return self.cueing.s.gain

    # ================================================================== recording
    def start_recording(self) -> None:
        info = {
            "source": {**self.source.status(), "kind": self.source.info.kind, "options": {
                k: v for k, v in self.cfg.source.items() if k != "host"}},
            "axes": dict(self.guard.axes.factors), "cueing": self.cfg.cueing.to_dict(), "rules": self.cfg.rules.to_dict(),
        }
        info["source"].pop("error", None)
        self.recorder = SessionRecorder(self.rate, info)

    def save_recording(self, path: str | Path | None = None) -> Path | None:
        """Write what has been recorded so far (the recording goes on). None when nothing is recorded."""
        rec = self.recorder
        if rec is None or len(rec) == 0:
            return None
        if path is None:
            folder = self.cfg.record_dir or Path.cwd()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{rec.started.astimezone().strftime('%Y-%m-%d_%H%M%S')}_{self.source.info.name}{SUFFIX}"
        return rec.save(path)

    def stop_recording(self, path: str | Path | None = None) -> Path | None:
        saved = self.save_recording(path)
        self.recorder = None
        return saved

    # ================================================================== reporting
    def output_problem(self) -> str:
        """What keeps an output from working, in one sentence ("" if nothing does): a venue patch that
        could not be used, sound that cannot be played, DMX that cannot be sent."""
        if self.service is None:
            return f"effects: {self.effects_error}" if self.effects_error else ""
        st = self.service.status()
        audio = st.get("audio") or {}
        if audio.get("error"):
            return f"no sound: {audio['error']}"
        last = (st.get("output") or {}).get("last_error") or st.get("last_error")
        return f"effects output: {last}" if last else ""

    def ranges(self) -> list[float] | None:
        """How far the first platform's live pose may go from neutral on each axis (m, rad) - the scale
        of the window's bars. None without a platform."""
        if not self.runtimes:
            return None
        source = self.runtimes[0].live_source
        return [float(v) for v in np.maximum(np.maximum(source.up, source.down), 1e-6)]  # type: ignore[union-attr]

    def status(self) -> dict[str, Any]:
        sample, arrived = self.guard.latest()
        now = time.monotonic()
        pose = self._pose[1]
        st: dict[str, Any] = {
            "state": self.guard.state,
            "envelope": round(self.guard.envelope, 3),
            "hold": self.guard.hold,
            "gain": self.cueing.s.gain,
            "source": self.source.status(),
            "rejected": self.guard.rejected,
            "age_ms": None if sample is None else round((now - arrived) * 1000.0, 1),
            "delay_ms": None if self._delay_s is None else round(self._delay_s * 1000.0, 1),
            "pose": {a: round(float(v), 5) for a, v in zip(AXES, pose, strict=True)},
            "levels": dict(self._levels),
            "platforms": [
                {"id": rt.cfg.platform_id, "phase": str(rt.phase), "fault": rt.fault_reason or None,
                 "following": rt.following, "limits": rt.pipeline.counters.as_dict(),
                 "stroke": round(rt.pipeline.stroke_utilisation(), 3)}
                for rt in self.runtimes
            ],
            "ticks": self.ticks, "overruns": self.overruns, "max_tick_ms": round(self.max_tick_s * 1000.0, 2),
            "max_late_ms": round(self.max_late_s * 1000.0, 1), "late_ticks": self.late_ticks,
            "ranges": self.ranges(), "problem": self.output_problem(),
            "recording_s": None if self.recorder is None else round(self.recorder.seconds, 1),
        }
        if sample is not None:
            st["input"] = {
                "accel": [round(float(v), 2) for v in sample.accel], "rates": [round(float(v), 3) for v in sample.rates],
                "speed": round(float(sample.speed), 1), "rpm": round(float(sample.rpm)), "rumble": round(float(sample.rumble), 2),
                "active": sample.active, "says": describe(sample),
            }
        if self.service is not None:
            st["effects"] = self.service.status()
        elif self.effects_error:
            st["effects"] = {"running": False, "error": self.effects_error}
        return st


def when() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")
