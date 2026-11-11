from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest


def texture(w: int, h: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = cv2.resize(rng.random((h // 4, w // 4)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    img += 0.5 * cv2.resize(rng.random((h // 16, w // 16)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def write_video(path: Path, frames: list[np.ndarray], fps: float) -> Path:
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    for f in frames:
        vw.write(f)
    vw.release()
    return path


def yaw_video_360(
    path: Path,
    deg_per_s: float = 30.0,
    seconds: float = 3.0,
    fps: float = 30.0,
    w: int = 720,
    cut_at: float | None = None,
) -> Path:
    """Equirectangular 360 video of a camera turning left (scene shifts right)."""
    h = w // 2
    base = texture(w, h)
    other = texture(w, h, seed=5)
    frames = []
    for i in range(int(seconds * fps)):
        t = i / fps
        img = base if cut_at is None or t < cut_at else other
        shift = deg_per_s * t / 360.0 * w
        m = np.float32([[1, 0, shift], [0, 1, 0]])
        frames.append(cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_WRAP))
    return write_video(path, frames, fps)


def forward_video_flat(
    path: Path, speed_m_s: float = 6.0, seconds: float = 3.0, fps: float = 30.0, hfov: float = 75.0
) -> Path:
    """Pinhole video of a camera moving forward through a random point cloud."""
    rng = np.random.default_rng(1)
    pts = np.column_stack([rng.uniform(-20, 20, 3000), rng.uniform(-10, 10, 3000), rng.uniform(5, 60, 3000)])
    w, h = 640, 360
    f = (w / 2) / math.tan(math.radians(hfov) / 2)
    frames = []
    for i in range(int(seconds * fps)):
        z = pts[:, 2] - i * speed_m_s / fps
        vis = z > 1
        u = w / 2 + f * pts[vis, 0] / z[vis]
        v = h / 2 + f * pts[vis, 1] / z[vis]
        img = np.full((h, w, 3), 20, np.uint8)
        for uu, vv, zz in zip(u, v, z[vis], strict=True):
            if 0 <= uu < w and 0 <= vv < h:
                cv2.circle(img, (int(uu), int(vv)), max(1, int(30 / zz)), (200, 200, 200), -1)
        frames.append(img)
    return write_video(path, frames, fps)


@pytest.fixture(scope="session")
def videos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("videos")
    return {
        "yaw360": yaw_video_360(d / "yaw360.avi"),
        "forward": forward_video_flat(d / "forward.avi"),
        "cut360": yaw_video_360(d / "cut360.avi", cut_at=1.5),
    }
