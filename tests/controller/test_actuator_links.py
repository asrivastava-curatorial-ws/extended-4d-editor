"""The ways to reach an actuator controller: Modbus TCP (checked against pymodbus, an independent
implementation) and the configurable UDP format - each with its simulated device, over real sockets."""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from controller_helpers import ROOT, write_motion
from fourdvr_motion.content_cache import MotionCache
from fourdvr_motion.drivers import ActuatorCommand, park_strokes
from fourdvr_motion.esp32_protocol import CommandState
from fourdvr_motion.interfaces import parse_udp
from fourdvr_motion.modbus import (
    ModbusDriver,
    ModbusError,
    ModbusSimDevice,
    ModbusTcpClient,
    decode_values,
    encode_values,
)
from fourdvr_motion.platform_config import load_platform_config
from fourdvr_motion.runtime import Phase, PlatformRuntime
from fourdvr_motion.udp_device import UdpDriver, UdpLayout, UdpSimDevice

pymodbus = pytest.importorskip("pymodbus")
from pymodbus.client import ModbusTcpClient as PyClient  # noqa: E402
from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext  # noqa: E402
from pymodbus.server import ServerStop, StartTcpServer  # noqa: E402

PLATFORMS = ROOT / "config" / "platforms"
HYDRAULIC = PLATFORMS / "example-hydraulic-6dof.toml"
TWO = PLATFORMS / "example-electric-2dof.toml"
ONOFF = PLATFORMS / "example-pneumatic-onoff-3dof.toml"


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ------------------------------------------------------------------------------------------- Modbus values
@pytest.mark.parametrize("kind, value, dt", [("u16", 54321, "UINT16"), ("i16", -1234, "INT16"), ("u32", 3_000_000_000, "UINT32"),
                                            ("i32", -70000, "INT32"), ("f32", 0.15625, "FLOAT32")])
@pytest.mark.parametrize("order", ["big", "little"])
def test_register_values_match_pymodbus(kind: str, value: float, dt: str, order: str) -> None:
    ours = encode_values([value], kind, order)
    theirs = PyClient.convert_to_registers(value, getattr(PyClient.DATATYPE, dt), word_order=order)
    assert ours == theirs
    assert decode_values(ours, kind, order) == [pytest.approx(value)]


# ------------------------------------------------------------------------------------------- our client, their server
@pytest.fixture
def pymodbus_server() -> Iterator[int]:
    port = free_port()
    device = ModbusDeviceContext(co=ModbusSequentialDataBlock(1, [False] * 200), hr=ModbusSequentialDataBlock(1, [0] * 200),
                                 ir=ModbusSequentialDataBlock(1, list(range(200))), di=ModbusSequentialDataBlock(1, [False] * 200))
    thread = threading.Thread(target=StartTcpServer, kwargs={"context": ModbusServerContext(devices=device, single=True),
                                                             "address": ("127.0.0.1", port)}, daemon=True)
    thread.start()
    assert wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, 5.0)
    yield port
    ServerStop()
    thread.join(timeout=5.0)


def test_our_client_against_pymodbus_server(pymodbus_server: int) -> None:
    ours = ModbusTcpClient("127.0.0.1", pymodbus_server, unit=1, timeout=1.0)
    theirs = PyClient("127.0.0.1", port=pymodbus_server)
    assert theirs.connect()
    try:
        ours.write_registers(10, [1, 2, 65535])
        ours.write_register(20, 777)
        ours.write_coils(5, [True, False, True, True])
        ours.write_coil(30, True)
        assert theirs.read_holding_registers(10, count=3, device_id=1).registers == [1, 2, 65535]
        assert theirs.read_holding_registers(20, count=1, device_id=1).registers == [777]
        assert theirs.read_coils(5, count=4, device_id=1).bits[:4] == [True, False, True, True]
        assert theirs.read_coils(30, count=1, device_id=1).bits[0] is True
        theirs.write_registers(40, [9, 8, 7], device_id=1)
        assert ours.read_registers(40, 3) == [9, 8, 7]
        assert ours.read_coils(5, 4) == [True, False, True, True]
        assert len(ours.read_registers(0, 4, "input")) == 4
        with pytest.raises(ModbusError, match="exception"):
            ours.read_registers(65530, 100)                    # beyond the device's registers
        assert ours.read_registers(40, 1) == [9]               # the connection survived the exception
    finally:
        ours.close()
        theirs.close()


