"""The 4DFX link: packets, the ESP32 output of a venue patch, and the effects service driving simulated nodes."""

from __future__ import annotations

import socket
import struct
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest

from fourdvr_fx import BassTrack, Cue, EffectsDocument
from fourdvr_fx import bridge as br
from fourdvr_fx.audio import AudioState
from fourdvr_fx.engine import THEATRE, ShowState
from fourdvr_fx.patch import AudioConfig, PatchError, patch_from_dict
from fourdvr_fx.service import EffectsService

PLATFORMS = ["PLATFORM-01", "PLATFORM-02"]
HOSTS = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def free_port() -> int:
    socks = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((HOSTS[0], 0))
        socks.append(s)
        port = s.getsockname()[1]
        for host in HOSTS[1:]:                      # the same port must be free on every loopback address used
            t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            socks.append(t)
            t.bind((host, port))
        return int(port)
    except OSError:
        pytest.skip("this system has no 127.0.0.2 / 127.0.0.3 loopback addresses")
    finally:
        for s in socks:
            s.close()


@pytest.fixture
def port() -> int:
    return free_port()


@pytest.fixture
def nodes(port: int) -> Iterator[Callable[..., br.SimNode]]:
    made: list[br.SimNode] = []

    def make(index: int, **kw: object) -> br.SimNode:
        node = br.SimNode(node_id=index + 1, host=HOSTS[index], port=port, **kw).start()  # type: ignore[arg-type]
        made.append(node)
        return node

    yield make
    for n in made:
        n.stop()


# ------------------------------------------------------------------------------ packets
def test_packets_round_trip_and_bad_ones_are_refused() -> None:
    p = br.build_dmx(7, 41, 1, 10, bytes([1, 2, 255]))
    assert len(p) == 12 + 6 + 3 + 4 and p[:4] == b"4DFX" and p[4:8] == bytes([1, 0, br.TYPE_DMX, 7])
    ptype, node, seq, payload = br.decode(p)
    assert (ptype, node, seq) == (br.TYPE_DMX, 7, 41) and br.parse_dmx(payload) == (1, 0, 10, bytes([1, 2, 255]))
    assert struct.unpack("<I", p[-4:])[0] == zlib.crc32(p[:-4])
    for bad, why in ((p[:-1], "checksum"), (b"XXXX" + p[4:], "not a 4DFX"), (p[:10], "too short"),
                     (p[:4] + b"\x02" + p[5:], "version"), (p[:20] + bytes([p[20] ^ 1]) + p[21:], "checksum")):
        with pytest.raises(br.LinkError, match=why):
            br.decode(bad)
    with pytest.raises(ValueError):
        br.build_dmx(1, 1, 0, 510, bytes(4))                                  # slots 510..513
    for payload in (struct.pack("<BBHH", 0, 0, 0, 1) + b"x", struct.pack("<BBHH", 0, 0, 512, 2) + b"xy", struct.pack("<BBHH", 0, 0, 1, 3) + b"xy"):
        with pytest.raises(br.LinkError):
            br.parse_dmx(payload)
    pcm = (np.arange(40, dtype=np.int16) - 20).reshape(20, 2)
    flags, rate, position, got = br.parse_audio(br.decode(br.build_audio(3, 9, 4000, 123456, pcm, br.AUDIO_START))[3])
    assert (flags, rate, position) == (br.AUDIO_START, 4000, 123456) and np.array_equal(got, pcm)
    with pytest.raises(ValueError):
        br.build_audio(3, 9, 4000, 0, np.zeros((br.MAX_AUDIO_FRAMES + 1, 1), np.int16))
    with pytest.raises(br.LinkError):
        br.parse_audio(struct.pack("<BBHIH", 3, 0, 4000, 0, 4) + bytes(24))   # three channels
    assert br.decode(br.build_hello(0, 1, 1_700_000_000_123_456))[3] == struct.pack("<Q", 1_700_000_000_123_456)
    assert br.decode(br.build_stop(2, 5, br.STOP_AUDIO)) == (br.TYPE_STOP, 2, 5, bytes([2, 0]))
    info = br.NodeInfo(99, 1234, 0x0102, br.CAP_DMX | br.CAP_AUDIO, 1, 2, br.STATE_INHIBITED, 4000, 120, 80, 40, 70000, 3, 2, 1, 4)
    assert len(info.pack()) == 38 and br.NodeInfo.unpack(info.pack()) == info
    assert info.has() == ["dmx", "audio"] and info.has_dmx and info.has_audio and info.inhibited
    assert not br.NodeInfo(capabilities=br.CAP_DMX, dmx_ports=0).has_dmx and br.NodeInfo(capabilities=br.CAP_ACTUATORS).has() == ["platform"]


