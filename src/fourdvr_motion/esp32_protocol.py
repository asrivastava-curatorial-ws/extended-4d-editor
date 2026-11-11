"""Binary link protocol between the Motion Controller and the ESP32 actuator bridge (version 1.0).

Specification: ESP32_PROTOCOL.md. The firmware implements the same layout in
firmware/esp32-actuator-bridge/lib/fourdvr_link/src/fourdvr_link.{h,cpp}; both sides are checked
against the shared test vectors in firmware/esp32-actuator-bridge/test/vectors.txt.

All integers are little-endian. Every packet ends with CRC-32 (IEEE 802.3 / zlib polynomial)
computed over all preceding bytes. UDP carries one packet per datagram; serial links frame each
packet with COBS and a 0x00 delimiter.

COMMAND (controller -> ESP32), 52 bytes
  0  4  magic "4DMC"           4  u8  version major (1)   5  u8 version minor (0)
  6  u8 type = 1               7  u8  platform id (1..250)
  8  u32 sequence             12  u64 timestamp (master clock, microseconds)
  20 u8 command state         21  u8  flags (bit0 targets valid)        22 u16 reserved
  24 6 x i32 actuator stroke targets (micrometres from the retracted end)
  48 u32 CRC-32

STATUS (ESP32 -> controller), 72 bytes
  0  4  magic "4DMS"  4 u8 major  5 u8 minor  6 u8 type = 2  7 u8 platform id
  8  u32 last accepted sequence   12 u64 echoed timestamp of that command
  20 u32 uptime (ms)  24 u8 device state  25 u8 fault flags  26 u16 reserved
  28 6 x i32 actuator positions (micrometres; commanded output when no feedback exists)
  52 u32 packets received   56 u16 lost  58 u16 duplicate  60 u16 invalid  62 u16 stale
  64 u16 timeouts           66 u16 reserved
  68 u32 CRC-32
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag

VERSION_MAJOR = 1
VERSION_MINOR = 0
CMD_MAGIC = b"4DMC"
STATUS_MAGIC = b"4DMS"
TYPE_COMMAND = 1
TYPE_STATUS = 2
DEFAULT_UDP_PORT = 47000

_CMD = struct.Struct("<4sBBBBIQBBH6i")
_STATUS = struct.Struct("<4sBBBBIQIBBH6iIHHHHHH")
CMD_SIZE = _CMD.size + 4
STATUS_SIZE = _STATUS.size + 4
assert CMD_SIZE == 52 and STATUS_SIZE == 72


class CommandState(IntEnum):
    IDLE = 0  # hold outputs, no tracking
    RUN = 1  # track the targets
    HOLD = 2  # hold the current position
    PARK = 3  # ramp (firmware speed limit) to the park position
    FAULT_RESET = 4  # request to clear latched faults


class DeviceState(IntEnum):
    BOOT = 0
    IDLE = 1
    RUNNING = 2
    HOLD = 3
    PARKING = 4
    PARKED = 5
    FAULT = 6


class Fault(IntFlag):
    NONE = 0
    TIMEOUT = 1
    INVALID_BURST = 2
    STALE = 4
    OUTPUT = 8
    ESTOP_INPUT = 16
    CONFIG = 32


class PacketError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # length | magic | version | type | crc


@dataclass(frozen=True)
class Command:
    platform_id: int
    sequence: int
    timestamp_us: int
    state: CommandState
    targets_um: tuple[int, ...]
    flags: int = 1


@dataclass(frozen=True)
class Status:
    platform_id: int
    last_sequence: int
    echo_timestamp_us: int
    uptime_ms: int
    device_state: DeviceState
    faults: Fault
    positions_um: tuple[int, ...]
    received: int
    lost: int
    duplicate: int
    invalid: int
    stale: int
    timeouts: int


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _check(data: bytes, size: int, magic: bytes, ptype: int) -> None:
    if len(data) != size:
        raise PacketError("length", f"packet is {len(data)} bytes, expected {size}")
    if data[:4] != magic:
        raise PacketError("magic", "bad magic")
    if data[4] != VERSION_MAJOR:
        raise PacketError("version", f"unsupported protocol major version {data[4]}")
    if data[6] != ptype:
        raise PacketError("type", f"unexpected packet type {data[6]}")
    (crc,) = struct.unpack_from("<I", data, size - 4)
    if crc != crc32(data[: size - 4]):
        raise PacketError("crc", "CRC mismatch")


def encode_command(cmd: Command) -> bytes:
    if len(cmd.targets_um) != 6:
        raise ValueError("six actuator targets required")
    body = _CMD.pack(
        CMD_MAGIC,
        VERSION_MAJOR,
        VERSION_MINOR,
        TYPE_COMMAND,
        cmd.platform_id,
        cmd.sequence & 0xFFFFFFFF,
        cmd.timestamp_us & 0xFFFFFFFFFFFFFFFF,
        int(cmd.state),
        cmd.flags,
        0,
        *[int(v) for v in cmd.targets_um],
    )
    return body + struct.pack("<I", crc32(body))


def decode_command(data: bytes) -> Command:
    _check(data, CMD_SIZE, CMD_MAGIC, TYPE_COMMAND)
    f = _CMD.unpack_from(data)
    try:
        state = CommandState(f[7])
    except ValueError as exc:
        raise PacketError("type", f"unknown command state {f[7]}") from exc
    return Command(
        platform_id=f[4], sequence=f[5], timestamp_us=f[6], state=state, targets_um=tuple(f[10:16]), flags=f[8]
    )


def encode_status(st: Status) -> bytes:
    body = _STATUS.pack(
        STATUS_MAGIC,
        VERSION_MAJOR,
        VERSION_MINOR,
        TYPE_STATUS,
        st.platform_id,
        st.last_sequence & 0xFFFFFFFF,
        st.echo_timestamp_us & 0xFFFFFFFFFFFFFFFF,
        st.uptime_ms & 0xFFFFFFFF,
        int(st.device_state),
        int(st.faults),
        0,
        *[int(v) for v in st.positions_um],
        st.received & 0xFFFFFFFF,
        min(st.lost, 0xFFFF),
        min(st.duplicate, 0xFFFF),
        min(st.invalid, 0xFFFF),
        min(st.stale, 0xFFFF),
        min(st.timeouts, 0xFFFF),
        0,
    )
    return body + struct.pack("<I", crc32(body))


def decode_status(data: bytes) -> Status:
    _check(data, STATUS_SIZE, STATUS_MAGIC, TYPE_STATUS)
    f = _STATUS.unpack_from(data)
    return Status(
        platform_id=f[4],
        last_sequence=f[5],
        echo_timestamp_us=f[6],
        uptime_ms=f[7],
        device_state=DeviceState(f[8]) if f[8] in DeviceState._value2member_map_ else DeviceState.FAULT,
        faults=Fault(f[9] & 0x3F),
        positions_um=tuple(f[11:17]),
        received=f[17],
        lost=f[18],
        duplicate=f[19],
        invalid=f[20],
        stale=f[21],
        timeouts=f[22],
    )


# ------------------------------------------------------------------ COBS framing (serial)
def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    block = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise PacketError("length", "zero byte inside COBS frame")
        i += 1
        end = i + code - 1
        if end > n:
            raise PacketError("length", "truncated COBS frame")
        out += data[i:end]
        i = end
        if code < 255 and i < n:
            out.append(0)
    return bytes(out)


class SequenceTracker:
    """Duplicate / out-of-order / loss detection on a u32 sequence (used by both link ends)."""

    def __init__(self) -> None:
        self.last: int | None = None
        self.lost = 0
        self.duplicate = 0

    def accept(self, seq: int) -> bool:
        if self.last is None:
            self.last = seq
            return True
        diff = (seq - self.last) & 0xFFFFFFFF
        if diff == 0 or diff >= 0x80000000:  # same or older (wrap-aware)
            self.duplicate += 1
            return False
        self.lost += diff - 1
        self.last = seq
        return True
