"""Simulated players: a session can be tried, shown and load-tested without a game and without people.

Every seat gets a bot that says it is ready, presses its buttons now and then and answers questions
like a person would (after a moment, not always right). With ``game=True`` one more bot plays the
game's part: it sends the events the show's blocks wait for and a made-up drive as every seat's own
telemetry.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Any

from .client import PlayClient, PlayClientError
from .config import PlayConfig

THINK_S = (0.8, 4.0)         # how long a bot takes to answer


class Bots:
    def __init__(self, cfg: PlayConfig, url: str, game: bool = False, seed: int | None = None, press_every_s: float = 3.0,
                 key: str = "") -> None:
        self.cfg = cfg
        self.url = url
        self.game = game
        self.key = key
        self.press_every_s = press_every_s
        self.rng = random.Random(seed)
        self.sent: dict[str, int] = {"inputs": 0, "answers": 0, "events": 0, "motion": 0}
        self.errors: list[str] = []
        self.results: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def _count(self, what: str) -> None:
        with self._lock:
            self.sent[what] += 1

    # ------------------------------------------------------------------ one player
    def _player(self, seat: str, seed: int) -> None:
        rng = random.Random(seed)
        buttons = list(self.cfg.buttons)
        try:
            with PlayClient(self.url, role="player", seat=seat, name=f"Bot {seat}") as hub:
                hub.ready()
                next_press = time.monotonic() + rng.uniform(0.5, self.press_every_s)
                answer_at: tuple[float, str, int] | None = None
                running = hub.welcome["phase"]["phase"] == "running"
                while not self._stop.is_set():
                    message = hub.recv(timeout=0.05)
                    now = time.monotonic()
                    if message is not None:
                        kind = message.get("type")
                        if kind == "phase":
                            running = message["phase"] == "running"
                        elif kind == "prompt":
                            answer_at = (now + rng.uniform(*THINK_S), message["id"], rng.randrange(len(message["choices"])))
                        elif kind == "prompt_end":
                            answer_at = None
                        elif kind == "results":
                            self.results = message
                    if answer_at is not None and now >= answer_at[0]:
                        hub.answer(answer_at[2], prompt=answer_at[1])
                        self._count("answers")
                        answer_at = None
                    if running and buttons and now >= next_press:
                        hub.input(rng.choice(buttons))
                        self._count("inputs")
                        next_press = now + rng.expovariate(1.0 / self.press_every_s)
        except PlayClientError as exc:
            if not self._stop.is_set():
                self.errors.append(f"seat {seat}: {exc}")

    # ------------------------------------------------------------------ the game's part
    def _game(self) -> None:
        events = sorted({b.params["event"] for b in self.cfg.graph.blocks.values()
                         if b.type == "on_event" and b.params["event"] != "*"})
        seats = [s.id for s in self.cfg.seats]
        rng = random.Random(self.rng.random())
        try:
            with PlayClient(self.url, role="game", name="Bot game", key=self.key or self.cfg.game_key) as hub:
                started = time.monotonic()
                next_event = started + 2.0
                next_motion = started
                running = hub.welcome["phase"]["phase"] == "running"
                while not self._stop.is_set():
                    message = hub.recv(timeout=0.005)
                    if message is not None and message.get("type") == "phase":
                        running = message["phase"] == "running"
                    now = time.monotonic()
                    if running and seats and now >= next_motion:
                        next_motion = now + 0.02
                        t = now - started
                        for k, seat in enumerate(seats):          # every seat its own little flight
                            phase = t * 0.6 + k * 1.7
                            hub.motion(seat, accel=[1.5 * math.sin(phase), 2.0 * math.sin(phase * 0.7), 0.8 * math.sin(phase * 1.9)],
                                       rates=[0.0, 0.0, 0.25 * math.cos(phase * 0.7)], speed=20.0 + 10.0 * math.sin(phase * 0.3),
                                       rumble=0.2)
                            self._count("motion")
                    if running and events and seats and now >= next_event:
                        next_event = now + rng.uniform(1.0, 4.0)
                        hub.event(rng.choice(events), seat=rng.choice(seats), value=round(rng.random(), 2))
                        self._count("events")
        except PlayClientError as exc:
            if not self._stop.is_set():
                self.errors.append(f"game: {exc}")

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._stop.clear()
        for seat in self.cfg.seats:
            self._threads.append(threading.Thread(target=self._player, args=(seat.id, self.rng.randrange(1 << 30)),
                                                  name=f"bot-{seat.id}", daemon=True))
        if self.game:
            self._threads.append(threading.Thread(target=self._game, name="bot-game", daemon=True))
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads.clear()

    def run(self, seconds: float = 0.0) -> None:
        self.start()
        try:
            end = time.monotonic() + seconds if seconds else None
            while end is None or time.monotonic() < end:
                if all(not t.is_alive() for t in self._threads):
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
