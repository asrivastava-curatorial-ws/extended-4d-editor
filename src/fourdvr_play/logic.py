"""Interaction logic: blocks, and what follows what.

A block is one small step - something that happens (*when*), a condition (*if*) or something to do
(*do*). Every block names the blocks that follow it (``then``), so a show's logic is a set of short
chains: "when the event *hit* arrives -> 10 points to that seat -> jolt that seat". The configuration
file holds them as ``[[play.blocks]]``; the Editor's interaction window draws the same blocks as boxes
and wires. Nothing here does I/O: the hub is the ``Host`` that carries the actions out.

What travels along a wire is a ``Signal``: which seat it is about (or nobody: everyone), a number and
a choice. Time is the session's own clock (it stands still while the session is paused).
"""

from __future__ import annotations

import heapq
import math
import random
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")
MAX_BLOCKS = 500
TARGETS = ("seat", "team", "others", "all")
PHASES = ("lobby", "countdown", "running", "paused", "results")
ANSWERS = ("a", "b", "c", "d")
QUIET = ("to", "edge", "clock", "per", "who", "mode", "correct")      # choices not written while at their default
SLOW = ("delay", "prompt", "together")        # blocks that take time: a loop must pass through one of them


class LogicError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    seat: str | None = None        # the seat this is about; None: nobody in particular (everyone)
    team: str = ""
    value: float = 0.0
    choice: str = ""
    name: str = ""                 # the event / button / key that started it


@dataclass(frozen=True)
class Reaction:
    """What a seat feels: a push (acceleration and rotation-rate pulse) and effect levels for a while."""

    accel: tuple[float, float, float] = (0.0, 0.0, 0.0)      # surge, sway, heave (m/s^2)
    rates: tuple[float, float, float] = (0.0, 0.0, 0.0)      # roll, pitch, yaw (rad/s)
    seconds: float = 0.3
    effects: Mapping[str, float] = field(default_factory=dict)
    effect_seconds: float = 0.5

    @property
    def moves(self) -> bool:
        return any(self.accel) or any(self.rates)


@dataclass(frozen=True)
class Limits:
    """How strong a reaction may be asked for (the platform's own limits apply underneath, as always)."""

    max_accel: float = 4.0         # m/s^2
    max_rate: float = 0.6          # rad/s
    max_seconds: float = 3.0
    max_effect_seconds: float = 30.0

    def check(self) -> None:
        if not (0.0 < self.max_accel <= 10.0 and 0.0 < self.max_rate <= 2.0 and 0.05 <= self.max_seconds <= 10.0
                and 0.1 <= self.max_effect_seconds <= 120.0):
            raise LogicError("reaction limits: max_accel 0-10 m/s², max_rate 0-2 rad/s, max_seconds 0.05-10, "
                             "max_effect_seconds 0.1-120")


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                      # text, number, number?, int, choice, vector, levels, list
    default: Any
    label: str
    options: tuple[str, ...] = ()
    low: float = -1e9
    high: float = 1e9
    required: bool = False
    unit: str = ""


@dataclass(frozen=True)
class BlockType:
    name: str
    title: str
    group: str                     # when, if, do
    summary: str
    params: tuple[Param, ...] = ()
    outputs: tuple[str, ...] = ("then",)

    @property
    def has_input(self) -> bool:
        return self.group != "when"

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)


