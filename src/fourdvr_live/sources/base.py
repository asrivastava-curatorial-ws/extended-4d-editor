"""What every live source has in common: a thread that receives, a decoder that turns what it
receives into :class:`LiveSample` values, counters for the monitor."""

from __future__ import annotations

import contextlib
import math
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..sample import G, LiveSample

Sink = Callable[[LiveSample, float], Any]
PICTURE_INPUT_LP_HZ = 4.0     # what the cueing's input smoothing is set to for motion estimated from a picture


class SourceError(RuntimeError):
    """A source cannot be started (port in use, option missing, package missing ...)."""


@dataclass(frozen=True)
class SourceInfo:
    name: str                 # what the configuration says: type = "forza"
    title: str
    games: str                # which games / simulators
    kind: str                 # "car", "aircraft" or "any" (chooses the class of the offline conversion)
    port: int = 0             # default UDP port to listen on (0: none)
    needs_host: bool = False  # the address of the game / console must be given
    setup: str = ""           # one line: what to switch on in the game
    published: str = ""       # where the format is published
    experimental: bool = False


DEMO = SourceInfo("demo", "Built-in demonstration drive", "no game needed: a made-up lap, for trying the outputs", "car",
                  setup="nothing")
REPLAY = SourceInfo("replay", "Recorded session", "a .4dlive file played again in real time", "any",
                    setup="file = \"session.4dlive\"")
SCREEN = SourceInfo("screen", "Screen, capture card or video stream", "anything with a moving picture: a game without "
                    "telemetry, a console on a capture card, a camera or a network stream", "any",
                    setup="[screen] in the configuration: what to capture and how the delay is handled")
SOUND = SourceInfo("sound", "Sound only", "anything with sound: the low end goes to the shakers", "any",
                   setup="[sound] in the configuration: which input or monitor device to listen to")


class Decoder:
    """Bytes in, samples out. A decoder keeps what it needs between packets and nothing else; it does
    no I/O, so that it is tested with bytes alone."""

    def __init__(self) -> None:
        self.invalid = 0              # packets that were not what the format says
        self.ignored = 0              # valid packets of a kind that carries nothing for us

    def feed(self, data: bytes, now: float) -> LiveSample | None:
        raise NotImplementedError

    def bad(self) -> None:
        self.invalid += 1


class Hits:
    """An impact shows as a jump of the acceleration from one packet to the next."""

    def __init__(self, threshold: float = 18.0, full: float = 60.0) -> None:
        self.threshold, self.full = threshold, full
        self._last: tuple[float, float, float] | None = None

    def step(self, accel: tuple[float, float, float]) -> float:
        last, self._last = self._last, accel
        if last is None:
            return 0.0
        jump = math.sqrt(sum((a - b) ** 2 for a, b in zip(accel, last, strict=True)))
        return min(1.0, max(0.0, (jump - self.threshold) / (self.full - self.threshold)))


class Roughness:
    """Road feel where the game does not say what the surface is: how much the vertical
    acceleration shakes above a few hertz, 0..1."""

    def __init__(self, full: float = 5.0, hp_hz: float = 3.0, hold_s: float = 0.15) -> None:
        self.full, self.hp_hz, self.hold_s = full, hp_hz, hold_s
        self._low = 0.0
        self._level = 0.0
        self._first = True

    def step(self, heave: float, dt: float) -> float:
        if self._first or dt <= 0.0:
            self._low, self._first = heave, False
            return 0.0
        dt = min(dt, 0.1)
        self._low += (heave - self._low) * (1.0 - math.exp(-2.0 * math.pi * self.hp_hz * dt))
        high = abs(heave - self._low)
        self._level += (high - self._level) * (1.0 - math.exp(-dt / self.hold_s))
        return min(1.0, self._level / self.full)


class Slope:
    """Rate of change of a value between packets, lightly smoothed (for sources that give a speed
    but no acceleration)."""

    def __init__(self, tau_s: float = 0.08) -> None:
        self.tau = tau_s
        self._last: float | None = None
        self.value = 0.0

    def step(self, x: float, dt: float) -> float:
        if self._last is None or dt <= 1e-4:
            self._last = x
            return self.value
        raw = (x - self._last) / dt
        self._last = x
        self.value += (raw - self.value) * (1.0 - math.exp(-min(dt, 0.2) / self.tau))
        return self.value


