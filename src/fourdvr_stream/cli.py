"""``fourdvr stream`` - videos from this computer to any device on the network, on one clock.

    fourdvr stream serve VIDEO_OR_FOLDER...      the server: a page for viewers, one for the operator, the files by address
    fourdvr stream serve FOLDER --follow http://control-server:8080 --user viewer --password-file FILE
                                                 ... in step with the theatre's show (the Control Server is not changed)
    fourdvr stream check VIDEO... --viewers 50   will browsers play it, and what does it ask of the network
    fourdvr stream prepare IN -o OUT.mp4         make it start at once (index to the front); --transcode: H.264 for every browser
    fourdvr stream test-video -o test.mp4        a clock, a sweeping bar and a beep: are two screens in step?
    fourdvr stream probe URL --viewers 50        simulated viewers: does the network carry them?
    fourdvr stream control play VIDEO --url URL --key KEY       the operator's commands from a script
    fourdvr stream cert -o DIR                   a certificate of your own for https (headset browsers want it for VR)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fourdvr_studio import PRODUCT_TITLE, reader_gone

from . import __version__
from .library import PROJECTIONS, STEREO_MODES, Library, LibraryError, MediaItem

KIND = {"flat": "flat", "180": "180°", "360": "360°"}
STEREO = {"mono": "", "sbs": " side by side", "ou": " over-under"}


def clock_text(seconds: float, unknown: str = "?") -> str:
    """A length or a position as minutes:seconds (a length of 0 is one that is not known)."""
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}" if seconds > 0 or not unknown else unknown


def describe_item(item: MediaItem) -> str:
    verdict, why = item.browsers()
    size = f"{item.width}x{item.height}" if item.width else "?"
    rate = f"{item.bitrate / 1e6:5.1f} Mbit/s" if item.bitrate else "    ? Mbit/s"
    return (f"  {item.id:<28} {KIND[item.projection] + STEREO[item.stereo]:<16} {clock_text(item.duration_s):>6}  {rate}  {size:<10} "
            f"browsers: {verdict} - {why}")


def build_library(args: argparse.Namespace) -> Library:
    library = Library()
    settings: dict[str, Any] = {}
    if getattr(args, "projection", None):
        settings["projection"] = args.projection
    if getattr(args, "stereo", None):
        settings["stereo"] = args.stereo
    for path in args.paths:
        p = Path(path).expanduser()
        if settings and p.is_dir():
            raise LibraryError("--projection / --stereo are for single files; for a folder write them into its stream.toml")
        library.add_path(p, **(settings if p.is_file() else {}))
    if not library.items:
        raise LibraryError("no video found (files, or folders with .mp4 .webm .mkv .mov ... in them)")
    return library


# =========================================================================== serve
def cmd_serve(args: argparse.Namespace) -> int:
    from .server import ControlError, StreamServer

    library = build_library(args)
    if bool(args.cert) != bool(args.cert_key):
        raise LibraryError("give --cert and --cert-key together ('fourdvr stream cert' makes both)")
    password = ""
    if args.follow:
        if not args.user:
            raise LibraryError("--follow needs --user (an account of the Control Server; the role 'viewer' is enough)")
        if args.password_file:
            password = Path(args.password_file).expanduser().read_text(encoding="utf-8").splitlines()[0].strip()
        else:
            password = os.environ.get("FOURDVR_STREAM_PASSWORD", "")
        if not password:
            raise LibraryError("--follow needs the account's password: --password-file FILE, or the environment variable "
                               "FOURDVR_STREAM_PASSWORD")
    try:
        server = StreamServer(library, args.bind, args.port, args.key or os.environ.get("FOURDVR_STREAM_KEY") or None,
                              args.cert, args.cert_key, args.max_viewers,
                              not args.no_library, args.start_lead, args.title or "")
    except OSError as exc:
        raise LibraryError(f"cannot listen on {args.bind}:{args.port} ({exc.strerror or exc}); another port: --port") from exc
    print(f"{PRODUCT_TITLE} {__version__} - stream")
    for item in library.items.values():
        print(describe_item(item))
        for note in item.notes:
            print(f"  {'':<28} note: {note}")
    urls = server.urls()
    largest = max((item.bitrate for item in library.items.values()), default=0.0)
    print(f"viewers       {urls[0]}/" + "".join(f"\n              {u}/" for u in urls[1:]))
    if not args.no_library:
        print(f"watch alone   {urls[0]}/library")
    print(f"operator      {urls[0]}/control?key={server.key}")
    print(f"any player    {urls[0]}/media/{next(iter(library.items))}" + ("   (and so on: /media/<name>)" if len(library.items) > 1 else ""))
    if largest:
        print(f"network       {args.max_viewers} viewers of the largest video take {largest * args.max_viewers / 1e6:.0f} Mbit/s; "
              "a wired gigabit link carries about 800, one Wi-Fi access point 150-400 in all")
    if not args.cert:
        print("note          plain http: fine for watching; a headset's browser offers VR, and a phone its motion sensor, "
              "only over https (--cert, --cert-key)")
    follower = None
    server.start()
    try:
        if args.follow:
            from .theatre import TheatreFollower

            follower = TheatreFollower(server, args.follow, args.user, password)
            follower.start()
            print(f"following     the show of the Control Server at {args.follow} (account {args.user})")
        elif args.play is not None:
            video = args.play or next(iter(library.items))
            try:
                server.control("play", video, loop=args.loop)
            except ControlError as exc:
                raise LibraryError(str(exc)) from exc
            print(f"playing       {video}" + (", again and again" if args.loop else ""))
        elif args.loop:
            server.control("loop", loop=True)
        print("Ctrl+C ends." if not args.seconds else f"ends by itself after {args.seconds:g} s")
        sys.stdout.flush()
        started = printed = time.monotonic()
        last = ""
        while not args.seconds or time.monotonic() - started < args.seconds:
            time.sleep(0.5 if args.seconds else 2.0)
            if args.quiet:
                continue
            st = server.status()
            tl = st["timeline"]
            what = (f"{tl['state']:<8} {tl['video'] or '-':<24} {st['watching']:3d} watching"
                    + (f", {st['stalled']} loading" if st["stalled"] else "")
                    + (f" | {follower.last_error}" if follower is not None and follower.last_error else "")
                    + (f" | {tl['note']}" if tl["note"] else ""))
            now = time.monotonic()
            if what != last or now - printed >= 15.0:     # when something happens, and now and then while it plays
                step = (f", {st['in_step']} in step" + (f", furthest off {st['worst_drift_ms']:.0f} ms"
                                                       if st["worst_drift_ms"] is not None else "")) if st["watching"] else ""
                if what != last or st["watching"] or st["bits_per_second"] > 0:
                    print(time.strftime("%H:%M:%S ") + what + step + f", {st['bits_per_second'] / 1e6:.1f} Mbit/s", flush=True)
                last, printed = what, now
    except KeyboardInterrupt:
        print()
    finally:
        if follower is not None:
            follower.stop()
        st = server.status()
        server.stop()
    print(f"sent {st['bytes_sent'] / 1e6:.1f} MB in {st['media_requests']} requests"
          + (f"; {st['refused']} viewers were refused (more than --max-viewers {args.max_viewers})" if st["refused"] else ""))
    return 0


# =========================================================================== check
def cmd_check(args: argparse.Namespace) -> int:
    library = build_library(args)
    items = list(library.items.values())
    if args.json:
        print(json.dumps([{**item.to_dict(), "path": str(item.path)} for item in items], indent=2))
        return 0
    worst = 0.0
    for item in items:
        verdict, why = item.browsers()
        print(f"{item.path.name}")
        print(f"  picture     {item.width} x {item.height}  {item.video_codec or '?'}   sound {item.audio_codec or 'none / ?'}"
              if item.width else f"  picture     ? ({item.video_codec or 'codec not known'})")
        print(f"  shown as    {KIND[item.projection]}{STEREO[item.stereo]}   (from the name and the shape: say so with "
              "--projection / --stereo or in stream.toml if this is wrong)")
        print(f"  length      {clock_text(item.duration_s)}   {item.size / 1e6:.1f} MB"
              + (f"   {item.bitrate / 1e6:.1f} Mbit/s" if item.bitrate else ""))
        print(f"  browsers    {verdict}: {why}")
        for note in item.notes:
            print(f"  note        {note}")
        if item.bitrate:
            need = item.bitrate * args.viewers / 1e6
            worst = max(worst, need)
            share = f" - {need / args.network * 100:.0f} % of {args.network:g} Mbit/s" if args.network else ""
            print(f"  network     {args.viewers} viewers take {need:.0f} Mbit/s{share}"
                  + ("  ** more than the network carries **" if args.network and need > args.network * 0.8 else ""))
    if args.network and worst > args.network * 0.8:
        print(f"\nFor {args.viewers} viewers on {args.network:g} Mbit/s a video may have at most "
              f"{args.network * 0.8 / args.viewers:.1f} Mbit/s (a fifth is kept free): 'fourdvr stream prepare --transcode "
              "--bitrate' makes such a version.")
        return 1
    return 0


# =========================================================================== prepare, test video
def _progress(quiet: bool) -> Any:
    shown = [-1]

    def say(fraction: float, text: str) -> None:
        percent = int(fraction * 100)
        if not quiet and sys.stdout.isatty() and percent != shown[0]:
            shown[0] = percent
            print(f"\r  {text} {percent:3d} %", end="", flush=True)

    return say


def cmd_prepare(args: argparse.Namespace) -> int:
    from .library import describe
    from .media import MediaError, ffmpeg_command, prepare

    source = Path(args.source).expanduser()
    target = Path(args.output).expanduser() if args.output else source.with_name(source.stem + ".stream.mp4")
    if args.print_ffmpeg:
        print(ffmpeg_command(source, target, args.transcode, args.height, args.bitrate))
        return 0
    if target.exists() and not args.force:
        raise LibraryError(f"{target} exists (--force writes over it)")
    try:
        result = prepare(source, target, args.transcode, args.height, args.bitrate, _progress(args.quiet))
    except MediaError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        print(f"with the ffmpeg program:  {ffmpeg_command(source, target, args.transcode, args.height, args.bitrate)}", file=sys.stderr)
        return 1
    if sys.stdout.isatty() and not args.quiet:
        print("\r" + " " * 30 + "\r", end="")
    item = describe(target)
    verdict, why = item.browsers()
    print(f"wrote {target} ({result['size'] / 1e6:.1f} MB, {'re-encoded' if args.transcode else 'picture and sound unchanged'})")
    print(f"  browsers    {verdict}: {why}")
    return 0


def cmd_test_video(args: argparse.Namespace) -> int:
    from .media import MediaError, test_video

    try:
        width, _x, height = args.size.lower().partition("x")
        w, h = int(width), int(height)
    except ValueError as exc:
        raise LibraryError("--size is WIDTHxHEIGHT, for example 1280x720") from exc
    target = Path(args.output).expanduser()
    if target.exists() and not args.force:
        raise LibraryError(f"{target} exists (--force writes over it)")
    try:
        result = test_video(target, args.seconds, w, h, args.fps, args.projection, _progress(args.quiet), args.codec or "")
    except MediaError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    if sys.stdout.isatty() and not args.quiet:
        print("\r" + " " * 30 + "\r", end="")
    print(f"wrote {target}: {result['seconds']:g} s, {result['width']} x {result['height']}, {result['fps']} pictures a second, "
          f"{result['codec'].upper()}, {result['size'] / 1e6:.1f} MB")
    print("The bar crosses the picture once a second and a beep sounds on every full second: on two screens side by side "
          "the distance between the bars is how far they are apart (the whole width = 1 s).")
    return 0


# =========================================================================== probe, control, cert
def cmd_probe(args: argparse.Namespace) -> int:
    from . import probe

    url = args.url
    bitrate = args.bitrate * 1e6
    if not bitrate and not args.flat_out:
        try:                                      # the server's own list knows the video's bitrate
            base, _sep, name = url.partition("/media/")
            listing = json.loads(_fetch(base + "/api/library", insecure=args.insecure))
            bitrate = float(next(v["bitrate"] for v in listing["videos"] if v["id"] == name))
        except (OSError, ValueError, KeyError, StopIteration):
            bitrate = 0.0
        if not bitrate:
            print("the video's bitrate is not known: taking it as fast as it comes (or give --bitrate MBIT)")
    try:
        size = probe.head(url, insecure=args.insecure)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.viewers} simulated viewers of {url} ({size / 1e6:.1f} MB) for {args.seconds:g} s, "
          + (f"each at the video's {bitrate / 1e6:.1f} Mbit/s" if bitrate and not args.flat_out else "as fast as it comes"))
    result = probe.run(url, args.viewers, args.seconds, bitrate, args.flat_out, args.insecure)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  answered      {result['answered']} of {result['viewers']}" + ("" if not result["errors"] else f"   errors: {'; '.join(result['errors'])}"))
        print(f"  together      {result['total_bits_per_second'] / 1e6:.0f} Mbit/s")
        print(f"  per viewer    slowest {result['slowest_bits_per_second'] / 1e6:.1f}, median {result['median_bits_per_second'] / 1e6:.1f} Mbit/s; "
              f"first byte after at most {result['slowest_first_byte_s'] * 1000:.0f} ms")
        if not result["flat_out"]:
            print(f"  would stall   {result['would_have_stalled']} viewer(s)"
                  + (f" (the worst was behind for {result['longest_behind_s']:.1f} s)" if result["would_have_stalled"] else
                     ": every viewer got the video faster than it plays"))
    ok = result["answered"] == result["viewers"] and (result["flat_out"] or result["would_have_stalled"] == 0)
    if not args.json:
        print("ok" if ok else "NOT ENOUGH: fewer viewers, a lower bitrate ('fourdvr stream prepare --transcode --bitrate'), or more network")
    return 0 if ok else 1


def _fetch(url: str, data: bytes | None = None, insecure: bool = False, timeout: float = 8.0) -> str:
    import ssl

    context = None
    if url.startswith("https") and insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return str(response.read().decode())


def cmd_control(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    key = args.key or os.environ.get("FOURDVR_STREAM_KEY", "")
    try:
        if args.action == "status":
            st = json.loads(_fetch(f"{base}/api/status?key={key}", insecure=args.insecure))
            if args.json:
                print(json.dumps(st, indent=2))
            else:
                tl = st["timeline"]
                print(f"{tl['state']}  {tl['video'] or '-'}  at {clock_text(max(0.0, tl['position_s']), '')}"
                      + (f" of {clock_text(tl['duration_s'])}" if tl["duration_s"] else "") + (", again and again" if tl["loop"] else "")
                      + (f"  ({tl['source']})" if tl["source"] != "operator" else ""))
                print(f"{st['watching']} watching, {st['in_step']} in step"
                      + (f", furthest off {st['worst_drift_ms']:.0f} ms" if st["worst_drift_ms"] is not None else "")
                      + f", {st['stalled']} loading, {st['bits_per_second'] / 1e6:.1f} Mbit/s")
            return 0
        body: dict[str, Any] = {"action": args.action}
        if args.video:
            body["video"] = args.video
        if args.position is not None:
            body["position_s"] = args.position
        if args.action == "loop":
            body["loop"] = (args.video or "on").lower() in ("on", "yes", "true", "1")
            body.pop("video", None)
        elif args.loop:
            body["loop"] = True
        tl = json.loads(_fetch(f"{base}/api/control?key={key}", json.dumps(body).encode(), args.insecure))
        print(f"{tl['state']}  {tl['video'] or '-'}" + (f"  starts in {tl['start_at'] - tl['server_time']:.1f} s"
                                                         if tl["state"] == "playing" and tl["start_at"] > tl["server_time"] else ""))
        return 0
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode()).get("error", "")
        except ValueError:
            message = ""
        print(f"error: {message or f'the server answers {exc.code}'}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: the stream server at {base} does not answer ({exc})", file=sys.stderr)
        return 1


def cert_command(folder: Path, names: list[str]) -> list[str]:
    alt = ",".join(("IP:" if n.replace(".", "").isdigit() else "DNS:") + n for n in names)
    return ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825", "-keyout", str(folder / "stream.key"),
            "-out", str(folder / "stream.crt"), "-subj", f"/CN={names[0]}", "-addext", f"subjectAltName={alt}"]


def cmd_cert(args: argparse.Namespace) -> int:
    from .server import local_addresses

    folder = Path(args.output).expanduser()
    names = args.names or [*local_addresses(), socket.gethostname(), "localhost"]
    command = cert_command(folder, names)
    printable = " ".join(f"'{c}'" if any(ch in c for ch in " =,/") and not c.startswith("-") else c for c in command)
    if args.print or shutil.which("openssl") is None:
        if not args.print:
            print("The openssl program is not installed here. On a computer that has it:", file=sys.stderr)
        print(printable)
        return 0 if args.print else 1
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "stream.crt").exists() and not args.force:
        raise LibraryError(f"{folder / 'stream.crt'} exists (--force makes a new one)")
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        print(f"error: openssl: {done.stderr.strip()[-400:]}", file=sys.stderr)
        return 1
    (folder / "stream.key").chmod(0o600)
    print(f"wrote {folder / 'stream.crt'} and {folder / 'stream.key'} for {', '.join(names)} (valid 825 days)")
    print(f"use them:  fourdvr stream serve ... --cert {folder / 'stream.crt'} --cert-key {folder / 'stream.key'}")
    print("A browser will say once that it does not know who issued this certificate: that is expected for one you made "
          "yourself - accept it on each device.")
    return 0


# =========================================================================== the parser
def build_parser(prog: str = "fourdvr stream") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("serve", help="serve videos to the devices on the network")
    s.add_argument("paths", nargs="+", metavar="VIDEO_OR_FOLDER")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all)")
    s.add_argument("--title", help="what the pages are called")
    s.add_argument("--projection", choices=PROJECTIONS, help="of a single file, if the guess is wrong")
    s.add_argument("--stereo", choices=STEREO_MODES)
    s.add_argument("--play", nargs="?", const="", metavar="VIDEO", help="start the show at once (the first video, or this one)")
    s.add_argument("--loop", action="store_true", help="again and again")
    s.add_argument("--key", help="the key of the operator's page (or FOURDVR_STREAM_KEY; default: a new random one each time)")
    s.add_argument("--max-viewers", type=int, default=100, help="more are refused (default %(default)s)")
    s.add_argument("--no-library", action="store_true", help="only the show: viewers cannot pick a video for themselves")
    s.add_argument("--start-lead", type=float, default=3.0, metavar="S",
                   help="play starts this far in the future, so that every device is ready (default %(default)s)")
    s.add_argument("--cert", help="certificate (PEM) for https")
    s.add_argument("--cert-key", help="its private key")
    s.add_argument("--follow", metavar="URL", help="follow the show of this Control Server instead of an operator")
    s.add_argument("--user", help="account at the Control Server (role viewer is enough)")
    s.add_argument("--password-file", help="file with that account's password (or FOURDVR_STREAM_PASSWORD)")
    s.add_argument("--quiet", action="store_true", help="no status lines")
    s.add_argument("--seconds", type=float, default=0.0, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("check", help="what a video is, whether browsers play it, what it asks of the network")
    s.add_argument("paths", nargs="+", metavar="VIDEO_OR_FOLDER")
    s.add_argument("--viewers", type=int, default=50)
    s.add_argument("--network", type=float, default=0.0, metavar="MBIT", help="what the network carries, to compare with")
    s.add_argument("--projection", choices=PROJECTIONS)
    s.add_argument("--stereo", choices=STEREO_MODES)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("prepare", help="a version of a video that starts at once / that every browser plays")
    s.add_argument("source")
    s.add_argument("-o", "--output", help="default: NAME.stream.mp4 next to the source")
    s.add_argument("--transcode", action="store_true", help="re-encode to H.264 with AAC sound (slow; without it nothing is re-encoded)")
    s.add_argument("--height", type=int, default=0, help="with --transcode: scale down to this height")
    s.add_argument("--bitrate", type=float, default=0.0, metavar="MBIT", help="with --transcode: megabits a second")
    s.add_argument("--print-ffmpeg", action="store_true", help="only print the ffmpeg command that does the same")
    s.add_argument("--force", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("test-video", help="make a video that shows whether screens are in step")
    s.add_argument("-o", "--output", required=True, help="NAME.mp4 (H.264) or NAME.webm (VP8)")
    s.add_argument("--seconds", type=float, default=60.0)
    s.add_argument("--size", default="1280x720")
    s.add_argument("--fps", type=int, default=30)
    s.add_argument("--projection", choices=("flat", "360"), default="flat")
    s.add_argument("--codec", choices=("h264", "vp8", "vp9"),
                   help=".mp4: h264 (default; with AAC sound) or vp9; .webm: vp8 (default) or vp9 - both with Opus sound")
    s.add_argument("--force", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_test_video)

    s = sub.add_parser("probe", help="simulated viewers: does the network carry them?")
    s.add_argument("url", help="a video's address: http://SERVER:8080/media/NAME")
    s.add_argument("--viewers", type=int, default=50)
    s.add_argument("--seconds", type=float, default=20.0)
    s.add_argument("--bitrate", type=float, default=0.0, metavar="MBIT", help="the video's (default: asked from the server)")
    s.add_argument("--flat-out", action="store_true", help="as fast as it comes: what the path carries at most")
    s.add_argument("--insecure", action="store_true", help="https with a certificate of your own")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("control", help="the operator's commands for a running server")
    s.add_argument("action", choices=("play", "pause", "resume", "seek", "stop", "loop", "load", "status"))
    s.add_argument("video", nargs="?", help="for play / load: the video's name; for loop: on / off")
    s.add_argument("--position", type=float, metavar="S", help="for play and seek")
    s.add_argument("--loop", action="store_true", help="with play: again and again")
    s.add_argument("--url", default="http://127.0.0.1:8080")
    s.add_argument("--key", help="the operator's key (or FOURDVR_STREAM_KEY)")
    s.add_argument("--insecure", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_control)

    s = sub.add_parser("cert", help="make a certificate of your own for https")
    s.add_argument("-o", "--output", default=".", help="folder for stream.crt and stream.key")
    s.add_argument("names", nargs="*", help="addresses and names of this server (default: its own)")
    s.add_argument("--print", action="store_true", help="only print the openssl command")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_cert)
    return p


def main(argv: list[str] | None = None, prog: str = "fourdvr stream") -> int:
    args = build_parser(prog).parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        code = int(args.func(args))
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        return reader_gone()
    except (LibraryError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
