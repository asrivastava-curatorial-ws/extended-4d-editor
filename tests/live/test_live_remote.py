"""The show in a process of its own, with the live window as its client (fourdvr_live.remote)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from fourdvr_live.config import LiveConfig, load_live_config
from fourdvr_live.remote import RemoteEngine, RemoteSource, blank_status, from_wire, to_wire
from fourdvr_live.sample import AXES, LiveSample
from fourdvr_live.session import Session
from fourdvr_live.sources import SourceError, create
from live_helpers import PROFILE, texture, turned

DEMO = ["run", "--source", "demo", "--platform", str(PROFILE), "--simulate", "--gain", "0.8"]


def wait_for(engine: RemoteEngine, cond: Callable[[dict[str, Any]], bool], seconds: float = 12.0) -> dict[str, Any]:
    """Poll the status as the window does (which also tells the engine that its window is alive)."""
    end = time.monotonic() + seconds
    st = engine.status()
    while time.monotonic() < end:
        engine.alive()
        st = engine.status()
        if cond(st):
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out; the last status: {st}")


def playing(st: dict[str, Any]) -> bool:
    return st["state"] == "running" and bool(st["platforms"]) and st["platforms"][0]["phase"] == "PLAYING"


def test_samples_cross_the_wire_unchanged() -> None:
    assert to_wire(LiveSample()) == {}
    sample = LiveSample(accel=(1.5, -2.0, 0.25), rates=(0.01, -0.02, 0.3), attitude=(0.1, -0.05), speed=31.0, rpm=5400.0,
                        rumble=0.4, impact=0.9, airborne=True, active=False, effects={"rain": 0.7},
                        params={"scent": {"slot": 2.0}}, time=12.5, captured=1234.5)
    assert from_wire(to_wire(sample)) == sample
    assert from_wire({"accel": [1, 2, 3], "something from a newer version": 1}).accel == (1, 2, 3)
    blank = blank_status("x")
    assert set(blank["pose"]) == set(AXES) and blank["state"] == "starting" and blank["platforms"] == []


def test_the_show_runs_in_a_process_of_its_own_and_takes_the_operators_calls(tmp_path: Path) -> None:
    engine = RemoteEngine(DEMO, audio_backend="null", title="demo")
    assert engine.status()["state"] == "starting"                # before it runs the window has something to show
    assert engine.command[:1] == [sys.executable] and engine.command[-3:] == ["--no-window", "--control", "--null-audio"]
    engine.start()
    try:
        st = wait_for(engine, playing)
        assert st["gain"] == 0.8 and st["hold"] is False and len(st["ranges"]) == 6 and st["problem"] == ""
        assert st["source"]["title"] == "Built-in demonstration drive" and st["source"]["rate_hz"] > 30
        moving = wait_for(engine, lambda s: abs(s["pose"]["pitch"]) > 0.005)
        assert moving["envelope"] == 1.0
        # hold: said at once here, done there - the envelope goes to zero and the platform to neutral
        engine.hold(True)
        assert engine.status()["hold"] is True
        held = wait_for(engine, lambda s: s["envelope"] == 0.0 and s["hold"])
        assert held["state"].startswith("held")
        wait_for(engine, lambda s: max(abs(v) for v in s["pose"].values()) < 1e-3)
        engine.engage()
        wait_for(engine, lambda s: s["envelope"] == 1.0 and not s["hold"])
        assert engine.set_gain(5.0) == 2.0 and engine.status()["gain"] == 2.0
        engine.set_gain(1.25)
        wait_for(engine, lambda s: s["gain"] == 1.25)
        # recording
        engine.start_recording()
        wait_for(engine, lambda s: (s["recording_s"] or 0.0) >= 1.0)
        saved = engine.stop_recording(tmp_path / "lap.4dlive")
        assert saved == tmp_path / "lap.4dlive" and saved.exists()
        session = Session.load(saved)
        assert 0.9 <= session.duration_s <= 4.0 and np.abs(session.pose).max() > 0.002
        assert wait_for(engine, lambda s: s["recording_s"] is None)["max_late_ms"] < 150.0
    finally:
        proc = engine._proc
        report = engine.stop()
    assert proc is not None and proc.returncode == 0
    assert report[0].startswith("source: ") and any(line.startswith("PLATFORM-01: STOPPED") for line in report), report
    assert engine._proc is None


def test_when_the_window_is_gone_the_show_ends_by_itself_at_rest() -> None:
    engine = RemoteEngine(DEMO, audio_backend="null")
    engine.start()
    proc = engine._proc
    assert proc is not None and proc.stdin is not None
    try:
        wait_for(engine, playing)
        wait_for(engine, lambda s: abs(s["pose"]["pitch"]) > 0.005)
        proc.stdin.close()                        # what the engine sees when the window's process dies
        proc.wait(15.0)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    end = time.monotonic() + 2.0
    while not engine.report and time.monotonic() < end:
        time.sleep(0.02)
    assert any(line.startswith("PLATFORM-01: STOPPED") for line in engine.report), engine.report     # faded out, parked
    assert engine.status()["state"] == "ended"
    engine.stop(1.0)


def test_a_window_that_stops_answering_holds_the_show_until_it_answers_again() -> None:
    engine = RemoteEngine(DEMO, audio_backend="null")
    engine.start()
    try:
        wait_for(engine, playing)
        time.sleep(3.6)                           # the window hangs: no "alive" for longer than the engine waits
        st = engine.status()
        assert st["hold"] is True and st["envelope"] < 1.0, st
        wait_for(engine, lambda s: not s["hold"], 3.0)           # it answers again: the show goes on
        wait_for(engine, lambda s: s["envelope"] == 1.0)
    finally:
        engine.stop()


def test_an_engine_that_cannot_start_says_why(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="unknown source"):
        RemoteEngine(["run", "--source", "pong"]).start()
    (tmp_path / "live.toml").write_text('[source]\ntype = "forza"\nprot = 1\n')
    with pytest.raises(SourceError, match="unknown key"):
        RemoteEngine(["run", "-c", str(tmp_path / "live.toml")]).start()


@pytest.fixture(scope="module")
def turning_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Six seconds of a camera turning steadily to the right at 0.3 rad/s (a true rotation)."""
    path = tmp_path_factory.mktemp("remote") / "turning.avi"
    tex = texture(3)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    for k in range(180):
        writer.write(turned(tex, -0.3 * k / 30.0))
    writer.release()
    return path


