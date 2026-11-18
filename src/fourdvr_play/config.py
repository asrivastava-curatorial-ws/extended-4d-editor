"""Configuration of an interactive show (``play.toml``).

It is a live configuration (``fourdvr live``: platforms, venue patch, source, cueing, safety) with one
more section, ``[play]``: the seats, the session's settings and the logic blocks. The platforms and
the patch come from the show service's own file, as in live mode, so nothing is kept in step by hand.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fourdvr_fx.patchfile import _key, _value
from fourdvr_live.config import LiveConfig, LiveConfigError, parse_live_config

from . import DEFAULT_PORT
from .logic import ID_PATTERN, NAME_PATTERN, Graph, Limits, LogicError

PLAY_KEYS = ("title", "port", "bind", "operator_key", "game_key", "countdown_s", "session_minutes", "max_events_per_s",
             "shared_motion", "results", "buttons", "seats", "blocks", "film", "limits")
MOTION = {"full": 1.0, "gentle": 0.5, "off": 0.0}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.~-]{6,64}$")
_HEADER = re.compile(r"^\s*\[{1,2}\s*([^\]]+?)\s*\]{1,2}\s*(#.*)?$")


class PlayConfigError(ValueError):
    pass


@dataclass
class SeatConfig:
    id: str
    platform: str = ""             # the platform this seat is on ("" : a player without a platform)
    name: str = ""
    team: str = ""
    motion: str = "full"           # full, gentle, off
    blocked: tuple[str, ...] = ()  # effect types this seat does not get (e.g. water, strobe)


@dataclass
class FilmConfig:
    server: str
    user: str = ""
    password: str = ""
    insecure: bool = False
    poll_s: float = 0.25


@dataclass
class PlayConfig:
    live: LiveConfig = field(default_factory=LiveConfig)
    title: str = "Interactive show"
    port: int = DEFAULT_PORT
    bind: str = "0.0.0.0"
    operator_key: str = ""         # empty: a key is made at every start and shown in the terminal
    game_key: str = ""             # empty: any program on the network may be the game
    countdown_s: float = 5.0
    session_minutes: float = 0.0   # 0: the session ends when the operator or the game ends it
    max_events_per_s: float = 20.0
    shared_motion: bool = True     # every seat also gets the motion and effects of [source]
    results_dir: Path | None = None
    buttons: tuple[str, ...] = ("a", "b", "c", "d")        # the seat boards' inputs 1.., as button names
    seats: list[SeatConfig] = field(default_factory=list)
    graph: Graph = field(default_factory=Graph)
    limits: Limits = field(default_factory=Limits)
    film: FilmConfig | None = None
    origin: str = ""
    notes: list[str] = field(default_factory=list)

    def seat(self, seat_id: str) -> SeatConfig | None:
        return next((s for s in self.seats if s.id == seat_id), None)

    def check(self) -> None:
        if not 1024 <= self.port <= 65535:
            raise PlayConfigError("[play] port must be 1024-65535")
        for name, key in (("operator_key", self.operator_key), ("game_key", self.game_key)):
            if key and not KEY_PATTERN.match(key):
                raise PlayConfigError(f"[play] {name} must be 6-64 letters, digits or . _ ~ -")
        if not 0.0 <= self.countdown_s <= 60.0:
            raise PlayConfigError("[play] countdown_s must be 0-60")
        if not 0.0 <= self.session_minutes <= 24 * 60:
            raise PlayConfigError("[play] session_minutes must be 0-1440")
        if not 1.0 <= self.max_events_per_s <= 200.0:
            raise PlayConfigError("[play] max_events_per_s must be 1-200")
        if not 1 <= len(self.buttons) <= 8 or any(not NAME_PATTERN.match(b) for b in self.buttons):
            raise PlayConfigError("[play] buttons must be 1-8 button names")
        self.limits.check()
        platforms = {p.platform_id for p in self.live.platforms}
        seen: set[str] = set()
        used: set[str] = set()
        if len(self.seats) > 64:
            raise PlayConfigError("more than 64 seats")
        for seat in self.seats:
            if not ID_PATTERN.match(seat.id):
                raise PlayConfigError(f"seat id {seat.id!r}: letters, digits and . _ - only (at most 40)")
            if seat.id in seen:
                raise PlayConfigError(f"two seats are called {seat.id!r}")
            seen.add(seat.id)
            if seat.platform:
                if seat.platform not in platforms:
                    raise PlayConfigError(f"seat {seat.id}: platform {seat.platform!r} is not among the platforms "
                                          f"({', '.join(sorted(platforms)) or 'none configured'})")
                if seat.platform in used:
                    raise PlayConfigError(f"platform {seat.platform} is given to two seats")
                used.add(seat.platform)
            if seat.motion not in MOTION:
                raise PlayConfigError(f"seat {seat.id}: motion must be one of {', '.join(MOTION)}")
            if seat.team and not NAME_PATTERN.match(seat.team):
                raise PlayConfigError(f"seat {seat.id}: team {seat.team!r}: letters, digits and . _ : - only")
        teams = {s.team for s in self.seats if s.team}
        effects = set()
        if self.live.patch is not None:
            effects = {f.effect for f in self.live.patch.fixtures}
            if self.live.patch.audio.enabled:
                effects.add("vibration")
        for block in self.graph.blocks.values():
            p = block.params
            if block.type == "filter":
                if p["seat"] and p["seat"] not in seen:
                    self.notes.append(f"block {block.id}: there is no seat {p['seat']!r}")
                if p["team"] and p["team"] not in teams:
                    self.notes.append(f"block {block.id}: there is no team {p['team']!r}")
            if block.type == "react" and self.live.patch is not None:
                missing = sorted(set(p["effects"]) - effects)
                if missing:
                    self.notes.append(f"block {block.id}: the venue patch has no fixture for {', '.join(missing)}")
            needs_film = block.type == "film" or (block.type == "on_time" and p["clock"] == "film")
            if needs_film and self.film is None:
                raise PlayConfigError(f"block {block.id} follows or steers a film: add [play.film] with the Control "
                                      "Server's address")
            if block.type == "film" and p["action"] == "play" and p["programme"] < 1:
                raise PlayConfigError(f"block {block.id}: 'play' needs programme = the programme's number")
        self.notes.extend(self.graph.notes())

    def describe(self) -> list[str]:
        live = self.live
        source = str(live.source.get("type", "none"))
        lines = [f"show        {self.title}", f"hub         port {self.port or 'chosen at the start'} (pages, WebSocket; UDP for seat telemetry)"]
        if source == "none":
            lines.append("source      none: only what the seats and the logic do")
        else:
            lines.append(f"source      {source}" + ("  (shared by every seat)" if self.shared_motion else "  (effects of the room only)"))
        if live.platforms:
            lines += [f"platform    {p.platform_id}  {p.driver}  {p.address or '-'}" for p in live.platforms]
        else:
            lines.append("platform    none (effects, scores and prompts only)")
        if live.patch is not None:
            lines.append(f"effects     {live.patch.source or 'patch'}: output {live.patch.output.protocol}, "
                         f"{len(live.patch.fixtures)} fixture type(s)")
        else:
            lines.append("effects     no venue patch")
        for seat in self.seats:
            extra = [f"platform {seat.platform}" if seat.platform else "no platform", f"team {seat.team}" if seat.team else "",
                     f"motion {seat.motion}" if seat.motion != "full" else "", f"no {', '.join(seat.blocked)}" if seat.blocked else ""]
            lines.append(f"seat        {seat.id}  " + ", ".join(x for x in extra if x))
        kinds: dict[str, int] = {}
        for block in self.graph.blocks.values():
            kinds[block.kind.group] = kinds.get(block.kind.group, 0) + 1
        lines.append(f"logic       {len(self.graph.blocks)} block(s)" + (
            f": {kinds.get('when', 0)} when, {kinds.get('if', 0)} if, {kinds.get('do', 0)} do" if kinds else ""))
        if self.film is not None:
            lines.append(f"film        follows the Control Server at {self.film.server}")
        return lines


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise PlayConfigError(f"[play.{name}] must be a table")
    return value


def _seat(entry: Any) -> SeatConfig:
    if not isinstance(entry, dict) or "id" not in entry:
        raise PlayConfigError('each [[play.seats]] entry needs id = "name"')
    unknown = sorted(set(entry) - {"id", "platform", "name", "team", "motion", "blocked"})
    if unknown:
        raise PlayConfigError(f"[[play.seats]] {entry['id']}: unknown key(s) {', '.join(unknown)}")
    blocked = entry.get("blocked", [])
    if not isinstance(blocked, list) or any(not isinstance(b, str) for b in blocked):
        raise PlayConfigError(f"[[play.seats]] {entry['id']}: blocked must be a list of effect names")
    return SeatConfig(str(entry["id"]), str(entry.get("platform", "")), str(entry.get("name", ""))[:40],
                      str(entry.get("team", "")), str(entry.get("motion", "full")), tuple(blocked))


def _film(table: dict[str, Any], base: Path) -> FilmConfig:
    unknown = sorted(set(table) - {"server", "user", "password_file", "insecure", "poll_s"})
    if unknown:
        raise PlayConfigError(f"[play.film]: unknown key(s) {', '.join(unknown)}")
    server = str(table.get("server", "")).strip()
    if not server.lower().startswith(("http://", "https://")):
        raise PlayConfigError('[play.film] needs server = "http://ADDRESS:8080" (the Control Server)')
    password = os.environ.get("FOURDVR_SERVER_PASSWORD", "")
    if table.get("password_file"):
        path = (base / str(table["password_file"])).expanduser()
        try:
            password = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PlayConfigError(f"[play.film] password_file: {exc}") from exc
    poll = float(table.get("poll_s", 0.25))
    if not 0.05 <= poll <= 2.0:
        raise PlayConfigError("[play.film] poll_s must be 0.05-2")
    return FilmConfig(server.rstrip("/"), str(table.get("user", "")), password, bool(table.get("insecure", False)), poll)


def parse_play_config(data: dict[str, Any], base: Path, origin: str = "") -> PlayConfig:
    data = dict(data)
    play = data.pop("play", {})
    if not isinstance(play, dict):
        raise PlayConfigError("[play] must be a table")
    unknown = sorted(set(play) - set(PLAY_KEYS))
    if unknown:
        raise PlayConfigError(f"[play]: unknown key(s) {', '.join(unknown)}")
    idle = "source" not in data
    try:
        live = parse_live_config(data, base, origin)
    except LiveConfigError as exc:
        raise PlayConfigError(str(exc)) from exc
    if idle:
        live.source = {"type": "none"}
    cfg = PlayConfig(live=live, origin=origin)
    try:
        cfg.title = str(play.get("title", cfg.title))[:80]
        cfg.port = int(play.get("port", cfg.port))
        cfg.bind = str(play.get("bind", cfg.bind))
        cfg.operator_key = str(play.get("operator_key", ""))
        cfg.game_key = str(play.get("game_key", ""))
        cfg.countdown_s = float(play.get("countdown_s", cfg.countdown_s))
        cfg.session_minutes = float(play.get("session_minutes", cfg.session_minutes))
        cfg.max_events_per_s = float(play.get("max_events_per_s", cfg.max_events_per_s))
        cfg.shared_motion = bool(play.get("shared_motion", True))
        if play.get("results"):
            cfg.results_dir = (base / str(play["results"])).expanduser().resolve()
        buttons = play.get("buttons", list(cfg.buttons))
        if not isinstance(buttons, list):
            raise PlayConfigError("[play] buttons must be a list of names")
        cfg.buttons = tuple(str(b) for b in buttons)
        seats = play.get("seats")
        if seats is None:
            cfg.seats = [SeatConfig(p.platform_id, p.platform_id) for p in live.platforms]
            if cfg.seats:
                cfg.notes.append("no [[play.seats]]: one seat per platform, named like the platform")
        elif not isinstance(seats, list):
            raise PlayConfigError("[[play.seats]] must be a list of tables")
        else:
            cfg.seats = [_seat(entry) for entry in seats]
        limits = _table(play, "limits")
        bad = sorted(set(limits) - set(Limits.__dataclass_fields__))
        if bad:
            raise PlayConfigError(f"[play.limits]: unknown key(s) {', '.join(bad)}")
        cfg.limits = Limits(**{k: float(v) for k, v in limits.items()})
        if "film" in play:
            cfg.film = _film(_table(play, "film"), base)
        cfg.graph = Graph.from_list(play.get("blocks", []))
        cfg.check()
    except LogicError as exc:
        raise PlayConfigError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PlayConfigError):
            raise
        raise PlayConfigError(str(exc)) from exc
    return cfg


def load_play_config(path: str | Path) -> PlayConfig:
    p = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PlayConfigError(f"{p}: {exc}") from exc
    try:
        return parse_play_config(data, p.parent, str(p))
    except PlayConfigError as exc:
        raise PlayConfigError(f"{p}: {exc}") from exc


# =========================================================================== writing the blocks back
def blocks_toml(graph: Graph, table: str = "play.blocks") -> str:
    """The blocks as ``[[play.blocks]]`` tables, the way a person would write them."""
    parts: list[str] = []
    for entry in graph.to_list():
        lines = [f"[[{table}]]"]
        lines += [f"{_key(k)} = {_value(v)}" for k, v in entry.items()]
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + ("\n" if parts else "")


def replace_blocks(text: str, graph: Graph, table: str = "play.blocks") -> str:
    """The text of a play.toml with its blocks replaced by `graph`'s; every other line stays as it is
    (comments inside the old blocks go with them). Raises PlayConfigError if the result would not hold
    exactly the same settings outside the blocks."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            name = re.sub(r"\s+", "", m.group(1))
            inside = name == table or name.startswith(table + ".")
        if not inside:
            kept.append(line)
    body = "\n".join(kept).rstrip("\n")
    new = blocks_toml(graph, table)
    result = body + ("\n\n" + new if new else "\n")
    try:
        before, after = tomllib.loads(text), tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise PlayConfigError(f"the file could not be rewritten safely: {exc}") from exc
    outer, _, leaf = table.rpartition(".")

    def without(data: dict[str, Any]) -> dict[str, Any]:
        copy = dict(data)
        if outer and isinstance(copy.get(outer), dict):
            copy[outer] = {k: v for k, v in copy[outer].items() if k != leaf}
            if not copy[outer]:
                del copy[outer]
        else:
            copy.pop(leaf, None)
        return copy

    def blocks_of(data: dict[str, Any]) -> Any:
        found: Any = data
        for part in table.split("."):
            found = found.get(part, []) if isinstance(found, dict) else []
        return found

    if without(before) != without(after) or blocks_of(after) != blocks_of(tomllib.loads(new) if new else {}):
        raise PlayConfigError("the file could not be rewritten safely: settings outside the blocks would change "
                              "(write the blocks at the end of the file, each as [[play.blocks]])")
    return result


