"""Effects tools: ``fourdvr fx <command>``.

Files      info, embed, extract, strip        effects inside .motion files and standalone .fx.json
Venue      example-patch, check-patch, check  the DMX patch of a theatre; a programme against a patch
Live DMX   play, fire, blackout, monitor      commissioning without the Control Server
Audio      devices                            sound cards (bass) and serial ports (USB DMX)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

from fourdvr_motion import motionfile

from . import BUILTIN_TYPES, EffectsDocument, effect_type
from .bridge import create_output
from .dmx import DmxOutput, DmxReceiver
from .embed import embed_into_motion, read_effects, write_effects
from .engine import THEATRE, ShowState
from .patch import Patch, PatchError, load_patch
from .preflight import default_patch, preflight
from .service import EffectsService

DEFAULT_PLATFORMS = "PLATFORM-01"


def _platforms(text: str) -> list[str]:
    ids = [p.strip() for p in text.split(",") if p.strip()]
    if not ids:
        raise ValueError("give at least one platform id, e.g. --platforms PLATFORM-01,PLATFORM-02")
    return ids


def _clock(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def _load_doc(path: str) -> EffectsDocument:
    doc = read_effects(path)
    if doc is None:
        raise ValueError(f"{path}: motion-only programme (no effects section)")
    return doc


def _load_patch(args: argparse.Namespace) -> tuple[Patch, list[str]]:
    patch = load_patch(args.patch)
    ids = _platforms(args.platforms)
    if getattr(args, "output", None):
        patch.output.protocol = args.output
    problems = patch.problems(ids)
    if problems:
        raise PatchError(f"{args.patch}: " + "; ".join(problems))
    return patch, ids


# ---------------------------------------------------------------------------------------- files
def describe(doc: EffectsDocument) -> list[str]:
    lines = [f"effects {doc.to_dict()['version']}: {doc.duration_s:.2f} s, {len(doc.tracks)} track(s)"]
    for tr in doc.tracks:
        et = effect_type(tr.type)
        on = sum(c.duration for c in tr.cues)
        peak = max([c.level for c in tr.cues] + [v for _t, v in tr.points], default=0.0)
        what = []
        if tr.cues:
            what.append(f"{len(tr.cues)} cue(s), {on:.1f} s")
        if tr.points:
            what.append(f"{len(tr.points)} curve point(s)")
        where = f" DMX {tr.universe}/{tr.channel}" if tr.type == "dmx" else ""
        params = "".join(f" {k}={v:g}" for k, v in tr.params.items())
        flag = "" if tr.enabled else "  [disabled]"
        builtin = "" if tr.type in BUILTIN_TYPES else " (venue-defined type)"
        lines.append(
            f"  {tr.id:16s} {et.label:20s}{where}{params}  {', '.join(what) or 'empty'}, peak {peak * 100:.0f} %{builtin}{flag}"
        )
    if doc.bass is not None:
        b = doc.bass
        lines.append(
            f"  bass track: {b.duration_s:.2f} s mono @ {b.sample_rate_hz} Hz, low-pass {b.cutoff_hz:g} Hz, "
            f"gain {b.gain_db:+.1f} dB, {len(b.samples) * 2 / 1024:.0f} KiB PCM"
        )
    for problem in doc.problems():
        lines.append(f"  warning: {problem}")
    return lines


def cmd_info(args: argparse.Namespace) -> int:
    doc = read_effects(args.file)
    if doc is None:
        print(f"{args.file}: motion-only programme (no effects section) - it plays on any platform as it is")
        return 0
    if args.json:
        slim = doc.to_dict()
        if "bass" in slim:
            slim["bass"] = {k: v for k, v in slim["bass"].items() if k != "data"}
        print(json.dumps(slim, indent=2))
        return 0
    print(args.file)
    print("\n".join(describe(doc)))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    doc = _load_doc(args.effects)
    mf = motionfile.read_motion(args.motion)
    if abs(doc.duration_s - mf.duration_s) > 0.5 and not args.force:
        raise ValueError(
            f"effects are {doc.duration_s:.2f} s long, the motion file {mf.duration_s:.2f} s (use --force to embed anyway)"
        )
    sha = embed_into_motion(args.motion, doc, args.output)
    print(f"wrote {args.output or args.motion} with effects embedded (sha256 {sha})")
    print("  the file changed: upload it to the Control Server again")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    doc = _load_doc(args.motion)
    out = args.output or str(Path(args.motion).with_suffix(".fx.json"))
    write_effects(out, doc)
    print(f"wrote {out} ({len(doc.tracks)} track(s){', bass track' if doc.bass is not None else ''})")
    return 0


def cmd_strip(args: argparse.Namespace) -> int:
    sha = embed_into_motion(args.motion, None, args.output)
    print(f"wrote {args.output or args.motion} without effects (motion only, sha256 {sha})")
    return 0


# ---------------------------------------------------------------------------------------- venue
def cmd_example_patch(args: argparse.Namespace) -> int:
    name = "patch.esp32.example.toml" if args.esp32 else "patch.example.toml"
    text = resources.files("fourdvr_fx").joinpath(f"examples/{name}").read_text(encoding="utf-8")
    if args.output:
        target = Path(args.output)
        if target.exists() and not args.force:
            raise ValueError(f"{target} exists (use --force to overwrite)")
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    else:
        sys.stdout.write(text)
    return 0


def patch_table(patch: Patch, ids: list[str], hosts: dict[str, str] | None = None) -> list[str]:
    """The venue in words. `hosts`: the ESP32 of each platform as the show service knows it (esp32 output)."""
    out = patch.output
    lines = [f"output {out.protocol}, {out.refresh_hz:g} frames/s"]
    esp32 = out.protocol == "esp32"
    wires = patch.esp32_lines(ids, hosts) if esp32 else {}

    def line_name(inst: Any) -> str:
        if not esp32:
            return f"U{inst.universe:<3d}"
        wire = wires.get(inst.universe)
        owner = "theatre" if inst.platform is None else inst.platform
        return f"{owner} @ {wire[0]}" + (f" line {wire[1] + 1}" if wire[1] else "") if wire else f"{owner} @ (no ESP32)"

    if esp32:
        lines[0] = f"output esp32 (DMX and bass through ESP32 nodes, UDP port {out.esp32.get('port', 47002)}), {out.refresh_hz:g} frames/s"
        nodes = sorted({w[0] for w in wires.values()})
        lines.append("  ESP32 nodes: " + (", ".join(nodes) if nodes else "none known here")
                     + " - each is asked what it has; a missing node, DMX line or audio output is left out, not an error")
    if out.protocol == "sacn":
        s = out.sacn
        to = (["multicast"] if s.get("multicast", True) else []) + [str(x) for x in s.get("unicast", [])]
        lines[0] += f" -> {', '.join(to) or 'nowhere (no multicast, no unicast)'}"
    elif out.protocol == "artnet":
        lines[0] += f" -> {', '.join(str(x) for x in out.artnet.get('targets', ['255.255.255.255']))}"
    elif out.protocol == "enttec-pro":
        lines[0] += f" -> {out.enttec.get('port', '?')} (universe {out.enttec.get('universe', 1)})"
    instances = sorted(patch.instances(ids), key=lambda i: (i.universe, i.address))
    width = max((len(line_name(i)) for i in instances), default=0)
    for inst in instances:
        f = inst.fixture
        chans = inst.channels()
        span = f"{chans[0]}" if len(chans) == 1 else f"{min(chans)}-{max(chans)}"
        roles = "+".join(c.role for c in f.channels)
        extra = []
        if f.lead_s:
            extra.append(f"lead {f.lead_s:g} s")
        if f.match:
            extra.append("only " + ",".join(f"{k}={v:g}" for k, v in f.match.items()))
        if f.effect == "strobe":
            extra.append(f"≤ {f.limits.max_rate_hz:g} flashes/s ({f.flash} flash)")
        lines.append(
            f"  {line_name(inst):{width}s} ch {span:7s} {inst.key:26s} {f.effect:10s} {roles:12s} "
            f"{f.limits.describe()}{'; ' if extra else ''}{'; '.join(extra)}"
        )
    ea = out.esp32_audio
    if esp32 and ea is not None and ea.enabled:
        lines.append(
            f"ESP32 audio: bass on channel(s) {ea.bass_channels}, vibration on {ea.vibration_channels} ({ea.vibration_frequency_hz:g} Hz) "
            "of every node that has an audio output; a platform's node plays that platform's show"
        )
    elif esp32:
        lines.append("ESP32 audio: off ([output.esp32.audio] enabled = false)")
    a = patch.audio
    if a.enabled:
        lines.append(
            f"audio: device '{a.device or 'system default'}', {a.channels} ch @ {a.sample_rate_hz} Hz, bass on {a.bass_channels}, "
            f"vibration on {a.vibration_channels} ({a.vibration_frequency_hz:g} Hz)"
        )
    else:
        lines.append("audio: off (no bass / seat-vibration output from this PC's sound card)")
    for name, cfg in sorted(patch.types.items()):
        if not cfg.enabled or cfg.master < 1.0:
            lines.append(f"type {name}: {'enabled' if cfg.enabled else 'SWITCHED OFF'}, master {cfg.master * 100:.0f} %")
    return lines


def cmd_check_patch(args: argparse.Namespace) -> int:
    patch = load_patch(args.patch)
    ids = _platforms(args.platforms)
    print(f"{args.patch} for {', '.join(ids)}")
    print("\n".join(patch_table(patch, ids)))
    for w in patch.warnings():
        print(f"warning: {w}")
    problems = patch.problems(ids)
    for p in problems:
        print(f"ERROR: {p}")
    print("patch has errors" if problems else "patch OK")
    return 1 if problems else 0


def cmd_check(args: argparse.Namespace) -> int:
    doc = _load_doc(args.file)
    if args.patch:
        patch, ids = _load_patch(args)
        print(f"{args.file} at the venue of {args.patch} ({', '.join(ids)}): one show run of {doc.duration_s:.1f} s")
    else:                                         # no venue yet: the limits a device has when its venue sets none
        patch, ids = default_patch(t.type for t in doc.tracks), _platforms(args.platforms)
        print(f"{args.file} against the default limits (no venue patch given): one show run of {doc.duration_s:.1f} s")
    report = preflight(doc, patch, ids)
    if not args.patch:
        report.unpatched = []                     # without a venue no device is "missing"
    for line in report.lines():
        print("  " + line)
    if report.limited:
        print("some cues are shortened or dropped by the " + ("venue's" if args.patch else "default") + " limits (the show still runs)")
    return 1 if (report.limited or report.unpatched) and args.strict else 0


# ---------------------------------------------------------------------------------------- live DMX
def _levels(service: EffectsService) -> str:
    lv = service.engine.type_levels
    text = " ".join(f"{k}={v * 100:3.0f}%" for k, v in sorted(lv.items())) or "(all off)"
    held = service.engine.limited()
    return text + (f"   held by limits: {', '.join(held)}" if held else "")


def cmd_play(args: argparse.Namespace, output: DmxOutput | None = None) -> int:
    doc = _load_doc(args.file)
    patch, ids = _load_patch(args)
    compiled = doc.compile()
    duration = doc.duration_s
    end = min(args.end if args.end is not None else duration, duration)
    if not 0.0 <= args.start < end:
        raise ValueError("--start must be inside the programme and before --end")
    box: dict[str, float] = {}

    def position(now: float) -> float:
        return args.start + (now - box["t0"])

    def states(now: float) -> dict[str, ShowState]:
        if "t0" not in box:
            return {}
        pos = position(now)
        active = pos >= args.start and (args.loop or pos < end)
        if args.loop:
            pos = args.start + (pos - args.start) % (end - args.start)
        st = ShowState(active, compiled, pos, duration, whole_loop, 1)
        return {THEATRE: st, **{pid: st for pid in ids}}

    whole_loop = bool(args.loop and args.start == 0.0 and end >= duration)   # effects sent early wrap around too
    service = EffectsService(patch, ids, states, output=output, audio_backend=args.audio_backend)
    service.prepare(compiled)
    print(f"playing {args.file} {_clock(args.start)} - {_clock(end)}{' (loop)' if args.loop else ''} on {patch.output.protocol}"
          f" for {', '.join(ids)}; Ctrl+C stops and blacks out")
    for w in patch.warnings():
        print(f"warning: {w}")
    service.start()
    try:
        box["t0"] = time.monotonic() + 0.3
        last = -1.0
        while True:
            now = time.monotonic()
            pos = position(now)
            if not args.loop and pos >= end:
                break
            if not args.quiet and pos - last >= args.every:
                last = pos
                shown = args.start + (pos - args.start) % (end - args.start) if args.loop else max(pos, args.start)
                print(f"  {_clock(shown)}  {_levels(service)}", flush=True)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        service.stop()
    st = service.status()
    out = st["output"]
    print(f"done: {out['frames']} DMX frames sent, {out['errors']} output error(s), {st['errors']} service error(s)"
          + (f" - last: {out['last_error'] or st['last_error']}" if out["errors"] or st["errors"] else ""))
    return 0 if not st["errors"] and not out["errors"] else 1


def cmd_fire(args: argparse.Namespace, output: DmxOutput | None = None) -> int:
    patch, ids = _load_patch(args)
    service = EffectsService(patch, ids, lambda _now: {}, output=output)
    keys = [it.inst.key for it in service.engine.instances]
    if args.fixture not in keys:
        raise ValueError(f"unknown fixture '{args.fixture}'; this patch has: {', '.join(keys)}")
    service.engine.test_enabled = True
    seconds = min(max(args.seconds, 0.05), 30.0)
    print(f"firing {args.fixture} at {args.level * 100:.0f} % for {seconds:g} s on {patch.output.protocol} (its limits still apply)")
    service.start()
    try:
        service.engine.fire(args.fixture, args.level, seconds)
        end = time.monotonic() + seconds + 0.25
        while time.monotonic() < end:
            time.sleep(0.05)
            row = next(f for f in service.engine.status()["fixtures"] if f["key"] == args.fixture)
            if row["limit"]:
                print(f"  held off by its limit: {row['limit']}")
                break
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        service.stop()
    out = service.status()["output"]
    print(f"off; {out['frames']} DMX frames sent, {out['errors']} output error(s)" + (f": {out['last_error']}" if out["errors"] else ""))
    return 0 if not out["errors"] else 1


def cmd_blackout(args: argparse.Namespace, output: DmxOutput | None = None) -> int:
    patch, ids = _load_patch(args)
    service = EffectsService(patch, ids, lambda _now: {}, output=output)
    service.output.open()
    frames = service.engine.blackout()
    for _ in range(5):
        for universe, data in frames.items():
            service.output.send(universe, data)
        time.sleep(0.03)
    service.output.close()
    print(f"blackout sent to universe(s) {', '.join(str(u) for u in sorted(frames))} on {patch.output.protocol}")
    return 0 if not service.output.errors else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    universes = sorted(set(args.universe or [1]))
    rx = DmxReceiver(args.protocol, universes, bind_ip=args.bind, port=args.port, multicast=not args.no_multicast)
    rx.start()
    print(f"listening for {args.protocol} on port {rx.port}, universe(s) {universes}; Ctrl+C stops")
    shown: dict[int, bytes] = {}
    start = time.monotonic()
    warned = False
    try:
        while args.seconds is None or time.monotonic() - start < args.seconds:
            time.sleep(0.05)
            for universe, frame in sorted(rx.snapshot().items()):
                if args.universe and universe not in universes:
                    continue
                data = bytes(frame.data)
                if shown.get(universe) == data:
                    continue
                shown[universe] = data
                live = " ".join(f"{i + 1}={v}" for i, v in enumerate(data) if v) or "all 0"
                print(f"  {time.monotonic() - start:8.2f} s  U{universe} from '{frame.source}' seq {frame.sequence:3d}:  {live}", flush=True)
            if not warned and not rx.packets and time.monotonic() - start > 5.0:
                warned = True
                print("  nothing received yet - check the interface (--bind), the firewall (UDP "
                      f"{rx.port}) and that the sender uses {args.protocol}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
    print(f"{rx.packets} packet(s) received")
    return 0 if rx.packets else 1


def cmd_devices(_args: argparse.Namespace) -> int:
    from .audio import list_output_devices, miniaudio_available

    if miniaudio_available():
        names = list_output_devices()
        print("audio outputs (patch: [audio] device = \"part of the name\"):")
        for name in names:
            print(f"  {name}")
        if not names:
            print("  none found")
    else:
        print("audio: the 'miniaudio' package is not installed - bass output is unavailable (pip install extended-4d-editor[audio])")
    try:
        from serial.tools import list_ports
    except ImportError:
        print("serial: the 'pyserial' package is not installed - USB DMX (enttec-pro) is unavailable (pip install extended-4d-editor[serial])")
        return 0
    ports = list(list_ports.comports())
    print("serial ports (patch: [output.enttec] port = ...):")
    for p in ports:
        print(f"  {p.device}  {p.description}")
    if not ports:
        print("  none found")
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    """Ask ESP32 nodes what they have. Returns 1 if none of them answered."""
    from .bridge import DEFAULT_PORT, BridgeOutput

    owners: dict[str, str | None] = {h: None for h in args.host}
    port = args.port or DEFAULT_PORT
    lines: dict[int, tuple[str, int, int]] = {}
    if args.patch:
        patch = load_patch(args.patch)
        if patch.output.protocol != "esp32":
            raise ValueError(f"{args.patch}: the output is '{patch.output.protocol}', not esp32")
        ids = _platforms(args.platforms)
        out = create_output(patch, ids)
        assert isinstance(out, BridgeOutput)
        owners |= {h: n.platform for h, n in out.nodes.items()}
        lines, port = out.lines, args.port or out.udp_port
    if not owners:
        raise ValueError('no ESP32 to ask: give addresses, or --patch with [output.esp32] theatre / platforms')
    bridge = BridgeOutput(lines, owners, port)
    bridge.open()
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            bridge.poll()
            time.sleep(0.05)
        nodes = bridge.describe()["nodes"]
    finally:
        bridge.close()
    print(f"ESP32 effects nodes (UDP port {port}):")
    for n in nodes:
        wired = ", ".join(f"line {int(p) + 1}: {w}" for p, w in n["lines"].items())
        role = f" - {n['platform']}" if n.get("platform") else ""
        if not n["present"]:
            print(f"  {n['host']:16s} no answer{role}{' (' + wired + ')' if wired else ''} - switched off, another address, "
                  "or firmware without effects: its part is left out")
            continue
        has = ", ".join(
            {"platform": "platform", "dmx": f"{n['dmx_ports']} DMX line(s)", "audio": f"audio {n['audio_channels']} channel(s)"}[h] for h in n["has"]
        ) or "nothing"
        print(f"  {n['host']:16s} node {n['node']}, firmware {n['firmware']}: {has}{role}{' (' + wired + ')' if wired else ''}"
              + ("  EMERGENCY STOP ACTIVE: outputs dark" if n["inhibited"] else ""))
    return 0 if any(n["present"] for n in nodes) else 1


def cmd_test_vectors(args: argparse.Namespace) -> int:
    from .bridge_vectors import vectors

    if args.output:
        Path(args.output).write_text(vectors())
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(vectors())
    return 0


# ---------------------------------------------------------------------------------------- parser
def build_parser(prog: str = "fourdvr fx") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="4D effects: files, venue patch and live DMX tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    def patch_args(s: argparse.ArgumentParser, output: bool = True) -> None:
        s.add_argument("--patch", required=True, help="venue patch (TOML)")
        s.add_argument("--platforms", default=DEFAULT_PLATFORMS, help="platform ids, comma separated (default %(default)s)")
        if output:
            s.add_argument("--output", choices=("sacn", "artnet", "enttec-pro", "none"),
                           help="override the patch's output protocol (not for esp32: its nodes are part of the patch)")

    s = sub.add_parser("info", help="list the effects of a .motion or .fx.json file")
    s.add_argument("file")
    s.add_argument("--json", action="store_true", help="print the document (without the bass samples)")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("embed", help="put an effects file into a .motion file")
    s.add_argument("motion")
    s.add_argument("effects", help=".fx.json (or another .motion file to copy the effects from)")
    s.add_argument("-o", "--output", help="write here instead of changing the .motion file in place")
    s.add_argument("--force", action="store_true", help="embed although the durations differ")
    s.set_defaults(func=cmd_embed)

    s = sub.add_parser("extract", help="write the effects of a .motion file to a standalone .fx.json")
    s.add_argument("motion")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("strip", help="remove the effects from a .motion file (motion only)")
    s.add_argument("motion")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_strip)

    s = sub.add_parser("example-patch", help="print or write the commented example venue patch")
    s.add_argument("-o", "--output")
    s.add_argument("--force", action="store_true")
    s.add_argument("--esp32", action="store_true", help="the example for DMX devices and bass shakers on ESP32s")
    s.set_defaults(func=cmd_example_patch)

    s = sub.add_parser("check-patch", help="validate a venue patch and print its DMX address table")
    s.add_argument("patch")
    s.add_argument("--platforms", default=DEFAULT_PLATFORMS)
    s.set_defaults(func=cmd_check_patch)

    s = sub.add_parser("check", help="preflight: what a programme's effects do at a venue (limits, missing fixtures); "
                                     "without --patch: against the default limits")
    s.add_argument("file")
    s.add_argument("--patch", help="venue patch (TOML); without it the default limits of every effect type are used")
    s.add_argument("--platforms", default=DEFAULT_PLATFORMS, help="platform ids, comma separated (default %(default)s)")
    s.add_argument("--strict", action="store_true", help="exit with 1 if anything is limited or unpatched")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("play", help="play a programme's effects to DMX now (no server, no motion)")
    s.add_argument("file")
    patch_args(s)
    s.add_argument("--start", type=float, default=0.0, help="start position in seconds")
    s.add_argument("--end", type=float, help="end position in seconds (default: end of programme)")
    s.add_argument("--loop", action="store_true")
    s.add_argument("--every", type=float, default=1.0, help="status line interval in seconds")
    s.add_argument("-q", "--quiet", action="store_true")
    s.add_argument("--audio-backend", default="", help=argparse.SUPPRESS)   # "null": tests without a sound card
    s.set_defaults(func=cmd_play)

    s = sub.add_parser("fire", help="commissioning: switch one fixture on for a moment")
    s.add_argument("fixture", help="fixture id, or id@PLATFORM for a platform fixture (see check-patch)")
    patch_args(s)
    s.add_argument("--level", type=float, default=1.0, help="0..1 (default 1)")
    s.add_argument("--seconds", type=float, default=1.0, help="at most 30")
    s.set_defaults(func=cmd_fire)

    s = sub.add_parser("blackout", help="send zero to every universe of the patch")
    patch_args(s)
    s.set_defaults(func=cmd_blackout)

    s = sub.add_parser("monitor", help="show the DMX that arrives on this PC (sACN or Art-Net)")
    s.add_argument("--protocol", choices=("sacn", "artnet"), default="sacn")
    s.add_argument("-u", "--universe", type=int, action="append", help="universe to show (repeatable; default 1)")
    s.add_argument("--bind", default="", help="local interface address")
    s.add_argument("--port", type=int, help="UDP port (default 5568 sACN, 6454 Art-Net)")
    s.add_argument("--no-multicast", action="store_true", help="sACN: do not join the multicast groups (unicast only)")
    s.add_argument("--seconds", type=float, help="stop after this time")
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("devices", help="list audio outputs and serial ports")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("nodes", help="ask ESP32 effects nodes what they have (DMX line, audio output, platform)")
    s.add_argument("host", nargs="*", help="addresses of ESP32s to ask")
    s.add_argument("--patch", help="ask the ESP32s named in this venue patch (output esp32)")
    s.add_argument("--platforms", default=DEFAULT_PLATFORMS, help="platform ids, comma separated (default %(default)s)")
    s.add_argument("--port", type=int, default=0, help="UDP port of the effects link (default 47002, or the patch's)")
    s.add_argument("--seconds", type=float, default=2.5, help="how long to wait for answers (default %(default)s)")
    s.set_defaults(func=cmd_nodes)

    s = sub.add_parser("test-vectors", help="write the test vectors of the ESP32 effects link (for the firmware's tests)")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_test_vectors)
    return p


def main(argv: list[str] | None = None, prog: str = "fourdvr fx") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        code = int(args.func(args))
        sys.stdout.flush()                    # inside the try: a reader that has gone shows here
        return code
    except BrokenPipeError:
        from fourdvr_studio import reader_gone

        return reader_gone()
    except (ValueError, OSError) as exc:   # EffectsError, PatchError and MotionFormatError are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
