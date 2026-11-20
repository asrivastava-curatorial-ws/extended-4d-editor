"""Simulated ESP32 actuator bridge (for testing without hardware).

``LinkGuard`` is a line-by-line Python port of the firmware's portable core
(firmware/esp32-actuator-bridge/lib/fourdvr_link/src/fourdvr_link.cpp): packet validation,
duplicate/loss/stale detection, the check of the number of actuators, communication watchdog,
latched faults and the speed-limited ramp to the park position. ``SimulatedEsp32`` adds simulated
actuators (plants.py: electric, hydraulic, pneumatic or on plain valves) and answers with STATUS
packets, exactly like the real firmware.

It can run in-process (simulation driver) or as a UDP server (``fourdvr controller esp32-sim``) so
the real UDP driver path can be exercised on a bench PC.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

import numpy as np

from .esp32_protocol import (
    STATUS_SIZE,
    Capabilities,
    Command,
    CommandState,
    DeviceState,
    Fault,
    PacketError,
    SequenceTracker,
    Status,
    decode_command,
    encode_status,
)
from .plants import Plant


@dataclass
class GuardConfig:
    platform_id: int = 1
    stroke_um: tuple[int, ...] = (300_000,) * 6
    park_um: tuple[int, ...] = (150_000,) * 6
    max_speed_um_s: int = 500_000
    park_speed_um_s: int = 60_000
    timeout_ms: int = 200
    stale_ms: int = 100
    invalid_burst: int = 20  # invalid packets within one second that latch a fault
    axes: int = 6            # actuators this board drives (FDVR_AXES)
    output: str = "position"  # position | dac | valves (FDVR_OUTPUT_*)
    measured: bool = False    # position sensors fitted (FDVR_FEEDBACK)
    supply_input: bool = False  # supply / drive-ready input fitted (FDVR_READY_PIN)

    def capabilities(self) -> Capabilities:
        return Capabilities(self.axes, self.measured, self.output, self.supply_input)


@dataclass
class LinkGuard:
    cfg: GuardConfig
    state: DeviceState = DeviceState.IDLE
    faults: Fault = Fault.NONE
    received: int = 0
    invalid: int = 0
    stale: int = 0
    timeouts: int = 0
    last_rx_ms: int | None = None
    echo_ts_us: int = 0
    seq: SequenceTracker = field(default_factory=SequenceTracker)
    _offset_us: int | None = None
    _stale_run: int = 0
    _window_start_ms: int = 0
    _invalid_in_window: int = 0

    def __post_init__(self) -> None:
        n = self.cfg.axes
        self.stroke = np.array(self.cfg.stroke_um[:n], dtype=np.float64)
        self.park = np.array(self.cfg.park_um[:n], dtype=np.float64)
        self.output = self.park.copy()
        self.target = self.output.copy()
        self.estop = False

    def handle(self, data: bytes, now_ms: int) -> bool:
        try:
            cmd = decode_command(data)
        except PacketError:
            self._invalid(now_ms)
            return False
        if cmd.platform_id != self.cfg.platform_id:
            self._invalid(now_ms)
            return False
        now_us = now_ms * 1000
        offset = cmd.timestamp_us - now_us
        if self._offset_us is None or offset > self._offset_us:
            self._offset_us = offset
        age_ms = (now_us + self._offset_us - cmd.timestamp_us) / 1000.0
        if age_ms > self.cfg.stale_ms:
            self.stale += 1
            self._stale_run += 1
            if self._stale_run >= 5:  # controller time base changed: relearn
                self._offset_us = None
                self._stale_run = 0
            return False
        self._stale_run = 0
        if not self.seq.accept(cmd.sequence):
            return False
        self.received += 1
        self.last_rx_ms = now_ms
        self.echo_ts_us = cmd.timestamp_us
        if (cmd.actuators or 6) != self.cfg.axes:      # the controller drives another number of actuators
            self._latch(Fault.CONFIG)
        self._apply(cmd)
        return True

    def _invalid(self, now_ms: int) -> None:
        self.invalid += 1
        if now_ms - self._window_start_ms >= 1000:
            self._window_start_ms = now_ms
            self._invalid_in_window = 0
        self._invalid_in_window += 1
        if self._invalid_in_window >= self.cfg.invalid_burst:
            self._latch(Fault.INVALID_BURST)

    def _latch(self, fault: Fault) -> None:
        self.faults |= fault
        if self.state not in (DeviceState.PARKED, DeviceState.FAULT):
            self.state = DeviceState.PARKING

    def input_fault(self, fault: Fault) -> None:
        """A fault found by the board's own inputs (sensor out of range, supply lost): also OUTPUT for old controllers."""
        if not self.faults & fault:
            self._latch(fault | Fault.OUTPUT)

    def _apply(self, cmd: Command) -> None:
        if cmd.state == CommandState.FAULT_RESET:
            if self.faults and not self.estop:
                self.faults = Fault.NONE
                self.state = DeviceState.IDLE
                self.target = self.output.copy()
            return
        if self.faults:
            return  # latched: only FAULT_RESET is accepted
        if cmd.state == CommandState.RUN and cmd.flags & 1:
            n = self.cfg.axes
            self.target = np.clip(np.array(cmd.targets_um[:n], dtype=np.float64), 0, self.stroke)
            self.state = DeviceState.RUNNING
        elif cmd.state == CommandState.HOLD:
            self.target = self.output.copy()
            self.state = DeviceState.HOLD
        elif cmd.state == CommandState.PARK:
            if self.state != DeviceState.PARKED:
                self.state = DeviceState.PARKING
        elif cmd.state == CommandState.IDLE:
            self.target = self.output.copy()
            self.state = DeviceState.IDLE

    def update(self, now_ms: int, dt_s: float) -> None:
        if self.estop:
            self.faults |= Fault.ESTOP_INPUT
            self.state = DeviceState.FAULT
            self.target = self.output.copy()
        elif (
            self.state in (DeviceState.RUNNING, DeviceState.HOLD, DeviceState.IDLE)
            and self.last_rx_ms is not None
            and now_ms - self.last_rx_ms > self.cfg.timeout_ms
        ):
            self.timeouts += 1
            self._latch(Fault.TIMEOUT)
        speed = self.cfg.max_speed_um_s
        if self.state in (DeviceState.PARKING, DeviceState.PARKED):
            self.target = self.park.copy()
            speed = self.cfg.park_speed_um_s
        if self.state == DeviceState.FAULT:
            return
        if self.cfg.output == "valves" and self.state != DeviceState.PARKING:
            self.output = self.target.copy()      # a valve switches at once (the controller keeps the dwell times)
        else:
            step = speed * max(dt_s, 0.0)
            self.output = self.output + np.clip(self.target - self.output, -step, step)
        if self.state == DeviceState.PARKING and np.all(np.abs(self.output - self.target) < 1.0):
            self.state = DeviceState.PARKED

    def valve_states(self) -> np.ndarray:
        """Output "valves": a valve is on when its output is at least half the stroke."""
        return self.output >= self.stroke / 2.0

    def status(self, now_ms: int, positions_um: np.ndarray | None = None) -> Status:
        pos = self.output if positions_um is None else positions_um
        padded = list(pos) + [0.0] * (6 - len(pos))
        return Status(
            platform_id=self.cfg.platform_id,
            last_sequence=self.seq.last or 0,
            echo_timestamp_us=self.echo_ts_us,
            uptime_ms=now_ms,
            device_state=self.state,
            faults=self.faults,
            positions_um=tuple(round(float(p)) for p in padded),
            received=self.received,
            lost=self.seq.lost,
            duplicate=self.seq.duplicate,
            invalid=self.invalid,
            stale=self.stale,
            timeouts=self.timeouts,
            capabilities=self.cfg.capabilities(),
        )


