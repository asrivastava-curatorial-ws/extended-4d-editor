"""Telemetry formats: every simulated packet is built from the published layout and must come out of
its decoder as the drive that went in; anything that is not the format is refused."""

from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from fourdvr_live import sim
from fourdvr_live.sample import G, LiveSample
from fourdvr_live.sources import DECODERS, INFOS, ORDER, SourceError, canonical, create, flight, openformat, racing

EXACT = ("open", "forza", "f1", "pcars2", "beamng", "xplane", "flightgear")       # carry every value directly
PUBLISHED_SIZES = {          # bytes per packet, as the makers' documents state them
    "forza": {232, 311, 324, 331}, "dirt": {264}, "pcars2": {559, 24}, "assetto": {328}, "outsim": {64, 92},
    "beamng": {88, 92}, "gt7": {296},
}


def collect(decoder, encode, drive: sim.Drive, seconds: float = sim.LAP_S, **kw):  # type: ignore[no-untyped-def]
    got, want, sizes = [], [], set()
    for k in range(int(seconds * 60)):
        state = drive.at(k / 60.0)
        state.frame = k
        out = None
        for packet in encode(state, **kw):
            sizes.add(len(packet))
            sample = decoder.feed(packet, k / 60.0)
            out = sample if sample is not None else out
        if out is not None and k >= 5:
            got.append(out)
            want.append(state.sample)
    return got, want, sizes


def errors(got: list[LiveSample], want: list[LiveSample]) -> dict[str, float]:
    a = np.array([[*g.accel, *g.rates, g.speed, g.rpm, g.throttle, g.brake] for g in got])
    b = np.array([[*w.accel, *w.rates, w.speed, w.rpm, w.throttle, w.brake] for w in want])
    names = ("surge", "sway", "heave", "roll", "pitch", "yaw", "speed", "rpm", "throttle", "brake")
    return {n: float(np.max(np.abs(a[:, i] - b[:, i]))) for i, n in enumerate(names)}


@pytest.mark.parametrize("name", EXACT)
def test_formats_that_carry_everything_come_back_exactly(name: str, drive: sim.Drive) -> None:
    decoder = DECODERS[name][1]()
    got, want, _sizes = collect(decoder, sim.ENCODERS[name], drive)
    assert len(got) == int(sim.LAP_S * 60) - 5 and decoder.invalid == 0
    e = errors(got, want)
    assert max(e[k] for k in ("surge", "sway", "heave")) < 2e-3
    assert max(e[k] for k in ("roll", "pitch", "yaw")) < 2e-4
    assert e["speed"] < 0.15 and e["rpm"] < 1.0                 # F1 sends whole km/h and whole revs
    if name not in ("xplane", "flightgear"):                    # aircraft formats have no pedals
        assert e["throttle"] < 0.003 and e["brake"] < 0.003
    assert all(s.active for s in got)
    assert max(s.impact for s in got) > 0.5                     # the collision of the drive is noticed


@pytest.mark.parametrize(("size", "has_pedals"), [(232, False), (311, True), (324, True), (331, True)])
def test_forza_in_all_four_packet_sizes(size: int, has_pedals: bool, drive: sim.Drive) -> None:
    got, want, sizes = collect(racing.ForzaDecoder(), sim.encode_forza, drive, 20.0, size=size)
    assert sizes == {size}
    e = errors(got, want)
    assert max(e[k] for k in ("surge", "sway", "heave", "roll", "pitch", "yaw", "speed", "rpm")) < 1e-3
    assert (e["throttle"] < 0.003) == has_pedals


def test_forza_race_flag_surface_and_puddles() -> None:
    state = sim.Drive().at(13.5)                                 # on the rough stretch
    state.sample.effects["splash"] = 0.7
    sample = racing.ForzaDecoder().feed(sim.encode_forza(state)[0], 0.0)
    assert sample is not None and sample.rumble == pytest.approx(state.sample.rumble, abs=1e-6)
    assert sample.effects == {"splash": pytest.approx(0.7)}
    state.sample.active = False
    paused = racing.ForzaDecoder().feed(sim.encode_forza(state)[0], 0.0)
    assert paused is not None and not paused.active
    strip = bytearray(sim.encode_forza(sim.Drive().at(3.0))[0])
    struct.pack_into("<4i", strip, 116, 0, 1, 0, 0)              # one wheel on a rumble strip
    on_strip = racing.ForzaDecoder().feed(bytes(strip), 0.0)
    assert on_strip is not None and on_strip.rumble == 0.8


