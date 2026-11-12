"""Fixtures for the live-mode tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from fourdvr_live.config import LiveConfig
from fourdvr_live.engine import LiveEngine
from fourdvr_live.sim import Drive
from fourdvr_motion.platform_config import PlatformConfig, load_platform_config
from live_helpers import PROFILE, ManualSource, Stepper


@pytest.fixture(scope="session")
def drive() -> Drive:
    return Drive()


@pytest.fixture
def profile() -> PlatformConfig:
    return replace(load_platform_config(PROFILE), driver="simulation")


@pytest.fixture
def make_engine(profile: PlatformConfig):  # type: ignore[no-untyped-def]
    def make(platforms: int = 1, **kw) -> tuple[LiveEngine, Stepper]:  # type: ignore[no-untyped-def]
        configs = [replace(profile, platform_id=f"PLATFORM-{i + 1:02d}", link_platform_id=i + 1) for i in range(platforms)]
        cfg = LiveConfig(platforms=configs, sound=kw.pop("sound", False), **kw)
        engine = LiveEngine(cfg, source=ManualSource(), audio_backend="null")
        return engine, Stepper(engine)

    return make


@pytest.fixture(scope="session")
def qapp():  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