class SimulatedEsp32:
    """Firmware core + simulated actuators. Time is supplied by the caller (milliseconds)."""

    def __init__(self, cfg: GuardConfig, actuator_time_constant_s: float = 0.02, plant: Plant | None = None) -> None:
        self.guard = LinkGuard(cfg)
        self.plant = plant or Plant("first_order", self.guard.stroke / 1e6, self.guard.output / 1e6,
                                    max(cfg.max_speed_um_s, 1) / 1e6, tau=actuator_time_constant_s)
        self._last_ms: int | None = None
        self.outbox: list[bytes] = []
        self.drop_next = 0  # test hook: drop N incoming packets (simulated packet loss)
        self.silent = False  # test hook: stop answering (simulated cable pull)
        self.supply_ok = True  # test hook: the supply / drive-ready input (with supply_input)
        self.sensor_ok = True  # test hook: the position sensors read inside their range (with measured)

    @property
    def actual(self) -> np.ndarray:
        """Where the simulated actuators are (micrometres)."""
        return self.plant.pos * 1e6

    def _advance(self, now_ms: int) -> None:
        if self._last_ms is None:
            self._last_ms = now_ms
        dt = max(0, now_ms - self._last_ms) / 1000.0
        self._last_ms = now_ms
        if dt <= 0:
            return
        g = self.guard
        if g.cfg.supply_input and not self.supply_ok and g.state in (DeviceState.RUNNING, DeviceState.HOLD):
            g.input_fault(Fault.SUPPLY)
        if g.cfg.measured and not self.sensor_ok:
            g.input_fault(Fault.FEEDBACK)
        g.update(now_ms, dt)
        self.plant.step(g.output / 1e6, dt)

    def on_packet(self, data: bytes, now_ms: int) -> None:
        self._advance(now_ms)
        if self.silent:
            return
        if self.drop_next > 0:
            self.drop_next -= 1
            return
        self.guard.handle(data, now_ms)
        self.outbox.append(encode_status(self.guard.status(now_ms, self.actual if self.guard.cfg.measured else None)))

    def tick(self, now_ms: int) -> None:
        self._advance(now_ms)

    def take_outbox(self) -> list[bytes]:
        out, self.outbox = self.outbox, []
        return out