@pytest.mark.parametrize("fmt", [2023, 2024, 2025])
def test_f1_formats_weather_and_player_index(fmt: int, drive: sim.Drive) -> None:
    decoder = racing.F1Decoder()
    got, want, sizes = collect(decoder, sim.encode_f1, drive, fmt=fmt, player=7)
    assert {1349, 1352, 1239}.issubset(sizes) and {2023: 217, 2024: 237, 2025: 273}[fmt] in sizes
    assert max(errors(got, want)[k] for k in ("surge", "sway", "heave", "roll", "pitch", "yaw")) < 2e-3
    raining = [s.effects.get("rain", 0.0) for s, w in zip(got, want, strict=True) if "rain" in w.effects]
    assert raining and np.mean(np.array(raining) > 0.0) > 0.8   # the session packet comes twice a second
    assert max(s.rumble for s in got) == 0.8 and max(s.rpm_max for s in got) == 7200.0
    assert decoder.invalid == 0


def test_f1_refuses_other_years_other_sizes_and_impossible_car_numbers() -> None:
    state = sim.Drive().at(5.0)
    motion = sim.encode_f1(state)[-1]
    d = racing.F1Decoder()
    assert d.feed(motion, 0.0) is not None
    old = bytearray(motion)
    struct.pack_into("<H", old, 0, 2022)
    assert d.feed(bytes(old), 0.0) is None and d.feed(motion[:-1], 0.0) is None
    bad_car = bytearray(motion)
    bad_car[27] = 22
    assert d.feed(bytes(bad_car), 0.0) is None and d.feed(b"\x00" * 10, 0.0) is None
    assert d.invalid == 4
    lap = bytearray(1285)                                        # a lap-data packet: valid, nothing for us
    lap[:29] = motion[:29]
    lap[6] = 2
    assert d.feed(bytes(lap), 0.0) is None and d.ignored == 1


def test_formats_that_must_work_things_out(drive: sim.Drive) -> None:
    # DiRT: rotation rates from how the direction vectors move, lift from the change of velocity
    got, want, sizes = collect(racing.CodemastersDecoder(), sim.encode_dirt, drive)
    e = errors(got, want)
    assert sizes == {264} and e["surge"] < 1e-3 and e["sway"] < 1e-3
    assert e["roll"] < 0.03 and e["pitch"] < 0.06 and e["yaw"] < 0.09 and e["heave"] < 1.2
    assert max(s.rumble for s in got) > 0.2                      # the rough stretch shows in the lift
    # OutSim: world axes turned by the heading, rates from the changes of the three angles
    got, want, sizes = collect(racing.OutSimDecoder(), sim.encode_outsim, drive)
    e = errors(got, want)
    assert sizes == {64, 92} and e["surge"] < 0.5 and e["sway"] < 0.2 and e["yaw"] < 0.09 and e["rpm"] < 1e-3
    got_id, _want, sizes = collect(racing.OutSimDecoder(), sim.encode_outsim, drive, 5.0, with_id=True)
    assert 68 in sizes and len(got_id) > 200
    # Assetto Corsa: no rotation in the format; the turning rate follows from sideways g and speed
    got, want, sizes = collect(racing.AssettoDecoder(), sim.encode_assetto, drive)
    e = errors(got, want)
    assert sizes == {328} and max(e[k] for k in ("surge", "sway", "heave")) < 1e-3 and e["yaw"] < 1e-3
    assert all(s.rates[0] == 0.0 and s.rates[1] == 0.0 for s in got)
    # GT7: no acceleration in the packet; from the change of speed and from speed x turning rate
    got, want, sizes = collect(racing.GT7Decoder(), sim.encode_gt7, drive, 25.0)
    a = np.array([[g.accel[0], w.accel[0], g.accel[1], w.accel[1]] for g, w in zip(got, want, strict=True)])
    assert sizes == {296} and np.sqrt(np.mean((a[:, 0] - a[:, 1]) ** 2)) < 0.3 and np.abs(a[:, 2] - a[:, 3]).max() < 1e-3


def test_packet_sizes_are_the_published_ones(drive: sim.Drive) -> None:
    state = drive.at(10.0)
    state.frame = 30                                             # a frame in which the slow packets are sent too
    for name, sizes in PUBLISHED_SIZES.items():
        made = {len(p) for p in sim.ENCODERS[name](state)}
        assert made <= sizes, name
    assert struct.calcsize("<HBBBBBQfIIBB") == 29                # the F1 header
    assert len(sim.assetto_greeting()) == 408 and racing.ac_request(1) == struct.pack("<iii", 1, 1, 1)
    assert (len(sim.encode_xplane(state)[0]) - 5) % 36 == 0


