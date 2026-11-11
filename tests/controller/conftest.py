from __future__ import annotations

import pytest

from controller_helpers import SIM
from fourdvr_motion.platform_config import PlatformConfig, load_platform_config


@pytest.fixture(scope="session")
def platform_cfg() -> PlatformConfig:
    return load_platform_config(SIM)
