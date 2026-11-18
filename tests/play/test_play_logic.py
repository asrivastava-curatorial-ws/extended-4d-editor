"""The logic blocks: reading and writing them, what is refused, and how each kind behaves."""

from __future__ import annotations

import itertools
import random
import tomllib
from typing import Any

import pytest

from fourdvr_play.config import EXAMPLE, PlayConfigError, blocks_toml, replace_blocks
from fourdvr_play.logic import TYPES, Block, Graph, Limits, Logic, LogicError, Signal, make_block, reaction_of


class Host:
    """Stands in for the hub: records what the blocks do."""

    def __init__(self, seats: tuple[str, ...] = ("A1", "A2", "A3"), teams: dict[str, str] | None = None) -> None:
        self.seats = list(seats)
        self.teams = teams or {"A1": "red", "A2": "blue", "A3": "red"}
        self.done: list[tuple[Any, ...]] = []

    def targets(self, to: str, signal: Signal) -> list[str]:
        return self.seats if to == "all" or signal.seat is None else [signal.seat]

    def active_seats(self) -> list[str]:
        return self.seats

    def team_of(self, seat: str) -> str:
        return self.teams.get(seat, "")

    def add_score(self, to: str, signal: Signal, points: float) -> None:
        self.done.append(("score", to, signal.seat, points))

    def react(self, to: str, signal: Signal, block: Block) -> None:
        self.done.append(("react", to, signal.seat, block.id))

    def change_state(self, key: str, value: str, add: float) -> None:
        self.done.append(("state", key, value, add))

    def send_event(self, name: str, to: str, signal: Signal) -> None:
        self.done.append(("send", name, to, signal.seat))

    def open_prompt(self, block: Block, signal: Signal) -> None:
        self.done.append(("prompt", block.id, signal.seat))

    def film(self, action: str, programme: int) -> None:
        self.done.append(("film", action, programme))

    def change_phase(self, to: str) -> None:
        self.done.append(("phase", to))


def logic(blocks: list[dict[str, Any]], seed: int = 1, **host: Any) -> tuple[Logic, Host]:
    h = Host(**host)
    return Logic(Graph.from_list(blocks), h, random.Random(seed)), h


