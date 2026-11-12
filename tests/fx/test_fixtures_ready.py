"""The fixture library (Open Fixture Library profiles as patch entries), the patch writer, and the
"is the venue ready?" report with simulated ESP32 boards, a stand-in Art-Net node and Control Server."""

from __future__ import annotations

import json
import socket
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fourdvr_fx import Cue, EffectsDocument
from fourdvr_fx import bridge as br
from fourdvr_fx import ready as rd
from fourdvr_fx.cli import main as fx_main
from fourdvr_fx.fixtures import FixtureError, Library, describe, level_range, library, parse_fixture, plan, strobe_range
from fourdvr_fx.patch import PatchError, load_patch, patch_from_dict
from fourdvr_fx.patchfile import fixture_toml, read_data, to_toml, write_data

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ["PLATFORM-01", "PLATFORM-02"]
HOSTS = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]


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


# ------------------------------------------------------------------------------ the library
def test_the_library_inside_the_package_is_found_by_words_and_by_category() -> None:
    lib = library()
    info = lib.info()
    assert info["fixtures"] > 600 and not info["problems"] and info["snapshot"]["commit"]
    assert "cameo/instant-air-1000-pro" in [e.key for e in lib.search("", "fan")]
    assert [e.key for e in lib.search("hazer 1500 cameo")] == ["cameo/instant-hazer-1500-t-pro"]
    assert lib.search("no such maker anywhere") == []
    assert lib.get("CAMEO/Instant-Air-1000-Pro").title == "cameo Instant Air 1000 Pro"
    with pytest.raises(FixtureError, match="did you mean cameo/instant-air-1000-pro"):
        lib.get("instant-air-1000")
    assert lib.categories()["Smoke"] >= 10


def test_every_mode_that_can_be_patched_gives_an_entry_the_patch_reader_accepts() -> None:
    lib = library()
    plans = refused = 0
    for key in lib.keys():
        model = lib.get(key)
        for mode in model.modes:
            if mode.unsupported:
                assert mode.unsupported.startswith(("a pixel matrix", "channel ")) or "channels" in mode.unsupported
                continue
            for effect in ("fog", "strobe", "heat"):
                try:
                    result = plan(model, mode, effect)
                except FixtureError as exc:
                    assert effect == "strobe" and "cannot be patched as a strobe" in str(exc)
                    refused += 1
                    continue
                patch = patch_from_dict(tomllib.loads(fixture_toml(result.entry("x", 1, 1))))
                fixture = patch.fixtures[0]
                assert fixture.footprint <= mode.footprint and fixture.model == key and result.driven
                assert any(c.role in ("level", "rate") for c in fixture.channels), (key, mode.short, effect)
                if effect == "strobe":          # never faster than the venue's limit: its own strobe only if it can be that slow
                    rates = [c for c in fixture.channels if c.role == "rate"]
                    assert (fixture.flash == "software" and not rates) or all(c.hz_min <= 3.0 for c in rates)
                    assert not patch.warnings() or "flash rate" not in " ".join(patch.warnings())
                plans += 1
    assert plans > 5000 and refused < 30


def test_what_the_effect_drives_fog_fan_dimmer_pack_and_fine_channels() -> None:
    lib = library()
    hazer = lib.get("cameo/instant-hazer-1500-t-pro")
    fog = plan(hazer, hazer.mode(), "fog")
    assert fog.channels[0] == {"role": "level", "offset": 0, "min": 6, "name": "Haze"} and fog.driven == "Haze"
    assert fog.channels[1]["role"] == "static" and "Fan" in " ".join(fog.notes)          # its fan is left at 0, and said so
    wind = plan(hazer, hazer.mode(), "wind")
    assert wind.channels[1]["role"] == "level" and wind.driven == "Fan"                  # as a wind device its fan is driven
    fan = lib.get("cameo/instant-air-1000-pro")
    assert plan(fan, fan.mode(), "wind").channels == [{"role": "level", "offset": 0, "min": 6, "name": "Fan"}]
    desk = lib.get("generic/desk-channel")
    fine = plan(desk, desk.mode("16bit"), "heat").channels
    assert [c["role"] for c in fine] == ["level", "static"] and "value" not in fine[1]
    lines = describe(hazer, hazer.mode(), fog)
    assert "driven by the effect: off 0, 6 … 255" in "\n".join(lines)


