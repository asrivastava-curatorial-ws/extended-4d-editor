"""Sound track, suggestions, manual motion editing and the Editor project (no GUI)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from fourdvr_extractor import soundtrack as st_mod
from fourdvr_extractor.manual import GESTURES, MotionEdits, curve_at, insert_shape, sample_curve
from fourdvr_extractor.pipeline import DEFAULT_PROFILE
from fourdvr_extractor.soundtrack import load_soundtrack, make_bass_track
from fourdvr_extractor.suggest import apply_suggestions, find_flashes, simplify, suggest_all
from fourdvr_fx import BassTrack, Cue, EffectsDocument
from fourdvr_fx.embed import read_effects
from fourdvr_motion import motionfile
from fourdvr_motion.platform_config import load_platform_config
from fourdvr_studio.cli import main
from fourdvr_studio.project import ProjectError, StudioProject


# ------------------------------------------------------------------------------ sound track
def test_soundtrack_envelopes_and_bass_track(media) -> None:  # type: ignore[no-untyped-def]
    st = load_soundtrack(media["stereo"])
    assert st is not None and st.decoder == "pyav" and st.layout == "stereo" and st.lfe is None
    assert st.duration_s == pytest.approx(4.0, abs=0.08)
    t = np.arange(len(st.bass)) / st_mod.FEATURE_RATE
    assert st.bass[(t > 1.2) & (t < 2.3)].min() > 0.8 and st.bass[(t < 0.8) | (t > 2.8)].max() < 0.1
    assert st.level[(t > 1.2) & (t < 2.3)].min() > 0.8 and len(st.peak_hi) == len(st.level)
    bass = make_bass_track(st, 4.0)
    assert bass is not None and bass.sample_rate_hz == 2000 and bass.duration_s == pytest.approx(4.0)
    x = bass.as_float()
    tt = np.arange(len(x)) / 2000.0
    assert np.abs(x).max() == pytest.approx(0.9, abs=0.01)                        # normalised, with headroom
    assert np.abs(x[(tt < 0.9) | (tt > 2.6)]).max() < 0.02                        # the 1 kHz tone is gone
    spectrum = np.abs(np.fft.rfft(x[(tt > 1.1) & (tt < 2.4)]))
    assert np.fft.rfftfreq(int(((tt > 1.1) & (tt < 2.4)).sum()), 1 / 2000.0)[spectrum.argmax()] == pytest.approx(60.0, abs=1.0)
    assert make_bass_track(st, 6.0).duration_s == pytest.approx(6.0)              # padded to the programme length
    assert st_mod.default_bass_rate(600) == 2000 and st_mod.default_bass_rate(1800) == 1000 and st_mod.default_bass_rate(5000) == 500
    with pytest.raises(ValueError):
        make_bass_track(st, 4.0, cutoff_hz=200.0, sample_rate_hz=500)


def test_surround_lfe_channel_silence_and_fallback(media, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    st = load_soundtrack(media["surround"])
    assert st is not None and st.lfe is not None and "5.1" in st.layout
    t = np.arange(len(st.lfe)) / st.rate_hz
    assert np.abs(st.lfe[(t > 1.2) & (t < 2.3)]).max() > 0.3                      # the rumble is in the LFE channel ...
    assert np.abs(st.mono[(t > 1.2) & (t < 2.3)]).max() < 0.15                    # ... and not in the mix-down
    assert make_bass_track(st, 3.0) is not None and st.bass[130:220].min() > 0.8
    assert load_soundtrack(media["silent"]) is None
    quiet = st_mod.Soundtrack(8000, (1e-4 * np.sin(np.arange(16000) / 20)).astype(np.float32))
    assert make_bass_track(quiet, 2.0) is None                                    # nothing worth amplifying
    if shutil.which("ffmpeg"):                                                    # same result without PyAV
        via_cli = st_mod._decode_ffmpeg(media["stereo"], 8000)
        assert via_cli is not None and len(via_cli[0]) == pytest.approx(32000, abs=600)
        assert st_mod._decode_ffmpeg(media["silent"], 8000) is None
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(st_mod.SoundtrackError, match="no audio decoder"):
        st_mod._decode_ffmpeg(media["stereo"], 8000)
    with pytest.raises(st_mod.SoundtrackError):
        load_soundtrack(Path(__file__))                                           # not a media file


# ------------------------------------------------------------------------------ manual motion
def test_manual_curves_ease_and_presets() -> None:
    pts = [(1.0, 0.0), (2.0, 1.0)]
    assert curve_at(pts, 0.0, 9.0) == 0.0 and curve_at(pts, 3.0, 9.0) == 1.0 and curve_at([], 1.0, 0.7) == 0.7
    assert curve_at(pts, 1.5, 0.0) == pytest.approx(0.5) and curve_at(pts, 1.1, 0.0) < 0.02   # eased, not linear
    grid = np.linspace(0, 3, 301)
    assert np.allclose(sample_curve(pts, grid, 0.0), [curve_at(pts, t, 0.0) for t in grid])
    v = np.gradient(sample_curve(pts, grid, 0.0), grid)
    assert abs(v[100]) < 1e-3 and abs(v[200]) < 1e-3                               # no velocity step at the points
    base = [(0.0, 0.2), (10.0, 0.2)]
    new = insert_shape(base, 4.0, GESTURES["drop"].shape, 0.0)
    assert new[-1] == (10.0, 0.2) and (4.0, 0.2) in new
    assert new[0] == (0.0, 0.2) and (5.5, 0.2) in new and min(v for _t, v in new) == pytest.approx(0.2 - 0.5)
    again = insert_shape(new, 4.1, GESTURES["jolt"].shape, 0.0)                    # added on top of the curve ...
    assert not any(abs(t - 4.45) < 1e-6 for t, _v in again)                        # ... replacing the points it spans
    for edge in (4.1, 5.0):
        assert curve_at(again, edge, 0.0) == pytest.approx(curve_at(new, edge, 0.0))   # outside: unchanged
    assert curve_at(again, 4.4, 0.0) == pytest.approx(curve_at(new, 4.4, 0.0) + 0.15)
    assert insert_shape([], 1.0, GESTURES["bank-left"].shape, 0.0, scale=3.0)[1][1] == -1.0   # clamped
    assert {g.axis for g in GESTURES.values()} == {"surge", "sway", "heave", "roll", "pitch", "yaw"}


def test_manual_edits_scale_add_and_stay_inside_the_limits() -> None:
    profile = load_platform_config(DEFAULT_PROFILE)
    fs, n = 100.0, 1001
    raw = np.zeros((n, 6))
    raw[:, 4] = 0.02
    edits = MotionEdits()
    assert edits.is_empty() and MotionEdits([(0.0, 1.0)], {"heave": [(1.0, 0.0)]}).is_empty()
    edits.lane("gain").extend([(2.0, 1.0), (3.0, 0.0), (6.0, 0.0), (7.0, 2.0)])
    edits.lane("heave").extend(insert_shape([], 4.0, GESTURES["jolt"].shape, 0.0))
    out = edits.apply(raw, fs, profile)
    assert np.all(raw[:, 4] == 0.02)                                                # the input is untouched
    assert out[100, 4] == pytest.approx(0.02) and out[450, 4] == 0.0 and out[900, 4] == pytest.approx(0.04)
    travel = min(abs(profile.limits.position_min[2]), profile.limits.position_max[2])
    assert out[:, 2].max() == pytest.approx(0.15 * travel, rel=0.01) and out[430, 2] == out[:, 2].max()
    assert out[0, 2] == 0.0 and out[-1, 2] == 0.0
    back = MotionEdits.from_dict(json.loads(json.dumps(edits.to_dict())))
    assert back.gain == edits.gain and back.offsets == edits.offsets and back.copy().offsets is not back.offsets
    for bad in ({"gain": [[0, 3]]}, {"offsets": {"wobble": []}}, {"offsets": {"roll": [[1, -2]]}}, {"gain": "x"}, []):
        with pytest.raises(ValueError):
            MotionEdits.from_dict(bad)
    with pytest.raises(ValueError):
        edits.lane("wobble")


# ------------------------------------------------------------------------------ suggestions
def test_simplify_keeps_the_shape_with_few_points() -> None:
    t = np.linspace(0, 100, 1001)
    v = np.clip(np.sin(t / 8.0), 0, 1)
    pts = simplify(t, v, 0.03)
    assert 8 < len(pts) < 80 and pts[0][0] == 0.0 and pts[-1][0] == 100.0
    approx = np.interp(t, [p[0] for p in pts], [p[1] for p in pts])
    assert np.abs(approx - v).max() < 0.032
    assert len(simplify(t, np.random.default_rng(1).random(1001), 0.001, max_points=50)) <= 50
    assert simplify(t[:2], v[:2], 0.1) == [(0.0, 0.0), (0.1, round(float(v[1]), 3))] and simplify(t[:0], v[:0], 0.1) == []


@pytest.fixture(scope="module")
def analysed(media) -> StudioProject:  # type: ignore[no-untyped-def]
    project = StudioProject()
    project.extraction.settings.projection = "360"
    project.open_video(media["stereo"])
    project.analyse()
    project.generate()
    return project


def test_suggestions_from_picture_motion_and_sound(analysed: StudioProject) -> None:
    ex = analysed.extraction
    assert ex.analysis is not None and len(ex.analysis.luma) == len(ex.analysis.times)
    flashes = find_flashes(ex.analysis)
    assert len(flashes) == 1 and flashes[0] == pytest.approx(1.5, abs=0.07)
    assert analysed.soundtrack is not None and analysed.effects.bass is not None and analysed.soundtrack_note == ""
    found = {s.type: s for s in suggest_all(ex.analysis, ex.events, analysed.soundtrack, analysed.duration_s)}
    assert "strobe" in found and "vibration" in found and "wind" not in found       # a pure pan has no forward speed
    cue = found["strobe"].cues[0]
    assert cue.t == pytest.approx(1.5, abs=0.07) and cue.duration == 0.15 and cue.params == {"rate_hz": 3.0}
    vib = EffectsDocument(4.0)
    apply_suggestions(vib, list(found.values()))
    track = vib.track("vibration")
    assert track is not None and track.value_at(1.8) > 0.5 and track.value_at(3.5) < 0.1   # where the rumble is
    assert vib.meta["suggested"] == sorted(found) and track.points[-1][0] == 4.0
    # hand-made tracks of the same type are not touched; the suggested track is replaced, not duplicated
    doc = EffectsDocument(4.0)
    doc.new_track("strobe").cues = [Cue(0.2, 0.1)]
    mine = doc.new_track("strobe", label="mine")
    mine.cues = [Cue(3.0, 0.5)]
    assert apply_suggestions(doc, list(found.values()), types=["strobe"]) == ["strobe"]
    assert [c.t for c in doc.track("strobe").cues] == [cue.t] and mine.cues[0].t == 3.0 and len(doc.tracks) == 2


# ------------------------------------------------------------------------------ project
def test_project_export_with_and_without_effects(analysed: StudioProject, tmp_path: Path) -> None:
    project = analysed
    project.process()
    assert project.validate().passed and project.motion_is_final
    project.bass_wanted = False
    project.refresh_bass()
    plain = tmp_path / "plain.motion"
    project.export(plain)                                                           # no effects: a plain motion file
    header = motionfile.read_motion(plain).header
    assert "effects" not in header and read_effects(plain) is None
    assert header["generation"]["generator"] == "Extended-4D-Editor"
    project.bass_wanted = True
    project.refresh_bass()
    assert project.suggest() == ["vibration", "strobe"]
    project.effects.new_track("water").cues = [Cue(2.0, 0.4)]
    project.effects.new_track("fog")                                                # empty tracks are not exported
    full = tmp_path / "full.motion"
    sha = project.export(full)
    mf = motionfile.read_motion(full, verify=True)
    assert np.array_equal(mf.samples, motionfile.read_motion(plain).samples) and len(sha) == 64
    doc = read_effects(full)
    assert doc is not None and [t.id for t in doc.tracks] == ["vibration", "strobe", "water"] and doc.bass is not None
    assert doc.duration_s == pytest.approx(project.duration_s) and doc.meta["generator"] == "Extended-4D-Editor"
    assert main(["extract", "validate", str(full)]) == 0 and main(["fx", "info", str(full)]) == 0


def test_manual_motion_goes_through_the_constraints(analysed: StudioProject) -> None:
    project = analysed
    project.edits = MotionEdits()
    project.process()
    before = project.extraction.motion.copy()
    project.edits.lane("heave").extend(insert_shape([], 1.0, GESTURES["jolt"].shape, 0.0, scale=2.0))   # far too much
    project.edits.lane("gain").extend([(0.0, 1.0), (2.5, 1.0), (3.0, 0.0)])
    project.process()
    after = project.extraction.motion
    assert (after[100:190, 2] - before[100:190, 2]).max() > 0.008                   # the jolt is there, scaled to fit
    assert np.abs(after[330:, 5]).max() < np.abs(before[330:, 5]).max() * 0.2 + 1e-6    # intensity curve at zero
    assert project.validate().passed                                                # still inside every limit
    assert project.extraction._generation_metadata()["manual_edits"]["offsets"]["heave"]
    project.generate(automatic=False)                                               # hand-made motion only
    project.process()
    assert np.abs(project.extraction.motion[:, 5]).max() < 1e-9 and project.extraction.motion[100:190, 2].max() > 0.008
    assert project.extraction._generation_metadata()["automatic_motion"] is False
    project.edits = MotionEdits()
    project.generate()
    project.process()


def test_project_file_round_trip(analysed: StudioProject, tmp_path: Path, media) -> None:  # type: ignore[no-untyped-def]
    project = analysed
    project.effects = project._new_effects(project.duration_s)
    project.refresh_bass()
    project.effects.new_track("wind").points = [(0.0, 0.0), (2.0, 0.8), (4.0, 0.0)]
    project.edits = MotionEdits([], {"roll": [(1.0, 0.0), (2.0, 0.5), (3.0, 0.0)]})
    project.patch_path, project.platform_ids = "/venue/patch.toml", ["PLATFORM-01", "PLATFORM-02"]
    project.process()
    video_copy = tmp_path / "work" / "pan.mp4"
    video_copy.parent.mkdir()
    shutil.copy(media["stereo"], video_copy)
    project.extraction.video.path = str(video_copy)
    saved = project.save(tmp_path / "work" / "ride")
    assert saved.name == "ride.4dvr" and json.loads(saved.read_text())["video"]["relative"] == "pan.mp4"
    moved = tmp_path / "elsewhere"
    shutil.move(str(tmp_path / "work"), moved)                                      # the folder is moved: still opens
    again = StudioProject.load(moved / "ride.4dvr")
    assert again.mode == "video" and again.extraction.analysis is not None and again.platform_ids == project.platform_ids
    assert np.allclose(again.extraction.analysis.angular_velocity, project.extraction.analysis.angular_velocity, atol=1e-6)
    assert again.edits.offsets == project.edits.offsets and again.effects.to_dict() == project.effects.to_dict()
    assert again.restore_motion() and np.allclose(again.extraction.motion, project.extraction.motion, atol=1e-6)
    (moved / "pan.mp4").write_bytes(media["silent"].read_bytes())
    with pytest.raises(ProjectError, match="not the video this project was made from"):
        StudioProject.load(moved / "ride.4dvr")
    (moved / "pan.mp4").unlink()
    with pytest.raises(ProjectError, match="video not found"):
        StudioProject.load(moved / "ride.4dvr")
    (tmp_path / "junk.4dvr").write_text("{}")
    with pytest.raises(ProjectError):
        StudioProject.load(tmp_path / "junk.4dvr")
    project.edits = MotionEdits()
    project.extraction.video.path = str(media["stereo"])


def test_motion_file_project_edits_effects_only(analysed: StudioProject, tmp_path: Path) -> None:
    source = tmp_path / "old.motion"
    analysed.effects = analysed._new_effects(analysed.duration_s)
    analysed.bass_wanted = False
    analysed.refresh_bass()
    analysed.process()
    analysed.export(source)                                                         # a v1-style motion-only file
    original = source.read_bytes()
    project = StudioProject()
    project.open_motion(source)
    assert project.mode == "motion" and project.motion_is_final and project.effects.is_empty()
    assert project.duration_s == pytest.approx(analysed.duration_s) and project.motion.shape[1] == 6
    project.effects.new_track("air").cues = [Cue(1.0, 0.25)]
    out = tmp_path / "new.motion"
    project.export(out)
    assert np.array_equal(motionfile.read_motion(out).samples, motionfile.read_motion(source).samples)
    assert read_effects(out).track("air").cues[0].t == 1.0 and source.read_bytes() == original
    saved = project.save(tmp_path / "fx-only.4dvr")
    again = StudioProject.load(saved)
    assert again.mode == "motion" and again.effects.track("air") is not None and not again.restore_motion()
    # a bass track that came with the file can be switched off and on again without the sound it was made from
    again.effects.bass = BassTrack.from_float(0.2 * np.sin(np.arange(4000) / 9.0), 2000)
    kept = again.effects.bass
    again.bass_wanted = False
    again.refresh_bass()
    assert again.effects.bass is None
    again.bass_wanted = True
    again.refresh_bass()
    assert again.effects.bass is kept
    again.effects.bass = None
    again.effects.tracks.clear()
    again.export(tmp_path / "stripped.motion")
    assert (tmp_path / "stripped.motion").read_bytes() == original                  # no effects: the original file again
    header = motionfile.read_header(source)
    header["effects"] = {"format": "nonsense"}
    motionfile.write_motion(tmp_path / "bad.motion", header, motionfile.read_motion(source).samples)
    with pytest.raises(ProjectError, match="effects section is damaged"):
        StudioProject().open_motion(tmp_path / "bad.motion")
    analysed.bass_wanted = True


def test_command_line_extraction_with_effects(media, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    plain = tmp_path / "plain.motion"
    assert main(["extract", "generate", str(media["stereo"]), "-o", str(plain), "--projection", "360", "-q"]) == 0
    assert "effects: none (motion-only programme)" in capsys.readouterr().out and read_effects(plain) is None
    full = tmp_path / "full.motion"
    assert main(["extract", "generate", str(media["stereo"]), "-o", str(full), "--projection", "360", "-q",
                 "--effects", "suggest", "--bass"]) == 0
    out = capsys.readouterr().out
    assert "Seat vibration" in out and "Strobe / lightning" in out and "bass track: 4.00 s" in out
    doc = read_effects(full)
    assert doc is not None and doc.bass is not None and {t.id for t in doc.tracks} == {"vibration", "strobe"}
    assert np.array_equal(motionfile.read_motion(full).samples, motionfile.read_motion(plain).samples)
    # a hand-made effects file is embedded as it is; the silent video gets no bass and says so
    assert main(["fx", "extract", str(full), "-o", str(tmp_path / "made.fx.json")]) == 0
    capsys.readouterr()
    third = tmp_path / "third.motion"
    assert main(["extract", "generate", str(media["stereo"]), "-o", str(third), "--projection", "360", "-q",
                 "--fx", str(tmp_path / "made.fx.json")]) == 0
    assert read_effects(third).to_dict()["tracks"] == doc.to_dict()["tracks"]
    assert main(["extract", "generate", str(media["silent"]), "-o", str(tmp_path / "s.motion"), "--projection", "360", "-q",
                 "--fx", str(tmp_path / "made.fx.json")]) == 2
    assert "is 4.00 s long, the video 3.00 s" in capsys.readouterr().err
    assert main(["extract", "generate", str(media["silent"]), "-o", str(tmp_path / "s.motion"), "--projection", "360", "-q",
                 "--bass"]) == 0
    out = capsys.readouterr().out
    assert "has no sound track - no bass track" in out and "effects: none" in out


def test_command_line_extraction_without_an_audio_decoder(media, tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An installation that cannot read sound: a bass track asked for by name is an error at once;
    suggestions are made from the picture alone, and the output says so."""
    def no_decoder(*_args: object, **_kw: object) -> None:
        raise st_mod.SoundtrackError(st_mod.NO_DECODER)

    monkeypatch.setattr(st_mod, "available_decoder", lambda: "")
    monkeypatch.setattr("fourdvr_studio.project.load_soundtrack", no_decoder)
    out_file = tmp_path / "n.motion"
    assert main(["extract", "generate", str(media["stereo"]), "-o", str(out_file), "--projection", "360", "-q", "--bass"]) == 2
    captured = capsys.readouterr()
    assert "error: --bass: no audio decoder" in captured.err and "sha256" not in captured.out and not out_file.exists()
    assert main(["extract", "generate", str(media["stereo"]), "-o", str(out_file), "--projection", "360", "-q",
                 "--effects", "suggest"]) == 0
    out = capsys.readouterr().out
    assert "note: no audio decoder" in out and "no sound-based suggestions" in out
    doc = read_effects(out_file)
    assert doc is not None and doc.bass is None and doc.track("strobe") is not None     # the picture flash is still found
