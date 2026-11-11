"""Actuator drivers.

``ActuatorDriver`` is the interface every actuator technology implements. Release 1.0.1 provides:

* ``ServoElectricDriver`` - electric servo actuators behind an ESP32/Arduino bridge, speaking the
  binary link protocol (ESP32_PROTOCOL.md) over UDP or serial.
* ``SimulationDriver``    - the same driver, protocol encoding and link supervision, connected to an
  in-process simulated ESP32 (testing only). Motion processing and kinematics are identical.

Hydraulic and pneumatic drivers are NOT implemented; configuring them is rejected with an error.
A future implementation subclasses ActuatorDriver (see STEWART_KINEMATICS.md, "Driver interface").
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
from .platform_config import FUTURE_DRIVER_TYPES, PlatformConfig
from .sim_esp32 import GuardConfig, SimulatedEsp32
from .transport import LoopbackTransport, Transport, transport_from_address


class DriverNotImplementedError(RuntimeError):
    pass


@dataclass
class ActuatorCommand:
    state: CommandState
    strokes_m: np.ndarray  # (6,) metres from the retracted end
    master_time: float


@dataclass
class LinkStatus:
    connected: bool
    last_status_age_ms: float | None
    device_state: str
    faults: list[str]
    positions_m: np.ndarray | None
    rtt_ms: float | None
    counters: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_status_age_ms": None if self.last_status_age_ms is None else round(self.last_status_age_ms, 1),
            "device_state": self.device_state,
            "faults": self.faults,
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 2),
            **self.counters,
        }


class ActuatorDriver(ABC):
    """Interface for an actuator back-end controlling the six legs of one platform."""

    kind = "abstract"

    @abstractmethod
    def open(self) -> None:
        """Acquire the link/hardware. May raise on configuration errors."""

    @abstractmethod
    def close(self) -> None:
        """Release resources; must be safe to call twice."""

    @abstractmethod
    def send(self, command: ActuatorCommand) -> None:
        """Send one control-period command (state + six stroke targets in metres)."""

    @abstractmethod
    def poll(self, now: float) -> LinkStatus:
        """Process feedback received so far and report link/device health (``now`` = monotonic s)."""


class ServoElectricDriver(ActuatorDriver):
    kind = "servo_electric"

    def __init__(
        self,
        transport: Transport,
        link_platform_id: int,
        stroke_m: np.ndarray,
        link_timeout_ms: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.link_platform_id = link_platform_id
        self.stroke_m = np.asarray(stroke_m, dtype=np.float64)
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
        self._rtt_ms: float | None = None  # not measured by the 1.0 link protocol (master-time echo only)
        self._was_connected = False

    def open(self) -> None:
        self.transport.open()
        self._opened_at = self.clock()

    def close(self) -> None:
        self.transport.close()

    def send(self, command: ActuatorCommand) -> None:
        strokes = np.clip(np.asarray(command.strokes_m, dtype=np.float64), 0.0, self.stroke_m)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        packet = encode_command(
            Command(
                platform_id=self.link_platform_id,
                sequence=self.sequence,
                timestamp_us=int(command.master_time * 1e6),
                state=command.state,
                targets_um=tuple(round(s * 1e6) for s in strokes),
            )
        )
        self.transport.send(packet)
        self.sent += 1

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
            positions_m=None if st is None else np.array(st.positions_um, dtype=np.float64) / 1e6,
            rtt_ms=self._rtt_ms,
            counters=counters,
        )

    def last_echo_timestamp(self) -> float | None:
        return None if self._last is None else self._last.echo_timestamp_us / 1e6


class SimulationDriver(ServoElectricDriver):
    """Servo/electric driver connected to an in-process simulated ESP32 (testing only)."""

    kind = "simulation"

    def __init__(self, cfg: PlatformConfig, clock: Callable[[], float] = time.monotonic) -> None:
        stroke = cfg.actuators.stroke
        park_lengths = cfg.geometry.leg_lengths(cfg.calibration.park_pose + cfg.calibration.zero_offsets)
        park_strokes = park_lengths - cfg.actuators.min_length + cfg.calibration.actuator_offsets
        self.device = SimulatedEsp32(
            GuardConfig(
                platform_id=cfg.link_platform_id,
                stroke_um=tuple(int(s * 1e6) for s in stroke),
                park_um=tuple(round(p * 1e6) for p in park_strokes),
                max_speed_um_s=int(cfg.actuators.max_velocity * 1.2e6),
                park_speed_um_s=int(cfg.actuators.max_velocity * 0.2e6),
                timeout_ms=max(cfg.link_timeout_ms, 100),
            )
        )
        super().__init__(
            LoopbackTransport(self.device, clock), cfg.link_platform_id, stroke, cfg.link_timeout_ms, clock
        )


def create_driver(cfg: PlatformConfig) -> ActuatorDriver:
    if cfg.driver == "simulation":
        return SimulationDriver(cfg)
    if cfg.driver == "servo_electric":
        return ServoElectricDriver(
            transport_from_address(cfg.address), cfg.link_platform_id, cfg.actuators.stroke, cfg.link_timeout_ms
        )
    if cfg.driver in FUTURE_DRIVER_TYPES:
        raise DriverNotImplementedError(f"{cfg.driver} actuator driver is not implemented in this release")
    raise DriverNotImplementedError(f"unknown driver {cfg.driver}")