def pack(outputs: int) -> dict[str, Any]:
    return {"name": "Relay Pack", "categories": ["Dimmer"],
            "availableChannels": {f"Out {k}": {"capability": {"type": "Intensity"}} for k in range(1, outputs + 1)},
            "modes": [{"name": f"{outputs} channels", "shortName": f"{outputs}ch", "channels": [f"Out {k}" for k in range(1, outputs + 1)]}]}


def test_a_pack_of_outputs_is_patched_one_output_at_a_time() -> None:
    model = parse_fixture(pack(4), "user/relay-pack")
    third = plan(model, model.mode(), "water", output=3)
    assert third.channels == [{"role": "level", "offset": 2, "name": "Out 3"}] and third.outputs == 4
    assert "output 3" in third.label and "others stay free" in third.notes[0]
    assert plan(model, model.mode(), "strobe", output=1).flash == "software"       # a lamp on a dimmer does not flash by itself
    with pytest.raises(FixtureError, match=r"outputs 1\.\.4"):
        plan(model, model.mode(), "water", output=5)
    two = patch_from_dict({"fixtures": [third.entry("valve", 1, 10), plan(model, model.mode(), "air", output=4).entry("blast", 1, 10)]})
    assert two.problems(PLATFORMS) == []            # outputs 3 and 4 of one pack at the same start address do not collide


def strobe_fixture(start: str, end: str, dimmer: bool = True) -> dict[str, Any]:
    channels: dict[str, Any] = {"Strobe": {"capabilities": [
        {"dmxRange": [0, 9], "type": "ShutterStrobe", "shutterEffect": "Open"},
        {"dmxRange": [10, 255], "type": "ShutterStrobe", "shutterEffect": "Strobe", "speedStart": start, "speedEnd": end}]},
        "Red": {"capability": {"type": "ColorIntensity", "color": "Red"}}}
    order = ["Strobe", "Red"]
    if dimmer:
        channels["Dimmer"] = {"capability": {"type": "Intensity"}}
        order.append("Dimmer")
    return {"name": "Flash", "categories": ["Strobe"], "availableChannels": channels,
            "modes": [{"name": "m", "shortName": "m", "channels": order}]}


def test_a_strobe_flashes_itself_only_if_it_can_be_as_slow_as_the_venue_allows() -> None:
    slow = parse_fixture(strobe_fixture("1Hz", "25Hz"), "user/slow")
    p = plan(slow, slow.mode(), "strobe")
    assert p.flash == "fixture"
    assert p.channels == [{"role": "rate", "offset": 0, "min": 10, "hz_max": 25.0, "name": "Strobe"},
                          {"role": "static", "offset": 1, "value": 255, "name": "Red"},
                          {"role": "level", "offset": 2, "name": "Dimmer"}]
    fast = parse_fixture(strobe_fixture("5Hz", "25Hz"), "user/fast")
    p = plan(fast, fast.mode(), "strobe")                    # 5 a second is above the limit of 3: we switch its dimmer
    assert p.flash == "software" and "cannot flash slower than 5" in p.notes[0]
    assert p.channels[0] == {"role": "static", "offset": 0, "value": 4, "name": "Strobe"}        # its shutter open while it is lit
    assert plan(fast, fast.mode(), "strobe", max_rate_hz=10.0).flash == "fixture"                # an assessed, higher limit
    unknown = parse_fixture(strobe_fixture("slow", "fast"), "user/unknown")
    assert plan(unknown, unknown.mode(), "strobe").flash == "software"
    for start, end in (("5Hz", "25Hz"), ("slow", "fast")):       # ... and without anything to switch it is refused
        alone = parse_fixture(strobe_fixture(start, end, dimmer=False), "user/alone")
        alone.modes[0].channels.pop(1)                       # nor a colour
        with pytest.raises(FixtureError, match="cannot be patched as a strobe"):
            plan(alone, alone.mode(), "strobe")
    # rates beyond what a patch can express are cut, and the values with them: 0..100 Hz over 0..200 -> 0.1..50 Hz over 0..100
    wide = parse_fixture({"name": "W", "availableChannels": {"S": {"capabilities": [
        {"dmxRange": [0, 200], "type": "ShutterStrobe", "shutterEffect": "Strobe", "speedStart": "0Hz", "speedEnd": "100Hz"},
        {"dmxRange": [201, 255], "type": "NoFunction"}]}}, "modes": [{"name": "m", "channels": ["S"]}]}, "user/wide")
    channel = wide.modes[0].channels[0]
    assert channel is not None and strobe_range(channel) == (0, 100, 0.1, 50.0, True)
    lit = plan(slow, slow.mode(), "heat")                    # as a light: shutter open and colours full while it is on
    assert lit.channels[0] == {"role": "static", "offset": 0, "value": 4, "name": "Strobe"} and lit.channels[1]["value"] == 255


