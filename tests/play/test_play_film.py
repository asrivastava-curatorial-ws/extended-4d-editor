"""The film link: following a film the Control Server plays, and steering it (against a stand-in for its HTTP interface)."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from typing import Any

import pytest

from fourdvr_fx.ready import ServerError
from fourdvr_play import film as film_module
from fourdvr_play.config import FilmConfig
from fourdvr_play.film import FilmLink
from fourdvr_play.hub import FilmState, Hub
from play_helpers import Clock, make_cfg

CFG = FilmConfig("http://control-server:8080", "op", "pw", False, 0.05)


def refused(code: int, payload: Any = None, path: str = "") -> ServerError:
    """What fourdvr_fx.ready._call raises for an HTTP error (the same words)."""
    if code in (401, 403):
        error = ServerError("the user name or password was not accepted" if path.endswith("/login")
                            else "this account may not read that (a viewer account is enough)")
    else:
        error = ServerError(f"answered {code} to {path}")
    error.__cause__ = urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(json.dumps(payload or {}).encode()))  # type: ignore[arg-type]
    return error


class FakeServer:
    """The three things the link uses of the Control Server: login, playback state, playback commands."""

    def __init__(self, clock: Any = None) -> None:
        self.clock = clock
        self.calls: list[tuple[str, Any]] = []
        self.logins = 0
        self.token = ""
        self.role = "operator"
        self.away = False
        self.state, self.programme, self.position = "PLAYING", 1, 12.0
        self.refuse = ""
        self.starts_in = 0.0             # > 0: PLAYING is said, the picture moves only then (the server schedules ahead)

    def __call__(self, url: str, path: str, token: str = "", body: Any = None, timeout_s: float = 4.0, insecure: bool = False) -> Any:
        assert url == CFG.server
        self.calls.append((path, body))
        if self.away:
            raise ServerError("does not answer (connection refused)")
        if path == "/api/v1/auth/login":
            if body != {"username": "op", "password": "pw"}:
                raise refused(401, path=path)
            self.logins += 1
            self.token = f"token-{self.logins}"
            return {"token": self.token}
        if token != self.token:
            raise refused(401)
        if path == "/api/v1/playback/state":
            assert body is None
            if self.clock is not None:
                self.clock.now += 0.2                        # the question and the answer travel 0.1 s each
            timeline = {"state": self.state, "programme_id": self.programme, "position_s": self.position, "start_at": 500.0}
            return {"timeline": timeline, "programme_name": "The ride", "server_time": 500.0 - self.starts_in,
                    "position_s": self.position - self.starts_in}        # as the Control Server reports it
        action = path.rsplit("/", 1)[1]
        if self.role != "operator":
            raise refused(403)
        if self.refuse:
            raise refused(409, {"detail": {"result": "rejected", "detail": self.refuse}})
        if action == "play":
            self.state, self.programme, self.position = "PLAYING", body["programme_id"], 0.0
        else:
            assert body == {}
            self.state = {"pause": "PAUSED", "resume": "PLAYING", "stop": "STOPPED"}[action]
        return {"result": "accepted"}


def test_the_position_is_read_with_one_login_and_carried_over_the_way_back() -> None:
    clock = Clock()
    server = FakeServer(clock)
    link = FilmLink(CFG, lambda state: None, call=server, clock=clock)
    assert link.poll() == FilmState(True, "PLAYING", 1, pytest.approx(12.1), "The ride")
    server.state, server.position = "PAUSED", 20.0
    assert link.poll() == FilmState(True, "PAUSED", 1, 20.0, "The ride")      # a film that stands still is where it is
    assert server.logins == 1 and [path for path, _ in server.calls].count("/api/v1/playback/state") == 2


def test_a_token_that_ran_out_is_renewed() -> None:
    server = FakeServer()
    link = FilmLink(CFG, lambda state: None, call=server)
    assert link.poll().ok
    server.token = "another"                                 # the Control Server was restarted
    lost = link.poll()
    assert not lost.ok and lost.problem.startswith("Control Server")
    assert link.poll().ok and server.logins == 2
    server.token = "another"
    assert link.send("pause") and server.state == "PAUSED" and server.logins == 3      # a command: at once, with a new token


def test_commands_and_what_the_operator_is_told_when_one_is_not_carried_out() -> None:
    server, notes = FakeServer(), []
    link = FilmLink(CFG, lambda state: None, call=server, on_note=notes.append)
    assert link.send("play", 2) and server.calls[-1] == ("/api/v1/playback/play", {"programme_id": 2})
    assert link.send("pause") and link.send("resume") and link.send("stop") and server.state == "STOPPED"
    assert link.commands_sent == 4 and notes == []
    server.refuse = "nothing is playing"
    assert not link.send("pause") and notes == ["film pause: the Control Server refused (nothing is playing)"]
    server.refuse, server.role = "", "viewer"
    assert not link.send("resume") and "needs an operator account" in notes[-1]
    server.away = True
    assert not link.send("stop") and notes[-1] == "film stop: Control Server does not answer (connection refused)"
    assert link.commands_failed == 3 and link.commands_sent == 4
    link.request("format the disk")                          # not one of the four: never sent
    assert link._requests.empty()


def test_a_wrong_password_and_a_server_that_is_away(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, states = Clock(), []
    server = FakeServer()
    link = FilmLink(FilmConfig(CFG.server, "op", "wrong", False, 0.05), states.append, call=server, clock=clock)
    link.round()
    assert not states[-1].ok and "password was not accepted" in states[-1].problem
    nobody = FilmLink(FilmConfig(CFG.server, "", "", False, 0.05), states.append, call=server, clock=clock)
    assert "needs user" in nobody.poll().problem
    link = FilmLink(CFG, states.append, call=server, clock=clock)
    server.away = True
    before = len(server.calls)
    for _ in range(40):                                      # 2 s of rounds: a server that is away is not asked each time
        link.round()
        clock.now += 0.05
    assert len(server.calls) - before == 1 and not states[-1].ok
    clock.now += film_module.RETRY_S
    server.away = False
    link.round()
    assert states[-1].ok


def test_the_thread_follows_the_film_and_carries_out_requests() -> None:
    server, states = FakeServer(), []
    link = FilmLink(CFG, states.append, call=server)
    link.start()
    try:
        end = time.monotonic() + 3.0
        while len(states) < 3 and time.monotonic() < end:
            time.sleep(0.01)
        assert len(states) >= 3 and all(s.ok and s.state == "PLAYING" for s in states)
        link.request("pause")
        while server.state != "PAUSED" and time.monotonic() < end:
            time.sleep(0.01)
        assert server.state == "PAUSED"
    finally:
        link.stop()
    count = len(states)
    time.sleep(0.15)
    assert len(states) == count and link._thread is None      # stopped means stopped


def test_a_decision_point_the_film_waits_for_the_vote_and_goes_on() -> None:
    """The blocks pause the film, ask, and resume it or start another programme: the session runs meanwhile."""
    blocks = [{"id": "cue", "type": "on_time", "at": 15.0, "clock": "film", "then": ["wait", "ask"]},
              {"id": "wait", "type": "film", "action": "pause"},
              {"id": "ask", "type": "prompt", "text": "Which way?", "choices": ["On", "The other film"], "seconds": 10.0,
               "mode": "vote", "then": {"a": ["on"], "b": ["other"], "none": ["on"]}},
              {"id": "on", "type": "film", "action": "resume"},
              {"id": "other", "type": "film", "action": "play", "programme": 2}]
    clock = Clock()
    server = FakeServer()
    hub = Hub(make_cfg(blocks, seats=2, film=True), None, clock)
    link = FilmLink(CFG, hub.film_update, call=server, clock=clock, on_note=hub.note)
    hub.film_control = link
    headset, _ = hub.join({"type": "hello", "play": 1, "role": "headset", "seat": "A1"})
    other, _ = hub.join({"type": "hello", "play": 1, "role": "player", "seat": "A2"})

    def run(seconds: float) -> None:
        for _ in range(round(seconds / 0.05)):
            clock.now += 0.05
            if server.state == "PLAYING":
                server.position += 0.05
            link.round()
            hub.step()

    run(3.5)                                                                     # film time 15 s passes
    assert server.state == "PAUSED" and len(hub.prompts) == 1
    assert hub.phase == "running" and hub.film_held and hub.snapshot()["film"]["held"] is True
    stood = server.position
    run(4.0)                                                                     # nobody answers yet: the film waits,
    assert server.position == stood and hub.phase == "running"                   # the question's time runs
    assert next(iter(hub.prompts.values())).left == pytest.approx(5.5, abs=0.2)      # asked 0.5 s before the first check
    hub.handle(headset, {"type": "answer", "choice": "a"})
    hub.handle(other, {"type": "answer", "choice": 0})
    run(0.5)
    assert server.state == "PLAYING" and not hub.film_held and hub.phase == "running" and link.commands_sent == 2
    server.state = "PAUSED"                                                      # paused at the Control Server instead:
    run(0.5)
    assert hub.phase == "paused" and not hub.film_held                           # then everything waits
    server.state = "PLAYING"
    run(0.5)
    assert hub.phase == "running"


def test_a_pause_the_control_server_refuses_does_not_confuse_the_session() -> None:
    blocks = [{"id": "cue", "type": "on_time", "at": 13.0, "clock": "film", "then": ["wait"]},
              {"id": "wait", "type": "film", "action": "pause"}]
    clock, notes = Clock(), []
    server = FakeServer()
    server.role = "viewer"                                                       # the account may only watch
    hub = Hub(make_cfg(blocks, seats=2, film=True), None, clock)
    link = FilmLink(CFG, hub.film_update, call=server, clock=clock, on_note=notes.append)
    hub.film_control = link
    for _ in range(120):                                                         # 6 s
        clock.now += 0.05
        server.position += 0.05
        link.round()
        hub.step()
    assert server.state == "PLAYING" and "needs an operator account" in notes[-1] and not hub.film_held
    server.state = "PAUSED"                                                      # later the operator pauses the film:
    link.round()                                                                 # that is his pause, not the blocks'
    assert hub.phase == "paused" and not hub.film_held


def test_a_vote_in_a_film_pauses_it_through_the_link() -> None:
    blocks = [{"id": "cue", "type": "on_time", "at": 15.0, "clock": "film", "then": ["ask"]},
              {"id": "ask", "type": "prompt", "text": "Stop here?", "choices": ["No", "Yes"], "seconds": 10.0, "mode": "vote",
               "then": {"b": ["halt"]}},
              {"id": "halt", "type": "film", "action": "pause"}]
    clock = Clock()
    server = FakeServer()
    hub = Hub(make_cfg(blocks, seats=2, film=True), None, clock)
    link = FilmLink(CFG, hub.film_update, call=server, clock=clock, on_note=hub.note)
    hub.film_control = link
    headset, _ = hub.join({"type": "hello", "play": 1, "role": "headset", "seat": "A1"})
    other, _ = hub.join({"type": "hello", "play": 1, "role": "player", "seat": "A2"})

    def run(seconds: float) -> None:
        for _ in range(round(seconds / 0.05)):
            clock.now += 0.05
            if server.state == "PLAYING":
                server.position += 0.05
            link.round()
            hub.step()

    run(1.0)
    assert hub.phase == "running" and hub.film_mode and not hub.prompts          # the film leads: no operator started it
    run(2.5)                                                                     # film time 15 s passes
    assert len(hub.prompts) == 1
    hub.handle(headset, {"type": "answer", "choice": 1})
    hub.handle(other, {"type": "answer", "choice": "b"})
    run(0.5)
    assert server.state == "PAUSED" and hub.film_held and hub.phase == "running" and link.commands_sent == 1
    server.state = "PLAYING"                                                     # the operator resumes at the Control Server
    run(0.5)
    assert hub.phase == "running" and not hub.film_held
    server.state = "STOPPED"
    run(0.5)
    assert hub.phase == "results"


def test_scheduled_starts_resumes_and_a_film_that_begins_again() -> None:
    """The Control Server says PLAYING ahead of time (start and resume are scheduled): nothing may happen early,
    nothing twice. And one showing of a film is one session."""
    blocks = [{"id": "cue", "type": "on_time", "at": 6.0, "clock": "film", "then": ["mark"]},
              {"id": "mark", "type": "set_state", "key": "cues", "add": 1.0}]
    clock = Clock()
    server = FakeServer()
    server.position, server.starts_in = 0.0, 2.0                                 # PLAY was pressed: it starts in 2 s
    hub = Hub(make_cfg(blocks, seats=2, film=True), None, clock)
    link = FilmLink(CFG, hub.film_update, call=server, clock=clock)

    def run(seconds: float) -> None:
        for _ in range(round(seconds / 0.05)):
            clock.now += 0.05
            if server.state == "PLAYING" and server.starts_in > 0.0:
                server.starts_in = max(0.0, round(server.starts_in - 0.05, 3))
            elif server.state == "PLAYING":
                server.position += 0.05
            link.round()
            hub.step()

    assert link.poll() == FilmState(True, "PLAYING", 1, 0.0, "The ride", starts_in_s=2.0)
    run(1.0)
    assert hub.phase == "countdown" and hub._count_left == pytest.approx(1.0, abs=0.11)      # the riders count down with it
    run(1.5)
    assert hub.phase == "running" and hub.session_s == pytest.approx(0.5, abs=0.15) and hub.sessions == 1
    run(6.0)
    assert hub.state == {"cues": 1}
    server.state, server.position = "PAUSED", server.position + 0.5              # paused at the Control Server
    run(1.0)
    assert hub.phase == "paused"
    stood = server.position
    server.state, server.starts_in = "PLAYING", 1.0                              # RESUME: the picture moves a second later
    assert link.poll() == FilmState(True, "PLAYING", 1, stood, "The ride", starts_in_s=1.0)   # it stands where it stood
    run(0.5)
    assert hub.phase == "paused"                                                 # ... and so does the session
    run(1.0)
    assert hub.phase == "running"
    run(2.0)
    assert hub.state == {"cues": 1} and hub.sessions == 1                        # the cue at 6 s did not fire again
    server.position = 0.0                                                        # the film is over and begins again
    run(0.25)
    assert hub.sessions == 2 and hub.phase == "running" and hub.state == {} and hub.last_results is None
    run(6.5)
    assert hub.state == {"cues": 1}


def test_what_lies_before_the_place_the_film_stops_at_still_happens() -> None:
    blocks = [{"id": "cue", "type": "on_time", "at": 13.0, "clock": "film", "then": ["mark"]},
              {"id": "mark", "type": "set_state", "key": "cue", "value": "seen"}]
    clock = Clock()
    server = FakeServer()
    hub = Hub(make_cfg(blocks, seats=2, film=True), None, clock)
    link = FilmLink(CFG, hub.film_update, call=server, clock=clock)
    link.round()                                                                 # playing at 12.0 s
    assert hub.phase == "running" and hub.state == {}
    server.state, server.position = "PAUSED", 13.2                               # paused: it stops at 13.2 s
    link.round()
    assert hub.state == {"cue": "seen"} and hub.phase == "paused"
