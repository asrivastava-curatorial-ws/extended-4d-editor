from __future__ import annotations

import struct
from pathlib import Path

import pytest

from fourdvr_motion.cli import main as cli_main
from fourdvr_motion.esp32_protocol import (
    CMD_SIZE,
    STATUS_SIZE,
    Command,
    CommandState,
    DeviceState,
    Fault,
    PacketError,
    SequenceTracker,
    Status,
    cobs_decode,
    cobs_encode,
    crc32,
    decode_command,
    decode_status,
    encode_command,
    encode_status,
)
from fourdvr_motion.sim_esp32 import GuardConfig, LinkGuard

VECTORS = Path(__file__).resolve().parents[2] / "firmware" / "esp32-actuator-bridge" / "test" / "vectors.txt"


def _cmd(seq: int, ts_us: int, state: CommandState = CommandState.RUN, targets=(1000,) * 6, pid: int = 1) -> bytes:
    return encode_command(Command(pid, seq, ts_us, state, tuple(targets)))


def test_command_and_status_roundtrip() -> None:
    cmd = Command(3, 42, 1_798_000_000_000_001, CommandState.HOLD, (1, -2, 3, 300_000, 0, 7))
    data = encode_command(cmd)
    assert len(data) == CMD_SIZE == 52 and data[:4] == b"4DMC"
    assert decode_command(data) == cmd
    st = Status(3, 42, 99, 1234, DeviceState.PARKING, Fault.TIMEOUT, (1, 2, 3, 4, 5, 6), 10, 1, 2, 3, 4, 5)
    raw = encode_status(st)
    assert len(raw) == STATUS_SIZE == 72
    assert decode_status(raw) == st


def test_invalid_packets_detected() -> None:
    data = bytearray(_cmd(1, 1000))
    data[30] ^= 0x01
    with pytest.raises(PacketError) as e:
        decode_command(bytes(data))
    assert e.value.kind == "crc"
    with pytest.raises(PacketError, match="bytes"):
        decode_command(bytes(data[:-1]))
    bad = bytearray(_cmd(1, 1000))
    bad[4] = 2  # future major version
    bad[-4:] = struct.pack("<I", crc32(bytes(bad[:-4])))
    with pytest.raises(PacketError) as e2:
        decode_command(bytes(bad))
    assert e2.value.kind == "version"
    with pytest.raises(PacketError):
        decode_status(_cmd(1, 1000))


def test_cobs_roundtrip() -> None:
    for payload in (b"", b"\x00", b"\x00\x00", bytes(range(256)) * 2, _cmd(5, 5)):
        enc = cobs_encode(payload)
        assert 0 not in enc
        assert cobs_decode(enc) == payload


def test_sequence_tracker_loss_duplicates_wrap() -> None:
    t = SequenceTracker()
    assert t.accept(10) and t.accept(11) and not t.accept(11) and not t.accept(9)
    assert t.accept(15) and t.lost == 3 and t.duplicate == 2
    t2 = SequenceTracker()
    assert t2.accept(0xFFFFFFFE) and t2.accept(0xFFFFFFFF) and t2.accept(0) and t2.lost == 0


def test_link_guard_accepts_and_clamps() -> None:
    g = LinkGuard(GuardConfig(platform_id=1, stroke_um=(300_000,) * 6))
    assert g.handle(_cmd(1, 1_000_000, targets=(-5, 10, 400_000, 0, 0, 0)), now_ms=0)
    assert g.state == DeviceState.RUNNING
    assert list(g.target) == [0, 10, 300_000, 0, 0, 0]
    g.update(10, 0.01)
    assert g.status(10).device_state == DeviceState.RUNNING


def test_link_guard_rejects_wrong_platform_dup_and_stale() -> None:
    g = LinkGuard(GuardConfig(platform_id=1))
    assert not g.handle(_cmd(1, 1_000_000, pid=2), 0) and g.invalid == 1
    assert g.handle(_cmd(1, 1_000_000), 0)
    assert not g.handle(_cmd(1, 1_000_000), 1) and g.seq.duplicate == 1
    assert g.handle(_cmd(5, 1_010_000), 10) and g.seq.lost == 3
    # packet whose timestamp lags the learned time base by 300 ms is stale
    assert not g.handle(_cmd(6, 1_020_000), 320) and g.stale == 1


def test_link_guard_timeout_parks_and_latches() -> None:
    cfg = GuardConfig(platform_id=1, park_um=(150_000,) * 6, timeout_ms=100, park_speed_um_s=1_000_000)
    g = LinkGuard(cfg)
    g.handle(_cmd(1, 0, targets=(250_000,) * 6), 0)
    for k in range(1, 30):
        g.update(k * 10, 0.01)
    assert g.faults & Fault.TIMEOUT and g.timeouts == 1
    assert g.state == DeviceState.PARKED and abs(g.output[0] - 150_000) < 1
    # RUN is ignored while a fault is latched; FAULT_RESET clears it
    assert g.handle(_cmd(2, 300_000, targets=(10_000,) * 6), 300)
    assert g.state == DeviceState.PARKED
    g.handle(_cmd(3, 310_000, state=CommandState.FAULT_RESET), 310)
    assert g.faults == Fault.NONE and g.state == DeviceState.IDLE


def test_invalid_burst_latches_fault() -> None:
    g = LinkGuard(GuardConfig(platform_id=1, invalid_burst=5))
    for k in range(5):
        g.handle(b"garbage", k)
    assert g.faults & Fault.INVALID_BURST


def test_shared_test_vectors_match(tmp_path: Path) -> None:
    out = tmp_path / "vectors.txt"
    assert cli_main(["test-vectors", "-o", str(out)]) == 0
    assert out.read_text() == VECTORS.read_text(), "regenerate firmware/.../test/vectors.txt"
    lines = [ln.split() for ln in VECTORS.read_text().splitlines() if ln and not ln.startswith("#")]
    for kind, *hexes in lines:
        if kind == "command":
            decode_command(bytes.fromhex(hexes[0]))
        elif kind == "status":
            st = decode_status(bytes.fromhex(hexes[0]))
            assert st.faults == Fault.TIMEOUT | Fault.STALE
        elif kind == "cobs":
            assert cobs_encode(bytes.fromhex(hexes[0])).hex() == hexes[1]
