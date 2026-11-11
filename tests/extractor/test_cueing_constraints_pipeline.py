from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from fourdvr_extractor import motionfile
from fourdvr_extractor.analysis import Analysis
from fourdvr_extractor.cli import main
from fourdvr_extractor.constraints import apply_constraints, soft_limit
from fourdvr_extractor.cueing import CueingSettings, compute_cues
from fourdvr_extractor.events import CLASSES, MotionEvents, analyse_events, bridge_handheld
from fourdvr_extractor.pipeline import DEFAULT_PROFILE, ExtractionProject, ExtractionSettings
from fourdvr_extractor.platform_config import load_platform_config
from fourdvr_extractor.signals import fade_envelope, washout_angle, washout_position
from fourdvr_extractor.validation import validate

FS = 100.0


def _events(
    n: int, label: str = "vehicle", acc: np.ndarray | None = None, omega: np.ndarray | None = None
) -> MotionEvents:
    t = np.arange(n) / FS
    return MotionEvents(
        times=t,
        angular_velocity=np.zeros((n, 3)) if omega is None else omega,
        velocity=np.zeros((n, 3)),
        acceleration=np.zeros((n, 3)) if acc is None else acc,
        confidence=np.ones(n),
        forward_speed=np.zeros(n),
        banking=np.zeros(n),
        vibration=np.zeros(n),
        labels=np.full(n, CLASSES.index(label)),
    )


def test_washout_filters_return_to_neutral() -> None:
    n = int(20 * FS)
    accel = np.zeros(n)
    accel[int(2 * FS) : int(12 * FS)] = 1.0  # sustained 10 s acceleration
    x = washout_position(accel, FS, 0.5, 0.12)
    assert np.max(np.abs(x)) < 0.2 and abs(x[-1]) < 0.01  # bounded, washes back toward neutral
    assert x[int(2.5 * FS)] > 0  # onset cue in the direction of the acceleration
    rate = np.zeros(n)
    rate[int(2 * FS) : int(10 * FS)] = 0.3  # constant turn rate
    ang = washout_angle(rate, FS, 0.25)
    assert ang[int(2.6 * FS)] > 0 and abs(ang[-1]) < 0.01


def test_tilt_coordination_sign_and_rate_limit() -> None:
    n = int(16 * FS)
    acc = np.zeros((n, 3))
    acc[int(3 * FS) : int(13 * FS), 0] = 1.0  # sustained forward acceleration
    acc[int(3 * FS) : int(13 * FS), 1] = 0.5  # and to the left
    s = CueingSettings(fade_s=0.5, max_tilt_deg=6.0, tilt_rate_deg_s=3.0)
    cues, ch = compute_cues(_events(n, acc=acc), FS, s)
    pitch = ch.tilt[:, 4]
    roll = ch.tilt[:, 3]
    assert pitch[int(10 * FS)] < 0  # nose up => gravity pushes the passenger back into the seat
    assert roll[int(10 * FS)] > 0  # right side down for acceleration to the left
    assert np.max(np.abs(pitch)) <= math.radians(6.0) + 1e-6
    assert np.max(np.abs(np.diff(pitch))) * FS <= math.radians(3.0) * 1.05
    assert np.allclose(cues[0], 0) and np.allclose(cues[-1], 0)


def test_static_class_produces_no_motion() -> None:
    n = int(6 * FS)
    omega = np.zeros((n, 3))
    omega[:, 2] = 0.5
    cues, _ = compute_cues(_events(n, label="static", omega=omega), FS, CueingSettings())
    assert np.max(np.abs(cues)) < 1e-9
    cues2, _ = compute_cues(_events(n, label="rapid", omega=omega), FS, CueingSettings())
    assert np.max(np.abs(cues2[:, 5])) > 0.005


def test_soft_limit_and_fade() -> None:
    x = np.linspace(-1, 1, 101)
    y = soft_limit(x, -0.1, 0.2)
    assert y.max() < 0.2 and y.min() > -0.1 and np.all(np.diff(y) > 0)
    env = fade_envelope(1001, FS, 1.0)
    assert env[0] == 0 and env[-1] == 0 and env[500] == 1


