"""The unified ``fourdvr`` command, the effects tools and the isolation of the show service."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from fourdvr_fx import BassTrack, Cue, EffectsDocument
from fourdvr_fx.cli import build_parser, cmd_blackout, cmd_fire, cmd_play
from fourdvr_fx.dmx import CaptureOutput, SacnOutput
from fourdvr_fx.embed import read_effects, write_effects
from fourdvr_fx.patch import load_patch
from fourdvr_fx.preflight import preflight
from fourdvr_motion import motionfile
from fourdvr_studio import __version__
from fourdvr_studio.cli import main

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "config" / "effects" / "patch.example.toml"
SIM = ROOT / "config" / "platforms" / "simulation-6dof.toml"


def make_motion(path: Path, duration: float = 4.0) -> str:
    n = motionfile.expected_sample_count(duration, 100.0)
    t = np.arange(n) / 100.0
    samples = np.zeros((n, 6))
    samples[:, 2] = 0.01 * np.sin(np.pi * t / duration) ** 2
    header = motionfile.build_header(programme_id=3, duration_s=duration, sample_rate_hz=100.0,
                                     video={"sha256": "a" * 64, "filename": "demo.mp4"}, generation={"generator": "tests"})
    return motionfile.write_motion(path, header, samples)


def make_effects(duration: float = 4.0) -> EffectsDocument:
    doc = EffectsDocument(duration)
    doc.new_track("wind").points = [(0.0, 0.0), (0.5, 1.0), (duration, 1.0)]
    doc.new_track("water").cues = [Cue(1.0, 0.4)]
    doc.new_track("fog").cues = [Cue(0.5, 1.0)]
    doc.new_track("scent").cues = [Cue(2.0, 1.0, params={"slot": 2})]
    return doc


def test_dispatch_version_help_and_unknown(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["version"]) == 0 and capsys.readouterr().out.strip() == __version__
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "fourdvr controller" in out and "fourdvr fx" in out and "fourdvr extract" in out
    assert main(["frobnicate"]) == 2 and "unknown command" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["controller", "--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.strip() == __version__
    with pytest.raises(SystemExit):
        main(["fx", "--help"])
    assert "fourdvr fx" in capsys.readouterr().out
    assert main(["extract", "profiles"]) == 0 and "example-electric-6dof" in capsys.readouterr().out


def test_fx_files_round_trip(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    motion = tmp_path / "p.motion"
    make_motion(motion)
    original = motion.read_bytes()
    assert main(["fx", "info", str(motion)]) == 0 and "motion-only" in capsys.readouterr().out
    fx = tmp_path / "p.fx.json"
    doc = make_effects()
    doc.bass = BassTrack.from_float(0.2 * np.sin(np.arange(8000) / 20.0), 2000)
    write_effects(fx, doc)
    assert main(["fx", "embed", str(motion), str(fx)]) == 0
    assert "upload it to the Control Server again" in capsys.readouterr().out
    assert motion.read_bytes() != original
    assert np.array_equal(motionfile.read_motion(motion).samples, motionfile.read_motion(tmp_path / "p.motion").samples)
    assert main(["fx", "info", str(motion)]) == 0
    out = capsys.readouterr().out
    assert "4 track(s)" in out and "Water spray" in out and "slot" not in out.split("Scent")[0] and "bass track: 4.00 s" in out
    assert main(["controller", "motion-info", str(motion)]) == 0
    out = capsys.readouterr().out
    assert "programme 3" in out and "Seat vibration" not in out and "Fog" in out
    back = tmp_path / "back.fx.json"
    assert main(["fx", "extract", str(motion), "-o", str(back)]) == 0
    assert read_effects(back).to_dict() == doc.to_dict()
    assert main(["fx", "info", str(back), "--json"]) == 0 and '"data"' not in capsys.readouterr().out
    assert main(["fx", "strip", str(motion)]) == 0
    assert motion.read_bytes() == original                                   # byte-identical motion-only file again
    short = EffectsDocument(1.0)
    short.new_track("water").cues = [Cue(0.1, 0.2)]
    write_effects(fx, short)
    assert main(["fx", "embed", str(motion), str(fx)]) == 2 and "use --force" in capsys.readouterr().err
    assert main(["fx", "embed", str(motion), str(fx), "--force", "-o", str(tmp_path / "forced.motion")]) == 0
    assert main(["fx", "info", str(tmp_path / "nothing.motion")]) == 2


def test_fx_patch_tools(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["fx", "check-patch", str(PATCH), "--platforms", "PLATFORM-01,PLATFORM-02"]) == 0
    out = capsys.readouterr().out
    assert "wind@PLATFORM-02" in out and "ch 9" in out and "≤ 3 flashes/s" in out and "patch OK" in out
    copy = tmp_path / "venue.toml"
    assert main(["fx", "example-patch", "-o", str(copy)]) == 0 and copy.read_text() == PATCH.read_text()
    assert main(["fx", "example-patch", "-o", str(copy)]) == 2                # no silent overwrite
    assert main(["fx", "example-patch"]) == 0 and "[[fixtures]]" in capsys.readouterr().out
    bad = tmp_path / "bad.toml"
    bad.write_text(PATCH.read_text().replace('id = "strobe-front"\neffect = "strobe"\nscope = "theatre"\nuniverse = 1\naddress = 10',
                                             'id = "strobe-front"\neffect = "strobe"\nscope = "theatre"\nuniverse = 1\naddress = 1'))
    assert main(["fx", "check-patch", str(bad)]) == 1 and "already used by" in capsys.readouterr().out
    fast = tmp_path / "fast.toml"
    fast.write_text(PATCH.read_text().replace("max_rate_hz = 3.0", "max_rate_hz = 8.0"))
    assert main(["fx", "check-patch", str(fast)]) == 0 and "photosensitivity" in capsys.readouterr().out


def test_preflight_reports_what_the_venue_limits(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    doc = EffectsDocument(60.0)
    doc.new_track("fog").cues = [Cue(5.0, 9.0), Cue(20.0, 2.0), Cue(40.0, 2.0)]   # venue: on <= 4 s, off >= 10 s
    doc.new_track("water").cues = [Cue(10.0, 0.5)]
    doc.new_track("bubbles").cues = [Cue(3.0, 2.0)]                                # no such fixture here
    doc.new_track("strobe").cues = [Cue(30.0, 1.0, params={"rate_hz": 10.0})]
    doc.bass = BassTrack.from_float(np.zeros(120000), 2000)
    report = preflight(doc, load_patch(PATCH), ["PLATFORM-01"])
    fog = next(f for f in report.fixtures if f.key == "fog-main")
    assert fog.requested_s == pytest.approx(13.0, abs=0.1) and fog.output_s == pytest.approx(8.0, abs=0.1)
    assert list(fog.reasons) == ["maximum on-time"] and fog.held_s == pytest.approx(5.0, abs=0.1)
    t0, t1, why = fog.intervals[0]
    assert t0 == pytest.approx(9.0, abs=0.06) and t1 == pytest.approx(14.0, abs=0.06) and why == "maximum on-time"
    water = next(f for f in report.fixtures if f.key == "water@PLATFORM-01")
    assert water.held_s == 0 and water.output_s == pytest.approx(0.5, abs=0.06)
    assert report.unpatched == ["bubbles"] and not report.clean()
    assert any("bass track" in n for n in report.notes) and any("10 flashes/s" in n for n in report.notes)
    fx = tmp_path / "show.fx.json"
    write_effects(fx, doc)
    assert main(["fx", "check", str(fx), "--patch", str(PATCH)]) == 0
    out = capsys.readouterr().out
    assert "LIMITED: maximum on-time 5.0 s" in out and "00:09.00 - 00:14.00" in out and "no fixture for effect type 'bubbles'" in out
    assert main(["fx", "check", str(fx), "--patch", str(PATCH), "--strict"]) == 1


def _args(*argv: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(list(argv))


def test_fx_play_fire_and_blackout(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    fx = tmp_path / "show.fx.json"
    write_effects(fx, make_effects(1.6))
    cap = CaptureOutput(history=10000)
    start = time.monotonic()
    assert cmd_play(_args("play", str(fx), "--patch", str(PATCH), "--platforms", "PLATFORM-01,PLATFORM-02", "--every", "0.5"), cap) == 0
    assert 1.6 < time.monotonic() - start < 3.5
    out = capsys.readouterr().out
    assert "wind=100%" in out and "DMX frames sent, 0 output error(s)" in out
    water = [t for t, u, d in cap.history if u == 2 and d[1] == 255 and d[9] == 255]     # both platforms, same frames
    assert water and max(water) - min(water) == pytest.approx(0.4, abs=0.08)
    assert max(cap.latest[1]) == 0 and max(cap.latest[2]) == 0 and not cap.is_open         # dark and closed afterwards
    fog = [t for t, u, d in cap.history if u == 1 and d[0] == 255]                         # cue 0.5-1.5 s, sent 1 s early
    first = min(t for t, u, d in cap.history if u == 2 and d[0] > 0)                       # wind marks the start
    assert fog and min(fog) - first < 0.15 and max(fog) - min(fog) == pytest.approx(0.5, abs=0.1)
    cap = CaptureOutput(history=1000)
    assert cmd_fire(_args("fire", "air@PLATFORM-01", "--patch", str(PATCH), "--seconds", "0.3"), cap) == 0
    on = [t for t, u, d in cap.history if u == 2 and d[2] == 255]
    assert on and 0.2 < max(on) - min(on) < 0.5 and max(cap.latest[2]) == 0
    with pytest.raises(ValueError, match="unknown fixture 'nope'"):
        cmd_fire(_args("fire", "nope", "--patch", str(PATCH)), CaptureOutput())
    cap = CaptureOutput()
    assert cmd_blackout(_args("blackout", "--patch", str(PATCH)), cap) == 0 and set(cap.latest) == {1, 2}
    assert main(["fx", "play", str(fx), "--patch", str(PATCH), "--start", "9"]) == 2
    assert main(["fx", "devices"]) == 0 and "audio" in capsys.readouterr().out


def test_fx_monitor_shows_what_arrives(capsys) -> None:  # type: ignore[no-untyped-def]
    port = 45568
    def send() -> None:
        out = SacnOutput(source_name="test sender", multicast=False, unicast=["127.0.0.1"], port=port)
        out.open()
        time.sleep(0.4)
        for value in (0, 128, 255):
            data = bytearray(512)
            data[4] = value
            out.send(7, bytes(data))
            time.sleep(0.15)
        out.close()

    thread = threading.Thread(target=send)
    thread.start()
    code = main(["fx", "monitor", "--port", str(port), "--no-multicast", "-u", "7", "--seconds", "1.3"])
    thread.join()
    out = capsys.readouterr().out
    assert code == 0 and "U7 from 'test sender'" in out and "5=128" in out and "5=255" in out


def test_check_config_reports_the_effects_setup(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "sim.toml").write_text(SIM.read_text())
    base = f'[controller]\nserver_url = "ws://127.0.0.1:1/ws/v1/device"\ncache_dir = "{tmp_path / "cache"}"\n'
    base += f'[[platforms]]\nconfig = "sim.toml"\nid = "PLATFORM-01"\ndevice_key = "{"k" * 40}"\n'
    cfg = tmp_path / "controller.toml"
    cfg.write_text(base)
    assert main(["controller", "check-config", "-c", str(cfg)]) == 0
    assert "effects: not configured - motion only" in capsys.readouterr().out
    cfg.write_text(base + f'[effects]\npatch = "{PATCH}"\n')
    assert main(["controller", "check-config", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "fog-main" in out and "wind@PLATFORM-01" in out and "configuration OK" in out
    cfg.write_text(base + '[effects]\npatch = "missing.toml"\n')
    assert main(["controller", "check-config", "-c", str(cfg)]) == 1
    assert "MOTION ONLY" in capsys.readouterr().out
    cfg.write_text(base + f'[effects]\npatch = "{PATCH}"\nreference_platform = "PLATFORM-09"\n')
    assert main(["controller", "check-config", "-c", str(cfg)]) == 1


def test_show_service_does_not_load_the_desktop_or_analysis_stack(tmp_path: Path) -> None:
    """`fourdvr controller ...` must run on a headless show PC: no Qt, OpenCV, SciPy, plotting, video codecs."""
    (tmp_path / "sim.toml").write_text(SIM.read_text())
    cfg = tmp_path / "controller.toml"
    cfg.write_text(f'[controller]\nserver_url = "ws://127.0.0.1:1/ws/v1/device"\ncache_dir = "{tmp_path / "cache"}"\n'
                   f'[[platforms]]\nconfig = "sim.toml"\nid = "PLATFORM-01"\ndevice_key = "{"k" * 40}"\n'
                   f'[effects]\npatch = "{PATCH}"\n')
    code = (
        "import sys\n"
        "from fourdvr_studio.cli import main\n"
        f"assert main(['controller', 'check-config', '-c', r'{cfg}']) == 0\n"
        "from fourdvr_motion.app import ControllerApp\n"
        "from fourdvr_motion.config import load_controller_config\n"
        "from fourdvr_fx.dmx import CaptureOutput\n"
        f"app = ControllerApp(load_controller_config(r'{cfg}'), effects_output=CaptureOutput())\n"
        "assert app.effects is not None and app.effects.service is not None\n"
        "heavy = [m for m in ('PySide6', 'cv2', 'scipy', 'pyqtgraph', 'av', 'miniaudio', 'fourdvr_extractor') if m in sys.modules]\n"
        "print('HEAVY', heavy)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "HEAVY []" in result.stdout


# ------------------------------------------------------------------------------ effects through ESP32 nodes
def test_esp32_patch_example_and_node_tools(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import socket

    from fourdvr_fx.bridge import SimNode
    from fourdvr_fx.patch import load_patch

    example = ROOT / "config" / "effects" / "patch.esp32.example.toml"
    assert example.read_text() == (ROOT / "src" / "fourdvr_fx" / "examples" / "patch.esp32.example.toml").read_text()
    patch = load_patch(example)
    ids = ["PLATFORM-01", "PLATFORM-02"]
    assert patch.problems(ids) == [] and patch.warnings() == [] and {"rain", "heat", "bubbles", "snow"} <= set(patch.effects())
    assert main(["fx", "check-patch", str(example), "--platforms", ",".join(ids)]) == 0
    out = capsys.readouterr().out
    assert "output esp32" in out and "theatre @ 192.168.10.101     ch 1 " in out and "PLATFORM-02 @ 192.168.10.102 ch 34" in out
    assert "ESP32 audio: bass on channel(s) [1, 2]" in out and "left out, not an error" in out and "patch OK" in out
    assert main(["fx", "example-patch", "--esp32"]) == 0 and capsys.readouterr().out == example.read_text()
    # the same venue on this PC: two simulated ESP32s, one with a DMX line and a shaker, one with a shaker only
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    try:
        first = SimNode(1, dmx_ports=1, audio_channels=2, actuators=True, host="127.0.0.1", port=port).start()
        second = SimNode(2, dmx_ports=0, audio_channels=1, actuators=True, host="127.0.0.2", port=port).start()
    except OSError:
        pytest.skip("this system has no 127.0.0.2 loopback address")
    local = tmp_path / "venue.toml"
    local.write_text(example.read_text().replace("192.168.10.101", "127.0.0.1").replace("192.168.10.102", "127.0.0.2")
                     .replace("port = 47002 ", f"port = {port}  "))
    try:
        assert main(["fx", "nodes", "--patch", str(local), "--platforms", ",".join(ids), "--seconds", "1.5"]) == 0
        out = capsys.readouterr().out
        assert "127.0.0.1        node 1, firmware 1.0: platform, 1 DMX line(s), audio 2 channel(s) - PLATFORM-01 (line 1: theatre + PLATFORM-01)" in out
        assert "127.0.0.2        node 2, firmware 1.0: platform, audio 1 channel(s) - PLATFORM-02" in out
        assert main(["fx", "nodes", "127.0.0.3", "--port", str(port), "--seconds", "0.6"]) == 1
        assert "no answer" in capsys.readouterr().out
        # a programme played to the venue: the fog cue reaches the theatre line, platform 2's devices are left out
        doc = EffectsDocument(4.0)
        doc.new_track("fog").cues = [Cue(0.2, 2.0)]
        doc.new_track("heat").cues = [Cue(0.2, 2.0)]
        fx_file = tmp_path / "show.fx.json"
        write_effects(fx_file, doc)
        assert main(["fx", "play", str(fx_file), "--patch", str(local), "--platforms", ",".join(ids), "--end", "2.4", "-q"]) == 0
        lit = [frame for _t, _port, frame in first.dmx_history if frame[0] == 255]
        assert lit and any(frame[33] == round(255 * 0.8) for frame in lit)        # fog; heat at its 80 % ceiling on platform 1
        assert first.channel(0, 1) == 0 and first.stops >= 1 and not second.dmx_history and second.invalid == 0
    finally:
        first.stop()
        second.stop()
    assert main(["fx", "test-vectors", "-o", str(tmp_path / "v.txt")]) == 0
    stored = ROOT / "firmware" / "esp32-actuator-bridge" / "test" / "fx_vectors.txt"
    assert (tmp_path / "v.txt").read_text() == stored.read_text()


def test_a_reader_that_stops_reading_is_not_an_error(tmp_path: Path) -> None:
    """`fourdvr ... | head -1` and `| grep -q`: the output's reader goes away early. No error message, exit 0."""
    import os
    import subprocess
    import sys

    doc = EffectsDocument(60.0)
    doc.new_track("rain").cues = [Cue(float(t), 1.0) for t in range(0, 50, 2)]
    fx_file = tmp_path / "show.fx.json"
    write_effects(fx_file, doc)
    for argv in (["--help"], ["fx", "example-patch"], ["fx", "info", str(fx_file), "--json"],
                 ["controller", "check-platform", str(ROOT / "config/platforms/simulation-6dof.toml")], ["extract", "profiles"]):
        code = f"import sys; from fourdvr_studio.cli import main; raise SystemExit(main({argv!r}))"
        r, w = os.pipe()
        os.close(r)                                                   # nobody reads
        try:
            p = subprocess.run([sys.executable, "-c", code], stdout=w, stderr=subprocess.PIPE, text=True, timeout=120)
        finally:
            os.close(w)
        assert p.returncode == 0 and p.stderr.strip() == "", (argv, p.returncode, p.stderr)