# ------------------------------------------------------------------------------------------- their client, our device
def test_pymodbus_client_against_our_simulated_device() -> None:
    cfg = load_platform_config(HYDRAULIC)
    dev = ModbusSimDevice(cfg, port=0).start()
    theirs = PyClient("127.0.0.1", port=dev.port)
    assert theirs.connect()
    try:
        assert theirs.write_registers(0, [5000] * 6, device_id=1).isError() is False     # all to mid stroke
        assert not theirs.write_register(10, 1, device_id=1).isError()                     # RUN
        assert wait_for(lambda: all(abs(v - 5000) < 30 for v in theirs.read_input_registers(0, count=6, device_id=1).registers))
        status = theirs.read_input_registers(10, count=1, device_id=1).registers[0]
        assert status & 1 and not status & 2                                               # ready, no fault
        ours = ModbusTcpClient("127.0.0.1", dev.port, unit=1, timeout=1.0)                 # too many at once: refused
        with pytest.raises(ModbusError, match="exception 3"):
            ours.request(struct.pack(">BHH", 3, 0, 126))
        ours.close()
    finally:
        theirs.close()
        dev.stop()


# ------------------------------------------------------------------------------------------- the Modbus driver
def _send(driver, state: CommandState, strokes: np.ndarray, seconds: float, step: float = 0.01) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        driver.send(ActuatorCommand(state, strokes, time.monotonic()))
        time.sleep(step)


def test_modbus_driver_drives_reads_and_supervises() -> None:
    cfg = load_platform_config(HYDRAULIC)
    dev = ModbusSimDevice(cfg, port=0).start()
    cfg = replace(cfg, address=f"tcp://127.0.0.1:{dev.port}")
    drv = ModbusDriver(cfg)
    drv.open()
    try:
        target = np.array([0.05, 0.10, 0.15, 0.20, 0.25, 0.12])
        _send(drv, CommandState.RUN, target, 1.0)
        link = drv.poll(time.monotonic())
        assert link.connected and link.measured and link.faults == [] and link.device_state == "RUNNING"
        assert np.allclose(link.positions_m, target, atol=0.002)                           # u16 0.1 % steps of 0.3 m
        assert dev.state == "RUN" and dev.bank.holding[11] == drv.heartbeat & 0xFFFF
        dev.ready = False                                                                  # the pump stops
        assert wait_for(lambda: any("SUPPLY" in f for f in drv.poll(time.monotonic()).faults))
        dev.ready = True
        _send(drv, CommandState.PARK, target, 2.0)                                         # the driver ramps to park itself
        park = park_strokes(cfg)
        assert dev.bank.holding[10] == 3 and np.all(np.abs(drv.ramp.output - target) > 0.0)
        assert np.allclose(drv.ramp.output, park, atol=0.07)                              # 0.175 m/s for 2 s
        dev.ready = False                                                                  # the pump off at rest: no fault
        time.sleep(0.1)
        assert not any("SUPPLY" in f for f in drv.poll(time.monotonic()).faults)
        dev.ready = True
        dev.silent = True                                                                  # the cable is pulled
        assert wait_for(lambda: not drv.poll(time.monotonic()).connected, 2.0)
        assert drv.poll(time.monotonic()).device_state == "NO_REPLY"
        dev.silent = False
        assert wait_for(lambda: drv.poll(time.monotonic()).connected, 3.0)                # and comes back by itself
    finally:
        drv.close()
        dev.stop()