def test_constraints_make_aggressive_motion_valid() -> None:
    profile = load_platform_config(DEFAULT_PROFILE)
    rng = np.random.default_rng(4)
    n = int(20 * FS)
    t = np.arange(n) / FS
    raw = np.column_stack(
        [
            0.3 * np.sin(2 * np.pi * f * t + p)
            for f, p in zip(rng.uniform(0.2, 3.0, 6), rng.uniform(0, 6, 6), strict=True)
        ]
    )
    raw += rng.normal(0, 0.01, raw.shape)
    raw *= fade_envelope(n, FS, 1.0)[:, None]
    assert not validate(raw, FS, profile).passed
    result = apply_constraints(raw, FS, profile)
    report = validate(result.motion, FS, profile)
    assert result.converged and report.passed, report.violations
    assert report.actuators["stroke_ok"] and report.workspace_violations == 0
    assert np.allclose(result.motion[0], 0) and np.allclose(result.motion[-1], 0)


def test_validation_reports_violations() -> None:
    profile = load_platform_config(DEFAULT_PROFILE)
    bad = np.zeros((500, 6))
    bad[200:300, 2] = 0.3  # beyond heave limit and actuator stroke, with steps
    report = validate(bad, FS, profile)
    assert not report.passed and report.workspace_violations > 0
    assert any("heave" in v for v in report.violations)
    assert report.rows()[-1][3] == "VIOLATION"


def test_project_end_to_end_and_export(videos, tmp_path: Path) -> None:
    project = ExtractionProject(ExtractionSettings(programme_id=7, projection="360"))
    report = project.run_all(videos["yaw360"], tmp_path / "out.motion")
    assert report.passed
    mf = motionfile.read_motion(tmp_path / "out.motion")
    assert mf.programme_id == 7 and mf.sample_rate_hz == 100.0
    assert mf.video_sha256 == project.video.sha256  # type: ignore[union-attr]
    assert mf.duration_s == pytest.approx(3.0) and mf.sample_count == 301
    assert mf.header["validation"]["passed"] is True
    assert mf.header["generation"]["generator"] == "Extended-4D-Editor"
    assert np.allclose(mf.samples[0], 0) and np.allclose(mf.samples[-1], 0)  # loops seamlessly
    assert np.max(np.abs(mf.samples[:, 5])) > 0  # the turn produced a yaw cue


def test_cli_generate_validate_info(videos, tmp_path: Path, capsys) -> None:
    out = tmp_path / "fwd.motion"
    report = tmp_path / "report.json"
    rc = main(
        [
            "generate",
            str(videos["forward"]),
            "-o",
            str(out),
            "--programme-id",
            "2",
            "--projection",
            "flat",
            "--rate",
            "120",
            "--report",
            str(report),
            "-q",
        ]
    )
    assert rc == 0
    text = capsys.readouterr().out
    assert "validation PASSED" in text and "vehicle" in text
    mf = motionfile.read_motion(out)
    assert mf.sample_rate_hz == 120.0 and mf.programme_id == 2
    assert json.loads(report.read_text())["passed"] is True
    assert main(["validate", str(out)]) == 0
    assert main(["info", str(videos["forward"]), "--no-hash"]) == 0
    settings = tmp_path / "s.json"
    assert main(["settings", str(settings)]) == 0
    loaded = ExtractionSettings.from_dict(json.loads(settings.read_text()))
    assert loaded.sample_rate_hz == 100.0
    assert main(["generate", str(videos["forward"]), "--rate", "30", "-o", str(tmp_path / "x.motion")]) == 2
    # the output directory is created before the analysis; a directory as output fails at once
    nested = tmp_path / "new" / "dir" / "fwd.motion"
    assert main(["generate", str(videos["forward"]), "--projection", "flat", "-o", str(nested), "-q"]) == 0
    assert motionfile.read_motion(nested).programme_id == 1
    assert main(["generate", str(videos["forward"]), "-o", str(tmp_path), "-q"]) == 2
    assert "is a directory" in capsys.readouterr().err


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        ExtractionSettings.from_dict({"projection": "dome"})
    with pytest.raises(ValueError):
        ExtractionSettings.from_dict({"cueing": {"no_such_option": 1}})


def test_settings_reject_unknown_keys() -> None:
    import pytest

    from fourdvr_extractor.pipeline import ExtractionSettings

    base = ExtractionSettings().to_dict()
    assert ExtractionSettings.from_dict(base).to_dict() == base
    for bad in (
        {"colour": 1},
        {"analysis": {"max_width": 640, "bogus": 1}},
        {"constraints": {"nope": 2}},
        {"cueing": {"unknown_gain": 1.0}},
    ):
        with pytest.raises(ValueError):
            ExtractionSettings.from_dict(bad)


