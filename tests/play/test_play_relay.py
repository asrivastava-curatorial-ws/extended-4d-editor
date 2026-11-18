"""Seat buttons during a film: the show service owns the boards and passes their buttons on to the hub."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fourdvr_fx import bridge as br
from fourdvr_motion.config import ControllerConfigError, EffectsSettings, load_controller_config
from fourdvr_motion.effects_link import EffectsLink
from fourdvr_play import relay as relay_module
from fourdvr_play.hub import Hub
from fourdvr_play.relay import ButtonRelay
from fourdvr_play.server import PlayServer
from play_helpers import PROFILE, Session, make_cfg

BLOCKS = [{"id": "fire", "type": "on_input", "button": "a", "then": ["shot"]}, {"id": "shot", "type": "score", "points": 1.0}]


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Show:
    def __init__(self, port: int = 0, **play: Any) -> None:
        self.cfg = make_cfg(BLOCKS, seats=2, platforms=2, bind="127.0.0.1", **play)
        self.hub = Hub(self.cfg, None)
        self.server = PlayServer(self.cfg, self.hub, None, port=port, udp=False)
        self.server.start()
        self.hub.command("start")
        self.url = f"ws://127.0.0.1:{self.server.port}/ws"


def test_the_hub_takes_board_inputs_from_a_show_service_only() -> None:
    s = Session(BLOCKS, platforms=2)
    service, player = s.join("game"), s.join("player", "A2")
    s.start()
    s.hub.handle(service, {"type": "boards", "platform": "PLATFORM-01", "bits": 1})
    s.hub.handle(service, {"type": "boards", "platform": "PLATFORM-01", "bits": 1})        # told again: not pressed again
    s.hub.handle(service, {"type": "boards", "platform": "PLATFORM-01", "bits": 0})
    s.hub.handle(service, {"type": "boards", "platform": "PLATFORM-02", "bits": 3})
    assert (s.hub.seats["A1"].score, s.hub.seats["A2"].score) == (1, 1)
    for wrong, code in (({"platform": "PLATFORM-09", "bits": 1}, "unknown_seat"), ({"platform": "PLATFORM-01", "bits": 256}, "bad_message"),
                        ({"platform": "PLATFORM-01", "bits": True}, "bad_message"), ({"bits": 1}, "bad_message")):
        s.clear()
        s.hub.handle(service, {"type": "boards", **wrong})
        assert [m["code"] for m in s.messages("error", service)] == [code]
    s.clear()
    s.hub.handle(player, {"type": "boards", "platform": "PLATFORM-01", "bits": 1})
    assert [m["code"] for m in s.messages("error", player)] == ["not_allowed"] and s.hub.seats["A1"].score == 1


def test_the_relay_keeps_the_hub_informed_across_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_module, "RETRY_S", (0.1,))
    show = Show()
    relay = ButtonRelay(show.url)
    relay.offer("PLATFORM-01", 1)                            # pressed before there was a connection
    relay.start()
    try:
        assert wait_for(lambda: show.hub.seats["A1"].score == 1) and relay.status()["connected"] is True
        relay.offer("PLATFORM-01", 0)
        relay.offer("PLATFORM-09", 1)                        # a platform without a seat: refused, and said so
        assert wait_for(lambda: "PLATFORM-09" in str(relay.status()["problem"]))
        port = show.server.port
        show.server.stop()
        assert wait_for(lambda: not relay.connected)
        relay.offer("PLATFORM-02", 1)                        # pressed while the hub was away
        again = Show(port)
        try:
            assert wait_for(lambda: again.hub.seats["A2"].score == 1) and again.hub.seats["A1"].score == 0
            assert relay.connects == 2 and relay.connected
        finally:
            again.server.stop()
    finally:
        relay.stop()
    assert not relay.connected and relay._thread is None


def test_the_relay_says_when_the_hub_wants_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_module, "RETRY_S", (0.1,))
    show = Show(game_key="games-only")
    refused, welcome = ButtonRelay(show.url), ButtonRelay(show.url, key="games-only")
    try:
        refused.start()
        welcome.start()
        welcome.offer("PLATFORM-02", 1)
        assert wait_for(lambda: show.hub.seats["A2"].score == 1)
        assert wait_for(lambda: bool(refused.last_problem)) and not refused.connected and refused.sent == 0
    finally:
        refused.stop()
        welcome.stop()
        show.server.stop()


def test_a_button_on_a_board_of_the_show_service_reaches_the_hub(tmp_path: Path) -> None:
    """Board -> effects link of the show service (which owns the board during a film) -> relay -> hub -> blocks."""
    port = free_port(socket.SOCK_DGRAM)
    patch = tmp_path / "patch.toml"
    patch.write_text(f'[output]\nprotocol = "esp32"\n[output.esp32]\nport = {port}\n[output.esp32.platforms]\n"PLATFORM-01" = "127.0.0.1"\n'
                     '[[fixtures]]\nid = "fan"\neffect = "wind"\nscope = "platform"\naddress = 1\n')
    show = Show()
    runtime = SimpleNamespace(cfg=SimpleNamespace(platform_id="PLATFORM-01", address=""), run_counter=0, mono=time.monotonic,
                              show_position=lambda now: (False, None, 0.0, 0.0, False))
    cfg = SimpleNamespace(effects=EffectsSettings(patch, buttons_to=show.url))
    link = EffectsLink.create(cfg, [runtime], SimpleNamespace(effects_errors={}))          # type: ignore[arg-type]
    assert link is not None and link.service is not None and link.buttons is not None
    board = br.SimNode(node_id=1, host="127.0.0.1", port=port, dmx_ports=1, audio_channels=0, actuators=True, inputs=4)
    try:
        board.start()
        link.start()
        assert wait_for(lambda: link.platform_status(runtime)["buttons_to"]["connected"])       # type: ignore[arg-type]
        board.press(0b0001)
        assert wait_for(lambda: show.hub.seats["A1"].score == 1)
        board.press(0)
        board.press(0b0001)
        assert wait_for(lambda: show.hub.seats["A1"].score == 2) and link.buttons.sent >= 3
    finally:
        link.stop()
        board.stop()
        show.server.stop()
    assert not link.buttons.connected


def test_the_show_service_setting(tmp_path: Path) -> None:
    base = (f'[controller]\nserver_url = "ws://127.0.0.1:8080/ws/v1/device"\n[[platforms]]\nconfig = "{PROFILE}"\n'
            'device_key = "k"\n[effects]\npatch = "patch.toml"\n')
    path = tmp_path / "controller.toml"
    path.write_text(base + 'buttons_to = "ws://192.168.10.2:47020/ws"\nbuttons_key = "games-only"\n')
    fx = load_controller_config(path).effects
    assert fx is not None and (fx.buttons_to, fx.buttons_key) == ("ws://192.168.10.2:47020/ws", "games-only")
    path.write_text(base)
    fx = load_controller_config(path).effects
    assert fx is not None and fx.buttons_to == ""
    path.write_text(base + 'buttons_to = "192.168.10.2"\n')
    with pytest.raises(ControllerConfigError, match="ws://ADDRESS:47020/ws"):
        load_controller_config(path)
