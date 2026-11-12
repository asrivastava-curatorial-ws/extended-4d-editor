#!/usr/bin/env python3
"""End-to-end test of effects through ESP32 nodes - with the firmware's own code on this PC.

    python integration/run_esp32_integration.py --server /path/to/fourdvr-server

What runs: a programme made by `fourdvr extract generate` (suggestions, bass track) and given cues
for every effect type -> the unchanged Control Server -> the show service with three simulated
platforms and a venue patch `protocol = "esp32"` -> the "4DFX" link over real UDP -> three ESP32s:

    PLATFORM-01   an ESP32 with a DMX line (its own devices and the theatre's) and an audio output
    PLATFORM-02   an ESP32 with a DMX line and no audio output
    PLATFORM-03   an ESP32 that knows only motion: nothing answers on the effects port

The two effects nodes are `fxnode_sim`: the firmware's portable core (lib/fourdvr_fx - packet
checking, DMX buffers and time-out, sound buffer) compiled for this PC behind a UDP socket; it writes
the DMX line as it would be sent and the sound as it would be played. Without a C++ compiler the
controller's Python stand-in for a node is used instead (and the report says so).

Checked: every node gets what it has and nothing else; the DMX on both lines is what the programme
prescribes at this venue; the bass is in step with the programme; a missing node, DMX line or audio
output is no error and the platform still runs; an emergency stop at a node darkens it; a node that
is unplugged and plugged in again is picked up; pause, resume, stop.

Needs: this package with the "studio" extra, the Control Server 1.0, g++ (optional), loopback
addresses 127.0.0.1-3 (Linux). No hardware: what a real ESP32 does with its UART and its DAC is not
part of this test.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_effects_integration import ADMIN_PASSWORD, ROOT, Api, add_test_cues, free_port, log, make_video, wait_for

from fourdvr_fx import Cue, EffectsDocument
from fourdvr_fx.audio import db_to_gain, resample
from fourdvr_fx.embed import embed_into_motion, read_effects
from fourdvr_fx.engine import THEATRE, EffectsEngine, ShowState
from fourdvr_fx.patch import load_patch

PLATFORMS = ["PLATFORM-01", "PLATFORM-02", "PLATFORM-03"]
HOSTS = {"PLATFORM-01": "127.0.0.1", "PLATFORM-02": "127.0.0.2", "PLATFORM-03": "127.0.0.3"}
FRAME_S = 0.025
FIRMWARE = ROOT / "firmware" / "esp32-actuator-bridge"
AUDIO_RATE = 4000


class Node:
    """One fxnode_sim process (or the Python stand-in) and what it wrote."""

    def __init__(self, binary: Path | None, work: Path, name: str, host: str, port: int, node_id: int, dmx_ports: int, audio_channels: int) -> None:
        self.dir = work / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audio_channels, self.sim = audio_channels, None
        self.proc: subprocess.Popen[str] | None = None
        if binary is not None:
            self.proc = subprocess.Popen(
                [str(binary), "--host", host, "--port", str(port), "--node", str(node_id), "--dmx-ports", str(dmx_ports),
                 "--audio-channels", str(audio_channels), "--actuators", "--log", str(self.dir)],
                stdout=subprocess.PIPE, text=True)
            assert self.proc.stdout is not None
            line = self.proc.stdout.readline()
            self.started = time.monotonic()
            if "fxnode_sim" not in line:
                raise RuntimeError(f"{name} did not start: {line!r}")
        else:
            from fourdvr_fx.bridge import SimNode

            self.sim = SimNode(node_id, dmx_ports, audio_channels, actuators=True, host=host, port=port).start()
            self.started = time.monotonic()

    def inhibit(self) -> None:
        """Switch the node's emergency-stop input (on, and with the next call off)."""
        if self.proc is not None:
            self.proc.send_signal(signal.SIGUSR1)
        elif self.sim is not None:
            self.sim.inhibited = not self.sim.inhibited

    def kill(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        elif self.sim is not None:
            self.sim.stop()

    def finish(self) -> dict[str, Any]:
        if self.proc is not None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)
            return dict(json.loads((self.dir / "summary.json").read_text()))
        assert self.sim is not None
        self.sim.stop()
        return {"invalid": self.sim.invalid, "audio_underruns": 0, "audio_gaps": self.sim.audio_gaps, "dmx_timeouts": self.sim.dmx_timeouts,
                "packets": self.sim.packets}

    def dmx(self) -> tuple[np.ndarray, np.ndarray]:
        """(times on this PC's clock, frames) - one row per change of DMX line 1."""
        times, rows = [], []
        if self.proc is not None:
            for line in (self.dir / "dmx.log").read_text().splitlines():
                ms, port, hexa = line.split()
                if port == "0":
                    times.append(self.started + int(ms) / 1000.0)
                    rows.append(np.frombuffer(bytes.fromhex(hexa).ljust(512, b"\x00"), np.uint8))
        elif self.sim is not None:
            for t, port, frame in self.sim.dmx_history:
                if port == 0:
                    times.append(t)
                    rows.append(np.frombuffer(frame, np.uint8))
        return np.array(times), (np.array(rows) if rows else np.zeros((0, 512), np.uint8))

    def audio(self) -> tuple[float, np.ndarray]:
        """(time of the first frame, the sound as played: (frames, channels) in -1..1)."""
        if self.proc is not None:
            raw = np.frombuffer((self.dir / "audio.raw").read_bytes(), dtype="<i2")
            ch = max(1, self.audio_channels)
            return self.started, raw[: len(raw) // ch * ch].reshape(-1, ch).astype(np.float64) / 32767.0
        assert self.sim is not None
        blocks = self.sim.audio_blocks
        if not blocks:
            return self.started, np.zeros((0, max(1, self.audio_channels)))
        return blocks[0][0], self.sim.audio().astype(np.float64) / 32767.0


def frame_at(times: np.ndarray, rows: np.ndarray, t: np.ndarray) -> np.ndarray:
    """The frame on the line at each time `t` (the last change before it; dark before the first)."""
    idx = np.searchsorted(times, t, side="right") - 1
    out = np.zeros((len(t), 512), np.uint8)
    ok = idx >= 0
    out[ok] = rows[idx[ok]]
    return out


def simulate(doc: EffectsDocument, patch_path: Path, seconds: float) -> dict[int, np.ndarray]:
    """What the programme prescribes at this venue, frame by frame from position 0 (the engine on a simulated clock)."""
    now = [0.0]
    engine = EffectsEngine(load_patch(patch_path), PLATFORMS, clock=lambda: now[0])
    state = ShowState(True, doc.compile(), 0.0, doc.duration_s, True, 1)
    shows = {THEATRE: state, **{p: state for p in PLATFORMS}}
    frames: dict[int, list[bytes]] = {}
    for k in range(int(seconds / FRAME_S) + 1):
        now[0] = state.position_s = k * FRAME_S
        for universe, data in engine.render(shows, now[0]).items():
            frames.setdefault(universe, []).append(data)
    return {u: np.array([np.frombuffer(f, dtype=np.uint8) for f in rows]) for u, rows in frames.items()}


def add_new_types(doc: EffectsDocument) -> None:
    """Cues for the effect types that came with the ESP32 output's example patch."""
    doc.new_track("rain").cues = [Cue(3.0, 3.0, 0.8, 0.5, 0.5), Cue(12.0, 8.0)]             # venue: 5 s at most
    doc.new_track("heat").cues = [Cue(7.0, 2.0, 1.0, 0.3, 0.5)]                             # venue: 80 % at most
    doc.new_track("bubbles").cues = [Cue(9.0, 3.0, 0.8)]
    doc.new_track("snow").cues = [Cue(13.0, 2.0, 0.7)]
    scent = doc.track("scent")
    assert scent is not None
    scent.cues.append(Cue(14.0, 2.0, params={"slot": 3.0}, label="flowers"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="fourdvr-server", help="Control Server command")
    ap.add_argument("--fourdvr", default=str(Path(sys.executable).with_name("fourdvr")), help="this product's command")
    ap.add_argument("--no-compiler", action="store_true", help="use the Python stand-in for the nodes even if g++ is there")
    ap.add_argument("--work", help="working directory (default: a temporary one, removed afterwards)")
    ap.add_argument("--report", help="write the result as JSON")
    args = ap.parse_args()
    if shutil.which(args.server) is None and not Path(args.server).is_file():
        log(f"error: Control Server command '{args.server}' not found (use --server)")
        return 2
    try:
        for host in HOSTS.values():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind((host, 0))
    except OSError:
        log("error: this test needs the loopback addresses 127.0.0.1, 127.0.0.2 and 127.0.0.3 (Linux)")
        return 2
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="fourdvr-esp32-it-"))
    shutil.rmtree(work, ignore_errors=True)
    (work / "logs").mkdir(parents=True)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"ok": bool(ok), "check": name, "detail": detail})
        log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def tool(*cmd: str, ok: tuple[int, ...] = (0,)) -> str:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode not in ok:
            raise RuntimeError(f"{' '.join(cmd[:3])} ... failed:\n{res.stdout[-1500:]}{res.stderr[-1500:]}")
        return res.stdout

    procs: list[subprocess.Popen[bytes]] = []
    nodes: list[Node] = []
    try:
        log("1/7 the ESP32 effects node for this PC")
        binary: Path | None = None
        compiler = None if args.no_compiler else (shutil.which("g++") or shutil.which("c++"))
        if compiler:
            binary = work / "fxnode_sim"
            core = FIRMWARE / "lib" / "fourdvr_fx" / "src"
            tool(compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", f"-I{core}", "-o", str(binary),
                 str(FIRMWARE / "test" / "host" / "fxnode_sim.cpp"), str(core / "fourdvr_fx.cpp"))
            tester = work / "test_fx"
            tool(compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", f"-I{core}", "-o", str(tester),
                 str(FIRMWARE / "test" / "host" / "test_fx.cpp"), str(core / "fourdvr_fx.cpp"))
            unit = tool(str(tester), str(FIRMWARE / "test" / "fx_vectors.txt")).strip()
            check("the firmware's effects core builds for this PC and passes its own tests (with the controller's packets)", unit.endswith(" 0 failed"), unit)
        else:
            log("  note: no C++ compiler - the nodes are the controller's Python stand-in, not the firmware's core")
        fx_port = free_port(socket.SOCK_DGRAM)
        nodes.append(Node(binary, work, "node1", HOSTS["PLATFORM-01"], fx_port, 1, dmx_ports=1, audio_channels=2))
        nodes.append(Node(binary, work, "node2", HOSTS["PLATFORM-02"], fx_port, 2, dmx_ports=1, audio_channels=0))
        node1, node2 = nodes

        log("2/7 programme")
        sim_profile = ROOT / "config" / "platforms" / "simulation-6dof.toml"
        video, motion = work / "test-ride.mp4", work / "programme1.motion"
        make_video(video, 36.0)
        tool(args.fourdvr, "extract", "generate", str(video), "-o", str(motion), "--projection", "360", "--profile", str(sim_profile),
             "--effects", "suggest", "--bass", "-q")
        doc = read_effects(motion)
        assert doc is not None and doc.bass is not None
        add_test_cues(doc)
        add_new_types(doc)
        embed_into_motion(motion, doc)
        doc = read_effects(motion)
        assert doc is not None and doc.bass is not None
        log(tool(args.fourdvr, "fx", "info", str(motion)).rstrip())

        log("3/7 venue patch: everything through ESP32s")
        patch_text = (ROOT / "config" / "effects" / "patch.esp32.example.toml").read_text()
        patch_text = patch_text.replace("port = 47002 ", f"port = {fx_port}  ").replace("192.168.10.101", HOSTS["PLATFORM-01"])
        patch_text = patch_text.replace('PLATFORM-02 = "192.168.10.102"', f'PLATFORM-02 = "{HOSTS["PLATFORM-02"]}"\nPLATFORM-03 = "{HOSTS["PLATFORM-03"]}"')
        patch_text = patch_text.replace("bass_channels = [1, 2] ", "bass_channels = [2]    ").replace("vibration_gain_db = -6.0", "vibration_gain_db = 0.0")
        patch_path = work / "venue.toml"
        patch_path.write_text(patch_text)
        patch = load_patch(patch_path)
        assert patch.output.esp32["port"] == fx_port and patch.output.esp32_audio is not None and patch.output.esp32_audio.bass_channels == [2]
        listing = tool(args.fourdvr, "fx", "nodes", "--patch", str(patch_path), "--platforms", ",".join(PLATFORMS), "--seconds", "2")
        log(listing.rstrip())
        rows = {line.split()[0]: line for line in listing.splitlines()[1:]}
        check("each ESP32 says what it has",
              "1 DMX line(s), audio 2 channel(s)" in rows[HOSTS["PLATFORM-01"]] and "1 DMX line(s)" in rows[HOSTS["PLATFORM-02"]]
              and "audio" not in rows[HOSTS["PLATFORM-02"]].split(" - ")[0] and "no answer" in rows[HOSTS["PLATFORM-03"]])
        log(tool(args.fourdvr, "fx", "check", str(motion), "--patch", str(patch_path), "--platforms", ",".join(PLATFORMS)).rstrip())

        log("4/7 Control Server and show service")
        port = free_port()
        cfg = work / "server.toml"
        cfg.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\ndata_dir = "{work / "server-data"}"\nlog_level = "WARNING"\nlog_format = "text"\n'
            f'[security]\ninitial_admin_password = "{ADMIN_PASSWORD}"\n[devices]\nheartbeat_interval_s = 1.0\noffline_timeout_s = 5.0\n'
            "[playback]\nstart_lead_s = 3.0\ntransition_gap_s = 1.5\nprepare_timeout_s = 60.0\n"
        )
        tool(args.server, "-c", str(cfg), "init")
        procs.append(subprocess.Popen([args.server, "-c", str(cfg), "serve"], stdout=(work / "logs/server.log").open("wb"), stderr=subprocess.STDOUT))
        api = Api(f"http://127.0.0.1:{port}")
        health = wait_for(lambda: api.call("GET", "/api/v1/health", timeout=2), 30, "the Control Server")
        api.token = api.call("POST", "/api/v1/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]
        api.call("POST", "/api/v1/programmes", {"programme_id": 1, "name": "Programme with effects", "projection": "360", "stereo": "mono", "loop": True})
        api.call("PUT", f"/api/v1/programmes/1/video?filename={video.name}", raw=video.read_bytes())
        api.call("PUT", f"/api/v1/programmes/1/motion?filename={motion.name}", raw=motion.read_bytes())
        res = api.call("POST", "/api/v1/programmes/1/validate")
        check(f"{health['product']} {health['version']} accepts the programme ({motion.stat().st_size // 1024} KiB, {len(doc.tracks)} effect tracks, bass)",
              bool(res["passed"]), f"{len(res['checks'])} checks")
        api.call("POST", "/api/v1/programmes/1/publish")
        keys = dict(line.split("\t") for line in tool(args.server, "-c", str(cfg), "device-key", *PLATFORMS).strip().splitlines())
        ctl = work / "controller.toml"
        ctl.write_text(
            f'[controller]\nserver_url = "ws://127.0.0.1:{port}/ws/v1/device"\ncontrol_rate_hz = 100.0\ncache_dir = "{work / "cache"}"\n'
            'status_interval_s = 0.5\ntime_sync_interval_s = 2.0\nlog_level = "INFO"\n'
            + "".join(f'\n[[platforms]]\nconfig = "{sim_profile}"\nid = "{pid}"\nlink_platform_id = {k + 1}\ndevice_key = "{keys[pid]}"\n'
                      for k, pid in enumerate(PLATFORMS))
            + f'\n[effects]\npatch = "{patch_path}"\nreference_platform = "{PLATFORMS[0]}"\n'
        )
        listing = tool(args.fourdvr, "controller", "check-config", "-c", str(ctl))
        (work / "logs/check-config.txt").write_text(listing)
        check("fourdvr controller check-config accepts the configuration", "configuration OK" in listing and "output esp32" in listing)
        procs.append(subprocess.Popen([args.fourdvr, "controller", "run", "-c", str(ctl)], stdout=(work / "logs/controller.log").open("wb"),
                                      stderr=subprocess.STDOUT))

        def platforms() -> list[dict[str, Any]]:
            return [p for p in api.call("GET", "/api/v1/devices", timeout=5)["motion_platforms"] if p["connection"] == "CONNECTED"]

        def effects_status() -> dict[str, Any]:
            return dict((platforms()[0].get("details") or {}).get("effects") or {})

        wait_for(lambda: len([p for p in platforms() if p["state"] == "READY"]) == 3, 60, "three platforms READY")
        # is the venue ready? - asked while the show service is running: it must see everything and touch nothing
        (work / "admin-password.txt").write_text(ADMIN_PASSWORD)
        venue_check = (args.fourdvr, "fx", "ready", "--patch", str(patch_path), "--platforms", ",".join(PLATFORMS), "-q")
        said = tool(*venue_check, "--server", f"http://127.0.0.1:{port}", "--user", "admin", "--password-file",
                    str(work / "admin-password.txt"), "--programme", str(motion), ok=(1,))
        (work / "logs/venue-check-running.txt").write_text(said)
        check("fourdvr fx ready while the show service runs: both boards answer and are left alone, the three platform controllers "
              "are connected, the board that is not there makes the venue NOT READY",
              said.count("the test was left out") == 2 and "PLATFORM-03: READY" in said and "is not known to the server" not in said
              and f"{HOSTS['PLATFORM-03']} (PLATFORM-03): no answer" in said and "NOT READY: 2 problem(s)" in said
              and "play this programme" in said and "no device for the effect 'vibration'" not in said,
              said.strip().splitlines()[-1] + " (the missing board; no headset is connected in this test)")
        best = (9.0, 0.0)
        for _ in range(15):          # this PC's clock against the server's master clock
            t0 = time.monotonic()
            master = api.call("GET", "/api/v1/time", timeout=2)["master_time"]
            t1 = time.monotonic()
            best = min(best, (t1 - t0, master - (t0 + t1) / 2))
        offset = best[1]

        log("5/7 PLAY: 16 s of the show on the two DMX lines and the audio output")
        res = api.call("POST", "/api/v1/playback/play", {"programme_id": 1})
        log(f"    PLAY 1: {res['result']} - {res['detail']}")
        show = wait_for(lambda: (lambda s: s if s["timeline"]["state"] == "PLAYING" and not s["transition_active"] else None)(
            api.call("GET", "/api/v1/playback/state", timeout=5)), 90, "programme 1 PLAYING")
        begin = float(show["timeline"]["start_at"]) - offset           # this PC's clock at programme position 0

        def until(position: float) -> None:
            time.sleep(max(0.0, begin + position - time.monotonic()))

        until(6.0)
        running = platforms()
        check("all three platforms RUNNING - also the one whose ESP32 has no effects at all",
              len(running) == 3 and all(p["state"] == "RUNNING" and p["programme_id"] == 1 for p in running))
        fx = effects_status()
        present = {n["host"]: n for n in (fx.get("nodes") or []) if n.get("present")}
        check("the show service reports the nodes it found; a missing one is no error",
              fx.get("playing") is True and fx.get("errors") == 0 and fx.get("output_errors") == 0 and set(present) == {HOSTS["PLATFORM-01"], HOSTS["PLATFORM-02"]},
              json.dumps({"output": fx.get("output"), "errors": fx.get("errors"), "nodes": {h: n.get("has") for h, n in present.items()}}))
        until(16.0)
        t_estop = time.monotonic()
        node1.inhibit()                                                # emergency stop at the first ESP32 ...
        until(18.0)
        inhibited = any(n.get("inhibited") for n in (effects_status().get("nodes") or []))
        node1.inhibit()                                                # ... released
        t_release = time.monotonic()
        until(20.0)
        t_unplug = time.monotonic()
        node2.kill()                                                   # the second ESP32 is unplugged ...
        until(24.5)                                                    # (a node counts as gone after three seconds without an answer)
        after_unplug = effects_status()
        still = platforms()
        node2b = Node(binary, work, "node2b", HOSTS["PLATFORM-02"], fx_port, 2, dmx_ports=1, audio_channels=0)   # ... and plugged in again
        nodes.append(node2b)
        until(28.0)
        api.call("POST", "/api/v1/playback/pause")
        t_pause = time.monotonic()
        time.sleep(2.0)
        api.call("POST", "/api/v1/playback/resume")
        wait_for(lambda: api.call("GET", "/api/v1/playback/state")["timeline"]["state"] == "PLAYING", 30, "RESUME")
        time.sleep(3.0)
        t_resumed = time.monotonic()
        resumed_at = float(api.call("GET", "/api/v1/playback/state")["position_s"])
        time.sleep(1.5)
        api.call("POST", "/api/v1/playback/stop")
        t_stop = time.monotonic()
        time.sleep(1.5)
        final = effects_status()
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            p.wait(timeout=15)
        procs.clear()
        time.sleep(0.3)
        summaries = {"node1": node1.finish(), "node2b": node2b.finish()}

        log("6/7 what the nodes put out")
        expected = simulate(doc, patch_path, 16.0)
        lines = {"node1": (node1, np.maximum(expected[1], expected[101]), ("theatre", "PLATFORM-01")), "node2": (node2, expected[102], ("PLATFORM-02",))}
        grid = np.arange(8, int(15.6 / FRAME_S)) * FRAME_S              # positions 0.2 .. 15.6 s
        for name, (node, want, owners) in lines.items():
            times, rows = node.dmx()
            got = frame_at(times, rows, begin + grid).astype(int)
            k = np.round(grid / FRAME_S).astype(int)
            # A value is right if it is what the programme prescribes within 75 ms around that moment (the node
            # refreshes its line 40 times a second, not in step with the controller's frames), give or take 3 DMX steps.
            near = np.stack([want[np.clip(k + d, 0, len(want) - 1)].astype(int) for d in (-3, -2, -1, 0, 1, 2, 3)])
            for owner in owners:
                insts = [i for i in patch.instances(PLATFORMS) if (i.platform or "theatre") == owner]
                bad = lit = total = 0
                for inst in insts:
                    for ch in inst.channels():
                        col = got[:, ch - 1]
                        bad += int(np.sum((col < near[:, :, ch - 1].min(axis=0) - 3) | (col > near[:, :, ch - 1].max(axis=0) + 3)))
                        lit += int(np.sum(col > 0))
                        total += len(col)
                check(f"{name}: the {len(insts)} {owner} fixtures on its DMX line as prescribed, within 75 ms", bad / max(1, total) <= 0.01,
                      f"{total} values, {lit} lit, {bad} off the mark ({100.0 * bad / max(1, total):.2f} %)")
            used = sorted({ch for i in patch.instances(PLATFORMS) if (i.platform or "theatre") in owners for ch in i.channels()})
            others = np.delete(got, [c - 1 for c in used], axis=1)
            check(f"{name}: no other slot of the line is ever lit", int(others.max()) == 0)
        t1, r1 = node1.dmx()
        check("before the show both lines are dark", frame_at(t1, r1, np.array([begin - 0.5])).max() == 0
              and frame_at(*node2.dmx(), np.array([begin - 0.5])).max() == 0)
        # the sound: channel 2 carries the bass track, channel 1 the seat vibration
        a0, sound = node1.audio()
        bass = resample(doc.bass.as_float() * db_to_gain(doc.bass.gain_db), doc.bass.sample_rate_hz, AUDIO_RATE)
        lo, hi = 2.5, 15.0
        first = round((begin + lo - a0) * AUDIO_RATE)
        played = sound[first : first + round((hi - lo) * AUDIO_RATE), 1]
        ref = bass[round(lo * AUDIO_RATE) : round(hi * AUDIO_RATE)]
        span = round(0.2 * AUDIO_RATE)
        corr = np.correlate(played[span:-span], ref, "valid") if len(played) == len(ref) else np.zeros(1)
        lag_ms = (int(np.argmax(corr)) - span) * 1000.0 / AUDIO_RATE if len(corr) > 1 else 999.0
        aligned = ref[span - round(lag_ms * AUDIO_RATE / 1000.0) :][: len(played) - 2 * span] if abs(lag_ms) < 190 else ref[: len(played) - 2 * span]
        match = float(np.corrcoef(played[span:-span], aligned)[0, 1]) if len(aligned) == len(played) - 2 * span else 0.0
        check("node1: the bass shaker plays the programme's bass track, in step with the show", abs(lag_ms) <= 50.0 and match > 0.95,
              f"{lag_ms:+.1f} ms from the programme position, correlation {match:.3f}, level {np.max(np.abs(played)):.2f} (track {np.max(np.abs(ref)):.2f})")
        vib = doc.track("vibration")
        assert vib is not None
        seg = sound[round((begin + 3.5 - a0) * AUDIO_RATE) : round((begin + 5.5 - a0) * AUDIO_RATE), 0]
        spectrum = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        peak = float(np.fft.rfftfreq(len(seg), 1.0 / AUDIO_RATE)[spectrum.argmax()])
        level = float(np.mean([vib.value_at(t) for t in np.arange(3.6, 5.4, 0.1)]))
        check("node1: the other channel carries the seat vibration tone at the track's level", abs(peak - 45.0) < 2.0 and abs(np.max(np.abs(seg)) - level) < 0.15,
              f"{peak:.1f} Hz, amplitude {np.max(np.abs(seg)):.2f} for a track level of {level:.2f}")
        quiet_before = float(np.max(np.abs(sound[: max(1, round((begin - 0.5 - a0) * AUDIO_RATE))]))) < 1e-3
        other = node2.audio()[1]
        check("before the show, and on the node without audio output, there is no sound",
              quiet_before and (other.size == 0 or float(np.max(np.abs(other))) == 0.0))
        for name, s in summaries.items():
            check(f"{name}: no invalid packet, no gap in the sound, no line time-out during the show",
                  s["invalid"] == 0 and s["audio_gaps"] == 0 and s["audio_underruns"] == 0 and s["dmx_timeouts"] == 0, json.dumps(
                      {k: s[k] for k in ("packets", "invalid", "audio_gaps", "audio_underruns", "dmx_timeouts")}))

        log("7/7 emergency stop, unplugging, pause, stop")

        def lit_between(node: Node, a: float, b: float) -> int:
            times, rows = node.dmx()
            return int(frame_at(times, rows, np.arange(a, b, 0.01)).max()) if b > a else 0

        def loud_between(a: float, b: float) -> float:
            seg = sound[max(0, round((a - a0) * AUDIO_RATE)) : max(0, round((b - a0) * AUDIO_RATE))]
            return float(np.max(np.abs(seg))) if len(seg) else 0.0

        check("emergency stop at node1: its line dark and its shaker silent within 0.1 s, for as long as it lasts",
              lit_between(node1, t_estop + 0.1, t_release - 0.05) == 0 and loud_between(t_estop + 0.1, t_release - 0.05) < 1e-3 and inhibited,
              f"line {lit_between(node1, t_estop + 0.1, t_release - 0.05)}, sound {loud_between(t_estop + 0.1, t_release - 0.05):.4f}, reported to the controller: {inhibited}")
        check("... the other node is not touched by it, and after the release node1's effects and sound return",
              lit_between(node2, t_estop + 0.1, t_release) > 0 and lit_between(node1, t_release + 0.3, t_unplug) > 0 and loud_between(t_release + 0.5, t_unplug) > 0.02)
        gone = {n["host"] for n in (after_unplug.get("nodes") or []) if n.get("present")}
        check("node2 unplugged: the show goes on - its platform runs, nothing is reported as an error",
              gone == {HOSTS["PLATFORM-01"]} and after_unplug.get("errors") == 0 and after_unplug.get("output_errors") == 0
              and len(still) == 3 and all(p["state"] == "RUNNING" for p in still), f"nodes present: {sorted(gone)}")
        check("node2 plugged in again: found and driven again within three seconds",
              lit_between(node2b, node2b.started + 0.2, node2b.started + 3.0) > 0, f"its line carries up to DMX {lit_between(node2b, node2b.started + 0.2, t_pause)}")
        check("PAUSE: both lines dark and the shaker silent", lit_between(node1, t_pause + 0.8, t_pause + 1.9) == 0
              and lit_between(node2b, t_pause + 0.8, t_pause + 1.9) == 0 and loud_between(t_pause + 0.8, t_pause + 1.9) < 1e-3)
        wind = doc.track("wind")
        expect_wind = wind is not None and min(wind.value_at(resumed_at - 0.9), wind.value_at(resumed_at + 0.9)) > 0.05
        check("RESUME: the effects continue with the programme", (lit_between(node1, t_resumed - 0.5, t_resumed + 1.0) > 0) or not expect_wind,
              f"position {resumed_at:.1f} s, wind {'expected' if expect_wind else 'not expected'}")
        check("STOP: dark and silent", lit_between(node1, t_stop + 0.8, t_stop + 1.4) == 0 and lit_between(node2b, t_stop + 0.8, t_stop + 1.4) == 0
              and loud_between(t_stop + 0.8, t_stop + 1.4) < 1e-3 and final.get("errors") == 0)
        text = (work / "logs/controller.log").read_text(errors="replace")
        check("show service log without errors", "Traceback" not in text and '"level": "ERROR"' not in text and " ERROR " not in text)

        log("after the show: the venue check with nobody sending (two boards switched on again)")
        nodes.append(Node(binary, work, "node1c", HOSTS["PLATFORM-01"], fx_port, 1, dmx_ports=1, audio_channels=2))
        nodes.append(Node(binary, work, "node2c", HOSTS["PLATFORM-02"], fx_port, 2, dmx_ports=1, audio_channels=0))
        time.sleep(0.5)
        said = tool(*venue_check, ok=(1,))
        (work / "logs/venue-check-idle.txt").write_text(said)
        dark = [line.strip() for line in said.splitlines() if "went dark by itself" in line]
        check("fourdvr fx ready with nobody sending: each board's DMX line is seen to go dark by itself when the frames stop",
              len(dark) == 2 and "still active" not in said and "left out" not in said, "; ".join(d.split("signal loss: ")[-1] for d in dark))
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for n in nodes:
            try:
                n.kill()
            except (OSError, subprocess.SubprocessError):
                pass
    passed = bool(checks) and all(c["ok"] for c in checks)
    report = {"result": "PASSED" if passed else "FAILED", "passed": sum(c["ok"] for c in checks), "checks": checks,
              "nodes": "firmware core (fxnode_sim)" if nodes and nodes[0].proc is not None else "Python stand-in"}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    log(f"RESULT: {report['result']} ({report['passed']}/{len(checks)} checks; nodes: {report['nodes']})" + ("" if passed or not args.work else f" - logs in {work / 'logs'}"))
    if not args.work and passed:
        shutil.rmtree(work, ignore_errors=True)
    elif not passed:
        log(f"working directory kept: {work}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
