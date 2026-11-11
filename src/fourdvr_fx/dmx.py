"""DMX512 output over the network or USB, and the matching packet decoders.

Outputs (all share ``send(universe, data)`` with a 512-byte frame, universes numbered from 1):

    SacnOutput       streaming ACN, ANSI E1.31 - multicast and/or unicast, universe discovery, stream termination
    ArtNetOutput     Art-Net ArtDmx (OpOutput) - unicast or directed broadcast
    EnttecProOutput  Enttec DMX USB Pro compatible serial widgets (one universe; needs pyserial)
    CaptureOutput    keeps the frames in memory (tests, the studio's DMX monitor)
    NullOutput       discards everything

DMX and these transports carry no safety function: effect power that must drop on an emergency stop is
switched by the safety system, never by DMX (docs/EFFECTS_GUIDE.md).
"""

from __future__ import annotations

import contextlib
import logging
import socket
import struct
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DMX_CHANNELS = 512
SACN_PORT = 5568
ARTNET_PORT = 6454
_ACN_ID = b"ASC-E1.17\x00\x00\x00"
_VECTOR_ROOT_DATA = 0x00000004
_VECTOR_ROOT_EXTENDED = 0x00000008
_VECTOR_FRAMING_DATA = 0x00000002
_VECTOR_FRAMING_DISCOVERY = 0x00000002
_VECTOR_DMP_SET_PROPERTY = 0x02
_VECTOR_DISCOVERY_LIST = 0x00000001
SACN_DISCOVERY_UNIVERSE = 64214
SACN_DISCOVERY_INTERVAL_S = 10.0
SACN_OPT_TERMINATED = 0x40
_ARTNET_ID = b"Art-Net\x00"
_ARTNET_OP_DMX = 0x5000


# ------------------------------------------------------------------------------ sACN (E1.31)
def sacn_multicast_address(universe: int) -> str:
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def _name64(name: str) -> bytes:
    raw = name.encode("utf-8")[:63]
    return raw + b"\x00" * (64 - len(raw))


def build_sacn_data(
    cid: bytes, source_name: str, universe: int, data: bytes, sequence: int, priority: int = 100, options: int = 0
) -> bytes:
    """E1.31 data packet (start code 0) for one universe."""
    if len(cid) != 16:
        raise ValueError("CID must be 16 bytes")
    if not 1 <= universe <= 63999:
        raise ValueError("sACN universe must be 1..63999")
    slots = bytes(data[:DMX_CHANNELS])
    dmp = struct.pack("!HBBHHH", 0x7000 | (10 + 1 + len(slots)), _VECTOR_DMP_SET_PROPERTY, 0xA1, 0, 1, 1 + len(slots))
    dmp += b"\x00" + slots
    framing_len = 77 + len(dmp)
    framing = struct.pack("!HI", 0x7000 | framing_len, _VECTOR_FRAMING_DATA) + _name64(source_name)
    framing += struct.pack("!BHBBH", priority & 0xFF, 0, sequence & 0xFF, options & 0xFF, universe)
    root_len = 22 + framing_len
    root = struct.pack("!HH", 0x0010, 0x0000) + _ACN_ID + struct.pack("!HI", 0x7000 | root_len, _VECTOR_ROOT_DATA) + cid
    return root + framing + dmp


def build_sacn_discovery(cid: bytes, source_name: str, universes: list[int], page: int = 0, last_page: int = 0) -> bytes:
    """E1.31 universe discovery packet (sent to universe 64214 every 10 s)."""
    body = b"".join(struct.pack("!H", u) for u in sorted(universes)[:512])
    disc = struct.pack("!HIBB", 0x7000 | (8 + len(body)), _VECTOR_DISCOVERY_LIST, page, last_page) + body
    framing_len = 74 + len(disc)
    framing = struct.pack("!HI", 0x7000 | framing_len, _VECTOR_FRAMING_DISCOVERY) + _name64(source_name) + b"\x00" * 4
    root_len = 22 + framing_len
    root = struct.pack("!HH", 0x0010, 0x0000) + _ACN_ID + struct.pack("!HI", 0x7000 | root_len, _VECTOR_ROOT_EXTENDED) + cid
    return root + framing + disc


