"""Every seat its own motion and effects - through the platform's own limits."""

from __future__ import annotations

from typing import Any

import numpy as np

from fourdvr_fx.dmx import CaptureOutput
from fourdvr_fx.patch import AudioConfig, FixtureDef, OutputConfig, Patch
from fourdvr_live.sample import LiveSample
from fourdvr_motion.runtime import Phase
from fourdvr_play.engine import MAX_PULSES, IdleSource, PlayEngine
from fourdvr_play.logic import Reaction
from play_helpers import PlayStepper, make_cfg, manual_source

JOLT = Reaction(accel=(0.0, 0.0, 3.0), seconds=0.25)


def make(platforms: int = 2, shared: bool = False, patch: Patch | None = None, **kw: Any) -> tuple[PlayEngine, PlayStepper]:
    cfg = make_cfg(seats=platforms, platforms=platforms, patch=patch, **kw)
    engine = PlayEngine(cfg, source=manual_source() if shared else None, audio_backend="null", effects_output=CaptureOutput())
    stepper = PlayStepper(engine)
    stepper.step(300)                                  # three quiet seconds: the platforms come up to their neutral pose
    return engine, stepper


def offsets(engine: PlayEngine) -> np.ndarray:
    return np.array([rt.pipeline.pose - rt.cfg.calibration.neutral_pose for rt in engine.runtimes])


def run(engine: PlayEngine, st: PlayStepper, n: int, each: Any = None) -> np.ndarray:
    """n control periods; the platforms' poses relative to neutral, [period, platform, axis]."""
    out = []
    for _ in range(n):
        st.step(1, each)
        out.append(offsets(engine))
    return np.array(out)


def within_limits(engine: PlayEngine, poses: np.ndarray) -> bool:
    ok = True
    for k, rt in enumerate(engine.runtimes):
        lim, neutral = rt.cfg.limits, rt.cfg.calibration.neutral_pose
        p = poses[:, k, :] + neutral
        v = np.diff(p, axis=0) * 100.0
        a = np.diff(v, axis=0) * 100.0
        ok = ok and bool(np.all(p <= lim.position_max + 1e-9) and np.all(p >= lim.position_min - 1e-9)
                         and np.all(np.abs(v) <= lim.velocity * 1.001) and np.all(np.abs(a) <= lim.acceleration * 1.001))
        ok = ok and rt.fault_reason == "" and rt.pipeline.counters.as_dict()["position_clamps"] == 0
    return ok


def test_without_a_shared_source_the_engine_idles_and_the_platforms_follow() -> None:
    engine, _st = make()
    assert isinstance(engine.source, IdleSource) and engine.guard.envelope == 0.0
    assert all(rt.phase == Phase.PLAYING and rt.following for rt in engine.runtimes)
    assert np.allclose(offsets(engine), 0.0, atol=1e-6) and engine.gate == 0.0


def test_a_push_moves_only_its_seat_and_the_seat_comes_back() -> None:
    engine, st = make()
    engine.set_live(True)
    st.step(60)
    assert engine.gate == 1.0
    engine.react(["A1"], JOLT)
    poses = run(engine, st, 150)
    peak = np.max(np.abs(poses), axis=0)
    assert 0.006 < peak[0, 2] < 0.03                      # seat A1 went up about a centimetre ...
    assert np.max(peak[0, [0, 1, 3, 4, 5]]) < 1e-4        # ... and nowhere else
    assert np.max(peak[1]) < 1e-9                         # seat A2 did not move at all
    later = run(engine, st, 500)
    assert np.max(np.abs(later[-1])) < 5e-4               # back at neutral
    assert within_limits(engine, np.concatenate([poses, later]))
    assert all(sum(rt.pipeline.counters.as_dict().values()) == 0 for rt in engine.runtimes)      # the limiter had nothing to do
    assert engine.reactions == 1 and engine.channels["A1"].reactions == 1 and engine.channels["A2"].reactions == 0


