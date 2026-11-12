"""``fourdvr live`` - motion, effects and bass from a running game or a live picture.

    fourdvr live sources                         the sources and what to switch on in each game
    fourdvr live example-config -o live.toml     a commented configuration to start from
    fourdvr live check -c live.toml              what a configuration says, and whether it can run
    fourdvr live monitor --source forza          what the source delivers, in plain words (directions!)
    fourdvr live run -c live.toml                the show (Space: hold / release, + -: strength, q: end)
    fourdvr live sim --format forza --to HOST    play the game's part: a made-up drive in that format
    fourdvr live info session.4dlive             what a recorded session holds
    fourdvr live convert session.4dlive --video session.mp4 --platform profile.toml     a programme of it
    fourdvr live align session.4dlive session.mp4       at which second of the session a video of it begins
    fourdvr live sender godot|unity|python       a ready sender of the open format for your own experience
    fourdvr live devices                         sound inputs and screens a source can use
    fourdvr live calibrate --source screen       measure the delay of a screen or capture-card picture
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from fourdvr_studio import reader_gone

from . import __version__
from .config import EXAMPLE, LiveConfig, LiveConfigError, load_live_config
from .engine import LiveEngine, when
from .session import Session, SessionError
from .sources import INFOS, ORDER, SourceError, canonical

KEYS_HELP = "Space hold / release · + - strength · e engage after a fault · r record · q end"


# =========================================================================== configuration from the command line
def add_show_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--config", help="live.toml (see 'fourdvr live example-config')")
    p.add_argument("--source", help="source type instead of / over the configuration ('fourdvr live sources')")
    p.add_argument("--port", type=int, help="UDP port to listen on")
    p.add_argument("--host", help="address of the game or console (assetto, gt7)")
    p.add_argument("--bind", help="local address to listen on (default: all)")
    p.add_argument("--file", help="file of a 'replay' source (.4dlive) or a 'screen' source (video file)")
    p.add_argument("--gain", type=float, help="strength of the motion, 0-2")
    p.add_argument("--axis", action="append", default=[], metavar="NAME=FACTOR",
                   help="direction factor, e.g. --axis yaw=-1 (may be given several times)")
    p.add_argument("--seconds", type=float, default=0.0, help="end by itself after this long")


def build_config(args: argparse.Namespace, outputs: bool = True) -> LiveConfig:
    cfg = load_live_config(args.config) if args.config else LiveConfig()
    if not args.config and not args.source:
        raise LiveConfigError("give a configuration (-c live.toml) or a source (--source forza); "
                              "'fourdvr live sources' lists the sources")
    if args.source:
        cfg.source = {"type": canonical(args.source)}
    for key in ("port", "host", "bind", "file"):
        value = getattr(args, key, None)
        if value is not None:
            cfg.source[key] = str(Path(value).expanduser().resolve()) if key == "file" else value
    if args.gain is not None:
        cfg.cueing.gain = args.gain
    for item in args.axis:
        name, sep, factor = item.partition("=")
        if not sep:
            raise LiveConfigError(f"--axis {item!r}: write NAME=FACTOR, e.g. yaw=-1")
        try:
            cfg.axes[name.strip()] = float(factor)
        except ValueError as exc:
            raise LiveConfigError(f"--axis {item!r}: {factor!r} is not a number") from exc
    if outputs:
        from fourdvr_fx.patch import PatchError, load_patch
        from fourdvr_motion.platform_config import PlatformConfigError, load_platform_config

        try:
            for n, profile in enumerate(getattr(args, "platform", None) or [], start=1):
                pc = load_platform_config(profile)
                if any(p.platform_id == pc.platform_id for p in cfg.platforms):
                    pc = replace(pc, platform_id=f"{pc.platform_id}-{n}")
                cfg.platforms.append(pc)
            if getattr(args, "patch", None):
                cfg.patch = load_patch(args.patch)
        except (PlatformConfigError, PatchError) as exc:
            raise LiveConfigError(str(exc)) from exc
        if getattr(args, "simulate", False):
            cfg.platforms = [replace(p, driver="simulation") for p in cfg.platforms]
        if getattr(args, "hold", False):
            cfg.hold = True
        if getattr(args, "no_sound", False):
            cfg.sound = False
    else:
        cfg.platforms, cfg.patch, cfg.sound = [], None, False
    cfg.check()
    return cfg


# =========================================================================== the terminal
class Keys:
    """Single key presses from the terminal without Enter (nothing if input is not a terminal)."""

    def __init__(self) -> None:
        self._restore: Any = None
        self.active = False

    def __enter__(self) -> Keys:
        try:
            import termios
            import tty

            if sys.stdin.isatty():
                fd = sys.stdin.fileno()
                self._restore = (fd, termios.tcgetattr(fd))
                tty.setcbreak(fd)
                self.active = True
        except (ImportError, OSError, ValueError):
            self.active = False
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._restore is not None:
            import termios

            fd, old = self._restore
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def read(self) -> str:
        if not self.active:
            return ""
        import select

        ready, _w, _x = select.select([sys.stdin], [], [], 0.0)
        return sys.stdin.read(1) if ready else ""


def _signed(values: Any, digits: int) -> str:
    return " ".join(f"{round(float(v), digits) + 0.0:+.{digits}f}" for v in values)       # (+ 0.0: no "-0.0")


def status_line(st: dict[str, Any]) -> str:
    src = st["source"]
    parts = [when(), f"{st['state']:<22}", f"{round(st['envelope'] * 100):>3d} %",
             f"{src['source']} {src['rate_hz']:g}/s"]
    if st.get("delay_ms") is not None:
        parts.append(f"delay {st['delay_ms']:.0f} ms")
    inp = st.get("input")
    if inp:
        a = inp["accel"]
        parts.append(f"surge {a[0]:+5.1f} sway {a[1]:+5.1f} heave {a[2]:+5.1f}")
        parts.append(f"{inp['speed'] * 3.6:3.0f} km/h" + (f" {inp['rpm']:5d} rpm" if inp["rpm"] else ""))
    levels = st["levels"]
    if levels:
        parts.append(" ".join(f"{k} {round(v * 100)}%" for k, v in sorted(levels.items())))
    for p in st["platforms"]:
        parts.append(f"{p['id']} {p['phase']}" + (f" ({p['fault']})" if p["fault"] else ""))
    if st.get("recording_s") is not None:
        parts.append(f"rec {st['recording_s']:.0f} s")
    if src.get("error"):
        parts.append(f"source: {src['error']}")
    return " | ".join(parts)


def run_show(engine: LiveEngine, seconds: float, record: str | None, words: bool, interval: float = 0.5,
             out: Any = None) -> Path | None:
    """Run until the time is up, 'q' or Ctrl+C; returns the recorded session's path, if any."""
    out = out or sys.stdout
    tty = out.isatty()
    recording_to: str | None = record
    if record is not None or engine.cfg.record_dir is not None:
        engine.start_recording()
    engine.start()
    if engine.output_problem():
        print(f"note: {engine.output_problem()}", file=out)
    saved: Path | None = None
    started = time.monotonic()
    last_words = ""
    try:
        with Keys() as keys:
            if keys.active:
                print(KEYS_HELP, file=out)
            next_status = 0.0
            while True:
                now = time.monotonic()
                if seconds and now - started >= seconds:
                    break
                key = keys.read()
                if key in ("q", "Q", "\x1b"):
                    break
                if key == " ":
                    engine.hold(not engine.guard.hold)
                elif key in ("+", "="):
                    engine.set_gain(engine.cueing.s.gain + 0.05)
                elif key in ("-", "_"):
                    engine.set_gain(engine.cueing.s.gain - 0.05)
                elif key in ("e", "E"):
                    engine.engage()
                elif key in ("r", "R"):
                    if engine.recorder is None:
                        engine.start_recording()
                    else:
                        saved = engine.stop_recording(recording_to if recording_to else None)
                        print(f"\nrecorded: {saved}" if saved else "\nnothing recorded", file=out)
                if now >= next_status:
                    next_status = now + interval
                    st = engine.status()
                    if words:
                        said = ", ".join(st.get("input", {}).get("says", [])) or "-"
                        if said != last_words or not tty:
                            inp = st.get("input")
                            values = (f"  [accel {_signed(inp['accel'], 1)} m/s²  rates {_signed(inp['rates'], 2)} rad/s  "
                                      f"{inp['speed'] * 3.6:.0f} km/h]" if inp else f"  ({st['state']})")
                            print(f"{when()}  {said}{values}", file=out)
                            last_words = said
                    elif tty:
                        print("\r\x1b[K" + status_line(st)[: max(40, _columns() - 1)], end="", file=out, flush=True)
                    else:
                        print(status_line(st), file=out, flush=True)
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        if tty and not words:
            print(file=out)
        engine.stop()
        if engine.recorder is not None:
            target = recording_to if recording_to else None
            saved = engine.stop_recording(target) or saved
    return saved


