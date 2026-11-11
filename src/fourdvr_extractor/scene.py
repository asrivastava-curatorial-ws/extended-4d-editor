"""Scene detection: what the picture and the sound of a programme show - rain, fog or smoke, fire, an
underwater world, snow, splashing water, forest, sea, flowers - as the basis for effect suggestions.

Two open models do the looking and the listening, both on this computer, without any network access:

* a picture model (CLIP ViT-B/32) compares one picture per second with short descriptions ("heavy
  rain falling", "a building on fire", ...) and with neutral ones; the share of a concept's
  descriptions in that comparison is its score, 0..1;
* a sound model (an AudioSet tagger) gives, second by second, the probability of sounds such as rain,
  thunder, fire, an explosion, splashing water.

The model files are not part of the Python package (they are about 200 MB): the AppImage carries them,
other installations build them once with ``packaging/models/build_models.py``. Without them - or
without the ``onnxruntime`` package - everything else works and these scenes are simply not detected.

Scores are evidence, not facts: a suggestion made from them is a draft for a person to review.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

MODELS_ENV = "FOURDVR_MODELS"
ORT_TELEMETRY_OFF = "ORT_DISABLE_TELEMETRY"
os.environ[ORT_TELEMETRY_OFF] = "1"     # before anything can load ONNX Runtime: see _onnxruntime()
MODEL_FORMAT = 1
PICTURE_INTERVAL_S = 1.0       # one picture per second is enough for conditions that last seconds
SOUND_WINDOW_S = 4.0
SOUND_HOP_S = 1.0
SOUND_RATE_HZ = 16000
FORWARD_VIEW_DEG = 100.0       # 180/360 video: the part of the picture the visitor mostly looks at

# AudioSet classes behind each sound cue (the largest probability of the group counts)
SOUND_GROUPS: dict[str, tuple[str, ...]] = {
    "rain": ("Rain", "Raindrop", "Rain on surface"),
    "thunder": ("Thunderstorm", "Thunder"),
    "fire": ("Fire", "Crackle"),
    "explosion": ("Explosion", "Boom"),
    "splash": ("Splash, splatter", "Spray", "Waterfall", "Waves, surf"),
    "sea": ("Ocean", "Waves, surf"),
    "bubbles": ("Gurgling", "Boiling"),
    "wind": ("Wind", "Wind noise (microphone)"),
}

ProgressFn = Callable[[float, str], None]


class SceneError(RuntimeError):
    """Scene detection is not available or failed (the message says why)."""


def user_models_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Extended 4D Editor" / "models"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Extended 4D Editor" / "models"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "extended-4d-editor" / "models"


def find_models() -> tuple[Path | None, str]:
    """The directory with the model files, or (None, why there is none)."""
    candidates = [Path(os.environ[MODELS_ENV])] if os.environ.get(MODELS_ENV) else []
    candidates += [Path(__file__).resolve().parent / "models", user_models_dir()]
    for d in candidates:
        manifest = d / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        if data.get("format") != MODEL_FORMAT:
            return None, f"the model files in {d} are of another version: build them again (packaging/models/build_models.py)"
        missing = [n for n in ("scene.onnx", "concepts.npz", "sound.onnx", "sound-labels.csv") if not (d / n).is_file()]
        if missing:
            return None, f"model files missing in {d}: {', '.join(missing)}"
        return d, ""
    return None, "scene models not installed (the AppImage has them; otherwise run packaging/models/build_models.py)"


def _onnxruntime() -> Any:
    """ONNX Runtime, with its telemetry off.

    From version 1.29 the Linux builds of ONNX Runtime contain a client that reports usage events to its
    maker whenever a network is there. This program sends nothing anywhere: it asks for an older version
    (pyproject.toml; the AppImage has 1.28) and - should a newer one be installed all the same - sets
    ORT_DISABLE_TELEMETRY=1 before the library is loaded, which keeps that client from being created."""
    os.environ[ORT_TELEMETRY_OFF] = "1"
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise SceneError('the onnxruntime package is not installed (pip install "onnxruntime<1.29")') from exc
    switch = getattr(ort, "disable_telemetry_events", None)
    if callable(switch):
        with contextlib.suppress(Exception):
            switch()
    return ort


def _session(path: Path) -> Any:
    ort = _onnxruntime()
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def availability() -> tuple[bool, str]:
    """(True, "") when scenes can be detected on this installation, else (False, the reason)."""
    directory, why = find_models()
    if directory is None:
        return False, why
    try:
        import kaldi_native_fbank  # noqa: F401

        _onnxruntime()
    except ImportError as exc:
        return False, f'the {exc.name} package is not installed (pip install "onnxruntime<1.29" kaldi-native-fbank)'
    except SceneError as exc:
        return False, str(exc)
    return True, ""


@dataclass
class Scene:
    """Scores along the programme. `picture`: concept -> one value per second; `sound`: cue -> one per second."""

    times: np.ndarray = field(default_factory=lambda: np.zeros(0))
    picture: dict[str, np.ndarray] = field(default_factory=dict)
    sound_times: np.ndarray = field(default_factory=lambda: np.zeros(0))
    sound: dict[str, np.ndarray] = field(default_factory=dict)
    note: str = ""

    @property
    def has_sound(self) -> bool:
        return len(self.sound_times) > 0

    def picture_at(self, concept: str, times: np.ndarray) -> np.ndarray:
        if concept not in self.picture or not len(self.times):
            return np.zeros(len(times))
        return np.interp(times, self.times, self.picture[concept])

    def sound_at(self, cue: str, times: np.ndarray) -> np.ndarray:
        if cue not in self.sound or not len(self.sound_times):
            return np.zeros(len(times))
        return np.interp(times, self.sound_times, self.sound[cue], left=0.0, right=0.0)

    def summary(self) -> dict[str, object]:
        def seconds(values: np.ndarray, step: float) -> float:
            return round(float(np.sum(values >= 0.8)) * step, 1)

        return {
            "pictures": len(self.times),
            "seconds_seen": {k: seconds(v, PICTURE_INTERVAL_S) for k, v in self.picture.items() if np.any(v >= 0.8)},
            "seconds_heard": {k: round(float(np.sum(v >= 0.3)) * SOUND_HOP_S, 1) for k, v in self.sound.items() if np.any(v >= 0.3)},
            "sound": self.has_sound,
        }

    def to_arrays(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {"scene_times": self.times.astype(np.float32), "scene_sound_times": self.sound_times.astype(np.float32)}
        out.update({f"scene_p_{k}": v.astype(np.float16) for k, v in self.picture.items()})
        out.update({f"scene_s_{k}": v.astype(np.float16) for k, v in self.sound.items()})
        return out

    @classmethod
    def from_arrays(cls, arrays: dict[str, np.ndarray], note: str = "") -> Scene | None:
        if "scene_times" not in arrays:
            return None
        return cls(
            np.asarray(arrays["scene_times"], np.float64),
            {k[len("scene_p_") :]: np.asarray(v, np.float64) for k, v in arrays.items() if k.startswith("scene_p_")},
            np.asarray(arrays.get("scene_sound_times", np.zeros(0)), np.float64),
            {k[len("scene_s_") :]: np.asarray(v, np.float64) for k, v in arrays.items() if k.startswith("scene_s_")},
            note,
        )


def forward_view(frame: np.ndarray, projection: str) -> np.ndarray:
    """The part of a (left-eye) frame to look at: all of a flat picture, the forward view of a 180/360 one."""
    if projection not in ("180", "360"):
        return frame
    h, w = frame.shape[:2]
    cw = w * FORWARD_VIEW_DEG / (360.0 if projection == "360" else 180.0)
    ch = h * FORWARD_VIEW_DEG / 180.0
    x0, y0 = round((w - cw) / 2), round((h - ch) / 2)
    return frame[y0 : y0 + max(8, round(ch)), x0 : x0 + max(8, round(cw))]


class PictureTagger:
    """Scores one picture per `interval_s` against the concepts. Feed every decoded frame to `offer`."""

    def __init__(self, models: Path, interval_s: float = PICTURE_INTERVAL_S, batch: int = 8) -> None:
        data = np.load(models / "concepts.npz")
        self.size = int(data["picture_size"])
        self.mean, self.std = data["mean"].astype(np.float32), data["std"].astype(np.float32)
        spec = json.loads(str(data["spec"]))
        common = data["common"]
        self.concepts: dict[str, tuple[np.ndarray, int]] = {}
        for name in spec["concepts"]:
            pos = data[f"pos:{name}"]
            self.concepts[name] = (np.concatenate([pos, data[f"contrast:{name}"], common]).astype(np.float32), len(pos))
        self.session = _session(models / "scene.onnx")
        self.interval_s, self.batch = float(interval_s), int(batch)
        self._next = 0.0
        self._pending: list[np.ndarray] = []
        self._times: list[float] = []
        self._scores: dict[str, list[float]] = {name: [] for name in self.concepts}

    def prepare(self, frame_bgr: np.ndarray, projection: str) -> np.ndarray:
        import cv2

        view = forward_view(frame_bgr, projection)
        h, w = view.shape[:2]
        scale = self.size / min(h, w)
        nw, nh = max(self.size, round(w * scale)), max(self.size, round(h * scale))
        img = cv2.resize(view, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        y0, x0 = (nh - self.size) // 2, (nw - self.size) // 2
        rgb = img[y0 : y0 + self.size, x0 : x0 + self.size, ::-1].astype(np.float32) / 255.0
        return np.ascontiguousarray(((rgb - self.mean) / self.std).transpose(2, 0, 1))

    def score(self, pixels: np.ndarray) -> dict[str, np.ndarray]:
        """pixels: (N, 3, S, S), prepared. Returns concept -> (N,) scores."""
        emb = self.session.run(None, {"pixels": pixels.astype(np.float32)})[0]
        out = {}
        for name, (text, n_pos) in self.concepts.items():
            logits = 100.0 * emb @ text.T
            e = np.exp(logits - logits.max(axis=1, keepdims=True))
            out[name] = e[:, :n_pos].sum(axis=1) / e.sum(axis=1)
        return out

    def offer(self, t: float, frame_bgr: np.ndarray, projection: str) -> None:
        if t + 1e-6 < self._next:
            return
        self._next = t + self.interval_s
        self._times.append(float(t))
        self._pending.append(self.prepare(frame_bgr, projection))
        if len(self._pending) >= self.batch:
            self._flush()

    def _flush(self) -> None:
        if self._pending:
            for name, values in self.score(np.stack(self._pending)).items():
                self._scores[name].extend(float(v) for v in values)
            self._pending = []

    def finish(self) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        self._flush()
        return np.array(self._times), {k: np.array(v) for k, v in self._scores.items()}


class SoundTagger:
    """Probabilities of the sound cues (SOUND_GROUPS) in windows of 4 s, every second."""

    def __init__(self, models: Path) -> None:
        with open(models / "sound-labels.csv", newline="", encoding="utf-8") as f:
            labels = [row["display_name"] for row in csv.DictReader(f)]
        self.groups = {cue: [labels.index(n) for n in names if n in labels] for cue, names in SOUND_GROUPS.items()}
        self.session = _session(models / "sound.onnx")

    @staticmethod
    def features(samples: np.ndarray) -> np.ndarray:
        """80-bin log-mel filterbank frames (100 per second) of a 16 kHz signal in -1..1."""
        try:
            import kaldi_native_fbank as knf
        except ImportError as exc:
            raise SceneError("the kaldi-native-fbank package is not installed (pip install kaldi-native-fbank)") from exc
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = SOUND_RATE_HZ
        opts.frame_opts.dither = 0.0
        opts.frame_opts.snip_edges = False
        opts.mel_opts.num_bins = 80
        opts.mel_opts.high_freq = -400
        bank = knf.OnlineFbank(opts)
        frames: list[np.ndarray] = []
        chunk = SOUND_RATE_HZ * 10
        for i in range(0, len(samples), chunk):
            bank.accept_waveform(SOUND_RATE_HZ, samples[i : i + chunk].astype(np.float32).tolist())
            while len(frames) < bank.num_frames_ready:
                frames.append(np.array(bank.get_frame(len(frames)), np.float32))
        bank.input_finished()
        while len(frames) < bank.num_frames_ready:
            frames.append(np.array(bank.get_frame(len(frames)), np.float32))
        return np.stack(frames) if frames else np.zeros((0, 80), np.float32)

    def run(
        self, samples: np.ndarray, progress: ProgressFn | None = None, cancel: Callable[[], bool] | None = None
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        feats = self.features(samples)
        win, hop = round(SOUND_WINDOW_S * 100), round(SOUND_HOP_S * 100)
        if len(feats) < 50:                                   # under half a second: nothing to say
            return np.zeros(0), {cue: np.zeros(0) for cue in self.groups}
        starts = list(range(0, max(1, len(feats) - win + 1), hop)) if len(feats) >= win else [0]
        times = np.array([(s + min(win, len(feats)) / 2) / 100.0 for s in starts])
        probs: list[np.ndarray] = []
        for i in range(0, len(starts), 16):
            if cancel is not None and cancel():
                raise SceneError("cancelled")
            batch = np.stack([feats[s : s + win] for s in starts[i : i + 16]])
            lens = np.full(len(batch), batch.shape[1], np.int64)
            probs.append(self.session.run(None, {"x": batch, "x_lens": lens})[0])
            if progress is not None:
                progress(min(1.0, (i + 16) / len(starts)), "listening to the sound track")
        p = np.concatenate(probs)
        return times, {cue: (p[:, idx].max(axis=1) if idx else np.zeros(len(p))) for cue, idx in self.groups.items()}


def detect_sound(models: Path, path: str | Path, progress: ProgressFn | None = None,
                 cancel: Callable[[], bool] | None = None) -> tuple[np.ndarray, dict[str, np.ndarray], str]:
    """Sound cues of a media file: (times, cue -> probability, note). No sound track: empty result."""
    from .soundtrack import SoundtrackError, decode_mono

    try:
        samples = decode_mono(path, SOUND_RATE_HZ, cancel)
    except SoundtrackError as exc:
        return np.zeros(0), {}, str(exc)
    if samples is None or not len(samples):
        return np.zeros(0), {}, "no sound track"
    times, cues = SoundTagger(models).run(samples, progress, cancel)
    return times, cues, ""
