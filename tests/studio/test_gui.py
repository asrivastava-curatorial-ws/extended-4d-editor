"""The Editor window end to end (Qt offscreen): one-step analysis, the three lane groups and their options,
editing, undo, the limit check, live output (DMX capture, sound, ESP32 boards), projects."""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication

from fourdvr_fx import Cue
from fourdvr_fx import bridge as br
from fourdvr_fx.dmx import CaptureOutput
from fourdvr_fx.embed import read_effects
from fourdvr_motion import motionfile
from fourdvr_studio.gui import theme
from fourdvr_studio.gui.main_window import StudioWindow

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "config" / "effects" / "patch.example.toml"
AXES = ("surge", "sway", "heave", "roll", "pitch", "yaw")


@pytest.fixture(scope="module")
def app():  # type: ignore[no-untyped-def]
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


def wait(app, cond, timeout: float = 90.0) -> None:  # type: ignore[no-untyped-def]
    start = time.time()
    while not cond() and time.time() - start < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert cond(), "timed out"


def spin(app, seconds: float) -> None:  # type: ignore[no-untyped-def]
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def new_window() -> StudioWindow:
    win = StudioWindow()
    win.ask = lambda *_: True                    # no modal questions in tests
    win.audio_backend = "null"
    win.resize(1440, 900)
    win.show()
    return win


@pytest.fixture(scope="module")
def studio(app, media):  # type: ignore[no-untyped-def]
    """A window with the test video analysed, motion generated, processed and validated."""
    win = new_window()
    win.open_video(str(media["stereo"]))
    wait(app, lambda: win.project.video is not None and win._thread is None)
    assert win.in_proj.currentText() == "360" and win.lbl_mode.text() == "video project"
    win.start_analysis()
    wait(app, lambda: win.project.analysis is not None and win._thread is None)
    assert win._guarded(win.do_generate) and win._guarded(win.do_process) and win._guarded(win.do_validate)
    yield win
    win.dirty = False
    win.close()


def test_window_starts_empty_with_the_bundled_font(app) -> None:  # type: ignore[no-untyped-def]
    assert theme.load_fonts() == "JetBrains Mono" and app.font().family() == "JetBrains Mono"
    win = new_window()
    assert win.project.mode == "empty" and not win.btn_analyse.isEnabled() and not win.btn_export.isEnabled()
    assert not win.btn_check.isEnabled() and not win.btn_add_track.isEnabled() and "open a video" in win.lbl_next.text()
    assert "Extended 4D Editor 3." in win.windowTitle() and "Open a video" in win.lbl_scene.text()
    assert win.timeline.lanes == [] and "Sound-only preview" in win.lbl_patch.text() and win.insp_title.text() == "Nothing selected"
    win.analyse_all()                                                              # nothing to analyse, check or update: no error
    win.check_all()
    win.update_motion()
    assert win._thread is None and win.txt_check.toPlainText() == ""
    win.toggle_play()                                                              # nothing to play: stays stopped
    win.add_track("water")
    win.suggest_effects()
    win.insert_gesture("jolt")
    assert not win.playing and not win.project.effects.tracks and not win.dirty
    image = win.grab().toImage()
    colours = {image.pixelColor(x, y).name() for x in range(3, image.width(), 9) for y in range(3, image.height(), 9)}
    assert {theme.CANVAS, theme.INSET, theme.SUBTLE} <= colours                    # the dark theme is applied
    win.close()


