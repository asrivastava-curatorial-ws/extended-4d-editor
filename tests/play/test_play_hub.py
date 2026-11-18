"""The session: who may do what, phases, scores, buttons, questions, shared values, a film that leads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fourdvr_play.hub import FilmState, PlayError
from play_helpers import KEY, Session

HIT = [{"id": "hit", "type": "on_event", "event": "hit", "then": ["jolt", "pts"]},
       {"id": "jolt", "type": "react", "accel": [-2.5, 0.0, 1.0], "seconds": 0.25, "effects": {"vibration": 1.0}},
       {"id": "pts", "type": "score", "points": -5.0}]


def errors(s: Session, client: Any) -> list[str]:
    return [m["code"] for m in s.messages("error", client)]


# ------------------------------------------------------------------ who may come in, and as what
def test_hello_decides_who_a_client_is() -> None:
    s = Session()
    for hello, code in (("hi", "hello"), ({"type": "hello", "play": 2}, "protocol"), ({"type": "hello", "play": 1, "role": "boss"}, "role"),
                        ({"type": "hello", "play": 1, "role": "operator", "key": "wrong-key"}, "key"),
                        ({"type": "hello", "play": 1, "role": "operator"}, "key"),
                        ({"type": "hello", "play": 1, "role": "player"}, "seat"),
                        ({"type": "hello", "play": 1, "role": "headset", "seat": "Z9"}, "seat")):
        with pytest.raises(PlayError) as refused:
            s.hub.join(hello)
        assert refused.value.code == code
    client, welcome = s.hub.join({"type": "hello", "play": 1, "role": "headset", "seat": "A2", "name": "Asha"})
    assert welcome["type"] == "welcome" and welcome["seat"] == "A2" and welcome["phase"]["phase"] == "lobby"
    assert welcome["buttons"] == ["a", "b", "c", "d"] and [x["id"] for x in welcome["seats"]] == ["A1", "A2"]
    seat = s.hub.seats["A2"]
    assert (seat.clients, seat.headsets, seat.name) == (1, 1, "Asha")
    s.join("player", "A2")
    assert s.hub.seats["A2"].clients == 2
    s.hub.leave(client)
    s.hub.leave(client)                                                                  # twice: no harm
    assert (seat.clients, seat.headsets) == (1, 0)
    s.cfg.game_key = "game-key-1"
    with pytest.raises(PlayError):
        s.join("game")
    assert s.join("game", key="game-key-1").role == "game"
    s.cfg.operator_key = ""                                                              # no key set: nobody is the operator
    with pytest.raises(PlayError):
        s.hub.join({"type": "hello", "play": 1, "role": "operator", "key": ""})


def test_each_role_may_do_its_own_things_only() -> None:
    s = Session(HIT)
    player, display, game, op = s.join("player", "A1"), s.join("display"), s.join("game"), s.join("operator")
    s.start()
    s.hub.handle(display, {"type": "event", "name": "hit", "seat": "A1"})
    s.hub.handle(player, {"type": "phase", "to": "end"})
    s.hub.handle(player, {"type": "score", "seat": "A1", "add": 100})
    s.hub.handle(player, {"type": "react", "seat": "A2", "accel": [0, 0, 3]})
    s.hub.handle(game, {"type": "hold", "on": True})
    s.hub.handle(player, {"type": "seat", "enabled": False})
    s.hub.handle(game, {"type": "seat", "seat": "A1", "motion": "off"})
    assert errors(s, display) == ["not_allowed"] and errors(s, player) == ["not_allowed"] * 4 and errors(s, game) == ["not_allowed"] * 2
    assert s.hub.seats["A1"].score == 0 and s.engine.reactions == [] and s.hub.phase == "running"
    # a player speaks for its own seat, whatever it writes into "seat"
    s.hub.handle(player, {"type": "event", "name": "hit", "seat": "A2"})
    assert s.hub.seats["A1"].score == -5 and s.hub.seats["A2"].score == 0
    s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A2"})
    s.hub.handle(game, {"type": "event", "name": "hit", "seat": "Z9"})
    s.hub.handle(game, {"type": "nonsense"})
    s.hub.handle(game, ["not", "an", "object"])
    assert s.hub.seats["A2"].score == -5 and errors(s, game)[2:] == ["seat", "bad_message", "bad_message"]
    s.hub.handle(op, {"type": "score", "seat": "A2", "set": 40})
    s.hub.handle(op, {"type": "score", "team": "red", "add": 7})
    assert s.hub.seats["A2"].score == 40 and s.hub.team_scores() == {"red": 2.0, "blue": 40.0}


def test_too_many_messages_are_left_out_not_queued() -> None:
    s = Session(HIT, max_events_per_s=10.0)
    game = s.join("game")
    s.start()
    for _ in range(50):
        s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A1"})
    assert s.hub.seats["A1"].events == 10 and game.dropped == 40 and errors(s, game) == ["slow_down"]
    s.run(0.5)
    for _ in range(50):
        s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A1"})
    assert s.hub.seats["A1"].events in (14, 15)                                              # half a second later: five more
    for _ in range(500):                                                                 # telemetry is not counted against it
        s.hub.handle(game, {"type": "motion", "seat": "A1", "accel": [1.0, 0.0, 0.0]})
    assert len(s.engine.offered) == 500 and s.engine.offered[0][0] == "A1"


# ------------------------------------------------------------------ the session
def test_a_session_from_the_lobby_to_the_results(tmp_path: Path) -> None:
    s = Session([*HIT, {"id": "end", "type": "on_phase", "phase": "results", "then": ["bye"]},
                 {"id": "bye", "type": "send", "event": "bye", "to": "all"}],
                countdown_s=3.0, session_minutes=1.0, results_dir=tmp_path)
    op, p1, game = s.join("operator"), s.join("player", "A1", name="Asha"), s.join("game")
    s.hub.handle(p1, {"type": "ready"})
    s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A1"})                   # in the lobby: scored, nothing moves
    assert s.hub.seats["A1"].ready and s.engine.live is False
    with pytest.raises(PlayError, match="cannot pause while the session is in 'lobby'"):
        s.hub.command("pause")
    s.hub.handle(op, {"type": "phase", "to": "start"})
    assert s.hub.phase == "countdown" and s.hub.seats["A1"].score == 0                   # a start clears what was before
    assert s.messages("phase")[-1]["seconds"] == 3.0
    s.run(2.9)
    assert s.hub.phase == "countdown" and not s.engine.live
    s.run(0.2)
    assert s.hub.phase == "running" and s.engine.live and s.hub.sessions == 1
    s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A1", "value": 0.4})
    seats, reaction, house = s.engine.reactions[-1]
    assert seats == ("A1",) and reaction.accel == (-2.5, 0.0, 1.0) and reaction.effects == {"vibration": 1.0} and not house
    assert s.messages("score", p1)[-1] == {"type": "score", "seat": "A1", "score": -5, "delta": -5}
    assert s.messages("react", p1)[-1]["push"] is True
    s.hub.handle(op, {"type": "phase", "to": "pause"})
    assert s.hub.phase == "paused" and not s.engine.live
    before = s.hub.session_s
    s.run(5.0)
    assert s.hub.session_s == before                                                     # the clock stands still
    s.hub.handle(op, {"type": "phase", "to": "resume"})
    s.run(60.0)                                                                          # the time is up
    assert s.hub.phase == "results" and not s.engine.live and 59.9 < s.hub.session_s < 60.2
    results = s.messages("results")[-1]
    assert [(r["place"], r["id"], r["score"]) for r in results["seats"]] == [(1, "A2", 0), (2, "A1", -5)]
    assert results["teams"] == [{"team": "blue", "score": 0}, {"team": "red", "score": -5}] and results["seats"][1]["name"] == "Asha"
    assert s.messages("event", game)[-1]["name"] == "bye"                                # the blocks hear of the end
    saved = list(tmp_path.glob("results-*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["seats"][0]["id"] == "A2"
    s.hub.handle(op, {"type": "phase", "to": "reset"})
    assert s.hub.phase == "lobby" and s.hub.seats["A1"].score == 0 and not s.hub.seats["A1"].ready


def test_targets_seat_team_others_all() -> None:
    blocks = [{"id": "go", "type": "on_event", "event": "go", "then": ["a", "b", "c", "d"]},
              {"id": "a", "type": "score", "points": 1.0, "to": "seat"}, {"id": "b", "type": "score", "points": 10.0, "to": "others"},
              {"id": "c", "type": "score", "points": 100.0, "to": "team"}, {"id": "d", "type": "react", "to": "all", "effects": {"fog": 0.5}}]
    s = Session(blocks, seats=4)
    game = s.join("game")
    s.start()
    s.hub.handle(game, {"type": "event", "name": "go", "seat": "A1"})
    assert [s.hub.seats[k].score for k in ("A1", "A2", "A3", "A4")] == [1, 10, 10, 10]
    assert s.hub.team_scores() == {"red": 111.0, "blue": 20.0}                            # the team's points are the team's
    assert s.engine.reactions[-1][0] == ("A1", "A2", "A3", "A4") and s.engine.reactions[-1][2] is True      # and the room
    s.hub.seats["A3"].enabled = False                                                    # a seat that is switched off gets nothing
    s.hub.handle(game, {"type": "event", "name": "go"})                                  # about nobody: everyone, for each wire
    assert [s.hub.seats[k].score for k in ("A1", "A2", "A3", "A4")] == [112, 121, 10, 121]


def test_scores_can_set_things_off_but_not_for_ever() -> None:
    s = Session([{"id": "w", "type": "on_score", "reaches": 10.0, "then": ["bonus", "tell"]},
                 {"id": "bonus", "type": "score", "points": 5.0}, {"id": "tell", "type": "send", "event": "level-up"},
                 {"id": "t", "type": "on_score", "reaches": 20.0, "who": "team", "then": ["team-won"]},
                 {"id": "team-won", "type": "send", "event": "team-won", "to": "team"}], seats=3)
    game = s.join("game")
    s.start()
    for add in (6, 6):
        s.hub.handle(game, {"type": "score", "seat": "A1", "add": add})
    assert s.hub.seats["A1"].score == 17 and [m["name"] for m in s.messages("event", game)] == ["level-up"]
    s.hub.handle(game, {"type": "score", "seat": "A3", "add": 4})                          # red: 17 + 4
    won = s.messages("event", game)[-1]
    assert won["name"] == "team-won" and won["seats"] == ["A1", "A3"]
    # a score that takes points away and gives them back would set itself off again and again
    s = Session([{"id": "w", "type": "on_score", "reaches": 10.0, "then": ["down"]},
                 {"id": "down", "type": "score", "points": -20.0, "then": ["up"]}, {"id": "up", "type": "score", "points": 20.0}])
    game = s.join("game")
    s.start()
    s.hub.handle(game, {"type": "score", "seat": "A1", "add": 10})
    assert s.hub.seats["A1"].score == 10 and "more than 8 deep" in s.hub.logic.problem


# ------------------------------------------------------------------ buttons
def test_buttons_from_pages_and_from_the_seat_boards() -> None:
    s = Session([{"id": "fire", "type": "on_input", "button": "a", "then": ["rate"]},
                 {"id": "rate", "type": "cooldown", "seconds": 0.5, "then": ["shot"]}, {"id": "shot", "type": "send", "event": "fire"},
                 {"id": "up", "type": "on_input", "edge": "release", "then": ["let-go"]}, {"id": "let-go", "type": "send", "event": "up"}],
                platforms=2)
    game, p1 = s.join("game"), s.join("player", "A1")
    s.start()

    def heard() -> list[tuple[str, Any]]:
        return [(m["name"], m["seat"]) for m in s.messages("event", game)]

    s.hub.handle(p1, {"type": "input", "button": "a", "pressed": True})
    s.hub.handle(p1, {"type": "input", "button": "a", "pressed": True})                   # held: not a second press
    assert heard() == [("fire", "A1")] and s.hub.seats["A1"].held == {"a"}
    s.hub.handle(p1, {"type": "input", "button": "a", "pressed": False})
    s.hub.handle(p1, {"type": "input", "button": "a"})                                    # a tap, but too soon after the last shot
    assert heard() == [("fire", "A1"), ("up", "A1"), ("up", "A1")]
    s.run(0.6)
    s.hub.node_inputs("PLATFORM-02", 0b0001)                                             # the board of seat A2: input 1 = "a"
    s.hub.node_inputs("PLATFORM-02", 0b0011)                                             # input 2 too: no block waits for "b"
    s.hub.node_inputs("PLATFORM-02", 0b0010)
    s.hub.node_inputs("NOT-A-PLATFORM", 0b1111)
    assert heard()[3:] == [("fire", "A2"), ("up", "A2")] and s.hub.seats["A2"].held == {"b"}
    assert [(m["button"], m["pressed"]) for m in s.messages("input", game) if m["seat"] == "A2"] == [("a", True), ("b", True), ("a", False)]
    s.hub.handle(p1, {"type": "input", "button": "c", "pressed": True})
    s.hub.leave(p1)                                                                      # the page is gone: its button is let go
    assert s.hub.seats["A1"].held == set() and heard()[-1] == ("up", "A1")


# ------------------------------------------------------------------ questions
QUIZ = [{"id": "ask", "type": "on_time", "at": 1.0, "then": ["q"]},
        {"id": "q", "type": "prompt", "text": "2 + 2?", "choices": ["3", "4", "5"], "seconds": 6.0, "correct": "b", "points": 50.0,
         "then": {"b": ["right"], "a": ["wrong"], "c": ["wrong"], "none": ["slept"]}},
        {"id": "right", "type": "react", "effects": {"air": 1.0}}, {"id": "wrong", "type": "score", "points": -1.0},
        {"id": "slept", "type": "send", "event": "wake-up"}]


def test_a_question_for_each_seat_with_a_right_answer() -> None:
    s = Session(QUIZ, seats=3)
    p1, p2, p3, game = s.join("player", "A1"), s.join("headset", "A2"), s.join("player", "A3"), s.join("game")
    s.start()
    s.run(1.0)
    asked = s.messages("prompt", p1)[-1]
    assert asked == {"type": "prompt", "id": "q1", "text": "2 + 2?", "choices": ["3", "4", "5"], "seconds": 6.0, "left": 6.0, "mode": "each"}
    s.hub.handle(p1, {"type": "answer", "prompt": "q1", "choice": 0})
    s.hub.handle(p1, {"type": "answer", "prompt": "q1", "choice": "b"})                   # changed its mind: the last one counts
    s.hub.handle(p2, {"type": "answer", "choice": 2.0})                                   # without the id: the open one
    s.hub.handle(p2, {"type": "answer", "choice": 7})
    assert errors(s, p2) == ["bad_message"] and s.messages("answered", p1)[-1]["choice"] == 1
    assert s.messages("prompt_state", game)[-1] == {"type": "prompt_state", "id": "q1", "answered": 2, "of": 3}
    s.run(5.0)
    assert list(s.hub.prompts) == ["q1"]
    s.run(1.1)
    assert s.hub.prompts == {}                                                           # the time was up
    ends = {c.seat: s.messages("prompt_end", c)[-1] for c in (p1, p2, p3)}
    assert ends["A1"]["yours"] == 1 and ends["A1"]["right"] is True and ends["A1"]["counts"] == [0, 1, 1] and ends["A1"]["correct"] == 1
    assert ends["A2"]["right"] is False and ends["A3"]["yours"] is None and ends["A3"]["right"] is False
    assert s.messages("prompt_end", game)[-1]["answers"] == {"A1": 1, "A2": 2}
    assert [s.hub.seats[k].score for k in ("A1", "A2", "A3")] == [50, -1, 0]
    assert s.engine.reactions[-1][0] == ("A1",) and s.messages("event", p3)[-1]["name"] == "wake-up"
    asked = s.hub.results()["asked"]
    assert len(asked) == 1 and asked[0]["counts"] == [0, 1, 1] and asked[0]["winner"] == "4" and 7.0 <= asked[0]["at"] <= 7.2
    assert (s.hub.seats["A1"].answers, s.hub.seats["A1"].right, s.hub.seats["A3"].answers) == (1, 1, 0)
    s.hub.handle(p1, {"type": "answer", "choice": 0})
    assert errors(s, p1) == ["no_prompt"]


def test_a_vote_is_decided_by_the_majority_and_closes_when_all_have_answered() -> None:
    vote = [{"id": "ask", "type": "on_event", "event": "fork", "then": ["q"]},
            {"id": "q", "type": "prompt", "text": "Which way?", "choices": ["Left", "Right"], "seconds": 8.0, "mode": "vote",
             "then": {"a": ["l"], "b": ["r"], "none": ["r"]}},
            {"id": "l", "type": "set_state", "key": "route", "value": "left"}, {"id": "r", "type": "set_state", "key": "route", "value": "right"}]
    s = Session(vote, seats=3, platforms=3)
    game, p1 = s.join("game"), s.join("player", "A1")
    s.start()
    s.hub.handle(game, {"type": "event", "name": "fork"})
    s.hub.handle(p1, {"type": "input", "button": "a"})                                    # the seat's buttons a-d answer
    s.hub.node_inputs("PLATFORM-02", 0b0010)                                             # "b" on the board of A2
    assert s.hub.state == {} and len(s.hub.prompts) == 1
    s.hub.node_inputs("PLATFORM-03", 0b0001)
    assert s.hub.prompts == {} and s.hub.state == {"route": "left"}                       # all three answered: no need to wait
    assert s.messages("prompt_end", game)[-1]["winner"] == 0 and s.messages("state")[-1] == {"type": "state", "key": "route", "value": "left"}
    s.hub.node_inputs("PLATFORM-03", 0)
    s.hub.node_inputs("PLATFORM-02", 0)
    s.hub.handle(game, {"type": "event", "name": "fork"})                                 # nobody answers
    s.run(8.1)
    assert s.hub.state == {"route": "right"} and s.messages("prompt_end", game)[-1]["winner"] is None
    s.hub.handle(game, {"type": "event", "name": "fork"})                                 # a tie: the first of the best
    s.hub.handle(p1, {"type": "answer", "choice": 1})
    s.hub.node_inputs("PLATFORM-02", 0b0001)
    s.run(8.1)
    assert s.hub.state == {"route": "left"}


def test_a_new_question_replaces_an_open_one_and_a_pause_stops_its_clock() -> None:
    s = Session(QUIZ)
    op, p1, game = s.join("operator"), s.join("player", "A1"), s.join("game")
    s.start()
    s.run(1.0)
    s.hub.command("pause")
    s.run(30.0)
    assert list(s.hub.prompts) == ["q1"] and s.hub.prompts["q1"].left == pytest.approx(6.0, abs=0.06)
    s.hub.command("resume")
    s.hub.handle(game, {"type": "prompt", "text": "Ready?", "choices": ["Yes", "No"], "seconds": 4, "mode": "vote"})
    assert list(s.hub.prompts) == ["q2"] and s.messages("prompt_end", p1)[-1] == {"type": "prompt_end", "id": "q1", "cancelled": True}
    assert s.hub.seats["A1"].score == 0                                                  # the replaced one's wires did not fire
    s.hub.handle(p1, {"type": "answer", "choice": "a"})
    s.run(4.1)
    end = s.messages("prompt_end", game)[-1]
    assert end["text"] == "Ready?" and end["counts"] == [1, 0] and end["winner"] == 0
    s.hub.handle(op, {"type": "prompt", "text": "One answer", "choices": ["x"]})
    s.hub.handle(p1, {"type": "prompt", "text": "May I?", "choices": ["Yes", "No"]})
    assert errors(s, op) == ["bad_message"] and errors(s, p1) == ["not_allowed"]
    s.hub.handle(op, {"type": "prompt", "text": "Again?", "choices": ["Yes", "No"], "seat": "A2"})
    s.hub.command("end")                                                                 # the end closes what is open
    assert s.hub.prompts == {} and s.messages("prompt_end", game)[-1]["text"] == "Again?"


# ------------------------------------------------------------------ shared values, seat settings
def test_shared_values_are_seen_by_all_and_can_set_blocks_off() -> None:
    s = Session([{"id": "hit", "type": "on_event", "event": "hit", "then": ["count"]},
                 {"id": "count", "type": "set_state", "key": "hits", "add": 1.0},
                 {"id": "three", "type": "on_state", "key": "hits", "equals": "3", "then": ["boom"]},
                 {"id": "boom", "type": "react", "to": "all", "effects": {"strobe": 1.0}},
                 {"id": "door", "type": "on_state", "key": "door", "then": ["tell"]}, {"id": "tell", "type": "send", "event": "door", "to": "all"}])
    game, p1 = s.join("game"), s.join("player", "A1")
    s.start()
    for _ in range(4):
        s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A1"})
    assert s.hub.state == {"hits": 4.0} and len(s.engine.reactions) == 1                  # at exactly three
    assert [m["value"] for m in s.messages("state", p1) if "key" in m] == [1, 2, 3, 4]
    s.hub.handle(game, {"type": "state", "key": "door", "value": "open"})
    s.hub.handle(game, {"type": "state", "key": "door", "value": "open"})                 # no change: nothing happens
    s.hub.handle(p1, {"type": "state", "key": "door", "value": "shut"})
    s.hub.handle(game, {"type": "state", "key": "door", "value": [1, 2]})
    assert len(s.messages("event", p1)) == 1 and errors(s, p1) == ["not_allowed"] and errors(s, game) == ["bad_message"]
    _client, welcome = s.hub.join({"type": "hello", "play": 1, "role": "display"})
    assert welcome["state"] == {"hits": 4.0, "door": "open"}                             # a late comer is told


def test_seat_settings_comfort_and_switching_a_seat_off() -> None:
    s = Session(HIT)
    op, p1, game = s.join("operator"), s.join("player", "A1"), s.join("game")
    assert s.engine.settings == {"A1": (True, "full", ()), "A2": (True, "full", ())}
    s.hub.handle(p1, {"type": "seat", "motion": "gentle", "blocked": ["water", "strobe", "water"], "name": "  Asha  "})
    assert s.engine.settings["A1"] == (True, "gentle", ("water", "strobe")) and s.hub.seats["A1"].name == "Asha"
    s.hub.handle(p1, {"type": "seat", "motion": "wild"})
    s.hub.handle(p1, {"type": "seat", "team": "blue"})
    assert errors(s, p1) == ["bad_message", "not_allowed"]
    s.hub.handle(op, {"type": "seat", "seat": "A2", "enabled": False, "team": "green"})
    assert s.engine.settings["A2"][0] is False and s.hub.seats["A2"].team == "green"
    s.start()
    s.hub.handle(game, {"type": "event", "name": "hit", "seat": "A2"})                    # a seat that is off: no points, no push
    s.hub.handle(game, {"type": "input", "seat": "A2", "button": "a"})
    assert s.hub.seats["A2"].score == 0 and s.engine.reactions == [] and s.hub.seats["A2"].inputs == 0
    s.run(0.3)
    listed = s.messages("seats", p1)[-1]
    assert listed["seats"][1]["enabled"] is False and listed["teams"] == {"red": 0, "green": 0}
    s.hub.handle(op, {"type": "hold", "on": True})
    assert s.engine.held and s.hub.held and s.messages("phase")[-1]["held"] is True
    s.hub.handle(op, {"type": "hold", "on": False})
    assert not s.engine.held and s.engine.engaged == 1                                   # releasing also acknowledges faults


def test_the_game_can_move_a_seat_directly_within_the_limits() -> None:
    s = Session()
    game = s.join("game")
    s.start()
    s.hub.handle(game, {"type": "react", "seat": "A2", "accel": [9.0, 0.0, 0.0], "seconds": 0.2, "effects": {"wind": 0.6}})
    seats, reaction, house = s.engine.reactions[-1]
    assert seats == ("A2",) and reaction.accel == (4.0, 0.0, 0.0) and reaction.effects == {"wind": 0.6} and not house
    s.hub.handle(game, {"type": "react", "accel": [0.0, 0.0, 50.0]})
    s.hub.handle(game, {"type": "react", "seat": "Z9"})
    assert errors(s, game) == ["bad_message", "seat"] and len(s.engine.reactions) == 1
    s.hub.handle(game, {"type": "react", "effects": {"fog": 1.0}, "effect_seconds": 2.0})
    assert s.engine.reactions[-1][0] == ("A1", "A2") and s.engine.reactions[-1][2] is True


# ------------------------------------------------------------------ a film leads
FILM = [{"id": "cue", "type": "on_time", "clock": "film", "at": 12.0, "then": ["q"]},
        {"id": "q", "type": "prompt", "text": "Open the door?", "choices": ["Yes", "No"], "seconds": 5.0, "mode": "vote",
         "then": {"a": ["next"], "b": ["hold"], "none": ["hold"]}},
        {"id": "next", "type": "film", "action": "play", "programme": 7}, {"id": "hold", "type": "film", "action": "pause"},
        {"id": "boom", "type": "on_time", "clock": "film", "at": 3.0, "then": ["shake"]},
        {"id": "shake", "type": "react", "to": "all", "accel": [0, 0, 2.0], "effects": {"vibration": 1.0}}]


def playing(position: float, programme: int = 3) -> FilmState:
    return FilmState(True, "PLAYING", programme, position, "Ride")


def test_a_film_leads_the_session_and_its_cues_fire_at_their_times() -> None:
    s = Session(FILM, film=True)
    op, p1, p2 = s.join("operator"), s.join("headset", "A1"), s.join("headset", "A2")
    assert s.hub.film_mode and s.messages("welcome") == []
    s.hub.handle(op, {"type": "phase", "to": "start"})
    assert errors(s, op) == ["film"] and s.hub.phase == "lobby"
    s.hub.film_update(FilmState(True, "IDLE"))
    assert s.hub.phase == "lobby"
    position = 0.0
    while position < 12.5:                                                               # the Control Server plays
        s.hub.film_update(playing(position))
        s.run(0.25)
        position += 0.25
    assert s.hub.phase == "running" and s.engine.live and s.hub.sessions == 1
    assert len(s.engine.reactions) == 1 and s.engine.reactions[0][2] is True              # the cue at 3 s
    assert s.messages("prompt", p1)[-1]["text"] == "Open the door?"                       # and the one at 12 s
    s.hub.film_update(FilmState(True, "PAUSED", 3, position))
    assert s.hub.phase == "paused" and not s.engine.live
    s.hub.film_update(playing(position))
    assert s.hub.phase == "running"
    s.hub.handle(p1, {"type": "answer", "choice": "a"})
    s.hub.handle(p2, {"type": "answer", "choice": "a"})
    assert s.film_requests == [("play", 7)]                                              # the vote starts programme 7
    s.hub.film_update(FilmState(True, "PREPARING", 7, 0.0))                               # the Control Server loads it
    assert s.hub.phase == "results"
    s.hub.film_update(playing(0.1, 7))
    assert s.hub.phase == "running" and s.hub.sessions == 2 and s.hub.seats["A1"].answers == 0
    s.hub.film_update(FilmState(True, "STOPPED", 7, 0.0))
    assert s.hub.phase == "results" and not s.engine.live


def test_a_lost_control_server_pauses_the_session_after_a_while() -> None:
    s = Session(FILM, film=True)
    s.hub.film_update(playing(1.0))
    s.run(0.2)
    assert s.hub.phase == "running"
    s.hub.film_update(FilmState(False, problem="does not answer"))
    s.run(4.0)
    s.hub.film_update(FilmState(False, problem="does not answer"))
    assert s.hub.phase == "running"                                                      # a hiccup is not a pause
    s.run(2.0)
    s.hub.film_update(FilmState(False, problem="does not answer"))
    assert s.hub.phase == "paused" and not s.engine.live
    assert any("no position for 5 s" in line for line in s.hub.log)
    s.hub.film_update(playing(8.0))
    assert s.hub.phase == "running"
    snapshot = s.hub.snapshot()
    assert snapshot["film"]["ok"] and snapshot["film"]["programme"] == 3 and snapshot["phase"]["phase"] == "running"
    assert snapshot["clients"] == {} and snapshot["logic"]["blocks"] == 6


def test_blocks_cannot_end_a_session_that_a_film_leads_and_without_a_film_they_can() -> None:
    blocks = [{"id": "t", "type": "on_time", "at": 2.0, "then": ["end"]}, {"id": "end", "type": "phase"}]
    s = Session(blocks)
    s.start()
    s.run(2.1)
    assert s.hub.phase == "results"
    assert KEY and s.hub.results()["duration_s"] == pytest.approx(2.0, abs=0.11)
