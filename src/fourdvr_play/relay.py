"""Seat buttons of the boards, passed on to the hub by another program.

A seat's ESP32 reports its buttons to the one controller that speaks to it. In a game that is the
interactive show itself (`fourdvr play run`). During a film it is the show service (`fourdvr
controller run`), which has no use for buttons: with ``buttons_to`` in its ``[effects]`` section it
hands them to the hub through this relay, so that a seat button can answer a question in a film.

Nothing here may hold up the caller: ``offer`` only notes the state, a thread of its own keeps the
connection and sends.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .client import PlayClient, PlayClientError

RETRY_S = (1.0, 2.0, 4.0, 8.0)


class ButtonRelay:
    def __init__(self, url: str, key: str = "", name: str = "show service") -> None:
        self.url = url
        self.key = key
        self.name = name
        self.sent = 0
        self.connects = 0
        self.connected = False
        self.last_problem = ""
        self._state: dict[str, int] = {}          # platform id -> inputs pressed, as last reported
        self._changes: deque[tuple[str, int]] = deque(maxlen=512)      # every change in its order: a quick tap counts
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def offer(self, platform_id: str, bits: int) -> None:
        """The inputs of a platform's board (BridgeOutput.on_input). Never waits."""
        with self._lock:
            self._state[platform_id] = int(bits)
            self._changes.append((platform_id, int(bits)))
        self._wake.set()

    def status(self) -> dict[str, object]:
        return {"hub": self.url, "connected": self.connected, "sent": self.sent, "connects": self.connects,
                "problem": self.last_problem or None}

    # ------------------------------------------------------------------ the thread
    def _send_changes(self, client: PlayClient, everything: bool = False) -> None:
        with self._lock:
            if everything:                # a hub that was away knows nothing: what is pressed now, not what was tapped
                self._changes.clear()
                self._changes.extend(self._state.items())
            changes = list(self._changes)
            self._changes.clear()
        for platform_id, bits in changes:
            client.send("boards", platform=platform_id, bits=bits)
            self.sent += 1

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            client = PlayClient(self.url, role="game", name=self.name, key=self.key, timeout=3.0)
            try:
                client.connect()
                self.connected, self.last_problem, failures = True, "", 0
                self.connects += 1
                self._send_changes(client, everything=True)
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    self._wake.clear()
                    self._send_changes(client)
                    while (told := client.recv(timeout=0.0)) is not None:      # what the hub tells everybody is not for
                        if told.get("type") == "error":                        # us, except that it refused something
                            self.last_problem = str(told.get("message", "refused"))
                    if time.monotonic() - last_ping >= 5.0:          # notices a hub that went away silently
                        client.send("ping")
                        last_ping = time.monotonic()
                    self._wake.wait(0.2)
            except PlayClientError as exc:
                self.last_problem = str(exc)
            finally:
                self.connected = False
                client.close()
            if not self._stop.is_set():
                self._stop.wait(RETRY_S[min(failures, len(RETRY_S) - 1)])
                failures += 1

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="play-button-relay", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
