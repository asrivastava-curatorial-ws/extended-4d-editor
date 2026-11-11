"""Scene detection as part of a project's analysis (stand-in models, see tests/scene_models.py)."""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pytest

from fourdvr_extractor import scene as sc
from fourdvr_studio.project import StudioProject

pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scene_models import make_models


@pytest.fixture(scope="module")
def models(tmp_path_factory) -> Path:  # type: ignore[no-untyped-def]
    return make_models(tmp_path_factory.mktemp("models"))


def test_project_detects_scenes_without_touching_the_motion_analysis(models: Path, media, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    video = media["stereo"]                                                  # 4 s, with a sound track
    monkeypatch.setenv(sc.MODELS_ENV, str(models))
    with_scenes = StudioProject()
    with_scenes.open_video(video)
    seen: list[tuple[float, str]] = []
    with_scenes.analyse(lambda f, m: seen.append((f, m)))
    scene = with_scenes.scene
    assert scene is not None and with_scenes.scene_note == "" and np.allclose(scene.times, np.arange(4))
    assert scene.has_sound and len(scene.sound_times) == 1 and set(scene.sound) == set(sc.SOUND_GROUPS)
    assert seen[-1] == (1.0, "analysis complete") and all(b[0] >= a[0] - 1e-9 for a, b in itertools.pairwise(seen))
    plain = StudioProject()
    plain.detect_scenes = False
    plain.open_video(video)
    plain.analyse()
    assert plain.scene is None and "switched off" in plain.scene_note
    a, b = with_scenes.analysis, plain.analysis
    assert a is not None and b is not None
    for name in ("times", "angular_velocity", "velocity", "confidence", "parallax", "tracked_points", "luma"):
        assert np.array_equal(getattr(a, name), getattr(b, name)), name      # bit for bit the same motion analysis
    assert a.cuts == b.cuts
    # the scores travel with the project file
    with_scenes.generate()
    with_scenes.process()
    again = StudioProject.load(with_scenes.save(tmp_path / "ride.4dvr"))
    assert again.scene is not None and np.allclose(again.scene.picture["rain"], scene.picture["rain"], atol=1e-3)
    assert np.allclose(again.scene.sound["rain"], scene.sound["rain"], atol=1e-3)
    # a silent video: the pictures are scored, the sound is not there
    silent = StudioProject()
    silent.open_video(media["silent"])
    silent.analyse()
    assert silent.scene is not None and not silent.scene.has_sound and len(silent.scene.times) == 3
    # no models: the note says so and everything else works
    monkeypatch.setenv(sc.MODELS_ENV, str(tmp_path / "nowhere"))
    monkeypatch.setattr(sc, "user_models_dir", lambda: tmp_path / "nowhere")
    without = StudioProject()
    without.open_video(video)
    without.analyse()
    assert without.scene is None and "not installed" in without.scene_note
    without.generate()
    without.process()
    assert "rain" not in without.suggest() and "vibration" in without.suggest()
    # a model file that cannot be loaded is a note, not a failure
    broken = make_models(tmp_path / "broken")
    (broken / "scene.onnx").write_bytes(b"not a model")
    monkeypatch.setenv(sc.MODELS_ENV, str(broken))
    damaged = StudioProject()
    damaged.open_video(video)
    damaged.analyse()
    assert damaged.scene is None and "not available" in damaged.scene_note and damaged.analysis is not None