def save_blocks(path: str | Path, graph: Graph) -> None:
    """Replace the blocks of a play.toml in one step (a crash leaves the old file)."""
    import tempfile

    graph.check()
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8") if p.exists() else "[play]\n"
    except OSError as exc:
        raise PlayConfigError(f"{p}: {exc}") from exc
    result = replace_blocks(text, graph)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(result)
        os.replace(tmp, p)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


BENCH_PROFILE = "bench-platform.toml"


def bench_example() -> str:
    """The example for one PC: two platforms from a profile that lies next to the file (the command writes
    it there), to be run with --simulate. No controller.toml, no venue patch."""
    head = EXAMPLE[EXAMPLE.index("[live]\ncontroller = "):]
    head = head[:head.index("\n\n") + 1]
    bench = ("# A bench test on one PC: two platforms from the profile next to this file, simulated with\n"
             "#   fourdvr play run -c play.toml --simulate\n"
             "# In the venue, take the platforms and the effects patch from the show service instead:\n"
             "#   [live]\n"
             '#   controller = "controller.toml"        (and remove the [[platforms]] entries below)\n\n'
             + "".join(f'[[platforms]]\nconfig = "{BENCH_PROFILE}"\nid = "PLATFORM-{k:02d}"\nlink_platform_id = {k}\n\n' for k in (1, 2)))
    return EXAMPLE.replace(head, bench.rstrip("\n") + "\n")


