from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from controller_helpers import FakeClock, make_runtime, run_for, write_motion
from fourdvr_motion.protocol import Timeline, TimelineState
from fourdvr_motion.runtime import Phase


def _prepared(platform_cfg, tmp_path: Path, clock: FakeClock, pid: int = 1, duration: float = 6.0):
    rt = make_runtime(platform_cfg, tmp_path, clock)
    sha = write_motion(rt.cache.path("x"), programme_id=pid, duration=duration)
    rt.cache.path("x").rename(rt.cache.path(sha))
    run_for(rt, clock, 0.2)  # first status from the simulated ESP32 initialises the pipeline
    assert rt.pipeline.initialised and rt.phase == Phase.IDLE
    rt.register_programme(pid, sha, duration)
    return rt


def test_initialises_from_reported_park_pose(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = make_runtime(platform_cfg, tmp_path, clock)
    run_for(rt, clock, 0.1)
    assert np.allclose(rt.pipeline.pose, platform_cfg.calibration.park_pose, atol=1e-4)
    status = rt.status()
    assert status["state"] == "READY" and status["link"]["connected"] is True


def test_scheduled_play_follows_timeline(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    rt.cmd_stop(1, rt.clock.master_time(clock()))
    run_for(rt, clock, 4.0)  # lift from park to neutral
    assert rt.phase == Phase.STOPPED and np.allclose(rt.pipeline.pose, 0.0, atol=1e-6)
    start_at = rt.clock.master_time(clock()) + 1.0
    rt.cmd_play(1, 2, start_at, 6.0, True)
    run_for(rt, clock, 0.5)
    assert rt.phase == Phase.SCHEDULED and np.allclose(rt.pipeline.pose, 0.0, atol=1e-9)  # does not start early
    record: list = []
    run_for(rt, clock, 9.0, record)
    track = rt.programmes[1].track
    errs = [
        np.abs(pose - track.sample((t + rt.pipeline.dt - start_at) % 6.0))
        for t, pose, _ in record
        if t - start_at > 2.0
    ]
    assert rt.phase == Phase.PLAYING
    assert max(float(e.max()) for e in errs) < 1e-5  # same timeline as the video, incl. the loop at 6 s
    st = rt.status()
    assert st["state"] == "RUNNING" and st["programme_id"] == 1 and abs(st["drift_s"]) < 1e-6
    # the simulated actuators (fed through the binary protocol) reproduce the pose (forward kinematics)
    measured = rt.pipeline.pose_from_strokes(rt.link.positions_m)
    assert np.max(np.abs(measured - rt.pipeline.pose)) < 0.01


def test_pause_resume_and_stop(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    start_at = rt.clock.master_time(clock()) + 0.5
    rt.cmd_play(1, 1, start_at, 6.0, True)
    run_for(rt, clock, 3.0)
    pause_at = rt.clock.master_time(clock()) + 0.2
    position = pause_at - start_at
    rt.cmd_pause(1, 2, pause_at, position)
    run_for(rt, clock, 3.0)
    held = rt.programmes[1].track.sample(position)
    assert rt.phase == Phase.PAUSED and np.allclose(rt.pipeline.pose, held, atol=1e-6)
    resume_at = rt.clock.master_time(clock()) + 0.3
    rt.cmd_resume(1, 3, resume_at, position)
    run_for(rt, clock, 2.5)
    t = rt.clock.master_time(clock())
    expected = rt.programmes[1].track.sample(position + (t + rt.pipeline.dt - resume_at))
    assert rt.phase == Phase.PLAYING and np.allclose(rt.pipeline.pose, expected, atol=1e-5)
    rt.cmd_stop(4, t)
    run_for(rt, clock, 6.0)
    assert rt.phase == Phase.STOPPED and np.allclose(rt.pipeline.pose, 0.0, atol=1e-6)
    assert rt.status()["state"] == "STOPPED"


def test_stale_timeline_commands_ignored(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    now = rt.clock.master_time(clock())
    rt.cmd_play(1, 5, now + 0.2, 6.0, True)
    run_for(rt, clock, 0.1)
    rt.cmd_stop(4, now + 0.3)  # older timeline id: must not stop the newer play
    run_for(rt, clock, 2.0)
    assert rt.phase in (Phase.ENGAGING, Phase.PLAYING) and rt.timeline.timeline_id == 5


def test_late_join_via_sync_blends_in(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    now = rt.clock.master_time(clock())
    tl = Timeline(
        timeline_id=9, state=TimelineState.PLAYING, programme_id=1, epoch=now - 2.5, duration_s=6.0, loop=True
    )
    rt.cmd_sync(tl, force=False)
    record: list = []
    run_for(rt, clock, 8.0, record)
    poses = np.array([p for _, p, _ in record])
    v = np.diff(poses, axis=0) * rt.rate
    lim = platform_cfg.limits
    assert np.all(np.abs(v) <= lim.velocity * (1 + 1e-6))
    assert rt.phase == Phase.PLAYING
    t = rt.clock.master_time(clock())
    expected = rt.programmes[1].track.sample((t + rt.pipeline.dt - tl.epoch) % 6.0)
    assert np.allclose(rt.pipeline.pose, expected, atol=1e-5)


def test_link_loss_faults_parks_and_manual_recovery(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    now = rt.clock.master_time(clock())
    rt.cmd_play(1, 1, now + 0.2, 6.0, True)
    run_for(rt, clock, 3.0)
    sim = rt.driver.device  # type: ignore[attr-defined]
    sim.silent = True  # cable pulled: no packets accepted, no replies
    run_for(rt, clock, 1.0)
    st = rt.status()
    assert rt.phase == Phase.FAULT and st["state"] == "ERROR" and "lost" in st["fault"]
    # firmware watchdog parks the actuators on its own
    run_for(rt, clock, 6.0)
    assert sim.guard.faults and sim.guard.state.name in ("PARKING", "PARKED")
    sim.silent = False
    run_for(rt, clock, 3.0)
    assert rt.phase == Phase.FAULT  # manual recovery: waits for the operator
    rt.cmd_stop(2, rt.clock.master_time(clock()))
    run_for(rt, clock, 6.0)
    assert rt.phase == Phase.STOPPED and not sim.guard.faults
    assert np.allclose(rt.pipeline.pose, 0.0, atol=1e-6)


def test_programme_without_motion_stays_neutral(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    rt.register_programme(2, "", 30.0)
    rt.cmd_play(2, 1, rt.clock.master_time(clock()) + 0.1, 30.0, True)
    run_for(rt, clock, 6.0)
    assert rt.phase == Phase.PLAYING and np.allclose(rt.pipeline.pose, 0.0, atol=1e-6)


def test_server_loss_returns_to_neutral(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _prepared(platform_cfg, tmp_path, clock)
    rt.server_loss_timeout_s = 2.0
    rt.on_server_connected()
    rt.cmd_play(1, 1, rt.clock.master_time(clock()) + 0.2, 6.0, True)
    run_for(rt, clock, 2.0)
    rt.on_server_disconnected()
    run_for(rt, clock, 1.0)
    assert rt.phase in (Phase.ENGAGING, Phase.PLAYING)  # keeps running through short outages
    run_for(rt, clock, 6.0)
    assert rt.phase == Phase.STOPPED and any("server unreachable" in e for e in rt.status()["errors"])


def test_programme_id_mismatch_rejected(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = make_runtime(platform_cfg, tmp_path, clock)
    sha = write_motion(rt.cache.path("y"), programme_id=7)
    rt.cache.path("y").rename(rt.cache.path(sha))
    with pytest.raises(ValueError, match="programme 7"):
        rt.register_programme(3, sha, 6.0)


def test_control_loop_ticks_on_a_uniform_grid_despite_wakeup_jitter() -> None:
    """The motion track must be sampled at the scheduled instants (as in the offline validation),
    not at the jittered wake-up times, or the limiter sees artificial velocity/jerk noise."""
    import threading
    import time

    from fourdvr_motion.runtime import ControlLoop

    class FakeDriver:
        def open(self) -> None: ...

        def close(self) -> None: ...

    class FakeRuntime:
        def __init__(self) -> None:
            self.driver = FakeDriver()
            self.loop_overruns = 0
            self.ticks: list[float] = []
            self.done = threading.Event()

        def tick(self, now_local: float) -> None:
            self.ticks.append(now_local)
            time.sleep(0.0004 * (len(self.ticks) % 5))  # variable work per tick: 0..1.6 ms
            if len(self.ticks) >= 60:
                self.done.set()

        def add_error(self, msg: str) -> None:
            raise AssertionError(msg)

    rt = FakeRuntime()
    loop = ControlLoop([rt], 200.0)  # type: ignore[list-item]
    loop.start()
    assert rt.done.wait(5.0)
    loop.stop()
    steps = np.diff(np.array(rt.ticks[:60]))
    if rt.loop_overruns == 0:  # a heavily loaded test machine may miss a deadline; then the grid restarts
        assert np.allclose(steps, 1.0 / 200.0, rtol=0, atol=1e-9)
