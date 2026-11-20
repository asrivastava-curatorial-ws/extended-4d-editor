"""Actuator drivers: how the strokes the pipeline computes reach the actuators' controller.

``ActuatorDriver`` is the interface. Electric, hydraulic and pneumatic actuators are all reached the
same few ways, because the loop that holds an actuator at a position runs in its own controller (a
servo drive, a valve amplifier with a position card, a servo-pneumatic positioner) or, for cylinders
on plain valves, there is no such loop at all:

* ``Esp32Driver``    - the ESP32 board (binary link protocol, ESP32_PROTOCOL.md) over UDP or serial; the
  board puts out a position signal (PWM or an analog 0-10 V DAC) or switches valves, and can read
  position sensors and a supply / drive-ready input.
* ``ModbusDriver``   - a PLC, servo drive or valve controller with Modbus TCP (modbus.py).
* ``UdpDriver``      - a controller that takes positions, a pose or valve states in a UDP format of its own
  (udp_device.py).
* ``SimulationDriver`` - the ESP32 driver connected to a simulated board in-process, with simulated
  actuators of the profile's kind (plants.py); testing only.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .esp32_protocol import (
    Command,
    CommandState,
    DeviceState,
    Fault,
    PacketError,
    SequenceTracker,
    decode_status,
    encode_command,
)
from .plants import plant_for
from .platform_config import PlatformConfig
from .sim_esp32 import GuardConfig, SimulatedEsp32
from .transport import LoopbackTransport, Transport, transport_from_address


class DriverNotImplementedError(RuntimeError):
    pass


@dataclass
class ActuatorCommand:
    state: CommandState
    strokes_m: np.ndarray  # (n,) metres from the retracted end
    master_time: float
    pose: np.ndarray | None = None  # (6,) the pose these strokes make (with the zero offsets): for pose devices


@dataclass
class LinkStatus:
    connected: bool
    last_status_age_ms: float | None
    device_state: str
    faults: list[str]
    positions_m: np.ndarray | None
    rtt_ms: float | None
    counters: dict[str, int] = field(default_factory=dict)
    measured: bool = False          # positions_m come from sensors (else: what was commanded)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_status_age_ms": None if self.last_status_age_ms is None else round(self.last_status_age_ms, 1),
            "device_state": self.device_state,
            "faults": self.faults,
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 2),
            "measured": self.measured,
            **self.counters,
        }


class ActuatorDriver(ABC):
    """Interface for an actuator back-end controlling the actuators of one platform."""

    kind = "abstract"

    @abstractmethod
    def open(self) -> None:
        """Acquire the link/hardware. May raise on configuration errors."""

    @abstractmethod
    def close(self) -> None:
        """Release resources; must be safe to call twice."""

    @abstractmethod
    def send(self, command: ActuatorCommand) -> None:
        """Send one control-period command (state + one stroke target per actuator, in metres)."""

    @abstractmethod
    def poll(self, now: float) -> LinkStatus:
        """Process feedback received so far and report link/device health (``now`` = monotonic s)."""


def park_strokes(cfg: PlatformConfig) -> np.ndarray:
    cal = cfg.calibration
    lengths = cfg.geometry.leg_lengths(cal.park_pose + cal.zero_offsets)
    return np.clip(lengths - cfg.actuators.min_length + cal.actuator_offsets, 0.0, cfg.actuators.stroke)


class ParkRamp:
    """For controllers that only take set-points: the driver itself brings the actuators to park, slowly,
    when the show service says PARK (an ESP32 board does this in its firmware)."""

    def __init__(self, cfg: PlatformConfig) -> None:
        self.park = park_strokes(cfg)
        self.speed = cfg.actuators.max_velocity * cfg.motion.park_speed_scale
        self.pose_park = cfg.calibration.park_pose + cfg.calibration.zero_offsets
        self.output = self.park.copy()
        self.pose = self.pose_park.copy()
        self.state = DeviceState.IDLE
        self._last: float | None = None

    def apply(self, command: ActuatorCommand, now: float) -> np.ndarray:
        dt = 0.0 if self._last is None else max(0.0, min(now - self._last, 0.2))
        self._last = now
        if command.state == CommandState.PARK:
            step = self.speed * dt
            self.output = self.output + np.clip(self.park - self.output, -step, step)
            self.pose = self.pose + (self.pose_park - self.pose) * min(1.0, 2.0 * dt)   # a pose device limits itself
            self.state = DeviceState.PARKED if np.allclose(self.output, self.park, atol=1e-6) else DeviceState.PARKING
        elif command.state == CommandState.RUN:
            self.output = np.asarray(command.strokes_m, dtype=np.float64).copy()
            if command.pose is not None:
                self.pose = np.asarray(command.pose, dtype=np.float64).copy()
            self.state = DeviceState.RUNNING
        elif command.state == CommandState.HOLD:
            self.state = DeviceState.HOLD
        elif command.state == CommandState.IDLE:
            self.state = DeviceState.IDLE
        return self.output


class Esp32Driver(ActuatorDriver):
    kind = "esp32"

    def __init__(
        self,
        transport: Transport,
        link_platform_id: int,
        stroke_m: np.ndarray,
        link_timeout_ms: int,
        clock: Callable[[], float] = time.monotonic,
        control: str = "position",
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.link_platform_id = link_platform_id
        self.stroke_m = np.asarray(stroke_m, dtype=np.float64)
        self.legs = len(self.stroke_m)
        self.control = control
        self.link_timeout_s = link_timeout_ms / 1000.0
        self.sequence = 0
        self.sent = 0
        self.status_rx = 0
        self.status_invalid = 0
        self.link_faults = 0
        self._status_seq = SequenceTracker()
        self._last_status_at: float | None = None
        self._opened_at = clock()
        self._last: Any = None
        self._rtt_ms: float | None = None  # not measured by the link protocol (master-time echo only)
        self._was_connected = False

    def open(self) -> None:
        self.transport.open()
        self._opened_at = self.clock()

    def close(self) -> None:
        self.transport.close()

    def send(self, command: ActuatorCommand) -> None:
        strokes = np.clip(np.asarray(command.strokes_m, dtype=np.float64), 0.0, self.stroke_m)
        targets = [round(s * 1e6) for s in strokes] + [0] * (6 - self.legs)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        packet = encode_command(
            Command(
                platform_id=self.link_platform_id,
                sequence=self.sequence,
                timestamp_us=int(command.master_time * 1e6),
                state=command.state,
                targets_um=tuple(targets),
                flags=1 | ((self.legs & 0x7) << 4),
            )
        )
        self.transport.send(packet)
        self.sent += 1

    def _mismatch(self, st: Any) -> str:
        """What the board says it has that does not fit the profile ("": fits, or the board does not say)."""
        cap = st.capabilities
        if cap.actuators and cap.actuators != self.legs:
            return f"CONFIG: the board drives {cap.actuators} actuators, the profile has {self.legs}"
        if cap.actuators and (cap.output == "valves") != (self.control == "onoff"):
            return ("CONFIG: the board switches valves, the profile asks for positions" if cap.output == "valves"
                    else 'CONFIG: the profile has control = "onoff", the board puts out positions')
        return ""

    def poll(self, now: float) -> LinkStatus:
        for data in self.transport.receive():
            try:
                st = decode_status(data)
            except PacketError:
                self.status_invalid += 1
                continue
            if st.platform_id != self.link_platform_id:
                self.status_invalid += 1
                continue
            self.status_rx += 1
            self._last_status_at = now
            self._last = st
        age = None if self._last_status_at is None else (now - self._last_status_at) * 1000.0
        startup_grace = now - self._opened_at < max(1.0, 4 * self.link_timeout_s)
        connected = age is not None and age <= self.link_timeout_s * 1000.0
        if self._was_connected and not connected:
            self.link_faults += 1
        self._was_connected = connected or (self._was_connected and startup_grace)
        st = self._last
        faults = [str(f.name) for f in Fault if f != Fault.NONE and st is not None and st.faults & f]
        if st is not None:
            mismatch = self._mismatch(st)
            if mismatch:
                faults.append(mismatch)
        counters = {
            "packets_sent": self.sent,
            "status_received": self.status_rx,
            "status_invalid": self.status_invalid,
            "link_faults": self.link_faults,
        }
        if st is not None:
            counters.update(
                device_received=st.received,
                device_lost=st.lost,
                device_duplicate=st.duplicate,
                device_invalid=st.invalid,
                device_stale=st.stale,
                device_timeouts=st.timeouts,
            )
        return LinkStatus(
            connected=connected,
            last_status_age_ms=age,
            device_state=DeviceState(st.device_state).name
            if st is not None
            else ("STARTING" if startup_grace else "NO_REPLY"),
            faults=faults,
            positions_m=None if st is None else np.array(st.positions_um[: self.legs], dtype=np.float64) / 1e6,
            rtt_ms=self._rtt_ms,
            counters=counters,
            measured=bool(st is not None and st.capabilities.measured),
        )

    def last_echo_timestamp(self) -> float | None:
        return None if self._last is None else self._last.echo_timestamp_us / 1e6


ServoElectricDriver = Esp32Driver          # the name before 3.4


class _CallerTime:
    """The simulated board's clock: the show service's own time (the ``now`` of the last poll), so that a
    show stepped faster than real time (tests, replays) moves its simulated actuators just as fast."""

    def __init__(self, fallback: Callable[[], float]) -> None:
        self.fallback = fallback
        self.now: float | None = None

    def __call__(self) -> float:
        return self.fallback() if self.now is None else self.now


class SimulationDriver(Esp32Driver):
    """The ESP32 driver connected to an in-process simulated board and simulated actuators (testing only)."""

    kind = "simulation"

    def __init__(self, cfg: PlatformConfig, clock: Callable[[], float] = time.monotonic) -> None:
        clock = self._time = _CallerTime(clock)
        stroke = cfg.actuators.stroke
        park = park_strokes(cfg)
        guard = GuardConfig(
            platform_id=cfg.link_platform_id,
            stroke_um=tuple(int(s * 1e6) for s in stroke),
            park_um=tuple(round(p * 1e6) for p in park),
            max_speed_um_s=int(cfg.actuators.max_velocity * 1.2e6),
            park_speed_um_s=int(cfg.actuators.max_velocity * 0.2e6),
            timeout_ms=max(cfg.link_timeout_ms, 100),
            axes=cfg.legs,
            output="valves" if cfg.control == "onoff" else "position",
            measured=True,
            supply_input=cfg.actuator != "electric",
        )
        self.device = SimulatedEsp32(guard, plant=plant_for(cfg, park))
        super().__init__(
            LoopbackTransport(self.device, clock), cfg.link_platform_id, stroke, cfg.link_timeout_ms, clock, cfg.control
        )

    def poll(self, now: float) -> LinkStatus:
        self._time.now = now
        return super().poll(now)


def create_driver(cfg: PlatformConfig) -> ActuatorDriver:
    if cfg.driver == "simulation":
        return SimulationDriver(cfg)
    if cfg.driver in ("esp32", "servo_electric"):
        return Esp32Driver(transport_from_address(cfg.address), cfg.link_platform_id, cfg.actuators.stroke,
                           cfg.link_timeout_ms, control=cfg.control)
    if cfg.driver == "modbus_tcp":
        from .modbus import ModbusDriver

        return ModbusDriver(cfg)
    if cfg.driver == "udp":
        from .udp_device import UdpDriver

        return UdpDriver(cfg)
    raise DriverNotImplementedError(f"unknown driver {cfg.driver}")