def test_workflow_reference_lanes_suggestions_and_export(studio, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    win = studio
    lanes = win.timeline.lanes
    assert [ln.key for ln in lanes] == ["motion:gain", *(f"motion:{a}" for a in AXES), "view:audio"] and "PASSED" in win.val_summary.text()
    assert [ln.group for ln in lanes] == ["Motion"] * 7 + ["Vibration and sound"] and [ln.axis for ln in lanes] == [-1, 0, 1, 2, 3, 4, 5, -1]
    assert win.btn_check.isEnabled() and win.btn_add_track.isEnabled() and "Edit on the timeline" in win.lbl_next.text()
    assert win.project.soundtrack is not None and win.timeline.wave is not None and win.timeline.duration == pytest.approx(4.0, abs=0.1)
    assert "bass track" in win.lbl_fx_summary.text() and "2000 Hz" in win.lbl_bass.text()
    win.suggest_effects()
    assert [t.id for t in win.project.effects.tracks] == ["vibration", "strobe"]
    groups = {ln.key: ln.group for ln in win.timeline.lanes}
    assert groups["fx:vibration"] == "Vibration and sound" and groups["fx:strobe"] == "Effects"      # felt and heard / effects
    assert win.dirty and "Suggested" in win.status_text.text()
    assert "2 effect track(s)" in win.lbl_fx_summary.text() and win.bottom_tabs.currentIndex() == 0
    out = tmp_path / "ride.motion"
    assert win._guarded(lambda: win._export(str(out)))
    assert "2 effect track(s) + bass track" in win.status_text.text()
    doc = read_effects(out)
    assert doc is not None and doc.bass is not None and [t.id for t in doc.tracks] == ["vibration", "strobe"]
    assert np.allclose(motionfile.read_motion(out).samples, win.project.motion, atol=1e-6)
    win.chk_bass.setChecked(False)                                                 # bass is a setting of its own
    assert win.project.effects.bass is None and "bass" not in win.lbl_fx_summary.text()
    win.undo()                                                                     # undo takes the suggestions back, not the bass setting
    assert not win.project.effects.tracks and win.project.effects.bass is None and "Motion only" in win.lbl_fx_summary.text()
    plain = tmp_path / "plain.motion"
    assert win._guarded(lambda: win._export(str(plain))) and "motion only" in win.status_text.text()
    assert read_effects(plain) is None and "effects" not in motionfile.read_header(plain)
    win.redo()
    win.chk_bass.setChecked(True)
    assert len(win.project.effects.tracks) == 2 and win.project.effects.bass is not None
    win.seek(1.5)
    win.toggle_play()
    spin(app, 0.3)
    win.toggle_play()
    assert 1.7 < win.current_t < 2.4 and win.timeline.playhead == win.current_t and "pose" in win.readout.text()


def test_tracks_inspector_and_undo(studio) -> None:  # type: ignore[no-untyped-def]
    win, tl = studio, studio.timeline
    depth = len(win._undo)
    win.add_track("scent")
    win.add_track("dmx")
    scent, dmx = win.project.effects.track("scent"), win.project.effects.track("dmx")
    assert tl.current_lane == "fx:dmx" and win.insp_title.text() == "DMX channel · track" and win.trk_universe.isVisible()
    win.trk_universe.setValue(3)
    win.trk_channel.setValue(40)
    win.trk_label.setText("House lights")
    win.trk_label.editingFinished.emit()
    assert (dmx.universe, dmx.channel, dmx.label) == (3, 40, "House lights") and tl.lane("fx:dmx").label == "House lights"
    cue = tl.add_cue(tl.lane("fx:scent"), 1.0)
    assert win.insp_title.text() == "Scent · cue" and win.cue_param.isVisible() and win.cue_param_label.text() == "Cartridge"
    assert (win.cue_start.value(), win.cue_duration.value(), win.cue_level.value(), win.cue_param.value()) == (1.0, 3.0, 100.0, 1.0)
    win.cue_param.setValue(2)
    win.cue_level.setValue(40)
    win.cue_level.setValue(50)                                                     # typing 40, then 50: one undo step
    win.cue_duration.setValue(1.5)
    win.cue_fade_in.setValue(9.0)                                                  # clamped to the cue
    win.cue_label.setText("pine forest")
    win.cue_label.editingFinished.emit()
    assert (cue.params, cue.level, cue.duration, cue.fade_in, cue.label) == ({"slot": 2.0}, 0.5, 1.5, 1.5, "pine forest")
    assert win.cue_fade_in.value() == 1.5
    second = tl.add_cue(tl.lane("fx:scent"), 3.0)
    tl.sel_cues = [cue, second]
    tl.selectionChanged.emit()
    assert win.insp_title.text() == "2 cues" and not win.cue_start.isEnabled()
    win.cue_level.setValue(80)
    assert cue.level == second.level == 0.8 and cue.t == 1.0                       # several cues: level yes, position no
    tl.select_point(tl.lane("fx:dmx"), 0)
    assert win.insp_title.text() == "House lights · curve point" and win.box_point.isVisible() and not win.box_cue.isVisible()
    win.pt_value.setValue(70)
    win.pt_time.setValue(9.0)                                                      # fenced by the next point
    assert dmx.points[0] == pytest.approx((3.99, 0.7), abs=0.11) and dmx.points[0][0] < dmx.points[1][0]
    tl.current_lane = "fx:scent"
    tl.clear_selection()
    win.trk_enabled.setChecked(False)
    win.trk_enabled.clicked.emit()
    win.trk_param.setValue(4)
    assert scent.enabled is False and scent.params == {"slot": 4.0}
    assert tl.add_cue(tl.lane("fx:scent"), 0.2).params == {"slot": 4.0}            # new cues take the track's cartridge
    win.delete_current_track()
    assert win.project.effects.track("scent") is None and "fx:scent" not in [ln.key for ln in tl.lanes]
    steps = 0
    while len(win._undo) > depth:
        win.undo()
        steps += 1
    assert win.project.effects.track("scent") is None and win.project.effects.track("dmx") is None and 10 <= steps <= 20
    for _ in range(steps):
        win.redo()
    assert win.project.effects.track("dmx").label == "House lights" and win.project.effects.track("scent") is None
    while len(win._undo) > depth:
        win.undo()
    win.add_track("water")
    assert not win._redo and win.project.effects.track("water") is not None      # a new edit ends the redo history
    win.undo()


def test_manual_motion_updates_the_motion_inside_the_limits(studio, app) -> None:  # type: ignore[no-untyped-def]
    win, tl = studio, studio.timeline
    before = win.project.extraction.motion.copy()
    assert tl.lane("motion:heave").pts() == [] and win.project.edits.is_empty()    # the axis lanes are there, nothing drawn yet
    win.insert_gesture("lift", at=0.5)
    assert tl.current_lane == "motion:heave" and win.insp_title.text() == "Heave · motion" and win._motion_stale()
    assert "Up / down" in win.insp_about.text() and "travel ±" in win.insp_about.text() and win.box_motion.isVisible()
    assert tl.motion_final is False                                               # shown as provisional until re-processed
    wait(app, lambda: not win._motion_stale(), 10.0)                              # re-processed by itself shortly after
    after = win.project.extraction.motion
    assert after[150:260, 2].max() - before[150:260, 2].max() > 0.02 and tl.motion_final and "Motion updated" in win.status_text.text()
    assert win._guarded(win.do_validate) and "PASSED" in win.val_summary.text()
    gain = tl.lane("motion:gain")
    tl.add_point(gain, 2.0, 0.0)                                                  # intensity to zero in the middle
    assert win.project.edits.gain == [(0.0, 1.0), (2.0, 0.0), (4.0, 1.0)]
    win._auto_process()
    assert np.abs(win.project.extraction.motion[195:205, 5]).max() < np.abs(before[195:205, 5]).max() * 0.2 + 1e-6
    shown = len(tl._layout())
    tl.toggle_group("Motion")                                                     # a click on the group's heading folds it away
    assert len(tl._layout()) == shown - 7 and tl.lane("motion:heave").pts() and tl.content_height() < 7 * 44
    tl.toggle_group("Motion")
    assert len(tl._layout()) == shown
    win.insert_gesture("drop", at=3.5)                                            # does not fit before the end
    assert "Not enough time" in win.status_text.text()
    win.clear_manual_motion()
    win._auto_process()
    assert np.allclose(win.project.extraction.motion, before, atol=1e-9) and tl.lane("motion:heave").pts() == []
    win.undo()
    assert win.project.edits.offsets["heave"] and win._motion_stale()
    win.clear_manual_motion()
    win._auto_process()
    win.act_auto.setChecked(False)                                                # hand-made motion only
    win._auto_motion_toggled()
    assert np.abs(win.project.extraction.motion).max() < 1e-9 and win.project.automatic_motion is False
    win.act_auto.setChecked(True)
    win._auto_motion_toggled()
    assert np.allclose(win.project.extraction.motion, before, atol=1e-9)
    # the options of an axis lane: how much automatic motion this axis gets (the same number as on the Programme tab)
    tl.choose_lane("motion:yaw")
    assert win.insp_title.text() == "Yaw · motion" and win.axis_amount.value() == 1.0 and np.abs(before[:, 5]).max() > 0.002
    win.axis_amount.setValue(0.0)
    assert win.in_axis["yaw"].value() == 0.0 and win.dirty
    wait(app, lambda: not win._regenerate.isActive(), 10.0)                       # the motion follows by itself
    assert np.abs(win.project.extraction.motion[:, 5]).max() < 1e-9 and "Motion updated" in win.status_text.text()
    win.in_axis["yaw"].setValue(1.0)                                              # ... and from the Programme tab as well
    wait(app, lambda: not win._regenerate.isActive(), 10.0)
    assert np.allclose(win.project.extraction.motion, before, atol=1e-9)
    tl.choose_lane("motion:gain")
    assert win.insp_title.text() == "Intensity · motion" and not win.btn_gesture.isVisible() and "all axes" in win.axis_amount_label.text()


def test_live_output_follows_the_playhead_and_the_venue_check(studio, app) -> None:  # type: ignore[no-untyped-def]
    win = studio
    cap = CaptureOutput(history=100000)
    win.live_output = lambda: cap
    assert win._guarded(lambda: win.load_patch(str(PATCH)))
    assert win.fixtures.rowCount() == 7 and "sACN" in win.lbl_patch.text() and win.project.patch_path == str(PATCH)
    win.add_track("water")
    win.add_track("fog")
    win.project.effects.track("water").cues = [Cue(0.3, 0.3), Cue(1.7, 2.3)]      # the second is longer than the venue allows
    win.project.effects.track("fog").cues = [Cue(1.8, 1.0)]
    win._on_edited()
    win.run_preflight()
    text = win.txt_check.toPlainText()
    assert "LIMITED: maximum on-time" in text and "no fixture for effect type 'vibration'" in text
    held = win.timeline.held["water"]
    assert held and held[0][0] == pytest.approx(3.7, abs=0.06) and held[0][2] == "maximum on-time"
    win.set_live(True)
    assert win.live is not None and win.btn_live.isChecked() and "LIVE" in win.lbl_live.text() and cap.is_open
    win.seek(0.0)
    spin(app, 0.25)
    assert all(max(d) == 0 for _t, _u, d in cap.history) and cap.history            # live but paused: dark frames
    win.toggle_play()
    start = win._anchor[0]
    spin(app, 2.0)
    win._update_monitor()
    assert "frames sent, 0 error(s)" in win.lbl_live_status.text()
    win.toggle_play()
    spin(app, 0.2)
    water = [t - start for t, u, d in cap.history if u == 2 and d[1] == 255]
    fog = [t - start for t, u, d in cap.history if u == 1 and d[0] == 255]
    assert min(water) == pytest.approx(0.3, abs=0.08) and max(water) > 1.8
    assert not any(0.68 < t < 1.62 for t in water)                                   # two bursts with the gap between them
    assert min(fog) == pytest.approx(0.8, abs=0.08)                                  # cue at 1.8 s, sent 1 s early
    assert max(cap.latest[1]) == 0 and max(cap.latest[2]) == 0                       # paused: everything off again
    # edits reach the running service; a single fixture can be fired by hand; blackout
    win.project.effects.track("water").cues = [Cue(0.2, 0.3)]
    win._on_edited()
    spin(app, 0.3)
    assert win.live._doc.level("water", 0.3) == 1.0 and win.live._doc.level("water", 1.5) == 0.0
    mark = len(cap.history)
    win.in_fire_s.setValue(0.3)
    row = next(r for r in range(win.fixtures.rowCount()) if win.fixtures.item(r, 0).text() == "air@PLATFORM-01")
    assert win.fixtures.item(row, 4).text() == "▶ Fire"
    win._fixture_clicked(row, 4)                                                     # a click on "Fire" in the fixture's row
    assert "Fired air@PLATFORM-01 for 0.3 s" in win.status_text.text()
    spin(app, 0.6)
    on = [t for t, u, d in cap.history[mark:] if u == 2 and d[2] == 255]
    assert on and 0.2 < max(on) - min(on) < 0.45
    win.chk_hold.setChecked(True)
    win.seek(0.3)                                                                    # hold: levels under the paused playhead
    spin(app, 0.3)
    assert cap.channel(2, 2) == 255
    win.blackout()
    spin(app, 0.2)
    assert cap.channel(2, 2) == 0 and not win.chk_hold.isChecked() and "Blackout" in win.status_text.text()
    win.set_live(False)
    assert win.live is None and not cap.is_open and not win.btn_live.isChecked() and win.lbl_live.text() == ""
    # without a venue patch: sound only (bass + seat vibration on the sound card; here the null device)
    win.clear_patch()
    assert win.fixtures.rowCount() == 0 and not any(win.timeline.held.values()) and "default limits" in win.txt_check.toPlainText()
    # no venue patch: the default limits mark what would be shortened (a 3 s water burst: at most 2 s)
    win.project.effects.track("water").cues = [Cue(0.2, 3.0)]
    win._on_edited()
    win.check_all()
    held = win.timeline.held["water"]
    assert held and held[0][2] == "maximum on-time" and held[0][0] == pytest.approx(2.2, abs=0.06)
    assert "1 effect device(s) limited" in win.status_text.text() and "motion inside the platform's limits" in win.status_text.text()
    assert win.tabs.currentIndex() == 2 and "no fixture" not in win.txt_check.toPlainText()
    win.timeline.choose_lane("fx:water")
    assert "Default limit: on ≤ 2 s" in win.insp_about.text() and "mist" in win.insp_about.text()
    win.project.effects.track("water").cues = [Cue(0.2, 0.3)]
    win._on_edited()
    win.set_live(True)
    assert win.live.patch.audio.enabled and win.live.service.audio is not None
    win.seek(1.2)
    win.toggle_play()
    spin(app, 0.5)
    win._update_monitor()
    state = win.live.service._audio_state
    assert state.active and state.track_id != 0 and win.live.service.audio.callbacks > 3
    assert "sound: playing" in win.lbl_live_status.text()
    win.toggle_play()
    win.set_live(False)
    for track_id in ("water", "fog"):
        win.timeline.current_lane = f"fx:{track_id}"
        win.delete_current_track()


def test_project_file_and_motion_only_projects(studio, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    win = studio
    win.insert_gesture("bank-left", at=0.5)
    win._auto_process()
    win.add_track("air")
    win.project.effects.track("air").cues = [Cue(2.0, 0.25)]
    win._on_edited()
    assert win._save_to(str(tmp_path / "ride")) and not win.dirty and win.project.path.name == "ride.4dvr"
    assert "ride.4dvr" in win.windowTitle() and "*" not in win.windowTitle()
    exported = tmp_path / "ride.motion"
    assert win._guarded(lambda: win._export(str(exported)))
    other = new_window()
    other.open_project(str(tmp_path / "ride.4dvr"))
    wait(app, lambda: other.project.mode == "video" and other._thread is None)
    assert other.project.effects.to_dict() == win.project.effects.to_dict() and other.project.edits.offsets == win.project.edits.offsets
    assert np.allclose(other.project.extraction.motion, win.project.extraction.motion, atol=1e-6) and not other.dirty
    assert "motion:roll" in [ln.key for ln in other.timeline.lanes] and other.reader is not None and other.project.soundtrack is not None
    # an exported file opened on its own: effects can be edited, the motion cannot
    other.open_motion(str(exported))
    assert other.project.mode == "motion" and other.lbl_mode.text() == "effects on an existing .motion"
    keys = [ln.key for ln in other.timeline.lanes]
    assert "motion:gain" not in keys and "fx:air" in keys and not other.btn_analyse.isEnabled() and other.btn_export.isEnabled()
    assert keys[:7] == [*(f"view:{a}" for a in AXES), "view:audio"] and "fixed" in other.lbl_next.text()   # the file's motion: shown only
    assert not other.timeline.lane("view:heave").editable and "finished .motion file" in other.lbl_scene.text()
    other.timeline.choose_lane("view:audio")                                      # the sound lane holds the bass options
    assert other.insp_title.text() == "Sound · bass" and other.box_sound.isVisible() and "No sound track" in other.insp_about.text()
    other.check_all()
    assert "from the file" in other.status_text.text()
    other.insert_gesture("jolt", at=1.0)
    assert "fixed" in other.status_text.text() and not other.dirty
    other.timeline.add_cue(other.timeline.lane("fx:air"), 3.0)
    again = tmp_path / "ride-fx.motion"
    assert other._guarded(lambda: other._export(str(again)))
    assert np.array_equal(motionfile.read_motion(again).samples, motionfile.read_motion(exported).samples)
    assert [c.t for c in read_effects(again).track("air").cues] == [2.0, 3.0]
    other.attach_media(str(win.project.video.path))                                # picture and waveform for the preview
    assert other.reader is not None and "view:audio" in [ln.key for ln in other.timeline.lanes]
    fx = tmp_path / "only.fx.json"
    from fourdvr_fx.embed import write_effects

    write_effects(fx, win.project.effects_for_export())
    other.import_effects(str(fx))
    assert [t.id for t in other.project.effects.tracks] == [t.id for t in win.project.effects_for_export().tracks]
    other.new_project()
    assert other.project.mode == "empty" and other.timeline.lanes == [] and not other.dirty
    other.close()
    win.clear_manual_motion()
    win._auto_process()


def test_analyse_button_does_the_whole_job_and_keeps_what_was_edited(app, media, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from fourdvr_extractor import scene as sc
    from scene_models import make_models

    monkeypatch.setenv(sc.MODELS_ENV, str(make_models(tmp_path / "models")))      # stand-in models: what they find is arbitrary
    win = new_window()
    win.open_path(str(media["stereo"]))
    wait(app, lambda: win.project.video is not None and win._thread is None)
    assert "2 Analyse video" in win.lbl_next.text() and "Scenes will be looked for" in win.lbl_scene.text() and not win.btn_check.isEnabled()
    win.analyse_all()                                                             # button 2
    assert not win.btn_analyse.isEnabled() and "Working" in win.lbl_next.text()
    wait(app, lambda: win._thread is None and win.status_text.text().startswith("Analysed"))
    p = win.project
    assert p.scene is not None and p.extraction.report is not None and p.extraction.report.passed and "PASSED" in win.val_summary.text()
    assert "motion inside the platform's limits; drafted: " in win.status_text.text() and win.tabs.currentIndex() == 2 and win.dirty
    assert {"vibration", "strobe"} <= {t.id for t in p.effects.tracks} and p.effects.bass is not None
    assert win.lbl_scene.text().startswith(("<b>Seen:</b>", "Nothing of rain")) and "Edit on the timeline" in win.lbl_next.text()
    assert {ln.group for ln in win.timeline.lanes} == {"Motion", "Vibration and sound", "Effects"}
    win.check_all()                                                               # button 3
    assert win.status_text.text().startswith("Check — motion inside the platform's limits; ")
    # a second analysis keeps what was edited by hand and drafts only the lanes that are empty
    p.effects.track("strobe").cues = [Cue(3.0, 0.5)]
    p.effects.tracks = [t for t in p.effects.tracks if t.id != "vibration"]
    win._rebuild_lanes()
    win._on_edited()
    win.analyse_all()
    wait(app, lambda: win._thread is None and win.status_text.text().startswith("Analysed"))
    assert [(c.t, c.duration) for c in p.effects.track("strobe").cues] == [(3.0, 0.5)] and p.effects.track("vibration") is not None
    assert "drafted: vibration" in win.status_text.text() and "strobe" not in win.status_text.text().split("drafted:")[1]
    win.undo()                                                                    # the draft is one step that can be taken back
    assert p.effects.track("vibration") is None and p.effects.track("strobe").cues[0].t == 3.0
    # looking for scenes can be switched off (menu and Effects tab show the same switch)
    win.chk_scenes.setChecked(False)
    assert not win.act_scenes.isChecked() and p.detect_scenes is False and "will not be looked for" not in win.lbl_scene.text()
    win.start_analysis()
    wait(app, lambda: win._thread is None and win.status_text.text().startswith("Analysis:"))
    assert p.scene is None and "switched off" in win.lbl_scene.text() and "switched off" in win.status_text.text()
    # a click on a group heading folds the group away
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    tl = win.timeline
    top = next(y for kind, item, y in tl._rows() if kind == "group" and item == "Motion")
    QTest.mouseClick(tl, Qt.MouseButton.LeftButton, pos=QPoint(60, int(top) + 6))
    assert tl.collapsed == {"Motion"} and all(ln.group != "Motion" for ln, _y in tl._layout())
    QTest.mouseClick(tl, Qt.MouseButton.LeftButton, pos=QPoint(60, int(top) + 6))
    assert tl.collapsed == set()
    win.dirty = False
    win.close()


def test_esp32_boards_in_the_live_tab(studio, app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A venue driven through ESP32 boards: one answers (a DMX line and a sound output), one is not there."""
    win = studio
    node = br.SimNode(1, dmx_ports=1, audio_channels=2, actuators=True, host="127.0.0.1", port=0).start()
    example = ROOT / "config" / "effects" / "patch.esp32.example.toml"
    local = tmp_path / "venue.toml"
    local.write_text(example.read_text().replace("192.168.10.101", "127.0.0.1").replace("192.168.10.102", "127.0.0.2")
                     .replace("port = 47002 ", f"port = {node.address[1]}  "))
    try:
        win.in_platforms.setText("PLATFORM-01, PLATFORM-02")
        win._platforms_changed()
        assert win._guarded(lambda: win.load_patch(str(local)))
        assert "through ESP32 boards" in win.lbl_patch.text() and "bass output" in win.lbl_patch.text()
        rows = [[win.nodes.item(r, c).text() for c in range(3)] for r in range(win.nodes.rowCount())]
        assert rows == [["PLATFORM-01 + theatre", "127.0.0.1", "not live"], ["PLATFORM-02", "127.0.0.2", "not live"]]
        assert not win.nodes.isHidden()
        win.add_track("fog")
        win.add_track("heat")
        win.project.effects.track("fog").cues = [Cue(1.3, 1.0)]                    # the fog device is sent 1 s early
        win.project.effects.track("heat").cues = [Cue(0.4, 1.0)]
        win._on_edited()
        win.timeline.choose_lane("fx:heat")
        assert "Limit at this venue: level ≤ 80 %" in win.insp_about.text()
        win.live_output = lambda: None                                             # the patch's own output: the ESP32 link
        win.set_live(True)
        assert win.live is not None and isinstance(win.live.service.output, br.BridgeOutput)

        def answered() -> bool:
            win._update_monitor()
            return win.nodes.item(0, 2).text() == "DMX ×1 + sound · answers"

        wait(app, answered, 6.0)
        assert win.nodes.item(1, 2).text() == "no answer: left out"
        assert "1 of 2 ESP32 board(s) answer" in win.lbl_live_status.text() and "0 error(s)" in win.lbl_live_status.text()
        win.seek(0.0)
        win.toggle_play()
        spin(app, 1.7)
        win.toggle_play()
        spin(app, 0.4)
        frames = [frame for _t, _port, frame in node.dmx_history]
        assert any(f[0] == 255 for f in frames) and max(f[33] for f in frames) == round(255 * 0.8)   # fog; heat at the venue's 80 %
        assert node.audio_blocks and node.channel(0, 1) == 0 and node.invalid == 0                   # bass went out; paused: dark
        node.inhibited = True                                                                        # emergency stop at the board
        spin(app, 1.3)
        win._update_monitor()
        assert "emergency stop" in win.nodes.item(0, 2).text()
    finally:
        win.set_live(False)
        node.stop()
        win.clear_patch()
        assert win.nodes.isHidden()
        win.in_platforms.setText("PLATFORM-01")
        win._platforms_changed()
        for track_id in ("fog", "heat"):
            if win.project.effects.track(track_id) is not None:
                win.timeline.current_lane = f"fx:{track_id}"
                win.delete_current_track()


def test_lane_options_the_type_parameter_and_scaling(studio) -> None:  # type: ignore[no-untyped-def]
    win, tl = studio, studio.timeline
    depth = len(win._undo)
    if win.project.effects.track("strobe") is None:
        win.add_track("strobe")
    tl.choose_lane("fx:strobe")                                                    # a lane's own options: here the flash rate of new cues
    assert win.insp_title.text() == "Strobe / lightning · track" and win.trk_param.isVisible() and win.trk_param.suffix() == " Hz"
    assert win.trk_param_label.text() == "Flash rate (new cues)" and "Default limit: on ≤ 5 s" in win.insp_about.text()
    win.trk_param.setValue(2.5)
    assert win.project.effects.track("strobe").params == {"rate_hz": 2.5}
    assert tl.add_cue(tl.lane("fx:strobe"), 2.0).params == {"rate_hz": 2.5} and "flash rate" in win.insp_hint.text()
    win.add_track("snow")
    snow = win.project.effects.track("snow")
    snow.cues = [Cue(0.5, 1.0, 0.8)]
    snow.points = [(0.0, 0.0), (2.0, 0.6), (4.0, 0.0)]
    win._on_edited()
    tl.choose_lane("fx:snow")
    assert "Snow machine" in win.insp_about.text() and "Default limit: on ≤ 10 s" in win.insp_about.text() and not win.trk_param.isVisible()
    win.scale_current_lane(0.5)                                                    # the whole lane weaker ...
    assert snow.cues[0].level == 0.4 and snow.points[1] == (2.0, 0.3)
    win.scale_current_lane(4.0)                                                    # ... or stronger, never above 100 %
    assert snow.cues[0].level == 1.0 and snow.points[1] == (2.0, 1.0)
    win.undo()
    win.undo()
    again = win.project.effects.track("snow")
    assert again.cues[0].level == 0.8 and again.points[1] == (2.0, 0.6)
    tl.choose_lane("view:audio")                                                   # the sound lane: the bass track's options
    assert win.insp_title.text() == "Sound · bass" and win.box_sound.isVisible() and not win.box_track.isVisible()
    assert "subwoofers and bass shakers" in win.insp_about.text() and "Hz" in win.lbl_bass.text()
    while len(win._undo) > depth:
        win.undo()
    assert win.project.effects.track("snow") is None
