"""Helpers shared by the live-mode tests (a plain module, like controller_helpers)."""

from __future__ import annotations

import math
import time
from pathlib import Path

import cv2
import numpy as np

from fourdvr_live.engine import LiveEngine
from fourdvr_live.sources.base import DEMO, LiveSource

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "config" / "platforms" / "example-electric-6dof.toml"


class ManualSource(LiveSource):
    """A source whose samples the test offers itself (no thread does anything)."""

    info = DEMO

    def run(self) -> None:
        return


class Stepper:
    """Drives a LiveEngine tick by tick on a clock of its own (faster than real time, never late)."""

    def __init__(self, engine: LiveEngine, rate: float = 100.0) -> None:
        self.engine = engine
        self.rate = rate
        self.base = time.monotonic()
        self.k = 0
        for rt in engine.runtimes:
            rt.driver.open()

    @property
    def now(self) -> float:
        return self.base + self.k / self.rate

    def step(self, sample=None, n: int = 1) -> None:  # type: ignore[no-untyped-def]
        for _ in range(n):
            if sample is not None:
                self.engine.guard.offer(sample() if callable(sample) else sample, self.now)
            self.engine.step_all(self.now)
            self.k += 1


def texture(seed: int = 1, size: tuple[int, int] = (360, 2400)) -> np.ndarray:
    """A picture with plenty to track (blurred noise and a few shapes), wide enough to pan across."""
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur((rng.random((*size, 3)) * 255).astype(np.uint8), (0, 0), 2.0)
    img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)
    for _ in range(140):
        centre = (int(rng.integers(0, size[1])), int(rng.integers(0, size[0])))
        cv2.circle(img, centre, int(rng.integers(4, 18)), [int(c) for c in rng.integers(0, 255, 3)], -1)
    return img


def turned(tex: np.ndarray, yaw_left: float, width: int = 640, hfov_deg: float = 90.0) -> np.ndarray:
    """What a camera sees that has turned `yaw_left` radians to the left, the texture being a flat wall
    far in front of where it first looked (a true rotation: the picture stretches towards the edges)."""
    h, w = tex.shape[:2]
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    k_view = np.array([[f, 0, width / 2.0], [0, f, h / 2.0], [0, 0, 1.0]])
    k_wall = np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1.0]])
    c, s = math.cos(yaw_left), math.sin(yaw_left)
    to_first = np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])     # the turned camera's axes in the first view's
    return cv2.warpPerspective(tex, k_wall @ to_first @ np.linalg.inv(k_view), (width, h),
                               flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)


def pan(tex: np.ndarray, x: float, width: int = 640) -> np.ndarray:
    """The view of a camera that has turned so far that the scene has moved x pixels to the left."""
    m = np.float32([[1, 0, -x], [0, 1, 0]])
    return cv2.warpAffine(tex, m, (width, tex.shape[0]), flags=cv2.INTER_LINEAR)
