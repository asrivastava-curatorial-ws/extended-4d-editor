#!/usr/bin/env python3
"""End-to-end test of the interactive mode on one PC, with the real programs and the real network paths.

Part A - a game session:
    fourdvr play run (two simulated platforms, a venue patch through ESP32s)  <- 4DPLAY over WebSocket ->
    a game, an operator and two players of this script, then `fourdvr play bots --game`;
    two seat boards = the firmware's own core built for this PC (fxnode_sim), with seat buttons.
Part B - questions inside a film (needs the Control Server: `fourdvr-server`, or --server CMD):
    Control Server plays a film -> show service (fourdvr controller run: platforms, the boards, their seat
    buttons passed on) and -> fourdvr play run --no-outputs (follows the film, asks in two "headsets").
    A decision point pauses the film, a seat button and a headset answer, the film goes on; a second vote
    starts another programme; a pause at the Control Server pauses the session; a block stops the film.

Without a C++ compiler the boards are the controller's Python stand-in (said in the output). Nothing here
needs hardware; nothing here proves a real board, headset or phone.

    python integration/run_play_integration.py [--fourdvr CMD] [--server CMD] [--skip-film] [--work DIR] [--keep]
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
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))      # the script next to this one, also for an isolated Python (-I)

from run_effects_integration import Api, free_port, log, make_video, wait_for

from fourdvr_motion import motionfile
from fourdvr_play.client import PlayClient, PlayClientError

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32-actuator-bridge"
PROFILE = ROOT / "config" / "platforms" / "simulation-6dof.toml"
PLATFORMS = ["PLATFORM-01", "PLATFORM-02"]
HOSTS = ["127.0.0.1", "127.0.0.2"]
KEY = "integration-operator-key"
ADMIN_PASSWORD = "Integration-Test-Passw0rd"

GAME_BLOCKS = """
[[play.blocks]]
id = "hit"
type = "on_event"
event = "hit"
then = ["hit-wind", "hit-points"]

[[play.blocks]]
id = "hit-wind"
type = "react"
accel = [-2.5, 0.0, 1.0]
seconds = 0.3
effects = { wind = 1.0 }
effect_seconds = 1.0

[[play.blocks]]
id = "hit-points"
type = "score"
points = -5.0

[[play.blocks]]
id = "button"
type = "on_input"
button = "a"
then = ["button-point"]

[[play.blocks]]
id = "button-point"
type = "score"
points = 1.0

[[play.blocks]]
id = "ask-at"
type = "on_time"
at = 14.0
then = ["route"]

[[play.blocks]]
id = "route"
type = "prompt"
text = "Which way?"
choices = ["Through the canyon", "Over the ridge"]
seconds = 5.0
mode = "vote"
then = { a = ["canyon"], b = ["ridge"], none = ["ridge"] }

[[play.blocks]]
id = "canyon"
type = "set_state"
key = "route"
value = "canyon"

[[play.blocks]]
id = "ridge"
type = "set_state"
key = "route"
value = "ridge"
"""

FILM_BLOCKS = """
[[play.blocks]]                  # a decision point: the film waits while the audience votes
id = "decision"
type = "on_time"
clock = "film"
programme = 1
at = 6.0
then = ["wait", "ask"]

[[play.blocks]]
id = "wait"
type = "film"
action = "pause"

[[play.blocks]]
id = "ask"
type = "prompt"
text = "Go on?"
choices = ["Go on", "Stop here"]
seconds = 20.0
mode = "vote"
then = { a = ["go-on"], b = ["halt"], none = ["go-on"] }

[[play.blocks]]
id = "go-on"
type = "film"
action = "resume"

[[play.blocks]]
id = "halt"
type = "film"
action = "stop"

[[play.blocks]]                  # a question over the running film; the majority may start another programme
id = "quiz-at"
type = "on_time"
clock = "film"
programme = 1
at = 10.0
then = ["quiz"]

[[play.blocks]]
id = "quiz"
type = "prompt"
text = "Switch to the other film?"
choices = ["Stay", "Switch"]
seconds = 10.0
mode = "vote"
then = { b = ["switch"] }

[[play.blocks]]
id = "switch"
type = "film"
action = "play"
programme = 2

