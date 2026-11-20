"""Settings of the ways a platform's actuator controller is reached: Modbus TCP and a configurable UDP format.

Only the profile's tables are read here (``[modbus]``, ``[udp]``); the drivers are in modbus.py and
udp_device.py. The ESP32 board needs no table: it says what it has (docs/ESP32_PROTOCOL.md).
See docs/ACTUATORS.md for every key.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from .kinematics import AXES

# value types: struct code, registers (Modbus), lowest and highest value (None: a float)
TYPES: dict[str, tuple[str, int, float | None, float | None]] = {
    "u16": ("H", 1, 0, 0xFFFF),
    "i16": ("h", 1, -0x8000, 0x7FFF),
    "u32": ("I", 2, 0, 0xFFFFFFFF),
    "i32": ("i", 2, -0x80000000, 0x7FFFFFFF),
    "f32": ("f", 2, None, None),
    "f64": ("d", 4, None, None),
    "u8": ("B", 1, 0, 0xFF),
}
STATES = {"IDLE": 0, "RUN": 1, "HOLD": 2, "PARK": 3, "FAULT_RESET": 4}


class InterfaceError(ValueError):
    pass


def _int(table: dict[str, Any], key: str, default: int, low: int, high: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InterfaceError(f"[{where}] {key} must be a whole number {low}..{high}")
    return value


def _pair(table: dict[str, Any], key: str, where: str) -> tuple[float, float] | None:
    value = table.get(key)
    if value is None:
        return None
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int | float) and not isinstance(v, bool)
                                                               for v in value)) or value[0] == value[1]:
        raise InterfaceError(f"[{where}] {key} must be [value when fully retracted, value when fully extended] (two different numbers)")
    return float(value[0]), float(value[1])


def _type(table: dict[str, Any], key: str, default: str, where: str, allowed: tuple[str, ...]) -> str:
    value = str(table.get(key, default))
    if value not in allowed:
        raise InterfaceError(f"[{where}] {key} must be one of {', '.join(allowed)}")
    return value


def _fits(value: tuple[float, float] | None, kind: str, key: str, where: str) -> None:
    lo, hi = TYPES[kind][2], TYPES[kind][3]
    if value is not None and lo is not None and hi is not None and not all(lo <= v <= hi for v in value):
        raise InterfaceError(f"[{where}] {key} does not fit into {kind} ({lo:g}..{hi:g})")


def _unknown(table: dict[str, Any], known: set[str], where: str) -> None:
    extra = sorted(set(table) - known)
    if extra:
        raise InterfaceError(f"[{where}]: unknown key(s) {', '.join(extra)}")


@dataclass(frozen=True)
class ModbusSettings:
    """Where the set-points and the feedback are in a Modbus TCP device's registers (protocol addresses, from 0)."""

    unit: int = 1
    setpoints: int = 0                  # holding register of actuator 1's set-point; the others follow
    type: str = "u16"
    word_order: str = "big"             # 32-bit values: big = high word first
    range: tuple[float, float] = (0.0, 10000.0)
    positions: int = -1                 # register of actuator 1's measured position (-1: the device reports none)
    positions_table: str = "input"      # input | holding
    positions_range: tuple[float, float] | None = None
    command: int = -1                   # holding register for the command state (0 idle, 1 run, 2 hold, 3 park, 4 reset)
    status: int = -1                    # register with the device's ready and fault bits
    status_table: str = "input"
    ready_bit: int = -1
    fault_bit: int = -1
    heartbeat: int = -1                 # holding register counted up every cycle: the device's watchdog
    enable_coil: int = -1               # coil set while the platform may move
    valves: int = -1                    # on/off control: coil of actuator 1's valve; the others follow
    period_ms: float = 10.0
    timeout_ms: float = 50.0

    @property
    def registers(self) -> int:
        return TYPES[self.type][1]

    def feedback_range(self) -> tuple[float, float]:
        return self.positions_range or self.range