def test_modbus_device_parks_when_the_heartbeat_stops() -> None:
    cfg = load_platform_config(HYDRAULIC)
    dev = ModbusSimDevice(cfg, port=0, watchdog_ms=200).start()
    drv = ModbusDriver(replace(cfg, address=f"tcp://127.0.0.1:{dev.port}"))
    drv.open()
    try:
        _send(drv, CommandState.RUN, np.full(6, 0.2), 0.8)
        assert dev.state == "RUN" and not dev.fault
        drv.close()                                                                        # the show service is gone
        assert wait_for(lambda: dev.fault and dev.state == "PARK", 2.0)
        assert dev.bank.input[10] & 2                                                      # it says so in its status
    finally:
        drv.close()
        dev.stop()


def test_modbus_valves_are_coils() -> None:
    cfg = load_platform_config(ONOFF)
    from fourdvr_motion.interfaces import parse_modbus

    cfg = replace(cfg, driver="modbus_tcp", modbus=parse_modbus({"valves": 100, "setpoints": 0}))
    dev = ModbusSimDevice(cfg, port=0).start()
    drv = ModbusDriver(replace(cfg, address=f"tcp://127.0.0.1:{dev.port}"))
    drv.open()
    try:
        _send(drv, CommandState.RUN, np.array([0.1, 0.0, 0.1]), 0.5)
        assert dev.bank.coils[100:103] == [True, False, True]
    finally:
        drv.close()
        dev.stop()


# ------------------------------------------------------------------------------------------- UDP
def test_udp_layouts_pack_and_read_back() -> None:
    cfg = load_platform_config(TWO)
    lay = UdpLayout(cfg)
    pkt = lay.pack(7, 1, np.array([0.1, 0.2]), None)
    assert pkt == b"MS" + struct.pack(">HB", 7, 1) + struct.pack(">HH", 10000, 20000)
    assert lay.unpack(pkt) == (1, pytest.approx(np.array([0.1, 0.2])))                  # type: ignore[comparison-overlap]
    assert lay.unpack(pkt[:-1]) == (-1, None) and lay.unpack(b"XX" + pkt[2:])[1] is None
    back = lay.pack_positions(8, 1, np.array([0.05, 0.15]))
    assert np.allclose(lay.unpack_positions(back), [0.05, 0.15])                         # type: ignore[arg-type]
    pose = replace(cfg, udp=parse_udp({"send": "pose", "type": "i32", "pose_scale": [1000, 1000, 1000, 57295.78, 57295.78, 57295.78]},
                                      cfg.dof))
    p = UdpLayout(pose).pack(1, 0, np.zeros(2), np.array([0, 0, 0, 0.01, -0.02, 0]))
    assert struct.unpack("<ii", p) == (573, -1146)                                       # roll, pitch in millidegrees
    valves = load_platform_config(ONOFF)
    v = UdpLayout(replace(valves, udp=parse_udp({"send": "valves", "header": "AA"}, valves.dof)))
    assert v.pack(0, 1, np.array([0.1, 0.0, 0.06]), None) == b"\xaa\x05"                # bits 1 0 1
    assert np.allclose(v.unpack(b"\xaa\x05")[1], [0.1, 0.0, 0.1])                          # type: ignore[arg-type]