def _ride_analysis(pitch_rate: float, yaw_rate: float, seconds: float = 8.0, fps: float = 30.0) -> Analysis:
    """Synthetic analysis: steady forward motion with a constant pitch/yaw rate from 2 s to 6 s."""
    t = np.arange(1, int(seconds * fps) + 1) / fps
    omega = np.zeros((len(t), 3))
    inside = (t > 2.0) & (t < 6.0)
    omega[inside, 1] = pitch_rate
    omega[inside, 2] = yaw_rate
    vel = np.zeros((len(t), 3))
    vel[:, 0] = 2.0
    return Analysis(
        times=t,
        angular_velocity=omega,
        velocity=vel,
        confidence=np.ones(len(t)),
        parallax=np.full(len(t), 0.1),
        frame_rate=fps,
        tracked_points=np.full(len(t), 200.0),
    )


def test_centripetal_accelerations_of_the_vehicle_frame() -> None:
    mid = int(4.0 * FS)
    # nose-up pitching at speed (valley, loop, pull-up): pushed up into the seat, a "dip" event
    dip = analyse_events(_ride_analysis(pitch_rate=-1.0, yaw_rate=0.0), 8.0, FS)
    assert dip.acceleration[mid, 2] == pytest.approx(2.0, rel=0.1)  # -forward * pitch rate
    assert abs(dip.acceleration[mid, 1]) < 0.05
    assert any(e["type"] == "dip" for e in dip.events)
    # nose-down pitching (crest, drop): lifted out of the seat, a "crest" event
    crest = analyse_events(_ride_analysis(pitch_rate=1.0, yaw_rate=0.0), 8.0, FS)
    assert crest.acceleration[mid, 2] == pytest.approx(-2.0, rel=0.1)
    assert any(e["type"] == "crest" for e in crest.events)
    # left turn (+yaw): acceleration towards the inside of the curve (+Y, left)
    turn = analyse_events(_ride_analysis(pitch_rate=0.0, yaw_rate=0.5), 8.0, FS)
    assert turn.acceleration[mid, 1] == pytest.approx(1.0, rel=0.1)
    assert abs(turn.acceleration[mid, 2]) < 0.05
    # the vertical specific force becomes an upward heave onset cue
    _cues, ch = compute_cues(dip, FS, CueingSettings())
    k = int(2.3 * FS)
    assert ch.translational[k, 2] > 0 and np.argmax(ch.translational[:, 2]) < int(4.0 * FS)


def test_short_handheld_stretch_inside_a_ride_is_the_same_ride() -> None:
    c = {name: CLASSES.index(name) for name in CLASSES}

    def runs(*parts: tuple[str, float]) -> np.ndarray:
        return np.concatenate([np.full(int(sec * FS), c[name]) for name, sec in parts])

    out = bridge_handheld(runs(("rapid", 3), ("handheld", 6), ("flight", 2)), FS, 10.0)
    assert set(out[int(3 * FS) : int(9 * FS)]) == {c["vehicle"]}
    for labels in (
        runs(("rapid", 3), ("handheld", 12), ("rapid", 2)),  # too long: really handheld
        runs(("static", 3), ("handheld", 4), ("rapid", 2)),  # after a stop
        runs(("rapid", 3), ("handheld", 4), ("cut", 1), ("rapid", 2)),  # before an edit
        runs(("handheld", 4), ("rapid", 2)),  # at the start
    ):
        assert np.array_equal(bridge_handheld(labels, FS, 10.0), labels)


def test_impacts_are_isolated_peaks_not_rough_sections() -> None:
    fps = 30.0
    t = np.arange(1, int(30 * fps) + 1) / fps
    rng = np.random.default_rng(3)
    omega = rng.normal(0, 0.01, (len(t), 3))
    rough = (t > 12) & (t < 24)
    omega[rough] += rng.normal(0, 0.6, (int(rough.sum()), 3))  # 12 s of continuously rough motion
    k = int(np.argmin(np.abs(t - 6.0)))
    omega[k : k + 5, 0] += 1.5  # one short shake in a calm stretch
    omega[k + 5 : k + 10, 0] -= 1.5
    vel = np.zeros((len(t), 3))
    vel[:, 0] = 1.0
    analysis = Analysis(
        times=t,
        angular_velocity=omega,
        velocity=vel,
        confidence=np.ones(len(t)),
        parallax=np.full(len(t), 0.1),
        frame_rate=fps,
        tracked_points=np.full(len(t), 200.0),
    )
    ev = analyse_events(analysis, 30.0, FS)
    times = [ti for ti, _m in ev.impacts]
    assert any(abs(ti - 6.15) < 0.3 for ti in times)
    assert not any(11.5 < ti < 24.5 for ti in times)  # neither inside nor at the edges of the rough part
