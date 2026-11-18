"""Following a film that the Control Server plays, and asking it to pause, resume, stop or play.

The Control Server is used as it is, through its ordinary HTTP interface: its playback state is read
a few times a second (a viewer account is enough) and the position in between is worked out from
this computer's clock. Commands need an operator account. The Control Server has no "jump to a
time" - a vote can therefore pause the film or start another programme, not seek.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from fourdvr_fx.ready import ServerError, _call

from .config import FilmConfig
from .hub import FilmState

ACTIONS = ("pause", "resume", "stop", "play")
RETRY_S = 3.0


def _refusal(error: Any) -> str:
    """Why the Control Server refused a command (its answer to a 409), in a few words."""
    try:
        detail = json.loads(error.read().decode("utf-8")).get("detail")
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("result")
        return str(detail)[:160] if detail else "no reason given"
    except (AttributeError, OSError, ValueError):
        return "no reason given"


class FilmLink:
    """A thread of its own: nothing here may ever hold up the session."""

    def __init__(self, cfg: FilmConfig, on_state: Callable[[FilmState], None],
                 call: Callable[..., Any] = _call, clock: Callable[[], float] = time.monotonic,
                 on_note: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.on_state = on_state
        self.on_note = on_note            # a command the Control Server did not carry out is told to the operator
        self._call = call
        self.clock = clock
        self.token = ""
        self.state = FilmState(problem="not connected yet")
        self.commands_sent = 0
        self.commands_failed = 0
        self.command_problem = ""         # the last command that was not carried out, and why
        self.last_problem = ""
        self._requests: queue.SimpleQueue[tuple[str, int]] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._retry_at = 0.0

    # ------------------------------------------------------------------ for the hub
    def request(self, action: str, programme: int = 0) -> None:
        if action in ACTIONS:
            self._requests.put((action, int(programme)))

    # ------------------------------------------------------------------ one round (also how the tests drive it)
    def _login(self) -> None:
        cfg = self.cfg
        if not cfg.user:
            raise ServerError("no account: [play.film] needs user = ... and a password")
        answer = self._call(cfg.server, "/api/v1/auth/login", body={"username": cfg.user, "password": cfg.password},
                            timeout_s=3.0, insecure=cfg.insecure)
        self.token = str(answer["token"])

    def poll(self) -> FilmState:
        cfg = self.cfg
        try:
            if not self.token:
                self._login()
            started = self.clock()
            try:
                data = self._call(cfg.server, "/api/v1/playback/state", self.token, timeout_s=2.0, insecure=cfg.insecure)
            except ServerError:
                self.token = ""                 # a token that ran out: log in again at the next round
                raise
            delay = (self.clock() - started) / 2.0
            timeline = data.get("timeline") or {}
            state = str(timeline.get("state", ""))
            position, starts_in = float(data.get("position_s") or 0.0), 0.0
            if state == "PLAYING":
                # The Control Server schedules ahead: a PLAY or a RESUME says PLAYING at once, the picture moves
                # from "start_at" on. Until then it stands at the timeline's own position.
                start_at, server_time = timeline.get("start_at"), data.get("server_time")
                if isinstance(start_at, int | float) and isinstance(server_time, int | float) and server_time + delay < start_at:
                    starts_in = float(start_at) - float(server_time) - delay
                    position = float(timeline.get("position_s") or 0.0)
                else:
                    position = max(0.0, position + delay)
            self.state = FilmState(True, state, int(timeline.get("programme_id") or 0), position,
                                   str(data.get("programme_name") or ""), starts_in_s=starts_in)
            self.last_problem = ""
        except (ServerError, KeyError, TypeError, ValueError) as exc:
            self.last_problem = str(exc) or type(exc).__name__
            self.state = FilmState(False, problem=f"Control Server {self.last_problem}")
        return self.state

    def send(self, action: str, programme: int = 0, again: bool = True) -> bool:
        cfg = self.cfg
        try:
            if not self.token:
                self._login()
            body: dict[str, Any] = {"programme_id": programme} if action == "play" else {}
            self._call(cfg.server, f"/api/v1/playback/{action}", self.token, body=body, timeout_s=4.0, insecure=cfg.insecure)
            self.commands_sent += 1
            return True
        except (ServerError, KeyError, TypeError, ValueError) as exc:
            code = getattr(exc.__cause__, "code", 0)
            if code == 401 and again:                 # the token ran out: once more with a new one
                self.token = ""
                return self.send(action, programme, again=False)
            if code == 403:
                problem = "this account may not steer the film: [play.film] needs an operator account"
            elif code == 409:
                problem = f"the Control Server refused ({_refusal(exc.__cause__)})"
            else:
                problem = f"Control Server {exc}" if isinstance(exc, ServerError) else f"{type(exc).__name__}: {exc}"
            self.command_problem = f"film {action}: {problem}"
            self.commands_failed += 1
            if self.on_note is not None:
                self.on_note(self.command_problem)
            return False

    # ------------------------------------------------------------------ the thread
    def round(self) -> None:
        """One turn: carry out what was asked, then read where the film is."""
        now = self.clock()
        while True:
            try:
                action, programme = self._requests.get_nowait()
            except queue.Empty:
                break
            self.send(action, programme)
        if now >= self._retry_at:
            state = self.poll()
            if not state.ok:
                self._retry_at = now + RETRY_S           # a server that is away is asked every few seconds only
            self.on_state(state)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.round()
            self._stop.wait(self.cfg.poll_s)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="play-film-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