def run_udp_server(port: int, cfg: GuardConfig, verbose: bool = False, plant: Plant | None = None,
                   bind: str = "0.0.0.0", seconds: float = 0.0) -> None:  # pragma: no cover - CLI tool
    """Bench tool: behave like an ESP32 on UDP <port> (replies to the sender of each command)."""
    sim = SimulatedEsp32(cfg, plant=plant)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, port))
    sock.settimeout(0.01)
    start = time.monotonic()
    last_print = 0.0
    print(f"simulated ESP32 platform {cfg.platform_id} listening on UDP {port}: {cfg.axes} actuators, "
          f"output {cfg.output}" + (", measured positions" if cfg.measured else ""), flush=True)
    while not seconds or time.monotonic() - start < seconds:
        now_ms = int((time.monotonic() - start) * 1000)
        try:
            data, addr = sock.recvfrom(256)
        except TimeoutError:
            sim.tick(now_ms)
            continue
        sim.on_packet(data, now_ms)
        for pkt in sim.take_outbox():
            assert len(pkt) == STATUS_SIZE
            sock.sendto(pkt, addr)
        if verbose and time.monotonic() - last_print > 1.0:
            last_print = time.monotonic()
            g = sim.guard
            print(
                f"state={g.state.name} faults={g.faults!r} rx={g.received} lost={g.seq.lost} "
                f"dup={g.seq.duplicate} invalid={g.invalid} stale={g.stale} pos_um={np.round(sim.actual).astype(int)}",
                flush=True,
            )
