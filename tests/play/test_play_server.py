"""The hub on the network: pages, the WebSocket protocol, seat telemetry over UDP, bots, the command line."""

from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from fourdvr_fx.dmx import CaptureOutput
from fourdvr_play import cli
from fourdvr_play.bots import Bots
from fourdvr_play.client import PlayClient, PlayClientError
from fourdvr_play.config import EXAMPLE, load_play_config
from fourdvr_play.engine import PlayEngine
from fourdvr_play.hub import Hub, To
from fourdvr_play.server import PlayServer
from play_helpers import KEY, PROFILE, make_cfg

BLOCKS = [{"id": "hit", "type": "on_event", "event": "hit", "then": ["jolt", "pts"]},
          {"id": "jolt", "type": "react", "accel": [-2.5, 0.0, 1.0], "seconds": 0.25, "effects": {"vibration": 1.0}},
          {"id": "pts", "type": "score", "points": -5.0},
          {"id": "fire", "type": "on_input", "button": "a", "then": ["shot"]}, {"id": "shot", "type": "send", "event": "fire"}]


class Show:
    def __init__(self, platforms: int = 2) -> None:
        self.cfg = make_cfg(BLOCKS, seats=2, platforms=platforms, bind="127.0.0.1")
        self.engine = PlayEngine(self.cfg, audio_backend="null", effects_output=CaptureOutput())
        self.hub = Hub(self.cfg, self.engine)
        self.server = PlayServer(self.cfg, self.hub, self.engine, port=0)

    def start(self) -> None:
        self.server.start()
        self.engine.start()

    def stop(self) -> None:
        self.server.stop()
        self.engine.stop(settle_s=0.5)

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    @property
    def ws(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}/ws"

    def client(self, role: str, seat: str | None = None, **kw: Any) -> PlayClient:
        client = PlayClient(self.ws, role=role, seat=seat, key=KEY if role == "operator" else "", **kw)
        client.connect()
        return client


