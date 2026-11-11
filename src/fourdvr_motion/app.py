"""Controller process: one real-time control loop + one server link per hosted platform."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from .config import ControllerConfig
from .content_cache import MotionCache
from .drivers import ActuatorDriver
from .effects_link import EffectsLink
from .runtime import ControlLoop, PlatformRuntime
from .server_link import ServerLink

log = logging.getLogger(__name__)


class ControllerApp:
    def __init__(
        self,
        cfg: ControllerConfig,
        drivers: dict[str, ActuatorDriver] | None = None,
        effects_output: Any = None,
    ) -> None:
        self.cfg = cfg
        self.cache = MotionCache(cfg.cache_dir)
        self.runtimes: list[PlatformRuntime] = []
        self.links: list[ServerLink] = []
        for entry in cfg.platforms:
            rt = PlatformRuntime(
                entry.config,
                cfg.control_rate_hz,
                self.cache,
                latency_compensation_s=cfg.latency_compensation_s,
                server_loss_timeout_s=cfg.server_loss_timeout_s,
                driver=(drivers or {}).get(entry.config.platform_id),
            )
            self.runtimes.append(rt)
            self.links.append(
                ServerLink(
                    cfg.server_url,
                    rt,
                    entry.device_key,
                    self.cache,
                    status_interval_s=cfg.status_interval_s,
                    time_sync_interval_s=cfg.time_sync_interval_s,
                )
            )
        self.loop = ControlLoop(self.runtimes, cfg.control_rate_hz)
        # Optional 4D effects (DMX, bass). Motion never depends on them: any failure here is reported
        # and the controller carries on with motion only.
        self.effects = EffectsLink.create(cfg, self.runtimes, self.cache, effects_output)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        self.loop.start()
        if self.effects is not None:
            self.effects.start()
        log.info(
            "motion controller running",
            extra={
                "platforms": ",".join(r.cfg.platform_id for r in self.runtimes),
                "rate_hz": self.cfg.control_rate_hz,
            },
        )
        tasks = [asyncio.create_task(link.run()) for link in self.links]
        try:
            await stop.wait()
        finally:
            for link in self.links:
                link.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.effects is not None:
                self.effects.stop()
            self.loop.stop()
            log.info("motion controller stopped")


def run_controller(cfg: ControllerConfig) -> None:  # pragma: no cover - process entry point
    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await ControllerApp(cfg).run(stop)

    asyncio.run(main())