def test_reading_a_fixture_file_resolutions_defaults_switching_templates_and_what_is_not_read() -> None:
    data = {
        "name": "Odd", "categories": ["Other"],
        "availableChannels": {
            "Fog": {"fineChannelAliases": ["Fog fine"], "defaultValue": 32768,
                    "capabilities": [{"dmxRange": [0, 2559], "type": "NoFunction"},
                                     {"dmxRange": [2560, 65535], "type": "Fog", "fogOutputStart": "strong", "fogOutputEnd": "weak"}]},
            "Mode": {"defaultValue": "50%", "capabilities": [
                {"dmxRange": [0, 99], "type": "Effect", "effectName": "a", "switchChannels": {"Aux": "Speed A"}},
                {"dmxRange": [100, 255], "type": "Effect", "effectName": "b", "switchChannels": {"Aux": "Speed B"}}]},
            "Speed A": {"capability": {"type": "Speed", "speedStart": "slow", "speedEnd": "fast"}},
            "Speed B": {"name": "Other speed", "capability": {"type": "Speed", "speedStart": "fast", "speedEnd": "slow"}},
        },
        "templateChannels": {"Cell $pixelKey": {"capability": {"type": "Intensity"}}},
        "modes": [
            {"name": "Full", "shortName": "full", "channels": ["Fog", "Fog fine", None, "Mode", "Aux", "Cell 2"]},
            {"name": "Pixels", "shortName": "px", "channels": ["Fog", {"insert": "matrixChannels", "repeatFor": "eachPixelABC",
                                                                         "templateChannels": ["Cell $pixelKey"]}]},
            {"name": "Huge", "shortName": "huge", "channels": ["Fog"] * 33},
            {"name": "Broken", "shortName": "bad", "channels": ["Fog", "Nowhere"]},
        ],
    }
    model = parse_fixture(data, "user/odd")
    full = model.mode("full")
    fog, fine, unused, mode, aux, cell = full.channels
    assert fog is not None and [(c.lo, c.hi, c.type) for c in fog.capabilities] == [(0, 9, "NoFunction"), (10, 255, "Fog")]
    assert fog.default == 128 and level_range(fog, ("Fog",)) == (0, 255, 10)        # strong at the low end: full level = value 10
    assert fine is not None and fine.fine_of == "Fog" and unused is None
    assert mode is not None and mode.default == 128                                  # 50 %
    assert aux is not None and aux.name == "Other speed"                             # the mode channel rests in its second range
    assert cell is not None and cell.name == "Cell 2"
    assert [m.unsupported for m in model.modes] == ["", "a pixel matrix", "33 channels (at most 32)", "channel 'Nowhere' is not described"]
    entry = plan(model, full, "fog")
    assert entry.channels[0] == {"role": "level", "offset": 0, "min": 255, "max": 10, "name": "Fog"}
    assert entry.channels[2] == {"role": "static", "offset": 3, "value": 128, "when": "always", "name": "Mode"}
    assert [c["offset"] for c in entry.channels] == [0, 1, 3, 4, 5]                   # the unused slot is nobody's
    with pytest.raises(FixtureError, match="pixel matrix"):
        plan(model, model.mode("px"), "fog")
    with pytest.raises(FixtureError, match=r"has no mode 'x' \(it has: full, px, huge, bad\)"):
        model.mode("x")
    with pytest.raises(FixtureError, match="not a fixture file"):
        parse_fixture({"name": "x"}, "user/x")


