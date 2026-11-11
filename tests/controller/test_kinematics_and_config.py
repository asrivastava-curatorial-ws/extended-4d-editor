from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from controller_helpers import EXAMPLE, SIM
from fourdvr_motion.kinematics import (
    StewartGeometry,
    matrix_to_rpy,
    rotation_matrices,
    rotation_matrix,
    symmetric_hexapod,
)
from fourdvr_motion.platform_config import PlatformConfigError, load_platform_config, parse_platform_config


def test_rotation_matrix_properties() -> None:
    rng = np.random.default_rng(0)
    for _ in range(50):
        r, p, y = rng.uniform(-0.6, 0.6, 3)
        m = rotation_matrix(r, p, y)
        assert np.allclose(m @ m.T, np.eye(3), atol=1e-12) and math.isclose(np.linalg.det(m), 1.0)
        assert np.allclose(matrix_to_rpy(m), (r, p, y), atol=1e-12)
    # sign conventions: +roll => right side (-Y) down, +pitch => nose (+X) down, +yaw => nose left (+Y)
    assert rotation_matrix(0.1, 0, 0) @ np.array([0, -1, 0]) @ np.array([0, 0, 1]) < 0
    assert rotation_matrix(0, 0.1, 0) @ np.array([1, 0, 0]) @ np.array([0, 0, 1]) < 0
    assert rotation_matrix(0, 0, 0.1) @ np.array([1, 0, 0]) @ np.array([0, 1, 0]) > 0
    rpy = rng.uniform(-0.5, 0.5, (10, 3))
    assert np.allclose(rotation_matrices(rpy), [rotation_matrix(*v) for v in rpy])


def test_neutral_and_pure_heave_analytic() -> None:
    g = symmetric_hexapod(0.75, 0.55, 20, 20, 0.80)
    assert np.allclose(g.neutral_lengths(), 0.80)
    # pure heave dz: every leg = sqrt(d_xy^2 + (h + dz)^2)
    d_xy = np.linalg.norm(g.platform_joints[0, :2] - g.base_joints[0, :2])
    h = g.neutral_position[2]
    for dz in (-0.1, 0.05, 0.12):
        expected = math.sqrt(d_xy**2 + (h + dz) ** 2)
        assert np.allclose(g.leg_lengths(np.array([0, 0, dz, 0, 0, 0])), expected)


def test_ik_fk_roundtrip_and_vectorised() -> None:
    cfg = load_platform_config(EXAMPLE)
    g = cfg.geometry
    rng = np.random.default_rng(1)
    poses = rng.uniform([-0.1, -0.1, -0.08, -0.15, -0.15, -0.18], [0.1, 0.1, 0.08, 0.15, 0.15, 0.18], (40, 6))
    many = g.leg_lengths_many(poses)
    for pose, lengths in zip(poses, many, strict=True):
        assert np.allclose(g.leg_lengths(pose), lengths, atol=1e-12)
        assert np.allclose(g.forward(lengths), pose, atol=1e-8)


def test_rotation_about_centre_keeps_centre_fixed() -> None:
    cfg = load_platform_config(EXAMPLE)
    g = cfg.geometry
    centre_world = g.neutral_position + g.rotation_centre
    for pose in ([0, 0, 0, 0.1, 0, 0], [0, 0, 0, 0, -0.12, 0.05]):
        origin, r = g.transform(np.array(pose, dtype=float))
        assert np.allclose(origin + r @ g.rotation_centre, centre_world, atol=1e-12)
    # translation moves the centre by exactly the translation
    origin, r = g.transform(np.array([0.05, -0.02, 0.03, 0, 0, 0]))
    assert np.allclose(origin + r @ g.rotation_centre - centre_world, [0.05, -0.02, 0.03])


def test_geometry_validation() -> None:
    with pytest.raises(ValueError):
        StewartGeometry(base_joints=np.zeros((5, 3)), platform_joints=np.zeros((6, 3)), neutral_position=np.zeros(3))
    with pytest.raises(ValueError):
        symmetric_hexapod(0.75, 0.55, 20, 20, 0.1)


def test_example_platform_configs_load() -> None:
    cfg = load_platform_config(EXAMPLE)
    assert cfg.platform_id == "PLATFORM-01" and cfg.driver == "servo_electric"
    assert np.allclose(cfg.actuators.stroke, 0.30) and len(cfg.sha256) == 64
    assert cfg.limits.velocity.shape == (6,) and cfg.calibration.park_pose[2] == pytest.approx(-0.08)
    sim = load_platform_config(SIM)
    assert sim.driver == "simulation"


def _data() -> dict:
    import tomllib

    return tomllib.loads(EXAMPLE.read_text())


def test_hydraulic_and_pneumatic_are_not_falsely_available() -> None:
    for kind in ("hydraulic", "pneumatic"):
        data = _data()
        data["platform"]["driver"] = kind
        with pytest.raises(PlatformConfigError, match="not implemented"):
            parse_platform_config(data)


def test_invalid_configs_rejected() -> None:
    data = _data()
    data["limits"]["position"]["heave"] = [-0.30, 0.10]  # outside the actuator stroke
    with pytest.raises(PlatformConfigError, match="heave"):
        parse_platform_config(data)
    data = _data()
    data["calibration"]["park_pose"] = [0, 0, -0.5, 0, 0, 0]
    with pytest.raises(PlatformConfigError):
        parse_platform_config(data)
    data = _data()
    del data["limits"]["jerk"]["yaw"]
    with pytest.raises(PlatformConfigError, match="yaw"):
        parse_platform_config(data)
    data = _data()
    data["actuators"]["max_length"] = 0.655
    with pytest.raises(PlatformConfigError):
        parse_platform_config(data)


def test_calibration_offsets_change_commands() -> None:
    data = _data()
    data["calibration"]["actuator_offsets"] = [0.001, 0, 0, 0, 0, -0.002]
    data["calibration"]["zero_offsets"] = [0, 0, 0.002, 0, 0, 0]
    cfg = parse_platform_config(data)
    base = load_platform_config(EXAMPLE)
    from fourdvr_motion.pipeline import PlatformPipeline

    p1, p0 = PlatformPipeline(cfg, 100), PlatformPipeline(base, 100)
    p1.reset(np.zeros(6))
    p0.reset(np.zeros(6))
    p1.step(0.0)
    p0.step(0.0)
    expected_shift = cfg.geometry.leg_lengths(np.array([0, 0, 0.002, 0, 0, 0])) - base.geometry.neutral_lengths()
    assert np.allclose(p1.strokes - p0.strokes, expected_shift + np.array([0.001, 0, 0, 0, 0, -0.002]), atol=1e-9)
    # forward kinematics undoes the calibration mapping
    assert np.allclose(p1.pose_from_strokes(p1.strokes), p1.pose, atol=1e-8)


def test_config_replace_keeps_validation_results() -> None:
    cfg = load_platform_config(EXAMPLE)
    clone = replace(cfg, platform_id="PLATFORM-02", link_platform_id=2)
    assert clone.geometry is cfg.geometry and clone.platform_id == "PLATFORM-02"