def parse_modbus(table: dict[str, Any]) -> ModbusSettings:
    w = "modbus"
    _unknown(table, {"unit", "setpoints", "type", "word_order", "range", "positions", "positions_table", "positions_range",
                     "command", "status", "status_table", "ready_bit", "fault_bit", "heartbeat", "enable_coil", "valves",
                     "period_ms", "timeout_ms"}, w)
    kind = _type(table, "type", "u16", w, ("u16", "i16", "u32", "i32", "f32"))
    s = ModbusSettings(
        unit=_int(table, "unit", 1, 0, 255, w),
        setpoints=_int(table, "setpoints", 0, 0, 65535, w),
        type=kind,
        word_order=_type(table, "word_order", "big", w, ("big", "little")),
        range=_pair(table, "range", w) or (0.0, 10000.0),
        positions=_int(table, "positions", -1, -1, 65535, w),
        positions_table=_type(table, "positions_table", "input", w, ("input", "holding")),
        positions_range=_pair(table, "positions_range", w),
        command=_int(table, "command", -1, -1, 65535, w),
        status=_int(table, "status", -1, -1, 65535, w),
        status_table=_type(table, "status_table", "input", w, ("input", "holding")),
        ready_bit=_int(table, "ready_bit", -1, -1, 15, w),
        fault_bit=_int(table, "fault_bit", -1, -1, 15, w),
        heartbeat=_int(table, "heartbeat", -1, -1, 65535, w),
        enable_coil=_int(table, "enable_coil", -1, -1, 65535, w),
        valves=_int(table, "valves", -1, -1, 65535, w),
        period_ms=float(table.get("period_ms", 10.0)),
        timeout_ms=float(table.get("timeout_ms", 50.0)),
    )
    _fits(s.range, kind, "range", w)
    _fits(s.positions_range, kind, "positions_range", w)
    if not 2.0 <= s.period_ms <= 100.0:
        raise InterfaceError("[modbus] period_ms must be 2-100")
    if not 5.0 <= s.timeout_ms <= 1000.0:
        raise InterfaceError("[modbus] timeout_ms must be 5-1000")
    if (s.ready_bit >= 0 or s.fault_bit >= 0) and s.status < 0:
        raise InterfaceError("[modbus] ready_bit / fault_bit need status = <register>")
    return s


@dataclass(frozen=True)
class UdpSettings:
    """A datagram per control period: an optional header, counter and state, then the values."""

    send: str = "positions"             # positions | pose | valves
    type: str = "f32"
    byte_order: str = "little"
    range: tuple[float, float] | None = None   # positions: value at fully retracted / extended (None: metres)
    pose_scale: tuple[float, ...] = (1.0,) * 6  # pose: units per metre / per radian, per axis
    pose_axes: tuple[str, ...] = ()     # pose: the axes sent, in order (empty: the platform's own)
    header: bytes = b""
    counter: str = ""                   # "" | u8 | u16 | u32: a packet counter after the header
    state: bool = False                 # one byte with the command state after the counter
    feedback: bool = False              # the device answers with its measured positions in the same layout

    def code(self) -> str:
        return ("<" if self.byte_order == "little" else ">")

    def value_struct(self, count: int) -> struct.Struct:
        return struct.Struct(self.code() + TYPES[self.type][0] * count)

    def prefix_size(self) -> int:
        size = len(self.header) + (struct.calcsize(TYPES[self.counter][0]) if self.counter else 0)
        return size + (1 if self.state else 0)


def parse_udp(table: dict[str, Any], dof: tuple[str, ...]) -> UdpSettings:
    w = "udp"
    _unknown(table, {"send", "type", "byte_order", "range", "pose_scale", "pose_axes", "header", "counter", "state", "feedback"}, w)
    send = _type(table, "send", "positions", w, ("positions", "pose", "valves"))
    kind = _type(table, "type", "f32", w, ("f32", "f64", "i16", "u16", "i32", "u32"))
    header_text = str(table.get("header", ""))
    try:
        header = bytes.fromhex(header_text.replace("0x", "").replace(",", " "))
    except ValueError as exc:
        raise InterfaceError('[udp] header must be hexadecimal bytes, e.g. "46 44 4D 01"') from exc
    if len(header) > 32:
        raise InterfaceError("[udp] header: at most 32 bytes")
    scale = table.get("pose_scale", 1.0)
    scales = (float(scale),) * 6 if isinstance(scale, int | float) and not isinstance(scale, bool) else tuple(float(v) for v in scale)
    if len(scales) != 6 or not all(v != 0 for v in scales):
        raise InterfaceError("[udp] pose_scale must be one number or six (surge, sway, heave, roll, pitch, yaw), none of them 0")
    axes = tuple(str(a) for a in table.get("pose_axes", dof))
    if not axes or any(a not in AXES for a in axes) or len(set(axes)) != len(axes):
        raise InterfaceError(f"[udp] pose_axes must name axes of {', '.join(AXES)}, each once")
    s = UdpSettings(
        send=send, type=kind, byte_order=_type(table, "byte_order", "little", w, ("little", "big")),
        range=_pair(table, "range", w), pose_scale=scales, pose_axes=axes, header=header,
        counter=_type(table, "counter", "", w, ("", "u8", "u16", "u32")),
        state=bool(table.get("state", False)), feedback=bool(table.get("feedback", False)),
    )
    if send != "valves":
        _fits(s.range, kind, "range", w)
    if send == "pose" and s.feedback:
        raise InterfaceError("[udp] feedback is read as actuator positions: it cannot be used with send = \"pose\"")
    return s


def scale_to(value_fraction: float, rng: tuple[float, float], kind: str) -> float | int:
    """A stroke fraction (0 = retracted, 1 = extended) as the device's number."""
    lo, hi = rng
    raw = lo + value_fraction * (hi - lo)
    vmin, vmax = TYPES[kind][2], TYPES[kind][3]
    if vmin is None or vmax is None:
        return raw
    return int(min(max(round(raw), vmin), vmax))


def scale_from(raw: float, rng: tuple[float, float]) -> float:
    """The device's number as a stroke fraction."""
    lo, hi = rng
    return (raw - lo) / (hi - lo)
