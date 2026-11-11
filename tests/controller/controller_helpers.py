"""Helpers shared by the controller tests (a plain module: several test directories have a conftest.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from fourdvr_motion import motionfile
from fourdvr_motion.content_cache import MotionCache
from fourdvr_motion.drivers import SimulationDriver
from fourdvr_motion.platform_config import PlatformConfig
from fourdvr_motion.runtime import PlatformRuntime

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "platforms" / "example-electric-6dof.toml"
SIM = ROOT / "config" / "platforms" / "simulation-6dof.toml"


def demo_samples(duration: float = 6.0, rate: float = 100.0) -> np.ndarray:
    """Smooth, limit-respecting test profile that starts and ends at neutral."""
    n = motionfile.expected_sample_count(duration, rate)
    t = np.arange(n) / rate
    env = np.sin(np.pi * t / duration) ** 2
    s = np.zeros((n, 6))
    s[:, 0] = 0.06 * env * np.sin(2 * np.pi * 0.4 * t)
    s[:, 2] = 0.03 * env * np.sin(2 * np.pi * 0.7 * t)
    s[:, 3] = 0.05 * env * np.sin(2 * np.pi * 0.3 * t)
    s[:, 4] = -0.06 * env * np.sin(2 * np.pi * 0.25 * t)
    s[:, 5] = 0.04 * env * np.sin(2 * np.pi * 0.2 * t)
    return s


def write_motion(path: Path, programme_id: int = 1, duration: float = 6.0, rate: float = 100.0) -> str:
    header = motionfile.build_header(
        programme_id=programme_id,
        duration_s=duration,
        sample_rate_hz=rate,
        video={"sha256": "c" * 64, "filename": "demo.mp4"},
        generation={"generator": "tests"},
    )
    return motionfile.write_motion(path, header, demo_samples(duration, rate))


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_runtime(cfg: PlatformConfig, tmp_path: Path, clock: FakeClock, rate: float = 100.0) -> PlatformRuntime:
    cfg = replace(cfg, driver="simulation")
    driver = SimulationDriver(cfg, clock=clock)
    driver.open()
    rt = PlatformRuntime(cfg, rate, MotionCache(tmp_path / "cache"), driver=driver, clock=clock)
    for _ in range(3):  # master clock = local clock + 5000 s
        rt.clock.add(clock(), clock() + 5000.0, clock() + 5000.0, clock())
    return rt


def run_for(rt: PlatformRuntime, clock: FakeClock, seconds: float, record: list | None = None) -> None:
    dt = 1.0 / rt.rate
    for _ in range(round(seconds / dt)):
        clock.advance(dt)
        rt.tick(clock())
        if record is not None:
            record.append((rt.clock.master_time(clock()), rt.pipeline.pose.copy(), rt.phase))