@dataclass
class DmxFrame:
    protocol: str        # "sacn" | "artnet"
    universe: int        # 1-based DMX universe (Art-Net: port-address, see ArtNetOutput.universe_base)
    data: bytes
    sequence: int = 0
    priority: int = 0
    terminated: bool = False
    source: str = ""


def parse_sacn(packet: bytes) -> DmxFrame | None:
    """Decode an E1.31 data packet; None for anything else (including discovery packets)."""
    if len(packet) < 126 or packet[4:16] != _ACN_ID:
        return None
    if struct.unpack_from("!I", packet, 18)[0] != _VECTOR_ROOT_DATA:
        return None
    if struct.unpack_from("!I", packet, 40)[0] != _VECTOR_FRAMING_DATA or packet[117] != _VECTOR_DMP_SET_PROPERTY:
        return None
    priority, _sync, sequence, options, universe = struct.unpack_from("!BHBBH", packet, 108)
    count = struct.unpack_from("!H", packet, 123)[0]
    if count < 1 or len(packet) < 125 + count or packet[125] != 0:  # start code 0 only
        return None
    name = packet[44:108].split(b"\x00", 1)[0].decode("utf-8", "replace")
    return DmxFrame("sacn", universe, bytes(packet[126 : 125 + count]), sequence, priority,
                    bool(options & SACN_OPT_TERMINATED), name)


# ------------------------------------------------------------------------------ Art-Net
def build_artdmx(port_address: int, data: bytes, sequence: int = 0, physical: int = 0) -> bytes:
    """ArtDmx packet. `port_address` is the 15-bit Art-Net address (net:subnet:universe)."""
    if not 0 <= port_address <= 0x7FFF:
        raise ValueError("Art-Net port-address must be 0..32767")
    slots = bytes(data[:DMX_CHANNELS])
    if len(slots) % 2:
        slots += b"\x00"
    if len(slots) < 2:
        slots = slots.ljust(2, b"\x00")
    head = _ARTNET_ID + struct.pack("<H", _ARTNET_OP_DMX) + struct.pack("!BB", 0, 14)
    head += struct.pack("!BBBBH", sequence & 0xFF, physical & 0xFF, port_address & 0xFF, (port_address >> 8) & 0x7F, len(slots))
    return head + slots


def parse_artdmx(packet: bytes) -> DmxFrame | None:
    if len(packet) < 20 or packet[:8] != _ARTNET_ID or struct.unpack_from("<H", packet, 8)[0] != _ARTNET_OP_DMX:
        return None
    sequence, _physical, sub_uni, net, length = struct.unpack_from("!BBBBH", packet, 12)
    if length > DMX_CHANNELS or len(packet) < 18 + length:
        return None
    return DmxFrame("artnet", ((net & 0x7F) << 8) | sub_uni, bytes(packet[18 : 18 + length]), sequence)