EXAMPLE = '''# Extended 4D Editor - an interactive show: every seat its own motion and effects, events, scores, prompts.
#   fourdvr play check -c play.toml          what this file says, and whether it can run
#   fourdvr play run -c play.toml            the session (the operator page's address is shown)
#   fourdvr play bots -c play.toml           simulated players, to try it without a game
# docs/PLAY.md explains every setting; the Editor's "Interaction" window draws the blocks.

[live]
controller = "controller.toml"   # platforms, their addresses and limits, and the venue's effects patch:
                                 # the same file the show service uses. Leave out for a bench test.

# [source]                       # motion and effects every seat shares (a ride everybody is on):
# type = "open"                  # any source of 'fourdvr live sources'. Leave out: only what the seats and
# port = 47010                   # the blocks below do.

[cueing]
gain = 0.7                       # overall strength, 0..2 (start low)

[play]
title = "Asteroid run"
# port = 47020                   # pages and WebSocket (TCP), seat telemetry (UDP)
# operator_key = "choose-a-key"  # leave out: a new key at every start, shown in the terminal
# game_key = "choose-a-key"      # leave out: any program on the network may be the game
countdown_s = 5
session_minutes = 0              # 0: until the operator or the game ends it
# shared_motion = false          # seats do not get the motion of [source], only its room effects
# results = "results"            # a result file per session in this folder

# [play.film]                    # prompts and reactions at times of a film the Control Server plays:
# server = "http://192.168.10.2:8080"
# user = "operator"              # an operator account, if blocks pause the film or start a programme;
# password_file = "server-password.txt"   # a viewer account is enough to follow it

# [play.limits]                  # how strong a block may ask a seat to react (the platform's limits stay)
# max_accel = 4.0                # m/s²
# max_seconds = 3.0

[[play.seats]]
id = "A1"
platform = "PLATFORM-01"
team = "red"

[[play.seats]]
id = "A2"
platform = "PLATFORM-02"
team = "blue"
# motion = "gentle"              # full, gentle, off
# blocked = ["water", "strobe"]  # effects this seat does not get

# ---- the logic: blocks, and what follows what ------------------------------------------------------------
[[play.blocks]]                  # when the game says a seat was hit ...
id = "hit"
type = "on_event"
event = "hit"
then = ["hit-jolt", "hit-points"]

[[play.blocks]]                  # ... that seat is pushed back and shaken
id = "hit-jolt"
type = "react"
accel = [-2.5, 0.0, 1.0]
seconds = 0.25
effects = { vibration = 1.0, air = 1.0 }
effect_seconds = 0.4

[[play.blocks]]                  # ... and loses points
id = "hit-points"
type = "score"
points = -5.0

[[play.blocks]]                  # button "a" on a seat: fire, at most twice a second
id = "fire"
type = "on_input"
button = "a"
then = ["fire-rate"]

[[play.blocks]]
id = "fire-rate"
type = "cooldown"
seconds = 0.5
then = ["fire-send"]

[[play.blocks]]                  # the game hears "fire" for that seat
id = "fire-send"
type = "send"
event = "fire"

[[play.blocks]]                  # 20 seconds in: everybody is asked, the majority decides
id = "ask"
type = "on_time"
at = 20.0
then = ["route"]

[[play.blocks]]
id = "route"
type = "prompt"
text = "Which way?"
choices = ["Through the canyon", "Over the ridge"]
seconds = 8.0
mode = "vote"
then = { a = ["canyon"], b = ["ridge"], none = ["ridge"] }

[[play.blocks]]
id = "canyon"
type = "send"
event = "route.canyon"
to = "all"

[[play.blocks]]
id = "ridge"
type = "send"
event = "route.ridge"
to = "all"
'''