def test_nothing_reacts_unless_the_session_runs_and_a_hold_brings_the_seats_to_rest() -> None:
    engine, st = make()
    engine.react(["A1", "A2"], JOLT)                      # the session does not run: left out
    poses = run(engine, st, 100)
    assert np.max(np.abs(poses)) < 1e-9 and engine.suppressed == 1 and engine.reactions == 0
    engine.set_live(True)
    st.step(60)
    long_push = Reaction(accel=(2.0, 0.0, 0.0), seconds=3.0)
    engine.react(["A1"], long_push)
    moving = run(engine, st, 80)
    assert abs(moving[-1, 0, 0]) > 0.004
    engine.hold(True)                                     # the operator's hold, in the middle of it
    held = run(engine, st, 250)
    assert engine.gate == 0.0 and np.max(np.abs(held[-1])) < 1e-3
    engine.react(["A1"], JOLT)
    st.step(5)
    assert engine.suppressed == 2
    engine.engage()
    st.step(60)
    engine.react(["A1"], JOLT)
    again = run(engine, st, 100)
    assert np.max(np.abs(again[:, 0, 2])) > 0.006
    assert within_limits(engine, np.concatenate([moving, held])) and within_limits(engine, again)
    engine.set_live(False)                                # the session ends: also at rest
    st.step(150)
    assert engine.gate == 0.0


def test_a_seats_own_telemetry_moves_that_seat_and_the_shared_source_moves_all() -> None:
    engine, st = make(shared=True)
    engine.set_live(True)

    def seat_two(now: float) -> None:                     # the game: seat A2 speeds up
        if st.k % 2 == 0:
            engine.offer("A2", {"4dlive": 1, "accel": [3.0, 0.0, 0.0], "speed": 20.0}, now)

    poses = run(engine, st, 500, seat_two)
    assert np.max(np.abs(poses[:, 0, :])) < 1e-9          # A1 gets nothing of it
    assert poses[-1, 1, 4] < -0.02                        # A2 leans back (nose up) for the push forwards
    assert engine.channels["A2"].guard.state == "running" and engine.channels["A1"].guard.state == "waiting for the source"
    assert not engine.offer("A2", {"accel": "fast"}, st.now) and not engine.offer("Z9", {"accel": [0, 0, 0]}, st.now)
    assert engine.channels["A2"].status()["rejected"] == 1
    run(engine, st, 400)                                  # the game stops sending: A2 fades to rest by itself
    assert engine.channels["A2"].guard.state == "source stalled" and np.max(np.abs(offsets(engine)[1])) < 2e-3

    def shared(now: float) -> None:                       # the ride everybody is on: a long left turn
        if st.k % 2 == 0:
            engine.guard.offer(LiveSample(accel=(0.0, 3.0, 0.0), speed=25.0), now)

    both = run(engine, st, 600, shared)
    assert both[-1, 0, 3] > 0.02 and np.allclose(both[-1, 0], both[-1, 1], atol=1e-4)     # the same lean for both
    assert within_limits(engine, poses) and within_limits(engine, both)

    engine, st = make(shared=True, shared_motion=False)   # shared_motion = false: the room's effects only
    engine.set_live(True)
    both = run(engine, st, 600, lambda now: engine.guard.offer(LiveSample(accel=(0.0, 3.0, 0.0), speed=25.0), now))
    assert np.max(np.abs(both)) < 1e-9 and engine.house_levels.get("wind", 0.0) > 0.2


