"""Scene detection: the model build tools, the runtime plumbing (with tiny stand-in models), suggestions."""

from __future__ import annotations

import collections
import io
import json
import pickle
import struct
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from fourdvr_extractor import scene as sc
from fourdvr_extractor.suggest import SCENE_SUGGESTABLE, scene_segments, suggest_scene

pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "packaging" / "models")]

import build_models  # noqa: E402

from scene_models import make_models, tiny_state_dict  # noqa: E402


@pytest.fixture(scope="module")
def models(tmp_path_factory) -> Path:  # type: ignore[no-untyped-def]
    return make_models(tmp_path_factory.mktemp("models"))


# ------------------------------------------------------------------------------ build tools
def test_onnx_graph_reproduces_the_numpy_network() -> None:
    import onnxruntime as ort

    sd = tiny_state_dict(width=128, layers=3, size=96)                     # two attention heads, 9 patches
    pixels = build_models.test_picture(96)
    reference = build_models.encode_image(sd, pixels)
    assert reference.shape == (1, 32) and np.linalg.norm(reference) == pytest.approx(1.0, abs=1e-5)
    for half, tolerance in ((False, 2e-5), (True, 3e-3)):
        model = build_models.build_image_onnx(sd, half=half)
        got = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"]).run(None, {"pixels": pixels})[0]
        assert np.abs(got - reference).max() < tolerance, half
    batch = np.concatenate([pixels, pixels[:, :, ::-1].copy()])            # a batch: each picture on its own
    sess = ort.InferenceSession(build_models.build_image_onnx(sd, half=False).SerializeToString(), providers=["CPUExecutionProvider"])
    out = sess.run(None, {"pixels": batch})[0]
    assert np.abs(out[0] - reference[0]).max() < 2e-5 and np.abs(out[1] - out[0]).max() > 1e-3


def _legacy_checkpoint(obj_bytes: bytes, storages: list[np.ndarray]) -> bytes:
    out = io.BytesIO()
    pickle.dump(0x1950A86A20F9469CFC6C, out, protocol=2)
    pickle.dump(1001, out, protocol=2)
    pickle.dump({"little_endian": True, "protocol_version": 1001}, out, protocol=2)
    out.write(obj_bytes)
    pickle.dump([str(i) for i in range(len(storages))], out, protocol=2)
    for st in storages:
        out.write(struct.pack("<q", st.size) + st.tobytes())
    return out.getvalue()