def chain(*middle: dict[str, Any], start: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """when event 'go' -> the given blocks in a row -> send 'done' (and send 'not' from an 'else')."""
    blocks = [start or {"id": "go", "type": "on_event", "event": "go"}, *middle, {"id": "done", "type": "send", "event": "done"},
              {"id": "not", "type": "send", "event": "not"}]
    for a, b in itertools.pairwise(blocks[:-1]):
        a["then"] = {"then": [b["id"]], "else": ["not"]} if "else" in TYPES[a["type"]].outputs else [b["id"]]
    return blocks


def sent(host: Host, name: str = "done") -> list[Any]:
    return [d[3] for d in host.done if d[0] == "send" and d[1] == name]


# ------------------------------------------------------------------ reading, writing, refusing
def test_blocks_are_read_checked_and_written_back_the_way_a_person_writes_them() -> None:
    data = tomllib.loads(EXAMPLE)["play"]["blocks"]
    graph = Graph.from_list(data)
    assert len(graph.blocks) == 10 and graph.notes() == []
    again = Graph.from_list(tomllib.loads(blocks_toml(graph))["play"]["blocks"])
    assert again.to_list() == graph.to_list()
    jolt = next(e for e in graph.to_list() if e["id"] == "hit-jolt")
    assert jolt == {"id": "hit-jolt", "type": "react", "accel": [-2.5, 0.0, 1.0], "seconds": 0.25,
                    "effects": {"vibration": 1.0, "air": 1.0}, "effect_seconds": 0.4}       # 'to = "seat"' is the default: left out
    route = next(e for e in graph.to_list() if e["id"] == "route")
    assert route["then"] == {"a": ["canyon"], "b": ["ridge"], "none": ["ridge"]} and route["mode"] == "vote"
    assert graph.blocks["hit"].label() == "event “hit”" and "push + vibration, air" in graph.blocks["hit-jolt"].label()


@pytest.mark.parametrize(("entry", "message"), [
    ({"id": "x", "type": "nothing"}, "unknown type"),
    ({"id": "bad id", "type": "delay"}, "a block needs id"),
    ({"id": "x", "type": "delay", "secs": 1}, "unknown key(s) secs"),
    ({"id": "x", "type": "delay", "seconds": 0}, "seconds must be between"),
    ({"id": "x", "type": "delay", "seconds": "1"}, "seconds must be a number"),
    ({"id": "x", "type": "on_event", "event": ""}, "event is missing"),
    ({"id": "x", "type": "on_event", "event": "two words"}, "may hold letters"),
    ({"id": "x", "type": "score", "to": "everyone"}, "to must be one of"),
    ({"id": "x", "type": "react", "accel": [1, 2]}, "three numbers"),
    ({"id": "x", "type": "react", "accel": [0, 0, 50]}, "accel must be between"),
    ({"id": "x", "type": "react", "effects": {"wind": 2}}, "wind must be between"),
    ({"id": "x", "type": "prompt", "text": "?", "choices": ["only one"]}, "2 to 4 texts"),
    ({"id": "x", "type": "count", "n": 2.5}, "whole number"),
    ({"id": "x", "type": "filter", "then": {"maybe": []}}, "no output 'maybe'"),
    ({"id": "x", "type": "delay", "then": "y", "pos": [1]}, "pos must be two numbers"),
])
def test_a_block_that_makes_no_sense_is_refused_with_its_name(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(LogicError, match=message.replace("(", r"\(").replace(")", r"\)")):
        make_block(entry)


def test_wires_must_lead_somewhere_sensible_and_loops_need_a_delay() -> None:
    go = {"id": "go", "type": "on_event", "event": "go"}
    with pytest.raises(LogicError, match="'nowhere', which does not exist"):
        Graph.from_list([{**go, "then": ["nowhere"]}])
    with pytest.raises(LogicError, match="nothing can lead to it"):
        Graph.from_list([{**go, "then": ["go2"]}, {"id": "go2", "type": "on_event", "event": "x"}])
    with pytest.raises(LogicError, match="two blocks are called"):
        Graph.from_list([go, go])
    loop = [{**go, "then": ["a"]}, {"id": "a", "type": "score", "then": ["b"]}, {"id": "b", "type": "chance", "then": {"else": ["a"]}}]
    with pytest.raises(LogicError, match="a -> b -> a form a loop without a 'delay'"):
        Graph.from_list(loop)
    loop[2] = {"id": "b", "type": "delay", "seconds": 1.0, "then": ["a"]}
    graph = Graph.from_list(loop)                                   # with a delay in it, a loop is a rhythm
    assert graph.can_connect("a", "then", "a") == "a block cannot lead to itself"
    assert graph.can_connect("b", "then", "b") == "" and graph.can_connect("a", "then", "go") == "nothing can lead to a 'when' block"
    assert graph.can_connect("go", "else", "a") == "When an event arrives has no output 'else'"


def test_notes_point_at_blocks_that_do_nothing() -> None:
    graph = Graph.from_list([{"id": "go", "type": "on_event", "event": "go"}, {"id": "lonely", "type": "react"},
                             {"id": "q", "type": "prompt", "text": "?", "correct": "c"}])
    notes = "\n".join(graph.notes())
    assert "block go (when an event arrives): nothing follows it" in notes
    assert "block lonely (move and effects): nothing leads to it" in notes and "no push and no effect is set" in notes
    assert "the right answer 'c' is not among the answers" in notes


def test_editing_a_graph_keeps_it_whole() -> None:
    graph = Graph()
    a = graph.add("on_event", (10, 20))
    b = graph.add("react")
    c = graph.add("react")
    assert (a.id, b.id, c.id) == ("event1", "react1", "react2") and a.params["event"] == "hit"
    graph.connect(a.id, "then", b.id)
    graph.connect(b.id, "then", c.id)
    with pytest.raises(LogicError, match="already connected"):
        graph.connect(a.id, "then", b.id)
    with pytest.raises(LogicError, match="loop without a 'delay'"):
        graph.connect(c.id, "then", b.id)
    graph.rename(b.id, "jolt")
    assert graph.blocks[a.id].then == {"then": ["jolt"]} and list(graph.blocks) == ["event1", "jolt", "react2"]
    with pytest.raises(LogicError, match="exists already"):
        graph.rename("jolt", "react2")
    graph.remove("jolt")
    assert graph.blocks[a.id].then == {} and "jolt" not in graph.blocks
    assert graph.to_list()[0] == {"id": "event1", "type": "on_event", "event": "hit", "pos": [10, 20]}


def test_a_reaction_is_cut_to_the_shows_limits() -> None:
    block = make_block({"id": "big", "type": "react", "accel": [9.0, -9.0, 1.0], "rates": [1.5, 0.0, 0.0], "seconds": 8.0,
                        "effects": {"wind": 1.0}, "effect_seconds": 100.0})
    r = reaction_of(block, Limits())
    assert r.accel == (4.0, -4.0, 1.0) and r.rates == (0.6, 0.0, 0.0) and r.seconds == 3.0 and r.effect_seconds == 30.0 and r.moves
    with pytest.raises(LogicError, match="reaction limits"):
        Limits(max_accel=40.0).check()


# ------------------------------------------------------------------ rewriting a file
def test_blocks_are_replaced_in_a_file_without_touching_anything_else() -> None:
    graph = Graph.from_list(tomllib.loads(EXAMPLE)["play"]["blocks"])
    graph.add("on_event", (40, 40), event="bonus")
    text = replace_blocks(EXAMPLE, graph)
    before, after = tomllib.loads(EXAMPLE), tomllib.loads(text)
    assert [b["id"] for b in after["play"]["blocks"]][-1] == "event1"
    assert {k: v for k, v in after["play"].items() if k != "blocks"} == {k: v for k, v in before["play"].items() if k != "blocks"}
    assert after["live"] == before["live"] and after["cueing"] == before["cueing"]
    assert '# controller = "controller.toml"' not in text and "# overall strength, 0..2 (start low)" in text   # comments stay
    assert "[[play.blocks]]                  # when the game says" not in text            # those inside the blocks go with them
    assert replace_blocks(text, graph) == text                                            # and again: nothing changes
    empty = replace_blocks(EXAMPLE, Graph())
    assert "blocks" not in tomllib.loads(empty)["play"] and "[[play.seats]]" in empty


def test_a_file_that_cannot_be_rewritten_safely_is_left_alone() -> None:
    odd = '[play]\ntitle = "x"\n\n[[play.blocks]]\nid = "a"\ntype = "delay"\n\n[play.limits]\nmax_accel = 2.0\n\n[[play.blocks]]\nid = "b"\ntype = "delay"\n'
    text = replace_blocks(odd, Graph.from_list(tomllib.loads(odd)["play"]["blocks"]))
    assert tomllib.loads(text)["play"]["limits"] == {"max_accel": 2.0}                  # a table between the blocks is kept
    with pytest.raises(PlayConfigError, match="could not be rewritten safely"):
        replace_blocks('[play]\nblocks = [{id = "a", type = "delay"}]\n', Graph())        # written as an inline list


# ------------------------------------------------------------------ how the blocks behave
def test_an_event_starts_its_chain_for_the_seat_it_is_about() -> None:
    lg, host = logic([{"id": "hit", "type": "on_event", "event": "hit", "then": ["pts", "jolt"]},
                      {"id": "pts", "type": "score", "points": -5.0, "then": ["tell"]},
                      {"id": "jolt", "type": "react", "accel": [0, 0, 2]}, {"id": "tell", "type": "send", "event": "ouch"},
                      {"id": "any", "type": "on_event", "event": "*", "then": ["tell"]}])
    assert lg.trigger("on_event", Signal("A2", value=0.7, name="hit"), event="hit") == 2
    assert host.done == [("score", "seat", "A2", -5.0), ("react", "seat", "A2", "jolt"), ("send", "ouch", "seat", "A2"),
                         ("send", "ouch", "seat", "A2")]
    assert lg.trigger("on_event", Signal("A1"), event="other") == 1 and lg.runs == 5


def test_filter_lets_through_by_seat_team_value_and_choice() -> None:
    lg, host = logic(chain({"id": "f", "type": "filter", "team": "red", "min": 0.5, "max": 2.0}))
    for seat, value in (("A1", 1.0), ("A2", 1.0), ("A3", 0.2), ("A3", 2.0), ("A1", 2.5)):
        lg.trigger("on_event", Signal(seat, value=value), event="go")
    assert sent(host) == ["A1", "A3"] and sent(host, "not") == ["A2", "A3", "A1"]
    lg, host = logic(chain({"id": "f", "type": "filter", "seat": "A2", "choice": "Left"}))
    lg.trigger("on_event", Signal("A2", choice="Left"), event="go")
    lg.trigger("on_event", Signal("A2", choice="Right"), event="go")
    lg.trigger("on_event", Signal("A1", choice="Left"), event="go")
    assert sent(host) == ["A2"] and len(sent(host, "not")) == 2


def test_chance_cooldown_and_count() -> None:
    lg, host = logic(chain({"id": "c", "type": "chance", "percent": 25.0}), seed=7)
    for _ in range(2000):
        lg.trigger("on_event", Signal("A1"), event="go")
    assert 430 < len(sent(host)) < 570 and len(sent(host)) + len(sent(host, "not")) == 2000

    lg, host = logic(chain({"id": "c", "type": "cooldown", "seconds": 1.0}))
    for t, seat in ((0.0, "A1"), (0.3, "A1"), (0.3, "A2"), (0.9, "A1"), (1.0, "A1"), (1.2, "A2"), (1.31, "A2")):
        lg.step(t)
        lg.trigger("on_event", Signal(seat), event="go")
    assert sent(host) == ["A1", "A2", "A1", "A2"] and sent(host, "not") == ["A1", "A1", "A2"]      # per seat

    lg, host = logic(chain({"id": "c", "type": "count", "n": 3, "per": "team", "within": 2.0}))
    for t, seat in ((0.0, "A1"), (0.5, "A3"), (0.6, "A2"), (1.0, "A1"), (1.1, "A3"), (4.0, "A1"), (4.1, "A3"), (6.5, "A1")):
        lg.step(t)
        lg.trigger("on_event", Signal(seat), event="go")
    assert sent(host) == ["A1"]            # red's third within 2 s; then counting starts again, and old ones are forgotten


def test_delay_waits_on_the_session_clock_and_a_loop_with_a_delay_is_a_rhythm() -> None:
    lg, host = logic(chain({"id": "d", "type": "delay", "seconds": 2.0}))
    lg.step(10.0)
    lg.trigger("on_event", Signal("A1"), event="go")
    lg.step(11.9)
    assert sent(host) == []
    lg.step(12.0)
    assert sent(host) == ["A1"]
    lg, host = logic([{"id": "go", "type": "on_phase", "phase": "running", "then": ["beat"]},
                      {"id": "beat", "type": "send", "event": "beat", "to": "all", "then": ["wait"]},
                      {"id": "wait", "type": "delay", "seconds": 0.5, "then": ["beat"]}])
    lg.trigger("on_phase", phase="running")
    for k in range(1, 41):
        lg.step(k * 0.05)
    assert len(sent(host, "beat")) == 5                                                 # at 0, 0.5, 1, 1.5, 2


def test_together_needs_enough_seats_in_time() -> None:
    blocks = chain({"id": "t", "type": "together", "seconds": 2.0, "share": 1.0})
    lg, host = logic(blocks)
    for t, seat in ((0.0, "A1"), (0.5, "A2"), (0.6, "A2"), (1.5, "A3")):
        lg.step(t)
        lg.trigger("on_event", Signal(seat), event="go")
    assert sent(host) == [None] and sent(host, "not") == []                              # everyone: about nobody in particular
    lg.step(5.0)
    assert sent(host, "not") == []                                                      # the window was closed with it
    for t, seat in ((6.0, "A1"), (6.5, "A2")):
        lg.step(t)
        lg.trigger("on_event", Signal(seat), event="go")
    lg.step(7.9)
    assert sent(host, "not") == []
    lg.step(8.01)
    assert sent(host, "not") == [None] and sent(host) == [None]                          # too few: the 'else' wire
    blocks[1]["share"] = 0.5
    lg, host = logic(blocks)
    lg.trigger("on_event", Signal("A1"), event="go")
    assert sent(host) == []
    lg.trigger("on_event", Signal("A3"), event="go")
    assert sent(host) == [None]                                                         # 2 of 3 is at least half


def test_times_of_the_session_and_of_a_film() -> None:
    at = {"id": "go", "type": "on_time", "at": 3.0}
    lg, host = logic([*chain(start=dict(at)), {"id": "tick", "type": "every", "seconds": 1.0, "then": ["done"]},
                      {"id": "zero", "type": "on_time", "at": 0.0, "then": ["not"]}])
    for k in range(0, 101):
        lg.step(k * 0.05)
    assert len(sent(host)) == 1 + 5 and len(sent(host, "not")) == 1                      # once at 3 s; every second 1..5; at 0
    lg.step(60.0)
    assert len(sent(host)) == 7                                                         # a long gap: one tick, no burst

    lg, host = logic(chain(start={**at, "clock": "film", "at": 30.0, "programme": 2}))
    lg.step(50.0)
    assert sent(host) == []                                                             # the session's clock is not the film's
    lg.film_position(0.2, 2)
    lg.film_position(31.0, 2)
    assert sent(host) == [] and lg.dropped == 1                                         # a jump over the cue: left out
    lg.film_position(0.1, 2)                                                            # the film starts again
    for k in range(1, 320):
        lg.film_position(k * 0.1, 2)
    assert len(sent(host)) == 1
    lg.film_position(0.0, 3)                                                            # another programme
    for k in range(1, 320):
        lg.film_position(k * 0.1, 3)
    assert len(sent(host)) == 1
    lg, host = logic(chain(start={**at, "clock": "film", "at": 30.0}))
    lg.film_position(200.0)                                                             # joined in the middle: no old cues
    lg.film_position(200.3)
    assert sent(host) == []


def test_actions_and_what_follows_a_prompt() -> None:
    lg, host = logic([{"id": "go", "type": "on_input", "button": "b", "then": ["s", "q", "f", "p"]},
                      {"id": "s", "type": "set_state", "key": "doors", "add": 1.0},
                      {"id": "q", "type": "prompt", "text": "Left or right?", "choices": ["Left", "Right"], "then": {"a": ["l"], "none": ["n"]}},
                      {"id": "f", "type": "film", "action": "play", "programme": 4}, {"id": "p", "type": "phase", "action": "pause"},
                      {"id": "l", "type": "send", "event": "left"}, {"id": "n", "type": "send", "event": "nobody"}])
    assert lg.trigger("on_input", Signal("A1", name="a"), button="a", edge="press") == 0
    assert lg.trigger("on_input", Signal("A1", name="b"), button="b", edge="release") == 0
    assert lg.trigger("on_input", Signal("A1", name="b"), button="b", edge="press") == 1
    assert host.done == [("state", "doors", "", 1.0), ("prompt", "q", "A1"), ("film", "play", 4), ("phase", "pause")]
    lg.prompt_closed("q", [("a", Signal("A1", choice="Left")), ("b", Signal("A2", choice="Right")), ("none", Signal("A3"))])
    assert host.done[4:] == [("send", "left", "seat", "A1"), ("send", "nobody", "seat", "A3")]


def test_a_runaway_chain_is_cut_off() -> None:
    blocks: list[dict[str, Any]] = [{"id": "go", "type": "on_event", "event": "go", "then": ["n0"]}]
    blocks += [{"id": f"n{k}", "type": "send", "event": "x", "then": [f"n{k + 1}", f"n{k + 1}b"] if k < 9 else []} for k in range(10)]
    blocks += [{"id": f"n{k}b", "type": "send", "event": "x", "then": [f"n{k + 1}", f"n{k + 1}b"] if k < 9 else []} for k in range(1, 10)]
    lg, host = logic(blocks)                                                            # 2^10 paths
    lg.trigger("on_event", event="go")
    assert len(host.done) == Logic.BUDGET and lg.dropped > 0 and "more than 400 blocks ran" in lg.problem
