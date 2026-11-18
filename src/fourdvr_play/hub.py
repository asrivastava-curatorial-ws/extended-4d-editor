"""The session of an interactive show: who sits where, what phase it is in, scores, shared values,
prompts - and the logic blocks that tie them together.

The hub does no I/O. Clients' messages come in through ``join`` / ``handle``, time through ``step``,
a film's position through ``film_update``; what has to be sent is collected and fetched with
``drain`` (the server delivers it), what a seat has to feel goes to the engine. A clock is passed
in, so the tests run a whole session in no time.

Phases:  lobby -> countdown -> running <-> paused -> results -> (reset) lobby.
Seats move and react only while the session is *running*.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import math
import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import PROTOCOL
from .config import MOTION, PlayConfig
from .logic import ANSWERS, NAME_PATTERN, Block, Logic, LogicError, Reaction, Signal, make_block, reaction_of

ROLES = ("game", "player", "headset", "display", "operator")
STAFF = frozenset(("game", "operator", "display"))
SEATED = ("player", "headset")
MAX_CLIENTS = 256
MAX_STATE = 64
SEATS_EVERY_S = 0.2          # the seat list is sent at most this often
LOG_LINES = 200


class PlayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Actuator(Protocol):
    """The engine, as the hub sees it."""

    def set_live(self, on: bool) -> None: ...
    def react(self, seats: Iterable[str], reaction: Reaction, house: bool = False) -> None: ...
    def offer(self, seat: str, msg: Any, now: float) -> bool: ...
    def configure(self, seat: str, enabled: bool, motion: str, blocked: Iterable[str]) -> None: ...
    def hold(self, on: bool = True) -> None: ...
    def engage(self) -> None: ...


class FilmControl(Protocol):
    def request(self, action: str, programme: int = 0) -> None: ...


@dataclass(frozen=True)
class FilmState:
    ok: bool = False
    state: str = ""                # PLAYING, PAUSED, STOPPED, IDLE, PREPARING
    programme: int = 0
    position_s: float = 0.0        # where the picture is (it stands while a start or a resume is only scheduled)
    name: str = ""
    problem: str = ""
    starts_in_s: float = 0.0       # PLAYING, but (re)starting in so many seconds: the Control Server schedules ahead


@dataclass(frozen=True)
class To:
    """Who gets a message."""

    everyone: bool = False
    client: int = 0
    seats: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()

    def matches(self, client: Client) -> bool:
        return (self.everyone or client.id == self.client or client.role in self.roles
                or (client.seat is not None and client.seat in self.seats))


EVERYONE = To(everyone=True)
TO_STAFF = To(roles=STAFF)


@dataclass
class Client:
    id: int
    role: str
    seat: str | None = None
    name: str = ""
    peer: str = ""
    tokens: float = 0.0
    stamp: float = 0.0
    dropped: int = 0
    warned: float = -1e9
    messages: int = 0

    def take(self, now: float, rate: float) -> bool:
        self.tokens = min(rate, self.tokens + (now - self.stamp) * rate)
        self.stamp = now
        if self.tokens < 1.0:
            self.dropped += 1
            return False
        self.tokens -= 1.0
        return True


@dataclass
class Seat:
    id: str
    platform: str = ""
    name: str = ""
    team: str = ""
    motion: str = "full"
    blocked: tuple[str, ...] = ()
    enabled: bool = True
    ready: bool = False
    clients: int = 0
    headsets: int = 0
    score: float = 0.0
    events: int = 0
    inputs: int = 0
    answers: int = 0
    right: int = 0
    reactions: int = 0
    held: set[str] = field(default_factory=set)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "team": self.team, "platform": self.platform, "enabled": self.enabled,
                "motion": self.motion, "blocked": list(self.blocked), "ready": self.ready, "clients": self.clients,
                "headset": self.headsets > 0, "score": _plain(self.score)}


@dataclass
class OpenPrompt:
    id: str
    block: Block | None
    text: str
    choices: tuple[str, ...]
    seconds: float
    mode: str
    correct: int                   # index of the right answer, -1: none
    points: float
    seats: list[str]
    left: float
    answers: dict[str, int] = field(default_factory=dict)
    asked_by: int = 0              # the client that asked (a game), 0: a block

    def public(self) -> dict[str, Any]:
        return {"type": "prompt", "id": self.id, "text": self.text, "choices": list(self.choices),
                "seconds": self.seconds, "left": round(max(0.0, self.left), 2), "mode": self.mode}


def _plain(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(float(value), 3)


def _number(value: Any, what: str, low: float = -1e9, high: float = 1e9) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise PlayError("bad_message", f"{what} must be a number")
    return min(high, max(low, float(value)))


def _same(given: Any, wanted: str) -> bool:
    """A key comparison that takes equally long whatever was given; no key set means nobody gets in."""
    return bool(wanted) and isinstance(given, str) and hmac.compare_digest(given.encode(), wanted.encode())


def state_text(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


class Hub:
    def __init__(self, cfg: PlayConfig, engine: Actuator | None = None, clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None, film: FilmControl | None = None) -> None:
        self.cfg = cfg
        self.engine = engine
        self.clock = clock
        self.film_control = film
        self.film_mode = cfg.film is not None
        self.seats: dict[str, Seat] = {
            s.id: Seat(s.id, s.platform, s.name, s.team, s.motion, tuple(s.blocked)) for s in cfg.seats}
        self.by_platform = {s.platform: s.id for s in cfg.seats if s.platform}
        self.clients: dict[int, Client] = {}
        self.logic = Logic(cfg.graph, self, rng)
        self.phase = "lobby"
        self.session_s = 0.0
        self.sessions = 0
        self.state: dict[str, Any] = {}
        self.team_bonus: dict[str, float] = {}
        self.prompts: dict[str, OpenPrompt] = {}
        self.asked: list[dict[str, Any]] = []
        self.log: list[str] = []
        self.film_state = FilmState()
        self.last_results: dict[str, Any] | None = None
        self.held = False
        self._lock = threading.RLock()
        self._out: list[tuple[To, dict[str, Any]]] = []
        self._next_client = 1
        self._next_prompt = 1
        self._count_left = 0.0
        self._last = clock()
        self._seats_dirty = False
        self._seats_due = 0.0
        self._buttons: dict[str, int] = {}
        self._film_lost: float | None = None
        self.film_held = False                    # the film stands because a block paused it: the session goes on
        self._hold_asked: float | None = None     # when a block asked for that pause
        self._nesting = 0
        for seat in self.seats.values():
            self._configure(seat)
        if engine is not None and hasattr(engine, "on_seat_inputs"):
            engine.on_seat_inputs = self.node_inputs          # the seat boards' buttons, through the effects link

    # ================================================================== sending
    def _send(self, to: To, message: dict[str, Any]) -> None:
        self._out.append((to, message))

    def drain(self) -> list[tuple[To, dict[str, Any]]]:
        with self._lock:
            out, self._out = self._out, []
            return out

    def note(self, text: str) -> None:
        self.log.append(f"{dt.datetime.now().strftime('%H:%M:%S')}  {text}")
        del self.log[:-LOG_LINES]
        self._send(To(roles=frozenset(("operator",))), {"type": "log", "text": self.log[-1]})

    def _seat_list(self) -> dict[str, Any]:
        return {"type": "seats", "seats": [s.public() for s in self.seats.values()],
                "teams": {name: _plain(total) for name, total in self.team_scores().items()}}

    def _phase_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"type": "phase", "phase": self.phase, "time": round(self.session_s, 2), "held": self.held}
        if self.phase == "countdown":
            msg["seconds"] = round(max(0.0, self._count_left), 2)
        if self.cfg.session_minutes > 0.0:
            msg["remaining"] = round(max(0.0, self.cfg.session_minutes * 60.0 - self.session_s), 1)
        return msg

    # ================================================================== clients
    def join(self, hello: Any, peer: str = "") -> tuple[Client, dict[str, Any]]:
        """A new connection's first message. Returns the client and the welcome to send it."""
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise PlayError("hello", 'the first message must be {"type": "hello", "play": 1, "role": ...}')
        if hello.get("play") != PROTOCOL:
            raise PlayError("protocol", f"this hub speaks protocol {PROTOCOL} (\"play\": {PROTOCOL})")
        role = hello.get("role", "player")
        if role not in ROLES:
            raise PlayError("role", f"role must be one of {', '.join(ROLES)}")
        key = hello.get("key", "")
        with self._lock:
            if role == "operator" and not _same(key, self.cfg.operator_key):
                raise PlayError("key", "the operator key is not right")
            if role == "game" and self.cfg.game_key and not _same(key, self.cfg.game_key):
                raise PlayError("key", "the game key is not right")
            if len(self.clients) >= MAX_CLIENTS:
                raise PlayError("full", "too many connections")
            seat_id = hello.get("seat") if role in SEATED else None
            if role in SEATED and (not isinstance(seat_id, str) or seat_id not in self.seats):
                raise PlayError("seat", f"there is no seat {seat_id!r} (seats: {', '.join(self.seats) or 'none'})")
            now = self.clock()
            client = Client(self._next_client, role, seat_id, str(hello.get("name", ""))[:40], peer,
                            tokens=self.cfg.max_events_per_s, stamp=now)
            self._next_client += 1
            self.clients[client.id] = client
            if seat_id is not None:
                seat = self.seats[seat_id]
                seat.clients += 1
                seat.headsets += role == "headset"
                if client.name and not seat.name:
                    seat.name = client.name
                self._seats_dirty = True
            self.note(f"{role} connected" + (f" for seat {seat_id}" if seat_id else "") + (f" from {peer}" if peer else ""))
            return client, self._welcome(client)

    def _welcome(self, client: Client) -> dict[str, Any]:
        from fourdvr_studio import __version__

        prompt = next((p for p in self.prompts.values() if client.seat in p.seats or client.role in STAFF), None)
        return {"type": "welcome", "play": PROTOCOL, "version": __version__, "title": self.cfg.title, "client": client.id,
                "role": client.role, "seat": client.seat, "phase": self._phase_message(), **{
                    k: v for k, v in self._seat_list().items() if k != "type"},
                "state": dict(self.state), "buttons": list(self.cfg.buttons), "film": self.film_mode,
                "prompt": prompt.public() if prompt else None, "results": self.last_results if self.phase == "results" else None}

    def leave(self, client: Client) -> None:
        with self._lock:
            if self.clients.pop(client.id, None) is None:
                return
            if client.seat is not None and client.seat in self.seats:
                seat = self.seats[client.seat]
                seat.clients = max(0, seat.clients - 1)
                seat.headsets = max(0, seat.headsets - (client.role == "headset"))
                if seat.clients == 0:
                    for button in sorted(seat.held):           # a page that went away lets go of its buttons
                        self.input(seat.id, button, False)
                self._seats_dirty = True
            self.note(f"{client.role} disconnected" + (f" (seat {client.seat})" if client.seat else ""))

    def _seat_of(self, client: Client, msg: dict[str, Any], needed: bool = True) -> Seat | None:
        if client.role in SEATED:
            return self.seats[client.seat] if client.seat in self.seats else None
        seat_id = msg.get("seat")
        if seat_id is None and not needed:
            return None
        if not isinstance(seat_id, str) or seat_id not in self.seats:
            raise PlayError("seat", f"there is no seat {seat_id!r}")
        return self.seats[seat_id]

    def _staff(self, client: Client, what: str, *roles: str) -> None:
        if client.role not in (roles or ("game", "operator")):
            raise PlayError("not_allowed", f"a {client.role} may not {what}")

    def handle(self, client: Client, msg: Any) -> None:
        """One message of a client. Problems go back to that client as an "error" message."""
        with self._lock:
            try:
                if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                    raise PlayError("bad_message", 'a message is an object with a "type"')
                client.messages += 1
                self._handle(client, msg["type"], msg)
            except PlayError as exc:
                self._send(To(client=client.id), {"type": "error", "code": exc.code, "message": exc.message})
            except LogicError as exc:
                self._send(To(client=client.id), {"type": "error", "code": "bad_message", "message": str(exc)})

    def _handle(self, client: Client, kind: str, msg: dict[str, Any]) -> None:
        now = self.clock()
        if kind == "ping":
            self._send(To(client=client.id), {"type": "pong", "t": msg.get("t"), "time": round(self.session_s, 3)})
            return
        if kind == "motion":
            if client.role == "display":
                raise PlayError("not_allowed", "a display may not send motion")
            seat = self._seat_of(client, msg)
            if seat is not None and self.engine is not None:
                self.engine.offer(seat.id, msg, now)
            return
        if kind == "boards":                 # a show service passes on its boards' seat buttons: every seat's presses
            self._staff(client, "report seat buttons")       # come through this one client, the boards debounce them
            platform, bits = msg.get("platform"), msg.get("bits")
            if not isinstance(platform, str) or isinstance(bits, bool) or not isinstance(bits, int) or not 0 <= bits <= 255:
                raise PlayError("bad_message", 'boards needs "platform" (its id) and "bits" (0-255: the inputs pressed)')
            if platform not in self.by_platform:
                raise PlayError("unknown_seat", f"no seat sits on platform {platform!r}")
            self.node_inputs(platform, bits)
            return
        if not client.take(now, self.cfg.max_events_per_s):
            if now - client.warned >= 1.0:
                client.warned = now
                raise PlayError("slow_down", f"more than {self.cfg.max_events_per_s:g} messages a second: some were left out")
            return
        if kind == "event":
            if client.role == "display":
                raise PlayError("not_allowed", "a display may not send events")
            name = msg.get("name")
            if not isinstance(name, str) or not NAME_PATTERN.match(name):
                raise PlayError("bad_message", "an event needs a name (letters, digits, . _ : -; at most 40)")
            seat = self._seat_of(client, msg, needed=False)
            self.event(name, seat.id if seat else None, _number(msg.get("value", 0.0), "value"), client.role)
        elif kind == "input":
            if client.role == "display":
                raise PlayError("not_allowed", "a display has no buttons")
            button = msg.get("button")
            if not isinstance(button, str) or not NAME_PATTERN.match(button):
                raise PlayError("bad_message", "an input needs a button name")
            seat = self._seat_of(client, msg)
            if seat is not None and "pressed" in msg:
                self.input(seat.id, button, bool(msg["pressed"]))
            elif seat is not None:                           # no "pressed": a tap
                self.input(seat.id, button, True)
                self.input(seat.id, button, False)
        elif kind == "answer":
            seat = self._seat_of(client, msg)
            if seat is not None:
                self.answer(seat.id, msg.get("prompt"), msg.get("choice"))
        elif kind == "ready":
            seat = self._seat_of(client, msg)
            if seat is not None:
                seat.ready = bool(msg.get("ready", True))
                self._seats_dirty = True
        elif kind == "seat":
            self._seat_settings(client, msg)
        elif kind == "state":
            self._staff(client, "set shared values")
            key = msg.get("key")
            if not isinstance(key, str) or not NAME_PATTERN.match(key):
                raise PlayError("bad_message", "a shared value needs a key (letters, digits, . _ : -)")
            value = msg.get("value")
            if isinstance(value, bool | int | float | str) and (not isinstance(value, str) or len(value) <= 200):
                self._set_state(key, float(value) if isinstance(value, int | float) and not isinstance(value, bool) else value)
            else:
                raise PlayError("bad_message", "a shared value is a number, a short text or true/false")
        elif kind == "score":
            self._staff(client, "change scores")
            points = _number(msg.get("add", msg.get("set", 0.0)), "points", -1e6, 1e6)
            if "team" in msg:
                team = str(msg["team"])
                if team not in self.team_scores():
                    raise PlayError("seat", f"there is no team {team!r}")
                before = self.team_scores()[team]
                self.team_bonus[team] = self.team_bonus.get(team, 0.0) + (points if "add" in msg else points - before)
                self._seats_dirty = True
                self.logic.score_changed("team", team, before, self.team_scores()[team])
            else:
                seat = self._seat_of(client, msg)
                if seat is not None:
                    self._score([seat.id], points if "add" in msg else points - seat.score)
        elif kind == "phase":
            self._staff(client, "run the session")
            self.command(str(msg.get("to", "")), client.role)
        elif kind == "react":
            self._staff(client, "move seats")
            block = make_block({"id": "direct", "type": "react", **{k: v for k, v in msg.items() if k in (
                "accel", "rates", "seconds", "effects", "effect_seconds")}})
            target = msg.get("seat", "all")
            if target != "all" and target not in self.seats:
                raise PlayError("seat", f"there is no seat {target!r}")
            self.react("all" if target == "all" else "seat", Signal(seat=None if target == "all" else target), block)
        elif kind == "prompt":
            self._staff(client, "ask")
            block = make_block({"id": "direct", "type": "prompt", **{k: v for k, v in msg.items() if k in (
                "text", "choices", "seconds", "mode", "correct", "points")}})
            target = msg.get("seat", "all")
            if target != "all" and target not in self.seats:
                raise PlayError("seat", f"there is no seat {target!r}")
            block.params["to"] = "all" if target == "all" else "seat"
            self._open(block, Signal(seat=None if target == "all" else target), None, client.id)
        elif kind == "hold":
            self._staff(client, "hold the show", "operator")
            self.hold(bool(msg.get("on", True)))
        else:
            raise PlayError("bad_message", f"unknown message type {kind!r}")

    def _seat_settings(self, client: Client, msg: dict[str, Any]) -> None:
        seat = self._seat_of(client, msg)
        if seat is None:
            return
        operator = client.role == "operator"
        if not operator and client.role not in SEATED:
            raise PlayError("not_allowed", f"a {client.role} may not change seats")
        if "name" in msg:
            seat.name = str(msg["name"]).strip()[:40]
        if "motion" in msg:
            if msg["motion"] not in MOTION:
                raise PlayError("bad_message", f"motion is one of {', '.join(MOTION)}")
            seat.motion = str(msg["motion"])
        if "blocked" in msg:
            blocked = msg["blocked"]
            if not isinstance(blocked, list) or len(blocked) > 16 or any(not isinstance(b, str) or not NAME_PATTERN.match(b) for b in blocked):
                raise PlayError("bad_message", "blocked is a list of effect names")
            seat.blocked = tuple(dict.fromkeys(blocked))
        if operator:
            if "enabled" in msg:
                seat.enabled = bool(msg["enabled"])
                if not seat.enabled:
                    seat.ready = False
            if "team" in msg:
                team = str(msg["team"]).strip()
                if team and not NAME_PATTERN.match(team):
                    raise PlayError("bad_message", "a team name holds letters, digits and . _ : - only")
                seat.team = team
        elif "enabled" in msg or "team" in msg:
            raise PlayError("not_allowed", "only the operator switches a seat on or off or changes its team")
        self._configure(seat)
        self._seats_dirty = True

    def _configure(self, seat: Seat) -> None:
        if self.engine is not None:
            self.engine.configure(seat.id, seat.enabled, seat.motion, seat.blocked)

    # ================================================================== what happens
    def event(self, name: str, seat_id: str | None = None, value: float = 0.0, source: str = "game") -> int:
        """An event from the game or a page: counted, shown to the staff, and given to the logic."""
        with self._lock:
            seat = self.seats.get(seat_id) if seat_id else None
            if seat is not None:
                seat.events += 1
            self._send(To(roles=frozenset(("operator",))), {"type": "event", "name": name, "seat": seat_id,
                                                           "value": _plain(value), "from": source})
            return self.logic.trigger("on_event", Signal(seat_id, seat.team if seat else "", value, name=name), event=name)

    def input(self, seat_id: str, button: str, pressed: bool = True) -> None:
        """A button of a seat (its board, a phone page, a controller)."""
        with self._lock:
            seat = self.seats.get(seat_id)
            if seat is None or (pressed and button in seat.held) or (not pressed and button not in seat.held):
                return
            (seat.held.add if pressed else seat.held.discard)(button)
            if not seat.enabled:
                return
            seat.inputs += 1
            self._send(To(roles=frozenset(("game", "operator"))), {"type": "input", "seat": seat_id, "button": button,
                                                                 "pressed": pressed})
            if pressed and button in ANSWERS:
                prompt = self._prompt_for(seat_id)
                if prompt is not None and ANSWERS.index(button) < len(prompt.choices):
                    self.answer(seat_id, prompt.id, ANSWERS.index(button))
                    return
            self.logic.trigger("on_input", Signal(seat_id, seat.team, 1.0 if pressed else 0.0, name=button),
                               button=button, edge="press" if pressed else "release")

    def node_inputs(self, platform_id: str, bits: int) -> None:
        """The inputs of a seat's board, as it reports them (bit 0: input 1 ...)."""
        with self._lock:
            seat_id = self.by_platform.get(platform_id)
            if seat_id is None:
                return
            before = self._buttons.get(platform_id, 0)
            self._buttons[platform_id] = bits
            for k, name in enumerate(self.cfg.buttons):
                if (before ^ bits) >> k & 1:
                    self.input(seat_id, name, bool(bits >> k & 1))

    def motion(self, seat_id: str, msg: Any) -> bool:
        """A seat's telemetry (the open live format with a "seat" key), e.g. from the UDP port."""
        return self.engine is not None and seat_id in self.seats and self.engine.offer(seat_id, msg, self.clock())

    def hold(self, on: bool) -> None:
        with self._lock:
            self.held = bool(on)
            if self.engine is not None:
                if on:
                    self.engine.hold(True)
                else:
                    self.engine.engage()
            self.note("held by the operator: nothing moves" if on else "released by the operator")
            self._send(EVERYONE, self._phase_message())

    # ================================================================== Host: what the blocks do
    def active_seats(self) -> list[str]:
        return [s.id for s in self.seats.values() if s.enabled]

    def team_of(self, seat: str) -> str:
        found = self.seats.get(seat)
        return found.team if found else ""

    def team_scores(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for seat in self.seats.values():
            if seat.team:
                totals[seat.team] = totals.get(seat.team, 0.0) + seat.score
        for team, bonus in self.team_bonus.items():
            if team in totals:
                totals[team] += bonus
        return totals

    def targets(self, to: str, signal: Signal) -> list[str]:
        active = self.active_seats()
        team = signal.team or (self.team_of(signal.seat) if signal.seat else "")
        if to == "all":
            return active
        if to == "others":
            return [s for s in active if s != signal.seat]
        if to == "team" and team:
            return [s for s in active if self.seats[s].team == team]
        if signal.seat is None:
            return [s for s in active if self.seats[s].team == signal.team] if signal.team else active
        return [signal.seat] if signal.seat in active else []

    def _score(self, seat_ids: Iterable[str], points: float) -> None:
        if not points:
            return
        teams_before = self.team_scores()
        changed: list[tuple[str, float, float]] = []
        for seat_id in seat_ids:
            seat = self.seats[seat_id]
            before, seat.score = seat.score, seat.score + points
            changed.append((seat_id, before, seat.score))
            self._send(To(seats=frozenset((seat_id,))), {"type": "score", "seat": seat_id, "score": _plain(seat.score),
                                                        "delta": _plain(points)})
        self._seats_dirty = True
        self._after_score(changed, teams_before)

    def _after_score(self, changed: list[tuple[str, float, float]], teams_before: dict[str, float]) -> None:
        if self._nesting >= 8:              # a score that sets off a score that sets off a score ...
            self.logic.problem = "scores set each other off more than 8 deep; the rest was left out"
            return
        self._nesting += 1
        try:
            for seat_id, before, after in changed:
                self.logic.score_changed("seat", seat_id, before, after)
            for team, total in self.team_scores().items():
                if total != teams_before.get(team, 0.0):
                    self.logic.score_changed("team", team, teams_before.get(team, 0.0), total)
        finally:
            self._nesting -= 1

    def add_score(self, to: str, signal: Signal, points: float) -> None:
        team = signal.team or (self.team_of(signal.seat) if signal.seat else "")
        if to == "team" and team:
            before = self.team_scores()
            self.team_bonus[team] = self.team_bonus.get(team, 0.0) + points
            self._seats_dirty = True
            self._after_score([], before)
            return
        self._score(self.targets(to, signal), points)

    def react(self, to: str, signal: Signal, block: Block) -> None:
        seats = self.targets(to, signal)
        reaction = reaction_of(block, self.cfg.limits)
        for seat_id in seats:
            self.seats[seat_id].reactions += 1
        if self.engine is not None and (seats or to == "all"):
            self.engine.react(seats, reaction, house=to == "all")
        if seats:
            self._send(To(seats=frozenset(seats), roles=STAFF), {
                "type": "react", "seats": seats, "push": reaction.moves, "effects": sorted(reaction.effects),
                "seconds": max(reaction.seconds if reaction.moves else 0.0, reaction.effect_seconds if reaction.effects else 0.0)})

    def change_state(self, key: str, value: str, add: float) -> None:
        if value != "":
            try:
                self._set_state(key, float(value))
            except ValueError:
                self._set_state(key, value)
        else:
            old = self.state.get(key, 0.0)
            self._set_state(key, (old if isinstance(old, float) else 0.0) + add)

    def _set_state(self, key: str, value: Any) -> None:
        if key not in self.state and len(self.state) >= MAX_STATE:
            raise PlayError("full", f"more than {MAX_STATE} shared values")
        if self.state.get(key) == value and key in self.state:
            return
        self.state[key] = value
        self._send(EVERYONE, {"type": "state", "key": key, "value": _plain(value) if isinstance(value, float) else value})
        if self._nesting >= 8:
            self.logic.problem = "shared values set each other off more than 8 deep; the rest was left out"
            return
        self._nesting += 1
        try:
            self.logic.trigger("on_state", Signal(value=value if isinstance(value, float) else 0.0, name=key),
                               key=key, value=state_text(value))
        finally:
            self._nesting -= 1

    def send_event(self, name: str, to: str, signal: Signal) -> None:
        seats = self.targets(to, signal)
        self._send(To(seats=frozenset(seats), roles=STAFF), {
            "type": "event", "name": name, "seat": signal.seat if to == "seat" else None, "seats": seats,
            "value": _plain(signal.value), "choice": signal.choice, "from": "logic"})

    def film(self, action: str, programme: int) -> None:
        if self.film_control is None:
            self.note(f"film {action}: no Control Server is configured ([play.film])")
            return
        self.film_control.request(action, programme)
        if action == "pause":                     # a decision point: the film waits, the session and its questions go on
            self._hold_asked = self.clock()
        self.note(f"film: {action}" + (f" programme {programme}" if action == "play" else ""))

    def change_phase(self, to: str) -> None:
        if self.film_mode:
            self.note(f"a block asked to {to} the session, but the film leads it: use a 'film' block")
            return
        try:
            self.command(to, "logic")
        except PlayError:
            pass

    # ================================================================== prompts
    def _prompt_for(self, seat_id: str) -> OpenPrompt | None:
        return next((p for p in self.prompts.values() if seat_id in p.seats), None)

    def open_prompt(self, block: Block, signal: Signal) -> None:
        self._open(block, signal, block, 0)

    def _open(self, block: Block, signal: Signal, origin: Block | None, asked_by: int) -> None:
        p = block.params
        seats = self.targets(p["to"], signal)
        choices = tuple(p["choices"])
        correct = ANSWERS.index(p["correct"]) if p["correct"] in ANSWERS[: len(choices)] else -1
        prompt = OpenPrompt(f"q{self._next_prompt}", origin, p["text"], choices, p["seconds"], p["mode"], correct,
                            p["points"], seats, p["seconds"], asked_by=asked_by)
        self._next_prompt += 1
        if not seats:
            self.note(f"not asked (no seat to ask): {prompt.text}")
            return
        for other in [o for o in self.prompts.values() if set(o.seats) & set(seats)]:
            del self.prompts[other.id]                      # one question at a time for a seat: the new one replaces it
            self._send(To(seats=frozenset(other.seats), roles=STAFF), {"type": "prompt_end", "id": other.id, "cancelled": True})
        self.prompts[prompt.id] = prompt
        self._send(To(seats=frozenset(seats), roles=STAFF), prompt.public())
        self.note(f"asked: {prompt.text} ({len(seats)} seat(s), {prompt.mode})")

    def answer(self, seat_id: str, prompt_id: Any, choice: Any) -> None:
        with self._lock:
            prompt = self.prompts.get(str(prompt_id)) if prompt_id is not None else self._prompt_for(seat_id)
            if prompt is None or seat_id not in prompt.seats:
                raise PlayError("no_prompt", "there is no open question for this seat")
            if isinstance(choice, str) and choice.lower() in ANSWERS:
                choice = ANSWERS.index(choice.lower())
            elif isinstance(choice, float) and choice.is_integer():      # clients whose numbers are all floats
                choice = int(choice)
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(prompt.choices):
                raise PlayError("bad_message", f"choice is 0-{len(prompt.choices) - 1} or a-{ANSWERS[len(prompt.choices) - 1]}")
            if not self.seats[seat_id].enabled:
                return
            prompt.answers[seat_id] = choice
            self._send(To(seats=frozenset((seat_id,))), {"type": "answered", "prompt": prompt.id, "choice": choice})
            self._send(TO_STAFF, {"type": "prompt_state", "id": prompt.id, "answered": len(prompt.answers), "of": len(prompt.seats)})
            if all(s in prompt.answers or not self.seats[s].enabled for s in prompt.seats):
                self._close(prompt)

    def _close(self, prompt: OpenPrompt) -> None:
        if self.prompts.pop(prompt.id, None) is not None:
            self._finish(prompt)

    def _finish(self, prompt: OpenPrompt) -> None:
        counts = [0] * len(prompt.choices)
        for choice in prompt.answers.values():
            counts[choice] += 1
        winner = counts.index(max(counts)) if any(counts) else -1
        result = {"type": "prompt_end", "id": prompt.id, "text": prompt.text, "choices": list(prompt.choices),
                  "counts": counts, "winner": winner if winner >= 0 else None,
                  "correct": prompt.correct if prompt.correct >= 0 else None, "mode": prompt.mode}
        self._send(TO_STAFF, {**result, "answers": dict(prompt.answers)})
        right: list[str] = []
        for seat_id in prompt.seats:
            seat = self.seats[seat_id]
            mine = prompt.answers.get(seat_id)
            if mine is not None:
                seat.answers += 1
            ok = prompt.correct >= 0 and mine == prompt.correct
            if ok:
                seat.right += 1
                right.append(seat_id)
            self._send(To(seats=frozenset((seat_id,))), {**result, "yours": mine,
                                                        "right": ok if prompt.correct >= 0 else None})
        self.asked.append({"text": prompt.text, "choices": list(prompt.choices), "counts": counts,
                           "winner": prompt.choices[winner] if winner >= 0 else None, "at": round(self.session_s, 1)})
        del self.asked[:-100]
        if right and prompt.points:
            self._score(right, prompt.points)
        if prompt.block is None:
            return
        outcomes: list[tuple[str, Signal]] = []
        if prompt.mode == "vote":
            total = sum(counts)
            outcomes.append((ANSWERS[winner], Signal(value=counts[winner] / total, choice=prompt.choices[winner], name=prompt.block.id))
                            if winner >= 0 else ("none", Signal(name=prompt.block.id)))
        else:
            for seat_id in prompt.seats:
                mine = prompt.answers.get(seat_id)
                team = self.seats[seat_id].team
                if mine is None:
                    outcomes.append(("none", Signal(seat_id, team, name=prompt.block.id)))
                else:
                    outcomes.append((ANSWERS[mine], Signal(seat_id, team, 1.0 if mine == prompt.correct else 0.0,
                                                           prompt.choices[mine], prompt.block.id)))
        self.logic.prompt_closed(prompt.block.id, outcomes)

    # ================================================================== the session
    def command(self, to: str, by: str = "operator") -> None:
        """start, pause, resume, end, reset."""
        with self._lock:
            if to not in ("start", "pause", "resume", "end", "reset"):
                raise PlayError("bad_message", "phase 'to' is one of start, pause, resume, end, reset")
            if self.film_mode and to != "reset" and by != "film":
                raise PlayError("film", "the film leads this session: start, pause and stop it on the Control Server")
            phase = self.phase
            if to == "start" and phase in ("lobby", "results"):
                self._reset()
                if self.cfg.countdown_s > 0.0 and by != "film":
                    self._count_left = self.cfg.countdown_s
                    self._enter("countdown")
                else:
                    self._enter("running")
            elif to == "pause" and phase == "running":
                self._enter("paused")
            elif to == "resume" and phase == "paused":
                self._enter("running")
            elif to == "end" and phase in ("countdown", "running", "paused"):
                self._enter("results")
            elif to == "reset":
                self._reset()
                self._enter("lobby")
            else:
                raise PlayError("phase", f"cannot {to} while the session is in '{phase}'")

    def _reset(self) -> None:
        for prompt in list(self.prompts.values()):
            self.prompts.pop(prompt.id, None)
            self._send(To(seats=frozenset(prompt.seats), roles=STAFF), {"type": "prompt_end", "id": prompt.id, "cancelled": True})
        self.session_s = 0.0
        self.state.clear()
        self.team_bonus.clear()
        self.asked.clear()
        for seat in self.seats.values():
            seat.score, seat.events, seat.inputs, seat.answers, seat.right, seat.reactions = 0.0, 0, 0, 0, 0, 0
        self.logic.reset()
        self.last_results = None
        self._seats_dirty = True
        self._send(EVERYONE, {"type": "state", "reset": True})

    def _enter(self, phase: str) -> None:
        self.phase = phase
        if self.engine is not None:
            self.engine.set_live(phase == "running")
        if phase == "running" and self.session_s == 0.0:
            self.sessions += 1
        if phase == "lobby":
            for seat in self.seats.values():
                seat.ready = False
        self._send(EVERYONE, self._phase_message())
        self.note(f"session: {phase}")
        if phase == "results":
            for prompt in list(self.prompts.values()):
                self._close(prompt)
            self.last_results = self.results()
            self._send(EVERYONE, {"type": "results", **self.last_results})
            self._save_results(self.last_results)
        self.logic.trigger("on_phase", Signal(value=self.session_s, name=phase), phase=phase)

    def step(self, now: float | None = None) -> None:
        """Time has passed (call 20-50 times a second)."""
        with self._lock:
            now = self.clock() if now is None else now
            elapsed = min(max(now - self._last, 0.0), 1.0)
            self._last = now
            if self.phase == "countdown":
                self._count_left -= elapsed
                if self._count_left <= 0.0:
                    self._enter("running")
            elif self.phase == "running":
                self.session_s += elapsed
                self.logic.step(self.session_s)
                limit = self.cfg.session_minutes * 60.0
                if limit > 0.0 and self.session_s >= limit and self.phase == "running" and not self.film_mode:
                    self._enter("results")
            if self.phase != "paused":
                for prompt in list(self.prompts.values()):
                    prompt.left -= elapsed
                    if prompt.left <= 0.0:
                        self._close(prompt)
            if self._seats_dirty and now >= self._seats_due:
                self._seats_dirty = False
                self._seats_due = now + SEATS_EVERY_S
                self._send(EVERYONE, self._seat_list())

    # ================================================================== a film leads
    def film_update(self, film: FilmState) -> None:
        """Where the Control Server's film is (called by the film link several times a second)."""
        with self._lock:
            before, self.film_state = self.film_state, film
            now = self.clock()
            if not film.ok:
                if before.ok:
                    self.note(f"film: the Control Server does not answer ({film.problem})")
                if self._film_lost is None:
                    self._film_lost = now
                elif now - self._film_lost > 5.0 and self.phase == "running":
                    self.note("film: no position for 5 s - the session is paused until the Control Server is back")
                    self.command("pause", "film")
                return
            if self._film_lost is not None or not before.ok:
                self.note(f"film: following the Control Server ({film.state.lower() or 'idle'})")
            self._film_lost = None
            was_playing = before.ok and before.state == "PLAYING" and before.starts_in_s <= 0.0
            changed = before.ok and film.programme != before.programme
            if film.state == "PLAYING" and film.starts_in_s > 0.0:
                # Scheduled: a start (the audience gets its countdown), a restart, or the end of a pause
                # (then everything stays as it is until the picture really moves).
                if self.phase in ("lobby", "results") or ((was_playing or changed) and self.phase in ("running", "paused")):
                    self._film_begins(film.starts_in_s)
                elif self.phase == "countdown":
                    self._count_left = film.starts_in_s   # in step with the Control Server
            elif film.state == "PLAYING":
                if self.film_held:
                    self.film_held, self._hold_asked = False, None
                elif self._hold_asked is not None and now - self._hold_asked > 3.0:
                    self._hold_asked = None               # the pause a block asked for never happened
                again = was_playing and not changed and film.position_s < before.position_s - 2.0     # it began again
                if self.phase in ("lobby", "results") or ((again or changed) and self.phase in ("running", "paused")):
                    self._film_begins(0.0)                # one showing of a film is one session
                elif self.phase == "countdown":
                    self._enter("running")
                elif self.phase == "paused":
                    self.command("resume", "film")
                self.logic.film_position(film.position_s, film.programme)
            elif film.state == "PAUSED":
                if self.phase == "running":               # what lies before the place it stops at still happens
                    self.logic.film_position(film.position_s, film.programme)
                if self._hold_asked is not None or self.film_held:
                    if not self.film_held:
                        self.note("film: waits for the blocks (the session goes on)")
                    self.film_held, self._hold_asked = True, None
                    if self.phase == "paused":            # the Control Server was away in between
                        self.command("resume", "film")
                elif self.phase == "running":             # paused at the Control Server: everything waits
                    self.command("pause", "film")
            else:
                self.film_held, self._hold_asked = False, None
                if self.phase in ("running", "paused", "countdown"):
                    self.logic.film_stopped()
                    self.command("end", "film")

    def _film_begins(self, starts_in: float) -> None:
        if self.phase in ("countdown", "running", "paused"):
            self.logic.film_stopped()
            self.command("end", "film")
        self._reset()
        if starts_in > 0.05:
            self._count_left = starts_in
            self._enter("countdown")
        else:
            self._enter("running")

    # ================================================================== results, status
    def results(self) -> dict[str, Any]:
        ranked = sorted((s for s in self.seats.values() if s.enabled), key=lambda s: (-s.score, s.id))
        seats = []
        for place, seat in enumerate(ranked, start=1):
            seats.append({"place": place, "id": seat.id, "name": seat.name, "team": seat.team, "score": _plain(seat.score),
                          "events": seat.events, "inputs": seat.inputs, "answers": seat.answers, "right": seat.right,
                          "reactions": seat.reactions})
        teams = [{"team": name, "score": _plain(total)}
                 for name, total in sorted(self.team_scores().items(), key=lambda kv: (-kv[1], kv[0]))]
        return {"title": self.cfg.title, "ended": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration_s": round(self.session_s, 1), "seats": seats, "teams": teams, "asked": list(self.asked),
                "state": dict(self.state)}

    def _save_results(self, results: dict[str, Any]) -> None:
        folder = self.cfg.results_dir
        if folder is None:
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"results-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.note(f"results written to {path}")
        except OSError as exc:
            self.note(f"results could not be written: {exc}")

    def snapshot(self) -> dict[str, Any]:
        """Everything the operator page shows about the session."""
        with self._lock:
            roles: dict[str, int] = {}
            for client in self.clients.values():
                roles[client.role] = roles.get(client.role, 0) + 1
            prompt = next(iter(self.prompts.values()), None)
            return {
                "title": self.cfg.title, "phase": self._phase_message(), "sessions": self.sessions,
                **{k: v for k, v in self._seat_list().items() if k != "type"}, "state": dict(self.state),
                "clients": roles, "dropped": sum(c.dropped for c in self.clients.values()),
                "logic": {"blocks": len(self.cfg.graph.blocks), "runs": self.logic.runs, "dropped": self.logic.dropped,
                          "problem": self.logic.problem or None},
                "prompt": ({**prompt.public(), "answered": len(prompt.answers), "of": len(prompt.seats)} if prompt else None),
                "film": ({"ok": self.film_state.ok, "state": self.film_state.state, "programme": self.film_state.programme,
                          "name": self.film_state.name, "position_s": round(self.film_state.position_s, 1),
                          "problem": self.film_state.problem or None, "held": self.film_held} if self.film_mode else None),
                "log": self.log[-40:],
            }
