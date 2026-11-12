"""Real-time filters, the live cueing and what stands between a source and the cueing."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import signal

from fourdvr_extractor.signals import washout_angle as offline_angle
from fourdvr_extractor.signals import washout_position as offline_position
from fourdvr_live import filters
from fourdvr_live.cueing import LiveCueing, LiveCueingSettings, soft_limit
from fourdvr_live.safety import HOLD, PAUSED, RESUMING, RUNNING, STALLED, WAITING, AxisMap, InputGuard, SafetySettings
from fourdvr_live.sample import ACCEL_LIMIT, G, LiveSample, describe


# --------------------------------------------------------------------------------------------- filters
@pytest.mark.parametrize(("num", "den"), [
    ([1.0, 0.0], [1.0, 4.0, 9.0]),
    ([4.0], [1.0, 2.8, 4.0]),
    ([1.0, 0.0], list(np.polymul([1.0, 6.28, 9.87], [1.0, 0.75]))),
])
def test_bilinear_is_the_transform_the_offline_stage_uses(num: list[float], den: list[float]) -> None:
    b, a = filters.bilinear(num, den, 0.01)
    bs, as_, _dt = signal.cont2discrete((num, den), 0.01, method="bilinear")
    assert np.allclose(b, np.ravel(bs), rtol=1e-9, atol=1e-12)
    assert np.allclose(a, as_, rtol=1e-9, atol=1e-12)


def test_live_washout_filters_give_the_offline_result_sample_for_sample() -> None:
    rng = np.random.default_rng(3)
    x = np.cumsum(rng.standard_normal(1500)) * 0.05
    live_pos = filters.washout_position(0.5, 0.12, 0.01)
    live_ang = filters.washout_angle(0.25, 0.01)
    pos = np.array([live_pos.step(np.array([v]))[0] for v in x])
    ang = np.array([live_ang.step(np.array([v]))[0] for v in x])
    assert np.allclose(pos, offline_position(x, 100.0, 0.5, 0.12), atol=1e-9)
    assert np.allclose(ang, offline_angle(x, 100.0, 0.25), atol=1e-9)


def test_a_filter_runs_several_signals_at_once_and_forgets_on_reset() -> None:
    f = filters.lowpass2(2.0, 0.01, channels=3)
    for _ in range(400):
        y = f.step(np.array([1.0, -2.0, 0.5]))
    assert np.allclose(y, [1.0, -2.0, 0.5], atol=1e-3)          # unity gain at rest
    f.reset()
    assert np.allclose(f.step(np.zeros(3)), 0.0)


# --------------------------------------------------------------------------------------------- cueing
def run_cueing(c: LiveCueing, accel, rates, seconds: float) -> np.ndarray:  # type: ignore[no-untyped-def]
    return np.array([c.step(np.asarray(accel, float), np.asarray(rates, float)).copy() for _ in range(int(seconds * 100))])


def test_sustained_acceleration_becomes_a_tilt_and_the_first_movement_washes_out() -> None:
    c = LiveCueing(100.0)
    out = run_cueing(c, (5.0, 0.0, 0.0), (0, 0, 0), 12.0)
    # speeding up: the seat moves forwards first ...
    assert out[:150, 0].max() > 0.01
    # ... and comes back; what stays is nose-up tilt of asin(scaled acceleration / g)
    assert abs(out[-1, 0]) < 0.004
    expected = -math.asin(soft_limit(np.array(5.0 * 0.2), 2.5) / G)
    assert out[-1, 4] == pytest.approx(expected, abs=2e-3)
    assert np.allclose(out[-1, [1, 2, 3, 5]], 0.0, atol=1e-6)
    # the tilt never turns faster than its rate limit, and never beyond the largest tilt
    rate = np.abs(np.diff(out[:, 4])) * 100.0
    assert rate.max() <= math.radians(5.0) + 1e-6


def test_tilt_directions_and_limit() -> None:
    c = LiveCueing(100.0)
    left = run_cueing(c, (0.0, 40.0, 0.0), (0, 0, 0), 8.0)        # pushed hard to the left (a left bend)
    assert left[-1, 3] > 0                                         # right side down, so that gravity pushes right
    assert left[-1, 3] <= math.radians(7.0) + 1e-9
    braking = run_cueing(LiveCueing(100.0), (-6.0, 0.0, 0.0), (0, 0, 0), 8.0)
    assert braking[-1, 4] > 0                                      # nose down


def test_rotation_onsets_wash_out_and_extreme_input_stays_bounded() -> None:
    c = LiveCueing(100.0)
    out = run_cueing(c, (0, 0, 0), (0.0, 0.0, 0.4), 15.0)
    assert out[:300, 5].max() > 0.02                               # turning left: the seat turns left at first
    assert abs(out[-1, 5]) < 2e-3                                  # and is back when the turn has gone on
    wild = run_cueing(LiveCueing(100.0), (ACCEL_LIMIT, -ACCEL_LIMIT, ACCEL_LIMIT), (12, -12, 12), 5.0)
    assert np.all(np.isfinite(wild))
    assert np.abs(wild[:, :3]).max() < 0.25 and np.abs(wild[:, 3:]).max() < 0.8


def test_gain_axis_gains_comfort_and_reset() -> None:
    base = run_cueing(LiveCueing(100.0), (3.0, 2.0, 1.0), (0.1, 0.1, 0.2), 2.0)
    half = run_cueing(LiveCueing(100.0, LiveCueingSettings(gain=0.5)), (3.0, 2.0, 1.0), (0.1, 0.1, 0.2), 2.0)
    assert np.allclose(half, 0.5 * base, atol=1e-12)
    s = LiveCueingSettings.from_dict({"axis_gains": {"yaw": 0.0, "surge": 2.0}})
    shaped = run_cueing(LiveCueing(100.0, s), (3.0, 2.0, 1.0), (0.1, 0.1, 0.2), 2.0)
    assert np.allclose(shaped[:, 5], 0.0) and np.allclose(shaped[:, 0], 2.0 * base[:, 0])
    calm = LiveCueing(100.0, LiveCueingSettings(comfort=0.5))
    out = run_cueing(calm, (3.0, 0.0, 0.0), (0, 0, 0.3), 0.5)
    assert np.allclose(out[:, 0], 0.5 * run_cueing(LiveCueing(100.0), (3.0, 0, 0), (0, 0, 0.3), 0.5)[:, 0])
    calm.reset()
    assert np.allclose(calm.step(np.zeros(3), np.zeros(3)), 0.0)
    with pytest.raises(ValueError, match="unknown cueing setting"):
        LiveCueingSettings.from_dict({"gian": 1.0})
    with pytest.raises(ValueError, match="unknown axis"):
        LiveCueingSettings.from_dict({"axis_gains": {"spin": 1.0}})


# --------------------------------------------------------------------------------------------- the guard
def moving() -> LiveSample:
    return LiveSample(accel=(2.0, 0.0, 0.0), rates=(0.0, 0.0, 0.1), speed=20.0)


def test_guard_fades_in_only_after_the_source_has_been_steady() -> None:
    g = InputGuard(SafetySettings(stall_s=0.25, resume_s=0.5, fade_out_s=1.0, fade_in_s=2.0))
    assert g.step(0.0) == (None, 0.0, False) and g.state == WAITING
    resets = 0
    t = 0.0
    for k in range(400):
        t = 0.01 * (k + 1)
        g.offer(moving(), t)
        sample, env, reset = g.step(t)
        resets += reset
        if t < 0.5:
            assert sample is None and env == 0.0 and g.state == RESUMING
    assert resets == 1 and g.state == RUNNING and g.envelope == 1.0


def test_guard_rests_on_stall_pause_and_hold_and_a_hiccup_is_only_a_dip() -> None:
    g = InputGuard(SafetySettings(stall_s=0.25, resume_s=0.5, fade_out_s=1.0, fade_in_s=2.0))
    t = 0.0

    def run(seconds: float, sample: LiveSample | None) -> None:
        nonlocal t
        for _ in range(int(seconds * 100)):
            t += 0.01
            if sample is not None:
                g.offer(sample, t)
            g.step(t)

    run(4.0, moving())
    assert g.envelope == 1.0
    run(0.35, None)                               # a hiccup: 0.1 s beyond the stall time
    assert g.state == STALLED and 0.7 < g.envelope < 1.0
    resets = g.resets
    run(3.0, moving())
    assert g.envelope == 1.0 and g.resets == resets        # no restart of the cueing for a dip
    run(2.0, None)                                # a real stall
    assert g.envelope == 0.0 and g.step(t)[0] is None
    run(3.5, moving())
    assert g.envelope == 1.0 and g.resets == resets + 1
    paused = moving()
    paused.active = False
    run(1.5, paused)
    assert g.state == PAUSED and g.envelope == 0.0
    run(3.5, moving())
    g.hold = True
    run(1.5, moving())
    assert g.state == HOLD and g.envelope == 0.0


def test_guard_refuses_what_is_not_a_number_and_cuts_what_is_extreme() -> None:
    g = InputGuard()
    assert not g.offer(LiveSample(accel=(float("nan"), 0.0, 0.0)), 1.0)
    assert not g.offer(LiveSample(rates=(0.0, float("inf"), 0.0)), 1.0)
    assert g.rejected == 2 and g.latest()[0] is None
    assert g.offer(LiveSample(accel=(1e6, -1e6, 3.0), rates=(99.0, 0.0, 0.0), throttle=7.0, rumble=-1.0,
                              effects={"rain": 3.0}), 2.0)
    s = g.latest()[0]
    assert s is not None
    assert s.accel == (ACCEL_LIMIT, -ACCEL_LIMIT, 3.0) and s.rates[0] == 12.0
    assert s.throttle == 1.0 and s.rumble == 0.0 and s.effects == {"rain": 1.0}


def test_axis_map_turns_and_scales_directions() -> None:
    g = InputGuard(axes=AxisMap({"yaw": -1.0, "sway": 0.5, "heave": 0.0}))
    g.offer(LiveSample(accel=(1.0, 2.0, 3.0), rates=(0.1, 0.2, 0.3), attitude=(0.1, 0.2)), 0.0)
    s = g.latest()[0]
    assert s is not None
    assert s.accel == (1.0, 1.0, 0.0) and s.rates == (0.1, 0.2, -0.3) and s.attitude == (0.1, 0.2)
    with pytest.raises(ValueError, match="unknown axis"):
        AxisMap({"twist": 1.0})


def test_plain_words_for_checking_directions() -> None:
    assert describe(LiveSample(accel=(-4.0, 3.0, 0.0), rates=(0.0, 0.0, 0.3))) == [
        "braking", "pushed left (left turn)", "nose turning left"]
    assert describe(LiveSample(accel=(2.0, -2.0, -3.0), rates=(0.2, -0.2, -0.2))) == [
        "speeding up", "pushed right (right turn)", "lifted", "nose turning right", "nose going up", "rolling right"]
    assert describe(LiveSample()) == []
