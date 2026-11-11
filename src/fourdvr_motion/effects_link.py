"""Glue between the motion controller and the optional effects service (fourdvr_fx).

The controller works without this module's service: no ``[effects]`` section, a broken patch, a
programme without an effects section, an unreachable DMX node or a missing sound card all leave
motion untouched. The link only *reads* the platform runtimes:

* a platform fixture follows its own platform and is off unless that platform is playing;
* theatre fixtures and the bass output follow the reference platform if it is playing, otherwise
  the first platform that is.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from fourdvr_fx.bridge import BridgeOutput
from fourdvr_fx.dmx import DmxOutput
from fourdvr_fx.engine import THEATRE, ShowState
from fourdvr_fx.patch import PatchError, load_patch
from fourdvr_fx.service import EffectsService

from .config import ControllerConfig
from .content_cache import MotionCache
from .runtime import LoadedProgramme, PlatformRuntime

log = logging.getLogger(__name__)


def link_hosts(runtimes: list[PlatformRuntime]) -> dict[str, str]:
    """The ESP32 of each platform: the host of its motion link (udp://host:port). Platforms on a serial
    link or in simulation have none - their effects need an address in the patch, or are left out."""
    hosts: dict[str, str] = {}
    for rt in runtimes:
        url = urlparse(getattr(rt.cfg, "address", "") or "")
        if url.scheme == "udp" and url.hostname:
            hosts[rt.cfg.platform_id] = url.hostname
    return hosts


class EffectsLink:
    def __init__(
        self,
        service: EffectsService | None,
        runtimes: list[PlatformRuntime],
        cache: MotionCache,
        reference_platform: str = "",
        error: str = "",
    ) -> None:
        self.service = service
        self.runtimes = runtimes
        self.cache = cache
        self.reference = reference_platform
        self.error = error
        self.warnings: list[str] = []
        self._theatre_key = ""
        for rt in runtimes:
            rt.effects_status = self.platform_status
            rt.on_programme = self.prepare

    @classmethod
    def create(
        cls,
        cfg: ControllerConfig,
        runtimes: list[PlatformRuntime],
        cache: MotionCache,
        output: DmxOutput | None = None,
    ) -> EffectsLink | None:
        """None when effects are not configured; otherwise a link, possibly without a service (error)."""
        fx = cfg.effects
        if fx is None:
            return None
        ids = [rt.cfg.platform_id for rt in runtimes]
        if not fx.enabled:
            return cls(None, runtimes, cache, fx.reference_platform, "disabled in the configuration")
        try:
            patch = load_patch(fx.patch_path)
            link = cls(None, runtimes, cache, fx.reference_platform)
            link.service = EffectsService(patch, ids, link.states, output=output, hosts=link_hosts(runtimes))
            link.warnings = patch.warnings()
            for w in link.warnings:
                log.warning("effects patch: %s", w)
            return link
        except (PatchError, ValueError, OSError) as exc:
            log.error("effects disabled: %s", exc)
            return cls(None, runtimes, cache, fx.reference_platform, str(exc))

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.service is None:
            return
        try:
            self.service.start()
            log.info("effects running", extra={"output": self.service.output.name,
                                               "fixtures": len(self.service.engine.instances)})
        except Exception as exc:  # noqa: BLE001 - effects must never prevent the controller from starting
            self.error = f"{type(exc).__name__}: {exc}"
            self.service = None
            log.error("effects disabled: %s", self.error)

    def stop(self) -> None:
        if self.service is not None:
            try:
                self.service.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("stopping effects: %s", exc)

    # ------------------------------------------------------------------ show state for the engine
    def _state(self, rt: PlatformRuntime, now: float) -> ShowState:
        playing, prog, position, duration, loop = rt.show_position(now)
        if not playing or prog is None or not prog.sha256:
            return ShowState()
        return ShowState(True, self.cache.effects(prog.sha256), position, duration, loop, rt.run_counter)

    def states(self, now: float) -> dict[str, ShowState]:
        shows = {rt.cfg.platform_id: self._state(rt, now) for rt in self.runtimes}
        theatre = shows.get(self.reference) if self.reference else None
        key = self.reference
        if theatre is None or not theatre.active:
            key, theatre = next(((k, s) for k, s in shows.items() if s.active), ("", ShowState()))
        self._theatre_key = key if theatre.active else ""
        shows[THEATRE] = theatre
        return shows

    def prepare(self, programme: LoadedProgramme) -> None:
        """Parse the effects section and resample the bass track when a programme is prepared."""
        if self.service is None or not programme.sha256:
            return
        doc = self.cache.effects(programme.sha256)
        if doc is not None:
            self.service.prepare(doc)

    # ------------------------------------------------------------------ reporting (STATUS "effects")
    def platform_status(self, rt: PlatformRuntime) -> dict[str, Any]:
        svc = self.service
        if svc is None:
            return {"running": False, "error": self.error or "not running"}
        playing, prog, _position, _duration, _loop = rt.show_position(time.monotonic())
        sha = prog.sha256 if prog is not None else ""
        doc = self.cache.effects(sha) if sha else None
        pid = rt.cfg.platform_id
        return {
            "running": svc.running,
            "playing": bool(playing and doc is not None),
            "programme_has_effects": doc is not None,
            "programme_error": self.cache.effects_errors.get(sha),
            "output": svc.output.name,
            "output_errors": svc.output.errors,
            "errors": svc.errors,
            "last_error": svc.last_error or svc.output.last_error or None,
            "overruns": svc.overruns,
            "limited": [k for k in svc.engine.limited() if "@" not in k or k.endswith("@" + pid)],
            "theatre_follows": self._theatre_key or None,
            "audio": svc.status().get("audio"),
            # effects through ESP32s: which of them answer and what each has (a missing one is not an error)
            **({"nodes": svc.output.describe()["nodes"]} if isinstance(svc.output, BridgeOutput) else {}),
        }