def test_a_picture_source_lives_with_the_window_and_its_samples_drive_the_show_elsewhere(turning_video: Path, tmp_path: Path) -> None:
    (tmp_path / "live.toml").write_text(
        f'[[platforms]]\nconfig = "{PROFILE}"\ndriver = "simulation"\n'
        f'[source]\ntype = "screen"\nfile = "{turning_video}"\nloop = true\nmode = "direct"\n[cueing]\ngain = 1.0\n')
    cfg = load_live_config(tmp_path / "live.toml")
    picture = create(cfg.source)
    engine = RemoteEngine(["run", "-c", str(tmp_path / "live.toml")], picture, audio_backend="null")
    assert engine.command[-2:] == ["--remote-source", "--null-audio"]
    engine.start()
    try:
        st = wait_for(engine, lambda s: playing(s) and "input" in s and s["input"]["rates"][2] < -0.15, 15.0)
        assert st["source"]["frames"] > 10 and st["source"]["capture"] == "file" and st["source"]["mode"] == "direct"
        assert -0.4 < st["input"]["rates"][2] < -0.15            # the picture turns right at 0.3 rad/s
        assert st["delay_ms"] is not None and 0.0 <= st["delay_ms"] < 400.0        # the capture time crossed the wire
        assert "turning right" in " ".join(st["input"]["says"]) or "nose turning right" in " ".join(st["input"]["says"])
    finally:
        report = engine.stop()
    assert any("STOPPED" in line for line in report) and not picture.running


def test_the_engines_end_of_a_picture_source() -> None:
    direct = RemoteSource({"type": "screen", "mode": "direct"})
    assert direct.suggested_comfort == 0.5 and direct.suggested_input_lp_hz == 4.0 and direct.feed is None
    delayed = RemoteSource({"type": "screen", "mode": "delay", "delay_s": 0.4, "sound_device": "Monitor"})
    assert delayed.suggested_comfort is None and delayed.feed is not None
    before = delayed.feed.delay_s
    delayed.set_delay(0.5)                        # the operator's ] key: the shakers' sound stays with the picture
    assert delayed.feed.delay_s == pytest.approx(before + 0.1) and delayed.delay_s == 0.5
    got: list[LiveSample] = []
    direct.start(lambda sample, _now: got.append(sample))
    direct.offer(LiveSample(rates=(0.0, 0.0, 0.2)))
    assert got[0].rates[2] == 0.2 and direct.samples == 1 and direct.status()["title"].startswith("Screen")
    direct.stop()


def test_the_window_opens_on_a_show_in_its_own_process(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """`fourdvr live run --window` as the program does it: open_window, the operator's keys, close."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from fourdvr_live.window import open_window

    def spin(cond: Callable[[dict[str, Any]], bool], seconds: float = 12.0) -> dict[str, Any]:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()                  # the window's own timers poll the status and say "alive"
            if cond(window._status):
                return window._status
            time.sleep(0.01)
        raise AssertionError(f"timed out; the last status: {window._status}")

    cfg = LiveConfig()
    cfg.source = {"type": "demo"}
    engine, window = open_window(cfg, DEMO, record=str(tmp_path / "show.4dlive"), audio_backend="null")
    try:
        assert isinstance(engine, RemoteEngine) and window.isVisible() and not window.has_picture
        spin(playing)
        spin(lambda s: (s["recording_s"] or 0.0) > 0.5)          # --record: recording from the start
        assert len(window._trace) > 20
        QTest.keyClick(window, Qt.Key.Key_Space)
        spin(lambda s: s["hold"] and s["envelope"] == 0.0)
        QTest.keyClick(window, Qt.Key.Key_Space)
        spin(lambda s: not s["hold"] and s["envelope"] == 1.0)
        QTest.keyClick(window, Qt.Key.Key_Plus)
        spin(lambda s: s["gain"] == pytest.approx(0.85))
        QTest.keyClick(window, Qt.Key.Key_R)                     # stop the recording: saved where --record said
        assert window.saved == tmp_path / "show.4dlive" and window.saved.exists()
        image = window.grab().toImage()
        assert len({image.pixel(x, 30) for x in range(20, 500, 7)}) > 3
    finally:
        window.close()
        report = engine.stop()
    assert any(line.startswith("PLATFORM-01: STOPPED") for line in report)