def test_fixture_files_of_the_user_are_part_of_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "relay-4.json").write_text(json.dumps(pack(4)))
    (tmp_path / "loose.json").write_text(json.dumps(pack(2)))
    (tmp_path / "broken.json").write_text("{")
    (tmp_path / "manufacturers.json").write_text(json.dumps({"acme": {"name": "ACME Lighting"}}))
    lib = Library([tmp_path], snapshot=False)
    assert lib.keys() == ["acme/relay-4", "user/loose"] and lib.get("acme/relay-4").title == "ACME Lighting Relay Pack"
    assert lib.info()["own"] == 2 and "broken.json" in lib.info()["problems"][0]
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    source = tmp_path / "My Fan.json"
    source.write_text(json.dumps(pack(3)))
    assert fx_main(["fixtures", "library", "--add", str(source)]) == 0
    assert (tmp_path / "data" / "extended-4d-editor" / "fixtures" / "My Fan.json").exists()
    assert "user/My Fan" in Library().keys() and Library().get("user/my fan").modes[0].short == "3ch"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "x", "modes": [{"name": "m", "channels": [{"insert": "matrixChannels"}]}]}))
    assert fx_main(["fixtures", "library", "--add", str(bad)]) == 2


# ------------------------------------------------------------------------------ the patch, written
def test_a_patch_is_written_as_it_was_read_and_only_if_it_can_be_read_back(tmp_path: Path) -> None:
    for name in ("patch.example.toml", "patch.esp32.example.toml"):
        data = read_data(ROOT / "config" / "effects" / name)
        text = to_toml(data, "written by a test\nsecond line")
        assert text.startswith("# written by a test\n# second line\n") and tomllib.loads(text) == data
    odd = {"allow_raw_dmx": False, "output": {"protocol": "sacn", "refresh_hz": 30.0, "sacn": {"source_name": 'Hall "A" \\ täst\n', "unicast": ["10.0.0.5"]}},
           "types": {"fog": {"enabled": False, "master": 0.5}},
           "fixtures": [{"id": "f1", "effect": "fog", "address": 7, "label": "x", "match": {"slot": 2.0},
                         "channels": [{"role": "level", "offset": 0, "min": 6, "name": "Fog"}], "limits": {"max_on_s": 1e-05}}]}
    target = tmp_path / "venue.toml"
    write_data(target, odd, "a venue")
    assert read_data(target) == odd and load_patch(target).fixtures[0].channels[0].name == "Fog"
    before = target.read_text()
    with pytest.raises(PatchError, match="address must be an integer"):
        write_data(target, {**odd, "fixtures": [{**odd["fixtures"][0], "address": 600}]})
    assert target.read_text() == before and [p.name for p in tmp_path.iterdir()] == ["venue.toml"]


def test_a_strobe_whose_slowest_rate_is_above_its_limit_is_warned_about() -> None:
    entry = {"id": "s", "effect": "strobe", "channels": [{"role": "level"}, {"role": "rate", "offset": 1, "hz_min": 5.0, "hz_max": 20.0}]}
    assert "slowest flash rate (hz_min = 5) is above its limit of 3" in " ".join(patch_from_dict({"fixtures": [entry]}).warnings())
    assert not patch_from_dict({"fixtures": [{**entry, "flash": "software"}]}).warnings()
    assert not patch_from_dict({"fixtures": [{**entry, "limits": {"max_rate_hz": 6.0}}]}).warnings()[1:]     # only "above 3 Hz" then