[[play.blocks]]
id = "mark-at"
type = "on_time"
clock = "film"
programme = 2
at = 2.0
then = ["mark"]

[[play.blocks]]
id = "mark"
type = "set_state"
key = "second-film"
value = "yes"

[[play.blocks]]
id = "end-at"
type = "on_time"
clock = "film"
programme = 2
at = 9.0
then = ["the-end"]

[[play.blocks]]
id = "the-end"
type = "film"
action = "stop"
"""


class Board:
    """A seat's ESP32 with four seat buttons: the firmware's core as a program of this PC, or the Python stand-in."""

    def __init__(self, binary: Path | None, work: Path, name: str, host: str, port: int, node_id: int) -> None:
        self.dir = work / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen[str] | None = None
        self.sim: Any = None
        self.pressed = False
        if binary is not None:
            self.proc = subprocess.Popen([str(binary), "--host", host, "--port", str(port), "--node", str(node_id), "--dmx-ports", "1",
                                          "--audio-channels", "0", "--actuators", "--inputs", "4", "--log", str(self.dir)],
                                         stdout=subprocess.PIPE, text=True)
            assert self.proc.stdout is not None
            if "fxnode_sim" not in self.proc.stdout.readline():
                raise RuntimeError(f"{name} did not start")
        else:
            from fourdvr_fx.bridge import SimNode

            self.sim = SimNode(node_id, 1, 0, actuators=True, host=host, port=port, inputs=4).start()

    def button(self) -> None:
        """Press the first seat button, or let it go again."""
        self.pressed = not self.pressed
        if self.proc is not None:
            self.proc.send_signal(signal.SIGUSR2)
        else:
            self.sim.press(1 if self.pressed else 0)

    def tap(self) -> None:
        self.button()
        time.sleep(0.25)
        self.button()

    def finish(self) -> dict[str, Any]:
        if self.proc is not None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)
            summary = dict(json.loads((self.dir / "summary.json").read_text()))
            lit = [line.split() for line in (self.dir / "dmx.log").read_text().splitlines()]
            summary["wind_frames"] = sum(1 for _ms, port, hexa in lit if port == "0" and bytes.fromhex(hexa)[:1] not in (b"", b"\x00"))
            return summary
        self.sim.stop()
        return {"invalid": self.sim.invalid, "packets": self.sim.packets, "input_changes": self.sim.input_changes,
                "wind_frames": sum(1 for _t, port, frame in self.sim.dmx_history if port == 0 and frame[0])}


def stop(proc: subprocess.Popen[bytes], how: int = signal.SIGINT) -> int:
    if proc.poll() is None:
        proc.send_signal(how)
        try:
            return proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            return -9
    return int(proc.returncode)


def get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode())


def until(client: PlayClient, kind: str, timeout: float, **match: Any) -> dict[str, Any]:
    try:
        return client.wait_for(kind, timeout, **match)
    except PlayClientError as exc:
        raise TimeoutError(str(exc)) from exc


def flush(client: PlayClient) -> None:
    """Forget what has arrived so far: the next wait is for something new."""
    while client.recv(timeout=0.05) is not None:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fourdvr", default="fourdvr", help="the Editor's command")
    ap.add_argument("--server", default="fourdvr-server", help="Control Server command (part B)")
    ap.add_argument("--skip-film", action="store_true", help="part A only")
    ap.add_argument("--no-compiler", action="store_true", help="use the Python stand-in for the boards even if g++ is there")
    ap.add_argument("--work", help="working folder (default: a temporary one)")
    ap.add_argument("--keep", action="store_true", help="keep the working folder")
    args = ap.parse_args()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="fourdvr-play-it-"))
    (work / "logs").mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def tool(*cmd: str) -> str:
        res = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:4])} ... ended with {res.returncode}: {(res.stdout + res.stderr)[-600:]}")
        return res.stdout

    procs: list[subprocess.Popen[bytes]] = []
    boards: list[Board] = []
    clients: list[PlayClient] = []

    def client(url: str, role: str, seat: str | None = None, key: str = "") -> PlayClient:
        c = PlayClient(url, role=role, seat=seat, key=key)
        wait_for(lambda: bool(c.connect()), 20, f"the hub at {url}")
        clients.append(c)
        return c

    def new_boards(name: str, fx_port: int, binary: Path | None) -> list[Board]:
        made = [Board(binary, work, f"{name}-board{k + 1}", HOSTS[k], fx_port, k + 1) for k in range(2)]
        boards.extend(made)
        return made

    def patch_file(path: Path, fx_port: int) -> Path:
        path.write_text(
            f'[output]\nprotocol = "esp32"\nrefresh_hz = 40\n[output.esp32]\nport = {fx_port}\ntheatre_platform = "PLATFORM-01"\n'
            f'[output.esp32.platforms]\n"PLATFORM-01" = "{HOSTS[0]}"\n"PLATFORM-02" = "{HOSTS[1]}"\n'
            '[[fixtures]]\nid = "seat-fan"\neffect = "wind"\nscope = "platform"\naddress = 1\n')
        return path

    platforms_toml = "".join(f'\n[[platforms]]\nconfig = "{PROFILE}"\nid = "{pid}"\nlink_platform_id = {k + 1}\n' for k, pid in enumerate(PLATFORMS))
    seats_toml = "".join(f'\n[[play.seats]]\nid = "A{k + 1}"\nplatform = "{pid}"\nteam = "{("red", "blue")[k]}"\n' for k, pid in enumerate(PLATFORMS))
    try:
        # ================================================================== part A: a game session
        log("A1/5 two seat boards for this PC")
        binary: Path | None = None
        compiler = None if args.no_compiler else (shutil.which("g++") or shutil.which("c++"))
        if compiler:
            binary = work / "fxnode_sim"
            core = FIRMWARE / "lib" / "fourdvr_fx" / "src"
            tool(compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", f"-I{core}", "-o", str(binary),
                 str(FIRMWARE / "test" / "host" / "fxnode_sim.cpp"), str(core / "fourdvr_fx.cpp"))
            log("  the firmware's effects core, built for this PC (lib/fourdvr_fx)")
        else:
            log("  note: no C++ compiler - the boards are the controller's Python stand-in, not the firmware's core")
        fx_port = free_port(socket.SOCK_DGRAM)
        game_boards = new_boards("game", fx_port, binary)

        log("A2/5 the show: fourdvr play check, fourdvr play run")
        port = free_port()
        play_toml = work / "play.toml"
        play_toml.write_text(platforms_toml + f'\n[effects]\npatch = "{patch_file(work / "venue.toml", fx_port)}"\n'
                             f'\n[cueing]\ngain = 1.0\n\n[play]\ntitle = "Integration show"\noperator_key = "{KEY}"\ncountdown_s = 1\n'
                             f'results = "{work / "results"}"\n' + seats_toml + GAME_BLOCKS)
        listing = tool(args.fourdvr, "play", "check", "-c", str(play_toml), "--strict")
        (work / "logs/play-check.txt").write_text(listing)
        check("fourdvr play check --strict accepts the show and puts its blocks into words",
              "hit" in listing and "Which way?" in listing and "this configuration can run" in listing, f"{len(listing.splitlines())} lines")
        hub = subprocess.Popen([args.fourdvr, "play", "run", "-c", str(play_toml), "--port", str(port), "--bind", "127.0.0.1"],
                               stdin=subprocess.DEVNULL, stdout=(work / "logs/play-run.log").open("wb"), stderr=subprocess.STDOUT)
        procs.append(hub)
        base, ws = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws"
        info = wait_for(lambda: get_json(base + "/api/info"), 30, "the hub")
        check("the hub answers on its port", info["seats"] == ["A1", "A2"] and info["phase"] == "lobby", json.dumps(info))
        operator, game = client(ws, "operator", key=KEY), client(ws, "game")
        p1, p2 = client(ws, "player", "A1"), client(ws, "player", "A2")

        def status() -> dict[str, Any]:
            return dict(get_json(f"{base}/api/status?key={KEY}"))

        def nodes_present() -> bool:
            nodes = ((status()["engine"].get("effects") or {}).get("output") or {}).get("nodes") or []
            return len(nodes) == 2 and all(n["present"] and "button" in str(n.get("has", "")) for n in nodes)

        wait_for(nodes_present, 20, "both boards answering the show")
        check("both boards answer and say they have seat buttons", True)

        log("A3/5 a session: seat button on a board, a hit from the game, a rider's own settings, seat telemetry")
        operator.phase("start")
        until(p1, "phase", 10, phase="running")
        started = time.monotonic()
        wait_for(lambda: all(p["following"] for p in status()["engine"]["platforms"]), 20, "both platforms following")
        game_boards[0].button()
        pressed = until(operator, "input", 5, seat="A1", button="a", pressed=True)
        game_boards[0].button()
        until(operator, "input", 5, seat="A1", button="a", pressed=False)
        scored = until(p1, "score", 5, seat="A1")
        check("a seat button on a board (firmware core -> effects link -> show) scores for its seat",
              pressed["seat"] == "A1" and scored["score"] == 1, f"score {scored['score']}")
        game.event("hit", seat="A1")
        reaction = until(p1, "react", 5)
        hit = until(p1, "score", 5, seat="A1")
        check("the game's 'hit' pushes that seat and takes points", hit["score"] == -4 and reaction["seats"] == ["A1"]
              and reaction["effects"] == ["wind"], f"score {hit['score']}")
        time.sleep(1.5)
        p2.send("seat", blocked=["wind"], motion="gentle")
        wait_for(lambda: next(s for s in status()["session"]["seats"] if s["id"] == "A2")["blocked"] == ["wind"], 5, "the seat's own settings")
        game.event("hit", seat="A2")
        until(p2, "score", 5, seat="A2")
        time.sleep(1.5)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:          # the seat's own motion, as a game sends it
            for k in range(60):
                udp.sendto(json.dumps({"4dlive": 1, "seat": "A1", "accel": [0.0, 2.0 if k < 40 else 0.0, 0.0], "speed": 20.0}).encode(),
                           ("127.0.0.1", port))
                time.sleep(0.02)
        st = status()
        seat1, seat2 = (next(c for c in st["engine"]["play"]["seats"] if c["id"] == sid) for sid in ("A1", "A2"))
        check("seat telemetry over UDP (the open live format with a seat) reaches that seat only",
              st["network"]["telemetry"] == 60 and st["network"]["telemetry_bad"] == 0 and seat1["telemetry"] != "waiting for the source"
              and seat2["telemetry"] == "waiting for the source" and st["engine"]["play"]["reactions"] == 2,
              f"A1: {seat1['telemetry']}, A2: {seat2['telemetry']}, {st['network']['telemetry']} packets")

        log("A4/5 simulated players and a simulated game for 12 s (fourdvr play bots --game); the vote at 14 s")
        for c in (p1, p2, game):
            c.close()
        bots = subprocess.Popen([args.fourdvr, "play", "bots", "-c", str(play_toml), "--url", ws, "--game", "--seconds", "12", "--seed", "7"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        vote: dict[str, Any] | None = None
        while bots.poll() is None or vote is None:                            # the operator keeps reading, as its page does
            message = operator.recv(timeout=0.2)
            if message is not None and message["type"] == "prompt_end" and not message.get("cancelled"):
                vote = message
            if bots.poll() is not None and vote is None and time.monotonic() - started > 40.0:
                raise TimeoutError("the question of the blocks was never decided")
        said = bots.communicate()[0]
        (work / "logs/play-bots.txt").write_text(said)
        check("the bots played without an error", bots.returncode == 0, said.strip().splitlines()[-1][:160] if said.strip() else "")
        st = status()
        check("the question of the blocks was asked on time, answered and decided", vote["choices"] == ["Through the canyon", "Over the ridge"]
              and sum(vote["counts"]) >= 1 and st["session"]["state"].get("route") in ("canyon", "ridge"),
              f"answers {vote['counts']}, route {st['session']['state'].get('route')}")
        operator.phase("end")
        final = until(operator, "results", 5)
        saved = sorted((work / "results").glob("*.json"))
        check("End gives the results, and they are saved", len(final["seats"]) == 2 and len(saved) == 1
              and json.loads(saved[0].read_text())["seats"] == final["seats"], saved[0].name if saved else "no file")
        st = status()
        engine = st["engine"]
        check("the show kept its timing and nothing faulted",
              engine["late_ticks"] == 0 and not any(p["fault"] for p in engine["platforms"]) and not engine.get("problem")
              and st["session"]["logic"]["dropped"] == 0 and not st["session"]["logic"]["problem"] and st["session"]["dropped"] == 0,
              f"late ticks {engine['late_ticks']}, block runs {st['session']['logic']['runs']}, reactions {engine['play']['reactions']}")

        log("A5/5 the end of the show")
        for c in clients:
            c.close()
        clients.clear()
        code = stop(hub)
        check("fourdvr play run ends cleanly on Ctrl+C", code == 0, f"exit {code}")
        sums = [b.finish() for b in game_boards]
        boards.clear()
        check("the boards: no invalid packet, the seat button reported, wind only where it was meant",
              all(s["invalid"] == 0 and s["packets"] > 50 for s in sums) and sums[0]["input_changes"] >= 2
              and sums[0]["wind_frames"] >= 1 and sums[1]["wind_frames"] == 0,
              f"board 1: {sums[0]['packets']} packets, {sums[0]['input_changes']} button changes, wind in {sums[0]['wind_frames']} frame(s); "
              f"board 2 (seat blocked wind): wind in {sums[1]['wind_frames']}")

        # ================================================================== part B: questions inside a film
        server_cmd = shutil.which(args.server)
        if args.skip_film or not server_cmd:
            log("B  questions inside a film: SKIPPED" + ("" if args.skip_film else f" - no Control Server command '{args.server}' (--server CMD)"))
        else:
            log("B1/4 a film (two programmes) on the Control Server")
            video, motion = work / "film.mp4", work / "programme1.motion"
            make_video(video, 24.0)
            tool(args.fourdvr, "extract", "generate", str(video), "-o", str(motion), "--projection", "360", "--profile", str(PROFILE), "-q")
            second = work / "programme2.motion"
            m = motionfile.read_motion(motion)
            motionfile.write_motion(second, {**m.header, "programme_id": 2}, m.samples)
            sport = free_port()
            scfg = work / "server.toml"
            scfg.write_text(
                f'[server]\nhost = "127.0.0.1"\nport = {sport}\ndata_dir = "{work / "server-data"}"\nlog_level = "WARNING"\nlog_format = "text"\n'
                f'[security]\ninitial_admin_password = "{ADMIN_PASSWORD}"\n[devices]\nheartbeat_interval_s = 1.0\noffline_timeout_s = 5.0\n'
                "[playback]\nstart_lead_s = 2.0\ntransition_gap_s = 1.0\nprepare_timeout_s = 60.0\n")
            tool(server_cmd, "-c", str(scfg), "init")
            server = subprocess.Popen([server_cmd, "-c", str(scfg), "serve"], stdout=(work / "logs/server.log").open("wb"), stderr=subprocess.STDOUT)
            procs.append(server)
            api = Api(f"http://127.0.0.1:{sport}")
            health = wait_for(lambda: api.call("GET", "/api/v1/health", timeout=2), 30, "the Control Server")
            api.token = api.call("POST", "/api/v1/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]
            for pid, path in ((1, motion), (2, second)):
                api.call("POST", "/api/v1/programmes", {"programme_id": pid, "name": f"Film {pid}", "projection": "360", "stereo": "mono", "loop": True})
                api.call("PUT", f"/api/v1/programmes/{pid}/video?filename={video.name}", raw=video.read_bytes())
                api.call("PUT", f"/api/v1/programmes/{pid}/motion?filename={path.name}", raw=path.read_bytes())
                res = api.call("POST", f"/api/v1/programmes/{pid}/validate")
                assert res["passed"], res
                api.call("POST", f"/api/v1/programmes/{pid}/publish")
            check(f"{health['product']} {health['version']} has two programmes", True)

            log("B2/4 show service (platforms, the boards, their seat buttons passed on) and the hub that follows the film")
            fx_port, port = free_port(socket.SOCK_DGRAM), free_port()
            film_boards = new_boards("film", fx_port, binary)
            base, ws = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws"
            keys = dict(line.split("\t") for line in tool(server_cmd, "-c", str(scfg), "device-key", *PLATFORMS).strip().splitlines())
            ctl = work / "controller.toml"
            ctl.write_text(
                f'[controller]\nserver_url = "ws://127.0.0.1:{sport}/ws/v1/device"\ncontrol_rate_hz = 100.0\ncache_dir = "{work / "cache"}"\n'
                'status_interval_s = 0.5\ntime_sync_interval_s = 2.0\nlog_level = "INFO"\n'
                + "".join(f'\n[[platforms]]\nconfig = "{PROFILE}"\nid = "{pid}"\nlink_platform_id = {k + 1}\ndevice_key = "{keys[pid]}"\n'
                          for k, pid in enumerate(PLATFORMS))
                + f'\n[effects]\npatch = "{patch_file(work / "film-venue.toml", fx_port)}"\nbuttons_to = "{ws}"\n')
            listing = tool(args.fourdvr, "controller", "check-config", "-c", str(ctl))
            check("fourdvr controller check-config names the hub the seat buttons go to", f"interactive show at {ws}" in listing)
            (work / "server-password.txt").write_text(ADMIN_PASSWORD + "\n")
            film_toml = work / "film.toml"
            film_toml.write_text(platforms_toml + f'\n[play]\ntitle = "Integration film"\noperator_key = "{KEY}"\n'
                                 f'results = "{work / "film-results"}"\n\n[play.film]\nserver = "http://127.0.0.1:{sport}"\nuser = "admin"\n'
                                 'password_file = "server-password.txt"\npoll_s = 0.1\n' + seats_toml + FILM_BLOCKS)
            tool(args.fourdvr, "play", "check", "-c", str(film_toml), "--strict")
            hub = subprocess.Popen([args.fourdvr, "play", "run", "-c", str(film_toml), "--no-outputs", "--port", str(port), "--bind", "127.0.0.1"],
                                   stdin=subprocess.DEVNULL, stdout=(work / "logs/film-hub.log").open("wb"), stderr=subprocess.STDOUT)
            procs.append(hub)
            wait_for(lambda: get_json(base + "/api/info"), 30, "the hub")
            service = subprocess.Popen([args.fourdvr, "controller", "run", "-c", str(ctl)], stdout=(work / "logs/controller.log").open("wb"),
                                       stderr=subprocess.STDOUT)
            procs.append(service)
            wait_for(lambda: len([p for p in api.call("GET", "/api/v1/devices", timeout=5)["motion_platforms"]
                                  if p["connection"] == "CONNECTED" and p["state"] == "READY"]) == 2, 60, "two platforms READY")
            operator = client(ws, "operator", key=KEY)
            h1, h2 = client(ws, "headset", "A1"), client(ws, "headset", "A2")
            wait_for(lambda: status()["session"]["clients"].get("game") == 1 and status()["session"]["film"]["ok"], 20,
                     "the show service's button relay and the film link")
            check("the hub follows the Control Server; the show service is connected to pass seat buttons on",
                  h1.welcome["film"] is True and status()["session"]["phase"]["phase"] == "lobby")

            def film() -> dict[str, Any]:
                return dict(api.call("GET", "/api/v1/playback/state", timeout=5))

            log("B3/4 PLAY at the Control Server: a decision point, a vote for another programme, a pause, the end")
            api.call("POST", "/api/v1/playback/play", {"programme_id": 1})
            ahead = until(h1, "phase", 30, phase="countdown")
            until(h1, "phase", 30, phase="running")
            pos = float(film()["position_s"])
            check("the film starts the session (no operator at the hub): a countdown over the Control Server's start lead, then running",
                  status()["session"]["sessions"] == 1 and 0.5 <= ahead["seconds"] <= 2.1 and -0.3 <= pos <= 0.6,
                  f"countdown from {ahead['seconds']:.1f} s; running at film position {pos:.2f} s")
            asked = until(h1, "prompt", 30)
            wait_for(lambda: film()["timeline"]["state"] == "PAUSED", 5, "the film paused by the blocks")
            time.sleep(0.6)
            st, pos = status()["session"], float(film()["position_s"])
            check("at 6 s of the film the question appears in the headsets and the film waits, the session goes on",
                  asked["text"] == "Go on?" and 6.0 <= pos <= 7.5 and st["film"]["held"] is True and st["phase"]["phase"] == "running",
                  f"film stands at {pos:.2f} s")
            film_boards[0].tap()                                         # seat A1 answers with the button on its board: "a"
            until(h1, "answered", 5, choice=0)
            h2.answer("a")
            decided = until(h1, "prompt_end", 5, id=asked["id"])
            wait_for(lambda: film()["timeline"]["state"] == "PLAYING" and status()["session"]["film"]["held"] is False, 8, "the film going on")
            went_on = float(status()["session"]["film"]["position_s"])
            check("a seat button (board -> show service -> hub) and a headset answer; the vote lets the film go on from where it stood",
                  decided["winner"] == 0 and decided["counts"] == [2, 0] and pos - 0.05 <= went_on <= pos + 1.0,
                  f"answers {decided['counts']}, goes on at {went_on:.1f} s")
            quiz = until(h2, "prompt", 20, text="Switch to the other film?")
            pos = float(film()["position_s"])
            check("at 10 s a question is asked over the running film", quiz["text"] == "Switch to the other film?" and 10.0 <= pos <= 11.5
                  and film()["timeline"]["state"] == "PLAYING", f"film at {pos:.2f} s")
            h1.answer(1)
            h2.answer("b")
            until(h1, "prompt_end", 5, id=quiz["id"])
            wait_for(lambda: (lambda s: s["timeline"]["state"] == "PLAYING" and s["timeline"]["programme_id"] == 2)(film()), 60, "programme 2 PLAYING")
            until(h1, "state", 20, key="second-film")
            st = status()["session"]
            check("the vote started programme 2: a new session, and its own cue fired", st["sessions"] == 2 and st["state"] == {"second-film": "yes"}
                  and st["film"]["programme"] == 2, f"sessions {st['sessions']}")
            flush(h1)
            api.call("POST", "/api/v1/playback/pause", {})
            until(h1, "phase", 10, phase="paused")
            st = status()["session"]
            api.call("POST", "/api/v1/playback/resume", {})
            until(h1, "phase", 10, phase="running")
            check("paused at the Control Server: the session waits (and is not 'held by the blocks'), then goes on", st["film"]["held"] is False)
            until(h1, "phase", 30, phase="results")
            wait_for(lambda: film()["timeline"]["state"] != "PLAYING", 5, "the film stopped by the blocks")
            check("the block at 9 s of programme 2 stops the film; the session ends with results",
                  len(sorted((work / "film-results").glob("*.json"))) == 2, f"Control Server: {film()['timeline']['state']}")

            log("B4/4 the end")
            st = status()
            check("the film session: no block was left out, no message dropped",
                  st["session"]["logic"]["dropped"] == 0 and not st["session"]["logic"]["problem"] and st["session"]["dropped"] == 0,
                  f"block runs {st['session']['logic']['runs']}")
            for c in clients:
                c.close()
            clients.clear()
            codes = (stop(hub), stop(service, signal.SIGTERM))
            check("hub and show service end cleanly", codes == (0, 0), f"exit codes {codes}")
            sums = [b.finish() for b in film_boards]
            boards.clear()
            check("the film's boards: no invalid packet, the seat button reported", all(s["invalid"] == 0 for s in sums)
                  and sums[0]["input_changes"] >= 2, f"board 1: {sums[0]['input_changes']} button changes")
            errors = [line for line in (work / "logs/controller.log").read_text(errors="replace").splitlines() if " ERROR " in line or "Traceback" in line]
            check("the show service logged no error", not errors, errors[0][:200] if errors else "")
    except Exception as exc:  # noqa: BLE001 - whatever stops the run is a failed check, with the logs kept
        check(f"the run was stopped: {type(exc).__name__}", False, str(exc)[:600])
        args.keep = True
    finally:
        for c in clients:
            c.close()
        for proc in reversed(procs):
            stop(proc, signal.SIGTERM)
        for board in boards:
            try:
                board.finish()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    failed = [r for r in results if not r[1]]
    log(f"\n{len(results) - len(failed)} of {len(results)} checks passed" + (f"; logs in {work}" if failed or args.keep else ""))
    (work / "report.json").write_text(json.dumps([{"check": n, "passed": ok, "detail": d} for n, ok, d in results], indent=1))
    if not (failed or args.keep):
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