@pytest.mark.parametrize("name", [*DECODERS, "assetto", "gt7"])
def test_every_decoder_refuses_what_is_not_its_format(name: str, drive: sim.Drive) -> None:
    make = {"assetto": racing.AssettoDecoder, "gt7": racing.GT7Decoder}.get(name) or DECODERS[name][1]
    decoder = make()
    good = sim.ENCODERS[name](drive.at(4.0))[-1]
    for junk in (b"", b"\x00", good[:-1], good + b"\x00", bytes(len(good) + 7), b"GET / HTTP/1.1\r\n\r\n"):
        if (name == "xplane" and junk == good + b"\x00") or (name == "flightgear" and junk == good[:-1]):
            continue                                             # still a whole message of that format
        assert decoder.feed(junk, 0.0) is None
    if name in ("gt7", "beamng", "xplane", "flightgear", "open", "assetto"):
        marked = bytearray(good)
        marked[0] ^= 0xFF                                        # the format's marker is wrong
        assert decoder.feed(bytes(marked), 0.0) is None
    assert decoder.invalid >= 4
    assert decoder.feed(good, 1.0) is not None                   # and it still works after all that


def test_salsa20_against_an_independent_implementation() -> None:
    # key stream of bytes(range(32)) / bytes(range(100, 108)), blocks 0 and 2, as PyCryptodome gives it
    key, nonce = bytes(range(32)), bytes(range(100, 108))
    assert racing.salsa20_block(key, nonce, 0).hex() == (
        "dac41fef88bcf58c96656f911f208f70fc63e045cdcd6d30eb76fdbc24be6930"
        "5c8f6ec704d87dd6af859d5753768f9c4e274b2312fb7b5a3eb3706e027549bc")
    assert racing.salsa20_block(key, nonce, 2).hex() == (
        "10c44c21a24be69d781571dad14258754568d240980aece914a74d5a51b3471d"
        "acbcea35117191e60cf885e2f602bf460fdd6332e21a7966782ba6e23929511f")


def test_gt7_pause_flag_and_wrong_key() -> None:
    state = sim.Drive().at(6.0)
    d = racing.GT7Decoder()
    assert d.feed(sim.encode_gt7(state)[0], 0.0).active        # type: ignore[union-attr]
    state.sample.active = False
    assert not d.feed(sim.encode_gt7(state)[0], 0.1).active    # type: ignore[union-attr]
    tampered = bytearray(sim.encode_gt7(state)[0])
    tampered[0x40] ^= 1                                          # another seed: nothing decrypts
    assert d.feed(bytes(tampered), 0.2) is None and d.invalid == 1


def test_pcars2_game_state_and_weather(drive: sim.Drive) -> None:
    d = racing.Pcars2Decoder()
    state = drive.at(26.0)                                       # it rains here
    state.frame = 0
    for packet in sim.encode_pcars2(state):
        sample = d.feed(packet, 0.0)
    assert sample is not None and sample.active and sample.effects["rain"] == pytest.approx(0.6, abs=0.01)
    state.sample.active = False
    for packet in sim.encode_pcars2(state):
        sample = d.feed(packet, 0.1)
    assert sample is not None and not sample.active
    other = bytearray(308)
    other[10] = 1                                                # race data: valid, nothing for us
    assert d.feed(bytes(other), 0.2) is None and d.ignored == 1


def test_dirt_notices_a_paused_game(drive: sim.Drive) -> None:
    d = racing.CodemastersDecoder()
    for k in range(10):
        s = drive.at(5.0 + k / 60.0)
        sample = d.feed(sim.encode_dirt(s)[0], k / 60.0)
    assert sample is not None and sample.active
    frozen = sim.encode_dirt(drive.at(5.2))[0]
    for k in range(5):                                           # the game's clock stands still
        sample = d.feed(frozen, 1.0 + k / 60.0)
    assert sample is not None and not sample.active and sample.rates == (0.0, 0.0, 0.0)


def test_xplane_uses_only_its_rows_and_needs_the_motion_rows() -> None:
    d = flight.XPlaneDecoder()
    speeds = b"DATA*" + struct.pack("<i8f", 3, 120.0, *([-999.0] * 7))
    assert d.feed(speeds, 0.0) is None and d.ignored == 1 and d.rows == {3}
    motion = b"DATA*" + struct.pack("<i8f", 4, 0.2, -999, 100.0, -999, 2.0, 0.1, -0.2, -999) + struct.pack(
        "<i8f", 16, 0.1, 0.2, -0.3, *([-999.0] * 5)) + struct.pack("<i8f", 99, *([1.0] * 8))
    s = d.feed(motion, 0.1)
    assert s is not None
    assert s.accel == pytest.approx((0.1 * G, 0.2 * G, G))       # a 2 g pull-up presses into the seat
    assert s.rates == pytest.approx((0.2, -0.1, 0.3))            # P; nose-up Q is nose-down negative; R right is left negative
    assert s.speed == pytest.approx(120.0 * flight.KNOT)


