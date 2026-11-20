"""A controller that takes its set-points as UDP datagrams in a format of its own ([udp] in the profile).

One datagram per control period: an optional fixed header, an optional packet counter, an optional
byte with the command state (0 idle, 1 run, 2 hold, 3 park, 4 reset), then the values - the
actuators' positions, the platform's pose, or the valve states as bits. With ``feedback = true`` the
device answers with its measured positions in the same layout, and a device that stops answering
is a fault; without, the show service cannot tell whether the device is there, and the device must
park by itself when the datagrams stop (docs/ACTUATORS.md).

``UdpSimDevice`` is such a device, simulated, for bench tests (``fourdvr controller udp-sim``).
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable

import numpy as np

from .drivers import ActuatorCommand, ActuatorDriver, LinkStatus, ParkRamp, park_strokes
from .esp32_protocol import CommandState
from .interfaces import STATES, TYPES, UdpSettings, scale_from, scale_to
from .kinematics import AXES
from .plants import plant_for
from .platform_config import PlatformConfig
from .transport import UdpTransport


class UdpLayout:
    """Packing and unpacking of one profile's datagrams."""

    def __init__(self, cfg: PlatformConfig) -> None:
        assert cfg.udp is not None
        self.cfg = cfg
        self.u: UdpSettings = cfg.udp
        self.legs = cfg.legs
        self.stroke = cfg.actuators.stroke
        self.axes = [AXES.index(a) for a in self.u.pose_axes]
        self.counter_struct = struct.Struct(self.u.code() + TYPES[self.u.counter][0]) if self.u.counter else None
        self.counter_mask = (1 << (8 * self.counter_struct.size)) - 1 if self.counter_struct else 0
        if self.u.send == "valves":
            self.values_size = (self.legs + 7) // 8
        else:
            count = len(self.axes) if self.u.send == "pose" else self.legs
            self.values = self.u.value_struct(count)
            self.values_size = self.values.size
        self.feedback_values = self.u.value_struct(self.legs)

    def _range(self) -> tuple[float, float] | None:
        return self.u.range

    def _number(self, fraction: float, i: int) -> float | int:
        rng = self.u.range or (0.0, float(self.stroke[i]))
        return scale_to(fraction, rng, self.u.type)

    def prefix(self, counter: int, state: int) -> bytes:
        out = self.u.header
        if self.counter_struct is not None:
            out += self.counter_struct.pack(counter & self.counter_mask)
        if self.u.state:
            out += bytes([state])
        return out

    def pack(self, counter: int, state: int, strokes: np.ndarray, pose: np.ndarray | None) -> bytes:
        head = self.prefix(counter, state)
        if self.u.send == "valves":
            bits = bytearray(self.values_size)
            for i, s in enumerate(strokes):
                if s >= self.stroke[i] / 2.0:
                    bits[i // 8] |= 1 << (i % 8)
            return head + bytes(bits)
        if self.u.send == "pose":
            p = np.zeros(6) if pose is None else pose
            nums = [p[k] * self.u.pose_scale[k] for k in self.axes]
            if TYPES[self.u.type][2] is not None:
                lo, hi = TYPES[self.u.type][2], TYPES[self.u.type][3]
                nums = [int(min(max(round(v), lo), hi)) for v in nums]        # type: ignore[type-var]
            return head + self.values.pack(*nums)
        frac = np.clip(np.asarray(strokes) / self.stroke, 0.0, 1.0)
        return head + self.values.pack(*[self._number(float(f), i) for i, f in enumerate(frac)])

    def unpack_positions(self, data: bytes) -> np.ndarray | None:
        """A feedback datagram (header, counter, state as configured, then one value per actuator), or None."""
        start = len(self.prefix(0, 0))
        if len(data) != start + self.feedback_values.size or not data.startswith(self.u.header):
            return None
        raw = self.feedback_values.unpack_from(data, start)
        frac = [scale_from(v, self.u.range or (0.0, float(self.stroke[i]))) for i, v in enumerate(raw)]
        return np.array(frac) * self.stroke

    def pack_positions(self, counter: int, state: int, strokes: np.ndarray) -> bytes:
        frac = np.clip(np.asarray(strokes) / self.stroke, 0.0, 1.0)
        return self.prefix(counter, state) + self.feedback_values.pack(*[self._number(float(f), i) for i, f in enumerate(frac)])

    def unpack(self, data: bytes) -> tuple[int, np.ndarray | None]:
        """A set-point datagram: (state, strokes in metres) or (state, None) if it does not fit."""
        start = len(self.prefix(0, 0))
        if len(data) != start + self.values_size or not data.startswith(self.u.header):
            return -1, None
        state = data[start - 1] if self.u.state else STATES["RUN"]
        body = data[start:]
        if self.u.send == "valves":
            return state, np.array([self.stroke[i] if body[i // 8] >> (i % 8) & 1 else 0.0 for i in range(self.legs)])
        nums = self.values.unpack(body)
        if self.u.send == "pose":
            pose = np.zeros(6)
            for k, v in zip(self.axes, nums, strict=True):
                pose[k] = v / self.u.pose_scale[k]
            cal = self.cfg.calibration
            lengths = self.cfg.geometry.leg_lengths(pose)
            return state, np.clip(lengths - self.cfg.actuators.min_length + cal.actuator_offsets, 0.0, self.stroke)
        frac = [scale_from(v, self.u.range or (0.0, float(self.stroke[i]))) for i, v in enumerate(nums)]
        return state, np.clip(np.array(frac), 0.0, 1.0) * self.stroke


class UdpDriver(ActuatorDriver):
    kind = "udp"

    def __init__(self, cfg: PlatformConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.layout = UdpLayout(cfg)
        host, _, port = cfg.address.removeprefix("udp://").partition(":")
        self.transport = UdpTransport(host, int(port))
        self.clock = clock
        self.ramp = ParkRamp(cfg)
        self.timeout_s = cfg.link_timeout_ms / 1000.0
        self.counter = 0
        self.sent = 0
        self.received = 0
        self.invalid = 0
        self.link_faults = 0
        self._last_rx: float | None = None
        self._positions: np.ndarray | None = None
        self._opened_at = clock()
        self._was_connected = False

    def open(self) -> None:
        self.transport.open()
        self._opened_at = self.clock()

    def close(self) -> None:
        self.transport.close()

    def send(self, command: ActuatorCommand) -> None:
        out = self.ramp.apply(command, self.clock())
        pose = self.ramp.pose if command.state != CommandState.RUN or command.pose is None else command.pose
        self.counter += 1
        self.transport.send(self.layout.pack(self.counter, STATES.get(command.state.name, 0), out, pose))
        self.sent += 1

    def poll(self, now: float) -> LinkStatus:
        for data in self.transport.receive():
            pos = self.layout.unpack_positions(data)
            if pos is None:
                self.invalid += 1
                continue
            self.received += 1
            self._positions = pos
            self._last_rx = now
        counters = {"packets_sent": self.sent, "feedback_received": self.received, "feedback_invalid": self.invalid,
                    "link_faults": self.link_faults}
        if not self.cfg.udp or not self.cfg.udp.feedback:          # nothing comes back: nothing to supervise here
            return LinkStatus(True, None, "UNSUPERVISED (no feedback)", [], self.ramp.output.copy(), None, counters)
        age = None if self._last_rx is None else (now - self._last_rx) * 1000.0
        connected = age is not None and age <= self.timeout_s * 1000.0
        if self._was_connected and not connected:
            self.link_faults += 1
        self._was_connected = connected
        startup = now - self._opened_at < max(1.0, 4 * self.timeout_s)
        state = self.ramp.state.name if connected else ("STARTING" if startup and self._last_rx is None else "NO_REPLY")
        return LinkStatus(connected, age, state, [], self._positions, None, counters, measured=self._positions is not None)


class UdpSimDevice:
    """A device that takes the profile's [udp] datagrams, with simulated actuators of the profile's kind.

    It parks by itself when no datagram has come for ``watchdog_ms`` (as a real device must), and
    answers each datagram with its measured positions if the profile has ``feedback = true``.
    """

    def __init__(self, cfg: PlatformConfig, port: int = 7408, host: str = "127.0.0.1", watchdog_ms: float = 300.0) -> None:
        self.cfg = cfg
        self.layout = UdpLayout(cfg)
        self.park = park_strokes(cfg)
        self.plant = plant_for(cfg, self.park)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.002)
        self.port = int(self.sock.getsockname()[1])
        self.watchdog_s = watchdog_ms / 1000.0
        self.silent = False            # test hook: no answers
        self.received = 0
        self.invalid = 0
        self.state = "IDLE"
        self._target = self.park.copy()
        self._last_rx: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.counter = 0

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except TimeoutError:
                data, addr = b"", None
            except OSError:
                break
            now = time.monotonic()
            if data:
                state, strokes = self.layout.unpack(data)
                if strokes is None:
                    self.invalid += 1
                else:
                    self.received += 1
                    self._last_rx = now
                    if state == STATES["PARK"]:
                        self.state = "PARK"
                    elif state in (STATES["RUN"], STATES["HOLD"]):
                        if state == STATES["RUN"]:
                            self._target = strokes
                        self.state = "RUN" if state == STATES["RUN"] else "HOLD"
                    if self.cfg.udp and self.cfg.udp.feedback and not self.silent and addr is not None:
                        self.counter += 1
                        self.sock.sendto(self.layout.pack_positions(self.counter, state, self.plant.pos), addr)
            if self._last_rx is not None and now - self._last_rx > self.watchdog_s:
                self.state = "PARK"                      # nobody talks to us any more: park
            dt = now - last
            last = now
            if self.state == "PARK":
                step = self.cfg.actuators.max_velocity * self.cfg.motion.park_speed_scale * dt
                self._target = self._target + np.clip(self.park - self._target, -step, step)
            self.plant.step(self._target, dt)

    def start(self) -> UdpSimDevice:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.sock.close()
