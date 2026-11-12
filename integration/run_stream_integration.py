#!/usr/bin/env python3
"""End-to-end test of the stream server - a real server process, fifty simulated viewers, real browsers.

    python3 integration/run_stream_integration.py [--fourdvr /path/to/fourdvr] [--server /path/to/fourdvr-server]
                                                 [--viewers 50] [--no-browser] [--keep]

What runs:

    the stream server   fourdvr stream serve FOLDER       (test videos made with 'fourdvr stream test-video')
    fifty viewers       fourdvr stream probe              first as fast as it comes, then at the video's pace
    four browsers       headless Chromium (Playwright)    the viewer's page: one joined by a click, three by themselves
    the operator        the server's control address, and the operator's page in a fifth browser

Checked: the pages and byte ranges; fifty viewers get their video and the server still answers at once; four
browsers start together and stay in step (each compares what it shows with the server's timeline); pause puts
all of them on the same frame, a jump takes all of them along, a viewer who comes late is in step within seconds;
a 360-degree video is drawn, can be looked around in and turns round at its end without losing step; the
operator's page works; a viewer can watch a video alone while the show runs.

With a Control Server (--server): a programme is published there and played by the theatre's show - a show
service with a simulated platform - while a second stream server follows it (--follow): browsers on that stream
show the programme at the position the Control Server's timeline has, through PLAY, PAUSE, RESUME and STOP.

This script needs nothing of the product but its command (--fourdvr); it runs with any Python 3.10+. The browser
part needs Playwright for Python with its Chromium (pip install playwright; playwright install chromium);
--no-browser leaves it out. H.264 is not in that Chromium, so the test videos use the open codecs VP8 and VP9.
What a real phone, tablet, television or headset makes of the page is not part of this test.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ADMIN_PASSWORD = "Integration-Test-Passw0rd"
VIEWER_PASSWORD = "Only-Looking-12345"
KEY = "integration-key"
VIDEO = "document.getElementById('video')"
POINTS = "[[0.5,0.5],[0.2,0.5],[0.8,0.5],[0.5,0.2],[0.5,0.8],[0.3,0.3],[0.7,0.7],[0.1,0.9]]"


def log(text: str) -> None:
    print(text, flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Http:
    def __init__(self, base: str) -> None:
        self.base, self.token = base, ""

    def call(self, method: str, path: str, body: Any = None, raw: bytes | None = None, headers: dict[str, str] | None = None,
             timeout: float = 60.0) -> Any:
        data, head = None, dict(headers or {})
        if self.token:
            head["Authorization"] = f"Bearer {self.token}"
        if raw is not None:
            data, head["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, head["Content-Type"] = json.dumps(body).encode(), "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read()
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
        return json.loads(text) if "json" in kind and text else text


def wait_for(cond: Any, timeout: float, what: str) -> Any:
    end = time.time() + timeout
    last: Exception | None = None
    while time.time() < end:
        try:
            value = cond()
            if value:
                return value
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            last = exc
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {what}" + (f" ({last})" if last else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fourdvr", default=shutil.which("fourdvr") or str(Path(sys.executable).with_name("fourdvr")),
                    help="the fourdvr command to test (an installation's, or the AppImage's AppRun)")
    ap.add_argument("--server", default="", help="Control Server command: adds the part that follows the theatre's show")
    ap.add_argument("--viewers", type=int, default=50)
    ap.add_argument("--no-browser", action="store_true", help="without the browser part")
    ap.add_argument("--keep", action="store_true", help="keep the working folder (videos, logs, screenshots)")
    ap.add_argument("--shots", help="folder to copy the screenshots of the pages to")
    args = ap.parse_args()
    sync_playwright: Any = None
    if not args.no_browser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log("error: the browser part needs Playwright for Python (pip install playwright; playwright install chromium) - "
                "or run with --no-browser")
            return 2
    if args.server and shutil.which(args.server) is None and not Path(args.server).is_file():
        log(f"error: Control Server command '{args.server}' not found")
        return 2
    work = Path(tempfile.mkdtemp(prefix="fourdvr-stream-it-"))
    videos = work / "videos"
    videos.mkdir()
    results: list[tuple[str, bool, str]] = []
    procs: list[subprocess.Popen[Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        log(f"   {'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def tool(*argv: str, timeout: float = 300.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([args.fourdvr, *argv], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})

    def start(name: str, *argv: str, env: dict[str, str] | None = None) -> subprocess.Popen[Any]:
        proc = subprocess.Popen(list(argv), stdout=(work / f"{name}.log").open("wb"), stderr=subprocess.STDOUT,
                                env={**os.environ, **(env or {})})
        procs.append(proc)
        return proc

    browser: Any = None
    playwright: Any = None
    try:
        log(f"working folder {work}")
        log("1/5 test videos, and what the program says about them")
        made = [tool("stream", "test-video", "-o", str(videos / "show.mp4"), "--codec", "vp9", "--size", "640x360", "--seconds", "40", "--quiet"),
                tool("stream", "test-video", "-o", str(videos / "grid_360.webm"), "--size", "1280x640", "--seconds", "20",
                     "--projection", "360", "--quiet"),
                tool("stream", "test-video", "-o", str(videos / "clip.mp4"), "--size", "320x180", "--seconds", "5", "--quiet")]
        check("'fourdvr stream test-video' makes a VP9 MP4, a 360-degree VP8 WebM and an H.264 MP4",
              all(m.returncode == 0 for m in made) and all((videos / n).stat().st_size > 20000 for n in ("show.mp4", "grid_360.webm", "clip.mp4")),
              "; ".join(m.stderr.strip()[-120:] for m in made if m.returncode != 0))
        with (videos / "big.mkv").open("wb") as fh:           # a large file to push through the network path (its content does not matter)
            fh.truncate(1024 * 1024 * 1024)
        listed = tool("stream", "check", str(videos), "--json")
        info = {v["id"]: v for v in json.loads(listed.stdout)} if listed.returncode == 0 else {}
        check("'fourdvr stream check' reads them without decoding: codecs, length, kind, and which of them browsers play",
              bool(info) and (info["show.mp4"]["video_codec"], info["show.mp4"]["audio_codec"]) == ("vp9", "opus")
              and info["show.mp4"]["browsers"] == "some" and abs(info["show.mp4"]["duration_s"] - 40.0) < 0.2
              and info["clip.mp4"]["browsers"] == "yes" and info["clip.mp4"]["faststart"] is True and info["clip.mp4"]["video_codec"] == "h264"
              and info["grid_360.webm"]["projection"] == "360" and info["big.mkv"]["browsers"] == "no",
              ", ".join(f"{k}: {v['video_codec'] or '?'} {v['projection']} browsers {v['browsers']}" for k, v in info.items()))

        log("2/5 the stream server")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        start("stream", args.fourdvr, "stream", "serve", str(videos), "--bind", "127.0.0.1", "--port", str(port), "--key", KEY,
              "--max-viewers", "60", "--title", "Integration test")
        http = Http(base)
        wait_for(lambda: http.call("GET", "/api/state", timeout=2), 30, "the stream server")
        page = http.call("GET", "/")
        library = http.call("GET", "/api/library")
        whole = (videos / "show.mp4").read_bytes()
        part = http.call("GET", "/media/show.mp4", headers={"Range": "bytes=5000-104999"})
        refused = ""
        try:
            http.call("POST", "/api/control", {"action": "stop"})
        except RuntimeError as exc:
            refused = str(exc)
        check("it serves the viewer's page, the list of videos and any part of a file; the operator's commands need the key",
              b"<video" in page and [v["id"] for v in library["videos"]] == ["big.mkv", "clip.mp4", "grid_360.webm", "show.mp4"]
              and part == whole[5000:105000] and "403" in refused, f"{len(page)} bytes of page, {len(library['videos'])} videos")

        def control(**body: Any) -> dict[str, Any]:
            return dict(http.call("POST", f"/api/control?key={KEY}", body))

        def status() -> dict[str, Any]:
            return dict(http.call("GET", f"/api/status?key={KEY}"))

        log(f"3/5 {args.viewers} viewers at once")
        answer_times: list[float] = []
        stop_asking = threading.Event()

        def ask() -> None:
            while not stop_asking.wait(0.2):
                t0 = time.monotonic()
                try:
                    http.call("GET", "/api/time", timeout=5)
                    answer_times.append(time.monotonic() - t0)
                except (RuntimeError, OSError):
                    answer_times.append(9.0)

        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        flat = tool("stream", "probe", f"{base}/media/big.mkv", "--viewers", str(args.viewers), "--seconds", "6", "--flat-out", "--json")
        stop_asking.set()
        asker.join(6.0)
        got = json.loads(flat.stdout[flat.stdout.index("{"):]) if "{" in flat.stdout else {}
        check(f"{args.viewers} simulated viewers take a file as fast as it comes: every one is served",
              flat.returncode == 0 and got.get("answered") == args.viewers and got.get("total_bits_per_second", 0) > 200e6,
              f"{got.get('total_bits_per_second', 0) / 1e9:.1f} Gbit/s together on this machine's loopback, the slowest "
              f"{got.get('slowest_bits_per_second', 0) / 1e6:.0f} Mbit/s" if got else flat.stderr.strip()[-200:])
        check("meanwhile the server answers everybody else at once", bool(answer_times) and max(answer_times) < 0.5,
              f"clock requests answered in at most {max(answer_times, default=9.0) * 1000:.0f} ms ({len(answer_times)} asked)")
        paced = tool("stream", "probe", f"{base}/media/show.mp4", "--viewers", str(args.viewers), "--seconds", "6", "--json")
        got = json.loads(paced.stdout[paced.stdout.index("{"):]) if "{" in paced.stdout else {}
        check(f"{args.viewers} viewers of the show's video at its own pace: none would have stalled",
              paced.returncode == 0 and got.get("answered") == args.viewers and got.get("would_have_stalled") == 0,
              f"{got.get('bitrate', 0) / 1e6:.2f} Mbit/s video, {got.get('total_bits_per_second', 0) / 1e6:.0f} Mbit/s together" if got else
              paced.stderr.strip()[-200:])

        if args.no_browser:
            log("4/5 skipped: --no-browser")
        else:
            log("4/5 four browsers on one clock (headless Chromium)")
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch()
            problems: list[str] = []

            def open_page(url: str, width: int = 800, height: int = 450) -> Any:
                tab = browser.new_context(viewport={"width": width, "height": height}).new_page()
                tab.on("pageerror", lambda exc: problems.append(f"{url}: {exc}"))
                tab.on("console", lambda m: problems.append(f"{url}: {m.text}") if m.type == "error" and "favicon" not in m.text else None)
                tab.goto(url)
                return tab

            def look(tab: Any) -> dict[str, Any]:
                return dict(tab.evaluate(
                    f"({{drift: stream.drift(), t: {VIDEO}.currentTime, paused: {VIDEO}.paused, state: (stream.state() || {{}}).state, "
                    f"video: stream.current() ? stream.current().id : null, waiting: !document.getElementById('wait').classList.contains('hidden'), "
                    f"canvas: document.getElementById('gl').classList.contains('on'), muted: {VIDEO}.muted, stalls: stream.stalls()}})"))

            def in_step(tabs: list[Any], limit: float, rounds: int = 3) -> tuple[bool, str]:
                worst = 0.0
                for _ in range(rounds):
                    for tab in tabs:
                        seen = look(tab)
                        if seen["paused"] or seen["drift"] is None or seen["state"] != "playing":
                            return False, f"a page is not playing: {seen}"
                        worst = max(worst, abs(seen["drift"]))
                    time.sleep(1.0)
                return worst <= limit, f"furthest off {worst * 1000:.0f} ms"

            tabs = [open_page(base + "/")] + [open_page(base + "/?autojoin&muted") for _ in range(3)]
            tabs[0].click("#joinButton")              # a person taps "Join": this page may play with sound
            time.sleep(1.0)
            tl = control(action="play", video="show.mp4")
            lead = tl["start_at"] - tl["server_time"]
            time.sleep(1.0)
            before = [look(tab) for tab in tabs]
            check("play is announced three seconds ahead: every page has the video and stands at its first frame",
                  2.5 < lead <= 3.01 and all(b["video"] == "show.mp4" and b["paused"] and b["t"] < 0.01 and b["waiting"] for b in before),
                  f"lead {lead:.2f} s")
            time.sleep(lead - 1.0 + 5.0)
            ok, detail = in_step(tabs, 0.08)
            check("five seconds into the show all four play, each within 80 ms of the server's timeline", ok, detail)
            check("the page a person joined plays with sound, the ones that started by themselves without",
                  look(tabs[0])["muted"] is False and all(look(tab)["muted"] for tab in tabs[1:]))
            time.sleep(1.3)
            st = status()
            check("the server knows its viewers and how each is doing",
                  st["watching"] == 4 and st["in_step"] == 4 and st["worst_drift_ms"] is not None and st["worst_drift_ms"] <= 80.0
                  and st["stalled"] == 0, f"{st['watching']} watching, {st['in_step']} in step, furthest off {st['worst_drift_ms']} ms")
            tl = control(action="pause")
            time.sleep(1.5)
            seen = [look(tab) for tab in tabs]
            places = ", ".join(f"{s['t']:.3f}" for s in seen)
            check("pause: all four stand on the same frame", all(s["paused"] and abs(s["t"] - tl["position_s"]) < 0.006 for s in seen),
                  f"paused at {tl['position_s']:.3f} s; pages at {places}")
            control(action="seek", position_s=25.0)
            time.sleep(1.5)
            seen = [look(tab) for tab in tabs]
            check("a jump while paused takes all of them along", all(s["paused"] and abs(s["t"] - 25.0) < 0.006 for s in seen),
                  ", ".join(f"{s['t']:.3f}" for s in seen))
            late = open_page(base + "/?autojoin&muted")           # somebody comes in while the show goes on
            control(action="resume")
            time.sleep(1.5 + 4.5)
            ok, detail = in_step([*tabs, late], 0.08)
            check("resume: in step again - and so is a viewer who came in late", ok and 29.0 < look(late)["t"] < 37.0, detail)
            control(action="seek", position_s=5.0)
            time.sleep(1.5 + 4.5)
            ok, detail = in_step([*tabs, late], 0.08)
            check("a jump while playing: everybody is taken along and in step again", ok and 8.0 < look(tabs[0])["t"] < 16.0, detail)
            late.close()

            log("     a 360-degree video, played again and again")
            control(action="play", video="grid_360.webm", position_s=15.0, loop=True)
            time.sleep(3.0 + 3.0)
            seen = [look(tab) for tab in tabs]
            check("the 360-degree video is drawn on a canvas of its own", all(s["canvas"] and s["video"] == "grid_360.webm" for s in seen))
            time.sleep(4.0)                                         # across the end of its 20 seconds
            ok, detail = in_step(tabs, 0.10)
            turned = [look(tab)["t"] for tab in tabs]
            check("it turns round at its end and the pages stay in step", ok and all(0.5 < t < 9.0 for t in turned),
                  f"{detail}; positions {', '.join(f'{t:.2f}' for t in turned)}")
            ahead = tabs[0].evaluate(f"stream.pixels({POINTS})")
            tabs[0].mouse.move(400, 225)
            tabs[0].mouse.down()
            tabs[0].mouse.move(150, 225, steps=8)                   # the picture is pushed to the left: the view turns right
            tabs[0].mouse.up()
            yaw = tabs[0].evaluate("stream.view.yaw")
            aside = tabs[0].evaluate(f"stream.pixels({POINTS})")
            spread = max(max(p) for p in ahead) - min(min(p) for p in ahead)
            check("there is a picture, and dragging it looks around", spread > 60 and 0.3 < yaw < 1.6 and ahead != aside
                  and tabs[1].evaluate("stream.view.yaw") == 0, f"turned {yaw * 57.2958:.0f} degrees to the right")
            maths = tabs[0].evaluate(
                "[stream.orientationMatrix(0, 90, 0, 0), stream.orientationMatrix(90, 90, 0, 0), stream.orientationMatrix(0, 0, 0, 0),"
                " stream.frustumOf([1,0,0,0, 0,1,0,0, 0,0,-1,-1, 0,0,-0.2,0]), [stream.yawOf(stream.rotation(0.5, 0.2))],"
                " stream.eyeRect('ou', 'left'), stream.eyeRect('ou', 'right'), stream.eyeRect('sbs', 'right')]")
            upright, turned_left, lying, frustum, yaw_back, top, bottom, right_half = maths

            def near(a: list[float], b: list[float]) -> bool:
                return len(a) == len(b) and all(abs(x - y) < 1e-9 for x, y in zip(a, b, strict=True))

            check("a phone held upright looks ahead, turned to the left it looks left, lying flat it looks down; an eye's "
                  "view is taken from its projection; each eye gets its half of a stereo frame",
                  near(upright, [1, 0, 0, 0, 1, 0, 0, 0, 1]) and near(turned_left, [0, 0, -1, 0, 1, 0, 1, 0, 0])
                  and near(lying, [1, 0, 0, 0, 0, -1, 0, 1, 0]) and near(frustum, [-1, 1, -1, 1]) and near(yaw_back, [0.5])
                  and top == [0, 0.5, 1, 0.5] and bottom == [0, 0, 1, 0.5] and right_half == [0.5, 0, 0.5, 1])

            log("     the operator's page, and a viewer who watches alone")
            desk = open_page(f"{base}/control?key={KEY}", 1100, 900)
            time.sleep(1.6)
            watching = desk.inner_text("#watching")
            desk.click("tr:has-text('show') >> button.go")          # "Play" in the row of the show's video
            time.sleep(1.0)
            tl = http.call("GET", "/api/state")
            shown_state = desk.inner_text("#state")
            check("the operator's page shows the viewers; its Play button starts a video for all of them",
                  watching.startswith("4") and tl["video"] == "show.mp4" and tl["state"] == "playing"
                  and ("playing" in shown_state or "starting" in shown_state), f"'{watching}' watching; state '{shown_state}'")
            alone = open_page(base + "/library")
            time.sleep(0.6)
            alone.screenshot(path=str(work / "stream-library.png"))
            alone.click(".item:has-text('grid 360')")
            time.sleep(4.0)
            a = look(alone)
            banner = alone.evaluate("!document.getElementById('banner').classList.contains('hidden')")
            check("a viewer picks another video and watches it alone while the show runs (and is told that it runs)",
                  not a["paused"] and a["t"] > 1.5 and a["video"] == "grid_360.webm" and a["canvas"] and banner, f"at {a['t']:.1f} s")
            time.sleep(2.5)
            ok, detail = in_step(tabs, 0.08)
            check("... and the show's viewers are not disturbed by it", ok, detail)
            desk.screenshot(path=str(work / "stream-operator.png"))
            tabs[1].mouse.move(300, 200)                            # (the bar at the bottom shows itself)
            tabs[1].screenshot(path=str(work / "stream-viewer.png"))
            control(action="play", video="grid_360.webm", position_s=3.0)
            time.sleep(3.0 + 2.5)
            tabs[0].mouse.move(400, 300)
            tabs[0].screenshot(path=str(work / "stream-viewer-360.png"))
            control(action="stop")
            time.sleep(0.8)
            seen = [look(tab) for tab in tabs]
            check("stop: every page stops and waits for the show", all(s["paused"] and s["waiting"] for s in seen))
            check("no page reported an error, none of them stalled", not problems and all(s["stalls"] == 0 for s in seen),
                  "; ".join(problems)[:300])
            for tab in [*tabs, desk, alone]:
                tab.close()

        if not args.server:
            log("5/5 skipped: no Control Server command (--server)")
        else:
            log("5/5 the theatre's show, followed by a second stream server")
            sim_profile = Path(__file__).resolve().parents[1] / "config" / "platforms" / "simulation-6dof.toml"
            motion = work / "show.motion"
            made_motion = tool("extract", "generate", str(videos / "show.mp4"), "-o", str(motion), "--projection", "flat", "--stereo", "mono",
                               "--profile", str(sim_profile), "--programme-id", "3", "-q")
            cs_port = free_port()
            cfg = work / "server.toml"
            cfg.write_text(
                f'[server]\nhost = "127.0.0.1"\nport = {cs_port}\ndata_dir = "{work / "server-data"}"\nlog_level = "WARNING"\n'
                f'log_format = "text"\n[security]\ninitial_admin_password = "{ADMIN_PASSWORD}"\n[devices]\nheartbeat_interval_s = 1.0\n'
                "offline_timeout_s = 5.0\n[playback]\nstart_lead_s = 3.0\ntransition_gap_s = 1.5\nprepare_timeout_s = 60.0\n")
            subprocess.run([args.server, "-c", str(cfg), "init"], capture_output=True, text=True, check=True, timeout=120)
            start("control-server", args.server, "-c", str(cfg), "serve")
            cs = Http(f"http://127.0.0.1:{cs_port}")
            health = wait_for(lambda: cs.call("GET", "/api/v1/health", timeout=2), 30, "the Control Server")
            cs.token = cs.call("POST", "/api/v1/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]
            cs.call("POST", "/api/v1/users", {"username": "streamviewer", "password": VIEWER_PASSWORD, "role": "viewer"})
            cs.call("POST", "/api/v1/programmes", {"programme_id": 3, "name": "The test show", "projection": "flat", "stereo": "mono", "loop": True})
            cs.call("PUT", "/api/v1/programmes/3/video?filename=show.mp4", raw=(videos / "show.mp4").read_bytes())
            cs.call("PUT", "/api/v1/programmes/3/motion?filename=show.motion", raw=motion.read_bytes() if motion.exists() else b"")
            verdict = cs.call("POST", "/api/v1/programmes/3/validate")
            if verdict["passed"]:
                cs.call("POST", "/api/v1/programmes/3/publish")
            check(f"{health['product']} {health['version']} accepts and publishes a programme with the test video",
                  made_motion.returncode == 0 and bool(verdict["passed"]),
                  f"{len(verdict['checks'])} checks" + "".join(f"; failed: {c['check']}" for c in verdict["checks"] if c["status"] == "fail"))
            keys = subprocess.run([args.server, "-c", str(cfg), "device-key", "PLATFORM-01"], capture_output=True, text=True, check=True)
            device_key = keys.stdout.strip().split("\t")[1]
            (work / "controller.toml").write_text(
                f'[controller]\nserver_url = "ws://127.0.0.1:{cs_port}/ws/v1/device"\ncontrol_rate_hz = 100.0\ncache_dir = "{work / "cache"}"\n'
                f'status_interval_s = 0.5\ntime_sync_interval_s = 2.0\nlog_level = "INFO"\n\n[[platforms]]\nconfig = "{sim_profile}"\n'
                f'id = "PLATFORM-01"\nlink_platform_id = 1\ndevice_key = "{device_key}"\n')
            start("show-service", args.fourdvr, "controller", "run", "-c", str(work / "controller.toml"))
            wait_for(lambda: [p for p in cs.call("GET", "/api/v1/devices", timeout=5)["motion_platforms"] if p["state"] == "READY"], 60,
                     "the simulated platform READY")
            (work / "password.txt").write_text(VIEWER_PASSWORD + "\n")
            port2 = free_port()
            base2 = f"http://127.0.0.1:{port2}"
            start("stream-follow", args.fourdvr, "stream", "serve", str(videos), "--bind", "127.0.0.1", "--port", str(port2), "--key", KEY,
                  "--follow", f"http://127.0.0.1:{cs_port}", "--user", "streamviewer", "--password-file", str(work / "password.txt"))
            follow = Http(base2)
            tl = wait_for(lambda: (lambda s: s if s["source"] == "theatre" else None)(follow.call("GET", "/api/state", timeout=2)), 30,
                          "the stream server following the theatre")
            check("a stream server signs in at the Control Server with a viewer's account and follows its show",
                  tl["source"] == "theatre" and tl["state"] == "stopped")
            pages: list[Any] = []
            if browser is not None:
                pages = [browser.new_context(viewport={"width": 800, "height": 450}).new_page() for _ in range(2)]
                for tab in pages:
                    tab.goto(base2 + "/?autojoin&muted")
                time.sleep(1.0)

            def both() -> tuple[dict[str, Any], dict[str, Any], float]:
                """The theatre's timeline and the stream's, asked one right after the other."""
                theirs = cs.call("GET", "/api/v1/playback/state", timeout=5)
                ours = follow.call("GET", "/api/state", timeout=5)
                return theirs, ours, float(ours["position_s"]) - float(theirs["position_s"])

            answer = cs.call("POST", "/api/v1/playback/play", {"programme_id": 3})
            log(f"     PLAY 3: {answer['result']} - {answer['detail']}")
            wait_for(lambda: cs.call("GET", "/api/v1/playback/state", timeout=5)["timeline"]["state"] == "PLAYING", 60, "the show playing")
            time.sleep(3.0 + 5.0)
            theirs, ours, apart = both()
            check("PLAY at the Control Server: the stream plays the programme's video at the theatre's position",
                  theirs["timeline"]["state"] == "PLAYING" and ours["state"] == "playing" and ours["video"] == "show.mp4"
                  and theirs["position_s"] > 3.0 and abs(apart) < 0.06 and ours["loop"] is True,
                  f"theatre at {theirs['position_s']:.2f} s, stream {apart * 1000:+.0f} ms from it")
            if pages:
                shown = []
                for tab in pages:
                    theirs = cs.call("GET", "/api/v1/playback/state", timeout=5)
                    shown.append(float(tab.evaluate(f"{VIDEO}.currentTime")) - float(theirs["position_s"]))
                drifts = [tab.evaluate("stream.drift()") for tab in pages]
                check("browsers on that stream show the programme where the theatre's timeline is",
                      all(abs(s) < 0.15 for s in shown) and all(d is not None and abs(d) < 0.08 for d in drifts),
                      "pages " + ", ".join(f"{s * 1000:+.0f} ms" for s in shown) + " from the Control Server's position (asked one "
                      "after the other); against the stream's timeline " + ", ".join(f"{d * 1000:+.0f} ms" for d in drifts if d is not None))
            refuse = tool("stream", "control", "stop", "--url", base2, "--key", KEY)
            check("the stream's own operator cannot interfere with the theatre's show",
                  refuse.returncode == 1 and "Control Server" in refuse.stderr, refuse.stderr.strip()[:120])
            cs.call("POST", "/api/v1/playback/pause", {})
            time.sleep(1.5)
            theirs, ours, apart = both()
            paused = [float(tab.evaluate(f"{VIDEO}.currentTime")) for tab in pages]
            places = ", ".join(f"{t:.3f}" for t in paused)
            check("PAUSE: the stream and its viewers stand where the theatre stands",
                  theirs["timeline"]["state"] == "PAUSED" and ours["state"] == "paused" and abs(apart) < 0.002
                  and all(abs(t - ours["position_s"]) < 0.006 for t in paused),
                  f"at {ours['position_s']:.3f} s" + (f"; pages at {places}" if paused else ""))
            cs.call("POST", "/api/v1/playback/resume", {})
            time.sleep(3.0 + 4.0)
            theirs, ours, apart = both()
            drifts = [tab.evaluate("stream.drift()") for tab in pages]
            check("RESUME: on again, together", theirs["timeline"]["state"] == "PLAYING" and ours["state"] == "playing" and abs(apart) < 0.06
                  and all(d is not None and abs(d) < 0.08 for d in drifts), f"stream {apart * 1000:+.0f} ms from the theatre")
            cs.call("POST", "/api/v1/playback/stop", {})
            time.sleep(1.5)
            theirs, ours, _apart = both()
            check("STOP: the stream stops, its viewers wait",
                  ours["state"] == "stopped" and all(tab.evaluate(f"{VIDEO}.paused") for tab in pages))
            service_log = (work / "show-service.log").read_text(errors="replace")
            check("the show service played it on its simulated platform, without an error in its log",
                  "ERROR" not in service_log and "Traceback" not in service_log)
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if args.shots:
            Path(args.shots).mkdir(parents=True, exist_ok=True)
            for shot in work.glob("stream-*.png"):
                shutil.copy(shot, Path(args.shots) / shot.name)
    stream_log = (work / "stream.log").read_text(errors="replace") if (work / "stream.log").exists() else ""
    check("the stream server's log has no error", "Traceback" not in stream_log and "ERROR" not in stream_log)
    failed = [r for r in results if not r[1]]
    log(f"\n{len(results) - len(failed)} of {len(results)} checks passed" + ("" if not failed else f"; FAILED: {'; '.join(r[0] for r in failed)}"))
    if args.keep or failed:
        log(f"working folder kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if results and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
