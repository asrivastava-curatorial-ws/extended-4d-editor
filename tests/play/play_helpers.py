"""Helpers shared by the interactive-show tests (a plain module, like live_helpers)."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from fourdvr_fx.patch import Patch
from fourdvr_live.config import LiveConfig
from fourdvr_motion.platform_config import load_platform_config
from fourdvr_play.config import FilmConfig, PlayConfig, SeatConfig
from fourdvr_play.engine import PlayEngine
from fourdvr_play.hub import Client, Hub
from fourdvr_play.logic import Graph, Reaction

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "config" / "platforms" / "example-electric-6dof.toml"
KEY = "operator-key"


def make_cfg(blocks: list[dict[str, Any]] | None = None, seats: int = 2, platforms: int = 0, teams: bool = True,
             film: bool = False, patch: Patch | None = None, **play: Any) -> PlayConfig:
    """A show with seats A1, A2 ... (teams red, blue, red ...), optionally on simulated platforms."""
    profile = replace(load_platform_config(PROFILE), driver="simulation")
    configs = [replace(profile, platform_id=f"PLATFORM-{k + 1:02d}", link_platform_id=k + 1) for k in range(platforms)]
    live = LiveConfig(platforms=configs, sound=False, source={"type": "none"}, patch=patch)
    cfg = PlayConfig(
        live=live, operator_key=KEY, countdown_s=play.pop("countdown_s", 0.0),
        seats=[SeatConfig(f"A{k + 1}", configs[k].platform_id if k < platforms else "", team=("red", "blue")[k % 2] if teams else "")
               for k in range(seats)],
        graph=Graph.from_list(blocks or []), film=FilmConfig("http://127.0.0.1:9") if film else None, **play)
    cfg.check()
    return cfg


class Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeEngine:
    """Records what the hub asks the engine to do."""

    def __init__(self) -> None:
        self.live = False
        self.reactions: list[tuple[tuple[str, ...], Reaction, bool]] = []
        self.offered: list[tuple[str, Any]] = []
        self.settings: dict[str, tuple[bool, str, tuple[str, ...]]] = {}
        self.held = False
        self.engaged = 0

    def set_live(self, on: bool) -> None:
        self.live = on

    def react(self, seats: Iterable[str], reaction: Reaction, house: bool = False) -> None:
        self.reactions.append((tuple(seats), reaction, house))

    def offer(self, seat: str, msg: Any, now: float) -> bool:
        self.offered.append((seat, msg))
        return True

    def configure(self, seat: str, enabled: bool, motion: str, blocked: Iterable[str]) -> None:
        self.settings[seat] = (enabled, motion, tuple(blocked))

    def hold(self, on: bool = True) -> None:
        self.held = on

    def engage(self) -> None:
        self.held = False
        self.engaged += 1


class Session:
    """A hub on a clock of its own, with clients that are plain objects."""

    def __init__(self, blocks: list[dict[str, Any]] | None = None, **kw: Any) -> None:
        self.cfg = make_cfg(blocks, **kw)
        self.clock = Clock()
        self.engine = FakeEngine()
        self.film_requests: list[tuple[str, int]] = []
        self.hub = Hub(self.cfg, self.engine, self.clock, film=self)      # type: ignore[arg-type]
        self.sent: list[tuple[Any, dict[str, Any]]] = []

    def request(self, action: str, programme: int = 0) -> None:
        self.film_requests.append((action, programme))

    def join(self, role: str = "player", seat: str | None = None, **more: Any) -> Client:
        hello = {"type": "hello", "play": 1, "role": role, **more}
        if seat is not None:
            hello["seat"] = seat
        if role == "operator":
            hello.setdefault("key", KEY)
        client, _welcome = self.hub.join(hello)
        return client

    def run(self, seconds: float, step: float = 0.05) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end - 1e-9:
            self.clock.now = min(end, self.clock.now + step)
            self.hub.step()
        self.sent += self.hub.drain()

    def start(self) -> None:
        self.hub.command("start")
        self.run(0.05)

    def messages(self, kind: str, client: Client | None = None) -> list[dict[str, Any]]:
        self.sent += self.hub.drain()
        return [m for to, m in self.sent if m["type"] == kind and (client is None or to.matches(client))]

    def clear(self) -> None:
        self.hub.drain()
        self.sent.clear()


class PlayStepper:
    """Drives a PlayEngine tick by tick on a clock of its own (see live_helpers.Stepper)."""

    def __init__(self, engine: PlayEngine, rate: float = 100.0) -> None:
        self.engine = engine
        self.rate = rate
        self.base = time.monotonic()
        self.k = 0
        for rt in engine.runtimes:
            rt.driver.open()

    @property
    def now(self) -> float:
        return self.base + self.k / self.rate

    def step(self, n: int = 1, each: Any = None) -> None:
        for _ in range(n):
            if each is not None:
                each(self.now)
            self.engine.step_all(self.now)
            self.k += 1


class ManualSource:
    """Made by ``manual_source()``: a shared source whose samples the test offers itself."""


def manual_source() -> Any:
    from fourdvr_live.sources.base import DEMO, LiveSource

    class _Manual(LiveSource):
        info = DEMO

        def run(self) -> None:
            return

    return _Manual()