def test_comfort_gentle_off_and_a_seat_that_is_switched_off() -> None:
    engine, st = make(platforms=4)
    engine.set_live(True)
    st.step(60)
    engine.configure("A2", True, "gentle", ())
    engine.configure("A3", True, "off", ())
    engine.configure("A4", False, "full", ())
    engine.react(["A1", "A2", "A3", "A4"], Reaction(accel=(0.0, 0.0, 3.0), seconds=0.25, effects={"vibration": 1.0, "water": 1.0},
                                                    effect_seconds=1.0))
    poses = run(engine, st, 60)
    peak = np.max(np.abs(poses[:, :, 2]), axis=0)
    assert peak[0] > 0.006 and abs(peak[1] / peak[0] - 0.5) < 0.05 and peak[2] < 1e-9 and peak[3] < 1e-9
    assert engine.channels["A1"].levels == {"vibration": 1.0, "water": 1.0}
    assert engine.channels["A3"].levels == {"vibration": 1.0, "water": 1.0}              # motion off, effects stay
    assert engine.channels["A4"].levels == {} and engine.channels["A4"].reactions == 0   # switched off: nothing
    engine.configure("A1", True, "full", ("water",))
    st.step(1)
    assert engine.channels["A1"].levels == {"vibration": 1.0}                             # "not for me: water"
    st.step(120)
    assert engine.channels["A1"].levels == {} and engine.channels["A3"].levels == {}      # over after a second


def test_effects_reach_the_seats_own_fixtures_and_the_room() -> None:
    patch = Patch(output=OutputConfig(protocol="none"), audio=AudioConfig(enabled=False), fixtures=[
        FixtureDef(id="seatfan", effect="wind", scope="platform", address=10, address_step=1),
        FixtureDef(id="roomfog", effect="fog", address=30)])
    engine, st = make(patch=patch)
    assert engine.service is not None and engine.effects_error == ""
    out = engine.service.output
    assert isinstance(out, CaptureOutput)

    def frames(n: int) -> None:
        for _ in range(n):
            st.step(2)
            engine.service.tick(st.now)

    engine.react(["A2"], Reaction(effects={"wind": 0.8}, effect_seconds=2.0))             # before the start: nothing
    frames(30)
    assert (out.channel(1, 10), out.channel(1, 11), out.channel(1, 30)) == (0, 0, 0)
    engine.set_live(True)
    frames(40)
    engine.react(["A2"], Reaction(effects={"wind": 0.8}, effect_seconds=2.0))
    frames(40)
    assert out.channel(1, 10) == 0 and out.channel(1, 11) > 150 and out.channel(1, 30) == 0      # A2's fan only
    engine.react(["A1", "A2"], Reaction(effects={"fog": 1.0}, effect_seconds=1.0), house=True)   # "all": the room too
    frames(20)
    assert out.channel(1, 30) > 200
    states = engine._states(st.now)
    assert states["PLATFORM-01"].doc is engine.channels["A1"].doc and states["theatre"].doc is engine.house_doc
    assert states["PLATFORM-02"].active and states["PLATFORM-02"].doc.level("wind", 0.0) == 0.8
    frames(150)
    assert (out.channel(1, 10), out.channel(1, 11), out.channel(1, 30)) == (0, 0, 0)              # all over, all dark
    engine.react(["A2"], Reaction(effects={"wind": 0.8}, effect_seconds=5.0))
    frames(30)
    engine.set_live(False)                                                                # the session is paused
    frames(120)
    assert out.channel(1, 11) == 0
    status = engine.status()["play"]
    assert status["live"] is False and status["gate"] == 0.0 and [s["id"] for s in status["seats"]] == ["A1", "A2"]


def test_the_strongest_request_stays_inside_the_platforms_limits() -> None:
    engine, st = make(platforms=1)
    engine.set_live(True)
    st.step(60)
    worst = Reaction(accel=(4.0, -4.0, 4.0), rates=(0.6, -0.6, 0.6), seconds=3.0)
    for _ in range(MAX_PULSES + 5):                       # far more than a show would ever ask for at once
        engine.react(["A1"], worst)
    poses = run(engine, st, 500)
    for _ in range(4):                                    # and jolts on top, in both directions
        engine.react(["A1"], Reaction(accel=(-4.0, 4.0, -4.0), seconds=0.1))
        poses = np.concatenate([poses, run(engine, st, 40)])
    assert engine.channels["A1"].left_out == 5 and np.max(np.abs(poses)) > 0.02
    assert within_limits(engine, poses)
    rt = engine.runtimes[0]
    assert rt.phase == Phase.PLAYING and rt.fault_reason == ""
    rest = run(engine, st, 1200)
    assert np.max(np.abs(rest[-1])) < 1e-3