@pytest.fixture
def show() -> Iterator[Show]:
    s = Show()
    s.start()
    try:
        yield s
    finally:
        s.stop()


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5.0) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def wait_until(condition: Any, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


def until(client: PlayClient, kind: str, wanted: Any, seconds: float = 5.0) -> dict[str, Any]:
    """The first message of this type for which `wanted` holds (earlier ones may have been waiting)."""
    end = time.monotonic() + seconds
    while True:
        message = client.wait_for(kind, timeout=max(0.1, end - time.monotonic()))
        if wanted(message):
            return message


def test_the_pages_are_served_and_the_status_needs_the_key(show: Show) -> None:
    for path, mark in (("/", b"Take this seat"), ("/operator", b"Operator key"), ("/scoreboard", b"<title>Scoreboard"),
                       ("/assets/play.css", b"JetBrains Mono"), ("/assets/play.js", b"const Play")):
        status, headers, body = get(show.http + path)
        assert status == 200 and mark in body, path
        assert headers["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store" and int(headers["Content-Length"]) == len(body)
    status, headers, body = get(show.http + "/assets/font.ttf")
    assert status == 200 and headers["Content-Type"] == "font/ttf" and len(body) > 100_000
    status, _h, body = get(show.http + "/api/info")
    assert status == 200 and json.loads(body) == {"title": "Interactive show", "play": 1, "seats": ["A1", "A2"], "phase": "lobby"}
    assert get(show.http + "/api/status")[0] == 403 and get(show.http + "/api/status?key=wrong")[0] == 403
    status, _h, body = get(show.http + f"/api/status?key={KEY}")
    st = json.loads(body)
    assert status == 200 and st["session"]["phase"]["phase"] == "lobby" and st["network"]["connections"] == 0
    assert [p["id"] for p in st["engine"]["platforms"]] == ["PLATFORM-01", "PLATFORM-02"] and st["engine"]["play"]["live"] is False
    assert get(show.http + "/nothing-here")[0] == 404 and get(show.http + "/../etc/passwd")[0] == 404


def test_a_whole_session_over_the_network(show: Show) -> None:
    op, p1, p2, game, board = (show.client("operator"), show.client("player", "A1", name="Asha"), show.client("headset", "A2"),
                               show.client("game"), show.client("display"))
    try:
        assert p1.welcome["seat"] == "A1" and op.welcome["role"] == "operator" and board.welcome["phase"]["phase"] == "lobby"
        p1.ready()
        assert until(board, "seats", lambda m: any(s["ready"] and s["name"] == "Asha" for s in m["seats"]))
        op.phase("start")
        assert board.wait_for("phase", phase="running")["time"] < 0.5
        assert wait_until(lambda: show.engine.gate == 1.0)

        game.event("hit", seat="A1", value=0.5)
        assert p1.wait_for("score") == {"type": "score", "seat": "A1", "score": -5, "delta": -5}
        assert p1.wait_for("react")["effects"] == ["vibration"]
        assert wait_until(lambda: show.engine.channels["A1"].reactions == 1) and show.engine.channels["A2"].reactions == 0

        p2.input("a")                                              # a tap on the headset's side of seat A2
        heard = game.wait_for("event", name="fire")
        assert heard["seat"] == "A2" and heard["from"] == "logic"
        assert game.wait_for("input", seat="A2", pressed=False)["button"] == "a"

        op.send("prompt", text="Go on?", choices=["Yes", "No"], seconds=20, mode="vote")
        asked = p1.wait_for("prompt")
        assert asked["text"] == "Go on?" and p2.wait_for("prompt")["id"] == asked["id"] and board.wait_for("prompt")["mode"] == "vote"
        p1.answer("a", prompt=asked["id"])
        p2.answer(0)
        end = p1.wait_for("prompt_end")
        assert end["counts"] == [2, 0] and end["winner"] == 0 and end["yours"] == 0 and board.wait_for("prompt_end")["winner"] == 0

        status = until(op, "status", lambda m: m["engine"]["play"]["reactions"] == 1)       # the operator is kept up to date
        assert status["session"]["clients"] == {"operator": 1, "player": 1, "headset": 1, "game": 1, "display": 1}
        assert status["engine"]["play"]["reactions"] == 1 and status["network"]["connections"] == 5

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # seat A2's telemetry, the open live format over UDP
        try:
            for _ in range(60):
                udp.sendto(json.dumps({"4dlive": 1, "seat": "A2", "accel": [2.0, 0.0, 0.0], "speed": 12.0}).encode(),
                           ("127.0.0.1", show.server.port))
                time.sleep(0.01)
            udp.sendto(b"not json", ("127.0.0.1", show.server.port))
            udp.sendto(json.dumps({"4dlive": 1, "seat": "Z9"}).encode(), ("127.0.0.1", show.server.port))
            udp.sendto(json.dumps({"4dlive": 1, "accel": [1, 0, 0]}).encode(), ("127.0.0.1", show.server.port))
        finally:
            udp.close()
        assert wait_until(lambda: show.server.telemetry == 60 and show.server.telemetry_bad == 3)
        assert show.engine.channels["A2"].guard.accepted == 60 and show.engine.channels["A1"].guard.accepted == 0
        game.motion("A1", accel=[0.0, 1.0, 0.0])                    # or over the WebSocket
        assert wait_until(lambda: show.engine.channels["A1"].guard.accepted == 1)

        op.phase("end")
        results = p1.wait_for("results")
        assert [(r["id"], r["score"]) for r in results["seats"]] == [("A2", 0), ("A1", -5)] and results["asked"][0]["winner"] == "Yes"
        assert wait_until(lambda: show.engine.gate == 0.0, 3.0)
        assert all(rt.fault_reason == "" for rt in show.engine.runtimes)
    finally:
        for client in (op, p1, p2, game, board):
            client.close()
    assert wait_until(lambda: show.hub.clients == {}) and show.hub.seats["A1"].clients == 0


def test_who_does_not_say_hello_properly_is_turned_away(show: Show) -> None:
    with pytest.raises(PlayClientError, match="the operator key is not right"):
        PlayClient(show.ws, role="operator", key="guess-again").connect()
    with pytest.raises(PlayClientError, match="there is no seat 'Z9'"):
        PlayClient(show.ws, role="player", seat="Z9").connect()
    with connect(show.ws) as raw:                                     # something else than a hello
        raw.send(json.dumps({"type": "event", "name": "hit"}))
        assert json.loads(raw.recv(timeout=3))["code"] == "hello"
        with pytest.raises(ConnectionClosed) as closed:
            raw.recv(timeout=3)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 4001
    with connect(show.ws) as raw:                                     # not JSON at all
        raw.send("hello?")
        with pytest.raises(ConnectionClosed) as closed:
            raw.recv(timeout=3)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 4000
    game = show.client("game")
    try:
        assert game._ws is not None
        for _ in range(3):
            game._ws.send("{not json")
            assert game.wait_for("error")["code"] == "bad_message"
        game._ws.send("x" * 20_000)                                   # far too long: the connection is closed
        with pytest.raises(PlayClientError, match="connection lost"):
            for _ in range(50):
                game.recv(timeout=0.2)
    finally:
        game.close()
    assert wait_until(lambda: show.hub.clients == {})


def test_a_client_that_does_not_read_is_dropped_and_the_others_go_on() -> None:
    cfg = make_cfg(seats=1)
    hub = Hub(cfg)
    server = PlayServer(cfg, hub, port=0)
    closed: list[tuple[int, str]] = []

    class Stuck:
        async def close(self, code: int, reason: str) -> None:
            closed.append((code, reason))

    async def scenario() -> tuple[int, int]:
        slow, _w = hub.join({"type": "hello", "play": 1, "role": "display"})
        fast, _w = hub.join({"type": "hello", "play": 1, "role": "display"})
        full: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        roomy: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        server._queues = {slow.id: (full, Stuck(), slow), fast.id: (roomy, Stuck(), fast)}       # type: ignore[dict-item]
        for k in range(5):
            server._post(To(everyone=True), {"type": "state", "key": "k", "value": k})
        await asyncio.sleep(0)
        return roomy.qsize(), len(server._queues)

    delivered, left = asyncio.run(scenario())
    assert delivered == 5 and left == 1 and server.kicked == 1 and closed == [(4003, "too slow")]


def test_bots_play_a_session_and_nothing_faults(show: Show) -> None:
    bots = Bots(show.cfg, show.ws, game=True, seed=5, press_every_s=0.4)
    op = show.client("operator")
    bots.start()
    try:
        assert wait_until(lambda: all(s.ready for s in show.hub.seats.values()))
        op.phase("start")
        assert wait_until(lambda: bots.sent["events"] >= 1 and bots.sent["inputs"] >= 4 and bots.sent["motion"] > 100, 12.0)
        op.send("prompt", text="Bots?", choices=["Yes", "No", "Maybe"], seconds=6)
        end = op.wait_for("prompt_end", timeout=8.0)
        assert sum(end["counts"]) == 2
        op.phase("end")
        assert wait_until(lambda: bots.results is not None)
    finally:
        bots.stop()
        op.close()
    assert bots.errors == [] and sum(s.answers for s in show.hub.seats.values()) == 2      # by choice or by a seat button
    assert all(ch.guard.accepted > 50 for ch in show.engine.channels.values())
    assert sum(s.events for s in show.hub.seats.values()) == bots.sent["events"]
    status = show.engine.status()
    assert all(p["fault"] is None for p in status["platforms"]) and status["play"]["reactions"] >= 1
    assert status["late_ticks"] == 0, status["max_late_ms"]


def test_the_command_line(show: Show, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "play.toml"
    assert cli.main(["example-config", "-o", str(config)]) == 0 and cli.main(["example-config", "-o", str(config)]) == 1
    assert "exists already" in capsys.readouterr().err
    text = EXAMPLE.replace('controller = "controller.toml"', "# controller").replace('platform = "PLATFORM-01"', "").replace(
        'platform = "PLATFORM-02"', "")
    config.write_text(text + f'\n[[platforms]]\nconfig = "{PROFILE}"\nid = "P1"\n', encoding="utf-8")
    assert cli.main(["check", "-c", str(config)]) == 0
    out = capsys.readouterr().out
    assert "seat        A1  no platform, team red" in out and "-> [a] tell the game: event “route.canyon”: all" in out
    assert "this configuration can run" in out and "platform    P1" in out
    config.write_text(text.replace('type = "score"', 'type = "scores"'), encoding="utf-8")
    assert cli.main(["check", "-c", str(config)]) == 1
    assert "block hit-points: unknown type 'scores'" in capsys.readouterr().err
    assert cli.main(["blocks", "--settings"]) == 0
    out = capsys.readouterr().out
    assert "  prompt     Ask - " in out and "outputs: a, b, c, d, none" in out and "      seconds = 10.0" in out

    url = ["--url", show.ws]
    assert cli.main(["send", "phase", "start", "--key", KEY, *url]) == 0
    assert wait_until(lambda: show.hub.phase == "running")
    assert cli.main(["send", "event", "hit", "--seat", "A2", *url]) == 0 and cli.main(["send", "input", "a", "--seat", "A1", *url]) == 0
    assert cli.main(["send", "state", "door", "open", *url]) == 0
    assert cli.main(["send", "react", '{"accel": [0, 0, 2], "seconds": 0.2}', "--seat", "A1", *url]) == 0
    assert wait_until(lambda: show.hub.seats["A2"].score == -5 and show.hub.state == {"door": "open"})
    assert show.hub.seats["A1"].inputs == 2 and show.hub.seats["A1"].reactions == 1
    capsys.readouterr()
    assert cli.main(["send", "event", "hit", "--seat", "Z9", *url]) == 1 and cli.main(["send", "phase", "start", "--key", "wrong-key", *url]) == 1
    err = capsys.readouterr().err
    assert "there is no seat 'Z9'" in err and "the operator key is not right" in err
    assert cli.main(["status", "--url", show.http, "--key", KEY]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "seats 0/2 connected" in out and "A2" in out
    assert cli.main(["status", "--url", show.http, "--key", "nope"]) == 1
    assert cli.main(["status", "--url", "http://127.0.0.1:9", "--key", KEY]) == 1
    assert "does not answer" in capsys.readouterr().err


def test_the_godot_addon_is_written_into_a_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["addon", "-o", str(tmp_path)]) == 1
    assert "is not a Godot project" in capsys.readouterr().err
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    assert cli.main(["addon", "-o", str(tmp_path)]) == 0
    addon = tmp_path / "addons" / "fourd_theatre"
    names = sorted(p.relative_to(addon).as_posix() for p in addon.rglob("*") if p.is_file())
    assert names == ["README.md", "example/fourd_example.gd", "fourd.gd", "fourd_link.gd", "fourd_seat_motion.gd",
                     "plugin.cfg", "plugin.gd"]
    assert 'script="plugin.gd"' in (addon / "plugin.cfg").read_text() and "wrote 7 files" in capsys.readouterr().out
    link = (addon / "fourd_link.gd").read_text(encoding="utf-8")
    assert "const PROTOCOL := 1" in link and "\t" in link                  # GDScript is indented with tabs: kept as they are
    assert cli.main(["addon", "-o", str(tmp_path)]) == 0                    # again: the files are written over


def test_the_example_configuration_is_a_show_that_runs(tmp_path: Path) -> None:
    config = tmp_path / "play.toml"
    config.write_text(EXAMPLE.replace('controller = "controller.toml"', "# controller")
                      + f'\n[[platforms]]\nconfig = "{PROFILE}"\nid = "PLATFORM-01"\n\n[[platforms]]\nconfig = "{PROFILE}"\n'
                      'id = "PLATFORM-02"\nlink_platform_id = 2\n', encoding="utf-8")
    cfg = load_play_config(config)
    assert [(s.id, s.platform, s.team) for s in cfg.seats] == [("A1", "PLATFORM-01", "red"), ("A2", "PLATFORM-02", "blue")]
    assert cfg.live.source == {"type": "none"} and cfg.countdown_s == 5.0 and cfg.notes == [] and len(cfg.graph.blocks) == 10
    assert "seat        A2  platform PLATFORM-02, team blue" in cfg.describe()


def test_the_bench_example_needs_nothing_else(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "play.toml"
    assert cli.main(["example-config", "--bench", "-o", str(config)]) == 0
    assert "--simulate" in capsys.readouterr().out and (tmp_path / "bench-platform.toml").exists()
    cfg = load_play_config(config)
    assert [p.platform_id for p in cfg.live.platforms] == ["PLATFORM-01", "PLATFORM-02"]
    assert [s.platform for s in cfg.seats] == ["PLATFORM-01", "PLATFORM-02"] and len(cfg.graph.blocks) == 10
    assert cli.main(["check", "-c", str(config), "--strict"]) == 0
    assert cli.main(["example-config", "--bench"]) == 1                      # two files: it needs a place
    assert cli.main(["example-config", "--bench", "-o", str(config)]) == 1   # nothing is written over unasked
