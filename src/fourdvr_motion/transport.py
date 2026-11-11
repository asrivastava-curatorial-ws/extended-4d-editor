"""Byte transports for the ESP32 link: UDP (Ethernet/Wi-Fi), serial (COBS framed), loopback (simulation)."""

from __future__ import annotations

import socket
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from .esp32_protocol import DEFAULT_UDP_PORT, PacketError, cobs_decode, cobs_encode
from .sim_esp32 import SimulatedEsp32


class TransportError(RuntimeError):
    pass


class Transport(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def send(self, packet: bytes) -> None: ...

    @abstractmethod
    def receive(self) -> list[bytes]:
        """All packets received since the last call (non-blocking)."""

    @abstractmethod
    def describe(self) -> str: ...


class UdpTransport(Transport):
    def __init__(self, host: str, port: int = DEFAULT_UDP_PORT, local_port: int = 0) -> None:
        self.host, self.port, self.local_port = host, port, local_port
        self.sock: socket.socket | None = None
        self.send_errors = 0

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF: low-latency on managed switches
        sock.bind(("0.0.0.0", self.local_port))
        sock.setblocking(False)
        self.sock = sock

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send(self, packet: bytes) -> None:
        if self.sock is None:
            raise TransportError("transport not open")
        try:
            self.sock.sendto(packet, (self.host, self.port))
        except OSError:
            self.send_errors += 1  # unreachable host etc.: reported through the link watchdog

    def receive(self) -> list[bytes]:
        out: list[bytes] = []
        if self.sock is None:
            return out
        while True:
            try:
                data, addr = self.sock.recvfrom(512)
            except (BlockingIOError, InterruptedError):
                return out
            except OSError:
                return out
            if addr[1] == self.port:
                out.append(data)

    def describe(self) -> str:
        return f"udp://{self.host}:{self.port}"


class SerialTransport(Transport):
    def __init__(self, device: str, baudrate: int = 921600) -> None:
        self.device, self.baudrate = device, baudrate
        self.port: Any = None
        self._buf = bytearray()

    def open(self) -> None:
        try:
            import serial  # pyserial (optional dependency: pip install fourdvr-motion-controller[serial])
        except ImportError as exc:  # pragma: no cover - depends on installation
            raise TransportError("serial transport requires pyserial (pip install pyserial)") from exc
        self.port = serial.Serial(self.device, self.baudrate, timeout=0, write_timeout=0.05)

    def close(self) -> None:
        if self.port is not None:
            self.port.close()
            self.port = None

    def send(self, packet: bytes) -> None:
        if self.port is None:
            raise TransportError("transport not open")
        self.port.write(cobs_encode(packet) + b"\x00")

    def receive(self) -> list[bytes]:
        if self.port is None:
            return []
        waiting = self.port.in_waiting
        if waiting:
            self._buf += self.port.read(waiting)
        return self.split_frames()

    def split_frames(self) -> list[bytes]:
        out: list[bytes] = []
        while True:
            idx = self._buf.find(0)
            if idx < 0:
                if len(self._buf) > 4096:
                    self._buf.clear()
                return out
            frame = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if frame:
                try:
                    out.append(cobs_decode(frame))
                except PacketError:
                    out.append(b"")  # counted as invalid by the receiver

    def describe(self) -> str:
        return f"serial://{self.device}?baud={self.baudrate}"


class LoopbackTransport(Transport):
    """In-process link to a SimulatedEsp32 (used by the simulation driver)."""

    def __init__(self, device: SimulatedEsp32, clock: Callable[[], float] = time.monotonic) -> None:
        self.device = device
        self.clock = clock
        self._t0 = clock()

    def now_ms(self) -> int:
        return int((self.clock() - self._t0) * 1000)

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, packet: bytes) -> None:
        self.device.on_packet(packet, self.now_ms())

    def receive(self) -> list[bytes]:
        self.device.tick(self.now_ms())
        return self.device.take_outbox()

    def describe(self) -> str:
        return "simulation://in-process"


def transport_from_address(address: str) -> Transport:
    """udp://host[:port] | serial:///dev/ttyUSB0?baud=921600 | serial://COM3?baud=921600"""
    url = urlparse(address)
    if url.scheme == "udp":
        if not url.hostname:
            raise TransportError(f"bad UDP address {address!r}")
        return UdpTransport(url.hostname, url.port or DEFAULT_UDP_PORT)
    if url.scheme == "serial":
        device = (url.netloc + url.path) or ""
        if not device:
            raise TransportError(f"bad serial address {address!r}")
        baud = int(parse_qs(url.query).get("baud", ["921600"])[0])
        return SerialTransport(device, baud)
    raise TransportError(f"unsupported controller address {address!r} (use udp:// or serial://)")
