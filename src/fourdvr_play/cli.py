"""``fourdvr play`` - interactive shows: every seat its own motion and effects, events, scores, prompts.

    fourdvr play example-config -o play.toml     a commented configuration to start from (--bench: for one PC)
    fourdvr play blocks                          the logic blocks and their settings
    fourdvr play check -c play.toml              what a configuration says, and whether it can run
    fourdvr play run -c play.toml                the session (s start, p pause, x end, Space hold, q quit)
    fourdvr play bots -c play.toml --game        simulated players (and a simulated game) to try it
    fourdvr play send event hit --seat A1        one message to a running session (scripts, tests)
    fourdvr play status --key KEY                what a running session is doing
    fourdvr play addon -o MY_GODOT_PROJECT       the Godot 4 add-on, written into a Godot project
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any

from fourdvr_studio import __version__, reader_gone

from . import DEFAULT_PORT
from .config import BENCH_PROFILE, EXAMPLE, PlayConfig, PlayConfigError, bench_example, load_play_config
from .logic import GROUPS, TYPES, chains

KEYS_HELP = "s start · p pause / resume · x end · r reset · Space hold / release · e go on after a fault · + - strength · q quit"


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def build_config(args: argparse.Namespace) -> PlayConfig:
    cfg = load_play_config(args.config)
    if getattr(args, "port", None) is not None:
        cfg.port = int(args.port)                # 0: any free port (the addresses printed say which)
    if getattr(args, "bind", None):
        cfg.bind = str(args.bind)
    if getattr(args, "simulate", False):
        cfg.live.platforms = [replace(p, driver="simulation") for p in cfg.live.platforms]
    if getattr(args, "hold", False):
        cfg.live.hold = True
    if getattr(args, "no_sound", False):
        cfg.live.sound = False
    return cfg


# =========================================================================== reading a configuration
def cmd_example(args: argparse.Namespace) -> int:
    text = bench_example() if args.bench else EXAMPLE
    if args.output:
        path = Path(args.output).expanduser()
        if path.exists() and not args.force:
            raise PlayConfigError(f"{path} exists already (--force overwrites it)")
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
        if args.bench:                               # the platform profile the file names, next to it
            profile = path.parent / BENCH_PROFILE
            if not profile.exists():
                packaged = resources.files("fourdvr_extractor") / "profiles" / "example-electric-6dof.toml"
                profile.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"wrote {profile}")
            print(f"try it:  fourdvr play run -c {path} --simulate      and, in a second terminal:\n"
                  f"         fourdvr play bots -c {path} --game      (then press s in the first one)")
    elif args.bench:
        raise PlayConfigError("--bench writes two files: name the first with -o play.toml")
    else:
        sys.stdout.write(text)
    return 0


def cmd_blocks(args: argparse.Namespace) -> int:
    for group, title in GROUPS.items():
        print(f"{title.upper()}")
        for kind in TYPES.values():
            if kind.group != group:
                continue
            outputs = "" if kind.outputs == ("then",) else f"   outputs: {', '.join(kind.outputs)}"
            print(f"  {kind.name:<10} {kind.title} - {kind.summary}{outputs}")
            if args.settings:
                for p in kind.params:
                    default = "" if p.default in ("", None, {}) else f" = {json.dumps(p.default)}"
                    options = f"  ({' | '.join(o or '\"\"' for o in p.options)})" if p.options else ""
                    print(f"      {p.name}{default}{options}   {p.label}{' ' + p.unit if p.unit else ''}")
        print()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    print("\n".join(cfg.describe()))
    if cfg.graph.blocks:
        print("\nthe logic:")
        print("\n".join("  " + line for line in chains(cfg.graph)))
    for note in cfg.live.notes:
        print(f"note: {note}")
    for note in cfg.notes:
        print(f"note: {note}")
    if not cfg.seats:
        print("note: no seats - list them as [[play.seats]] (or give platforms: each becomes a seat)")
    print("\nthis configuration can run" if not (args.strict and cfg.notes) else "\nthere are notes (--strict)")
    return 1 if args.strict and cfg.notes else 0


# =========================================================================== the session
def status_line(st: dict[str, Any]) -> str:
    session = st["session"]
    phase = session["phase"]
    parts = [f"{phase['phase']:<9} {_clock(phase['time'])}" + ("  HELD" if phase.get("held") else "")]
    seats = session["seats"]
    parts.append(f"seats {sum(1 for s in seats if s['clients'])}/{sum(1 for s in seats if s['enabled'])} connected")
    top = sorted(seats, key=lambda s: -s["score"])[:4]
    if any(s["score"] for s in top):
        parts.append(" ".join(f"{s['id']} {s['score']:g}" for s in top))
    engine = st.get("engine")
    if engine:
        play = engine["play"]
        parts.append(f"reactions {play['reactions']}" + (f" (left out {play['suppressed']})" if play["suppressed"] else ""))
        faults = [p["id"] for p in engine["platforms"] if p["fault"]]
        if faults:
            parts.append("FAULT " + ", ".join(faults))
        if engine["problem"]:
            parts.append(engine["problem"])
    if session["prompt"]:
        parts.append(f"asked: {session['prompt']['answered']}/{session['prompt']['of']} answered")
    film = session.get("film")
    if film:
        parts.append(f"film {'waits for the blocks' if film.get('held') else film['state'].lower() or '-'} {_clock(film['position_s'])}"
                     if film["ok"] else "film: no Control Server")
    if session["logic"]["problem"]:
        parts.append(session["logic"]["problem"])
    return " | ".join(parts)


def cmd_run(args: argparse.Namespace) -> int:
    from fourdvr_live.cli import Keys, _columns
    from fourdvr_live.sources import SourceError
    from fourdvr_stream.server import local_addresses

    from .film import FilmLink
    from .hub import Hub, PlayError
    from .server import PlayServer

    cfg = build_config(args)
    if not cfg.operator_key:
        cfg.operator_key = secrets.token_urlsafe(9)
    print("\n".join(cfg.describe()))
    for note in (*cfg.live.notes, *cfg.notes):
        print(f"note: {note}")
    engine = None
    if not args.no_outputs:
        from .engine import PlayEngine

        engine = PlayEngine(cfg, audio_backend="null" if args.null_audio else "")
    hub = Hub(cfg, engine)
    film = None
    if cfg.film is not None:
        film = FilmLink(cfg.film, hub.film_update, on_note=hub.note)
        hub.film_control = film
    server = PlayServer(cfg, hub, engine)
    out = sys.stdout
    tty = out.isatty()
    started = time.monotonic()
    try:
        server.start()                 # the port first: if it is taken, no platform link has been opened yet
        if engine is not None:
            try:
                engine.start()
            except SourceError as exc:
                raise PlayConfigError(f"the shared source cannot start: {exc}") from exc
            if engine.output_problem():
                print(f"note: {engine.output_problem()}")
        if film is not None:
            film.start()
        hosts = [cfg.bind] if cfg.bind not in ("0.0.0.0", "::", "") else (local_addresses() or ["127.0.0.1"])
        print(f"\noperator    http://{hosts[0]}:{server.port}/operator?key={cfg.operator_key}")
        print(f"seats       http://{hosts[0]}:{server.port}/            (a phone or tablet per seat)")
        print(f"scoreboard  http://{hosts[0]}:{server.port}/scoreboard")
        print(f"game        ws://{hosts[0]}:{server.port}/ws           (the Godot add-on, or any WebSocket client)")
        if args.start:
            if hub.film_mode:
                print("note: --start is left out: the film leads this session")
            else:
                hub.command("start", "operator")
        with Keys() as keys:
            if keys.active:
                print(KEYS_HELP)
            next_status = 0.0
            while True:
                now = time.monotonic()
                if args.seconds and now - started >= args.seconds:
                    break
                key = keys.read()
                try:
                    if key in ("q", "Q", "\x1b"):
                        break
                    if key == " ":
                        hub.hold(not hub.held)
                    elif key in ("s", "S"):
                        hub.command("start")
                    elif key in ("p", "P"):
                        hub.command("resume" if hub.phase == "paused" else "pause")
                    elif key in ("x", "X"):
                        hub.command("end")
                    elif key in ("e", "E"):
                        hub.hold(False)
                    elif key in ("r", "R"):
                        hub.command("reset")
                    elif key in ("+", "=") and engine is not None:
                        engine.set_gain(engine.cueing.s.gain + 0.05)
                    elif key in ("-", "_") and engine is not None:
                        engine.set_gain(engine.cueing.s.gain - 0.05)
                except PlayError as exc:
                    print(("\n" if tty else "") + f"note: {exc.message}")
                if now >= next_status:
                    next_status = now + args.status_interval
                    line = status_line(server.status())
                    if tty:
                        print("\r\x1b[K" + line[: max(40, _columns() - 1)], end="", flush=True)
                    else:
                        print(line, flush=True)
                if server.error:
                    raise PlayConfigError(server.error)
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        if tty:
            print()
        if film is not None:
            film.stop()
        server.stop()
        if engine is not None:
            engine.stop()
    results = hub.last_results or (hub.results() if hub.sessions else None)
    if results:
        print(f"session of {_clock(results['duration_s'])}:")
        for seat in results["seats"]:
            print(f"  {seat['place']}. {seat['id']:<10} {seat['name']:<16} {seat['score']:>8g} points"
                  + (f"   team {seat['team']}" if seat["team"] else ""))
        for team in results["teams"]:
            print(f"  team {team['team']:<12} {team['score']:>8g} points")
    if engine is not None:
        play = engine.status()["play"]
        print(f"reactions: {play['reactions']}" + (f", {play['suppressed']} left out while the session was not running" if play["suppressed"] else ""))
        for rt in engine.runtimes:
            c = rt.pipeline.counters.as_dict()
            print(f"platform {rt.cfg.platform_id}: limiter position {c['position_clamps']}, velocity {c['velocity_clamps']}, "
                  f"acceleration {c['acceleration_clamps']}" + (f"; FAULT: {rt.fault_reason}" if rt.fault_reason else ""))
    return 0


# =========================================================================== tools around a running session
def _ws_url(args: argparse.Namespace) -> str:
    if args.url:
        return str(args.url)
    port = DEFAULT_PORT
    if getattr(args, "config", None):
        port = load_play_config(args.config).port
    return f"ws://127.0.0.1:{port}/ws"


def cmd_bots(args: argparse.Namespace) -> int:
    from .bots import Bots
    from .client import PlayClient

    cfg = load_play_config(args.config)
    if not cfg.seats:
        raise PlayConfigError("the configuration has no seats to play")
    url = _ws_url(args)
    if args.start:
        if not args.key:
            raise PlayConfigError("--start needs the operator key (--key)")
        with PlayClient(url, role="operator", key=args.key) as operator:
            operator.phase("start")
    bots = Bots(cfg, url, game=args.game, seed=args.seed, press_every_s=args.press_every)
    print(f"{len(cfg.seats)} simulated player(s)" + (" and a simulated game" if args.game else "") + f" at {url}"
          + (f" for {args.seconds:g} s" if args.seconds else " (Ctrl+C ends)"))
    bots.run(args.seconds)
    print(", ".join(f"{count} {what}" for what, count in bots.sent.items()))
    for error in bots.errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if bots.errors else 0


def cmd_send(args: argparse.Namespace) -> int:
    from .client import PlayClient

    role = "operator" if args.what in ("phase", "hold") or (args.key and args.operator) else "game"
    with PlayClient(_ws_url(args), role=role, key=args.key or "") as hub:
        if args.what == "event":
            hub.event(args.name, seat=args.seat, value=args.value)
        elif args.what == "input":
            if not args.seat:
                raise PlayConfigError("an input needs --seat")
            hub.input(args.name, seat=args.seat)
        elif args.what == "phase":
            hub.phase(args.name)
        elif args.what == "hold":
            hub.send("hold", on=args.name not in ("off", "release", "0", "false"))
        elif args.what == "state":
            try:
                value: Any = float(args.value_text)
            except (TypeError, ValueError):
                value = args.value_text or ""
            hub.state(args.name, value)
        else:
            hub.send("react", seat=args.seat or "all", **json.loads(args.name))
        answer = hub.recv(timeout=args.wait)
        while answer is not None:
            if answer.get("type") == "error":
                print(f"error: {answer.get('message')}", file=sys.stderr)
                return 1
            answer = hub.recv(timeout=0.05)
    print("sent")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    url = (args.url or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/status?key={args.key}", timeout=4.0) as response:
            status = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PlayConfigError("the operator key was not accepted" if exc.code == 403 else f"{url} answered {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PlayConfigError(f"{url} does not answer ({getattr(exc, 'reason', exc)})") from exc
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    print(status_line(status))
    for seat in status["session"]["seats"]:
        print(f"  {seat['id']:<10} {seat['name']:<16} {'on ' if seat['enabled'] else 'off'} "
              f"{'connected' if seat['clients'] else '-':<10} {'headset' if seat['headset'] else '':<8} {seat['score']:>8g}")
    return 0


def cmd_addon(args: argparse.Namespace) -> int:
    project = Path(args.output).expanduser()
    if not (project / "project.godot").exists() and not args.force:
        raise PlayConfigError(f"{project} is not a Godot project (no project.godot there); --force writes anyway")
    source = resources.files("fourdvr_play").joinpath("godot", "addons", "fourd_theatre")
    target = project / "addons" / "fourd_theatre"
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with resources.as_file(source) as folder:
        for item in sorted(Path(folder).rglob("*")):
            if item.is_file() and "__pycache__" not in item.parts:
                destination = target / item.relative_to(folder)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)
                count += 1
    print(f"wrote {count} files to {target}")
    print("In Godot: Project > Project Settings > Plugins > switch on \"4D Theatre\". It adds the autoload 'FourD'.")
    print(f"Then set the hub's address:  FourD.url = \"ws://ADDRESS:{DEFAULT_PORT}/ws\"   (see addons/fourd_theatre/README.md)")
    return 0


# =========================================================================== command line
def build_parser(prog: str = "fourdvr play") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("example-config", help="print a commented play.toml")
    s.add_argument("-o", "--output", help="write it to this file")
    s.add_argument("--force", action="store_true", help="overwrite the file")
    s.add_argument("--bench", action="store_true", help="a version for one PC: two simulated platforms, nothing else needed")
    s.set_defaults(func=cmd_example)

    s = sub.add_parser("blocks", help="list the logic blocks")
    s.add_argument("--settings", action="store_true", help="with every block's settings")
    s.set_defaults(func=cmd_blocks)

    s = sub.add_parser("check", help="say what a configuration means and whether it can run")
    s.add_argument("-c", "--config", required=True, help="play.toml")
    s.add_argument("--strict", action="store_true", help="exit with 1 if there are notes")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="run the session")
    s.add_argument("-c", "--config", required=True, help="play.toml (see 'fourdvr play example-config')")
    s.add_argument("--port", type=int, help="TCP and UDP port instead of the configuration's (0: any free port)")
    s.add_argument("--bind", help="local address to listen on (default: all)")
    s.add_argument("--simulate", action="store_true", help="simulated platforms instead of the real ones")
    s.add_argument("--no-outputs", action="store_true", help="no platforms, effects or sound: the session and its logic only")
    s.add_argument("--hold", action="store_true", help="start held: nothing moves until released (Space)")
    s.add_argument("--start", action="store_true", help="start the session at once")
    s.add_argument("--seconds", type=float, default=0.0, help="end by itself after this long")
    s.add_argument("--no-sound", action="store_true", help="without a venue patch: no shaker sound on this computer")
    s.add_argument("--null-audio", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--status-interval", type=float, default=0.5, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("bots", help="simulated players (and, with --game, a simulated game)")
    s.add_argument("-c", "--config", required=True, help="the play.toml of the running session")
    s.add_argument("--url", help="the hub's address (default: ws://127.0.0.1:PORT/ws)")
    s.add_argument("--game", action="store_true", help="also play the game's part: events and every seat's telemetry")
    s.add_argument("--start", action="store_true", help="start the session first (needs --key)")
    s.add_argument("--key", help="the operator key (for --start)")
    s.add_argument("--seconds", type=float, default=0.0, help="end after this long")
    s.add_argument("--seed", type=int, help="make the bots do the same every time")
    s.add_argument("--press-every", type=float, default=3.0, help="a bot presses a button about this often (s)")
    s.set_defaults(func=cmd_bots)

    s = sub.add_parser("send", help="send one message to a running session")
    s.add_argument("what", choices=("event", "input", "phase", "hold", "state", "react"))
    s.add_argument("name", help="event name | button | start/pause/resume/end/reset | on/off | key | reaction as JSON")
    s.add_argument("value_text", nargs="?", help="for 'state': the value")
    s.add_argument("--seat", help="the seat it is about")
    s.add_argument("--value", type=float, default=0.0, help="for 'event': a number that goes with it")
    s.add_argument("--url", help="the hub's address (default: ws://127.0.0.1:PORT/ws)")
    s.add_argument("-c", "--config", help="take the port from this play.toml")
    s.add_argument("--key", help="the game key, or the operator key for 'phase' and 'hold'")
    s.add_argument("--operator", action="store_true", help="send as the operator (with --key)")
    s.add_argument("--wait", type=float, default=0.3, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("status", help="what a running session is doing")
    s.add_argument("--url", help=f"the hub's address (default: http://127.0.0.1:{DEFAULT_PORT})")
    s.add_argument("--key", required=True, help="the operator key")
    s.add_argument("--json", action="store_true", help="everything, as JSON")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("addon", help="write the Godot 4 add-on into a Godot project")
    s.add_argument("-o", "--output", required=True, help="the Godot project's folder (where project.godot is)")
    s.add_argument("--force", action="store_true", help="write even if there is no project.godot")
    s.set_defaults(func=cmd_addon)
    return p


def main(argv: list[str] | None = None, prog: str = "fourdvr play") -> int:
    from .client import PlayClientError

    args = build_parser(prog).parse_args(argv)
    try:
        code = int(args.func(args))
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        return reader_gone()
    except (PlayConfigError, PlayClientError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