def test_udp_driver_with_feedback_against_the_simulated_device() -> None:
    cfg = load_platform_config(TWO)
    dev = UdpSimDevice(cfg, port=0).start()
    drv = UdpDriver(replace(cfg, address=f"udp://127.0.0.1:{dev.port}"))
    drv.open()
    try:
        assert not drv.poll(time.monotonic()).connected
        _send(drv, CommandState.RUN, np.array([0.05, 0.15]), 1.0)
        link = drv.poll(time.monotonic())
        assert link.connected and link.measured and np.allclose(link.positions_m, [0.05, 0.15], atol=0.002)
        assert dev.received > 50 and dev.invalid == 0 and drv.invalid == 0
        dev.silent = True
        time.sleep(0.4)
        assert not drv.poll(time.monotonic()).connected                                   # no answers: a link fault
        dev.silent = False
        _send(drv, CommandState.RUN, np.array([0.05, 0.15]), 0.2)
        assert drv.poll(time.monotonic()).connected
        time.sleep(0.6)                                                                    # the show service stops sending:
        assert dev.state == "PARK"                                                         # the device parks by itself
    finally:
        drv.close()
        dev.stop()


def test_udp_driver_without_feedback_says_it_is_unsupervised() -> None:
    cfg = load_platform_config(TWO)
    cfg = replace(cfg, udp=replace(cfg.udp, feedback=False))                              # type: ignore[type-var]
    dev = UdpSimDevice(cfg, port=0).start()
    drv = UdpDriver(replace(cfg, address=f"udp://127.0.0.1:{dev.port}"))
    drv.open()
    try:
        _send(drv, CommandState.RUN, np.array([0.12, 0.08]), 0.6)
        link = drv.poll(time.monotonic())
        assert link.connected and not link.measured and link.device_state.startswith("UNSUPERVISED")
        assert np.allclose(link.positions_m, [0.12, 0.08])                               # what was commanded
        assert np.allclose(dev.plant.pos, [0.12, 0.08], atol=0.003)                       # and the device did it
    finally:
        drv.close()
        dev.stop()


# ------------------------------------------------------------------------------------------- the runtime, real time
def _runtime(cfg, tmp_path: Path, driver) -> PlatformRuntime:  # type: ignore[no-untyped-def]
    rt = PlatformRuntime(cfg, 100.0, MotionCache(tmp_path / "cache"), driver=driver)
    now = time.monotonic()
    for _ in range(3):
        rt.clock.add(now, now + 5000.0, now + 5000.0, now)
    driver.open()
    return rt


def _tick(rt: PlatformRuntime, seconds: float) -> None:
    end = time.monotonic() + seconds
    nxt = time.monotonic()
    while time.monotonic() < end:
        rt.tick(time.monotonic())
        nxt += 0.01
        time.sleep(max(0.0, nxt - time.monotonic()))


@pytest.mark.parametrize("kind", ["modbus", "udp"])
def test_a_programme_through_a_network_device(kind: str, tmp_path: Path) -> None:
    cfg = load_platform_config(HYDRAULIC if kind == "modbus" else TWO)
    dev = (ModbusSimDevice(cfg, port=0) if kind == "modbus" else UdpSimDevice(cfg, port=0)).start()
    scheme = "tcp" if kind == "modbus" else "udp"
    cfg = replace(cfg, address=f"{scheme}://127.0.0.1:{dev.port}")
    driver = ModbusDriver(cfg) if kind == "modbus" else UdpDriver(cfg)
    rt = _runtime(cfg, tmp_path, driver)
    try:
        _tick(rt, 0.4)
        assert rt.phase == Phase.IDLE and rt.pipeline.initialised
        sha = write_motion(rt.cache.path("x"), programme_id=1, duration=6.0)
        rt.cache.path("x").rename(rt.cache.path(sha))
        rt.register_programme(1, sha, 6.0)
        rt.cmd_play(1, 1, rt.clock.master_time(time.monotonic()) + 0.2, 6.0, True)
        _tick(rt, 3.0)
        st = rt.status()
        assert rt.phase == Phase.PLAYING and st["fault"] is None, st["fault"]
        assert st["link"]["measured"] and max(rt.follow_monitor.worst) < cfg.supervision.following_error
        dev.silent = True                                                                   # the device stops answering
        _tick(rt, 0.8)
        assert rt.phase == Phase.FAULT and "communication with actuator controller lost" in rt.fault_reason
    finally:
        driver.close()
        dev.stop()