_TO = Param("to", "choice", "seat", "for", TARGETS)
_TYPES = (
    BlockType("on_event", "When an event arrives", "when", "the game, a page or another program sends this event",
              (Param("event", "text", "hit", "event", required=True),)),
    BlockType("on_input", "When a button is pressed", "when", "a seat button, a phone button or a controller",
              (Param("button", "text", "", "button (empty: any)"), Param("edge", "choice", "press", "on", ("press", "release")))),
    BlockType("on_phase", "When the session changes", "when", "the session enters this phase",
              (Param("phase", "choice", "running", "phase", PHASES),)),
    BlockType("on_time", "At a time", "when", "once, at this second of the session or of the film",
              (Param("at", "number", 10.0, "at", low=0.0, high=86400.0, unit="s"),
               Param("clock", "choice", "session", "clock", ("session", "film")),
               Param("programme", "int", 0, "programme (0: any)", low=0, high=1e9))),
    BlockType("every", "Every so often", "when", "again and again while the session runs",
              (Param("seconds", "number", 10.0, "every", low=0.2, high=3600.0, unit="s"),)),
    BlockType("on_score", "When a score is reached", "when", "a seat's or a team's score reaches this",
              (Param("reaches", "number", 100.0, "reaches", low=-1e6, high=1e6),
               Param("who", "choice", "seat", "whose", ("seat", "team")))),
    BlockType("on_state", "When a shared value changes", "when", "a value of the shared state is set",
              (Param("key", "text", "", "key", required=True), Param("equals", "text", "", "equals (empty: any change)"))),
    BlockType("filter", "Only if", "if", "lets the signal pass for one seat, one team, a range of values or one choice",
              (Param("seat", "text", "", "seat (empty: any)"), Param("team", "text", "", "team (empty: any)"),
               Param("min", "number?", None, "value at least"), Param("max", "number?", None, "value at most"),
               Param("choice", "text", "", "choice (empty: any)")), ("then", "else")),
    BlockType("chance", "By chance", "if", "passes this often, at random",
              (Param("percent", "number", 50.0, "chance", low=0.0, high=100.0, unit="%"),), ("then", "else")),
    BlockType("cooldown", "Not too often", "if", "passes at most once in this time",
              (Param("seconds", "number", 1.0, "at most once in", low=0.05, high=3600.0, unit="s"),
               Param("per", "choice", "seat", "counted per", ("seat", "all"))), ("then", "else")),
    BlockType("count", "Every n-th time", "if", "passes on the n-th signal, then starts counting again",
              (Param("n", "int", 3, "n", low=2, high=1000), Param("per", "choice", "seat", "counted per", ("seat", "team", "all")),
               Param("within", "number", 0.0, "within (0: any time)", low=0.0, high=3600.0, unit="s"))),
    BlockType("delay", "Wait", "if", "passes the signal on after this time",
              (Param("seconds", "number", 1.0, "wait", low=0.05, high=3600.0, unit="s"),)),
    BlockType("together", "All together", "if", "passes once enough seats have signalled within the time",
              (Param("seconds", "number", 2.0, "within", low=0.2, high=60.0, unit="s"),
               Param("share", "number", 1.0, "share of the seats", low=0.1, high=1.0)), ("then", "else")),
    BlockType("score", "Give points", "do", "adds points (negative: takes them)",
              (Param("points", "number", 10.0, "points", low=-1e6, high=1e6), _TO)),
    BlockType("react", "Move and effects", "do", "a push of the seat and effects for a moment",
              (_TO, Param("accel", "vector", (0.0, 0.0, 0.0), "push: forward, left, up", high=10.0, unit="m/s²"),
               Param("rates", "vector", (0.0, 0.0, 0.0), "turn: roll, pitch, yaw", high=2.0, unit="rad/s"),
               Param("seconds", "number", 0.3, "push for", low=0.05, high=10.0, unit="s"),
               Param("effects", "levels", {}, "effects (name = level 0-1)"),
               Param("effect_seconds", "number", 0.5, "effects for", low=0.05, high=120.0, unit="s"))),
    BlockType("set_state", "Set a shared value", "do", "sets or adds to a value everybody sees",
              (Param("key", "text", "", "key", required=True), Param("value", "text", "", "set to (empty: add)"),
               Param("add", "number", 0.0, "add", low=-1e6, high=1e6))),
    BlockType("send", "Tell the game", "do", "sends an event to the game and the pages",
              (Param("event", "text", "", "event", required=True), _TO)),
    BlockType("prompt", "Ask", "do", "shows a question in the headsets and on the phones; a wire per answer",
              (Param("text", "text", "", "question", required=True), Param("choices", "list", ("Yes", "No"), "answers (2-4)"),
               Param("seconds", "number", 10.0, "time to answer", low=2.0, high=120.0, unit="s"),
               Param("to", "choice", "all", "asked", TARGETS), Param("mode", "choice", "each", "counts", ("each", "vote")),
               Param("correct", "choice", "", "right answer", ("", *ANSWERS)),
               Param("points", "number", 0.0, "points for the right answer", low=0.0, high=1e6)),
              (*ANSWERS, "none")),
    BlockType("film", "Film", "do", "pauses, resumes or stops the film, or starts another programme",
              (Param("action", "choice", "pause", "action", ("pause", "resume", "stop", "play")),
               Param("programme", "int", 0, "programme (for play)", low=0, high=1e9))),
    BlockType("phase", "Session", "do", "ends, pauses or resumes the session",
              (Param("action", "choice", "end", "action", ("end", "pause", "resume")),)),
)
TYPES: dict[str, BlockType] = {t.name: t for t in _TYPES}
GROUPS = {"when": "When", "if": "If", "do": "Do"}