# ------------------------------------------------------------------------------ outputs
class DmxOutput(ABC):
    """One DMX transport. send() never raises: errors are counted and logged (rate-limited)."""

    name = "output"

    def __init__(self) -> None:
        self.frames_sent = 0
        self.errors = 0
        self.last_error = ""
        self._last_log = 0.0
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def send(self, universe: int, data: bytes) -> None:
        try:
            self._send(universe, data)
            self.frames_sent += 1
        except (OSError, ValueError) as exc:
            self._error(exc)

    def _error(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        now = time.monotonic()
        if now - self._last_log > 10.0:
            self._last_log = now
            log.warning("DMX output error", extra={"output": self.name, "error": self.last_error, "errors": self.errors})

    @abstractmethod
    def _send(self, universe: int, data: bytes) -> None: ...

    def flush(self, now: float | None = None) -> None:
        """Called once after the universes of a frame were handed to send(). Outputs that send at once ignore it."""
        return None

    def describe(self) -> dict[str, Any]:
        return {"output": self.name, "open": self.is_open, "frames": self.frames_sent, "errors": self.errors,
                "last_error": self.last_error or None}


class NullOutput(DmxOutput):
    name = "none"

    def _send(self, universe: int, data: bytes) -> None:
        return None


class CaptureOutput(DmxOutput):
    """Keeps the latest frame of every universe (and optionally a history) in memory."""

    name = "capture"

    def __init__(self, history: int = 0) -> None:
        super().__init__()
        self.latest: dict[int, bytes] = {}
        self.history: list[tuple[float, int, bytes]] = []
        self._keep = history
        self._lock = threading.Lock()

    def _send(self, universe: int, data: bytes) -> None:
        frame = bytes(data)
        with self._lock:
            self.latest[universe] = frame
            if self._keep:
                self.history.append((time.monotonic(), universe, frame))
                if len(self.history) > self._keep:
                    del self.history[: len(self.history) - self._keep]

    def channel(self, universe: int, channel: int) -> int:
        with self._lock:
            frame = self.latest.get(universe)
        return frame[channel - 1] if frame else 0


class SacnOutput(DmxOutput):
    name = "sacn"

    def __init__(
        self,
        source_name: str = "4D VR Theatre",
        priority: int = 100,
        multicast: bool = True,
        unicast: list[str] | None = None,
        interface_ip: str = "",
        port: int = SACN_PORT,
        ttl: int = 8,
        cid: bytes | None = None,
    ) -> None:
        super().__init__()
        self.source_name = source_name
        self.priority = max(0, min(200, int(priority)))
        self.multicast = multicast
        self.unicast = list(unicast or [])
        self.interface_ip = interface_ip
        self.port = port
        self.ttl = ttl
        # a stable CID per source name and host, as E1.31 expects from one component
        self.cid = cid or uuid.uuid5(uuid.NAMESPACE_DNS, f"4dvr-effects/{socket.gethostname()}/{source_name}").bytes
        self._sock: socket.socket | None = None
        self._seq: dict[int, int] = {}
        self._last_discovery = 0.0

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
        if self.interface_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface_ip))
            sock.bind((self.interface_ip, 0))
        sock.setblocking(False)
        self._sock = sock
        self.is_open = True

    def _targets(self, universe: int) -> list[str]:
        return ([sacn_multicast_address(universe)] if self.multicast else []) + self.unicast

    def _send(self, universe: int, data: bytes, options: int = 0) -> None:
        if self._sock is None:
            raise OSError("sACN output is not open")
        seq = self._seq.get(universe, 0)
        self._seq[universe] = (seq + 1) & 0xFF
        packet = build_sacn_data(self.cid, self.source_name, universe, data, seq, self.priority, options)
        for host in self._targets(universe):
            self._sock.sendto(packet, (host, self.port))
        now = time.monotonic()
        if now - self._last_discovery >= SACN_DISCOVERY_INTERVAL_S:
            self._last_discovery = now
            disc = build_sacn_discovery(self.cid, self.source_name, list(self._seq))
            for host in ([sacn_multicast_address(SACN_DISCOVERY_UNIVERSE)] if self.multicast else []) + self.unicast:
                self._sock.sendto(disc, (host, self.port))

    def close(self) -> None:
        """Send three stream-terminated packets per universe (E1.31 6.2.6), then close."""
        if self._sock is not None:
            for universe in list(self._seq):
                for _ in range(3):
                    with contextlib.suppress(OSError, ValueError):
                        self._send(universe, bytes(DMX_CHANNELS), SACN_OPT_TERMINATED)
            self._sock.close()
        self._sock = None
        self.is_open = False


class ArtNetOutput(DmxOutput):
    """ArtDmx to one or more nodes. DMX universe u is sent as Art-Net port-address universe_base + (u - 1)."""

    name = "artnet"

    def __init__(self, targets: list[str] | None = None, universe_base: int = 0, port: int = ARTNET_PORT, interface_ip: str = "") -> None:
        super().__init__()
        self.targets = list(targets or ["255.255.255.255"])
        self.universe_base = universe_base
        self.port = port
        self.interface_ip = interface_ip
        self._sock: socket.socket | None = None
        self._seq: dict[int, int] = {}

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self.interface_ip:
            sock.bind((self.interface_ip, 0))
        sock.setblocking(False)
        self._sock = sock
        self.is_open = True

    def _send(self, universe: int, data: bytes) -> None:
        if self._sock is None:
            raise OSError("Art-Net output is not open")
        seq = self._seq.get(universe, 0) % 255 + 1   # 1..255; 0 would disable sequencing
        self._seq[universe] = seq
        packet = build_artdmx(self.universe_base + universe - 1, data, seq)
        for host in self.targets:
            self._sock.sendto(packet, (host, self.port))

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
        self._sock = None
        self.is_open = False


