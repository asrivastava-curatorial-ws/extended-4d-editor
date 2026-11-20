"""Modbus TCP: the client the show service uses, the driver, and a simulated device for bench tests.

Only what a platform needs is implemented - read coils / holding / input registers, write a
coil, a register, several coils, several registers (function codes 1, 3, 4, 5, 6, 15, 16) - with
strict time-outs, so a slow or absent device can never hold up the 100 Hz control loop: the driver
talks to the device from a thread of its own and the loop only reads what that thread last saw.

The device decides what happens when the show service stops talking. It should watch the
``heartbeat`` register (or the connection) and park its actuators itself: see docs/ACTUATORS.md.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .drivers import ActuatorCommand, ActuatorDriver, LinkStatus, ParkRamp, park_strokes
from .esp32_protocol import CommandState, DeviceState
from .interfaces import STATES, TYPES, ModbusSettings, scale_from, scale_to
from .plants import Plant, plant_for
from .platform_config import PlatformConfig

EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "server device failure",
              6: "server device busy"}


class ModbusError(OSError):
    pass


# ---------------------------------------------------------------------------------------------- values
def encode_values(values: list[float | int], kind: str, word_order: str = "big") -> list[int]:
    code, regs, _lo, _hi = TYPES[kind]
    out: list[int] = []
    for v in values:
        raw = struct.pack(">" + code, v if code in "fd" else int(v))
        words = list(struct.unpack(">" + "H" * regs, raw))
        if word_order == "little":
            words.reverse()
        out.extend(words)
    return out


def decode_values(registers: list[int], kind: str, word_order: str = "big") -> list[float]:
    code, regs, _lo, _hi = TYPES[kind]
    out: list[float] = []
    for k in range(0, len(registers) - regs + 1, regs):
        words = list(registers[k:k + regs])
        if word_order == "little":
            words.reverse()
        out.append(float(struct.unpack(">" + code, struct.pack(">" + "H" * regs, *words))[0]))
    return out


def _bits_to_bytes(bits: list[bool]) -> bytes:
    data = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            data[i // 8] |= 1 << (i % 8)
    return bytes(data)


# ---------------------------------------------------------------------------------------------- client
class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 0.05) -> None:
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self.sock: socket.socket | None = None
        self.tid = 0
        self.requests = 0
        self.errors = 0

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=max(self.timeout, 0.2))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        self.sock = sock

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self, n: int) -> bytes:
        assert self.sock is not None
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ModbusError("the device closed the connection")
            data += chunk
        return data

    def request(self, pdu: bytes) -> bytes:
        if self.sock is None:
            self.connect()
        assert self.sock is not None
        self.tid = (self.tid + 1) & 0xFFFF
        self.requests += 1
        try:
            self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu)
            while True:
                tid, proto, length, unit = struct.unpack(">HHHB", self._recv(7))
                body = self._recv(length - 1) if length > 1 else b""
                if tid == self.tid:
                    break                      # an answer to an older request that timed out is skipped
            if proto != 0 or unit != self.unit or not body:
                raise ModbusError("malformed answer")
            if body[0] == pdu[0] | 0x80:
                code = body[1] if len(body) > 1 else 0
                raise ModbusError(f"exception {code} ({EXCEPTIONS.get(code, 'unknown')}) for function {pdu[0]}")
            if body[0] != pdu[0]:
                raise ModbusError("answer to another function")
            return body
        except (OSError, struct.error) as exc:
            self.errors += 1
            self.close()                       # the next request connects again
            if isinstance(exc, ModbusError):
                raise
            raise ModbusError(str(exc) or type(exc).__name__) from exc

    def read_registers(self, address: int, count: int, table: str = "holding") -> list[int]:
        body = self.request(struct.pack(">BHH", 3 if table == "holding" else 4, address, count))
        if len(body) != 2 + 2 * count or body[1] != 2 * count:
            raise ModbusError("wrong number of registers in the answer")
        return list(struct.unpack(">" + "H" * count, body[2:]))

    def read_coils(self, address: int, count: int) -> list[bool]:
        body = self.request(struct.pack(">BHH", 1, address, count))
        return [bool(body[2 + i // 8] >> (i % 8) & 1) for i in range(count)]

    def write_register(self, address: int, value: int) -> None:
        self.request(struct.pack(">BHH", 6, address, value & 0xFFFF))

    def write_registers(self, address: int, values: list[int]) -> None:
        self.request(struct.pack(">BHHB", 16, address, len(values), 2 * len(values)) + struct.pack(">" + "H" * len(values), *values))

    def write_coil(self, address: int, on: bool) -> None:
        self.request(struct.pack(">BHH", 5, address, 0xFF00 if on else 0))

    def write_coils(self, address: int, bits: list[bool]) -> None:
        data = _bits_to_bytes(bits)
        self.request(struct.pack(">BHHB", 15, address, len(bits), len(data)) + data)


# ---------------------------------------------------------------------------------------------- driver
class ModbusDriver(ActuatorDriver):
    kind = "modbus_tcp"

    def __init__(self, cfg: PlatformConfig, client: ModbusTcpClient | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        assert cfg.modbus is not None
        self.cfg = cfg
        self.m: ModbusSettings = cfg.modbus
        host, _, port = cfg.address.removeprefix("tcp://").partition(":")
        self.client = client or ModbusTcpClient(host, int(port or 502), self.m.unit, self.m.timeout_ms / 1000.0)
        self.clock = clock
        self.legs = cfg.legs
        self.stroke = cfg.actuators.stroke
        self.ramp = ParkRamp(cfg)
        self.timeout_s = cfg.link_timeout_ms / 1000.0
        self._lock = threading.Lock()
        self._command = ActuatorCommand(CommandState.IDLE, park_strokes(cfg), 0.0)
        self._positions: np.ndarray | None = None
        self._ready: bool | None = None
        self._device_fault = False
        self._last_ok: float | None = None
        self._problem = ""
        self._opened_at = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cycles = 0
        self.heartbeat = 0
        self._enabled: bool | None = None
        self.link_faults = 0
        self._was_connected = False

    def open(self) -> None:
        self._opened_at = self.clock()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"modbus-{self.cfg.platform_id}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self.client.close()

    def send(self, command: ActuatorCommand) -> None:
        with self._lock:
            self._command = command

    # ----------------------------------------------------------------------- the device's thread
    def _cycle(self) -> None:
        m = self.m
        with self._lock:
            command = self._command
        out = self.ramp.apply(command, self.clock())
        frac = np.clip(out / self.stroke, 0.0, 1.0)
        if self.cfg.control == "onoff" and m.valves >= 0:
            self.client.write_coils(m.valves, [bool(f >= 0.5) for f in frac])
        else:
            if self.cfg.control == "onoff":
                frac = (frac >= 0.5).astype(np.float64)
            values = [scale_to(float(f), m.range, m.type) for f in frac]
            self.client.write_registers(m.setpoints, encode_values(values, m.type, m.word_order))
        state = self.ramp.state
        if m.command >= 0:
            code = STATES["FAULT_RESET"] if command.state == CommandState.FAULT_RESET else STATES.get(command.state.name, 0)
            self.client.write_register(m.command, code)
        if m.heartbeat >= 0:
            self.heartbeat = (self.heartbeat + 1) & 0xFFFF
            self.client.write_register(m.heartbeat, self.heartbeat)
        if m.enable_coil >= 0:
            enable = state in (DeviceState.RUNNING, DeviceState.HOLD, DeviceState.PARKING)
            if enable != self._enabled:
                self.client.write_coil(m.enable_coil, enable)
                self._enabled = enable
        positions = None
        if m.positions >= 0:
            regs = self.client.read_registers(m.positions, self.legs * m.registers, m.positions_table)
            raw = decode_values(regs, m.type, m.word_order)
            positions = np.array([scale_from(v, m.feedback_range()) for v in raw]) * self.stroke
        ready, fault = None, False
        if m.status >= 0:
            word = self.client.read_registers(m.status, 1, m.status_table)[0]
            ready = bool(word >> m.ready_bit & 1) if m.ready_bit >= 0 else None
            fault = bool(word >> m.fault_bit & 1) if m.fault_bit >= 0 else False
        with self._lock:
            self._positions = positions
            self._ready, self._device_fault = ready, fault
            self._last_ok = self.clock()
            self._problem = ""
            self.cycles += 1

    def _run(self) -> None:
        period = self.m.period_ms / 1000.0
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._cycle()
            except (ModbusError, OSError) as exc:
                with self._lock:
                    self._problem = str(exc)
                self._stop.wait(min(0.2, max(period, self.timeout_s / 4)))   # an absent device is asked again soon
                next_at = time.monotonic()
                continue
            next_at += period
            delay = next_at - time.monotonic()
            if delay < -period:
                next_at = time.monotonic()
            self._stop.wait(max(0.0, delay))

    # ----------------------------------------------------------------------- what the runtime sees
    def poll(self, now: float) -> LinkStatus:
        with self._lock:
            last, positions, ready, fault, problem = self._last_ok, self._positions, self._ready, self._device_fault, self._problem
            cycles = self.cycles
        age = None if last is None else (now - last) * 1000.0
        connected = age is not None and age <= self.timeout_s * 1000.0
        if self._was_connected and not connected:
            self.link_faults += 1
        self._was_connected = connected
        faults: list[str] = []
        if fault:
            faults.append("DEVICE: the device reports a fault (status register)")
        if ready is False and self.ramp.state in (DeviceState.RUNNING, DeviceState.HOLD):   # as the ESP32 board: a stopped pump is no fault at rest
            faults.append("SUPPLY: the device is not ready (pressure, pump or drive)")
        startup = now - self._opened_at < max(1.0, 4 * self.timeout_s)
        state = self.ramp.state.name if connected else ("STARTING" if startup and last is None else "NO_REPLY")
        return LinkStatus(
            connected=connected,
            last_status_age_ms=age,
            device_state=state,
            faults=faults,
            positions_m=positions if positions is not None else (self.ramp.output.copy() if last is not None else None),
            rtt_ms=None,
            counters={"cycles": cycles, "requests": self.client.requests, "errors": self.client.errors,
                      "link_faults": self.link_faults, **({"problem": problem} if problem else {})},  # type: ignore[dict-item]
            measured=positions is not None,
        )


# ---------------------------------------------------------------------------------------------- a simulated device
class RegisterBank:
    def __init__(self) -> None:
        self.holding = [0] * 65536
        self.input = [0] * 65536
        self.coils = [False] * 65536
        self.lock = threading.Lock()
        self.writes = 0


def handle_pdu(bank: RegisterBank, pdu: bytes) -> bytes:
    """One request on the register bank (a Modbus server's whole job)."""
    fc = pdu[0]
    try:
        with bank.lock:
            if fc in (3, 4):
                addr, count = struct.unpack(">HH", pdu[1:5])
                if not 1 <= count <= 125 or addr + count > 65536:
                    return bytes([fc | 0x80, 3 if count > 125 or count < 1 else 2])
                regs = (bank.holding if fc == 3 else bank.input)[addr:addr + count]
                return struct.pack(">BB", fc, 2 * count) + struct.pack(">" + "H" * count, *regs)
            if fc == 1:
                addr, count = struct.unpack(">HH", pdu[1:5])
                if not 1 <= count <= 2000 or addr + count > 65536:
                    return bytes([fc | 0x80, 2])
                data = _bits_to_bytes(bank.coils[addr:addr + count])
                return struct.pack(">BB", fc, len(data)) + data
            if fc == 5:
                addr, value = struct.unpack(">HH", pdu[1:5])
                if value not in (0, 0xFF00):
                    return bytes([fc | 0x80, 3])
                bank.coils[addr] = value == 0xFF00
                bank.writes += 1
                return pdu[:5]
            if fc == 6:
                addr, value = struct.unpack(">HH", pdu[1:5])
                bank.holding[addr] = value
                bank.writes += 1
                return pdu[:5]
            if fc == 15:
                addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                if not 1 <= count <= 1968 or addr + count > 65536 or nbytes != (count + 7) // 8 or len(pdu) != 6 + nbytes:
                    return bytes([fc | 0x80, 3])
                for i in range(count):
                    bank.coils[addr + i] = bool(pdu[6 + i // 8] >> (i % 8) & 1)
                bank.writes += 1
                return pdu[:5]
            if fc == 16:
                addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                if not 1 <= count <= 123 or addr + count > 65536 or nbytes != 2 * count or len(pdu) != 6 + nbytes:
                    return bytes([fc | 0x80, 3])
                bank.holding[addr:addr + count] = list(struct.unpack(">" + "H" * count, pdu[6:6 + nbytes]))
                bank.writes += 1
                return pdu[:5]
    except struct.error:
        return bytes([fc | 0x80, 3])
    return bytes([fc | 0x80, 1])


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        server: Any = self.server
        while True:
            try:
                head = sock.recv(7, socket.MSG_WAITALL)
                if len(head) < 7:
                    return
                tid, proto, length, unit = struct.unpack(">HHHB", head)
                pdu = sock.recv(length - 1, socket.MSG_WAITALL) if length > 1 else b""
                if proto != 0 or len(pdu) != length - 1 or not pdu:
                    return
                if server.unit not in (0, unit) or server.silent:
                    continue                              # another unit, or "unplugged": no answer
                answer = handle_pdu(server.bank, pdu)
                sock.sendall(struct.pack(">HHHB", tid, 0, len(answer) + 1, unit) + answer)
            except OSError:
                return


class ModbusSimDevice:
    """A PLC / valve controller with the profile's register map, and simulated actuators of the profile's kind.

    It does what a careful device should: it follows the set-points while the command register (if
    any) says RUN, ramps to the park position on PARK, and - if a heartbeat register is configured -
    parks by itself and reports a fault when the heartbeat stops counting.
    """

    def __init__(self, cfg: PlatformConfig, port: int = 1502, host: str = "127.0.0.1", watchdog_ms: float = 300.0) -> None:
        assert cfg.modbus is not None
        self.cfg = cfg
        self.m = cfg.modbus
        self.bank = RegisterBank()
        self.park = park_strokes(cfg)
        self.plant: Plant = plant_for(cfg, self.park)
        self.watchdog_s = watchdog_ms / 1000.0
        self.ready = True                       # test hook: the ready bit
        self.fault = False                      # latched by the watchdog; cleared by command 4 (reset)
        self.state = "IDLE"
        self._target = self.park.copy()
        self._beat: tuple[int, float] | None = None

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server((host, port), _Handler)
        self.server.bank = self.bank             # type: ignore[attr-defined]
        self.server.unit = self.m.unit           # type: ignore[attr-defined]
        self.server.silent = False               # type: ignore[attr-defined]
        self.port = int(self.server.server_address[1])
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._publish()

    @property
    def silent(self) -> bool:
        return bool(self.server.silent)         # type: ignore[attr-defined]

    @silent.setter
    def silent(self, value: bool) -> None:
        self.server.silent = value               # type: ignore[attr-defined]

    def _setpoints(self) -> np.ndarray:
        m = self.m
        with self.bank.lock:
            if self.cfg.control == "onoff" and m.valves >= 0:
                return np.array([self.plant.stroke[i] if self.bank.coils[m.valves + i] else 0.0 for i in range(self.cfg.legs)])
            regs = self.bank.holding[m.setpoints:m.setpoints + self.cfg.legs * m.registers]
        raw = decode_values(regs, m.type, m.word_order)
        return np.clip(np.array([scale_from(v, m.range) for v in raw]), 0.0, 1.0) * self.plant.stroke

    def _publish(self) -> None:
        m = self.m
        with self.bank.lock:
            if m.positions >= 0:
                frac = self.plant.pos / self.plant.stroke
                values = [scale_to(float(f), m.feedback_range(), m.type) for f in frac]
                regs = encode_values(values, m.type, m.word_order)
                table = self.bank.input if m.positions_table == "input" else self.bank.holding
                table[m.positions:m.positions + len(regs)] = regs
            if m.status >= 0:
                word = (1 << m.ready_bit if m.ready_bit >= 0 and self.ready else 0) | \
                       (1 << m.fault_bit if m.fault_bit >= 0 and self.fault else 0)
                (self.bank.input if m.status_table == "input" else self.bank.holding)[m.status] = word

    def step(self, dt: float, now: float) -> None:
        m = self.m
        with self.bank.lock:
            command = self.bank.holding[m.command] if m.command >= 0 else STATES["RUN"]
            beat = self.bank.holding[m.heartbeat] if m.heartbeat >= 0 else None
        if command == STATES["FAULT_RESET"] and self.ready:
            self.fault = False
        if beat is not None:
            if self._beat is None or beat != self._beat[0]:
                self._beat = (beat, now)
            elif now - self._beat[1] > self.watchdog_s and not self.fault and self.state in ("RUN", "HOLD"):
                self.fault = True               # the show service stopped talking: park
        if self.fault or command == STATES["PARK"] or not self.ready:
            self.state = "PARK"
            speed = self.cfg.actuators.max_velocity * self.cfg.motion.park_speed_scale * dt
            self._target = self._target + np.clip(self.park - self._target, -speed, speed)
        elif command == STATES["RUN"]:
            self.state = "RUN"
            self._target = self._setpoints()
        else:
            self.state = "HOLD" if command == STATES["HOLD"] else "IDLE"
        self.plant.step(self._target, dt)
        self._publish()

    def _physics(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            self._stop.wait(0.002)
            now = time.monotonic()
            self.step(now - last, now)
            last = now

    def start(self) -> ModbusSimDevice:
        for target in (self.server.serve_forever, self._physics):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        for t in self._threads:
            t.join(timeout=2.0)
