"""Platforms with 2 to 6 actuators - electric, hydraulic, pneumatic, on plain valves - and their supervision."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from controller_helpers import ROOT, FakeClock, make_runtime, run_for, write_motion
from fourdvr_motion.esp32_protocol import Fault
from fourdvr_motion.kinematics import AXES, StewartGeometry, seat_geometry
from fourdvr_motion.pipeline import OnOffStage, PlatformPipeline
from fourdvr_motion.platform_config import (
    OnOffSettings,
    PlatformConfigError,
    SupervisionSettings,
    load_platform_config,
    parse_platform_config,
)
from fourdvr_motion.runtime import Phase
from fourdvr_motion.sources import ConstantSource
from fourdvr_motion.supervision import FollowingErrorMonitor

PLATFORMS = ROOT / "config" / "platforms"
HYDRAULIC = PLATFORMS / "example-hydraulic-6dof.toml"
PNEUMATIC = PLATFORMS / "example-pneumatic-3dof.toml"
ONOFF = PLATFORMS / "example-pneumatic-onoff-3dof.toml"
TWO = PLATFORMS / "example-electric-2dof.toml"


def data(path: Path) -> dict:
    return tomllib.loads(path.read_text())


# ------------------------------------------------------------------------------------------- profiles
def test_the_examples_load_with_their_kind_axes_and_link() -> None:
    hyd, pne, onoff, two = (load_platform_config(p) for p in (HYDRAULIC, PNEUMATIC, ONOFF, TWO))
    assert (hyd.actuator, hyd.control, hyd.driver, hyd.legs, hyd.dof) == ("hydraulic", "position", "modbus_tcp", 6, AXES)
    assert hyd.modbus is not None and hyd.modbus.positions == 0 and hyd.modbus.heartbeat == 11 and hyd.udp is None
    assert (pne.actuator, pne.legs, pne.dof, pne.driver) == ("pneumatic", 3, ("heave", "roll", "pitch"), "esp32")
    assert pne.supervision == SupervisionSettings(0.03, 0.5, 0.2)
    assert (onoff.control, onoff.onoff) == ("onoff", OnOffSettings("retracted", 0.25, 0.10, 0.25, 0.4))
    assert onoff.supervision.lag_s == pytest.approx(0.6) and onoff.supervision.following_error == pytest.approx(0.025)
    assert (two.legs, two.dof, two.udp.send, two.udp.header, two.udp.counter) == (2, ("roll", "pitch"), "positions", b"MS", "u16")  # type: ignore[union-attr]
    assert two.to_summary()["actuators"] == 2 and two.to_summary()["dof"] == ["roll", "pitch"]
    # the axes a seat does not have: no limits needed, held at zero
    assert np.all(pne.limits.position_min[[0, 1, 5]] == 0) and np.all(pne.limits.position_max[[0, 1, 5]] == 0)


@pytest.mark.parametrize("change, message", [
    (lambda d: d["geometry"].update(dof=["heave", "roll", "pitch", "yaw"]), "3 actuators cannot move 4 axes"),
    (lambda d: d["geometry"].update(dof=["heave", "spin"]), "dof must list axes"),
    (lambda d: d["calibration"].update(park_pose=[0.01, 0, -0.04, 0, 0, 0]), "does not move on surge, sway, yaw"),
    (lambda d: d["calibration"].update(actuator_offsets=[0.0] * 6), "list of 3 numbers"),
    (lambda d: d["platform"].update(control="onoff", actuator="electric"), "solenoid valves"),
    (lambda d: d.update(onoff={"rest": "retracted"}), "is for control"),
    (lambda d: d["limits"]["position"].pop("roll"), "missing axes: roll"),
    (lambda d: d.update(supervision={"following_error": -1}), "following_error"),
    (lambda d: d.update(supervision={"lag": 1}), "unknown key"),
    (lambda d: d["platform"].update(driver="modbus_tcp"), 'needs address = "tcp://'),
])
def test_wrong_seat_profiles_are_refused_with_a_reason(change, message: str) -> None:
    d = data(PNEUMATIC)
    change(d)
    with pytest.raises(PlatformConfigError, match=message):
        parse_platform_config(d)


def test_joints_that_cannot_move_an_axis_are_found() -> None:
    d = data(TWO)
    d["geometry"]["base_joints"] = [[0.0, 0.3, 0.0], [0.0, -0.3, 0.0]]       # both on the pitch axis: no pitch
    d["geometry"]["platform_joints"] = [[0.0, 0.3, 0.0], [0.0, -0.3, 0.0]]
    with pytest.raises(PlatformConfigError, match="cannot move the platform on 'pitch'"):
        parse_platform_config(d)
    with pytest.raises(ValueError, match="2 to 6"):
        StewartGeometry(np.zeros((7, 3)), np.zeros((7, 3)), np.zeros(3))


@pytest.mark.parametrize("change, message", [
    (lambda d: d["modbus"].update(type="u8"), "type must be one of"),
    (lambda d: d["modbus"].update(range=[0, 70000]), "does not fit into u16"),
    (lambda d: d["modbus"].update(range=[5, 5]), "two different numbers"),
    (lambda d: d["modbus"].update(ready_bit=0, status=-1), "need status"),
    (lambda d: d["modbus"].update(colour=1), "unknown key"),
    (lambda d: d["modbus"].update(period_ms=500), "period_ms"),
])
def test_wrong_modbus_maps_are_refused(change, message: str) -> None:
    d = data(HYDRAULIC)
    change(d)
    with pytest.raises(PlatformConfigError, match=message):
        parse_platform_config(d)


@pytest.mark.parametrize("change, message", [
    (lambda d: d["udp"].update(send="valves"), 'is for control = "onoff"'),
    (lambda d: d["udp"].update(header="zz"), "hexadecimal"),
    (lambda d: d["udp"].update(pose_scale=[1, 2]), "pose_scale"),
    (lambda d: d["udp"].update(send="pose", feedback=True), "cannot be used with send"),
    (lambda d: d["udp"].update(pose_axes=["heave", "heave"]), "each once"),
    (lambda d: d["platform"].update(address="tcp://x:1"), 'needs address = "udp://'),
])
def test_wrong_udp_formats_are_refused(change, message: str) -> None:
    d = data(TWO)
    change(d)
    with pytest.raises(PlatformConfigError, match=message):
        parse_platform_config(d)


# ------------------------------------------------------------------------------------------- kinematics
@pytest.mark.parametrize("legs", [2, 3])
def test_seat_kinematics_forward_and_back(legs: int) -> None:
    g = seat_geometry(legs, 0.45, 0.5)
    rng = np.random.default_rng(legs)
    for _ in range(20):
        pose = np.where(g.dof_mask, rng.uniform(-0.08, 0.08, 6), 0.0)
        lengths = g.leg_lengths(pose)
        assert lengths.shape == (legs,)
        back = g.forward(lengths, guess=np.zeros(6))
        assert np.allclose(back, pose, atol=1e-9) and np.all(back[~g.dof_mask] == 0)
    assert g.jacobian(np.zeros(6)).shape == (legs, 6)


def test_four_cylinders_under_three_axes_take_the_best_fit() -> None:
    corners = [[0.4, 0.3, 0.0], [0.4, -0.3, 0.0], [-0.4, 0.3, 0.0], [-0.4, -0.3, 0.0]]
    g = StewartGeometry(np.array(corners), np.array(corners), np.array([0, 0, 0.5]),
                        dof_mask=np.array([a in ("heave", "roll", "pitch") for a in AXES]))
    pose = np.array([0, 0, 0.02, 0.05, -0.04, 0])
    noisy = g.leg_lengths(pose) + np.array([0.0002, -0.0002, -0.0002, 0.0002])   # sensors are never exact
    assert np.allclose(g.forward(noisy), pose, atol=2e-3)
    with pytest.raises(ValueError):
        g.forward(g.leg_lengths(pose) + np.array([0.05, 0, 0, 0]))               # one cylinder 5 cm off: no pose


# ------------------------------------------------------------------------------------------- pipeline
def test_a_seat_leaves_out_what_it_cannot_do_and_counts_it() -> None:
    cfg = load_platform_config(PNEUMATIC)
    pipe = PlatformPipeline(cfg, 100.0)
    pipe.reset(cfg.calibration.neutral_pose)
    source = ConstantSource(np.array([0.08, 0.05, 0.03, 0.06, -0.05, 0.1]), "six axes")
    pipe.transition(source, 0.0)
    for k in range(400):
        strokes = pipe.step(k * 0.01)
    assert np.all(pipe.pose[[0, 1, 5]] == 0.0) and pipe.pose[2] == pytest.approx(0.03, abs=1e-3)
    assert pipe.pose[3] == pytest.approx(0.06, abs=1e-3) and pipe.pose[4] == pytest.approx(-0.05, abs=1e-3)
    assert strokes.shape == (3,) and np.all((strokes >= 0) & (strokes <= cfg.actuators.stroke))
    assert pipe.counters.axes_left_out > 300
    assert np.allclose(pipe.pose_from_strokes(strokes), pipe.pose, atol=1e-6)


def test_valves_switch_with_hysteresis_and_dwell() -> None:
    stage = OnOffStage(OnOffSettings("retracted", 0.25, 0.10, 0.25, 0.4), np.full(1, 0.1), np.full(1, 0.05), 0.01)
    level = lambda f: np.array([0.05 + f * 0.05])                                 # noqa: E731 - level -> stroke
    out = [stage.step(level(f))[0] for f in [0.2] * 10]                          # below on_level: stays down
    assert set(out) == {0.0}
    assert stage.step(level(0.3))[0] == 0.1 and stage.switches == 1              # up
    assert stage.step(level(0.05))[0] == 0.1                                      # down asked at once: the dwell holds it
    out = [stage.step(level(0.05))[0] for _ in range(30)]
    assert out[23] == 0.0 and out[22] == 0.1 and stage.switches == 2             # down 0.25 s after it went up
    for f in (0.2, 0.15, 0.2):                                                    # inside the hysteresis: nothing
        assert stage.step(level(f))[0] == 0.0
    rest_up = OnOffStage(OnOffSettings("extended", 0.25, 0.10, 0.0, 0.4), np.full(1, 0.1), np.full(1, 0.05), 0.01)
    rest_up.reset(np.full(1, 0.05))
    assert rest_up.step(level(0.0))[0] == 0.1                                     # rests up
    assert rest_up.step(level(-0.3))[0] == 0.0 and rest_up.step(level(-0.05))[0] == 0.1


def test_an_onoff_seat_turns_motion_into_valve_states() -> None:
    cfg = load_platform_config(ONOFF)
    pipe = PlatformPipeline(cfg, 100.0)
    pipe.reset(cfg.calibration.park_pose)
    assert np.all(pipe.strokes == 0.0)                                            # parked: all down
    pipe.transition(ConstantSource(np.array([0, 0, 0.03, 0, 0, 0]), "up"), 0.0)
    for k in range(300):
        strokes = pipe.step(k * 0.01)
    assert np.all(strokes == cfg.actuators.stroke)                                # heave: all up
    pipe.transition(ConstantSource(np.array([0, 0, 0, 0, 0.09, 0]), "nose down"), 3.0)
    for k in range(300):
        strokes = pipe.step(3.0 + k * 0.01)
    assert strokes[0] == 0.0 and np.all(strokes[1:] == cfg.actuators.stroke[1:])  # nose down: front down, rear up
    assert np.allclose(pipe.pose_from_strokes(strokes), cfg.calibration.park_pose)  # ends only: the model restarts at park


# ------------------------------------------------------------------------------------------- following error
def test_the_following_error_allows_any_lag_up_to_lag_s() -> None:
    mon = FollowingErrorMonitor(SupervisionSettings(0.01, 0.2, 0.1), 1)
    for k in range(100):                                                          # a ramp, 0.5 m/s
        t = k * 0.01
        mon.command(t, np.array([0.5 * t]))
        assert mon.check(t, np.array([0.5 * max(0.0, t - 0.09)])) == ""         # 90 ms late: fine
    assert mon.error[0] == 0.0
    msgs = []
    for k in range(100, 140):
        t = k * 0.01
        mon.command(t, np.array([0.5 * t]))
        msgs.append(mon.check(t, np.array([0.45])))                              # stuck at 0.45 m
    first = next(i for i, m in enumerate(msgs) if m)
    assert "actuator 1 does not follow" in msgs[first] and 20 <= first <= 25    # 10 mm off after ~20 ms, then 0.2 s
    mon.reset()
    assert mon.check(2.0, np.array([0.0])) == ""                                  # nothing commanded: nothing to say
    off = FollowingErrorMonitor(SupervisionSettings(0.0, 0.2, 0.1), 1)
    off.command(0.0, np.array([0.3]))
    assert off.check(0.5, np.array([0.0])) == "" and not off.enabled


# ------------------------------------------------------------------------------------------- the runtime, simulated
def _play(cfg, tmp_path: Path, clock: FakeClock, seconds: float = 4.0):  # type: ignore[no-untyped-def]
    rt = make_runtime(cfg, tmp_path, clock)
    run_for(rt, clock, 0.3)
    sha = write_motion(rt.cache.path("x"), programme_id=1, duration=6.0)
    rt.cache.path("x").rename(rt.cache.path(sha))
    rt.register_programme(1, sha, 6.0)
    rt.cmd_play(1, 1, rt.clock.master_time(clock()) + 0.2, 6.0, True)
    run_for(rt, clock, seconds)
    return rt


@pytest.mark.parametrize("profile", [HYDRAULIC, PNEUMATIC, ONOFF, TWO])
def test_every_kind_plays_a_programme_followed_and_supervised(profile: Path, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _play(load_platform_config(profile), tmp_path, clock)
    st = rt.status()
    assert rt.phase == Phase.PLAYING and st["fault"] is None, st["fault"]
    assert st["link"]["measured"] is True and len(st["measured_strokes_m"]) == rt.cfg.legs
    assert len(st["actuator_lengths_m"]) == rt.cfg.legs and st["profile"]["actuators"] == rt.cfg.legs
    assert max(rt.follow_monitor.worst) < rt.cfg.supervision.following_error     # it followed all along
    if rt.cfg.control == "onoff":
        assert rt.pipeline.onoff is not None and rt.pipeline.onoff.switches >= 1
        assert set(np.round(rt.pipeline.strokes, 6)) <= {0.0, round(float(rt.cfg.actuators.stroke[0]), 6)}


@pytest.mark.parametrize("profile", [HYDRAULIC, PNEUMATIC])
def test_a_jammed_actuator_stops_and_parks_the_platform(profile: Path, tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _play(load_platform_config(profile), tmp_path, clock, seconds=1.5)
    plant = rt.driver.device.plant                                            # type: ignore[attr-defined]
    plant.jammed[1] = True                                                    # lost its pressure: sank and stays down
    plant.pos[1] = 0.0
    run_for(rt, clock, 2.0)
    assert rt.phase == Phase.FAULT and "actuator 2 does not follow its command" in rt.fault_reason
    run_for(rt, clock, 1.0)
    assert rt.driver.device.guard.state.name in ("PARKING", "PARKED")       # type: ignore[attr-defined]


def test_lost_supply_is_a_fault_and_parks(tmp_path: Path) -> None:
    clock = FakeClock()
    rt = _play(load_platform_config(HYDRAULIC), tmp_path, clock, seconds=1.0)
    device = rt.driver.device                                                # type: ignore[attr-defined]
    device.supply_ok = False
    run_for(rt, clock, 0.3)
    assert rt.phase == Phase.FAULT and "SUPPLY" in rt.fault_reason and "OUTPUT" in rt.fault_reason
    assert device.guard.faults & Fault.SUPPLY


def test_a_board_for_another_number_of_actuators_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    cfg = load_platform_config(PNEUMATIC)
    rt = make_runtime(cfg, tmp_path, clock)
    rt.driver.device.guard.cfg.axes = 6                                      # type: ignore[attr-defined]
    rt.driver.device.guard.__post_init__()                                   # type: ignore[attr-defined]
    run_for(rt, clock, 0.5)
    assert rt.phase == Phase.FAULT and "the board drives 6 actuators, the profile has 3" in rt.fault_reason


def test_a_valve_board_needs_an_onoff_profile(tmp_path: Path) -> None:
    clock = FakeClock()
    cfg = replace(load_platform_config(PNEUMATIC), control="position")
    rt = make_runtime(cfg, tmp_path, clock)
    rt.driver.device.guard.cfg.output = "valves"                             # type: ignore[attr-defined]
    run_for(rt, clock, 0.5)
    assert rt.phase == Phase.FAULT and "the board switches valves" in rt.fault_reason
