from __future__ import annotations

import numpy as np
import pytest

from controller_helpers import demo_samples
from fourdvr_motion.interpolation import MotionTrack, pchip_slopes
from fourdvr_motion.limits import PoseLimiter, blend_time, smootherstep
from fourdvr_motion.platform_config import PlatformConfig
from fourdvr_motion.sources import BlendSource, ConstantSource, SourceSelector, TrajectorySource

DT = 0.01


def _run(lim, ref: np.ndarray, start: np.ndarray | None = None) -> np.ndarray:
    pl = PoseLimiter(lim, DT)
    pl.reset(ref[0] if start is None else start)
    out = [pl.x.copy()]
    for k in range(len(ref) - 1):
        out.append(pl.step(ref[k], ref[k + 1]))
    return np.array(out)


def _derivs(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    v = np.diff(x, axis=0) / DT
    a = np.diff(v, axis=0) / DT
    j = np.diff(a, axis=0) / DT
    return v, a, j


def _assert_within(lim, x: np.ndarray) -> None:
    v, a, j = _derivs(x)
    assert np.all(x <= lim.position_max + 1e-12) and np.all(x >= lim.position_min - 1e-12)
    assert np.all(np.abs(v) <= lim.velocity * (1 + 1e-9))
    assert np.all(np.abs(a) <= lim.acceleration * (1 + 1e-6))
    assert np.all(np.abs(j) <= lim.jerk * (1 + 1e-6))


def test_step_response_respects_all_limits_without_overshoot(platform_cfg: PlatformConfig) -> None:
    lim = platform_cfg.limits
    target = np.array([0.1, -0.1, 0.08, 0.15, -0.15, 0.18])
    x = _run(lim, np.tile(target, (500, 1)), start=np.zeros(6))
    _assert_within(lim, x)
    assert np.allclose(x[-1], target, atol=1e-6)
    overshoot = np.max(np.sign(target) * (x - target), axis=0)
    assert np.all(overshoot <= 1e-9)


def test_out_of_range_targets_never_exceed_position_limits(platform_cfg: PlatformConfig) -> None:
    lim = platform_cfg.limits
    target = np.array([1.0, -1.0, 1.0, 1.0, -1.0, 1.0])
    x = _run(lim, np.tile(target, (400, 1)), start=np.zeros(6))
    _assert_within(lim, x)
    assert np.allclose(x[-1], np.clip(target, lim.position_min, lim.position_max), atol=1e-6)


def test_feasible_trajectory_is_tracked_exactly(platform_cfg: PlatformConfig) -> None:
    lim = platform_cfg.limits
    ref = demo_samples(8.0, 100.0)
    x = _run(lim, ref)
    assert np.max(np.abs(x - ref)) < 2e-4  # only the start-up acceleration mismatch
    assert np.max(np.abs(x[100:] - ref[100:])) < 1e-9


def test_infeasible_trajectory_is_limited(platform_cfg: PlatformConfig) -> None:
    lim = platform_cfg.limits
    t = np.arange(0, 6, DT)
    ref = np.zeros((len(t), 6))
    ref[:, 0] = 0.11 * np.sin(2 * np.pi * 2.5 * t)  # far beyond velocity/acceleration limits
    ref[:, 4] = 0.3 * np.sign(np.sin(2 * np.pi * 0.5 * t))  # square wave beyond position limits
    x = _run(lim, ref)
    _assert_within(lim, x)


def test_discontinuity_recovers_smoothly(platform_cfg: PlatformConfig) -> None:
    lim = platform_cfg.limits
    ref = demo_samples(8.0, 100.0)
    ref[300:, 1] += 0.02
    x = _run(lim, ref)
    _assert_within(lim, x)
    assert np.max(np.abs(x[500:] - ref[500:])) < 1e-6


def test_pipeline_keeps_stroke_and_actuator_speed(platform_cfg: PlatformConfig) -> None:
    from fourdvr_motion.pipeline import PlatformPipeline

    pipe = PlatformPipeline(platform_cfg, 100.0)
    pipe.reset(np.zeros(6))
    combined = np.array([0.12, -0.12, 0.10, 0.17, 0.17, -0.2])  # each axis legal, the combination is not
    lengths = platform_cfg.geometry.leg_lengths(combined)
    act = platform_cfg.actuators
    assert np.any(lengths > act.max_length) or np.any(lengths < act.min_length)
    pipe.transition(ConstantSource(combined), 0.0, minimum=0.05)
    hist = []
    for k in range(800):
        pipe.step(k * DT)
        hist.append(pipe.lengths.copy())
    hist = np.array(hist)
    assert np.all(hist >= act.usable_min() - 1e-9) and np.all(hist <= act.usable_max() + 1e-9)
    vel = np.diff(hist, axis=0) / DT
    assert np.max(np.abs(vel)) <= act.max_velocity * (1 + 1e-6)
    acc = np.diff(vel, axis=0) / DT
    excess = int(np.sum(np.any(np.abs(acc) > act.max_acceleration * (1 + 1e-6), axis=1)))
    assert excess <= pipe.counters.actuator_accel_excess  # any residual excess is detected and reported
    assert pipe.counters.workspace_scalings > 0


def test_validated_profile_needs_no_guard_intervention(platform_cfg: PlatformConfig) -> None:
    from fourdvr_motion.pipeline import PlatformPipeline

    samples = demo_samples(6.0, 100.0)
    track = MotionTrack(samples, 100.0)
    pipe = PlatformPipeline(platform_cfg, 100.0)
    pipe.reset(np.zeros(6))
    pipe.transition(TrajectorySource(track, 0.0, 6.0, False, 1, 1), 0.0)
    errs = []
    for k in range(700):
        pipe.step(k * DT)
        if k > 150:
            errs.append(np.abs(pipe.pose - track.sample((k + 1) * DT)))
    c = pipe.counters
    assert c.workspace_scalings == 0 and c.actuator_rate_scalings == 0 and c.actuator_accel_excess == 0
    assert np.max(errs) < 1e-6


def test_blend_time_respects_limits(platform_cfg: PlatformConfig) -> None:
    lim = platform_cfg.limits
    delta = np.array([0.1, 0.0, -0.08, 0.1, 0.0, 0.15])
    duration = blend_time(delta, lim, 1.0, 0.0)
    t = np.arange(0, duration + DT, DT)
    path = np.array([smootherstep(u) * delta for u in t / duration])
    _assert_within(lim, path)
    assert smootherstep(0.0) == 0.0 and smootherstep(1.0) == 1.0 and smootherstep(0.5) == pytest.approx(0.5)


def test_pchip_no_overshoot_and_continuity() -> None:
    rate = 50.0
    y = np.zeros((20, 6))
    y[5:, 0] = 0.1  # a step: cubic splines would ring, PCHIP must not
    y[:, 1] = np.linspace(0, 0.05, 20) ** 2
    track = MotionTrack(y, rate)
    t = np.linspace(0, track.last_time, 2000)
    s = track.sample_many(t)
    assert s[:, 0].max() <= 0.1 + 1e-12 and s[:, 0].min() >= -1e-12
    assert np.all(np.diff(s[:, 1]) >= -1e-12)  # monotone data stays monotone
    for i in range(20):  # exact at the knots
        assert np.allclose(track.sample(i / rate), y[i])
    slopes = pchip_slopes(y, 1 / rate)
    assert slopes.shape == y.shape


def test_interpolation_is_independent_of_control_rate() -> None:
    samples = demo_samples(5.0, 100.0)
    track = MotionTrack(samples, 100.0)
    for rate in (50.0, 100.0, 137.0, 200.0):
        times = np.arange(0, 5.0, 1 / rate)
        s = track.sample_many(times)
        ref = np.array([np.interp(times, np.arange(len(samples)) / 100.0, samples[:, i]) for i in range(6)]).T
        assert np.max(np.abs(s - ref)) < 2e-4


def test_trajectory_source_and_blend() -> None:
    track = MotionTrack(demo_samples(4.0), 100.0)
    traj = TrajectorySource(track, epoch=100.0, duration_s=4.0, loop=True, programme_id=1, timeline_id=1)
    assert traj.position(102.5) == pytest.approx(2.5) and traj.position(109.0) == pytest.approx(1.0)
    assert traj.position(90.0) == 0.0
    sel = SourceSelector(ConstantSource(np.zeros(6)))
    sel.transition(ConstantSource(np.ones(6) * 0.01), 10.0, 2.0)
    assert np.allclose(sel.pose(10.0), 0.0) and np.allclose(sel.pose(11.0), 0.005) and np.allclose(sel.pose(12.5), 0.01)
    assert not isinstance(sel.current, BlendSource)
