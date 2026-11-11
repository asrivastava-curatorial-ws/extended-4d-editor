"""End-to-end extraction workflow: open -> analyse -> generate -> process -> validate -> export."""

from __future__ import annotations

import datetime as dt
import json
import platform as host_platform
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from fourdvr_fx import EffectsDocument
from fourdvr_fx.embed import attach_effects
from fourdvr_motion import motionfile
from fourdvr_motion.platform_config import PlatformConfig, load_platform_config

from . import PRODUCT_NAME, __version__
from .analysis import Analysis, AnalysisSettings, analyse
from .constraints import ConstraintResult, ConstraintSettings, apply_constraints
from .cueing import CueChannels, CueingSettings, compute_cues
from .events import MotionEvents, analyse_events
from .manual import MotionEdits
from .validation import ValidationReport, validate
from .video import PROJECTIONS, STEREO_MODES, VideoInfo, probe

ProgressFn = Callable[[float, str], None]
DEFAULT_PROFILE = Path(__file__).resolve().parent / "profiles" / "example-electric-6dof.toml"


@dataclass
class ExtractionSettings:
    programme_id: int = 1
    sample_rate_hz: float = 100.0
    projection: str | None = None  # None: keep the auto-detected value
    stereo: str | None = None
    hfov_deg: float = 75.0
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    cueing: CueingSettings = field(default_factory=CueingSettings)
    constraints: ConstraintSettings = field(default_factory=ConstraintSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "programme_id": self.programme_id,
            "sample_rate_hz": self.sample_rate_hz,
            "projection": self.projection,
            "stereo": self.stereo,
            "hfov_deg": self.hfov_deg,
            "analysis": asdict(self.analysis),
            "cueing": self.cueing.to_dict(),
            "constraints": self.constraints.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractionSettings:
        scalars = ("programme_id", "sample_rate_hz", "projection", "stereo", "hfov_deg")
        unknown = sorted(set(data) - set(scalars) - {"analysis", "cueing", "constraints"})
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
        s = cls()
        for key in scalars:
            if key in data:
                setattr(s, key, data[key])
        if "analysis" in data:
            s.analysis = _section(AnalysisSettings, data["analysis"], "analysis")
        if "cueing" in data:
            s.cueing = CueingSettings.from_dict(data["cueing"])
        if "constraints" in data:
            s.constraints = _section(ConstraintSettings, data["constraints"], "constraints")
        s.check()
        return s

    def check(self) -> None:
        if not motionfile.MIN_SAMPLE_RATE_HZ <= self.sample_rate_hz <= motionfile.MAX_SAMPLE_RATE_HZ:
            raise ValueError("sample rate must be 50-200 Hz")
        if self.projection is not None and self.projection not in PROJECTIONS:
            raise ValueError(f"projection must be one of {PROJECTIONS}")
        if self.stereo is not None and self.stereo not in STEREO_MODES:
            raise ValueError(f"stereo must be one of {STEREO_MODES}")
        if not 20 <= self.hfov_deg <= 170:
            raise ValueError("horizontal field of view must be 20-170 degrees")
        if self.programme_id < 1:
            raise ValueError("programme id must be >= 1")


def _section(kind: Any, values: Any, name: str) -> Any:
    """Build a settings dataclass, turning unknown/invalid keys into a readable ValueError."""
    if not isinstance(values, dict):
        raise ValueError(f"settings section '{name}' must be an object")
    try:
        return kind(**values)
    except TypeError as exc:
        raise ValueError(f"invalid '{name}' settings: {exc}") from exc


class ExtractionProject:
    def __init__(self, settings: ExtractionSettings | None = None, profile: PlatformConfig | None = None) -> None:
        self.settings = settings or ExtractionSettings()
        self.profile = profile or load_platform_config(DEFAULT_PROFILE)
        self.video: VideoInfo | None = None
        self.analysis: Analysis | None = None
        self.events: MotionEvents | None = None
        self.raw: np.ndarray | None = None
        self.channels: CueChannels | None = None
        self.constraint_result: ConstraintResult | None = None
        self.motion: np.ndarray | None = None
        self.report: ValidationReport | None = None
        self.edits = MotionEdits()  # manual motion editing (applied between generate and process)

    # -------------------------------------------------------------- stages
    def load_profile(self, path: str | Path) -> None:
        self.profile = load_platform_config(path)
        self.motion = None
        self.report = None

    def open_video(self, path: str | Path, with_hash: bool = True) -> VideoInfo:
        info = probe(path, with_hash=with_hash)
        s = self.settings
        if s.projection:
            info.projection = s.projection
        if s.stereo:
            info.stereo = s.stereo
        info.hfov_deg = s.hfov_deg
        self.video = info
        self.analysis = self.events = self.raw = self.motion = None
        self.report = None
        return info

    def _require_video(self) -> VideoInfo:
        if self.video is None:
            raise RuntimeError("open a video first")
        return self.video

    def apply_format(self, projection: str, stereo: str, hfov_deg: float | None = None) -> None:
        info = self._require_video()
        if projection not in PROJECTIONS or stereo not in STEREO_MODES:
            raise ValueError("unsupported projection/stereo")
        info.projection, info.stereo = projection, stereo
        if hfov_deg:
            info.hfov_deg = hfov_deg
        self.settings.projection, self.settings.stereo = projection, stereo
        self.analysis = self.events = self.raw = self.motion = None

    def analyse(self, progress: ProgressFn | None = None, cancel: Callable[[], bool] | None = None,
                tap: Callable[[float, np.ndarray], None] | None = None) -> Analysis:
        info = self._require_video()
        self.analysis = analyse(info, self.settings.analysis, progress, cancel, tap)
        self.events = self.raw = self.motion = None
        return self.analysis

    def generate(self) -> np.ndarray:
        """Motion/event analysis, classification and perceptual cueing (raw 6-DoF cues)."""
        info = self._require_video()
        if self.analysis is None:
            raise RuntimeError("analyse the video first")
        fs = self.settings.sample_rate_hz
        self.events = analyse_events(self.analysis, info.duration_s, fs)
        self.raw, self.channels = compute_cues(self.events, fs, self.settings.cueing)
        self.motion = None
        return self.raw

    def generate_blank(self) -> np.ndarray:
        """No automatic motion: an all-neutral track for hand-made motion (manual axis curves)."""
        info = self._require_video()
        fs = self.settings.sample_rate_hz
        self.events = None
        self.channels = None
        self.raw = np.zeros((motionfile.expected_sample_count(info.duration_s, fs), 6))
        self.motion = None
        return self.raw

    def edited_raw(self) -> np.ndarray:
        """The raw cues with the manual edits applied (what the constraint stage receives)."""
        if self.raw is None:
            raise RuntimeError("generate motion first")
        if self.edits.is_empty():
            return self.raw
        return self.edits.apply(self.raw, self.settings.sample_rate_hz, self.profile, self.settings.cueing.fade_s)

    def process(self) -> np.ndarray:
        """Manual edits, then filtering to bandwidth and workspace/velocity/acceleration/jerk constraints."""
        self.constraint_result = apply_constraints(
            self.edited_raw(), self.settings.sample_rate_hz, self.profile, self.settings.constraints
        )
        self.motion = self.constraint_result.motion
        self.report = None
        return self.motion

    def validate(self) -> ValidationReport:
        if self.motion is None:
            raise RuntimeError("process motion first")
        self.report = validate(self.motion, self.settings.sample_rate_hz, self.profile)
        return self.report

    def export(
        self,
        path: str | Path,
        allow_violations: bool = False,
        compression: str = "none",
        effects: EffectsDocument | None = None,
    ) -> str:
        """Write the .motion file; `effects` (optional) are embedded in its header (see fourdvr_fx.embed)."""
        info = self._require_video()
        if self.motion is None:
            raise RuntimeError("nothing to export: process motion first")
        report = self.report or self.validate()
        if not report.passed and not allow_violations:
            raise ValueError("validation failed; fix the settings/profile or export with --allow-violations")
        fs = self.settings.sample_rate_hz
        header = motionfile.build_header(
            programme_id=self.settings.programme_id,
            duration_s=info.duration_s,
            sample_rate_hz=fs,
            video=info.to_dict(),
            generation=self._generation_metadata(),
            validation={
                "passed": report.passed,
                "profile": report.profile,
                "profile_sha256": report.profile_sha256,
                "violations": report.violations,
                "workspace_violations": report.workspace_violations,
                "stroke_utilisation": round(report.actuators.get("stroke_utilisation", 0.0), 4),
                "disclaimer": "numerical limit check only; not a passenger-safety certification",
            },
        )
        return motionfile.write_motion(path, attach_effects(header, effects), self.motion, compression=compression)

    def _generation_metadata(self) -> dict[str, Any]:
        ev = self.events
        meta: dict[str, Any] = {
            "generator": PRODUCT_NAME,
            "generator_version": __version__,
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": host_platform.platform(terse=True),
            "settings": self.settings.to_dict(),
            "analysis": self.analysis.summary() if self.analysis else {},
        }
        if ev is not None:
            meta["classes"] = ev.class_fractions()
            meta["segments"] = [[round(a, 3), round(b, 3), c] for a, b, c in ev.segments()][:2000]
            meta["events"] = ev.events[:2000]
        if not self.edits.is_empty():
            meta["manual_edits"] = self.edits.to_dict()
        if ev is None and self.raw is not None:
            meta["automatic_motion"] = False
        if self.constraint_result is not None:
            meta["constraints"] = {
                "iterations": self.constraint_result.iterations,
                "converged": self.constraint_result.converged,
                "axis_gain_min": self.constraint_result.axis_gain_min,
            }
        return meta

    def run_all(
        self,
        video: str | Path,
        output: str | Path | None = None,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
        allow_violations: bool = False,
    ) -> ValidationReport:
        self.open_video(video)
        self.analyse(progress, cancel)
        self.generate()
        self.process()
        report = self.validate()
        if output is not None:
            self.export(output, allow_violations=allow_violations)
        return report


def load_settings(path: str | Path) -> ExtractionSettings:
    return ExtractionSettings.from_dict(json.loads(Path(path).read_text()))


def save_settings(settings: ExtractionSettings, path: str | Path) -> None:
    Path(path).write_text(json.dumps(settings.to_dict(), indent=2) + "\n")
