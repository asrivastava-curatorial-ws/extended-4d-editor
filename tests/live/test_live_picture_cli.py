"""Motion from a picture, sound as a source, the live window and the command line."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from fourdvr_live import cli
from fourdvr_live.config import LiveConfig
from fourdvr_live.engine import LiveEngine
from fourdvr_live.sample import LiveSample
from fourdvr_live.sim import SimSender
from fourdvr_live.sources import SourceError, create
from fourdvr_live.sources.screen import PictureMotion, ScreenSource
from fourdvr_live.sources.sound import SoundSource, SoundTap
from live_helpers import PROFILE, ROOT, ManualSource, Stepper, pan, texture, turned


# --------------------------------------------------------------------------------------------- picture
def test_a_panning_picture_gives_the_turning_rate_with_the_right_sign() -> None:
    tex = texture()
    for rate in (0.15, -0.15):                                   # rad/s: to the left, then to the right
        pm = PictureMotion("flat", hfov_deg=90.0, width=480)
        yaw = []
        for k in range(135):
            s = pm.step(turned(tex, rate * k / 30.0), k / 30.0)
            if s is not None and k > 125:
                yaw.append(s.rates[2])
                assert s.active and abs(s.rates[0]) < 0.02 and abs(s.rates[1]) < 0.02
        assert np.mean(yaw) == pytest.approx(rate, rel=0.08)     # nose-left is positive
        assert pm.cuts == 0 and pm.confidence > 0.6 and pm.points > 60


def test_a_cut_is_not_motion_and_a_black_screen_means_rest() -> None:
    a, b = texture(1), texture(2)
    pm = PictureMotion("flat", width=480)
    samples = []
    for k in range(240):
        if k < 90:
            frame = pan(a, 100.0 + 4.0 * k)
        elif k < 150:
            frame = pan(b, 900.0 - 4.0 * (k - 90))               # a different scene, moving the other way
        else:
            frame = np.zeros((360, 640, 3), np.uint8)            # a loading screen
        samples.append(pm.step(frame, k / 30.0))
    assert pm.cuts >= 1
    at_cut = samples[90]
    assert at_cut is not None and at_cut.rates == (0.0, 0.0, 0.0) and at_cut.accel == (0.0, 0.0, 0.0)
    assert samples[130].active and samples[130].rates[2] > 0.1   # type: ignore[union-attr]
    assert samples[170].active and not samples[239].active       # type: ignore[union-attr]
    assert all(np.isfinite([*s.accel, *s.rates]).all() for s in samples if s is not None)
    assert max(np.linalg.norm(s.accel) for s in samples if s is not None) <= 4.0 * pm.strength + 1e-6


def test_masked_parts_of_the_picture_do_not_count() -> None:
    tex = texture()
    hud = np.zeros((360, 640, 3), np.uint8)
    cv2.rectangle(hud, (0, 250), (640, 360), (40, 200, 240), -1)
    for i in range(12):
        cv2.putText(hud, str(i * 37), (20 + 50 * i, 320), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    plain, masked = PictureMotion("flat", width=480), PictureMotion("flat", width=480, masks=[(0.0, 0.69, 1.0, 0.31)])
    pts = []
    for k in range(40):
        frame = pan(tex, 100.0 + 5.0 * k)
        frame[250:] = hud[250:]                                  # a dashboard that does not move with the scene
        plain.step(frame, k / 30.0)
        masked.step(frame, k / 30.0)
        pts.append((plain.points, masked.points))
    assert masked._mask is not None and not masked._mask[340 * 270 // 360 :, :].any()
    assert masked.confidence > 0.6 and np.mean([p[1] for p in pts[5:]]) < np.mean([p[0] for p in pts[5:]])
    with pytest.raises(SourceError, match="fractions of the picture"):
        ScreenSource({"capture": "file", "file": "x.mp4", "mask": [[0, 0, 2, 1]]})


@pytest.fixture(scope="module")
def panning_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("video") / "pan.avi"
    tex = texture(3)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    assert writer.isOpened()
    for k in range(150):
        writer.write(pan(tex, 100.0 + 6.0 * k + 60.0 * math.sin(k / 9.0)))
    writer.release()
    return path


def test_screen_source_delays_motion_to_meet_the_delayed_picture(panning_video: Path) -> None:
    src = create({"type": "screen", "file": str(panning_video), "mode": "delay", "delay_s": 0.4, "motion_lead_s": 0.05})
    assert isinstance(src, ScreenSource) and not src.needs_gui and src.suggested_comfort is None
    got: list[tuple[float, LiveSample]] = []
    src.start(lambda sample, now: got.append((now, sample)))
    shown: list[tuple[float, bool]] = []
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and not src.ended:
        now = time.monotonic()
        shown.append((now, src.picture_at(now) is not None))
        time.sleep(0.02)
    st = src.status()
    src.stop()
    assert len(got) > 60 and st["mode"] == "delay" and st["delay_ms"] == 400 and st["analysis_ms"] > 0
    lags = np.array([now - sample.captured for now, sample in got[10:]])       # capture -> motion
    assert np.median(lags) == pytest.approx(0.4 - 0.05 - 0.16 - 0.05, abs=0.06)
    first_picture = next(t for t, has in shown if has)
    assert first_picture - shown[0][0] >= 0.35                  # nothing is shown before it is delay_s old
    assert src.set_delay(0.5) == 0.5 and src.set_delay(9.0) == 3.0


def test_screen_source_direct_mode_and_refusals(panning_video: Path, tmp_path: Path) -> None:
    src = create({"type": "screen", "file": str(panning_video), "mode": "direct"})
    assert src.suggested_comfort == 0.5 and src.delay_s == 0.0 and src.set_delay(1.0) == 0.0     # type: ignore[attr-defined]
    got: list[tuple[float, LiveSample]] = []
    src.start(lambda sample, now: got.append((now, sample)))
    time.sleep(1.5)
    src.stop()
    lags = np.array([now - sample.captured for now, sample in got[5:]])
    assert len(got) > 20 and np.median(lags) < 0.12             # as early as the analysis allows
    engine = LiveEngine(LiveConfig(source={"type": "screen", "file": str(panning_video), "mode": "direct"}, sound=False))
    assert engine.cueing.s.comfort == 0.5 and engine.cueing.s.input_lp_hz == 4.0
    for options, message in (({"capture": "tape"}, "capture must be"), ({"capture": "stream"}, "needs url"),
                             ({"capture": "file"}, "needs file"), ({"file": "x", "mode": "later"}, "mode must be"),
                             ({"file": "x", "fps": 500}, "fps must be"), ({"file": "x", "delay_s": 9}, "delay_s must be"),
                             ({"file": "x", "projection": "dome"}, "projection must be"), ({"file": "x", "colour": 1}, "unknown key")):
        with pytest.raises(SourceError, match=message):
            create({"type": "screen", **options})
    with pytest.raises(SourceError, match="cannot open"):
        create({"type": "screen", "file": str(tmp_path / "nothing.mp4")}).open()


# --------------------------------------------------------------------------------------------- sound
@pytest.fixture(scope="module")
def thumping_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sound") / "thumps.wav"
    rate = 16000
    t = np.arange(rate * 3) / rate
    x = 0.15 * np.sin(2 * np.pi * 60.0 * t) + 0.1 * np.sin(2 * np.pi * 2000.0 * t)
    for start in (1.0, 2.0):                                     # two bangs
        i = int(start * rate)
        x[i : i + 2400] += 0.7 * np.sin(2 * np.pi * 45.0 * t[:2400]) * np.exp(-t[:2400] / 0.05)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def test_sound_source_feeds_the_shakers_and_marks_thumps(thumping_wav: Path) -> None:
    src = create({"type": "sound", "file": str(thumping_wav), "kick": 5.0})
    assert isinstance(src, SoundSource) and src.feed is src.tap.feed
    got: list[LiveSample] = []
    played = []
    src.start(lambda sample, _now: got.append(sample))
    end = time.monotonic() + 3.3
    while time.monotonic() < end:
        played.append(src.feed.read(1, 960, 48000, 0.0))
        time.sleep(0.02)
    status = src.status()
    src.stop()
    y = np.concatenate(played)
    spectrum = np.abs(np.fft.rfft(y))
    freqs = np.fft.rfftfreq(len(y), 1 / 48000)
    assert spectrum[(freqs > 40) & (freqs < 70)].max() > 50 * spectrum[(freqs > 1900) & (freqs < 2100)].max()
    thumps = [s for s in got if s.impact > 0.0]
    assert 2 <= len(thumps) <= 4 and all(s.accel[2] < 0.0 and s.accel[0] == 0.0 for s in thumps)
    assert max(s.rumble for s in got) > 0.5 and status["device"] == "sound of the file"
    assert not got[-1].active or src.tap.ended                   # the file is over: rest


def test_sound_capture_starts_on_a_device_or_says_why_not() -> None:
    tap = SoundTap(backend="null")
    tap.start()
    time.sleep(0.25)
    assert tap.device_name == "null device" and tap.band.blocks > 0 and tap.band.level < 0.01     # silence in
    tap.stop()
    with pytest.raises(SourceError, match="cut-off"):
        SoundTap(cutoff_hz=5.0)
    with pytest.raises(SourceError, match="kick must be"):
        create({"type": "sound", "kick": 99})
    with pytest.raises(SourceError, match="no sound track"):
        SoundTap(file=__file__).start()


# --------------------------------------------------------------------------------------------- the window
def test_live_window_shows_the_show_and_takes_the_operators_keys(qapp, drive) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from fourdvr_live.window import LiveWindow, measure_delay, pattern_share
    from fourdvr_motion.platform_config import load_platform_config

    cfg = LiveConfig(platforms=[replace(load_platform_config(PROFILE), driver="simulation")], sound=False)
    engine = LiveEngine(cfg, source=ManualSource())
    st = Stepper(engine)
    st.step(n=300)
    st.step(lambda: drive.at(10.0).sample, n=500)
    win = LiveWindow(engine)
    win.resize(1200, 700)
    win.show()
    for _ in range(20):
        win._refresh()
        win._poll_status()
    image = win.grab().toImage()
    assert image.width() == 1200 and len({image.pixel(x, 30) for x in range(20, 500, 7)}) > 3      # the overlay is painted
    assert len(win._trace) >= 20 and win._status["state"] == "running" and len(win._status["ranges"]) == 6
    QTest.keyClick(win, Qt.Key.Key_Space)
    assert engine.guard.hold
    QTest.keyClick(win, Qt.Key.Key_Space)
    assert not engine.guard.hold
    gain = engine.cueing.s.gain
    QTest.keyClick(win, Qt.Key.Key_Plus)
    QTest.keyClick(win, Qt.Key.Key_Plus)
    QTest.keyClick(win, Qt.Key.Key_Minus)
    assert engine.cueing.s.gain == pytest.approx(gain + 0.05)
    QTest.keyClick(win, Qt.Key.Key_R)
    assert engine.recorder is not None
    st.step(lambda: drive.at(10.0).sample, n=50)
    engine.recorder = None                        # (not saved: the default folder is not used in this test)
    QTest.keyClick(win, Qt.Key.Key_H)
    assert not win.overlay
    engine.runtimes[0]._enter_fault("test")
    st.step(lambda: drive.at(10.0).sample, n=5)
    QTest.keyClick(win, Qt.Key.Key_E)
    st.step(lambda: drive.at(10.0).sample, n=400)
    assert engine.runtimes[0].following
    win.close()
    # the delay test: a pattern that turns white at 1.0 and black at 1.7, seen 80 ms later each time
    flips = [(1.0, True), (1.7, False), (2.4, True)]
    seen = [(t, 0.0 if (int((t - 1.08 + 1e-9) / 0.7) % 2 or t < 1.08) else 1.0) for t in np.arange(0.9, 3.0, 0.02)]
    delays = measure_delay(flips, seen)
    assert len(delays) == 3 and all(0.07 <= d <= 0.11 for d in delays)
    # the pattern in a window that covers an eighth of the captured picture: the same delays
    part = pattern_share([(t, 0.3 + 0.125 * luma) for t, luma in seen])
    assert measure_delay(flips, part) == pytest.approx(delays)
    assert pattern_share([(t, 0.3 + 0.01 * luma) for t, luma in seen]) == []          # hardly any change: not the pattern
    # a picture that never changes shows no flip at all - not "black, seen at once"
    assert measure_delay(flips, [(t, 0.0) for t in np.arange(0.5, 3.0, 0.02)]) == []
    assert measure_delay(flips, [(t, 1.0) for t in np.arange(0.5, 3.0, 0.02)]) == []
    # ... and one that is later than the next flip is not measured either
    late = [(t, 1.0 if int((t - 1.9) / 0.7) % 2 == 0 and t >= 1.9 else 0.0) for t in np.arange(0.5, 3.0, 0.02)]
    assert measure_delay(flips, late) == []


# --------------------------------------------------------------------------------------------- command line
def run(*argv: str) -> int:
    return cli.main(list(argv))


def test_cli_lists_explains_and_writes_senders(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert run("sources") == 0
    out = capsys.readouterr().out
    for name in ("open", "forza", "f1", "dirt", "pcars2", "assetto", "outsim", "beamng", "xplane", "flightgear", "gt7",
                 "screen", "sound", "replay", "demo"):
        assert f"\n{name:<11} " in "\n" + out
    assert run("sources", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 15 and next(s for s in listed if s["name"] == "gt7")["experimental"] is True
    assert run("example-config", "-o", str(tmp_path / "live.toml")) == 0 and "[source]" in (tmp_path / "live.toml").read_text()
    for kind, marker in (("godot", "extends Node3D"), ("unity", "MonoBehaviour"), ("python", "socket.SOCK_DGRAM"),
                         ("flightgear", "<PropertyList>")):
        capsys.readouterr()
        assert run("sender", kind) == 0
        text = capsys.readouterr().out
        assert marker in text and ('"4dlive"' in text or "4DFG1" in text or "4dlive" in text)
    assert run("sender", "godot", "-o", str(tmp_path)) == 0 and (tmp_path / "FourDLiveSender.gd").exists()


def test_cli_check_says_what_will_run_and_what_is_missing(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert run("check", "--source", "forza", "--platform", str(PROFILE), "--simulate", "--gain", "0.6", "--axis", "yaw=-1") == 0
    out = capsys.readouterr().out
    assert "Forza Data Out (UDP port 5300)" in out and "simulation" in out and "yaw x -1" in out and out.endswith("ok\n")
    assert run("check", "--source", "gt7") == 1
    out = capsys.readouterr().out
    assert "PROBLEM" in out and "needs host" in out
    assert run("check", "--source", "gt7", "--host", "192.168.1.9") == 0 and "experimental" in capsys.readouterr().out
    (tmp_path / "typo.toml").write_text('[source]\ntype = "forza"\nprot = 5300\n')
    assert run("check", "-c", str(tmp_path / "typo.toml")) == 1           # a mistyped setting is found by the check,
    out = capsys.readouterr().out                                         # not when the show starts
    assert "PROBLEM" in out and "unknown key(s) prot" in out and not out.endswith("ok\n")
    assert run("check", "--source", "replay", "--file", str(tmp_path / "none.4dlive")) == 1
    assert "PROBLEM" in capsys.readouterr().out
    for argv, message in ((["check"], "give a configuration"), (["check", "--source", "pong"], "unknown source"),
                          (["check", "--source", "forza", "--axis", "yaw"], "NAME=FACTOR"),
                          (["check", "--source", "forza", "--gain", "7"], "gain must be"),
                          (["check", "-c", str(tmp_path / "none.toml")], "none.toml"),
                          (["run", "--source", "screen", "--no-window"], "is done by the live window"),
                          (["monitor", "--source", "screen"], "is done by the live window"),
                          (["info", str(tmp_path / "none.4dlive")], "cannot be read")):
        assert run(*argv) == 1
        assert message in capsys.readouterr().err


def test_the_example_configurations_in_the_package_are_what_the_program_prints_and_accepts(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    from fourdvr_live.config import EXAMPLE, load_live_config

    assert (ROOT / "config" / "live.example.toml").read_text(encoding="utf-8") == EXAMPLE
    bench = ROOT / "config" / "live-bench.example.toml"
    assert run("check", "-c", str(bench)) == 0
    out = capsys.readouterr().out
    assert "BENCH-01" in out and "simulation" in out and out.endswith("ok\n")
    # every source it offers as a comment is a valid one: put each in place of the demonstration drive
    text = bench.read_text(encoding="utf-8")
    start, end = text.index('[source]\ntype = "demo"'), text.index("[cueing]")
    head, offered, tail = text[:start], text[start:end].split("# --- ")[1:], text[end:]
    kinds = []
    for number, block in enumerate(offered):
        lines = [line[2:] for line in block.splitlines()[1:] if line.startswith("# ")]
        assert lines[0] == "[source]"
        folder = tmp_path / str(number)
        folder.mkdir()
        (folder / "platforms").symlink_to(ROOT / "config" / "platforms")
        (folder / "live.toml").write_text(head + "\n".join(lines) + "\n\n" + tail, encoding="utf-8")
        cfg = load_live_config(folder / "live.toml")
        kinds.append(create(cfg.source).info.name)
        assert cfg.platforms[0].platform_id == "BENCH-01" and cfg.cueing.gain == 0.7
    assert kinds == ["forza", "screen", "sound"]


def test_cli_monitor_shows_a_simulated_game_in_plain_words_and_records_it(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """The real thing over UDP on this machine: the simulator plays Forza, the monitor listens."""
    sender = SimSender("forza", "127.0.0.1", 45300)
    stop = threading.Event()
    thread = threading.Thread(target=sender.run, args=(0.0, stop), daemon=True)
    thread.start()
    session = tmp_path / "forza.4dlive"
    try:
        assert run("monitor", "--source", "forza", "--port", "45300", "--seconds", "5", "--record", str(session)) == 0
    finally:
        stop.set()
        thread.join(timeout=2.0)
    out = capsys.readouterr().out
    assert "speeding up" in out and "Forza Data Out" in out and f"recorded: {session}" in out
    said = next(line for line in out.splitlines() if "speeding up" in line)           # the numbers behind the words
    assert re.search(r"speeding up.*\[accel \+\d\.\d [+-]\d\.\d [+-]\d\.\d m/s²  rates [+-]\d\.\d\d [+-]\d\.\d\d [+-]\d\.\d\d rad/s  "
                     r"\d+ km/h\]$", said), said
    assert "float64" not in out
    assert run("info", str(session)) == 0
    info = capsys.readouterr().out
    assert "Forza Data Out" in info and "live session" in info and "top speed" in info
    assert run("convert", str(session), "--platform", str(PROFILE), "--programme-id", "3") == 0
    assert "validation passed" in capsys.readouterr().out and session.with_suffix(".motion").exists()


def test_cli_run_with_a_simulated_platform_and_the_request_driven_formats(capsys: pytest.CaptureFixture[str]) -> None:
    """Assetto Corsa and GT7 only send when asked: the simulator answers the handshake / the heartbeat."""
    for name, port in (("assetto", "45996"), ("gt7", "45740")):
        sender = SimSender(name, port=int(port))
        stop = threading.Event()
        thread = threading.Thread(target=sender.run, args=(0.0, stop), daemon=True)
        thread.start()
        try:
            code = run("run", "--source", name, "--host", "127.0.0.1", "--port", port, "--platform", str(PROFILE),
                       "--simulate", "--no-sound", "--no-window", "--seconds", "5", "--status-interval", "1")
        finally:
            stop.set()
            thread.join(timeout=2.0)
        out = capsys.readouterr().out
        assert code == 0 and "PLATFORM-01" in out and "running" in out, out
        assert sender.sent > 100
