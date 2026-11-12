#!/usr/bin/env python3
"""End-to-end test of the live mode - real processes, real UDP, on this PC.

    python integration/run_live_integration.py [--fourdvr /path/to/fourdvr] [--server /path/to/fourdvr-server] [--keep]

What runs:

    "the game"      fourdvr live sim --format f1        a made-up drive as F1 25 telemetry (UDP)
    the live show   fourdvr live run -c live.toml       two platforms, a venue patch, recording
    two boards      the firmware's link guard and simulated actuators behind real UDP sockets
                    (the ESP32 link protocol as on the wire), here in this process so that every
                    command they receive can be looked at
    a DMX receiver  sACN (E1.31) on the loopback interface

The game runs for half a minute, is closed, and comes back. Checked: the configuration is
accepted; both boards are commanded without a gap, inside their strokes, never faulted; the
platforms move the right way round (nose up when the car speeds up, leaning into the bends, the
left bend before the right one) - through the real cueing, limiter, kinematics and link; wind,
rain (the game's weather) and the air blast (the collision) come out as DMX and within the
fixtures' limits; when the game is closed everything fades to rest and the platforms stand at
neutral; when it comes back the show does too; the live show ends by itself, clean; the recorded
session becomes a programme that passes validation.

With a Control Server (--server, or fourdvr-server on the PATH) a last step makes a 360-degree
video "of the session" - a panorama that turns as the recording says the car turned, beginning
seven seconds into it - and checks that 'fourdvr live convert --video' finds by itself where the
video belongs, that the server refuses the session programme made without a video, and that it
accepts and publishes the one made for the video.

Needs: this package (the "studio" extra for the last steps). No hardware.
What a real game sends, and what real boards do, is not part of this test.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from fourdvr_fx.dmx import DmxReceiver
from fourdvr_fx.embed import read_effects
from fourdvr_fx.patch import load_patch
from fourdvr_motion import motionfile
from fourdvr_motion.esp32_protocol import CommandState, decode_command
from fourdvr_motion.platform_config import load_platform_config
from fourdvr_motion.sim_esp32 import GuardConfig, SimulatedEsp32

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config" / "platforms" / "example-electric-6dof.toml"
PLATFORMS = ["PLATFORM-01", "PLATFORM-02"]
GAME_S, GAP_S, BACK_S = 31.0, 6.0, 9.0          # the game runs, is closed, comes back
VIDEO_AT, VIDEO_S = 7.0, 22.0                   # the video of the session begins this far into the recording


def log(text: str) -> None:
    print(text, flush=True)


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_session_video(session_path: Path, video: Path) -> None:
    """A 360-degree video "of the session": a panorama that turns as the recording says the car turned,
    beginning VIDEO_AT seconds into the recording and VIDEO_S seconds long."""
    from fractions import Fraction

    import av
    import cv2

    from fourdvr_live.session import Session

    session = Session.load(session_path)
    heading = np.cumsum(session.rates[:, 2]) / session.rate_hz          # rad, + = turned to the left
    w, h, fps = 640, 320, 30
    rng = np.random.default_rng(11)
    tex = cv2.resize(rng.random((h // 4, w // 4)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    tex = cv2.cvtColor(cv2.normalize(tex, None, 40, 200, cv2.NORM_MINMAX).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    with av.open(str(video), "w") as out:
        vs = out.add_stream("mpeg4", rate=fps)
        vs.width, vs.height, vs.pix_fmt, vs.bit_rate = w, h, "yuv420p", 2_500_000
        for i in range(int(VIDEO_S * fps)):
            k = min(len(heading) - 1, round((VIDEO_AT + i / fps) * session.rate_hz))
            shift = float(heading[k]) / (2.0 * np.pi) * w                # the camera turns left: the scene moves right
            img = cv2.warpAffine(tex, np.float32([[1, 0, shift], [0, 1, 0]]), (w, h), borderMode=cv2.BORDER_WRAP)
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            frame.pts, frame.time_base = i, Fraction(1, fps)
            for packet in vs.encode(frame):
                out.mux(packet)
        for packet in vs.encode(None):
            out.mux(packet)


class Board:
    """A motion board on UDP: the simulated ESP32 of the controller tests, with a log of every command."""

    def __init__(self, port: int, link_id: int) -> None:
        cfg = load_platform_config(PROFILE)
        stroke = cfg.actuators.stroke
        park = cfg.geometry.leg_lengths(cfg.calibration.park_pose + cfg.calibration.zero_offsets)
        park_strokes = park - cfg.actuators.min_length + cfg.calibration.actuator_offsets
        self.cfg = cfg
        self.sim = SimulatedEsp32(GuardConfig(
            platform_id=link_id, stroke_um=tuple(int(s * 1e6) for s in stroke),
            park_um=tuple(round(p * 1e6) for p in park_strokes),
            max_speed_um_s=int(cfg.actuators.max_velocity * 1.2e6), park_speed_um_s=int(cfg.actuators.max_velocity * 0.2e6),
            timeout_ms=200))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.01)
        self.rows: list[tuple[float, int, tuple[int, ...], int, int]] = []   # time, command state, targets, device state, faults
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        start = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            now_ms = int((now - start) * 1000)
            try:
                data, addr = self.sock.recvfrom(256)
            except TimeoutError:
                self.sim.tick(now_ms)
                continue
            except OSError:
                return
            self.sim.on_packet(data, now_ms)
            for packet in self.sim.take_outbox():
                self.sock.sendto(packet, addr)
            try:
                cmd = decode_command(data)
            except ValueError:
                continue
            g = self.sim.guard
            self.rows.append((time.monotonic(), int(cmd.state), tuple(cmd.targets_um), int(g.state), int(g.faults)))

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=1.0)
        self.sock.close()

    def poses(self, rows: list[tuple[float, int, tuple[int, ...], int, int]], every: int = 5) -> tuple[np.ndarray, np.ndarray]:
        """Times and platform poses (relative to neutral) of every `every`-th command, by forward kinematics."""
        cfg = self.cfg
        cal = cfg.calibration
        times, poses = [], []
        guess = cal.neutral_pose + cal.zero_offsets
        for t, _state, targets, _dev, _faults in rows[::every]:
            lengths = np.array(targets, dtype=np.float64) / 1e6 + cfg.actuators.min_length - cal.actuator_offsets
            pose = cfg.geometry.forward(lengths, guess=guess)
            guess = pose
            times.append(t)
            poses.append(pose - cal.zero_offsets - cal.neutral_pose)
        return np.array(times), np.array(poses)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fourdvr", default=shutil.which("fourdvr") or str(Path(sys.executable).with_name("fourdvr")),
                    help="the fourdvr command to test (an installation's, or the AppImage's AppRun)")
    ap.add_argument("--server", default=shutil.which("fourdvr-server") or "",
                    help="Control Server command: adds the last step (a programme of the session, with its video, is accepted)")
    ap.add_argument("--keep", action="store_true", help="keep the working folder")
    args = ap.parse_args()
    if args.server and shutil.which(args.server) is None and not Path(args.server).is_file():
        log(f"error: Control Server command '{args.server}' not found")
        return 2
    fourdvr = [args.fourdvr]
    work = Path(tempfile.mkdtemp(prefix="fourdvr-live-it-"))
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        log(f"   {'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def tool(*argv: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*fourdvr, *argv], capture_output=True, text=True, timeout=timeout)

    procs: list[subprocess.Popen[Any]] = []
    boards: list[Board] = []
    rx: DmxReceiver | None = None
    try:
        log(f"working folder {work}")
        game_port, dmx_port = free_udp_port(), free_udp_port()
        board_ports = [free_udp_port(), free_udp_port()]
        boards = [Board(port, k + 1) for k, port in enumerate(board_ports)]

        log("1/6 configuration: two platforms on UDP, the example venue patch with sACN to this PC")
        patch_text = (ROOT / "config" / "effects" / "patch.example.toml").read_text()
        patch_text = patch_text.replace("multicast = true ", "multicast = false").replace("unicast = [] ", f'unicast = ["127.0.0.1"]\nport = {dmx_port}\n#')
        patch_text += '\n[[fixtures]]\nid = "rain-main"\neffect = "rain"\nscope = "theatre"\nuniverse = 1\naddress = 30\nprofile = "dimmer"\n'
        (work / "venue.toml").write_text(patch_text)
        patch = load_patch(work / "venue.toml")
        where = {i.key: (i.universe, i.address) for i in patch.instances(PLATFORMS)}
        platforms = "".join(
            f'[[platforms]]\nconfig = "{PROFILE}"\nid = "{pid}"\nlink_platform_id = {k + 1}\n'
            f'driver = "servo_electric"\naddress = "udp://127.0.0.1:{board_ports[k]}"\n' for k, pid in enumerate(PLATFORMS))
        (work / "live.toml").write_text(
            f'[live]\nrecord = "sessions"\n{platforms}[effects]\npatch = "venue.toml"\n'
            f'[source]\ntype = "f1"\nport = {game_port}\n[cueing]\ngain = 0.9\n')
        listing = tool("live", "check", "-c", str(work / "live.toml"))
        check("fourdvr live check accepts the configuration and names source, platforms and patch",
              listing.returncode == 0 and "EA SPORTS F1 UDP" in listing.stdout and all(p in listing.stdout for p in PLATFORMS)
              and "output sacn" in listing.stdout and listing.stdout.rstrip().endswith("ok"), listing.stderr.strip()[:200])

        rx = DmxReceiver("sacn", [1, 2], bind_ip="127.0.0.1", port=dmx_port, multicast=False, history=400000).start()
        total = 4.0 + GAME_S + GAP_S + BACK_S + 3.0
        show_log = (work / "show.log").open("w")
        log(f"2/6 the live show starts ({total:.0f} s); the game is not running yet")
        show = subprocess.Popen([*fourdvr, "live", "run", "-c", str(work / "live.toml"), "--no-window", "--null-audio",
                                 "--seconds", str(total), "--status-interval", "1"], stdout=show_log, stderr=subprocess.STDOUT)
        procs.append(show)
        t_show = time.monotonic()
        time.sleep(4.0)
        n_before = [len(b.rows) for b in boards]
        check("before the game runs both boards are commanded and stand at neutral, without a fault",
              all(n > 200 for n in n_before) and all(b.rows[-1][1] == CommandState.RUN and b.rows[-1][4] == 0 for b in boards),
              f"{n_before} commands")

        log(f"3/6 the game runs for {GAME_S:.0f} s (F1 25 telemetry, 60 packets a second)")
        game = subprocess.Popen([*fourdvr, "live", "sim", "--format", "f1", "--to", f"127.0.0.1:{game_port}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(game)
        t_game = time.monotonic()
        time.sleep(GAME_S)
        game.terminate()
        game.wait(timeout=5)
        t_closed = time.monotonic()
        log(f"4/6 the game is closed for {GAP_S:.0f} s, then comes back for {BACK_S:.0f} s")
        time.sleep(GAP_S)
        game2 = subprocess.Popen([*fourdvr, "live", "sim", "--format", "f1", "--to", f"127.0.0.1:{game_port}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(game2)
        t_back = time.monotonic()
        time.sleep(BACK_S)
        game2.terminate()
        game2.wait(timeout=5)
        code = show.wait(timeout=30)
        t_end = time.monotonic()
        show_log.close()
        time.sleep(0.6)
        text = (work / "show.log").read_text()

        # ------------------------------------------------------------------ the boards
        rows = [list(b.rows) for b in boards]
        for k, r in enumerate(rows):
            during = [x for x in r if t_show + 3.0 < x[0] < t_end - 1.5]
            times = np.array([x[0] for x in during])
            gaps = np.diff(times)
            strokes = np.array([x[2] for x in during], dtype=np.float64) / 1e6
            limit = boards[k].cfg.actuators.stroke
            check(f"{PLATFORMS[k]}: a command every control period for the whole show, no gap near the board's time-out",
                  len(during) > 0.95 * 100 * (times[-1] - times[0]) and gaps.max() < 0.12, f"{len(during)} commands, longest gap {gaps.max() * 1000:.0f} ms")
            check(f"{PLATFORMS[k]}: always RUN, never a fault, every target inside the stroke",
                  all(x[1] == CommandState.RUN and x[4] == 0 for x in during) and strokes.min() >= 0.0 and np.all(strokes.max(axis=0) <= limit + 1e-6),
                  f"lost {boards[k].sim.guard.seq.lost}, invalid {boards[k].sim.guard.invalid}")
        a = np.array([x[2] for x in rows[0] if t_game + 5 < x[0] < t_game + 25], dtype=np.float64)
        b = np.array([x[2] for x in rows[1] if t_game + 5 < x[0] < t_game + 25], dtype=np.float64)
        check("both platforms get the same motion", abs(len(a) - len(b)) <= 3 and np.allclose(np.ptp(a, axis=0), np.ptp(b, axis=0), rtol=0.02),
              f"stroke ranges {np.round(np.ptp(a, axis=0) / 1000).astype(int).tolist()} mm")

        times, poses = boards[0].poses([x for x in rows[0] if t_game - 1.0 < x[0] < t_closed + GAP_S])
        rel = times - t_game
        lim = boards[0].cfg.limits
        neutral = boards[0].cfg.calibration.neutral_pose
        inside = np.all(poses + neutral <= lim.position_max + 1e-4) and np.all(poses + neutral >= lim.position_min - 1e-4)
        drive = (rel > 2.0) & (rel < GAME_S)
        pitch, roll = poses[:, 4], poses[:, 3]
        t_nose_up = rel[drive][np.argmin(pitch[drive])]
        t_lean_right = rel[drive][np.argmax(roll[drive])]
        t_lean_left = rel[drive][np.argmin(roll[drive])]
        check("the platform moves, and stays inside its position limits",
              bool(inside) and np.ptp(poses[drive], axis=0)[[0, 1, 3, 4]].min() > 0.02,
              f"range surge {np.ptp(poses[drive][:, 0]) * 1000:.0f} mm, sway {np.ptp(poses[drive][:, 1]) * 1000:.0f} mm, "
              f"roll {np.degrees(np.ptp(roll[drive])):.1f}°, pitch {np.degrees(np.ptp(pitch[drive])):.1f}°")
        check("the right way round: nose up while the car speeds up, right side down in the left bend, left side down in the right bend - in that order",
              pitch[drive].min() < -0.02 and roll[drive].max() > 0.03 and roll[drive].min() < -0.03 and t_nose_up < t_lean_right < t_lean_left,
              f"nose up {np.degrees(-pitch[drive].min()):.1f}° at {t_nose_up:.1f} s, leaning right {np.degrees(roll[drive].max()):.1f}° at "
              f"{t_lean_right:.1f} s, left {np.degrees(-roll[drive].min()):.1f}° at {t_lean_left:.1f} s")
        rest = (times > t_closed + 3.5) & (times < t_closed + GAP_S - 0.3)
        check("the game is closed: within 3.5 s the platforms stand at neutral, and stay there",
              rest.sum() > 20 and np.abs(poses[rest][:, :3]).max() < 0.003 and np.abs(poses[rest][:, 3:]).max() < 0.004,
              f"largest offset {np.abs(poses[rest][:, :3]).max() * 1000:.1f} mm, {np.degrees(np.abs(poses[rest][:, 3:]).max()):.2f}°")
        _times2, poses2 = boards[0].poses([x for x in rows[0] if t_back + 4.0 < x[0] < t_back + BACK_S])
        check("the game comes back: the platforms move again", np.ptp(poses2[:, 4]) > 0.01,
              f"pitch range {np.degrees(np.ptp(poses2[:, 4])):.1f}°")

        # ------------------------------------------------------------------ effects on the wire
        history = list(rx.history)
        ft = np.array([t for t, _f in history])
        u = np.array([f.universe for _t, f in history])

        def channel(key: str) -> tuple[np.ndarray, np.ndarray]:
            universe, address = where[key]
            sel = u == universe
            return ft[sel] - t_game, np.array([f.data[address - 1] for (_t, f), s in zip(history, sel, strict=True) if s])

        tw, wind = channel("wind@PLATFORM-01")
        tw2, wind2 = channel("wind@PLATFORM-02")
        check("wind on both platforms follows the car's speed, and is off before the game runs",
              wind[tw < 0].max() == 0 and wind[(tw > 12) & (tw < 20)].min() > 60 and wind2[(tw2 > 12) & (tw2 < 20)].min() > 60,
              f"DMX {int(wind[(tw > 12) & (tw < 20)].min())}..{int(wind[(tw > 0) & (tw < GAME_S)].max())} of 255 while driving")
        tr, rain = channel("rain-main")
        lit = tr[rain > 0]
        check("rain from the game's weather: on while it rains in the game, and no longer than the fixture's limit",
              len(lit) > 40 and 23.0 < lit.min() < 27.0 and 2.0 < (lit.max() - lit.min()) <= 5.2 and rain[tr < 22].max() == 0,
              f"lit from {lit.min():.1f} to {lit.max():.1f} s of the drive" if len(lit) else "never lit")
        ta, air = channel("air@PLATFORM-01")
        blast = ta[air > 0]
        check("the collision fires the air blast once, briefly",
              len(blast) > 3 and 28.5 < blast.min() < 30.5 and (blast.max() - blast.min()) < 1.1,
              f"at {blast.min():.2f} s for {(blast.max() - blast.min()):.2f} s" if len(blast) else "never fired")
        others = [k for k in where if k.split("@")[0] in ("fog-main", "strobe-front", "scent-pine", "scent-sea", "water")]
        check("nothing the source did not ask for is ever lit (fog, strobe, scent, water)",
              all(channel(k)[1].max() == 0 for k in others))
        gone = wind[(tw > (t_closed - t_game) + 2.5) & (tw < (t_back - t_game) - 0.2)]
        check("the game is closed: wind and every other effect off within 2.5 s",
              len(gone) > 50 and gone.max() == 0 and rain[(tr > (t_closed - t_game) + 2.5) & (tr < (t_back - t_game))].max() == 0)
        again = wind[(tw > (t_back - t_game) + 5.0) & (tw < (t_back - t_game) + BACK_S)]
        check("the game comes back: so does the wind", len(again) > 50 and again.max() > 40, f"DMX up to {int(again.max())}")

        # ------------------------------------------------------------------ the end, and the recording
        check("the live show ends by itself with exit code 0 and reports both platforms stopped without a fault",
              code == 0 and all(f"{p}: STOPPED" in text for p in PLATFORMS) and "fault:" not in text, f"exit code {code}")
        check("its log has no error", "Traceback" not in text and "error:" not in text and "internal error" not in text)
        time.sleep(0.5)
        check("afterwards the boards park by themselves (their own time-out)",
              all(b.sim.guard.state.name in ("PARKING", "PARKED") for b in boards),
              ", ".join(b.sim.guard.state.name for b in boards))

        log("5/6 the recorded session becomes a programme")
        sessions = sorted((work / "sessions").glob("*.4dlive"))
        info = tool("live", "info", str(sessions[0])) if sessions else None
        check("the session was recorded and 'fourdvr live info' reads it",
              info is not None and info.returncode == 0 and "EA SPORTS F1 UDP" in info.stdout and "wind" in info.stdout,
              sessions[0].name if sessions else "no file")
        if sessions:
            out = work / "drive.motion"
            conv = tool("live", "convert", str(sessions[0]), "-o", str(out), "--platform", str(PROFILE), "--programme-id", "5",
                        "--start", "3", "--end", str(3.0 + GAME_S))
            ok = conv.returncode == 0 and out.exists()
            detail = conv.stderr.strip()[:200]
            if ok:
                mf = motionfile.read_motion(out, verify=True)
                doc = read_effects(out)
                kinds = sorted({t.type for t in doc.tracks}) if doc is not None else []
                ok = (mf.programme_id == 5 and bool(mf.header["validation"]["passed"]) and {"wind", "rain", "air", "vibration"} <= set(kinds)
                      and doc is not None and doc.bass is not None and abs(mf.duration_s - GAME_S) < 0.05)
                detail = f"{mf.duration_s:.1f} s, effects {', '.join(kinds)}, bass track, validation passed"
            check("'fourdvr live convert' makes a .motion programme of it that passes validation, with the effects and a bass track", ok, detail)
            listed = tool("fx", "check", str(out), "--patch", str(work / "venue.toml"))
            check("'fourdvr fx check' accepts that programme against the venue patch", listed.returncode == 0, listed.stdout.strip().splitlines()[-1][:120] if listed.stdout else listed.stderr[:120])

        if sessions and not args.server:
            log("6/6 skipped: no Control Server command (--server)")
        elif sessions:
            log("6/6 the session and a 360° video of it become a programme the Control Server accepts")
            from fourdvr_extractor.video import file_sha256
            from fourdvr_live.session import Session

            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from run_effects_integration import ADMIN_PASSWORD, Api, free_port, wait_for

            video, made = work / "drive-360.mp4", work / "drive-360.motion"
            make_session_video(sessions[0], video)
            conv = tool("live", "convert", str(sessions[0]), "--video", str(video), "--projection", "360", "--stereo", "mono",
                        "-o", str(made), "--platform", str(PROFILE), "--programme-id", "5", timeout=300.0)
            found = re.search(r"begins at second (-?[0-9.]+) of the session \(match ([0-9.]+)\)", conv.stdout)
            offset = float(found.group(1)) if found else float("nan")
            check(f"'fourdvr live convert --video' finds by the picture where the video begins in the session ({VIDEO_AT:g} s)",
                  conv.returncode == 0 and made.exists() and abs(offset - VIDEO_AT) < 0.2,
                  f"found {offset:.2f} s, match {found.group(2)}" if found else (conv.stderr.strip() or conv.stdout.strip())[-200:])
            if made.exists():
                mf = motionfile.read_motion(made, verify=True)
                doc = read_effects(made)
                session = Session.load(sessions[0])
                raining = np.flatnonzero(session.effects[:, session.effect_names.index("rain")] > 0.05)
                rain_at = float(raining[0]) / session.rate_hz - offset if len(raining) else float("nan")
                cues = [c.t for t in doc.tracks if t.type == "rain" for c in t.cues] if doc is not None else []
                check("that programme has the video's length and names the video; the rain begins where the video shows it",
                      abs(mf.duration_s - VIDEO_S) < 0.05 and mf.video_sha256 == file_sha256(video) and bool(mf.header["validation"]["passed"])
                      and len(cues) == 1 and abs(cues[0] - rain_at) < 0.3,
                      f"{mf.duration_s:.2f} s, rain cue at {cues[0] if cues else float('nan'):.2f} s of the video (recorded at {rain_at:.2f} s)")
                port = free_port()
                cfg = work / "server.toml"
                cfg.write_text(
                    f'[server]\nhost = "127.0.0.1"\nport = {port}\ndata_dir = "{work / "server-data"}"\nlog_level = "WARNING"\n'
                    f'log_format = "text"\n[security]\ninitial_admin_password = "{ADMIN_PASSWORD}"\n')
                subprocess.run([args.server, "-c", str(cfg), "init"], capture_output=True, text=True, check=True, timeout=120)
                procs.append(subprocess.Popen([args.server, "-c", str(cfg), "serve"], stdout=(work / "server.log").open("wb"),
                                              stderr=subprocess.STDOUT))
                api = Api(f"http://127.0.0.1:{port}")
                health = wait_for(lambda: api.call("GET", "/api/v1/health", timeout=2), 30, "the Control Server")
                api.token = api.call("POST", "/api/v1/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]
                api.call("POST", "/api/v1/programmes", {"programme_id": 5, "name": "A drive, recorded live", "projection": "360",
                                                        "stereo": "mono", "loop": False})
                api.call("PUT", f"/api/v1/programmes/5/video?filename={video.name}", raw=video.read_bytes())
                try:                              # the programme of step 5: the same session, made without a video
                    api.call("PUT", "/api/v1/programmes/5/motion?filename=drive.motion", raw=(work / "drive.motion").read_bytes())
                    res = api.call("POST", "/api/v1/programmes/5/validate")
                    refused = sorted(c["check"] for c in res["checks"] if c["status"] == "fail")
                    check(f"{health['product']} {health['version']} refuses the session programme that was made without the video",
                          not res["passed"] and "motion_video_match" in refused, f"failed: {', '.join(refused)}")
                except RuntimeError as exc:
                    check("the Control Server refuses the session programme that was made without the video", True, str(exc)[:160])
                api.call("PUT", f"/api/v1/programmes/5/motion?filename={made.name}", raw=made.read_bytes())
                res = api.call("POST", "/api/v1/programmes/5/validate")
                failed_checks = [c for c in res["checks"] if c["status"] == "fail"]
                published = False
                if res["passed"]:
                    api.call("POST", "/api/v1/programmes/5/publish")
                    published = True
                check("... and accepts and publishes the one made for the video", bool(res["passed"]) and not failed_checks and published,
                      f"{len(res['checks'])} checks" + (f", failed: {failed_checks}" if failed_checks else ""))
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        for board in boards:
            board.stop()
        if rx is not None:
            rx.stop()
    failed = [r for r in results if not r[1]]
    log(f"\n{len(results) - len(failed)} of {len(results)} checks passed" + ("" if not failed else f"; FAILED: {'; '.join(r[0] for r in failed)}"))
    if args.keep or failed:
        log(f"working folder kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if results and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