def test_vectors_for_the_firmware(tmp_path: Path) -> None:
    """The vectors the firmware's host tests check its packet code against are what this side produces."""
    from fourdvr_fx.bridge_vectors import vectors

    stored = Path(__file__).resolve().parents[2] / "firmware" / "esp32-actuator-bridge" / "test" / "fx_vectors.txt"
    assert stored.read_text() == vectors(), "regenerate with: fourdvr fx test-vectors -o " + str(stored)


# ------------------------------------------------------------------------------ output <-> nodes
def test_a_node_gets_what_it_has_and_nothing_else(nodes, port: int) -> None:  # type: ignore[no-untyped-def]
    full = nodes(0, dmx_ports=1, audio_channels=2)
    no_dmx = nodes(1, dmx_ports=0, audio_channels=2)                        # an ESP32 with a shaker but no DMX line
    out = br.BridgeOutput({1: (HOSTS[0], 0, 12), 101: (HOSTS[0], 0, 30), 102: (HOSTS[1], 0, 30), 103: (HOSTS[2], 0, 30)},
                          {HOSTS[0]: "PLATFORM-01", HOSTS[1]: "PLATFORM-02", HOSTS[2]: "PLATFORM-03"}, port,
                          AudioConfig(enabled=True, sample_rate_hz=4000, channels=2))
    out.open()
    frame = bytearray(512)
    frame[0], frame[11], frame[29] = 200, 50, 99

    def tick() -> None:
        out.send(1, bytes(frame))
        other = bytearray(512)
        other[0], other[20] = 100, 77                                       # the platform's own fixtures on the same wire
        out.send(101, bytes(other))
        out.send(102, bytes(frame))
        out.send(103, bytes(frame))
        out.flush()

    assert wait_for(lambda: (tick(), out.nodes[HOSTS[0]].present(time.monotonic()) and out.nodes[HOSTS[1]].present(time.monotonic()))[1])
    assert wait_for(lambda: (tick(), full.channel(0, 21) == 77)[1])
    assert full.channel(0, 1) == 200 and full.channel(0, 12) == 50 and full.channel(0, 30) == 99      # merged: the larger value wins
    assert max(len(d) for d in [bytes(full.dmx[0])]) == 512 and full.dmx_history[-1][2][30:] == bytes(482)   # only 30 slots are sent
    d = out.describe()
    by_host = {n["host"]: n for n in d["nodes"]}
    assert d["output"] == "esp32" and d["nodes_configured"] == 3 and d["nodes_present"] == 2 and d["errors"] == 0
    assert by_host[HOSTS[0]]["has"] == ["dmx", "audio"] and by_host[HOSTS[0]]["lines"] == {"0": "theatre + PLATFORM-01"}
    assert by_host[HOSTS[1]]["has"] == ["audio"] and not by_host[HOSTS[2]]["present"]
    assert no_dmx.packets > 0 and no_dmx.invalid == 0 and not no_dmx.dmx_history     # asked what it has; sent no DMX
    assert out.nodes[HOSTS[1]].dmx_skipped > 0 and out.nodes[HOSTS[2]].dmx_skipped > 0 and out.errors == 0
    assert {n.host for n in out.audio_nodes()} == {HOSTS[0], HOSTS[1]}
    # a node that is switched on later is picked up within about a second
    late = nodes(2, dmx_ports=1, audio_channels=0)
    assert wait_for(lambda: (tick(), late.channel(0, 30) == 99)[1], 4.0)
    assert {n.host for n in out.audio_nodes()} == {HOSTS[0], HOSTS[1]}     # ... and it has no audio output
    out.close()
    assert wait_for(lambda: full.channel(0, 1) == 0 and late.channel(0, 30) == 0) and full.stops >= 1     # told to go dark