def _number(value: Any, p: Param, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise LogicError(f"{where}: {p.name} must be a number")
    if not p.low <= float(value) <= p.high:
        raise LogicError(f"{where}: {p.name} must be between {p.low:g} and {p.high:g}")
    return float(value)


def clean(p: Param, value: Any, where: str) -> Any:
    """The value of one setting, checked and in its plain form."""
    if p.kind == "text":
        if not isinstance(value, str) or len(value) > 200:
            raise LogicError(f"{where}: {p.name} must be a text of at most 200 characters")
        text = value.strip()
        if p.required and not text:
            raise LogicError(f"{where}: {p.name} is missing")
        if text and p.name in ("event", "button", "key", "seat", "team") and not NAME_PATTERN.match(text) and text != "*":
            raise LogicError(f"{where}: {p.name} {text!r} may hold letters, digits and . _ : - only")
        return text
    if p.kind == "number":
        return _number(value, p, where)
    if p.kind == "number?":
        return None if value is None or value == "" else _number(value, p, where)
    if p.kind == "int":
        number = _number(value, p, where)
        if number != int(number):
            raise LogicError(f"{where}: {p.name} must be a whole number")
        return int(number)
    if p.kind == "choice":
        if value not in p.options:
            raise LogicError(f"{where}: {p.name} must be one of {', '.join(o or '(none)' for o in p.options)}")
        return str(value)
    if p.kind == "vector":
        if not isinstance(value, list | tuple) or len(value) != 3:
            raise LogicError(f"{where}: {p.name} must be three numbers")
        limit = Param(p.name, "number", 0.0, p.label, low=-p.high, high=p.high)
        return tuple(_number(v, limit, where) for v in value)
    if p.kind == "levels":
        if not isinstance(value, Mapping) or len(value) > 12:
            raise LogicError(f"{where}: {p.name} must be a table of at most 12 effect levels")
        levels: dict[str, float] = {}
        for name, level in value.items():
            if not isinstance(name, str) or not NAME_PATTERN.match(name):
                raise LogicError(f"{where}: {name!r} is not an effect name")
            levels[name] = _number(level, Param(name, "number", 0.0, name, low=0.0, high=1.0), where)
        return levels
    if p.kind == "list":
        if not isinstance(value, list | tuple) or not 2 <= len(value) <= 4:
            raise LogicError(f"{where}: {p.name} must be 2 to 4 texts")
        items = tuple(str(v).strip() for v in value)
        if any(not v or len(v) > 80 for v in items):
            raise LogicError(f"{where}: every entry of {p.name} needs 1-80 characters")
        return items
    raise LogicError(f"{where}: unknown kind of setting {p.kind!r}")       # pragma: no cover - a bug in the table


@dataclass
class Block:
    id: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    then: dict[str, list[str]] = field(default_factory=dict)
    pos: tuple[float, float] | None = None

    @property
    def kind(self) -> BlockType:
        return TYPES[self.type]

    def label(self) -> str:
        """One line that says what this block does (boxes in the Editor, messages)."""
        p, t = self.params, self.type
        if t == "on_event":
            return f"event “{p['event']}”"
        if t == "on_input":
            return f"button {p['button'] or 'any'} {p['edge']}"
        if t == "on_phase":
            return f"session: {p['phase']}"
        if t == "on_time":
            return f"at {p['at']:g} s of the {p['clock']}"
        if t == "every":
            return f"every {p['seconds']:g} s"
        if t == "on_score":
            return f"{p['who']} reaches {p['reaches']:g}"
        if t == "on_state":
            return f"{p['key']}" + (f" = {p['equals']}" if p["equals"] else " changes")
        if t == "filter":
            parts = [f"seat {p['seat']}" if p["seat"] else "", f"team {p['team']}" if p["team"] else "",
                     f"≥ {p['min']:g}" if p["min"] is not None else "", f"≤ {p['max']:g}" if p["max"] is not None else "",
                     f"choice {p['choice']}" if p["choice"] else ""]
            return ", ".join(x for x in parts if x) or "anything"
        if t == "chance":
            return f"{p['percent']:g} %"
        if t == "cooldown":
            return f"once in {p['seconds']:g} s per {p['per']}"
        if t == "count":
            return f"every {p['n']}. time per {p['per']}"
        if t == "delay":
            return f"wait {p['seconds']:g} s"
        if t == "together":
            return f"{round(p['share'] * 100)} % of the seats within {p['seconds']:g} s"
        if t == "score":
            return f"{p['points']:+g} points: {p['to']}"
        if t == "react":
            parts = ["push" if any(p["accel"]) or any(p["rates"]) else "", ", ".join(p["effects"])]
            return f"{' + '.join(x for x in parts if x) or 'nothing yet'}: {p['to']}"
        if t == "set_state":
            return f"{p['key']} = {p['value']}" if p["value"] else f"{p['key']} {p['add']:+g}"
        if t == "send":
            return f"event “{p['event']}”: {p['to']}"
        if t == "prompt":
            return f"“{p['text'][:28]}” ({p['mode']})"
        if t == "film":
            return f"{p['action']}" + (f" programme {p['programme']}" if p["action"] == "play" else "")
        return f"{p['action']} the session"


class Graph:
    """The blocks of a show, checked. ``from_list`` reads what the configuration holds, ``to_list``
    writes it back (settings at their default value are left out)."""

    def __init__(self, blocks: Iterable[Block] = ()) -> None:
        self.blocks: dict[str, Block] = {}
        for block in blocks:
            if block.id in self.blocks:
                raise LogicError(f"two blocks are called {block.id!r}")
            self.blocks[block.id] = block

    # ------------------------------------------------------------------ reading and writing
    @classmethod
    def from_list(cls, data: Any, lenient: bool = False) -> Graph:
        """`lenient`: texts that must be given may still be empty (work in progress in the Editor)."""
        if not isinstance(data, list):
            raise LogicError("[[play.blocks]] must be a list of tables")
        if len(data) > MAX_BLOCKS:
            raise LogicError(f"more than {MAX_BLOCKS} blocks")
        graph = cls(make_block(entry, lenient) for entry in data)
        graph.check()
        return graph

    def incomplete(self) -> list[tuple[str, str]]:
        """(block, what is missing) for every setting that must be given and is not."""
        missing: list[tuple[str, str]] = []
        for block in self.blocks.values():
            for p in block.kind.params:
                if p.required and not block.params[p.name]:
                    missing.append((block.id, f"block {block.id} ({block.kind.title.lower()}): {p.label} is missing"))
        return missing

    def to_list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for b in self.blocks.values():
            entry: dict[str, Any] = {"id": b.id, "type": b.type}
            for p in b.kind.params:
                value = b.params[p.name]
                plain = value is None or value in ("", 0) or (p.kind in ("vector", "levels") and not any(value))
                if value is None or (value == p.default and (plain or (p.kind == "choice" and p.name in QUIET))):
                    continue
                entry[p.name] = list(value) if isinstance(value, tuple) else (dict(value) if isinstance(value, Mapping) else value)
            wired = {o: list(ids) for o, ids in b.then.items() if ids}
            if wired:
                entry["then"] = wired["then"] if set(wired) == {"then"} else wired
            if b.pos is not None:
                entry["pos"] = [round(b.pos[0]), round(b.pos[1])]
            out.append(entry)
        return out

    # ------------------------------------------------------------------ checks
    def check(self) -> None:
        for b in self.blocks.values():
            for targets in b.then.values():
                for target in targets:
                    other = self.blocks.get(target)
                    if other is None:
                        raise LogicError(f"block {b.id}: 'then' names {target!r}, which does not exist")
                    if not other.kind.has_input:
                        raise LogicError(f"block {b.id}: {target!r} is a 'when' block; nothing can lead to it")
                    if target == b.id and b.type not in SLOW:
                        raise LogicError(f"block {b.id} leads to itself")
        loop = self.fast_loop()
        if loop:
            raise LogicError(f"blocks {' -> '.join(loop)} form a loop without a 'delay' block: it would never end")

    def fast_loop(self) -> list[str]:
        """A loop that passes through no time-taking block (empty list: none)."""
        state: dict[str, int] = {}
        path: list[str] = []

        def visit(bid: str) -> list[str]:
            state[bid] = 1
            path.append(bid)
            block = self.blocks[bid]
            if block.type not in SLOW:
                for targets in block.then.values():
                    for target in targets:
                        if target not in self.blocks:
                            continue
                        if state.get(target) == 1:
                            return [*path[path.index(target):], target]
                        if state.get(target) is None:
                            found = visit(target)
                            if found:
                                return found
            path.pop()
            state[bid] = 2
            return []

        for bid in self.blocks:
            if state.get(bid) is None:
                found = visit(bid)
                if found:
                    return found
        return []

    def notes(self) -> list[str]:
        """Things that are allowed but probably not meant."""
        notes: list[str] = []
        reached = {t for b in self.blocks.values() for ids in b.then.values() for t in ids}
        for b in self.blocks.values():
            wired = any(b.then.values())
            if b.kind.group == "when" and not wired:
                notes.append(f"block {b.id} ({b.kind.title.lower()}): nothing follows it")
            if b.kind.has_input and b.id not in reached:
                notes.append(f"block {b.id} ({b.kind.title.lower()}): nothing leads to it")
            if b.type == "react" and not (any(b.params["accel"]) or any(b.params["rates"]) or b.params["effects"]):
                notes.append(f"block {b.id}: no push and no effect is set")
            if b.type == "prompt":
                unused = [a for a, _ in zip(ANSWERS, b.params["choices"], strict=False)
                          if not b.then.get(a)] if b.params["mode"] == "vote" else []
                if b.params["correct"] and b.params["correct"] not in ANSWERS[: len(b.params["choices"])]:
                    notes.append(f"block {b.id}: the right answer '{b.params['correct']}' is not among the answers")
                if unused and len(unused) == len(b.params["choices"]) and not b.then.get("none"):
                    notes.append(f"block {b.id}: a vote without anything following its answers")
        return notes

    # ------------------------------------------------------------------ editing (the Editor's window)
    def new_id(self, type_name: str) -> str:
        stem = type_name.removeprefix("on_")
        n = 1
        while f"{stem}{n}" in self.blocks:
            n += 1
        return f"{stem}{n}"

    def add(self, type_name: str, pos: tuple[float, float] | None = None, **params: Any) -> Block:
        block = make_block({"id": self.new_id(type_name), "type": type_name, **params}, lenient=True)
        block.pos = pos
        self.blocks[block.id] = block
        return block

    def remove(self, block_id: str) -> None:
        self.blocks.pop(block_id, None)
        for b in self.blocks.values():
            b.then = {output: kept for output, ids in b.then.items() if (kept := [t for t in ids if t != block_id])}

    def rename(self, old: str, new: str) -> None:
        if old == new:
            return
        if not ID_PATTERN.match(new):
            raise LogicError(f"{new!r} is not a name for a block (letters, digits, . _ -; at most 40)")
        if new in self.blocks:
            raise LogicError(f"a block called {new!r} exists already")
        self.blocks = {(new if k == old else k): b for k, b in self.blocks.items()}
        self.blocks[new].id = new
        for b in self.blocks.values():
            for ids in b.then.values():
                ids[:] = [new if t == old else t for t in ids]

    def can_connect(self, source: str, output: str, target: str) -> str:
        """Empty text if the wire is allowed, else why not."""
        a, b = self.blocks.get(source), self.blocks.get(target)
        if a is None or b is None:
            return "no such block"
        if output not in a.kind.outputs:
            return f"{a.kind.title} has no output '{output}'"
        if not b.kind.has_input:
            return "nothing can lead to a 'when' block"
        if target in a.then.get(output, []):
            return "already connected"
        if source == target and a.type not in SLOW:
            return "a block cannot lead to itself"
        a.then.setdefault(output, []).append(target)
        loop = self.fast_loop()
        self.disconnect(source, output, target)
        return "this would make a loop without a 'delay' block" if loop else ""

    def connect(self, source: str, output: str, target: str) -> None:
        problem = self.can_connect(source, output, target)
        if problem:
            raise LogicError(problem)
        self.blocks[source].then.setdefault(output, []).append(target)

    def disconnect(self, source: str, output: str, target: str) -> None:
        wires = self.blocks[source].then
        ids = wires.get(output, [])
        if target in ids:
            ids.remove(target)
        if output in wires and not ids:
            del wires[output]


def chains(graph: Graph, limit: int = 60) -> list[str]:
    """The logic in words: one indented line per block, from each 'when' block on."""
    lines: list[str] = []

    def walk(block: Block, depth: int, via: str, seen: tuple[str, ...]) -> None:
        if len(lines) >= limit:
            return
        arrow = "" if depth == 0 else ("-> " if via == "then" else f"-> [{via}] ")
        again = block.id in seen
        lines.append(f"{'   ' * depth}{arrow}{block.kind.title.lower()}: {block.label()}" + ("  (see above)" if again else ""))
        if again:
            return
        for output in block.kind.outputs:
            for target in block.then.get(output, ()):
                nxt = graph.blocks.get(target)
                if nxt is not None:
                    walk(nxt, depth + 1, output, (*seen, block.id))

    for block in graph.blocks.values():
        if block.kind.group == "when":
            walk(block, 0, "then", ())
    if len(lines) >= limit:
        lines.append("...")
    return lines


def make_block(entry: Any, lenient: bool = False) -> Block:
    """One ``[[play.blocks]]`` table as a block. ``lenient``: required texts may still be empty (a block
    that was just added in the Editor)."""
    if not isinstance(entry, Mapping):
        raise LogicError("each [[play.blocks]] entry must be a table")
    bid = entry.get("id")
    if not isinstance(bid, str) or not ID_PATTERN.match(bid):
        raise LogicError(f"a block needs id = \"name\" (letters, digits, . _ -; at most 40), not {bid!r}")
    kind = TYPES.get(str(entry.get("type", "")))
    where = f"block {bid}"
    if kind is None:
        raise LogicError(f"{where}: unknown type {entry.get('type')!r} (types: {', '.join(TYPES)})")
    unknown = sorted(set(entry) - {"id", "type", "then", "pos"} - {p.name for p in kind.params})
    if unknown:
        raise LogicError(f"{where} ({kind.name}): unknown key(s) {', '.join(unknown)}")
    params: dict[str, Any] = {}
    for p in kind.params:
        value = entry.get(p.name, p.default)
        params[p.name] = clean(Param(p.name, p.kind, p.default, p.label, p.options, p.low, p.high, False, p.unit)
                               if lenient else p, value, where)
    then: dict[str, list[str]] = {}
    raw = entry.get("then", [])
    wires = {kind.outputs[0]: raw} if isinstance(raw, list | str) else raw
    if not isinstance(wires, Mapping):
        raise LogicError(f"{where}: then must be a list of block names, or a table of them per output")
    for output, targets in wires.items():
        if output not in kind.outputs:
            raise LogicError(f"{where}: {kind.name} has no output {output!r} (outputs: {', '.join(kind.outputs)})")
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or any(not isinstance(t, str) for t in targets):
            raise LogicError(f"{where}: then.{output} must be a list of block names")
        if targets:
            then[str(output)] = list(dict.fromkeys(targets))
    pos = entry.get("pos")
    if pos is not None and not (isinstance(pos, list | tuple) and len(pos) == 2
                                and all(isinstance(v, int | float) and math.isfinite(v) for v in pos)):
        raise LogicError(f"{where}: pos must be two numbers")
    return Block(bid, kind.name, params, then, (float(pos[0]), float(pos[1])) if pos is not None else None)


def reaction_of(block: Block, limits: Limits) -> Reaction:
    """The reaction a ``react`` block asks for, cut to the show's limits."""
    p = block.params

    def cut(values: Iterable[float], limit: float) -> tuple[float, float, float]:
        a, b, c = (max(-limit, min(limit, float(v))) for v in values)
        return (a, b, c)

    return Reaction(cut(p["accel"], limits.max_accel), cut(p["rates"], limits.max_rate),
                    min(p["seconds"], limits.max_seconds), dict(p["effects"]),
                    min(p["effect_seconds"], limits.max_effect_seconds))


class Host(Protocol):
    """What the logic needs done (the hub)."""

    def targets(self, to: str, signal: Signal) -> list[str]: ...
    def active_seats(self) -> list[str]: ...
    def team_of(self, seat: str) -> str: ...
    def add_score(self, to: str, signal: Signal, points: float) -> None: ...
    def react(self, to: str, signal: Signal, block: Block) -> None: ...
    def change_state(self, key: str, value: str, add: float) -> None: ...
    def send_event(self, name: str, to: str, signal: Signal) -> None: ...
    def open_prompt(self, block: Block, signal: Signal) -> None: ...
    def film(self, action: str, programme: int) -> None: ...
    def change_phase(self, to: str) -> None: ...


class Logic:
    """Runs a graph. ``trigger`` starts chains, ``step`` moves the session clock (timers, delays)."""

    BUDGET = 400                   # blocks run for one thing that happened; beyond it the rest is dropped

    def __init__(self, graph: Graph, host: Host, rng: random.Random | None = None) -> None:
        self.graph = graph
        self.host = host
        self.rng = rng or random.Random()
        self.by_type: dict[str, list[Block]] = {}
        for block in graph.blocks.values():
            self.by_type.setdefault(block.type, []).append(block)
        self.runs = 0
        self.dropped = 0
        self.problem = ""
        self.reset()

    def reset(self) -> None:
        self._clock = -1e-9
        self._timers: list[tuple[float, int, str, str, Signal]] = []
        self._n = 0
        self._last: dict[tuple[str, str], float] = {}
        self._counts: dict[tuple[str, str], deque[float]] = {}
        self._together: dict[str, dict[str, float]] = {}
        self._every: dict[str, float] = {}
        self._film: float | None = None
        self._programme = 0

    @property
    def clock(self) -> float:
        return max(0.0, self._clock)

    # ------------------------------------------------------------------ from outside
    def trigger(self, kind: str, signal: Signal | None = None, **match: Any) -> int:
        """Something happened: start every matching 'when' block. Returns how many matched."""
        signal = signal or Signal()
        started = 0
        for block in self.by_type.get(kind, ()):
            p = block.params
            if kind == "on_event" and p["event"] not in ("*", match.get("event")):
                continue
            if kind == "on_input" and ((p["button"] and p["button"] != match.get("button")) or p["edge"] != match.get("edge", "press")):
                continue
            if kind == "on_phase" and p["phase"] != match.get("phase"):
                continue
            if kind == "on_state" and (p["key"] != match.get("key") or (p["equals"] and p["equals"] != str(match.get("value")))):
                continue
            started += 1
            self._emit(block, "then", signal)
        return started

    def score_changed(self, who: str, name: str, before: float, after: float) -> None:
        for block in self.by_type.get("on_score", ()):
            p = block.params
            if p["who"] == who and before < p["reaches"] <= after:
                self._emit(block, "then", Signal(seat=name, value=after) if who == "seat" else Signal(team=name, value=after))

    def step(self, session_s: float) -> None:
        """The session clock has reached `session_s`."""
        before, self._clock = self._clock, session_s
        for block in self.by_type.get("on_time", ()):
            if block.params["clock"] == "session" and before < block.params["at"] <= session_s:
                self._emit(block, "then", Signal(value=session_s))
        for block in self.by_type.get("every", ()):
            period = block.params["seconds"]
            due = self._every.setdefault(block.id, period)
            if session_s >= due:
                self._every[block.id] = max(due + period, session_s + period * 0.5)   # late: skip, do not catch up in a burst
                self._emit(block, "then", Signal(value=session_s))
        while self._timers and self._timers[0][0] <= session_s:
            _due, _n, bid, output, signal = heapq.heappop(self._timers)
            waiting = self.graph.blocks.get(bid)
            if waiting is None:
                continue
            if output == "?":                       # the window of a 'together' block has ended
                if self._together.pop(bid, None):
                    self._emit(waiting, "else", Signal())
            else:
                self._emit(waiting, output, signal)

    def film_position(self, position_s: float, programme: int = 0) -> None:
        """Where the film is now. Cues between the last position and this one fire; after a jump (another
        programme, a restart, a lost connection) the cues in between are left out."""
        before = self._film
        fresh = before is None or programme != self._programme or position_s < before - 0.5
        self._film, self._programme = position_s, programme
        if fresh:
            before = -1e-9 if position_s < 1.0 else position_s
        assert before is not None
        if position_s - before > 5.0:
            self.dropped += 1
            return
        for block in self.by_type.get("on_time", ()):
            p = block.params
            if p["clock"] == "film" and before < p["at"] <= position_s and p["programme"] in (0, programme):
                self._emit(block, "then", Signal(value=position_s))

    def film_stopped(self) -> None:
        self._film = None

    def prompt_closed(self, block_id: str, outcomes: Iterable[tuple[str, Signal]]) -> None:
        block = self.graph.blocks.get(block_id)
        if block is not None:
            for output, signal in outcomes:
                self._emit(block, output, signal)

    # ------------------------------------------------------------------ running
    def _later(self, seconds: float, block: Block, output: str, signal: Signal) -> None:
        self._n += 1
        heapq.heappush(self._timers, (self.clock + seconds, self._n, block.id, output, signal))

    def _emit(self, block: Block, output: str, signal: Signal) -> None:
        queue: deque[tuple[str, Signal]] = deque((t, signal) for t in block.then.get(output, ()))
        budget = self.BUDGET
        while queue:
            if budget <= 0:
                self.dropped += len(queue)
                self.problem = f"more than {self.BUDGET} blocks ran for one signal after {block.id}; the rest was dropped"
                return
            budget -= 1
            bid, sig = queue.popleft()
            nxt = self.graph.blocks.get(bid)
            if nxt is None:
                continue
            self.runs += 1
            for out, out_sig in self._run(nxt, sig):
                queue.extend((t, out_sig) for t in nxt.then.get(out, ()))

    def _key(self, per: str, signal: Signal) -> str:
        if per == "seat":
            return signal.seat or ""
        if per == "team":
            return signal.team or (self.host.team_of(signal.seat) if signal.seat else "")
        return ""

    def _run(self, block: Block, s: Signal) -> list[tuple[str, Signal]]:
        p, t, host, now = block.params, block.type, self.host, self.clock
        if t == "filter":
            team = s.team or (host.team_of(s.seat) if s.seat else "")
            ok = ((not p["seat"] or p["seat"] == s.seat) and (not p["team"] or p["team"] == team)
                  and (p["min"] is None or s.value >= p["min"]) and (p["max"] is None or s.value <= p["max"])
                  and (not p["choice"] or p["choice"] == s.choice))
            return [("then" if ok else "else", s)]
        if t == "chance":
            return [("then" if self.rng.random() * 100.0 < p["percent"] else "else", s)]
        if t == "cooldown":
            key = (block.id, self._key(p["per"], s))
            if now - self._last.get(key, -1e18) >= p["seconds"]:
                self._last[key] = now
                return [("then", s)]
            return [("else", s)]
        if t == "count":
            times = self._counts.setdefault((block.id, self._key(p["per"], s)), deque())
            times.append(now)
            while p["within"] > 0.0 and times and now - times[0] > p["within"]:
                times.popleft()
            if len(times) >= p["n"]:
                times.clear()
                return [("then", s)]
            return []
        if t == "delay":
            self._later(p["seconds"], block, "then", s)
            return []
        if t == "together":
            if s.seat is None:
                return []
            seen = self._together.get(block.id)
            if seen is None:
                seen = self._together[block.id] = {}
                self._later(p["seconds"], block, "?", Signal())
            seen[s.seat] = now
            active = host.active_seats()
            need = max(1, math.ceil(p["share"] * len(active) - 1e-9))
            if sum(1 for seat in active if seat in seen) >= need:
                del self._together[block.id]
                self._timers = [x for x in self._timers if not (x[2] == block.id and x[3] == "?")]
                heapq.heapify(self._timers)
                return [("then", Signal(value=float(len(seen))))]
            return []
        if t == "score":
            host.add_score(p["to"], s, p["points"])
        elif t == "react":
            host.react(p["to"], s, block)
        elif t == "set_state":
            host.change_state(p["key"], p["value"], p["add"])
        elif t == "send":
            host.send_event(p["event"], p["to"], s)
        elif t == "prompt":
            host.open_prompt(block, s)
            return []
        elif t == "film":
            host.film(p["action"], p["programme"])
        elif t == "phase":
            host.change_phase(p["action"])
        return [("then", s)]
