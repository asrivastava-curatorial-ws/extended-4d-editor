"""Seat buttons on the seats' own boards: from a (simulated) ESP32 over the effects link into the session."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any

import pytest

from fourdvr_fx import bridge as br
from fourdvr_fx.patch import patch_from_dict
from fourdvr_play.engine import PlayEngine
from fourdvr_play.hub import Hub
from play_helpers import make_cfg

HOSTS = ["127.0.0.1", "127.0.0.2"]
BLOCKS = [{"id": "fire", "type": "on_input", "button": "a", "then": ["shot"]}, {"id": "shot", "type": "score", "points": 1.0},
          {"id": "ask", "type": "on_input", "button": "d", "then": ["q"]},
          {"id": "q", "type": "prompt", "text": "Go?", "choices": ["Yes", "No"], "seconds": 30.0, "mode": "vote", "then": {"b": ["no"]}},
          {"id": "no", "type": "set_state", "key": "answer", "value": "no"}]


def wait_for(cond: Callable[[], bool], timeout: float = 4.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def port() -> int:
    """A UDP port that is free on both loopback addresses (the boards all listen on the same port)."""
    socks: list[socket.socket] = []
    try:
        for host in HOSTS:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            socks.append(s)
            s.bind((host, socks[0].getsockname()[1] if len(socks) > 1 else 0))
        return int(socks[0].getsockname()[1])
    except OSError:
        pytest.skip("this system has no 127.0.0.2 loopback address")
    finally:
        for s in socks:
            s.close()


def test_a_button_on_a_seats_board_reaches_the_blocks(port: int) -> None:
    patch = patch_from_dict({"output": {"protocol": "esp32", "esp32": {
        "port": port, "platforms": {"PLATFORM-01": HOSTS[0], "PLATFORM-02": HOSTS[1]}}},
        "fixtures": [{"id": "fan", "effect": "wind", "scope": "platform", "address": 1}]}, "test")
    cfg = make_cfg(BLOCKS, seats=2, platforms=2, patch=patch)
    boards: list[Any] = []
    engine = PlayEngine(cfg, audio_backend="null")
    hub = Hub(cfg, engine)
    assert engine.seat_boards and engine.on_seat_inputs is not None
    try:
        boards = [br.SimNode(node_id=k + 1, host=HOSTS[k], port=port, dmx_ports=1, audio_channels=0, actuators=True, inputs=4).start()
                  for k in range(2)]
        engine.start()
        hub.command("start")
        out = engine.service.output                          # type: ignore[union-attr]
        assert wait_for(lambda: all(n.present(time.monotonic()) for n in out.nodes.values()))
        boards[0].press(0b0001)                              # "a" on seat A1's board
        assert wait_for(lambda: hub.seats["A1"].score == 1)
        boards[0].press(0)
        boards[1].press(0b0001)
        boards[1].press(0)
        boards[1].press(0b0001)
        assert wait_for(lambda: hub.seats["A2"].score == 2) and hub.seats["A1"].score == 1
        boards[1].press(0)
        boards[0].press(0b1000)                              # "d": asks everybody
        assert wait_for(lambda: len(hub.prompts) == 1)
        boards[0].press(0)
        boards[0].press(0b0010)                              # the boards' buttons answer: "b" on both seats
        boards[1].press(0b0010)
        assert wait_for(lambda: hub.state == {"answer": "no"}) and hub.prompts == {}
        assert hub.seats["A1"].score == 1 and hub.seats["A2"].score == 2        # answering did not fire "a" blocks
        assert all(rt.fault_reason == "" for rt in engine.runtimes)
    finally:
        engine.stop(settle_s=0.5)
        for board in boards:
            board.stop()


def test_the_firmwares_own_code_reports_a_button(port: int, tmp_path: Any) -> None:
    """The node is the firmware's portable core (lib/fourdvr_fx), built for this PC: its debouncing and its
    INPUT packets, received by the controller's real output."""
    import json
    import shutil
    import signal
    import subprocess
    from pathlib import Path

    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        pytest.skip("no C++ compiler here")
    firmware = Path(__file__).resolve().parents[2] / "firmware" / "esp32-actuator-bridge"
    core = firmware / "lib" / "fourdvr_fx" / "src"
    binary = tmp_path / "fxnode_sim"
    subprocess.run([compiler, "-std=c++17", "-O1", f"-I{core}", "-o", str(binary), str(firmware / "test" / "host" / "fxnode_sim.cpp"),
                    str(core / "fourdvr_fx.cpp")], check=True)
    node = subprocess.Popen([str(binary), "--host", HOSTS[0], "--port", str(port), "--node", "1", "--dmx-ports", "1",
                             "--audio-channels", "0", "--actuators", "--inputs", "4", "--log", str(tmp_path)],
                            stdout=subprocess.PIPE, text=True)
    out = br.BridgeOutput({101: (HOSTS[0], 0, 8)}, {HOSTS[0]: "PLATFORM-01"}, port)
    seen: list[tuple[str | None, int]] = []
    out.on_input = lambda platform, bits: seen.append((platform, bits))
    try:
        assert node.stdout is not None and "fxnode_sim: node 1" in node.stdout.readline()
        out.open()

        def tick() -> bool:
            out.send(101, bytes(512))
            out.flush()
            return True

        assert wait_for(lambda: tick() and out.nodes[HOSTS[0]].present(time.monotonic()))
        info = out.nodes[HOSTS[0]].info
        assert info is not None and info.has() == ["platform", "dmx", "buttons"] and info.firmware == br.FIRMWARE and info.inputs == 0
        node.send_signal(signal.SIGUSR2)                     # the button goes down
        assert wait_for(lambda: tick() and seen == [("PLATFORM-01", 1)])
        node.send_signal(signal.SIGUSR2)                     # and is let go
        assert wait_for(lambda: tick() and seen == [("PLATFORM-01", 1), ("PLATFORM-01", 0)])
        for _ in range(12):                                  # the repeats of the last report arrive: no third event
            tick()
            time.sleep(0.01)
        assert len(seen) == 2 and out.nodes[HOSTS[0]].input_reports == 2
    finally:
        out.close()
        node.send_signal(signal.SIGINT)
        node.wait(timeout=5)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["inputs"] == 4 and summary["input_changes"] == 2 and 4 <= summary["input_reports"] <= 6 and summary["invalid"] == 0