class EnttecProOutput(DmxOutput):
    """Enttec DMX USB Pro (and compatible) widget: "Output Only Send DMX Packet Request" (label 6)."""

    name = "enttec-pro"

    REOPEN_INTERVAL_S = 5.0

    def __init__(self, port: str, universe: int = 1) -> None:
        super().__init__()
        self.port = port
        self.universe = universe
        self._serial: Any = None
        self._last_open = 0.0

    @staticmethod
    def build_message(data: bytes) -> bytes:
        payload = b"\x00" + bytes(data[:DMX_CHANNELS]).ljust(24, b"\x00")   # start code + at least 24 slots
        return bytes((0x7E, 6, len(payload) & 0xFF, len(payload) >> 8)) + payload + b"\xE7"

    def open(self) -> None:
        self._last_open = time.monotonic()
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError as exc:
            raise OSError("the USB DMX output needs pyserial (pip install pyserial)") from exc
        self._serial = serial.Serial(self.port, baudrate=57600, timeout=0, write_timeout=0.05)
        self.is_open = True

    def _send(self, universe: int, data: bytes) -> None:
        if universe != self.universe:
            return
        if self._serial is None:
            # unplugged, or not there when the service started: look for it again every few seconds
            if time.monotonic() - self._last_open < self.REOPEN_INTERVAL_S:
                raise OSError(f"USB DMX interface {self.port} is not open")
            self.open()
        try:
            self._serial.write(self.build_message(data))
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._serial.close()
            self._serial, self.is_open = None, False
            raise OSError(f"USB DMX interface {self.port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            with contextlib.suppress(Exception):
                self._serial.write(self.build_message(bytes(DMX_CHANNELS)))
                self._serial.flush()
                self._serial.close()
        self._serial = None
        self.is_open = False


# ------------------------------------------------------------------------------ receiver (tests, monitor)
class DmxReceiver:
    """Listens for sACN and/or Art-Net and keeps the latest frame per universe."""

    def __init__(
        self,
        protocol: str = "sacn",
        universes: list[int] | None = None,
        bind_ip: str = "",
        port: int | None = None,
        multicast: bool = True,
        history: int = 0,
    ) -> None:
        if protocol not in ("sacn", "artnet"):
            raise ValueError("protocol must be sacn or artnet")
        self.protocol = protocol
        self.universes = list(universes or [1])
        self.bind_ip = bind_ip
        self.port = port if port is not None else (SACN_PORT if protocol == "sacn" else ARTNET_PORT)
        self.multicast = multicast and protocol == "sacn"
        self.latest: dict[int, DmxFrame] = {}
        self.history: list[tuple[float, DmxFrame]] = []
        self.packets = 0
        self._keep = history
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> DmxReceiver:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_ip if not self.multicast else "", self.port))
        if self.port == 0:
            self.port = sock.getsockname()[1]
        if self.multicast:
            for u in self.universes:
                mreq = socket.inet_aton(sacn_multicast_address(u)) + socket.inet_aton(self.bind_ip or "0.0.0.0")
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(0.1)
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name="dmx-receiver", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        assert self._sock is not None
        parse = parse_sacn if self.protocol == "sacn" else parse_artdmx
        while not self._stop.is_set():
            try:
                packet, _addr = self._sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                break
            frame = parse(packet)
            if frame is None:
                continue
            with self._lock:
                self.packets += 1
                self.latest[frame.universe] = frame
                if self._keep:
                    self.history.append((time.monotonic(), frame))
                    if len(self.history) > self._keep:
                        del self.history[: len(self.history) - self._keep]

    def channel(self, universe: int, channel: int) -> int:
        with self._lock:
            frame = self.latest.get(universe)
        return frame.data[channel - 1] if frame and len(frame.data) >= channel else 0

    def snapshot(self) -> dict[int, DmxFrame]:
        with self._lock:
            return dict(self.latest)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._sock is not None:
            self._sock.close()
        self._sock = None