def test_a_node_goes_dark_by_itself_and_ignores_strangers(nodes, port: int) -> None:  # type: ignore[no-untyped-def]
    node = nodes(0, dmx_ports=2, audio_channels=0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    to = (HOSTS[0], port)
    sock.sendto(br.build_dmx(1, 1, 1, 5, bytes([10, 20, 30])), to)
    sock.sendto(br.build_dmx(9, 2, 0, 1, bytes([1])), to)                    # another node's packet
    sock.sendto(b"4DFX" + bytes(30), to)                                     # garbage
    sock.sendto(br.build_dmx(1, 3, 5, 1, bytes([1])), to)                    # a port this node does not have: ignored
    assert wait_for(lambda: node.channel(1, 6) == 20 and node.invalid == 2)
    assert node.channel(0, 1) == 0 and node.info().state & br.STATE_DMX_ACTIVE
    sock.sendto(br.build_dmx(1, 4, 1, 6, bytes([99])), to)                   # other slots stay as they were
    assert wait_for(lambda: node.channel(1, 6) == 99) and node.channel(1, 5) == 10 and node.channel(1, 7) == 30
    assert wait_for(lambda: node.channel(1, 6) == 0, 2.5) and node.dmx_timeouts == 1      # a second without packets: dark
    node.inhibited = True                                                    # emergency stop on a node with a platform
    sock.sendto(br.build_dmx(1, 5, 1, 5, bytes([10, 20, 30])), to)
    sock.sendto(br.build_hello(0, 6, 5), to)
    sock.settimeout(2.0)
    info = br.NodeInfo.unpack(br.decode(sock.recvfrom(256)[0])[3])
    assert info.inhibited and info.echo_us == 5 and node.channel(1, 6) == 0 and info.dmx_ports == 2
    sock.close()


def test_sound_is_streamed_in_step_with_the_show(nodes, port: int) -> None:  # type: ignore[no-untyped-def]
    node = nodes(0, dmx_ports=0, audio_channels=2, audio_latency_ms=100)
    cfg = AudioConfig(enabled=True, sample_rate_hz=4000, channels=2, bass_channels=[1], vibration_channels=[2],
                      vibration_frequency_hz=50.0, vibration_gain_db=0.0)
    out = br.BridgeOutput({}, {HOSTS[0]: "PLATFORM-01"}, port, cfg)
    out.open()
    assert wait_for(lambda: (out.poll(), bool(out.audio_nodes()))[1])
    (n,) = out.audio_nodes()
    assert n.audio is not None and n.audio.rate == 4000 and n.audio.latency_s == pytest.approx(0.1)
    t = np.arange(8000) / 2000.0
    track = BassTrack.from_float(0.5 * np.sin(2 * np.pi * 40.0 * t), 2000)   # 4 s of 40 Hz
    tid = n.audio.renderer.prepare(track)
    start = time.monotonic()
    while time.monotonic() - start < 1.0:                                    # one second of show from position 1.0 s
        now = time.monotonic()
        out.stream(n, AudioState(True, 1.0 + (now - start), now, tid, vibration=0.5), now)
        time.sleep(0.02)
    out.stream(n, AudioState(False), time.monotonic())                       # the show pauses
    assert wait_for(lambda: node.stops >= 1)
    got = node.audio().astype(np.float64) / 32767.0
    assert node.audio_gaps == 0 and node.invalid == 0 and node.audio_blocks[0][1] == br.AUDIO_START
    assert 3600 <= len(got) <= 4300                                          # about one second at 4 kHz, sent in real time
    k = np.arange(len(got))
    # channel 1: the bass track where the show will be when it is heard: position 1.0 s + 100 ms of buffer ahead
    expected = 0.5 * np.sin(2 * np.pi * 40.0 * (1.1 + k / 4000.0))
    lag = np.argmax(np.correlate(got[400:3400, 0], expected[300:3500], "valid")) - 100
    assert abs(lag) <= 20, lag                                               # within 5 ms
    assert np.max(np.abs(got[500:3000, 0])) == pytest.approx(0.5, abs=0.06)
    spectrum = np.abs(np.fft.rfft(got[1000:3000, 1] * np.hanning(2000)))
    assert np.fft.rfftfreq(2000, 1 / 4000.0)[spectrum.argmax()] == pytest.approx(50.0, abs=2.5)     # channel 2: the vibration tone
    assert np.max(np.abs(got[1000:3000, 1])) == pytest.approx(0.5, abs=0.08)
    blocks = [b[2] for b in node.audio_blocks]
    assert blocks == sorted(blocks) and len(blocks) >= 30                    # a block per frame, numbered without a break
    out.close()


# ------------------------------------------------------------------------------ the patch
ESP32_PATCH = {
    "output": {"protocol": "esp32", "esp32": {"theatre_platform": "PLATFORM-01", "audio": {"bass_channels": [1, 2], "vibration_channels": [1]}}},
    "fixtures": [
        {"id": "fog", "effect": "fog", "address": 1},
        {"id": "strobe", "effect": "strobe", "address": 2, "flash": "software"},
        {"id": "wind", "effect": "wind", "scope": "platform", "address": 10},
        {"id": "rain", "effect": "rain", "scope": "platform", "address": 11},
        {"id": "heat", "effect": "heat", "scope": "platform", "address": 12},
    ],
}


def test_patch_with_esp32_output() -> None:
    patch = patch_from_dict(ESP32_PATCH)
    inst = {i.key: (i.universe, i.address) for i in patch.instances(PLATFORMS)}
    assert inst == {"fog": (1, 1), "strobe": (1, 2), "wind@PLATFORM-01": (101, 10), "rain@PLATFORM-01": (101, 11), "heat@PLATFORM-01": (101, 12),
                    "wind@PLATFORM-02": (102, 10), "rain@PLATFORM-02": (102, 11), "heat@PLATFORM-02": (102, 12)}
    assert patch.problems(PLATFORMS) == [] and patch.warnings() == []        # the same address on every platform's own line
    hosts = {"PLATFORM-01": "10.0.0.1", "PLATFORM-02": "10.0.0.2"}
    assert patch.esp32_lines(PLATFORMS, hosts) == {1: ("10.0.0.1", 0), 2: ("10.0.0.1", 1), 101: ("10.0.0.1", 0), 102: ("10.0.0.2", 0)}
    assert patch.esp32_lines(PLATFORMS) == {}                                # no addresses known: nothing to send to
    out = br.create_output(patch, PLATFORMS, hosts)
    assert isinstance(out, br.BridgeOutput) and out.udp_port == 47002
    assert out.lines == {1: ("10.0.0.1", 0, 2), 101: ("10.0.0.1", 0, 12), 102: ("10.0.0.2", 0, 12)}
    assert {h: n.platform for h, n in out.nodes.items()} == {"10.0.0.1": "PLATFORM-01", "10.0.0.2": "PLATFORM-02"}
    assert out.audio_cfg is not None and out.audio_cfg.vibration_channels == [1] and out.audio_cfg.sample_rate_hz == 4000
    limits = {f.id: f.limits for f in patch.fixtures}
    assert (limits["rain"].max_on_s, limits["rain"].min_off_s, limits["rain"].max_total_on_s) == (5.0, 5.0, 40.0)
    assert (limits["heat"].max_on_s, limits["heat"].min_off_s, limits["heat"].max_duty) == (6.0, 10.0, 0.25)
    # theatre devices on a platform's ESP32 share its wire: an address used twice there is a mistake
    clash = patch_from_dict(ESP32_PATCH | {"fixtures": [*ESP32_PATCH["fixtures"], {"id": "bubbles", "effect": "bubbles", "address": 10}]})
    clash.output.esp32["platforms"] = hosts
    assert clash.problems(PLATFORMS) == ["fixture bubbles: DMX line 1 of 10.0.0.1 channel 10 already used by wind@PLATFORM-01"]
    # ... on an ESP32 of their own it is none
    own = patch_from_dict(ESP32_PATCH | {"fixtures": [*ESP32_PATCH["fixtures"], {"id": "bubbles", "effect": "bubbles", "address": 10}]})
    own.output.esp32 |= {"platforms": hosts, "theatre": "10.0.0.9", "theatre_platform": ""}
    assert own.problems(PLATFORMS) == [] and br.create_output(own, PLATFORMS).nodes["10.0.0.9"].platform is None      # type: ignore[attr-defined]
    far = patch_from_dict({"output": {"protocol": "esp32"}, "fixtures": [{"id": "fog", "effect": "fog", "universe": 3}]})
    assert "universe 1" in far.problems(PLATFORMS)[0] and "no ESP32" in far.warnings()[0]
    for bad, message in (({"platforms": {"PLATFORM-01": 5}}, "platforms"), ({"audio": {"bass_channels": [3]}}, "channels are 1"),
                         ({"address": "x"}, "unknown key"), ({"port": 0}, "port"),
                         ({"theatre": "10.0.0.9", "theatre_platform": "PLATFORM-01"}, "not both")):
        with pytest.raises(PatchError, match=message):
            patch_from_dict({"output": {"protocol": "esp32", "esp32": bad}})
    assert not isinstance(br.create_output(patch_from_dict({"output": {"protocol": "none"}}), PLATFORMS), br.BridgeOutput)


# ------------------------------------------------------------------------------ the service through nodes
def test_service_drives_what_each_esp32_has(nodes, port: int) -> None:  # type: ignore[no-untyped-def]
    first = nodes(0, dmx_ports=1, audio_channels=2, actuators=True)          # platform 1: DMX line (with the theatre devices) and shaker
    second = nodes(1, dmx_ports=0, audio_channels=2, actuators=True)         # platform 2: a shaker, no DMX line
    spec = {**ESP32_PATCH, "output": {"protocol": "esp32", "esp32": {"port": port, "theatre_platform": "PLATFORM-01"}}}
    patch = patch_from_dict(spec)
    doc = EffectsDocument(60.0)
    doc.new_track("fog").cues = [Cue(1.0, 2.0)]
    doc.new_track("wind").points = [(0.0, 0.6), (60.0, 0.6)]
    doc.new_track("heat").cues = [Cue(1.0, 3.0)]
    doc.new_track("vibration").points = [(0.0, 0.8), (60.0, 0.8)]
    t = np.arange(120000) / 2000.0
    doc.bass = BassTrack.from_float(0.4 * np.sin(2 * np.pi * 35.0 * t), 2000)
    compiled = doc.compile()
    playing = {"PLATFORM-01": True, "PLATFORM-02": True}
    start = time.monotonic()

    def provider(now: float) -> dict[str, ShowState]:
        pos = 1.2 + (now - start)
        shows = {p: (ShowState(True, compiled, pos, 60.0, False, 1) if playing[p] else ShowState()) for p in PLATFORMS}
        shows[THEATRE] = shows["PLATFORM-01"] if playing["PLATFORM-01"] else shows["PLATFORM-02"]
        return shows

    svc = EffectsService(patch, PLATFORMS, provider, hosts={"PLATFORM-01": HOSTS[0], "PLATFORM-02": HOSTS[1]})
    svc.start()
    try:
        assert wait_for(lambda: first.channel(0, 1) == 255 and first.channel(0, 12) == 255 and first.channel(0, 10) > 100, 4.0)
        assert wait_for(lambda: len(first.audio()) > 2000 and len(second.audio()) > 2000, 4.0)
        st = svc.status()
        assert st["errors"] == 0 and st["output"]["errors"] == 0 and st["output"]["nodes_present"] == 2
        by_host = {n["host"]: n for n in st["output"]["nodes"]}
        assert by_host[HOSTS[0]]["has"] == ["platform", "dmx", "audio"] and by_host[HOSTS[1]]["has"] == ["platform", "audio"]
        assert not second.dmx_history and second.invalid == 0               # its DMX fixtures were dropped without a word
        for node in (first, second):                                         # both shakers play the bass and the rumble
            x = node.audio().astype(np.float64)[400:2400, 0] / 32767.0
            spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            freqs = np.fft.rfftfreq(len(x), 1 / 4000.0)
            assert {round(float(freqs[i]) / 5) * 5 for i in np.argsort(-spectrum)[:2]} == {35, 45}
        # platform 2 pauses: its shaker falls silent; platform 1 (and the theatre devices on it) carry on
        playing["PLATFORM-02"] = False
        before = len(second.audio())
        assert wait_for(lambda: second.stops >= 1)
        time.sleep(0.3)
        assert len(second.audio()) - before < 600 and first.channel(0, 10) > 100
        grown = len(first.audio())
        assert wait_for(lambda: len(first.audio()) > grown + 800)
    finally:
        svc.stop()
    assert wait_for(lambda: first.channel(0, 10) == 0 and first.channel(0, 1) == 0) and svc.status()["errors"] == 0


def test_service_without_any_node_is_quiet(port: int) -> None:
    """No ESP32 answers at all (or none is known): nothing is sent, nothing is an error, the frames are still rendered."""
    patch = patch_from_dict({**ESP32_PATCH, "output": {"protocol": "esp32", "esp32": {"port": port, "theatre_platform": "PLATFORM-01"}}})
    doc = EffectsDocument(10.0)
    doc.new_track("wind").points = [(0.0, 1.0), (10.0, 1.0)]
    compiled = doc.compile()
    show = ShowState(True, compiled, 2.0, 10.0, False, 1)
    for hosts in ({"PLATFORM-01": HOSTS[0], "PLATFORM-02": HOSTS[1]}, None):
        svc = EffectsService(patch, PLATFORMS, lambda _now, s=show: {THEATRE: s, "PLATFORM-01": s, "PLATFORM-02": s}, hosts=hosts)
        svc.start()
        time.sleep(0.4)
        st = svc.status()
        svc.stop()
        assert st["running"] and st["errors"] == 0 and st["output"]["errors"] == 0 and st["output"]["nodes_present"] == 0
        assert svc.universes_sent[101][9] > 0 and st["output"]["nodes_configured"] == (2 if hosts else 0)
