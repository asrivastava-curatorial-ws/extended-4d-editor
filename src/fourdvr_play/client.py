"""A small client of the hub for Python programs: a game or simulator, a show controller, a test.

    from fourdvr_play.client import PlayClient

    with PlayClient("ws://192.168.10.2:47020/ws", role="game") as hub:
        hub.event("hit", seat="A1")                       # the logic blocks take it from here
        hub.motion("A1", accel=[1.2, 0.0, 0.0], speed=14.0)
        message = hub.recv(timeout=0.1)                   # {"type": "input", "seat": "A1", "button": "a", ...}

The messages are those of docs/PLAY_PROTOCOL.md; ``send`` sends any of them.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from . import PROTOCOL


class PlayClientError(RuntimeError):
    pass


class PlayClient:
    def __init__(self, url: str = "ws://127.0.0.1:47020/ws", role: str = "game", seat: str | None = None, name: str = "",
                 key: str = "", timeout: float = 5.0) -> None:
        self.url = url
        self.role = role
        self.seat = seat
        self.name = name
        self.key = key
        self.timeout = timeout
        self.welcome: dict[str, Any] = {}
        self.inbox: deque[dict[str, Any]] = deque(maxlen=2000)
        self._ws: ClientConnection | None = None

    # ------------------------------------------------------------------ connection
    def connect(self) -> dict[str, Any]:
        try:
            # entered like a 'with' block (newer versions of the library warn otherwise); close() leaves it
            self._ws = connect(self.url, open_timeout=self.timeout, max_size=1 << 20).__enter__()
            hello: dict[str, Any] = {"type": "hello", "play": PROTOCOL, "role": self.role}
            if self.seat is not None:
                hello["seat"] = self.seat
            if self.name:
                hello["name"] = self.name
            if self.key:
                hello["key"] = self.key
            self._ws.send(json.dumps(hello))
            first = json.loads(self._ws.recv(timeout=self.timeout))
        except (OSError, TimeoutError, ConnectionClosed, ValueError) as exc:
            self.close()
            raise PlayClientError(f"{self.url}: {exc or type(exc).__name__}") from exc
        if first.get("type") != "welcome":
            self.close()
            raise PlayClientError(f"{self.url}: {first.get('message', 'not welcomed')}")
        self.welcome = first
        return first

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except (OSError, ConnectionClosed):
                pass

    def __enter__(self) -> PlayClient:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # ------------------------------------------------------------------ sending
    def send(self, kind: str, **fields: Any) -> None:
        if self._ws is None:
            raise PlayClientError("not connected")
        try:
            self._ws.send(json.dumps({"type": kind, **{k: v for k, v in fields.items() if v is not None}}))
        except (OSError, ConnectionClosed) as exc:
            self.close()
            raise PlayClientError(f"connection lost: {exc or type(exc).__name__}") from exc

    def event(self, name: str, seat: str | None = None, value: float = 0.0) -> None:
        self.send("event", name=name, seat=seat, value=value)

    def input(self, button: str, pressed: bool | None = None, seat: str | None = None) -> None:
        """A button; without `pressed` it is a tap (pressed and let go)."""
        self.send("input", button=button, pressed=pressed, seat=seat)

    def answer(self, choice: int | str, prompt: str | None = None, seat: str | None = None) -> None:
        self.send("answer", choice=choice, prompt=prompt, seat=seat)

    def motion(self, seat: str | None = None, **telemetry: Any) -> None:
        """A seat's telemetry: the keys of the open live format (accel, rates, speed, rumble, effects ...)."""
        self.send("motion", seat=seat, **telemetry)

    def react(self, seat: str = "all", **reaction: Any) -> None:
        self.send("react", seat=seat, **reaction)

    def ready(self, ready: bool = True, seat: str | None = None) -> None:
        self.send("ready", ready=ready, seat=seat)

    def phase(self, to: str) -> None:
        self.send("phase", to=to)

    def state(self, key: str, value: Any) -> None:
        self.send("state", key=key, value=value)

    def score(self, seat: str, add: float) -> None:
        self.send("score", seat=seat, add=add)

    # ------------------------------------------------------------------ receiving
    def recv(self, timeout: float | None = 0.0) -> dict[str, Any] | None:
        """The next message, or None if there is none within `timeout` seconds."""
        if self.inbox:
            return self.inbox.popleft()
        return self._read(timeout)

    def _read(self, timeout: float | None) -> dict[str, Any] | None:
        if self._ws is None:
            raise PlayClientError("not connected")
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except (OSError, ConnectionClosed) as exc:
            self.close()
            raise PlayClientError(f"connection lost: {exc or type(exc).__name__}") from exc
        message = json.loads(raw)
        return message if isinstance(message, dict) else None

    def wait_for(self, kind: str, timeout: float = 5.0, **match: Any) -> dict[str, Any]:
        """The next message of this type (and with these values); the others stay for ``recv``."""
        kept: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        try:
            while True:
                message = self.inbox.popleft() if self.inbox else self._read(max(0.0, deadline - time.monotonic()))
                if message is None:
                    raise PlayClientError(f"no '{kind}' message within {timeout:g} s")
                if message.get("type") == kind and all(message.get(k) == v for k, v in match.items()):
                    return message
                kept.append(message)
        finally:
            self.inbox.extendleft(reversed(kept))
