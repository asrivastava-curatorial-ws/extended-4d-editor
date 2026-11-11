"""Effects through ESP32 nodes: DMX devices and bass shakers / subwoofers driven by the same kind of
ESP32 that moves a platform - or by a second, small one next to it.

The "4DFX" link is a UDP protocol of its own, on its own port (47002), beside the motion link (47000):

* nothing here can disturb motion - an ESP32 that only knows the motion link never sees these packets;
* a node says what it has (``HELLO`` -> ``INFO``: a DMX line, an audio output, a platform) and the
  controller sends it that and nothing else. A node that does not answer, or has no DMX line, or no
  audio output, is simply not sent to: no error, no fault - it is asked again every second, so
  hardware that is switched on later is picked up;
* the node fails dark: one second without DMX packets and its line is at zero, an empty audio buffer
  is silence, and a node that also runs a platform blacks out by itself while its emergency stop is in.

Packets (little-endian; a CRC-32 as in zlib over everything before it ends every packet):

    header   4s magic "4DFX" | u8 major 1 | u8 minor 0 | u8 type | u8 node id (0: any node) | u32 sequence
    HELLO  1 u64 controller time (us)
    DMX    2 u8 port | u8 flags | u16 first slot (1..512) | u16 count | count bytes
    AUDIO  3 u8 channels | u8 flags (1: start of a stream) | u16 rate (Hz) | u32 position (frame index)
             | u16 frames | frames x channels i16
    STOP   4 u8 what (1 DMX dark, 2 audio silent) | u8 reserved
    INFO 129 u64 echoed time | u32 uptime ms | u16 firmware | u8 capabilities | u8 DMX ports
             | u8 audio channels | u8 state | u16 audio rate | u16 audio latency ms | u16 buffer ms
             | u8 DMX refresh Hz | u8 reserved | u32 packets | u16 invalid | u16 DMX timeouts
             | u16 audio underruns | u16 audio gaps

docs/ESP32_EFFECTS_LINK.md is the reference; firmware/esp32-actuator-bridge/lib/fourdvr_fx implements
the node, and both sides are tested against the same vectors.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import struct
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .audio import AudioState, BassRenderer
from .dmx import DMX_CHANNELS, DmxOutput
from .patch import ESP32_PLATFORM_UNIVERSE, ESP32_THEATRE_UNIVERSES, AudioConfig, Patch

log = logging.getLogger(__name__)

MAGIC = b"4DFX"
VERSION_MAJOR, VERSION_MINOR = 1, 0
DEFAULT_PORT = 47002
TYPE_HELLO, TYPE_DMX, TYPE_AUDIO, TYPE_STOP, TYPE_INFO = 1, 2, 3, 4, 0x81
CAP_ACTUATORS, CAP_DMX, CAP_AUDIO = 1, 2, 4
STATE_DMX_ACTIVE, STATE_AUDIO_PLAYING, STATE_INHIBITED = 1, 2, 4
STOP_DMX, STOP_AUDIO = 1, 2
AUDIO_START = 1
MAX_AUDIO_FRAMES = 320           # per packet: with two channels still inside one Ethernet frame
_HEADER = struct.Struct("<4sBBBBI")
_INFO = struct.Struct("<QIHBBBBHHHBBIHHHH")
_DMX = struct.Struct("<BBHH")
_AUDIO = struct.Struct("<BBHIH")

HELLO_INTERVAL_S = 1.0
NODE_TIMEOUT_S = 3.0             # no INFO for this long: the node counts as absent
NODE_DMX_TIMEOUT_S = 1.0         # what a node does by itself without DMX packets: go dark
THEATRE_UNIVERSES = ESP32_THEATRE_UNIVERSES       # theatre fixtures: the theatre node's DMX port 0 / 1
PLATFORM_UNIVERSE_BASE = ESP32_PLATFORM_UNIVERSE   # the k-th platform's own line is universe 101 + k


class LinkError(ValueError):
    """Not a valid 4DFX packet."""


# ------------------------------------------------------------------------------ packets
def encode(ptype: int, node: int, sequence: int, payload: bytes = b"") -> bytes:
    body = _HEADER.pack(MAGIC, VERSION_MAJOR, VERSION_MINOR, ptype, node & 0xFF, sequence & 0xFFFFFFFF) + payload
    return body + struct.pack("<I", zlib.crc32(body))


def decode(packet: bytes) -> tuple[int, int, int, bytes]:
    """(type, node id, sequence, payload) of a valid packet; LinkError otherwise."""
    if len(packet) < _HEADER.size + 4:
        raise LinkError("too short")
    magic, major, _minor, ptype, node, sequence = _HEADER.unpack_from(packet)
    if magic != MAGIC:
        raise LinkError("not a 4DFX packet")
    if major != VERSION_MAJOR:
        raise LinkError(f"protocol version {major} not supported")
    if struct.unpack_from("<I", packet, len(packet) - 4)[0] != zlib.crc32(packet[:-4]):
        raise LinkError("checksum")
    return ptype, node, sequence, packet[_HEADER.size : -4]


def build_hello(node: int, sequence: int, time_us: int) -> bytes:
    return encode(TYPE_HELLO, node, sequence, struct.pack("<Q", time_us & 0xFFFFFFFFFFFFFFFF))


def build_dmx(node: int, sequence: int, port: int, first: int, data: bytes, flags: int = 0) -> bytes:
    if not (1 <= first and len(data) >= 1 and first + len(data) - 1 <= DMX_CHANNELS):
        raise ValueError("DMX slots outside 1..512")
    return encode(TYPE_DMX, node, sequence, _DMX.pack(port, flags, first, len(data)) + bytes(data))


def build_audio(node: int, sequence: int, rate_hz: int, position: int, pcm: np.ndarray, flags: int = 0) -> bytes:
    """pcm: int16, shape (frames, channels) with 1 or 2 channels."""
    frames, channels = pcm.shape
    if not (1 <= channels <= 2 and 1 <= frames <= MAX_AUDIO_FRAMES):
        raise ValueError("audio block size")
    head = _AUDIO.pack(channels, flags, rate_hz, position & 0xFFFFFFFF, frames)
    return encode(TYPE_AUDIO, node, sequence, head + np.ascontiguousarray(pcm, dtype="<i2").tobytes())


def build_stop(node: int, sequence: int, what: int = STOP_DMX | STOP_AUDIO) -> bytes:
    return encode(TYPE_STOP, node, sequence, struct.pack("<BB", what, 0))


def parse_dmx(payload: bytes) -> tuple[int, int, int, bytes]:
    """(port, flags, first slot, data)."""
    if len(payload) < _DMX.size:
        raise LinkError("DMX packet too short")
    port, flags, first, count = _DMX.unpack_from(payload)
    if count < 1 or first < 1 or first + count - 1 > DMX_CHANNELS or len(payload) != _DMX.size + count:
        raise LinkError("DMX slots")
    return port, flags, first, payload[_DMX.size :]


def parse_audio(payload: bytes) -> tuple[int, int, int, np.ndarray]:
    """(flags, rate, position, int16 samples of shape (frames, channels))."""
    if len(payload) < _AUDIO.size:
        raise LinkError("audio packet too short")
    channels, flags, rate, position, frames = _AUDIO.unpack_from(payload)
    if not (1 <= channels <= 2 and 1 <= frames <= MAX_AUDIO_FRAMES and 1000 <= rate <= 48000):
        raise LinkError("audio format")
    if len(payload) != _AUDIO.size + 2 * frames * channels:
        raise LinkError("audio length")
    return flags, rate, position, np.frombuffer(payload, dtype="<i2", offset=_AUDIO.size).reshape(frames, channels)


@dataclass
class NodeInfo:
    """What a node says about itself (INFO)."""

    echo_us: int = 0
    uptime_ms: int = 0
    firmware: int = 0x0100
    capabilities: int = 0
    dmx_ports: int = 0
    audio_channels: int = 0
    state: int = 0
    audio_rate_hz: int = 4000
    audio_latency_ms: int = 120
    audio_level_ms: int = 0
    dmx_refresh_hz: int = 40
    packets: int = 0
    invalid: int = 0
    dmx_timeouts: int = 0
    audio_underruns: int = 0
    audio_gaps: int = 0

    def pack(self) -> bytes:
        return _INFO.pack(self.echo_us, self.uptime_ms, self.firmware, self.capabilities, self.dmx_ports, self.audio_channels,
                          self.state, self.audio_rate_hz, self.audio_latency_ms, self.audio_level_ms, self.dmx_refresh_hz, 0,
                          self.packets & 0xFFFFFFFF, self.invalid & 0xFFFF, self.dmx_timeouts & 0xFFFF,
                          self.audio_underruns & 0xFFFF, self.audio_gaps & 0xFFFF)

    @classmethod
    def unpack(cls, payload: bytes) -> NodeInfo:
        if len(payload) < _INFO.size:
            raise LinkError("INFO too short")
        v = _INFO.unpack_from(payload)
        return cls(v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[12], v[13], v[14], v[15], v[16])

    @property
    def has_dmx(self) -> bool:
        return bool(self.capabilities & CAP_DMX) and self.dmx_ports > 0

    @property
    def has_audio(self) -> bool:
        return bool(self.capabilities & CAP_AUDIO) and self.audio_channels > 0

    @property
    def inhibited(self) -> bool:
        return bool(self.state & STATE_INHIBITED)

    def has(self) -> list[str]:
        """In words: what this node can drive."""
        return [name for flag, name in ((CAP_ACTUATORS, "platform"), (CAP_DMX, "dmx"), (CAP_AUDIO, "audio")) if self.capabilities & flag]


# ------------------------------------------------------------------------------ controller side
class NodeAudio:
    """The bass and vibration stream to one node: rendered against the show position of the node's
    platform (or the theatre), a block per frame, far enough ahead for the node's buffer."""

    RESTART_AFTER_S = 0.25        # a stall longer than this: start a new stream instead of sending stale sound

    def __init__(self, cfg: AudioConfig, rate_hz: int, channels: int, latency_s: float) -> None:
        own = AudioConfig(
            enabled=True, sample_rate_hz=rate_hz, channels=channels,
            bass_channels=[c for c in cfg.bass_channels if c <= channels],
            vibration_channels=[c for c in cfg.vibration_channels if c <= channels],
            vibration_frequency_hz=cfg.vibration_frequency_hz, bass_gain_db=cfg.bass_gain_db,
            vibration_gain_db=cfg.vibration_gain_db, latency_offset_s=cfg.latency_offset_s,
        )
        self.renderer = BassRenderer(own)
        self.rate, self.channels, self.latency_s = rate_hz, channels, latency_s
        self.streaming = False
        self.blocks = 0
        self._t0 = 0.0
        self._sent = 0

    def step(self, state: AudioState, now: float) -> tuple[int, int, np.ndarray] | None:
        """The next block as (flags, position, int16 samples), or None when nothing is due."""
        if not state.active:
            return None
        flags = 0
        if not self.streaming or now - (self._t0 + self._sent / self.rate) > self.RESTART_AFTER_S:
            self.streaming, self._t0, self._sent, flags = True, now, 0, AUDIO_START
        due = int((now - self._t0) * self.rate) - self._sent
        if due < max(1, self.rate // 200) and not flags:
            return None
        frames = min(max(due, self.rate // 40), MAX_AUDIO_FRAMES)            # 25 ms at least, so that a stream starts at once
        block_time = self._t0 + self._sent / self.rate
        pcm = self.renderer.render(frames, state, block_time + self.latency_s)
        position, self._sent = self._sent, self._sent + frames
        self.blocks += 1
        return flags, position, np.round(np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)

    def stop(self) -> bool:
        """True if a stream was running (the node should be told to fall silent)."""
        was, self.streaming = self.streaming, False
        return was


@dataclass
class BridgeNode:
    host: str
    platform: str | None = None               # the platform this ESP32 belongs to (None: theatre devices only)
    lines: dict[int, str] = field(default_factory=dict)     # DMX port -> what is wired to it ("theatre", "PLATFORM-01")
    info: NodeInfo | None = None
    last_seen: float = -1e9
    last_hello: float = -1e9
    node_id: int = 0
    sequence: int = 0
    dmx_frames: int = 0
    dmx_skipped: int = 0
    audio: NodeAudio | None = None

    def present(self, now: float) -> bool:
        return self.info is not None and now - self.last_seen <= NODE_TIMEOUT_S

    def next_sequence(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        return self.sequence


class BridgeOutput(DmxOutput):
    """DMX frames and bass sound to ESP32 nodes.

    `lines` maps each universe of the engine to a physical line: (host, DMX port of that node, highest
    patched slot). Several universes may share a line (theatre devices on a platform's ESP32); they are
    merged, the highest value winning. `owners` says which platform each ESP32 belongs to, for the sound.
    """

    name = "esp32"

    def __init__(
        self,
        lines: dict[int, tuple[str, int, int]],
        owners: dict[str, str | None] | None = None,
        udp_port: int = DEFAULT_PORT,
        audio: AudioConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.lines = dict(lines)
        self.udp_port = int(udp_port)
        self.audio_cfg = audio
        self.clock = clock
        self.nodes: dict[str, BridgeNode] = {}
        for host, platform in (owners or {}).items():
            self.nodes[host] = BridgeNode(host, platform)
        for universe, (host, port, _slots) in self.lines.items():
            node = self.nodes.setdefault(host, BridgeNode(host))
            wired = "theatre" if universe < PLATFORM_UNIVERSE_BASE else (node.platform or f"universe {universe}")
            node.lines[port] = wired if port not in node.lines else f"{node.lines[port]} + {wired}"
        self._pending: dict[int, bytes] = {}
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------ transport
    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", 0))
        sock.setblocking(False)
        self._sock = sock
        self.is_open = True

    def close(self) -> None:
        now = self.clock()
        for _ in range(3):
            for node in self.nodes.values():
                if node.present(now):
                    self._to(node, build_stop(node.node_id, node.next_sequence()))
        for node in self.nodes.values():
            if node.audio is not None:
                node.audio.stop()
        if self._sock is not None:
            self._sock.close()
        self._sock = None
        self.is_open = False

    def _to(self, node: BridgeNode, packet: bytes) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(packet, (node.host, self.udp_port))
        except OSError as exc:        # no route, interface down: the node is just not there for now
            self._error(exc)

    def poll(self, now: float | None = None) -> None:
        """Read the nodes' answers and ask every node, present or not, once a second what it has."""
        now = self.clock() if now is None else now
        sock = self._sock
        if sock is None:
            return
        while True:
            try:
                packet, (host, _port) = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break                 # e.g. "connection refused" reported for a node that is switched off
            node = self.nodes.get(host)
            if node is None:
                continue
            try:
                ptype, node_id, _seq, payload = decode(packet)
                if ptype != TYPE_INFO:
                    continue
                info = NodeInfo.unpack(payload)
            except LinkError:
                continue
            if node.info is None or not node.present(now):
                log.info("effects node found", extra={"host": host, "node": node_id, "has": ",".join(info.has()) or "nothing"})
            node.info, node.node_id, node.last_seen = info, node_id, now
        for node in self.nodes.values():
            if now - node.last_hello >= HELLO_INTERVAL_S:
                node.last_hello = now
                self._to(node, build_hello(0, node.next_sequence(), int(now * 1e6)))
            if node.info is not None and not node.present(now) and node.audio is not None:
                node.audio.stop()

    # ------------------------------------------------------------------ DMX
    def _send(self, universe: int, data: bytes) -> None:
        self._pending[universe] = bytes(data)

    def flush(self, now: float | None = None) -> None:
        """Send what `send()` collected for this frame: one packet per line whose node has that line."""
        now = self.clock() if now is None else now
        self.poll(now)
        merged: dict[tuple[str, int], tuple[int, bytearray]] = {}
        for universe, data in self._pending.items():
            line = self.lines.get(universe)
            if line is None:
                continue
            host, port, slots = line
            have = merged.get((host, port))
            if have is None:
                merged[(host, port)] = (slots, bytearray(data[:DMX_CHANNELS].ljust(DMX_CHANNELS, b"\x00")))
            else:
                merged[(host, port)] = (max(have[0], slots), bytearray(max(a, b) for a, b in zip(have[1], data.ljust(DMX_CHANNELS, b"\x00"), strict=False)))
        self._pending = {}
        for (host, port), (slots, frame) in merged.items():
            node = self.nodes[host]
            info = node.info
            if info is None or not node.present(now) or not info.has_dmx or port >= info.dmx_ports:
                node.dmx_skipped += 1          # no such hardware (now): nothing to send, nothing to report as an error
                continue
            self._to(node, build_dmx(node.node_id, node.next_sequence(), port, 1, bytes(frame[: max(1, min(slots, DMX_CHANNELS))])))
            node.dmx_frames += 1

    # ------------------------------------------------------------------ sound
    def audio_nodes(self, now: float | None = None) -> list[BridgeNode]:
        """The nodes that have an audio output right now (their streams are created on first use)."""
        if self.audio_cfg is None or not self.audio_cfg.enabled:
            return []
        now = self.clock() if now is None else now
        out = []
        for node in self.nodes.values():
            info = node.info
            if info is None or not node.present(now) or not info.has_audio:
                continue
            if node.audio is None or node.audio.rate != info.audio_rate_hz or node.audio.channels != min(2, info.audio_channels):
                node.audio = NodeAudio(self.audio_cfg, info.audio_rate_hz, min(2, info.audio_channels), info.audio_latency_ms / 1000.0)
            out.append(node)
        return out

    def stream(self, node: BridgeNode, state: AudioState, now: float) -> None:
        """One step of a node's sound: the next block while its show plays, silence when it does not."""
        audio = node.audio
        if audio is None:
            return
        if not state.active:
            if audio.stop():
                for _ in range(2):
                    self._to(node, build_stop(node.node_id, node.next_sequence(), STOP_AUDIO))
            return
        block = audio.step(state, now)
        if block is not None:
            flags, position, pcm = block
            self._to(node, build_audio(node.node_id, node.next_sequence(), audio.rate, position, pcm, flags))

    # ------------------------------------------------------------------ reporting
    def describe(self) -> dict[str, Any]:
        now = self.clock()
        nodes = []
        for node in self.nodes.values():
            info, present = node.info, node.present(now)
            entry: dict[str, Any] = {"host": node.host, "present": present, "platform": node.platform,
                                     "lines": {str(p): w for p, w in sorted(node.lines.items())}}
            if info is not None and present:
                entry |= {
                    "node": node.node_id, "has": info.has(), "firmware": f"{info.firmware >> 8}.{info.firmware & 0xFF}",
                    "dmx_ports": info.dmx_ports, "audio_channels": info.audio_channels, "inhibited": info.inhibited,
                    "dmx_frames": node.dmx_frames, "dmx_timeouts": info.dmx_timeouts,
                    "audio_buffer_ms": info.audio_level_ms, "audio_underruns": info.audio_underruns, "audio_gaps": info.audio_gaps,
                    "invalid": info.invalid,
                }
            nodes.append(entry)
        return {**super().describe(), "nodes": nodes,
                "nodes_present": sum(1 for n in nodes if n["present"]), "nodes_configured": len(nodes)}


def create_output(patch: Patch, platform_ids: list[str], default_hosts: dict[str, str] | None = None) -> DmxOutput:
    """The DMX output a patch asks for. For "esp32" the lines are worked out from the platforms:
    `default_hosts` gives the ESP32 of each platform (its motion link) unless the patch names another."""
    if patch.output.protocol != "esp32":
        return patch.output.create()
    wires = patch.esp32_lines(platform_ids, default_hosts)
    slots: dict[int, int] = {}
    for inst in patch.instances(platform_ids):
        slots[inst.universe] = max(slots.get(inst.universe, 0), *inst.channels())
    lines = {u: (host, port, slots.get(u, 0)) for u, (host, port) in wires.items() if slots.get(u, 0) > 0}
    owners: dict[str, str | None] = {}
    for k, pid in enumerate(platform_ids):
        wire = wires.get(ESP32_PLATFORM_UNIVERSE + 1 + k)
        if wire is not None:
            owners.setdefault(wire[0], pid)          # the ESP32 of a platform plays that platform's sound
    theatre = wires.get(ESP32_THEATRE_UNIVERSES[0])
    if theatre is not None:
        owners.setdefault(theatre[0], None)          # an ESP32 for the theatre devices only follows the theatre show
    return BridgeOutput(lines, owners, int(patch.output.esp32.get("port", DEFAULT_PORT)), patch.output.esp32_audio)


# ------------------------------------------------------------------------------ a node in software
class SimNode:
    """A simulated effects node (UDP): answers HELLO, keeps the DMX frames and the sound it is sent.

    It follows the rules of the firmware (lib/fourdvr_fx): slots stay as last sent, the line goes dark
    after a second without DMX packets or on STOP, sound is a stream of numbered frames. For tests, for
    `fourdvr controller esp32-sim` and for trying a venue patch without hardware.
    """

    def __init__(self, node_id: int = 1, dmx_ports: int = 1, audio_channels: int = 2, actuators: bool = False,
                 host: str = "127.0.0.1", port: int = 0, audio_rate_hz: int = 4000, audio_latency_ms: int = 120,
                 keep_history: bool = True) -> None:
        self.node_id, self.dmx_ports, self.audio_channels = node_id, dmx_ports, audio_channels
        self.capabilities = (CAP_ACTUATORS if actuators else 0) | (CAP_DMX if dmx_ports else 0) | (CAP_AUDIO if audio_channels else 0)
        self.audio_rate_hz, self.audio_latency_ms = audio_rate_hz, audio_latency_ms
        self.dmx = [bytearray(DMX_CHANNELS) for _ in range(dmx_ports)]
        self.dmx_history: list[tuple[float, int, bytes]] = []
        self.audio_blocks: list[tuple[float, int, int, np.ndarray]] = []        # (received at, flags, position, samples)
        self.keep_history = keep_history
        self.inhibited = False
        self.packets = self.invalid = self.dmx_timeouts = self.audio_gaps = self.stops = 0
        self._last_dmx = [-1e9] * dmx_ports
        self._dmx_live = [False] * dmx_ports
        self._expected = 0
        self._last_audio = -1e9
        self._started = time.monotonic()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self._sock.settimeout(0.05)
        self.address: tuple[str, int] = self._sock.getsockname()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sim-fx-node-{node_id}", daemon=True)

    def start(self) -> SimNode:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        with contextlib.suppress(OSError):
            self._sock.close()

    def channel(self, port: int, slot: int) -> int:
        with self._lock:
            return self.dmx[port][slot - 1]

    def audio(self) -> np.ndarray:
        """The sound received so far, stream after stream, as (frames, channels) int16."""
        with self._lock:
            blocks = [b[3] for b in self.audio_blocks]
        return np.concatenate(blocks) if blocks else np.zeros((0, max(1, self.audio_channels)), np.int16)

    def info(self, echo_us: int = 0) -> NodeInfo:
        now = time.monotonic()
        state = (STATE_DMX_ACTIVE if any(self._dmx_live) else 0) | (STATE_AUDIO_PLAYING if now - self._last_audio < 0.3 else 0)
        state |= STATE_INHIBITED if self.inhibited else 0
        return NodeInfo(echo_us, int((now - self._started) * 1000), 0x0100, self.capabilities, self.dmx_ports, self.audio_channels,
                        state, self.audio_rate_hz, self.audio_latency_ms, 0, 40, self.packets, self.invalid,
                        self.dmx_timeouts, 0, self.audio_gaps)

    def _dark(self, port: int, now: float) -> None:
        self.dmx[port][:] = bytes(DMX_CHANNELS)
        self._dmx_live[port] = False
        if self.keep_history:
            self.dmx_history.append((now, port, bytes(DMX_CHANNELS)))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                packet, sender = self._sock.recvfrom(2048)
            except TimeoutError:
                packet = b""
            except OSError:
                return
            now = time.monotonic()
            with self._lock:
                for port in range(self.dmx_ports):
                    if self._dmx_live[port] and now - self._last_dmx[port] > NODE_DMX_TIMEOUT_S:
                        self.dmx_timeouts += 1
                        self._dark(port, now)
                if packet:
                    self._handle(packet, sender, now)

    def _handle(self, packet: bytes, sender: tuple[str, int], now: float) -> None:
        try:
            ptype, node, _seq, payload = decode(packet)
            if node not in (0, self.node_id):
                raise LinkError("another node's packet")
            if ptype == TYPE_HELLO:
                (echo,) = struct.unpack("<Q", payload[:8])
                self.packets += 1
                self._sock.sendto(encode(TYPE_INFO, self.node_id, self.packets, self.info(echo).pack()), sender)
            elif ptype == TYPE_DMX:
                port, _flags, first, data = parse_dmx(payload)
                self.packets += 1
                if port < self.dmx_ports:
                    frame = bytes(len(data)) if self.inhibited else data
                    self.dmx[port][first - 1 : first - 1 + len(frame)] = frame
                    self._last_dmx[port], self._dmx_live[port] = now, True
                    if self.keep_history:
                        self.dmx_history.append((now, port, bytes(self.dmx[port])))
            elif ptype == TYPE_AUDIO:
                flags, _rate, position, pcm = parse_audio(payload)
                self.packets += 1
                if self.audio_channels:
                    if flags & AUDIO_START:
                        self._expected = position
                    elif position != self._expected:
                        self.audio_gaps += 1
                    self._expected = (position + len(pcm)) & 0xFFFFFFFF
                    self._last_audio = now
                    if self.keep_history:
                        self.audio_blocks.append((now, flags, position, pcm.copy() if not self.inhibited else np.zeros_like(pcm)))
            elif ptype == TYPE_STOP:
                self.packets += 1
                self.stops += 1
                if payload and payload[0] & STOP_DMX:
                    for port in range(self.dmx_ports):
                        self._dark(port, now)
                if payload and payload[0] & STOP_AUDIO:
                    self._last_audio = -1e9
            else:
                raise LinkError("unknown type")
        except (LinkError, struct.error):
            self.invalid += 1