# ------------------------------------------------------------------------------ the commands
def test_fixtures_commands_show_and_add_to_a_patch_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fx_main(["fixtures", "search", "--category", "Hazer"]) == 0 and "cameo/instant-hazer-1500-t-pro" in capsys.readouterr().out
    assert fx_main(["fixtures", "search", "zzzz"]) == 1
    assert fx_main(["fixtures", "search"]) == 0 and "Categories" in capsys.readouterr().out
    assert fx_main(["fixtures", "show", "cameo/instant-hazer-1500-t-pro", "--effect", "fog"]) == 0
    assert "-> driven by the effect: off 0, 6 … 255" in capsys.readouterr().out
    target = tmp_path / "venue.toml"
    assert fx_main(["example-patch", "-o", str(target)]) == 0
    comment = target.read_text().splitlines()[0]
    taken = {ch for i in load_patch(target).instances(["PLATFORM-01"]) if i.universe == 1 for ch in i.channels()}
    assert fx_main(["fixtures", "entry", "chauvet-dj/geyser-rgb", "--effect", "fog", "--patch", str(target)]) == 0
    out = capsys.readouterr().out
    patch = load_patch(target)
    added = patch.fixtures[-1]
    assert (added.id, added.model, added.mode, added.footprint) == ("fog1", "chauvet-dj/geyser-rgb", "8ch", 8)
    assert not taken & set(range(added.address, added.address + 8)) and f"channels {added.address}–{added.address + 7}" in out
    assert target.read_text().splitlines()[0] == comment and patch.problems(["PLATFORM-01"]) == []     # the file's comments are kept
    before = target.read_text()
    assert fx_main(["fixtures", "entry", "cameo/instant-air-1000-pro", "--effect", "wind", "--patch", str(target),
                    "--address", str(added.address)]) == 2
    assert "already used by fog1" in capsys.readouterr().err and target.read_text() == before
    assert fx_main(["fixtures", "entry", "cameo/instant-air-1000-pro", "--effect", "wind", "--id", "fan", "--address", "40"]) == 0
    assert tomllib.loads(capsys.readouterr().out)["fixtures"][0]["address"] == 40
    assert fx_main(["fixtures", "entry", "cameo/instant-air-1000-pro"]) == 2 and "--effect" in capsys.readouterr().err


# ------------------------------------------------------------------------------ is the venue ready?
@pytest.fixture
def port() -> int:
    return free_port()


@pytest.fixture
def nodes(port: int) -> Iterator[Callable[..., br.SimNode]]:
    made: list[br.SimNode] = []

    def make(index: int, **kw: Any) -> br.SimNode:
        node = br.SimNode(node_id=index + 1, host=HOSTS[index], port=port, **kw).start()
        made.append(node)
        return node

    yield make
    for n in made:
        n.stop()


def esp32_patch(port: int, tmp_path: Path, second_line: bool = False) -> Path:
    data = read_data(ROOT / "config" / "effects" / "patch.esp32.example.toml")
    data["output"]["esp32"] = {"port": port, "theatre": HOSTS[0], "platforms": {"PLATFORM-01": HOSTS[1], "PLATFORM-02": HOSTS[2]}}
    if second_line:
        data["fixtures"].append({"id": "far", "effect": "fog", "universe": 2, "address": 1})
    path = tmp_path / "venue.toml"
    write_data(path, data)
    return path


def texts(report: rd.Report, status: str) -> str:
    return "\n".join(i.text for i in report.items if i.status == status)


