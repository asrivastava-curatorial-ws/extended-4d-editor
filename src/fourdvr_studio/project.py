"""A Editor project: one programme in the making.

It holds the video analysis, the motion (automatic cues plus manual edits), the effects document
and the venue settings used for live testing, and it knows how to save itself (``.4dvr``, JSON) and
how to export the finished programme: ONE ``.motion`` file with the effects embedded - or a plain
motion-only file when the programme has no effects.

Two kinds of project:

* **video** - made from a video file: analyse -> generate -> (edit) -> process -> validate -> export;
* **motion** - an existing ``.motion`` file opened to add or change its effects; the motion samples
  are copied bit-exactly on export.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from fourdvr_extractor import scene as scene_mod
from fourdvr_extractor.analysis import Analysis
from fourdvr_extractor.manual import MotionEdits
from fourdvr_extractor.pipeline import DEFAULT_PROFILE, ExtractionProject, ExtractionSettings
from fourdvr_extractor.scene import Scene
from fourdvr_extractor.soundtrack import Soundtrack, SoundtrackError, load_soundtrack, make_bass_track
from fourdvr_extractor.suggest import Suggestion, apply_suggestions, suggest_all
from fourdvr_extractor.validation import ValidationReport
from fourdvr_extractor.video import VideoInfo
from fourdvr_fx import BassTrack, EffectsDocument
from fourdvr_fx.embed import effects_from_header, embed_into_motion
from fourdvr_motion import motionfile

from . import PRODUCT_NAME, __version__

PROJECT_FORMAT = "4D-VR-THEATRE-STUDIO-PROJECT"
PROJECT_VERSION = 1
PROJECT_SUFFIX = ".4dvr"
ANALYSIS_ARRAYS = ("times", "angular_velocity", "velocity", "confidence", "parallax", "tracked_points", "luma")

ProgressFn = Callable[[float, str], None]


class ProjectError(ValueError):
    """The project file cannot be used."""


def _pack_analysis(a: Analysis) -> dict[str, Any]:
    buf = io.BytesIO()
    arrays = {k: np.asarray(getattr(a, k), dtype=np.float32 if k != "times" else np.float64) for k in ANALYSIS_ARRAYS}
    np.savez_compressed(buf, **arrays)  # type: ignore[arg-type]  # numpy's stub types the keyword arrays as bool
    return {"npz": base64.b64encode(buf.getvalue()).decode("ascii"), "cuts": [round(c, 4) for c in a.cuts],
            "frame_rate": a.frame_rate, "settings": a.settings}


def _pack_scene(scene: Scene) -> dict[str, Any]:
    buf = io.BytesIO()
    np.savez_compressed(buf, **scene.to_arrays())  # type: ignore[arg-type]
    return {"npz": base64.b64encode(buf.getvalue()).decode("ascii"), "note": scene.note}


def _unpack_scene(d: dict[str, Any]) -> Scene | None:
    with np.load(io.BytesIO(base64.b64decode(d["npz"])), allow_pickle=False) as z:
        return Scene.from_arrays({k: z[k] for k in z.files}, str(d.get("note", "")))


def _unpack_analysis(d: dict[str, Any]) -> Analysis:
    with np.load(io.BytesIO(base64.b64decode(d["npz"])), allow_pickle=False) as z:
        arrays = {k: np.asarray(z[k], dtype=np.float64) for k in ANALYSIS_ARRAYS}
    n = len(arrays["times"])
    if any(len(v) != n for v in arrays.values()):
        raise ProjectError("stored analysis is inconsistent")
    return Analysis(cuts=[float(c) for c in d.get("cuts", [])], frame_rate=float(d.get("frame_rate", 30.0)),
                    settings=dict(d.get("settings", {})), **arrays)


class StudioProject:
    def __init__(self) -> None:
        self.extraction = ExtractionProject(ExtractionSettings())
        self.effects = EffectsDocument(1.0)
        self.soundtrack: Soundtrack | None = None
        self.soundtrack_note = ""              # why there is no sound track / bass (shown in the UI)
        self.scene: Scene | None = None        # what the picture and the sound show (rain, fog, fire ...)
        self.scene_note = ""                   # why scenes were not (fully) detected
        self.detect_scenes = True              # switch for the analysis; without the models it has no effect
        self.suggestions: list[Suggestion] = []
        self.motion_file: motionfile.MotionFile | None = None
        self.motion_path: Path | None = None
        self.path: Path | None = None          # the .4dvr file
        self.profile_path = ""                 # "" = the bundled example profile
        self.patch_path = ""
        self.platform_ids: list[str] = ["PLATFORM-01"]
        self.bass_wanted = True
        self.bass_cutoff_hz = 120.0
        self.automatic_motion = True           # False: hand-made motion only (no camera-motion cues)
        self._parked_bass: BassTrack | None = None

    # ------------------------------------------------------------------ state
    @property
    def mode(self) -> str:
        if self.motion_file is not None:
            return "motion"
        return "video" if self.extraction.video is not None else "empty"

    @property
    def video(self) -> VideoInfo | None:
        return self.extraction.video

    @property
    def analysis(self) -> Analysis | None:
        return self.extraction.analysis

    @property
    def edits(self) -> MotionEdits:
        return self.extraction.edits

    @edits.setter
    def edits(self, value: MotionEdits) -> None:
        self.extraction.edits = value

    @property
    def duration_s(self) -> float:
        if self.motion_file is not None:
            return float(self.motion_file.duration_s)
        return float(self.extraction.video.duration_s) if self.extraction.video is not None else 0.0

    @property
    def sample_rate_hz(self) -> float:
        if self.motion_file is not None:
            return float(self.motion_file.sample_rate_hz)
        return float(self.extraction.settings.sample_rate_hz)

    @property
    def motion(self) -> np.ndarray | None:
        """The motion to show: the finished track, else the (edited) raw cues, else nothing."""
        if self.motion_file is not None:
            return np.asarray(self.motion_file.samples, dtype=np.float64)
        ex = self.extraction
        return ex.motion if ex.motion is not None else ex.raw

    @property
    def motion_is_final(self) -> bool:
        return self.motion_file is not None or self.extraction.motion is not None

    def title(self) -> str:
        if self.path is not None:
            return self.path.name
        if self.motion_path is not None:
            return self.motion_path.name
        return Path(self.extraction.video.path).name if self.extraction.video is not None else "untitled"

    # ------------------------------------------------------------------ opening
    def open_video(self, path: str | Path, with_hash: bool = True) -> None:
        self.extraction.open_video(path, with_hash=with_hash)
        self.motion_file, self.motion_path = None, None
        self.extraction.edits = MotionEdits()
        self.effects = self._new_effects(self.duration_s)
        self.soundtrack, self.soundtrack_note, self.suggestions = None, "", []
        self.scene, self.scene_note = None, ""

    def open_motion(self, path: str | Path) -> None:
        mf = motionfile.read_motion(path, verify=True)
        try:
            doc = effects_from_header(mf.header)
        except ValueError as exc:
            raise ProjectError(f"{Path(path).name}: the effects section is damaged ({exc}); "
                               "'fourdvr fx strip' removes it") from exc
        self.extraction = ExtractionProject(self.extraction.settings, self.extraction.profile)
        self.motion_file, self.motion_path = mf, Path(path)
        self.effects = doc if doc is not None else self._new_effects(float(mf.duration_s))
        self.effects.duration_s = float(mf.duration_s)
        self.soundtrack, self.soundtrack_note, self.suggestions = None, "", []
        self.scene, self.scene_note = None, ""

    def _new_effects(self, duration_s: float) -> EffectsDocument:
        return EffectsDocument(max(duration_s, 0.01), meta={"generator": PRODUCT_NAME, "generator_version": __version__})

    def load_profile(self, path: str | Path) -> None:
        self.extraction.load_profile(path)
        self.profile_path = str(path)

    # ------------------------------------------------------------------ stages
    def analyse(self, progress: ProgressFn | None = None, cancel: Callable[[], bool] | None = None) -> None:
        """Video analysis (camera motion; the scenes on the way), then the sound track.

        A missing sound track, missing scene models or a failure of scene detection are not errors:
        the notes say what is missing, and the motion analysis is the same with or without them."""
        def part(lo: float, hi: float) -> ProgressFn:
            return lambda f, m: progress(lo + (hi - lo) * f, m) if progress is not None else None

        self.scene, self.scene_note = None, ""
        tagger, models = None, None
        if self.detect_scenes:
            models, self.scene_note = scene_mod.find_models()
            if models is not None:
                try:
                    tagger = scene_mod.PictureTagger(models)
                except Exception as exc:  # noqa: BLE001 - a broken model file must not stop the motion analysis
                    self.scene_note = f"scene detection is not available: {exc}"
        else:
            self.scene_note = "scene detection is switched off"
        projection = self.extraction.video.projection if self.extraction.video is not None else "flat"
        failed: list[str] = []

        def tap(t: float, frame: np.ndarray) -> None:
            if tagger is not None and not failed:
                try:
                    tagger.offer(t, frame, projection)
                except Exception as exc:  # noqa: BLE001 - nor may a failure while it runs
                    failed.append(str(exc))

        self.extraction.analyse(part(0.0, 0.92), cancel, tap if tagger is not None else None)
        self.read_soundtrack(progress=part(0.92, 0.95), cancel=cancel)
        if tagger is not None and models is not None:
            try:
                if failed:
                    raise scene_mod.SceneError(failed[0])
                times, picture = tagger.finish()
                self.scene = Scene(times, picture)
                video = self.extraction.video
                if video is not None and self.soundtrack is not None:
                    s_times, cues, note = scene_mod.detect_sound(models, video.path, part(0.95, 1.0), cancel)
                    self.scene.sound_times, self.scene.sound, self.scene.note = s_times, cues, note
            except Exception as exc:  # noqa: BLE001 - reported in the notes; the analysis itself is complete
                if self.scene is None:
                    self.scene_note = f"scene detection failed: {exc}"
                else:
                    self.scene.note = f"the sound was not analysed: {exc}"
        if progress is not None:
            progress(1.0, "analysis complete")

    def read_soundtrack(self, source: str | Path | None = None, progress: ProgressFn | None = None,
                        cancel: Callable[[], bool] | None = None, rebuild_bass: bool = True) -> None:
        """Decode the programme's audio (the video, or another media file) and rebuild the bass track.

        With rebuild_bass=False the sound track is only read for display and suggestions; the bass track
        already in the effects document (from a project or .motion file) is kept as it is.
        """
        target = source if source is not None else (self.extraction.video.path if self.extraction.video else None)
        if target is None:
            self.soundtrack, self.soundtrack_note = None, "no media file to read audio from"
            return
        try:
            self.soundtrack = load_soundtrack(target, progress=progress, cancel=cancel)
            self.soundtrack_note = "" if self.soundtrack is not None else f"{Path(target).name} has no sound track"
        except SoundtrackError as exc:
            self.soundtrack, self.soundtrack_note = None, str(exc)
        if rebuild_bass:
            self.refresh_bass()

    def refresh_bass(self) -> None:
        """(Re)build effects.bass from the sound track according to bass_wanted / bass_cutoff_hz."""
        if self.soundtrack is None:
            # no sound to build from: the bass track that came with an opened .motion file or project
            # is switched off and on again, never rebuilt (and never lost)
            if not self.bass_wanted:
                self._parked_bass = self.effects.bass or self._parked_bass
                self.effects.bass = None
            elif self.effects.bass is None:
                self.effects.bass = self._parked_bass
            return
        self.effects.bass = None
        if self.bass_wanted:
            self.effects.bass = make_bass_track(self.soundtrack, self.duration_s, self.bass_cutoff_hz)
            if self.effects.bass is None:
                self.soundtrack_note = "the sound track has no low-frequency content worth a bass track"

    def generate(self, automatic: bool = True) -> None:
        self.automatic_motion = automatic
        if automatic:
            self.extraction.generate()
        else:
            self.extraction.generate_blank()

    def process(self) -> None:
        self.extraction.process()

    def validate(self) -> ValidationReport:
        return self.extraction.validate()

    def suggest(self, types: list[str] | None = None) -> list[str]:
        """Draft effect tracks from the analysis; replaces the content of the tracks named after each type."""
        ex = self.extraction
        self.suggestions = suggest_all(ex.analysis, ex.events, self.soundtrack, self.duration_s, self.scene)
        return apply_suggestions(self.effects, self.suggestions, types)

    # ------------------------------------------------------------------ export
    def effects_for_export(self) -> EffectsDocument | None:
        doc = self.effects.copy()
        doc.duration_s = self.duration_s
        for track in doc.tracks:
            track.sort()
        doc.tracks = [t for t in doc.tracks if not t.is_empty()]
        doc.meta = {**doc.meta, "generator": PRODUCT_NAME, "generator_version": __version__}
        return None if doc.is_empty() else doc

    def export(self, path: str | Path, allow_violations: bool = False) -> str:
        """Write the programme. Returns the SHA-256 of the file (the Control Server shows the same value)."""
        doc = self.effects_for_export()
        if self.motion_file is not None:
            assert self.motion_path is not None
            return embed_into_motion(self.motion_path, doc, path)
        return self.extraction.export(path, allow_violations=allow_violations, effects=doc)

    # ------------------------------------------------------------------ project file
    def to_dict(self) -> dict[str, Any]:
        ex = self.extraction
        base = self.path.parent if self.path is not None else None

        def ref(p: str | Path | None) -> dict[str, str] | None:
            if not p:
                return None
            absolute = Path(p).resolve()
            out = {"path": str(absolute)}
            if base is not None:
                try:
                    out["relative"] = str(absolute.relative_to(base.resolve()))
                except ValueError:
                    pass
            return out

        data: dict[str, Any] = {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "saved_with": f"{PRODUCT_NAME} {__version__}",
            "video": None,
            "motion_file": ref(self.motion_path),
            "profile": ref(self.profile_path),
            "patch": ref(self.patch_path),
            "platforms": list(self.platform_ids),
            "settings": ex.settings.to_dict(),
            "automatic_motion": self.automatic_motion,
            "edits": ex.edits.to_dict(),
            "bass": {"wanted": self.bass_wanted, "cutoff_hz": self.bass_cutoff_hz},
            "effects": self.effects.to_dict(),
        }
        if ex.video is not None:
            data["video"] = {**(ref(ex.video.path) or {}), "sha256": ex.video.sha256, "duration_s": ex.video.duration_s}
            if ex.analysis is not None:
                data["analysis"] = _pack_analysis(ex.analysis)
                if self.scene is not None:
                    data["scene"] = _pack_scene(self.scene)
        return data

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ProjectError("no file name")
        if target.suffix != PROJECT_SUFFIX:
            target = target.with_name(target.name + PROJECT_SUFFIX)
        self.path = target
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> StudioProject:
        """Open a project. The media it refers to must still be where it was (or next to the project)."""
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProjectError(f"{p.name} is not a Editor project ({exc})") from exc
        if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
            raise ProjectError(f"{p.name} is not a Editor project")
        if int(data.get("version", 0)) > PROJECT_VERSION:
            raise ProjectError(f"{p.name} was saved by a newer version ({data.get('saved_with', '?')})")

        def find(ref: dict[str, str] | None, what: str) -> Path | None:
            if not ref:
                return None
            for candidate in (p.parent / ref.get("relative", "\0"), Path(ref.get("path", "\0")), p.parent / Path(ref.get("path", "x")).name):
                try:
                    if candidate.is_file():
                        return candidate
                except (OSError, ValueError):
                    continue
            raise ProjectError(f"{what} not found: {ref.get('path')} (put it next to {p.name} or restore the path)")

        proj = cls()
        proj.path = p
        proj.extraction.settings = ExtractionSettings.from_dict(data.get("settings", {}))
        profile = find(data.get("profile"), "platform profile")
        if profile is not None:
            proj.load_profile(profile)
        patch = data.get("patch")
        proj.patch_path = str(patch.get("path", "")) if isinstance(patch, dict) else ""
        if isinstance(patch, dict) and (p.parent / patch.get("relative", "\0")).is_file():
            proj.patch_path = str(p.parent / patch["relative"])
        proj.platform_ids = [str(x) for x in data.get("platforms", ["PLATFORM-01"])] or ["PLATFORM-01"]
        bass = data.get("bass", {})
        proj.bass_wanted = bool(bass.get("wanted", True))
        proj.bass_cutoff_hz = float(bass.get("cutoff_hz", 120.0))
        motion = find(data.get("motion_file"), "motion file")
        video = data.get("video")
        if motion is not None:
            proj.open_motion(motion)
        elif video:
            found = find(video, "video")
            assert found is not None
            proj.extraction.open_video(found, with_hash=True)
            info = proj.extraction.video
            assert info is not None
            if video.get("sha256") and info.sha256 != video["sha256"]:
                raise ProjectError(f"{found.name} is not the video this project was made from (different content)")
            if "analysis" in data:
                proj.extraction.analysis = _unpack_analysis(data["analysis"])
                if "scene" in data:
                    proj.scene = _unpack_scene(data["scene"])
        proj.effects = EffectsDocument.from_dict(data["effects"]) if "effects" in data else proj._new_effects(proj.duration_s)
        if proj.duration_s:
            proj.effects.duration_s = proj.duration_s
        proj.extraction.edits = MotionEdits.from_dict(data.get("edits", {}))
        proj.automatic_motion = bool(data.get("automatic_motion", True))
        return proj

    def restore_motion(self) -> bool:
        """After load(): rebuild the motion from the stored analysis (fast). False if there is none."""
        ex = self.extraction
        if self.motion_file is not None or ex.video is None:
            return False
        if self.automatic_motion:
            if ex.analysis is None:
                return False
            ex.generate()
        else:
            ex.generate_blank()
        ex.process()
        return True


def default_profile_path() -> Path:
    return DEFAULT_PROFILE