def _columns() -> int:
    import shutil

    return shutil.get_terminal_size((120, 24)).columns


# =========================================================================== commands
def cmd_sources(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps([{"name": n, "title": INFOS[n].title, "games": INFOS[n].games, "port": INFOS[n].port or None,
                           "needs_host": INFOS[n].needs_host, "setup": INFOS[n].setup, "published": INFOS[n].published,
                           "experimental": INFOS[n].experimental} for n in ORDER], indent=2))
        return 0
    for name in ORDER:
        i = INFOS[name]
        port = f"UDP port {i.port}" if i.port else "no port"
        print(f"{name:<11} {i.title}  [{port}{', needs host' if i.needs_host else ''}]")
        print(f"            for: {i.games}")
        print(f"            in the game: {i.setup}")
        if i.published:
            print(f"            format: {i.published}")
    print("\nThe directions of each game's values are checked once with 'fourdvr live monitor' (docs/LIVE.md).")
    return 0


def cmd_example(args: argparse.Namespace) -> int:
    if args.output:
        Path(args.output).write_text(EXAMPLE, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(EXAMPLE, end="")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    print("\n".join(cfg.describe()))
    for note in cfg.notes:
        print(f"note        {note}")
    src = INFOS[canonical(str(cfg.source.get("type", "open")))]
    print(f"in the game {src.setup}")
    if src.needs_host and not cfg.source.get("host"):
        print("PROBLEM     this source needs host = \"<address of the game or console>\"")
        return 1
    try:                                          # made, not started: its settings are read, nothing is opened
        from .sources import create

        create(cfg.source)
        file = str(cfg.source.get("file", "") or "")
        if file and not Path(file).expanduser().is_file():
            raise SourceError(f"the file {file} does not exist")
    except ImportError as exc:
        print(f"PROBLEM     this source needs the workstation packages ({exc})")
        return 1
    except (SourceError, SessionError, ValueError, OSError) as exc:
        print(f"PROBLEM     {exc}")
        return 1
    if src.experimental:
        print("note        experimental: this format is not published by the game's maker and may stop working")
    if cfg.patch is not None:
        for w in cfg.patch.warnings():
            print(f"warning     effects patch: {w}")
    print("ok")
    return 0


def _grabs_screen(cfg: LiveConfig) -> bool:
    source = cfg.source
    return (canonical(str(source.get("type", "open"))) == "screen"
            and str(source.get("capture", "file" if source.get("file") else "screen")) == "screen")


def cmd_run(args: argparse.Namespace) -> int:
    if args.control:                              # started by a live window: the show for it (remote.py)
        from .remote import RemoteSource, serve

        try:
            cfg = build_config(args)
            engine = LiveEngine(cfg, source=RemoteSource(cfg.source) if args.remote_source else None,
                                audio_backend="null" if args.null_audio else "")
        except (LiveConfigError, SourceError, ValueError, OSError) as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
            return 1
        return serve(engine)
    cfg = build_config(args)
    print("\n".join(cfg.describe()))
    picture = canonical(str(cfg.source.get("type", "open"))) == "screen"
    grabbed = picture and _grabs_screen(cfg)      # the window does the grabbing
    if args.window or (args.window is None and picture and (grabbed or str(cfg.source.get("mode", "delay")) == "delay")):
        try:
            from .window import run_window
        except ImportError as exc:
            print(f"error: the live window needs the workstation packages ({exc}); run without --window",
                  file=sys.stderr)
            return 2
        return run_window(cfg, args.raw, args.seconds, args.record, audio_backend="null" if args.null_audio else "")
    if grabbed:
        raise LiveConfigError('capture = "screen" is done by the live window: it cannot run with --no-window '
                              "(a camera, a stream or a file can)")
    engine = LiveEngine(cfg, audio_backend="null" if args.null_audio else "")
    saved = run_show(engine, args.seconds, args.record, words=False, interval=args.status_interval)
    _report(engine, saved)
    return 0


def report_lines(engine: LiveEngine, saved: Path | None) -> list[str]:
    """What there is to say when a show has ended."""
    st = engine.status()
    src = st["source"]
    lines = [f"source: {src['samples']} samples" + (f", {src.get('invalid', 0)} packets refused" if src.get("invalid") else "")
             + (f", {st['rejected']} values refused" if st["rejected"] else "")]
    for p in st["platforms"]:
        clamps = {k: v for k, v in p["limits"].items() if v}
        lines.append(f"{p['id']}: {p['phase']}" + (f", fault: {p['fault']}" if p["fault"] else "")
                     + (f", limiter worked: {clamps}" if clamps else ""))
    if engine.output_problem():
        lines.append(engine.output_problem())
    if st["max_late_ms"] > 50.0:
        lines.append(f"note: the control loop ran up to {st['max_late_ms']:.0f} ms late ({st['late_ticks']} of {st['ticks']} "
                     "periods more than 50 ms) - this computer was busy (a platform's board stops and reports a fault "
                     "when commands are missing for its time-out)")
    if saved is not None:
        lines.append(f"recorded: {saved}")
    return lines


def _report(engine: LiveEngine, saved: Path | None) -> None:
    print("\n".join(report_lines(engine, saved)))


def cmd_monitor(args: argparse.Namespace) -> int:
    cfg = build_config(args, outputs=False)
    if _grabs_screen(cfg):
        raise LiveConfigError('capture = "screen" is done by the live window: use "fourdvr live run"')
    src = INFOS[canonical(str(cfg.source.get("type", "open")))]
    print(f"{src.title}: {src.setup}")
    print("Drive and compare: brake -> 'braking', turn left -> 'pushed left' and 'nose turning left', "
          "go over a crest -> 'lifted'.\nA direction that comes out the wrong way round is turned with "
          "--axis NAME=-1 (surge, sway, heave, roll, pitch, yaw) and then written into [axes].\n")
    engine = LiveEngine(cfg, effects=False)
    saved = run_show(engine, args.seconds, args.record, words=True, interval=0.25)
    _report(engine, saved)
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    from .sim import ENCODERS, SimSender

    name = canonical(args.format)
    if name not in ENCODERS:
        print(f"error: there is no simulator for {name!r} (formats: {', '.join(ENCODERS)})", file=sys.stderr)
        return 2
    host, _sep, port = args.to.partition(":")
    sender = SimSender(name, host or "127.0.0.1", int(port) if port else 0, args.rate)
    where = "waits to be asked" if name in ("assetto", "gt7") else f"to {sender.host}:{sender.port or INFOS[name].port}"
    print(f"simulated {INFOS[name].title}: a 40-second drive, again and again, {args.rate:g} frames a second, {where} "
          "(Ctrl+C ends)")
    stop = threading.Event()
    try:
        sent = sender.run(args.seconds, stop)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent {sent} packets")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    session = Session.load(args.file)
    summary = session.summary()
    if args.json:
        print(json.dumps({**summary, "header": session.header}, indent=2))
        return 0
    print(f"{Path(args.file).name}: live session, {summary['duration_s']:.1f} s at {summary['rate_hz']:g} Hz "
          f"({session.samples} rows)")
    print(f"  source        {summary['source']}")
    print(f"  recorded      {session.header.get('created_at', '?')} by {session.header.get('generator', '?')}")
    print(f"  running       {summary['running_s']:.1f} s (the rest: waiting, paused or held)")
    print(f"  top speed     {summary['top_speed_m_s'] * 3.6:.0f} km/h")
    a = summary["peak_accel_m_s2"]
    print(f"  strongest     surge {a[0]:.1f}, sway {a[1]:.1f}, heave {a[2]:.1f} m/s²")
    if summary["effects"]:
        print("  effects       " + ", ".join(f"{k} up to {round(v * 100)} %" for k, v in summary["effects"].items()))
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    try:
        from .convert import ConvertOptions, convert_session
    except ImportError as exc:
        print(f"error: making a programme of a session needs the workstation packages ({exc}):\n"
              "       pip install 'extended-4d-editor[studio]'", file=sys.stderr)
        return 2
    session = Session.load(args.file)
    output = Path(args.output) if args.output else Path(args.file).with_suffix(".motion")
    offset = 0.0
    if args.video:
        if args.start or args.end:
            raise ValueError("with --video the programme is as long as the video: use --offset, not --start / --end")
        if args.offset is None:
            from .convert import find_offset

            print(f"  looking for where {Path(args.video).name} belongs in the session")
            offset, confidence = find_offset(session, args.video, projection=args.projection, stereo=args.stereo)
            if confidence < 0.4:
                raise ValueError(f"could not find where the video belongs in the session (best match {confidence:.2f} at "
                                 f"{offset:.2f} s): give it with --offset SECONDS")
            print(f"  the video begins at second {offset:.2f} of the session (match {confidence:.2f})")
        else:
            offset = args.offset
    options = ConvertOptions(
        profile=args.platform, programme_id=args.programme_id, gain=args.gain, kind=args.kind,
        effects=not args.no_effects, bass=not args.no_bass, start_s=args.start, end_s=args.end,
        allow_violations=args.allow_violations, video=args.video, video_offset_s=offset,
        projection=args.projection, stereo=args.stereo,
    )
    result = convert_session(session, output, options, progress=lambda _f, text: print(f"  {text}"))
    print(f"wrote {output}: {result.duration_s:.1f} s, programme {options.programme_id}, "
          f"validation {'passed' if result.passed else 'FAILED'}")
    print(f"  motion    stroke used {result.stroke_utilisation * 100:.0f} %, strongest axes: {result.peaks}")
    if result.effects:
        print(f"  effects   {', '.join(result.effects)}" + (", bass track" if result.bass else ""))
    if args.video:
        print(f"  video     {Path(args.video).name}: upload this video and this file to programme {options.programme_id}")
    else:
        print("  video     none: the Control Server accepts a motion file only together with the video it was made for "
              "(--video FILE)")
    print("Open it in the Editor (fourdvr FILE.motion) to look at it and to change the effects.")
    return 0 if result.passed or args.allow_violations else 1


def cmd_align(args: argparse.Namespace) -> int:
    try:
        from .convert import find_offset
    except ImportError as exc:
        print(f"error: this needs the workstation packages ({exc})", file=sys.stderr)
        return 2
    session = Session.load(args.file)
    offset, confidence = find_offset(session, args.video, args.seconds, projection=args.projection, stereo=args.stereo)
    if confidence < 0.4:
        print(f"not found: the best match is {confidence:.2f} at {offset:.2f} s - the picture does not turn the way the "
              "session says")
        return 1
    print(f"the video begins at second {offset:.2f} of the session (match {confidence:.2f})")
    return 0


def cmd_sender(args: argparse.Namespace) -> int:
    from .senders import SENDERS

    name, text = SENDERS[args.kind]
    if args.output:
        target = Path(args.output)
        if target.is_dir():
            target = target / name
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(text, end="")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    """What a "sound" or "screen" source can be pointed at on this computer."""
    from .sources.sound import list_capture_devices

    inputs = list_capture_devices()
    print("sound inputs ([source] type = \"sound\", device = part of a name):")
    for name in inputs:
        print(f"  {name}")
    if not inputs:
        print("  none found (no sound system, or the miniaudio package is missing)")
    print("screens ([source] type = \"screen\", capture = \"screen\", screen = number):")
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
        for k, screen in enumerate(QGuiApplication.screens()):
            g = screen.geometry()
            print(f"  {k}: {screen.name()}  {g.width()} x {g.height()} at {g.x()},{g.y()}")
        platform = app.platformName() if hasattr(app, "platformName") else ""
        if platform.startswith("wayland"):
            print("  this is a Wayland session: the screen cannot be grabbed here (docs/LIVE.md, section 9)")
    except Exception as exc:  # noqa: BLE001 - no display, no toolkit: say so and go on
        print(f"  not available here ({type(exc).__name__}: {exc})")
    print("cameras and capture cards: capture = \"camera\", device = 0, 1 ... or \"/dev/videoN\" (v4l2-ctl --list-devices)")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    try:
        from .window import run_calibration
    except ImportError as exc:
        print(f"error: measuring the picture delay needs the workstation packages ({exc})", file=sys.stderr)
        return 2
    cfg = build_config(args, outputs=False)
    return run_calibration(cfg, args.seconds or 12.0)


# =========================================================================== parser
def build_parser(prog: str = "fourdvr live") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sources", help="list the sources and what to switch on in each game")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sources)

    s = sub.add_parser("example-config", help="print a commented live.toml")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_example)

    def outputs(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--platform", action="append", metavar="PROFILE.toml",
                        help="a platform profile (in addition to the configuration); may be given several times")
        sp.add_argument("--simulate", action="store_true", help="simulation driver for every platform (no hardware)")
        sp.add_argument("--patch", help="venue effects patch (instead of the configuration's)")
        sp.add_argument("--hold", action="store_true", help="start held: nothing moves until released (Space)")
        sp.add_argument("--no-sound", action="store_true", help="without a patch: no shaker sound on this computer")

    s = sub.add_parser("check", help="say what a configuration means and whether it can run")
    add_show_options(s)
    outputs(s)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="run the live show")
    add_show_options(s)
    outputs(s)
    s.add_argument("--record", nargs="?", const="", metavar="FILE.4dlive",
                   help="record the session (to FILE, or into the configured / current folder)")
    s.add_argument("--window", action=argparse.BooleanOptionalAction, default=None,
                   help="the live window: picture (delayed, for screen capture), meters, controls")
    s.add_argument("--status-interval", type=float, default=0.5)
    s.add_argument("--null-audio", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--control", action="store_true", help=argparse.SUPPRESS)          # the engine for a live window
    s.add_argument("--remote-source", action="store_true", help=argparse.SUPPRESS)    # ... whose picture source is there
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("monitor", help="show what the source delivers, in plain words (no outputs)")
    add_show_options(s)
    s.add_argument("--record", nargs="?", const="", metavar="FILE.4dlive")
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("sim", help="play the game's part: send a made-up drive in a game's format")
    s.add_argument("--format", required=True, help="open, forza, f1, dirt, pcars2, assetto, outsim, beamng, xplane, "
                   "flightgear, gt7")
    s.add_argument("--to", default="127.0.0.1", metavar="HOST[:PORT]", help="where the live show listens")
    s.add_argument("--rate", type=float, default=60.0)
    s.add_argument("--seconds", type=float, default=0.0)
    s.set_defaults(func=cmd_sim)

    s = sub.add_parser("info", help="summarise a recorded session")
    s.add_argument("file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("convert", help="make a .motion programme (with effects) of a recorded session")
    s.add_argument("file")
    s.add_argument("-o", "--output", help="default: the session's name with .motion")
    s.add_argument("--platform", help="platform profile to shape and validate the motion for (default: the example)")
    s.add_argument("--programme-id", type=int, default=1)
    s.add_argument("--gain", type=float, default=1.0, help="strength of the motion (as the Editor's Amount)")
    s.add_argument("--kind", choices=("auto", "car", "aircraft", "ride"), default="auto",
                   help="what the session is of (chooses the balance of the cues)")
    s.add_argument("--start", type=float, default=0.0, help="leave out the session before this second")
    s.add_argument("--end", type=float, default=0.0, help="... and after this one (0: to the end)")
    s.add_argument("--no-effects", action="store_true")
    s.add_argument("--no-bass", action="store_true")
    s.add_argument("--allow-violations", action="store_true")
    s.add_argument("--video", help="the video the programme is for (a recording of the session): sets the length "
                   "and the video reference the Control Server checks")
    s.add_argument("--offset", type=float, help="second of the session at which that video begins (default: found "
                   "by comparing the picture with the telemetry)")
    s.add_argument("--projection", choices=("flat", "180", "360"), help="of the video, if the guess is wrong")
    s.add_argument("--stereo", choices=("mono", "sbs", "ou"))
    s.set_defaults(func=cmd_convert)

    s = sub.add_parser("align", help="find at which second of a session a video of it begins")
    s.add_argument("file", help="the session (.4dlive)")
    s.add_argument("video")
    s.add_argument("--seconds", type=float, default=90.0, help="how much of the video to look at (default %(default)s)")
    s.add_argument("--projection", choices=("flat", "180", "360"))
    s.add_argument("--stereo", choices=("mono", "sbs", "ou"))
    s.set_defaults(func=cmd_align)

    s = sub.add_parser("sender", help="print a sender of the open live format for your own experience")
    s.add_argument("kind", choices=("godot", "unity", "python", "flightgear"))
    s.add_argument("-o", "--output", help="file or folder to write to")
    s.set_defaults(func=cmd_sender)

    s = sub.add_parser("devices", help="list the sound inputs and screens a source can use")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("calibrate", help="measure the delay of a screen / capture-card picture with a test pattern")
    add_show_options(s)
    s.set_defaults(func=cmd_calibrate)
    return p


def main(argv: list[str] | None = None, prog: str = "fourdvr live") -> int:
    args = build_parser(prog).parse_args(argv)
    args.raw = list(sys.argv[1:] if argv is None else argv)
    try:
        code = int(args.func(args))
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        return reader_gone()
    except (LiveConfigError, SourceError, SessionError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
