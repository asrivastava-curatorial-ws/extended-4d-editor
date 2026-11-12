"""``fourdvr fx fixtures``, ``fourdvr fx ready`` and the search for ESP32 boards (``fourdvr fx nodes --find``)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from .patch import STROBE_DEFAULT_MAX_HZ, PatchError, patch_from_dict

DEFAULT_PLATFORMS = "PLATFORM-01"
PASSWORD_ENV = "FOURDVR_SERVER_PASSWORD"


def _ids(text: str) -> list[str]:
    ids = [p.strip() for p in text.split(",") if p.strip()]
    if not ids:
        raise ValueError("give at least one platform id, e.g. --platforms PLATFORM-01,PLATFORM-02")
    return ids


# ---------------------------------------------------------------------------------------------- fixtures
def cmd_search(args: argparse.Namespace) -> int:
    from .fixtures import library

    lib = library()
    text = " ".join(args.words)
    if not text and not args.category:
        print(f"{len(lib)} fixtures. Categories (fourdvr fx fixtures search --category NAME):")
        for name, count in lib.categories().items():
            print(f"  {count:4d}  {name}")
        print("Search by words of the maker or the name: fourdvr fx fixtures search hazer cameo")
        return 0
    found = lib.search(text, args.category)
    for e in found:
        print(f"{e.key:44s} {e.title[:40]:40s} {', '.join(e.categories)[:26]:26s} modes: {', '.join(e.modes[:8])}"
              + (" …" if len(e.modes) > 8 else ""))
    print(f"{len(found)} fixture(s)" + ("" if found else " - try fewer words, or the category alone"))
    return 0 if found else 1


def _plan(args: argparse.Namespace) -> Any:
    from .fixtures import library, plan

    model = library().get(args.fixture)
    mode = model.mode(args.mode or "")
    return model, mode, plan(model, mode, args.effect, args.output_number, args.max_rate) if args.effect else None


def cmd_show(args: argparse.Namespace) -> int:
    from .fixtures import describe

    model, mode, result = _plan(args)
    print(f"{model.title}   [{model.key}]   {', '.join(model.categories)}   from: {model.source}")
    if model.comment:
        print(f"  {model.comment[:300]}")
    print("modes: " + ", ".join(f"{m.short} ({m.footprint} ch{'' if not m.unsupported else ' - cannot be patched: ' + m.unsupported})"
                                for m in model.modes))
    print(f"mode {mode.short}" + (f" as a {args.effect} device:" if result is not None else
                                  " (add --effect wind|fog|strobe|… to see what each channel would do):"))
    print("\n".join(describe(model, mode, result)))
    if result is not None:
        for note in result.notes:
            print(f"note: {note}")
    return 0


def _free_address(patch: Any, ids: list[str], universe: int, footprint: int) -> int:
    used = {ch for inst in patch.instances(ids) if inst.universe == universe for ch in inst.channels()}
    for start in range(1, 514 - footprint):
        if not any(ch in used for ch in range(start, start + footprint)):
            return start
    raise PatchError(f"universe {universe} has no {footprint} free channels in a row")


def cmd_entry(args: argparse.Namespace) -> int:
    from .patchfile import fixture_toml

    if not args.effect:
        raise ValueError("say what the fixture is used for: --effect wind | fog | strobe | heat | water | …")
    _model, _mode, result = _plan(args)
    footprint = max(c["offset"] for c in result.channels) + 1
    fixture_id, address = args.id, args.address
    text = ""
    ids = _ids(args.platforms)
    if args.patch:
        target = Path(args.patch).expanduser()
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        patch = patch_from_dict(tomllib.loads(text), str(target))
        taken = {f.id for f in patch.fixtures}
        if not fixture_id:
            fixture_id = next(f"{args.effect}{n}" for n in range(1, 1000) if f"{args.effect}{n}" not in taken)
        if not address:
            address = _free_address(patch, ids, args.universe, footprint) if args.scope == "theatre" else 1
    entry = result.entry(fixture_id or f"{args.effect}1", args.universe, address or 1, args.scope)
    block = fixture_toml(entry)
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    if not args.patch:
        sys.stdout.write(block)
        return 0
    combined = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    patch = patch_from_dict(tomllib.loads(combined), args.patch)
    problems = patch.problems(ids)
    if problems:
        raise PatchError("not added - " + "; ".join(problems))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(combined, encoding="utf-8")
    os.replace(tmp, target)
    last = address + footprint - 1 if address else footprint
    print(f"added {entry['id']} ({entry['label']}) to {target}: universe {args.universe}, "
          f"channel{'s' if footprint > 1 else ''} {address}" + (f"–{last}" if footprint > 1 else "")
          + f"; the effect drives '{result.driven}'")
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    from .fixtures import FixtureError, Library, fixtures_dir, forget_library, parse_fixture

    for name in args.add or []:
        source = Path(name).expanduser()
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{source}: {exc}") from exc
        model = parse_fixture(data, f"user/{source.stem}", "user", str(source))
        usable = [m.short for m in model.modes if not m.unsupported]
        if not usable:
            raise FixtureError(f"{source}: none of its modes can be patched ({model.modes[0].unsupported})")
        fixtures_dir().mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, fixtures_dir() / source.name)
        print(f"added user/{source.stem}: {model.name}, modes {', '.join(usable)}")
    forget_library()
    info = Library().info()
    snap = info["snapshot"] or {}
    print(f"{info['fixtures']} fixtures: {info['from_the_library']} from the Open Fixture Library"
          + (f" (as of {snap.get('date')}, {str(snap.get('commit'))[:10]})" if snap else " (snapshot missing)")
          + f", {info['own']} of your own")
    print(f"your own fixture files: {fixtures_dir()}" + ("" if fixtures_dir().is_dir() else "  (not there yet)")
          + "  - and every folder named in FOURDVR_FIXTURES")
    print("a fixture that is missing: describe it with the editor at https://open-fixture-library.org/fixture-editor, "
          "download it as \"Open Fixture Library JSON\", then: fourdvr fx fixtures library --add FILE.json")
    for problem in info["problems"]:
        print(f"note: {problem}")
    return 0


# ---------------------------------------------------------------------------------------------- boards, ready
def cmd_find(args: argparse.Namespace) -> int:
    from .bridge import DEFAULT_PORT
    from .ready import broadcast_addresses, find_nodes

    port = args.port or DEFAULT_PORT
    targets = list(args.host) or broadcast_addresses()
    found = find_nodes(port, max(args.seconds, 0.5), targets)
    print(f"ESP32 boards that answer on UDP port {port} (asked: {', '.join(targets)}):")
    for address in sorted(found, key=lambda a: [int(x) if x.isdigit() else 0 for x in a.split(".")]):
        probe = found[address]
        info = probe.info
        assert info is not None
        stop = "  EMERGENCY STOP IN" if info.inhibited else ""
        print(f"  {address:15s} node {probe.node_id:<3d} has {probe.has()}; firmware {info.firmware >> 8}.{info.firmware & 0xFF}; "
              f"answers in {probe.median_ms:.1f} ms{stop}")
    if not found:
        print("  none - boards in another network segment are not reached this way: fourdvr fx nodes ADDRESS")
    return 0 if found else 1


def cmd_ready(args: argparse.Namespace) -> int:
    from .ready import venue_ready

    password = ""
    if args.user:
        if args.password_file:
            password = Path(args.password_file).expanduser().read_text(encoding="utf-8").strip()
        else:
            password = os.environ.get(PASSWORD_ENV, "")
        if not password:
            raise ValueError(f"--user needs the password: --password-file FILE, or the environment variable {PASSWORD_ENV}")
    quiet = args.json or args.quiet
    report = venue_ready(
        args.patch, _ids(args.platforms), server_url=args.server or "", user=args.user or "", password=password,
        insecure=args.insecure, programme=args.programme, signal_loss=not args.no_signal_loss,
        progress=None if quiet else lambda text: print(f"… {text}", file=sys.stderr, flush=True),
    )
    text = json.dumps(report.to_dict(), indent=2) if args.json else "\n".join(report.lines())
    print(text)
    if args.output:
        Path(args.output).expanduser().write_text(text + "\n", encoding="utf-8")
    return 0 if report.ready and not (args.strict and report.count("warn")) else 1


def add_commands(sub: Any) -> None:
    s = sub.add_parser("fixtures", help="the fixture library: real DMX devices and their channels, as venue patch entries")
    fx = s.add_subparsers(dest="fixtures_cmd", required=True)

    def planned(p: argparse.ArgumentParser) -> None:
        p.add_argument("fixture", help='its name in the library, e.g. "cameo/instant-air-1000-pro" (see: fixtures search)')
        p.add_argument("--mode", help="the DMX mode the device is set to (default: its first)")
        p.add_argument("--effect", help="what it is used for: wind, fog, strobe, heat, water, rain, air, bubbles, snow, scent, …")
        p.add_argument("--output", dest="output_number", type=int, default=1,
                       help="a dimmer or relay pack: which of its outputs (default 1)")
        p.add_argument("--max-rate", type=float, default=STROBE_DEFAULT_MAX_HZ,
                       help="strobe: the venue's flash-rate limit in flashes per second (default %(default)g)")

    p = fx.add_parser("search", help="find fixtures by words of their maker and name, or list a category")
    p.add_argument("words", nargs="*")
    p.add_argument("--category", default="", help="Smoke, Hazer, Fan, Strobe, Dimmer, Color Changer, …")
    p.set_defaults(func=cmd_search)

    p = fx.add_parser("show", help="the modes and channels of a fixture - with --effect: what each channel would do")
    planned(p)
    p.set_defaults(func=cmd_show)

    p = fx.add_parser("entry", help="a fixture as a [[fixtures]] entry of a venue patch: printed, or added to a patch file")
    planned(p)
    p.add_argument("--id", help="the fixture's id in the patch (default: the effect and a number)")
    p.add_argument("--universe", type=int, default=1)
    p.add_argument("--address", type=int, default=0, help="its DMX start address (default with --patch: the first free one)")
    p.add_argument("--scope", choices=("theatre", "platform"), default="theatre",
                   help="platform: one such device on every platform (set address_step or addresses in the patch)")
    p.add_argument("--patch", help="add the entry to this patch file (it is checked first; nothing is written if it would not load)")
    p.add_argument("--platforms", default=DEFAULT_PLATFORMS, help="platform ids for that check (default %(default)s)")
    p.set_defaults(func=cmd_entry)

    p = fx.add_parser("library", help="what the library holds and where your own fixture files go; --add FILE.json adds one")
    p.add_argument("--add", action="append", metavar="FILE.json", help="a fixture file in the Open Fixture Library format")
    p.set_defaults(func=cmd_library)

    s = sub.add_parser("ready", help="is the venue ready? patch, DMX output, ESP32 boards (answers, delay, lost packets, "
                                     "signal loss), sound, Control Server devices, a programme")
    s.add_argument("--patch", required=True, help="venue patch (TOML)")
    s.add_argument("--platforms", default=DEFAULT_PLATFORMS, help="platform ids, comma separated (default %(default)s)")
    s.add_argument("--server", help="address of the Control Server, e.g. http://192.168.10.2:8080")
    s.add_argument("--user", help="a Control Server account (a viewer account will do): lists headsets and platform controllers")
    s.add_argument("--password-file", help=f"file with that account's password (or the environment variable {PASSWORD_ENV})")
    s.add_argument("--insecure", action="store_true", help="https: accept the server's certificate without checking it")
    s.add_argument("--programme", help="a .motion or .fx.json file: what this venue does with its effects")
    s.add_argument("--no-signal-loss", action="store_true", help="leave out the signal-loss test of the ESP32 boards")
    s.add_argument("--strict", action="store_true", help="exit with 1 also when there are only warnings")
    s.add_argument("--json", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true", help="no progress lines")
    s.add_argument("-o", "--output", help="also write the report to this file")
    s.set_defaults(func=cmd_ready)