def test_boards_are_found_asked_timed_and_seen_to_go_dark(nodes, port: int, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    theatre, first, second = nodes(0, dmx_ports=2), nodes(1, actuators=True), nodes(2, actuators=True)
    found = rd.find_nodes(port, 0.6, targets=list(HOSTS))
    assert sorted(found) == sorted(HOSTS) and found[HOSTS[1]].has() == "a platform, 1 DMX line(s), a sound output"
    probes = rd.probe_nodes(list(HOSTS), port, count=10, interval_s=0.01)
    assert all(p.lost == 0 and len(p.rtts_ms) == 10 and 0 < p.median_ms < 50 for p in probes.values())
    said: list[str] = []
    report = rd.venue_ready(esp32_patch(port, tmp_path), PLATFORMS, progress=said.append, probes=5)
    assert report.ready and report.count(rd.FAIL) == 0, report.lines()
    ok = texts(report, rd.OK)
    assert ok.count("went dark by itself") == 3 and "0 of 5 answers lost; has 2 DMX line(s), a sound output; firmware 1.0" in ok
    assert theatre.dmx_timeouts == first.dmx_timeouts == second.dmx_timeouts == 1 and theatre.stops == 0
    assert max(max(frame) for _t, _p, frame in theatre.dmx_history) == 0          # nothing was switched on to find this out
    assert "signal-loss test of 127.0.0.1" in said and report.verdict().startswith("READY")
    assert report.lines()[0].startswith("Venue check 20") and report.to_dict()["ready"] is True


def test_what_makes_a_venue_not_ready(nodes, port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    theatre = nodes(0, dmx_ports=1)                  # one DMX line, where the patch has devices on two
    stopped = nodes(1, actuators=True)
    stopped.inhibited = True                         # its emergency stop is in; the third board is switched off
    report = rd.venue_ready(esp32_patch(port, tmp_path, second_line=True), PLATFORMS, probes=3, signal_loss=False)
    fails = texts(report, rd.FAIL)
    assert not report.ready and report.verdict().startswith("NOT READY: 3 problem(s)")
    assert f"{HOSTS[2]} (PLATFORM-02): no answer on UDP port {port}" in fails
    assert "has no DMX line 2, where the patch has the devices of theatre" in fails and "emergency stop is in" in fails
    assert "went dark" not in "\n".join(report.lines())
    # something else is sending: the test is left out and nothing is disturbed
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.sendto(br.build_dmx(0, 1, 0, 1, bytes([0, 0, 9])), (HOSTS[0], port))
    time.sleep(0.1)
    status, seen = rd.signal_loss_test(HOSTS[0], port, {0: bytes(8)})
    assert status == rd.INFO and "left out" in seen and theatre.channel(0, 3) == 9
    sender.close()
    live = rd.venue_ready(esp32_patch(port, tmp_path), PLATFORMS, probes=2, sending=True)
    assert "the Editor is sending to it (LIVE)" in texts(live, rd.INFO)
    # a board that keeps its line alive without a signal fails the test
    time.sleep(br.NODE_DMX_TIMEOUT_S + 0.3)
    monkeypatch.setattr(br, "NODE_DMX_TIMEOUT_S", 30.0)
    status, seen = rd.signal_loss_test(HOSTS[0], port, {0: bytes(8)}, send_s=0.2, limit_s=1.2)
    assert status == rd.FAIL and "still active 1.2 s after the frames stopped" in seen
    assert rd.signal_loss_test(HOSTS[2], port, {0: bytes(8)})[0] == rd.FAIL          # nobody there


def test_a_patch_with_errors_and_other_outputs(tmp_path: Path) -> None:
    missing = rd.venue_ready(tmp_path / "nowhere.toml", PLATFORMS)
    assert not missing.ready and "nowhere.toml" in missing.items[0].text
    twice = {"output": {"protocol": "sacn"}, "fixtures": [{"id": "a", "effect": "fog", "address": 1}, {"id": "b", "effect": "wind", "address": 1}]}
    report = rd.venue_ready(patch_from_dict(twice), PLATFORMS)
    assert "channel 1 already used by a" in texts(report, rd.FAIL) and "not looked at" in texts(report, rd.INFO)
    sacn = rd.venue_ready(patch_from_dict({"output": {"protocol": "sacn", "sacn": {"multicast": False, "unicast": ["127.0.0.1"]}},
                                           "fixtures": [{"id": "a", "effect": "wind", "address": 1}]}), PLATFORMS)
    assert sacn.ready and "sACN (E1.31): the output opens" in texts(sacn, rd.OK) and "hold the last look" in texts(sacn, rd.INFO)
    none = rd.venue_ready(patch_from_dict({"fixtures": [{"id": "a", "effect": "fog", "address": 1}]}), PLATFORMS)
    assert none.ready and "sends no DMX" in texts(none, rd.WARN) and "no activation limit" not in texts(none, rd.WARN)
    usb = rd.venue_ready(patch_from_dict({"output": {"protocol": "enttec-pro", "enttec": {"port": "/dev/nonexistent-dmx"}},
                                          "fixtures": [{"id": "a", "effect": "wind", "address": 1}]}), PLATFORMS)
    assert not usb.ready and "enttec-pro output cannot be opened" in texts(usb, rd.FAIL)


def test_art_net_nodes_that_answer_a_poll() -> None:
    node = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    node.bind(("127.0.0.1", 0))
    node.settimeout(2.0)

    def answer() -> None:
        try:
            packet, sender = node.recvfrom(64)
        except OSError:
            return
        if packet[:10] == b"Art-Net\x00\x00\x20":
            reply = bytearray(239)
            reply[:10] = b"Art-Net\x00\x00\x21"
            reply[26:26 + 9] = b"DMX node\x00"
            reply[44:44 + 16] = b"A long node name"
            reply[173] = 2
            node.sendto(bytes(reply), sender)
            node.sendto(b"Art-Net\x00\x00\x50" + bytes(20), sender)      # not an answer to a poll

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    found = rd.artnet_poll(["127.0.0.1"], node.getsockname()[1], seconds=0.6, bind_port=0)
    thread.join(2.0)
    node.close()
    assert found == [{"address": "127.0.0.1", "name": "DMX node", "long_name": "A long node name", "ports": 2}]
    assert rd.artnet_poll(["127.0.0.1"], free_port(), seconds=0.2, bind_port=0) == []


class FakeServer:
    """What the Control Server answers to the report's four questions."""

    def __init__(self) -> None:
        self.devices: dict[str, Any] = {
            "vr_players": [{"device_id": "VR-01", "enabled": True, "connection": "CONNECTED", "state": "READY", "latency_ms": 12.0,
                            "drift_ms": 3.0, "errors": []},
                           {"device_id": "VR-02", "enabled": True, "connection": "OFFLINE", "state": "DISCONNECTED", "errors": []},
                           {"device_id": "VR-03", "enabled": False, "connection": "OFFLINE", "state": "DISCONNECTED", "errors": []}],
            "motion_platforms": [{"device_id": "PLATFORM-01", "enabled": True, "connection": "CONNECTED", "state": "READY",
                                  "latency_ms": 2.0, "errors": []}],
        }
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def reply(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path == "/api/v1/auth/login" and body == {"username": "viewer", "password": "secret"}:
                    self.reply(200, {"token": "t0k"})
                else:
                    self.reply(401, {"detail": "no"})

            def do_GET(self) -> None:
                if self.path == "/api/v1/health":
                    self.reply(200, {"status": "ok", "product": "4D VR Theatre Control Server", "version": "1.0.0", "uptime_s": 7300.0,
                                     "devices_connected": 2})
                elif self.headers.get("Authorization") != "Bearer t0k":
                    self.reply(401, {"detail": "no"})
                elif self.path == "/api/v1/devices":
                    self.reply(200, outer.devices)
                elif self.path == "/api/v1/playback/state":
                    self.reply(200, {"timeline": {"state": "IDLE"}, "programme_name": None})
                else:
                    self.reply(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_the_control_servers_devices_are_part_of_the_report() -> None:
    server = FakeServer()
    patch = patch_from_dict({"fixtures": []})
    try:
        report = rd.venue_ready(patch, ["PLATFORM-01"], server_url=server.url, user="viewer", password="secret")
        lines = "\n".join(report.lines())
        assert "4D VR Theatre Control Server 1.0.0" in lines and "running for 2.0 h" in lines
        assert "1 of 2 headset(s) the server expects are connected - missing: VR-02" in texts(report, rd.WARN)   # not the disabled one
        assert "1 of 1 platform controller(s) the server expects are connected" in texts(report, rd.OK)
        assert report.ready and "show: idle" in lines and "VR-01: READY, 12 ms to the server" in lines
        unknown = rd.venue_ready(patch, PLATFORMS, server_url=server.url, user="viewer", password="secret")
        assert not unknown.ready and "PLATFORM-02 is not known to the server" in texts(unknown, rd.FAIL)
        server.devices["motion_platforms"] += [{"device_id": f"PLATFORM-{k:02d}", "enabled": True, "connection": "OFFLINE"} for k in range(3, 12)]
        others = rd.venue_ready(patch, ["PLATFORM-01"], server_url=server.url, user="viewer", password="secret")
        assert others.ready and "1 of 10 platform controller(s) the server expects are connected - missing: PLATFORM-03, PLATFORM-04, " \
            "PLATFORM-05, PLATFORM-06, PLATFORM-07, PLATFORM-08 and 3 more" in texts(others, rd.WARN)    # not this venue's: a warning
        del server.devices["motion_platforms"][1:]
        server.devices["vr_players"][0]["connection"] = "OFFLINE"
        nobody = rd.venue_ready(patch, ["PLATFORM-01"], server_url=server.url, user="viewer", password="secret")
        assert not nobody.ready and "0 of 2 headset(s) the server expects are connected" in texts(nobody, rd.FAIL)
        server.devices["vr_players"][0]["connection"] = "CONNECTED"
        server.devices["motion_platforms"][0].update(state="FAULT", errors=["actuator 3 following error"])
        server.devices["motion_platforms"].append({"device_id": "PLATFORM-02", "enabled": True, "connection": "OFFLINE"})
        server.devices["vr_players"][0]["latency_ms"] = 400.0
        report = rd.venue_ready(patch, PLATFORMS, server_url=server.url, user="viewer", password="secret")
        fails = texts(report, rd.FAIL)
        assert not report.ready and "1 of 2 platform controller(s) the server expects are connected - missing: PLATFORM-02" in fails
        assert "PLATFORM-01: FAULT, 2 ms to the server - actuator 3 following error" in fails
        assert "latency above 100 ms" in texts(report, rd.WARN)
        wrong = rd.venue_ready(patch, PLATFORMS, server_url=server.url, user="viewer", password="nope")
        assert "the user name or password was not accepted" in texts(wrong, rd.FAIL)
        anonymous = rd.venue_ready(patch, PLATFORMS, server_url=server.url)
        assert anonymous.ready and "with a server account" in texts(anonymous, rd.INFO)
    finally:
        server.close()
    gone = rd.venue_ready(patch, PLATFORMS, server_url=server.url)
    assert not gone.ready and "does not answer" in texts(gone, rd.FAIL)


def test_a_programme_is_checked_against_the_venue_and_the_command_says_ready_or_not(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = EffectsDocument(60.0)
    doc.new_track("fog").cues = [Cue(5.0, 30.0)]
    doc.new_track("snow").cues = [Cue(40.0, 5.0)]
    patch = patch_from_dict({"fixtures": [{"id": "fog", "effect": "fog", "address": 1}]})
    report = rd.venue_ready(patch, PLATFORMS, programme=doc)
    assert "no device for the effect 'snow'" in texts(report, rd.WARN) and "held back by its limits" in texts(report, rd.INFO)
    assert "1 device(s) play this programme, 1 of them shortened" in texts(report, rd.OK)
    venue = tmp_path / "venue.toml"
    write_data(venue, {"output": {"protocol": "sacn", "sacn": {"multicast": False, "unicast": ["127.0.0.1"]}},
                       "fixtures": [{"id": "fan", "effect": "wind", "address": 1}]})
    out = tmp_path / "report.txt"
    assert fx_main(["ready", "--patch", str(venue), "-q", "-o", str(out)]) == 0
    text = capsys.readouterr().out
    assert text.rstrip().endswith("READY") and out.read_text() == text
    assert fx_main(["ready", "--patch", str(venue), "--json"]) == 0 and json.loads(capsys.readouterr().out)["problems"] == 0
    write_data(venue, {"fixtures": [{"id": "fan", "effect": "wind", "address": 1}]})
    assert fx_main(["ready", "--patch", str(venue), "-q"]) == 0 and fx_main(["ready", "--patch", str(venue), "-q", "--strict"]) == 1
    assert fx_main(["ready", "--patch", str(venue), "-q", "--server", "http://127.0.0.1:9"]) == 1
    capsys.readouterr()
    assert fx_main(["ready", "--patch", str(venue), "--user", "viewer"]) == 2 and "--password-file" in capsys.readouterr().err


def test_boards_are_searched_from_the_command_line(nodes, port: int, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    nodes(0, dmx_ports=2, audio_channels=0)
    assert fx_main(["nodes", "--find", "--port", str(port), "--seconds", "0.5", HOSTS[0], HOSTS[1]]) == 0
    out = capsys.readouterr().out
    assert f"{HOSTS[0]}       node 1   has 2 DMX line(s); firmware 1.0; answers in" in out and HOSTS[1] + " " not in out.split("\n", 1)[1]
    assert fx_main(["nodes", "--find", "--port", str(free_port()), "--seconds", "0.5", HOSTS[0]]) == 1