def lateral_from_yaw(speed: float, yaw_rate: float) -> float:
    """Sideways acceleration of a vehicle that turns without sliding (yaw + = nose left -> + = to the left)."""
    return speed * yaw_rate


def yaw_from_lateral(speed: float, lateral: float, minimum_speed: float = 5.0) -> float:
    """The other way round, for sources that give sideways g but no rotation."""
    return lateral / speed if speed >= minimum_speed else 0.0


def g_to_ms2(g: float) -> float:
    return g * G


class LiveSource:
    """A running source. `start(sink)` begins delivering samples to `sink(sample, now)` from the
    source's own thread; `stop()` ends it."""

    info: SourceInfo

    def __init__(self) -> None:
        self.samples = 0
        self.last_error = ""
        self._sink: Sink | None = None
        self._stamps: deque[float] = deque(maxlen=120)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ for subclasses
    def emit(self, sample: LiveSample, now: float) -> None:
        self.samples += 1
        self._stamps.append(now)
        sink = self._sink
        if sink is not None:
            sink(sample, now)

    def run(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ lifecycle
    def start(self, sink: Sink) -> None:
        self._sink = sink
        self._stop.clear()
        self.open()
        self._thread = threading.Thread(target=self._guarded, name=f"live-source-{self.info.name}", daemon=True)
        self._thread.start()

    def open(self) -> None:
        """Acquire what `run` needs; raise SourceError with a message a user can act on."""

    def close(self) -> None:
        """Release it (called after the thread has ended; must be safe to call twice)."""

    def _guarded(self) -> None:
        try:
            self.run()
        except Exception as exc:  # noqa: BLE001 - a source that dies is a stalled source, not a crash
            self.last_error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self.close()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def rate_hz(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        recent = [t for t in self._stamps if now - t <= 2.0]
        return (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 2 and recent[-1] > recent[0] else 0.0

    def status(self) -> dict[str, Any]:
        return {"source": self.info.name, "title": self.info.title, "samples": self.samples,
                "rate_hz": round(self.rate_hz(), 1), "error": self.last_error or None}


class UdpSource(LiveSource):
    """A source that listens on a UDP port and hands every datagram to a decoder."""

    def __init__(self, decoder: Decoder, port: int, bind: str = "0.0.0.0", host: str = "") -> None:
        super().__init__()
        self.decoder = decoder
        self.port = int(port)
        self.bind = bind
        self.host = host
        self.packets = 0
        self.sock: socket.socket | None = None

    def open(self) -> None:
        if not 1 <= self.port <= 65535:
            raise SourceError(f"{self.info.title}: port must be 1-65535")
        if self.info.needs_host and not self.host:
            raise SourceError(f"{self.info.title}: the address of the game is needed (host = \"...\")")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self.bind, self.port))
            sock.settimeout(0.1)
        except OSError as exc:
            sock.close()
            raise SourceError(f"{self.info.title}: cannot listen on UDP port {self.port} ({exc})") from exc
        self.sock = sock

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                self.goodbye(sock)
            sock.close()

    def idle(self, sock: socket.socket, now: float) -> None:
        """Called a few times a second: heartbeats and handshakes of formats that need them."""

    def goodbye(self, sock: socket.socket) -> None:
        """Called once before the socket closes."""

    def run(self) -> None:
        sock = self.sock
        assert sock is not None
        next_idle = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_idle:
                next_idle = now + 0.25
                try:
                    self.idle(sock, now)
                except OSError as exc:
                    self.last_error = f"sending to the game: {exc}"
            try:
                data, _addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                self.last_error = str(exc)
                continue
            now = time.monotonic()
            self.packets += 1
            sample = self.decoder.feed(data, now)
            if sample is not None:
                self.emit(sample, now)

    def status(self) -> dict[str, Any]:
        return {**super().status(), "port": self.port, "packets": self.packets,
                "invalid": self.decoder.invalid, "ignored": self.decoder.ignored}