def test_flightgear_protocol_file_matches_the_decoder() -> None:
    root = ET.fromstring(flight.flightgear_protocol_xml())
    out = root.find("generic/output")
    assert out is not None and out.findtext("var_separator") == "," and out.findtext("line_separator") == "newline"
    chunks = out.findall("chunk")
    assert chunks[0].findtext("format") == flight.FG_MAGIC and chunks[0].find("node") is None
    assert [c.findtext("node") for c in chunks[1:]] == [c[1] for c in flight.FG_CHUNKS]
    line = (b"4DFG1,3.2808,0.0,-32.1740,0,10.0,0,0,0,100,90,2300,1,0,0.5,0\n")
    s = flight.FlightGearDecoder().feed(line, 0.0)
    assert s is not None and s.accel == pytest.approx((1.0, 0.0, 0.0), abs=2e-3)
    assert s.rates[1] == pytest.approx(-0.17453, abs=1e-4) and s.effects == {"rain": 0.5} and not s.airborne
    paused = flight.FlightGearDecoder().feed(line.replace(b",1,0,0.5", b",1,1,0.5"), 0.0)
    assert paused is not None and not paused.active


def test_open_format_reads_what_a_sender_may_send() -> None:
    d = openformat.OpenDecoder()
    s = d.feed(json.dumps({"4dlive": 1, "accel_g": [0.5, 0, 0], "rates_deg": [0, 0, 90], "speed": 12, "attitude": [0.1, 0.2],
                           "effects": {"rain": 0.4, "fog": 2}, "scent": 3, "strobe_hz": 5}).encode(), 0.0)
    assert s is not None and s.accel[0] == pytest.approx(0.5 * G) and s.rates[2] == pytest.approx(np.pi / 2)
    assert s.effects == {"rain": 0.4, "fog": 2.0} and s.params == {"scent": {"slot": 3.0}, "strobe": {"rate_hz": 5.0}}
    assert d.feed(b'{"4dlive": 1}', 0.1) is not None             # everything else is optional
    for bad in (b"not json", b"[1, 2]", b'{"accel": [1, 2, 3]}', b'{"4dlive": 2}', b'{"4dlive": 1, "accel": [1, 2]}',
                b'{"4dlive": 1, "effects": 3}', b'{"4dlive": 1, "speed": "fast"}', b"{" + b" " * 5000 + b"}"):
        assert d.feed(bad, 0.2) is None
    assert d.invalid == 8
    fresh = openformat.OpenDecoder()
    assert fresh.feed(b'{"4dlive": 1, "rates": 1}', 0.0) is None and "rates must be three numbers" in fresh.last_problem


def test_open_format_velocity_instead_of_acceleration() -> None:
    d = openformat.OpenDecoder()
    out = []
    for k in range(120):                                         # 20 m/s forwards, turning left at 0.3 rad/s, then braking
        t = k / 60.0
        v = 20.0 if t < 1.0 else 20.0 - 6.0 * (t - 1.0)
        out.append(d.feed(json.dumps({"4dlive": 1, "t": t, "velocity": [v, 0, 0], "rates": [0, 0, 0.3]}).encode(), t))
    assert out[0] is not None and out[0].accel == (0.0, 0.0, 0.0)
    assert out[30].accel == pytest.approx((0.0, 6.0, 0.0), abs=1e-6)      # sideways: speed x turning rate, to the left
    assert out[100].accel[0] == pytest.approx(-6.0, abs=1e-3) and out[100].speed == pytest.approx(16.0, abs=0.1)


def test_registry_names_aliases_and_options() -> None:
    assert set(ORDER) == set(INFOS) and canonical("AMS2") == "pcars2" and canonical("X-Plane") == "xplane"
    with pytest.raises(SourceError, match="unknown source"):
        canonical("pong")
    with pytest.raises(SourceError, match="unknown key"):
        create({"type": "forza", "prot": 5300})
    with pytest.raises(SourceError, match="needs file"):
        create({"type": "replay"})
    src = create({"type": "dirt", "port": 20999})
    assert src.info.name == "dirt" and src.status()["port"] == 20999
    for name in ("assetto", "gt7"):
        with pytest.raises(SourceError, match="address of the game"):
            create({"type": name, "port": 0 or 39999}).open()
    with pytest.raises(SourceError, match="port must be"):
        create({"type": "forza", "port": 0}).open()
