"""DMX packet codecs and outputs (sACN E1.31, Art-Net, Enttec USB Pro) over the loopback interface."""

from __future__ import annotations

import struct
import time
import uuid

import pytest

from fourdvr_fx import dmx


def test_sacn_data_packet_layout() -> None:
    cid = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff").bytes
    data = bytes(range(256)) * 2
    pkt = dmx.build_sacn_data(cid, "4D VR Theatre", 300, data, sequence=7, priority=120)
    assert len(pkt) == 638                                                   # full 512-slot E1.31 packet
    assert pkt[:4] == b"\x00\x10\x00\x00" and pkt[4:16] == b"ASC-E1.17\x00\x00\x00"
    assert struct.unpack_from("!H", pkt, 16)[0] == 0x7000 | (638 - 16)       # root PDU length
    assert struct.unpack_from("!I", pkt, 18)[0] == 4 and pkt[22:38] == cid
    assert struct.unpack_from("!H", pkt, 38)[0] == 0x7000 | (638 - 38)       # framing PDU length
    assert struct.unpack_from("!I", pkt, 40)[0] == 2 and pkt[44:57] == b"4D VR Theatre" and pkt[57] == 0
    assert pkt[108] == 120 and pkt[111] == 7 and pkt[112] == 0 and struct.unpack_from("!H", pkt, 113)[0] == 300
    assert struct.unpack_from("!H", pkt, 115)[0] == 0x7000 | (638 - 115)     # DMP PDU length
    assert pkt[117] == 0x02 and pkt[118] == 0xA1 and struct.unpack_from("!HHH", pkt, 119) == (0, 1, 513)
    assert pkt[125] == 0 and pkt[126:] == data                               # start code 0 + slots
    frame = dmx.parse_sacn(pkt)
    assert frame is not None and (frame.universe, frame.sequence, frame.priority, frame.data) == (300, 7, 120, data)
    assert frame.source == "4D VR Theatre" and not frame.terminated
    assert dmx.parse_sacn(dmx.build_sacn_data(cid, "x", 1, data, 0, options=dmx.SACN_OPT_TERMINATED)).terminated
    assert dmx.sacn_multicast_address(300) == "239.255.1.44" and dmx.sacn_multicast_address(1) == "239.255.0.1"
    assert dmx.parse_sacn(pkt[:100]) is None and dmx.parse_sacn(b"\x00" * 638) is None
    with pytest.raises(ValueError):
        dmx.build_sacn_data(cid, "x", 0, data, 0)
    disc = dmx.build_sacn_discovery(cid, "4D VR Theatre", [3, 1, 2])
    assert struct.unpack_from("!I", disc, 18)[0] == 8 and struct.unpack_from("!I", disc, 40)[0] == 2
    assert struct.unpack_from("!I", disc, 114)[0] == 1 and disc[120:] == b"\x00\x01\x00\x02\x00\x03"
    assert dmx.parse_sacn(disc) is None


def test_artdmx_packet_layout() -> None:
    pkt = dmx.build_artdmx(0x1234, bytes([1, 2, 3]), sequence=9)
    assert pkt[:8] == b"Art-Net\x00" and pkt[8:10] == b"\x00\x50" and pkt[10:12] == b"\x00\x0e"
    assert pkt[12] == 9 and pkt[14] == 0x34 and pkt[15] == 0x12 and pkt[16:18] == b"\x00\x04"   # padded to even length
    assert pkt[18:] == bytes([1, 2, 3, 0])
    frame = dmx.parse_artdmx(pkt)
    assert frame is not None and frame.universe == 0x1234 and frame.data == bytes([1, 2, 3, 0]) and frame.sequence == 9
    assert len(dmx.build_artdmx(0, bytes(512))) == 530 and dmx.parse_artdmx(b"Art-Net\x00\x00\x20" + bytes(20)) is None
    with pytest.raises(ValueError):
        dmx.build_artdmx(0x8000, bytes(2))


def test_enttec_message() -> None:
    msg = dmx.EnttecProOutput.build_message(bytes([9, 8, 7]))
    assert msg[0] == 0x7E and msg[1] == 6 and msg[-1] == 0xE7
    assert msg[2] | (msg[3] << 8) == 25 and msg[4] == 0 and msg[5:8] == bytes([9, 8, 7])    # start code + >= 24 slots
    full = dmx.EnttecProOutput.build_message(bytes(512))
    assert full[2] | (full[3] << 8) == 513 and len(full) == 518
    out = dmx.EnttecProOutput("/dev/does-not-exist")
    with pytest.raises(OSError):
        out.open()
    out.send(1, bytes(512))                                                  # not open: counted, never raised
    assert out.errors == 1 and "not open" in out.last_error


