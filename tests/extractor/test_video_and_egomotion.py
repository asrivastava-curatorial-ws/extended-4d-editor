from __future__ import annotations

import hashlib
import math
from collections import deque

import numpy as np
import pytest

from fourdvr_extractor.analysis import analyse, tracking_collapsed
from fourdvr_extractor.egomotion import CAM_TO_PLATFORM, bearings, estimate, kabsch, rotvec
from fourdvr_extractor.video import eye_view, guess_format, iterate_frames, probe


def test_probe_and_hash(videos) -> None:
    info = probe(videos["yaw360"])
    assert (info.width, info.height, info.fps, info.frame_count) == (720, 360, 30.0, 90)
    assert info.duration_s == pytest.approx(3.0)
    assert info.sha256 == hashlib.sha256(videos["yaw360"].read_bytes()).hexdigest()
    assert (info.projection, info.stereo) == ("360", "mono")
    with pytest.raises(Exception, match="does not exist"):
        probe(videos["yaw360"].with_name("missing.avi"))


def test_format_guess_and_eye_crop() -> None:
    assert guess_format(3840, 1920) == ("360", "mono")
    assert guess_format(7680, 1920) == ("360", "sbs")
    assert guess_format(4096, 4096) == ("360", "ou")
    assert guess_format(1920, 1080) == ("flat", "mono")
    frame = np.arange(4 * 8 * 3).reshape(4, 8, 3)
    assert eye_view(frame, "sbs").shape == (4, 4, 3) and eye_view(frame, "ou").shape == (2, 8, 3)


def test_frame_decimation(videos) -> None:
    info = probe(videos["yaw360"], with_hash=False)
    frames = list(iterate_frames(info, max_width=320, max_fps=15.0))
    assert len(frames) == 45 and frames[1][0] == 2 and frames[0][2].shape == (160, 320)


def _random_rotation(rng: np.random.Generator, max_angle: float) -> np.ndarray:
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    angle = rng.uniform(-max_angle, max_angle)
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * k @ k


def test_kabsch_and_rotvec_roundtrip() -> None:
    rng = np.random.default_rng(3)
    r = _random_rotation(rng, 0.5)
    a = rng.normal(size=(20, 3))
    assert np.allclose(kabsch(a, a @ r.T), r, atol=1e-10)
    v = rotvec(r)
    assert 0 < np.linalg.norm(v) <= 0.5 + 1e-9


@pytest.mark.parametrize("fov", ["sphere", "flat"])
def test_egomotion_recovers_rotation_and_translation(fov: str) -> None:
    rng = np.random.default_rng(11)
    n = 300
    if fov == "sphere":
        dirs = rng.normal(size=(n, 3))
    else:  # 75 degree pinhole field of view looking along +z
        dirs = np.column_stack([rng.uniform(-0.75, 0.75, n), rng.uniform(-0.4, 0.4, n), np.ones(n)])
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    depth = rng.uniform(4.0, 40.0, n)
    points = dirs * depth[:, None]
    q = _random_rotation(rng, 0.03)  # camera rotation between frames
    t = np.array([0.02, -0.01, 0.25])  # camera moves mostly forward
    p2 = (points - t) @ q  # P2 = Q^T (P1 - T)
    b1 = points / np.linalg.norm(points, axis=1, keepdims=True)
    b2 = p2 / np.linalg.norm(p2, axis=1, keepdims=True)
    em = estimate(b1, b2, threshold=0.002, rng=np.random.default_rng(0))
    assert em.valid
    assert np.allclose(em.rotation, rotvec(q), atol=2e-3)
    t_est = em.direction
    t_true = (q.T @ t) / np.linalg.norm(t)
    assert float(np.dot(t_est, t_true)) > math.cos(math.radians(6))
    assert em.speed > 0


def test_bearings_conventions() -> None:
    # centre of a 360 equirect = forward (+z camera = +X platform); right edge quarter = right
    b = bearings(np.array([[360.0, 180.0], [540.0, 180.0], [360.0, 0.0]]), 720, 360, "360")
    assert np.allclose(CAM_TO_PLATFORM @ b[0], [1, 0, 0], atol=1e-9)
    assert np.allclose(CAM_TO_PLATFORM @ b[1], [0, -1, 0], atol=1e-9)  # 90 deg right = -Y (left is +Y)
    assert np.allclose(CAM_TO_PLATFORM @ b[2], [0, 0, 1], atol=1e-9)  # top row = up


def test_analysis_360_yaw_rate_and_sign(videos) -> None:
    info = probe(videos["yaw360"], with_hash=False)
    a = analyse(info)
    yaw = np.degrees(np.median(a.angular_velocity[3:, 2]))
    assert yaw == pytest.approx(30.0, rel=0.1)  # camera turning left => positive yaw
    assert abs(np.degrees(np.median(a.angular_velocity[3:, 0]))) < 1.0
    assert np.median(a.confidence) > 0.8 and not a.cuts


def test_analysis_forward_translation(videos) -> None:
    info = probe(videos["forward"], with_hash=False)
    info.projection = "flat"
    a = analyse(info)
    v = np.median(a.velocity[3:], axis=0)
    assert v[0] > 0.05 and abs(v[1]) < 0.3 * v[0] and abs(v[2]) < 0.3 * v[0]
    assert np.degrees(np.max(np.abs(np.median(a.angular_velocity[3:], axis=0)))) < 1.0


def test_scene_cut_detected(videos) -> None:
    info = probe(videos["cut360"], with_hash=False)
    a = analyse(info)
    assert len(a.cuts) == 1 and a.cuts[0] == pytest.approx(1.5, abs=0.05)


def test_tracking_collapse_must_be_severe_and_sudden() -> None:
    # fast motion through sky: survival hovers around 20 % for many frames -> not an edit
    hard: deque[float] = deque([0.21, 0.22, 0.2, 0.21, 0.23], maxlen=30)
    assert not tracking_collapsed(0.19, hard)
    # a normal scene whose tracks suddenly all fail -> an edit
    normal: deque[float] = deque([0.8, 0.75, 0.82], maxlen=30)
    assert tracking_collapsed(0.04, normal)
    # an edit after a hard-to-track shot is still found when survival collapses further
    assert tracking_collapsed(0.03, deque([0.2, 0.22, 0.21], maxlen=30))
    # the first frame has no history: only a severe collapse counts
    assert tracking_collapsed(0.1, deque(maxlen=30)) and not tracking_collapsed(0.5, deque(maxlen=30))
    history: deque[float] = deque(maxlen=3)
    for f in (0.9, 0.8, 0.7, 0.6):
        tracking_collapsed(f, history)
    assert list(history) == [0.8, 0.7, 0.6]  # the history is updated and bounded