def test_checkpoint_reader_reads_tensors_and_refuses_everything_else(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # a stand-in "torch" that exists only while the test file is written: the reader itself never imports it
    torch = types.ModuleType("torch")
    utils = types.ModuleType("torch._utils")

    class FloatStorage:
        pass

    def _rebuild_tensor_v2(*_a: object) -> None:
        return None

    FloatStorage.__module__, FloatStorage.__qualname__ = "torch", "FloatStorage"
    _rebuild_tensor_v2.__module__, _rebuild_tensor_v2.__qualname__ = "torch._utils", "_rebuild_tensor_v2"
    torch.FloatStorage, utils._rebuild_tensor_v2 = FloatStorage, _rebuild_tensor_v2  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torch._utils", utils)

    class Stored:
        def __init__(self, key: int, numel: int) -> None:
            self.key, self.numel = key, numel

    class Tensor:
        def __init__(self, key: int, shape: tuple[int, ...]) -> None:
            self.key, self.shape = key, shape

        def __reduce__(self):  # type: ignore[no-untyped-def]
            n = int(np.prod(self.shape))
            stride = tuple(int(np.prod(self.shape[i + 1 :])) for i in range(len(self.shape)))
            return _rebuild_tensor_v2, (Stored(self.key, n), 0, self.shape, stride, False, collections.OrderedDict())

    class Writer(pickle.Pickler):
        def persistent_id(self, obj):  # type: ignore[no-untyped-def]
            return ("storage", FloatStorage, str(obj.key), "cpu", obj.numel, None) if isinstance(obj, Stored) else None

    a, b = np.arange(6, dtype=np.float32), np.linspace(-1, 1, 12).astype(np.float32)
    buf = io.BytesIO()
    Writer(buf, protocol=2).dump({"state_dict": collections.OrderedDict(weight=Tensor(0, (2, 3)), bias=Tensor(1, (12,)))})
    path = tmp_path / "ok.pt"
    path.write_bytes(_legacy_checkpoint(buf.getvalue(), [a, b]))
    monkeypatch.delitem(sys.modules, "torch")
    monkeypatch.delitem(sys.modules, "torch._utils")
    sd = build_models.read_checkpoint(path)
    assert list(sd) == ["weight", "bias"] and np.array_equal(sd["weight"], a.reshape(2, 3)) and np.array_equal(sd["bias"], b)
    assert "torch" not in sys.modules
    # a file that tries to run something when it is loaded is refused, and nothing has run
    marker = tmp_path / "ran"

    class Evil:
        def __reduce__(self):  # type: ignore[no-untyped-def]
            return Path.write_text, (marker, "x")

    bad = tmp_path / "bad.pt"
    bad.write_bytes(_legacy_checkpoint(pickle.dumps({"state_dict": Evil()}, protocol=2), []))
    with pytest.raises(pickle.UnpicklingError, match="refused"):
        build_models.read_checkpoint(bad)
    assert not marker.exists()
    (tmp_path / "junk.pt").write_bytes(pickle.dumps("something else"))
    with pytest.raises(ValueError, match="not a PyTorch checkpoint"):
        build_models.read_checkpoint(tmp_path / "junk.pt")


def test_inputs_are_pinned_and_a_wrong_download_is_rejected(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert all(url.startswith("https://") and len(digest) == 64 for url, digest in build_models.INPUTS.values())
    assert not any("/main/" in url or "/master/" in url for url, _d in build_models.INPUTS.values())      # fixed revisions only
    (tmp_path / "icefall-LICENSE.txt").write_text("not the licence")
    with pytest.raises(SystemExit, match="--offline"):
        build_models.fetch(tmp_path, "icefall-LICENSE.txt", offline=True)
    monkeypatch.setattr(build_models.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"tampered"))
    with pytest.raises(SystemExit, match="checksum mismatch"):
        build_models.fetch(tmp_path, "icefall-LICENSE.txt", offline=False)
    assert (tmp_path / "icefall-LICENSE.txt.rejected").exists()
    spec = json.loads((ROOT / "packaging" / "models" / "concepts.json").read_text())
    assert {"rain", "fog", "fire", "bubbles", "snow", "splash", "forest", "sea", "flowers"} <= set(spec["concepts"])
    assert all(p.isascii() and p == p.lower() for c in spec["concepts"].values() for k in ("pos", "contrast") for p in c[k])


# ------------------------------------------------------------------------------ runtime
def test_models_are_found_or_the_reason_is_given(models: Path, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(sc.MODELS_ENV, str(models))
    assert sc.find_models() == (models, "") and sc.availability() == (True, "")
    monkeypatch.setenv(sc.MODELS_ENV, str(tmp_path / "none"))
    monkeypatch.setattr(sc, "user_models_dir", lambda: tmp_path / "none")
    found, why = sc.find_models()
    assert found is None and "not installed" in why and "build_models.py" in why
    old = tmp_path / "old"
    old.mkdir()
    (old / "manifest.json").write_text('{"format": 0}')
    monkeypatch.setenv(sc.MODELS_ENV, str(old))
    assert "another version" in sc.find_models()[1]
    (old / "manifest.json").write_text('{"format": 1}')
    assert "missing" in sc.find_models()[1] and "scene.onnx" in sc.find_models()[1]


def test_forward_view_of_flat_and_panoramic_pictures() -> None:
    frame = np.zeros((180, 360, 3), np.uint8)
    assert sc.forward_view(frame, "flat") is frame
    assert sc.forward_view(frame, "360").shape[:2] == (100, 100)            # 100 x 100 degrees around straight ahead
    frame[40:140, 130:230] = 255
    assert sc.forward_view(frame, "360").min() == 255
    assert sc.forward_view(np.zeros((180, 180, 3), np.uint8), "180").shape[:2] == (100, 100)


def test_picture_tagger_takes_one_picture_per_second(models: Path) -> None:
    tagger = sc.PictureTagger(models, batch=4)
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 255, (120, 240, 3), dtype=np.uint8)
    before = frame.copy()
    for i in range(0, 330):                                                 # 11 s at 30 pictures per second
        tagger.offer(i / 30.0, frame if i % 60 else frame[:, ::-1], "360")
    times, scores = tagger.finish()
    assert np.array_equal(frame, before)                                    # the analysis' picture is not touched
    assert np.allclose(times, np.arange(11)) and set(scores) == set(tagger.concepts) >= {"rain", "fog", "fire"}
    assert all(len(v) == 11 and np.all((v >= 0) & (v <= 1)) for v in scores.values())
    assert tagger.prepare(frame, "360").shape == (3, tagger.size, tagger.size)
    one = tagger.score(tagger.prepare(frame, "flat")[None])
    again = tagger.score(np.stack([tagger.prepare(frame, "flat")] * 3))
    assert all(np.allclose(again[k], one[k][0], atol=1e-5) for k in one)    # the same in a batch as alone


def test_sound_tagger_windows_and_cues(models: Path) -> None:
    tagger = sc.SoundTagger(models)
    t = np.arange(int(9.5 * sc.SOUND_RATE_HZ)) / sc.SOUND_RATE_HZ
    samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    feats = tagger.features(samples)
    assert feats.shape == (950, 80)                                          # 100 frames per second, 80 mel bins
    assert np.allclose(tagger.features(samples[: 3 * sc.SOUND_RATE_HZ])[:290], feats[:290], atol=1e-3)   # the same frames from a part
    seen: list[float] = []
    times, cues = tagger.run(samples, progress=lambda f, _m: seen.append(f))
    assert np.allclose(times, [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]) and set(cues) == set(sc.SOUND_GROUPS)
    assert all(len(v) == 6 and np.all((v > 0) & (v < 1)) for v in cues.values()) and seen and seen[-1] == 1.0
    short_t, short = tagger.run(samples[: 2 * sc.SOUND_RATE_HZ])            # shorter than a window: one value
    assert len(short_t) == 1 and short_t[0] == pytest.approx(1.0) and len(short["rain"]) == 1
    assert len(tagger.run(samples[:2000])[0]) == 0                          # an eighth of a second: nothing
    with pytest.raises(sc.SceneError, match="cancelled"):
        tagger.run(samples, cancel=lambda: True)


def test_scene_round_trip_and_lookup() -> None:
    s = sc.Scene(np.arange(5.0), {"rain": np.array([0, 0.9, 0.95, 0.2, 0])}, np.array([2.0, 3.0]), {"rain": np.array([0.5, 0.1])}, "note")
    back = sc.Scene.from_arrays(s.to_arrays(), "note")
    assert back is not None and np.allclose(back.picture["rain"], s.picture["rain"], atol=1e-3) and back.has_sound and back.note == "note"
    assert np.allclose(s.sound_at("rain", np.array([0.0, 2.0, 2.5, 9.0])), [0.0, 0.5, 0.3, 0.0])
    assert np.allclose(s.picture_at("snow", np.arange(3.0)), 0) and s.summary()["seconds_seen"] == {"rain": 2.0}
    assert sc.Scene.from_arrays({}) is None and not sc.Scene().has_sound


# ------------------------------------------------------------------------------ suggestions
def scene_of(seconds: int, sound: bool = True, **picture: list[tuple[float, float, float]]) -> sc.Scene:
    t = np.arange(float(seconds))
    pic = {}
    for name, spans in picture.items():
        v = np.zeros(seconds)
        for a, b, level in spans:
            v[(t >= a) & (t < b)] = level
        pic[name.removeprefix("p_")] = v
    return sc.Scene(t, pic, t if sound else np.zeros(0), {})


def test_segments_join_drop_and_snap() -> None:
    t = np.arange(20.0)
    active = np.zeros(20, bool)
    active[[2, 3, 4, 7, 8, 15]] = True
    assert scene_segments(t, active, 20.0, 2.0, 2.0) == [(1.5, 8.5)]        # joined across the 2 s gap; the lone second dropped
    assert scene_segments(t, active, 20.0, 1.0, 1.0) == [(1.5, 4.5), (6.5, 8.5), (14.5, 15.5)]
    assert scene_segments(t, active, 20.0, 2.0, 2.0, cuts=[1.2, 9.0]) == [(1.2, 9.0)]
    assert scene_segments(t, np.zeros(20, bool), 20.0, 1.0, 1.0) == [] and scene_segments(np.zeros(0), np.zeros(0, bool), 5.0, 1, 1) == []
    active[:] = False
    active[[0, 19]] = True
    assert scene_segments(t, active, 19.2, 0.5, 0.5) == [(0.0, 0.5), (18.5, 19.2)]   # kept inside the programme


def test_scene_suggestions_follow_the_evidence() -> None:
    s = scene_of(200, p_rain=[(10, 20, 0.95), (40, 46, 0.5), (60, 61, 0.9)], p_fog=[(100, 150, 0.9)], p_fire=[(70, 72, 0.95), (80, 83, 0.5)],
                 p_bubbles=[(160, 170, 0.9)], p_snow=[(180, 190, 0.85), (192, 193, 0.9)])
    s.sound = {"rain": np.where((s.times >= 40) & (s.times < 46), 0.5, 0.0) + np.where((s.times >= 10) & (s.times < 20), 0.6, 0.0),
               "explosion": np.where((s.times >= 80) & (s.times < 83), 0.4, 0.0)}
    by_type = {x.type: x for x in suggest_scene(s, 200.0)}
    assert set(by_type) == {"rain", "fog", "heat", "bubbles", "snow"} <= set(SCENE_SUGGESTABLE)
    rain = by_type["rain"].cues                                              # seen; weakly seen but heard; a single second is not rain
    assert [(c.t, c.duration) for c in rain] == [(9.5, 10.0), (39.5, 6.0)] and rain[0].level == 1.0 and rain[0].fade_in == 0.5
    fog = by_type["fog"].cues                                                # a burst at the start, then every 20 s while it lasts
    assert [(c.t, c.duration) for c in fog] == [(99.5, 3.0), (119.5, 3.0), (139.5, 3.0)]
    assert [(c.t, c.duration) for c in by_type["heat"].cues] == [(69.5, 2.0), (79.5, 3.0)]   # fire seen; fire guessed and an explosion heard
    assert [(c.t, c.duration, c.level) for c in by_type["bubbles"].cues] == [(159.5, 10.0, 0.8)]
    assert [(c.t, c.duration) for c in by_type["snow"].cues] == [(179.5, 13.0)]      # joined across the two-second gap
    silent = scene_of(60, sound=False, p_rain=[(10, 20, 0.95), (40, 46, 0.5)], p_splash=[(30, 39, 0.9)])
    by_type = {x.type: x for x in suggest_scene(silent, 60.0)}
    assert [(c.t, c.level) for c in by_type["rain"].cues] == [(9.5, 0.7)]    # no sound: only what is clearly seen, at a middle level
    assert [(c.t, c.duration) for c in by_type["water"].cues] == [(29.5, 0.5), (33.5, 0.5), (37.5, 0.5)]
    heard = scene_of(60, p_splash=[(30, 39, 0.9)])                           # with a sound track, a splash must also be heard
    assert "water" not in {x.type for x in suggest_scene(heard, 60.0)}
    heard.sound = {"splash": np.where((heard.times >= 30) & (heard.times < 33), 0.3, 0.0)}
    assert [(c.t, c.duration) for c in suggest_scene(heard, 60.0)[0].cues] == [(29.5, 0.5)]
    assert suggest_scene(sc.Scene(), 10.0) == []


def test_scent_is_released_once_per_scene_and_not_too_often() -> None:
    s = scene_of(300, p_forest=[(10, 30, 0.9), (50, 70, 0.9), (150, 170, 0.9), (200, 203, 0.9)], p_sea=[(60, 80, 0.95)], p_flowers=[(100, 104, 0.9)])
    (scent,) = suggest_scene(s, 300.0)
    assert scent.type == "scent"
    assert [(c.t, c.params["slot"], c.label) for c in scent.cues] == [(9.5, 1.0, "forest"), (59.5, 2.0, "sea"), (149.5, 1.0, "forest")]
    assert all(c.duration == 3.0 for c in scent.cues)                        # short forest at 200 s and flowers at 100 s: under 5 s


def test_onnx_runtime_is_loaded_with_its_telemetry_off() -> None:
    """ONNX Runtime 1.29+ can report usage to its maker; this program must send nothing anywhere."""
    import os

    assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"                     # set when fourdvr_extractor.scene is imported
    os.environ["ORT_DISABLE_TELEMETRY"] = "0"
    ort = sc._onnxruntime()
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1" and sc.availability()[1] != "the onnxruntime package is not installed"
    release = tuple(int(x) for x in ort.__version__.split(".")[:2])
    assert release < (1, 29), "pyproject.toml asks for an ONNX Runtime without the telemetry client (below 1.29)"
