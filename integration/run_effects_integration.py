#!/usr/bin/env python3
"""Integration test of the complete effects chain on one PC, without theatre hardware:

    Editor export (motion + effects in ONE .motion file)
      -> the Control Server (validates, publishes, distributes, commands PLAY / PAUSE / RESUME / STOP)
      -> the show service  (fourdvr controller run, two simulated platforms, a venue patch)
      -> real sACN (E1.31) packets on the loopback interface
      -> a receiver that compares every DMX frame with what the programme prescribes.

It also plays a motion-only programme through the same service and checks that no effect is output.

Needs: this package (run the script with its Python), the 4D-VR-Theatre-Control-Server command
(`fourdvr-server`, or --server /path/to/fourdvr-server) and, for the generated test programme, the
`av` package. With --video/--motion an existing programme (exported from the Editor with effects)
is used instead of the generated one.

    python integration/run_effects_integration.py
    python integration/run_effects_integration.py --video ride.mp4 --motion ride.motion --seconds 90
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from fourdvr_fx import Cue, EffectsDocument
from fourdvr_fx.dmx import DmxReceiver
from fourdvr_fx.embed import embed_into_motion, read_effects
from fourdvr_fx.engine import THEATRE, EffectsEngine, ShowState
from fourdvr_fx.patch import load_patch
from fourdvr_motion import motionfile

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "Integration-Test-Passw0rd"
PLATFORMS = ["PLATFORM-01", "PLATFORM-02"]
FRAME_S = 0.025


def log(text: str) -> None:
    print(text, flush=True)


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Api:
    def __init__(self, base: str) -> None:
        self.base, self.token = base, ""

    def call(self, method: str, path: str, body: Any = None, raw: bytes | None = None, timeout: float = 120) -> Any:
        data, headers = None, {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:500]}") from exc
        return json.loads(text) if text else None


def wait_for(cond: Any, timeout: float, what: str) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = cond()
            if value:
                return value
        except (RuntimeError, OSError, KeyError):
            pass
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {what}")


def make_video(path: Path, seconds: float) -> None:
    """A 360 pan with a picture flash, and a sound track with bass bursts (so that every suggestion has something to find)."""
    import av
    import cv2

    rng = np.random.default_rng(3)
    w, h, fps, rate = 640, 320, 30, 48000
    tex = cv2.resize(rng.random((h // 4, w // 4)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    tex = cv2.cvtColor(cv2.normalize(tex, None, 40, 150, cv2.NORM_MINMAX).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    with av.open(str(path), "w") as out:
        vs = out.add_stream("mpeg4", rate=fps)
        vs.width, vs.height, vs.pix_fmt, vs.bit_rate = w, h, "yuv420p", 2_500_000
        a_s = out.add_stream("aac", rate=rate, layout="stereo")
        for i in range(int(seconds * fps)):
            t = i / fps
            shift = (25.0 * t + 6.0 * np.sin(t)) / 360.0 * w
            img = cv2.warpAffine(tex, np.float32([[1, 0, shift], [0, 1, 0]]), (w, h), borderMode=cv2.BORDER_WRAP)
            if 12.0 <= t < 12.0 + 2.0 / fps:
                img = cv2.add(img, np.full_like(img, 110))
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            frame.pts, frame.time_base = i, Fraction(1, fps)
            for packet in vs.encode(frame):
                out.mux(packet)
        for packet in vs.encode(None):
            out.mux(packet)
        ts = np.arange(int(seconds * rate)) / rate
        rumble = 0.5 * np.sin(2 * np.pi * 55 * ts) * (((ts % 10.0) > 3.0) & ((ts % 10.0) < 6.0))
        sound = (rumble + 0.04 * np.sin(2 * np.pi * 900 * ts)).astype(np.float32)
        for start in range(0, len(sound), 1024):
            chunk = np.ascontiguousarray(np.stack([sound[start : start + 1024]] * 2))
            frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
            frame.sample_rate, frame.pts, frame.time_base = rate, start, Fraction(1, rate)
            for packet in a_s.encode(frame):
                out.mux(packet)
        for packet in a_s.encode(None):
            out.mux(packet)


def add_test_cues(doc: EffectsDocument) -> None:
    """One of everything a venue patch can limit or delay, at known places."""
    d = doc.duration_s
    wind = doc.track("wind") or doc.new_track("wind")
    wind.points = [(0.0, 0.0), (2.0, 0.0), (5.0, 0.8), (15.0, 0.3), (22.0, 1.0), (d - 4.0, 1.0), (d - 2.0, 0.0), (d, 0.0)]
    (doc.track("water") or doc.new_track("water")).cues = [Cue(6.0, 0.4), Cue(17.0, 3.0)]            # venue: 2 s at most
    (doc.track("air") or doc.new_track("air")).cues = [Cue(4.0, 0.25), Cue(4.6, 0.25), Cue(20.0, 0.25)]
    (doc.track("fog") or doc.new_track("fog")).cues = [Cue(8.0, 6.0, fade_in=0.5, fade_out=1.0)]     # venue: 4 s at most
    (doc.track("scent") or doc.new_track("scent")).cues = [Cue(10.0, 2.0, params={"slot": 1.0}), Cue(19.0, 2.0, params={"slot": 2.0})]
    (doc.track("strobe") or doc.new_track("strobe")).cues = [Cue(12.0, 0.15), Cue(15.0, 2.0, params={"rate_hz": 12.0})]   # venue: 3 flashes/s


def simulate(doc: EffectsDocument, patch_path: Path, seconds: float) -> dict[int, np.ndarray]:
    """The DMX the programme prescribes at this venue, frame by frame from position 0 (same engine, simulated clock)."""
    now = [0.0]
    engine = EffectsEngine(load_patch(patch_path), PLATFORMS, lambda: now[0])
    state = ShowState(True, doc.compile(), 0.0, doc.duration_s, True, 1)
    shows = {THEATRE: state, **{p: state for p in PLATFORMS}}
    frames: dict[int, list[bytes]] = {}
    for k in range(int(seconds / FRAME_S) + 1):
        now[0] = state.position_s = k * FRAME_S
        for universe, data in engine.render(shows, now[0]).items():
            frames.setdefault(universe, []).append(data)
    return {u: np.array([np.frombuffer(f, dtype=np.uint8) for f in rows]) for u, rows in frames.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="fourdvr-server", help="Control Server command")
    ap.add_argument("--fourdvr", default=str(Path(sys.executable).with_name("fourdvr")), help="this product's command")
    ap.add_argument("--video", help="use this video (with --motion) instead of the generated test programme")
    ap.add_argument("--motion", help="Editor export with effects that belongs to --video")
    ap.add_argument("--projection", default="360", choices=("flat", "180", "360"), help="of --video (server metadata)")
    ap.add_argument("--stereo", default="mono", choices=("mono", "sbs", "ou"))
    ap.add_argument("--seconds", type=float, default=24.0, help="how long programme 1 is played and compared")
    ap.add_argument("--work", help="working directory (default: a temporary one, removed afterwards)")
    ap.add_argument("--report", help="write the result as JSON")
    args = ap.parse_args()
    if shutil.which(args.server) is None and not Path(args.server).is_file():
        log(f"error: Control Server command '{args.server}' not found (use --server)")
        return 2
    if bool(args.video) != bool(args.motion):
        log("error: give --video and --motion together")
        return 2
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="fourdvr-fx-it-"))
    shutil.rmtree(work, ignore_errors=True)
    (work / "logs").mkdir(parents=True)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"ok": bool(ok), "check": name, "detail": detail})
        log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def tool(*cmd: str) -> str:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} ... failed:\n{res.stdout[-1500:]}{res.stderr[-1500:]}")
        return res.stdout

    procs: list[subprocess.Popen[bytes]] = []
    rx: DmxReceiver | None = None
    try:
        log("1/6 programme")
        sim_profile = ROOT / "config" / "platforms" / "simulation-6dof.toml"
        if args.video:
            video, motion = Path(args.video), work / "programme1.motion"
            shutil.copy(args.motion, motion)
            projection, stereo = args.projection, args.stereo
        else:
            video, motion = work / "test-ride.mp4", work / "programme1.motion"
            projection, stereo = "360", "mono"
            make_video(video, 36.0)
            tool(args.fourdvr, "extract", "generate", str(video), "-o", str(motion), "--projection", "360", "--profile", str(sim_profile),
                 "--effects", "suggest", "--bass", "-q")
            suggested = read_effects(motion)
            assert suggested is not None
            check("suggestions from the test video: seat vibration from the bass, strobe at the picture flash, bass track",
                  {"vibration", "strobe"} <= {t.id for t in suggested.tracks} and suggested.bass is not None,
                  ", ".join(f"{t.id} ({len(t.cues)} cues, {len(t.points)} points)" for t in suggested.tracks))
            add_test_cues(suggested)
            embed_into_motion(motion, suggested)
        doc = read_effects(motion)
        if doc is None:
            log("error: the programme has no effects - nothing to test (export it from the Editor with effect tracks)")
            return 2
        mf = motionfile.read_motion(motion, verify=True)
        seconds = min(args.seconds, float(mf.duration_s) - 2.0)
        plain = work / "programme2.motion"
        motionfile.write_motion(plain, {**{k: v for k, v in mf.header.items() if k != "effects"}, "programme_id": 2}, mf.samples)
        log(tool(args.fourdvr, "fx", "info", str(motion)).rstrip())

        log("2/6 Control Server: validate, publish")
        port, dmx_port = free_port(), free_port(socket.SOCK_DGRAM)
        cfg = work / "server.toml"
        cfg.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\ndata_dir = "{work / "server-data"}"\nlog_level = "WARNING"\nlog_format = "text"\n'
            f'[security]\ninitial_admin_password = "{ADMIN_PASSWORD}"\n[devices]\nheartbeat_interval_s = 1.0\noffline_timeout_s = 5.0\n'
            "[playback]\nstart_lead_s = 3.0\ntransition_gap_s = 1.5\nprepare_timeout_s = 60.0\n"
        )
        tool(args.server, "-c", str(cfg), "init")
        procs.append(subprocess.Popen([args.server, "-c", str(cfg), "serve"], stdout=(work / "logs/server.log").open("wb"),
                                      stderr=subprocess.STDOUT))
        api = Api(f"http://127.0.0.1:{port}")
        health = wait_for(lambda: api.call("GET", "/api/v1/health", timeout=2), 30, "the Control Server")
        api.token = api.call("POST", "/api/v1/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]
        video_bytes = video.read_bytes()
        for pid, name, path in ((1, "Programme with effects", motion), (2, "Programme, motion only", plain)):
            mf_pid = motionfile.read_header(path)["programme_id"]
            api.call("POST", "/api/v1/programmes", {"programme_id": pid, "name": name, "projection": projection, "stereo": stereo, "loop": True})
            api.call("PUT", f"/api/v1/programmes/{pid}/video?filename={video.name}", raw=video_bytes)
            if mf_pid != pid:      # a user's file made for another programme number
                fixed = work / f"programme{pid}-renumbered.motion"
                m = motionfile.read_motion(path)
                motionfile.write_motion(fixed, {**m.header, "programme_id": pid}, m.samples)
                path = fixed
            api.call("PUT", f"/api/v1/programmes/{pid}/motion?filename={path.name}", raw=path.read_bytes())
            res = api.call("POST", f"/api/v1/programmes/{pid}/validate")
            failed = [c for c in res["checks"] if c["status"] == "fail"]
            check(f"{health['product']} {health['version']} accepts programme {pid} ({'with effects' if pid == 1 else 'motion only'}, {path.stat().st_size // 1024} KiB)",
                  bool(res["passed"]) and not failed, f"{len(res['checks'])} checks" + (f", failed: {failed}" if failed else ""))
            api.call("POST", f"/api/v1/programmes/{pid}/publish")

        log("3/6 show service with a venue patch (sACN to this PC)")
        keys = dict(line.split("\t") for line in tool(args.server, "-c", str(cfg), "device-key", *PLATFORMS).strip().splitlines())
        patch_text = (ROOT / "config" / "effects" / "patch.example.toml").read_text()
        patch_text = patch_text.replace("multicast = true ", "multicast = false").replace("unicast = [] ", f'unicast = ["127.0.0.1"]\nport = {dmx_port}\n#')
        patch_path = work / "venue.toml"
        patch_path.write_text(patch_text)
        sacn = load_patch(patch_path).output.sacn
        assert sacn["unicast"] == ["127.0.0.1"] and sacn["port"] == dmx_port and sacn["multicast"] is False
        ctl = work / "controller.toml"
        ctl.write_text(
            f'[controller]\nserver_url = "ws://127.0.0.1:{port}/ws/v1/device"\ncontrol_rate_hz = 100.0\ncache_dir = "{work / "cache"}"\n'
            'status_interval_s = 0.5\ntime_sync_interval_s = 2.0\nlog_level = "INFO"\n'
            + "".join(f'\n[[platforms]]\nconfig = "{sim_profile}"\nid = "{pid}"\nlink_platform_id = {k + 1}\ndevice_key = "{keys[pid]}"\n'
                      for k, pid in enumerate(PLATFORMS))
            + f'\n[effects]\npatch = "{patch_path}"\nreference_platform = "{PLATFORMS[0]}"\n'
        )
        listing = subprocess.run([args.fourdvr, "controller", "check-config", "-c", str(ctl)], capture_output=True, text=True, check=False)
        (work / "logs/check-config.txt").write_text(listing.stdout + listing.stderr)
        check("fourdvr controller check-config accepts the configuration", listing.returncode == 0 and "configuration OK" in listing.stdout)
        log(tool(args.fourdvr, "fx", "check", str(motion), "--patch", str(patch_path), "--platforms", ",".join(PLATFORMS)).rstrip())
        rx = DmxReceiver("sacn", [1, 2], bind_ip="127.0.0.1", port=dmx_port, multicast=False, history=600000).start()
        procs.append(subprocess.Popen([args.fourdvr, "controller", "run", "-c", str(ctl)], stdout=(work / "logs/controller.log").open("wb"),
                                      stderr=subprocess.STDOUT))

        def platforms() -> list[dict[str, Any]]:
            return [p for p in api.call("GET", "/api/v1/devices", timeout=5)["motion_platforms"] if p["connection"] == "CONNECTED"]

        wait_for(lambda: len([p for p in platforms() if p["state"] == "READY"]) == 2, 60, "two platforms READY")
        time.sleep(1.0)
        idle = rx.snapshot()
        check("before the show: DMX frames arrive and are dark", set(idle) >= {1, 2} and all(max(f.data) == 0 for f in idle.values()),
              f"{rx.packets} packets from '{idle[1].source if 1 in idle else '?'}'")
        best = (9.0, 0.0)
        for _ in range(15):          # this PC's clock against the server's master clock
            t0 = time.monotonic()
            master = api.call("GET", "/api/v1/time", timeout=2)["master_time"]
            t1 = time.monotonic()
            best = min(best, (t1 - t0, master - (t0 + t1) / 2))
        offset = best[1]

        def play(pid: int) -> dict[str, Any]:
            res = api.call("POST", "/api/v1/playback/play", {"programme_id": pid})
            log(f"    PLAY {pid}: {res['result']} - {res['detail']}")
            return wait_for(lambda: (lambda s: s if s["timeline"]["state"] == "PLAYING" and s["timeline"]["programme_id"] == pid
                                     and not s["transition_active"] else None)(api.call("GET", "/api/v1/playback/state", timeout=5)),
                            90, f"programme {pid} PLAYING")

        log(f"4/6 PLAY programme 1 and compare {seconds:.0f} s of DMX with the programme")
        show = play(1)
        start_at = float(show["timeline"]["start_at"])
        status: list[dict[str, Any]] = []
        while time.monotonic() < start_at - offset + seconds + 0.5:
            time.sleep(0.5)
            if not status and time.monotonic() > start_at - offset + 5.0:
                status = platforms()
        check("both platforms RUNNING programme 1", len(status) == 2 and all(p["state"] == "RUNNING" and p["programme_id"] == 1 for p in status))
        fx = (status[0].get("details") or {}).get("effects") if status else None
        check("the show service reports its effects state to the server", isinstance(fx, dict) and fx.get("playing") is True and fx.get("errors") == 0,
              json.dumps({k: fx.get(k) for k in ("running", "playing", "programme_has_effects", "output", "errors", "theatre_follows")}) if isinstance(fx, dict) else str(fx))
        history = list(rx.history)
        position = np.array([h[0] for h in history]) + offset - start_at
        universe = np.array([h[1].universe for h in history])
        expected = simulate(doc, patch_path, seconds)
        patch = load_patch(patch_path)
        check("dark until the timeline starts", all(max(h[1].data) == 0 for h, p in zip(history, position, strict=True) if p < -0.05))
        worst = 0.0
        for inst in sorted(patch.instances(PLATFORMS), key=lambda i: (i.universe, i.address)):
            sel = (universe == inst.universe) & (position >= 0.2) & (position <= seconds - 0.2)
            pos = position[sel]
            rows = [h[1].data for h, s in zip(history, sel, strict=True) if s]
            want_all = expected[inst.universe]
            bad = on_frames = 0
            for ch in inst.channels():
                got = np.array([row[ch - 1] for row in rows], dtype=int)
                idx = np.clip(np.round(pos / FRAME_S).astype(int), 2, len(want_all) - 3)
                # A frame is right if its value lies within what the programme prescribes during the
                # +-50 ms around it (the service's frames are not on the reference's 25 ms grid, so
                # on a fade the value falls between two reference values), give or take 3 DMX steps.
                near = np.stack([want_all[idx + d, ch - 1].astype(int) for d in (-2, -1, 0, 1, 2)])
                bad += int(np.sum((got < near.min(axis=0) - 3) | (got > near.max(axis=0) + 3)))
                on_frames += int(np.sum(got > 0))
            share = bad / max(1, len(rows) * len(inst.channels()))
            worst = max(worst, share)
            check(f"{inst.key} (U{inst.universe} ch {inst.address}): output as prescribed, within 50 ms", share <= 0.01,
                  f"{len(rows)} frames, {on_frames} lit, {bad} off the mark ({share * 100:.2f} %)")
        rate = float(np.sum((universe == 1) & (position > 2) & (position < seconds - 2))) / max(1e-9, seconds - 4)
        check("DMX refresh rate as configured", abs(rate - patch.output.refresh_hz) < 1.5, f"{rate:.2f} frames/s")

        log("5/6 PAUSE, RESUME, STOP")
        api.call("POST", "/api/v1/playback/pause")
        time.sleep(1.6)
        check("PAUSE: every effect off", all(max(f.data) == 0 for f in rx.snapshot().values()))
        api.call("POST", "/api/v1/playback/resume")
        wait_for(lambda: api.call("GET", "/api/v1/playback/state")["timeline"]["state"] == "PLAYING", 30, "RESUME")
        time.sleep(3.0)
        mark = len(rx.history)
        time.sleep(1.0)
        lit = any(max(h[1].data) > 0 for h in rx.history[mark:])
        now_pos = api.call("GET", "/api/v1/playback/state")["position_s"]
        wind = doc.track("wind")
        wanted = wind is not None and wind.enabled and min(wind.value_at(now_pos - 0.9), wind.value_at(now_pos - 0.1)) > 0.05
        check("RESUME: effects continue with the programme", lit if wanted else True,
              f"position {now_pos:.1f} s, wind {'expected and ' if wanted else 'not expected, output '}{'on' if lit else 'off'}")
        api.call("POST", "/api/v1/playback/stop")
        time.sleep(1.5)
        check("STOP: dark", all(max(f.data) == 0 for f in rx.snapshot().values()))

        log("6/6 PLAY programme 2 (motion only) on the same service")
        mark = len(rx.history)
        play(2)
        time.sleep(6.0)
        running = platforms()
        later = rx.history[mark:]
        check("motion-only programme: both platforms RUNNING", len(running) == 2 and all(p["state"] == "RUNNING" and p["programme_id"] == 2 for p in running))
        check("motion-only programme: no effect output", len(later) > 300 and all(max(h[1].data) == 0 for h in later), f"{len(later)} dark frames")
        api.call("POST", "/api/v1/playback/stop")
        time.sleep(1.0)
        text = (work / "logs/controller.log").read_text(errors="replace")
        check("show service log without errors", "Traceback" not in text and '"level": "ERROR"' not in text and " ERROR " not in text)
    finally:
        if rx is not None:
            rx.stop()
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    passed = bool(checks) and all(c["ok"] for c in checks)
    report = {"result": "PASSED" if passed else "FAILED", "passed": sum(c["ok"] for c in checks), "checks": checks}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    log(f"RESULT: {report['result']} ({report['passed']}/{len(checks)} checks)" + ("" if passed or not args.work else f" - logs in {work / 'logs'}"))
    if not args.work and passed:
        shutil.rmtree(work, ignore_errors=True)
    elif not passed:
        log(f"working directory kept: {work}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