def _wait(cond, timeout: float = 2.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_sacn_output_reaches_a_receiver_and_terminates() -> None:
    rx = dmx.DmxReceiver("sacn", universes=[1, 2], bind_ip="127.0.0.1", port=0, multicast=False, history=100).start()
    try:
        out = dmx.SacnOutput(multicast=False, unicast=["127.0.0.1"], port=rx.port, priority=150)
        out.open()
        a, b = bytearray(512), bytearray(512)
        a[0], a[511], b[99] = 255, 7, 128
        for _ in range(3):
            out.send(1, bytes(a))
            out.send(2, bytes(b))
        assert _wait(lambda: rx.packets >= 6)
        assert rx.channel(1, 1) == 255 and rx.channel(1, 512) == 7 and rx.channel(2, 100) == 128
        snap = rx.snapshot()
        assert snap[1].priority == 150 and snap[1].sequence == 2 and snap[1].source == "4D VR Theatre"
        out.close()
        assert _wait(lambda: rx.snapshot()[1].terminated and rx.snapshot()[2].terminated)
        assert rx.channel(1, 1) == 0                                         # dark frame with the termination
        assert out.frames_sent == 6 and out.errors == 0
        out.send(1, bytes(512))                                              # after close: counted, not raised
        assert out.errors == 1
    finally:
        rx.stop()


def test_artnet_output_reaches_a_receiver() -> None:
    rx = dmx.DmxReceiver("artnet", bind_ip="127.0.0.1", port=0).start()
    try:
        out = dmx.ArtNetOutput(targets=["127.0.0.1"], universe_base=16, port=rx.port)
        out.open()
        frame = bytearray(512)
        frame[41] = 200
        out.send(3, bytes(frame))                                            # DMX universe 3 -> port-address 18
        assert _wait(lambda: 18 in rx.snapshot())
        assert rx.channel(18, 42) == 200 and rx.snapshot()[18].sequence == 1
        out.close()
    finally:
        rx.stop()


def test_capture_and_null_outputs() -> None:
    cap = dmx.CaptureOutput(history=2)
    for v in (1, 2, 3):
        cap.send(1, bytes([v]) + bytes(511))
    assert cap.channel(1, 1) == 3 and len(cap.history) == 2 and cap.channel(9, 1) == 0
    null = dmx.NullOutput()
    null.send(1, bytes(512))
    assert null.frames_sent == 1 and null.describe()["output"] == "none"


def test_usb_dmx_reopens_after_the_interface_was_unplugged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The widget may be missing at start or pulled during a show: errors are counted and it is found again."""
    import sys
    import types

    written: list[bytes] = []

    class FakeSerial:
        present = False

        def __init__(self, port: str, **_kw: object) -> None:
            if not FakeSerial.present:
                raise OSError(f"could not open port {port}")

        def write(self, data: bytes) -> int:
            if not FakeSerial.present:
                raise OSError("device disconnected")
            written.append(bytes(data))
            return len(data)

        def flush(self) -> None: ...
        def close(self) -> None: ...

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    out = dmx.EnttecProOutput("/dev/ttyUSB7", universe=1)
    out.REOPEN_INTERVAL_S = 0.05
    with pytest.raises(OSError):
        out.open()                                   # not plugged in when the service starts
    out.send(1, bytes(512))
    assert out.errors == 1 and "not open" in out.last_error and not out.is_open
    FakeSerial.present = True
    time.sleep(0.06)
    out.send(1, bytes([7]) + bytes(511))             # found again
    assert out.is_open and out.errors == 1 and written[-1][5] == 7 and written[-1][0] == 0x7E
    out.send(2, bytes(512))                          # another universe is not for this interface
    assert len(written) == 1
    FakeSerial.present = False                       # pulled during the show
    out.send(1, bytes(512))
    assert out.errors == 2 and not out.is_open and "disconnected" in out.last_error
    FakeSerial.present = True
    time.sleep(0.06)
    out.send(1, bytes(512))
    assert out.is_open and len(written) == 2
    out.close()
    assert written[-1][5:517] == bytes(512) and not out.is_open      # a dark frame on the way out
