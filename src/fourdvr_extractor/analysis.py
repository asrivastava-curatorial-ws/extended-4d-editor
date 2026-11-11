"""Video analysis: frame decoding, feature tracking (pyramidal Lucas-Kanade optical flow with a
forward-backward check), scene-cut detection and per-frame camera ego-motion estimation."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from .egomotion import bearings, estimate, pixel_angle, to_platform
from .video import VideoInfo, analysis_size, iterate_frames

ProgressFn = Callable[[float, str], None]

# Tracking collapse counts as a scene cut only if it is both severe and sudden: fewer than
# COLLAPSE_FRACTION of the detected features survive the forward-backward check AND fewer than
# COLLAPSE_RELATIVE x the median survival of the preceding ~1 s. Fast motion through low-texture
# views (sky, fog, motion blur) keeps survival low for many frames; that is not an edit.
COLLAPSE_FRACTION = 0.2
COLLAPSE_RELATIVE = 0.3


@dataclass
class AnalysisSettings:
    max_width: int = 640
    max_fps: float = 30.0
    grid_cols: int = 12
    grid_rows: int = 8
    features_per_cell: int = 6
    pole_limit_deg: float = 72.0  # 180/360: ignore features nearer the poles (strong distortion)
    cut_threshold: float = 0.45  # histogram (Bhattacharyya) distance for a hard cut
    seed: int = 7


@dataclass
class Analysis:
    """Per-analysis-frame camera motion in PLATFORM axes (X forward, Y left, Z up)."""

    times: np.ndarray  # (N,) seconds
    angular_velocity: np.ndarray  # (N, 3) roll/pitch/yaw rates, rad/s
    velocity: np.ndarray  # (N, 3) translation direction * speed, 1/s (scale-free)
    confidence: np.ndarray  # (N,) 0..1
    parallax: np.ndarray  # (N,) median de-rotated flow per second (rad/s)
    cuts: list[float] = field(default_factory=list)
    frame_rate: float = 30.0
    tracked_points: np.ndarray = field(default_factory=lambda: np.zeros(0))
    settings: dict[str, object] = field(default_factory=dict)
    luma: np.ndarray = field(default_factory=lambda: np.zeros(0))  # (N,) mean picture brightness 0..1

    def summary(self) -> dict[str, object]:
        return {
            "analysis_frames": len(self.times),
            "analysis_rate_hz": round(self.frame_rate, 3),
            "scene_cuts": len(self.cuts),
            "mean_confidence": round(float(np.mean(self.confidence)) if len(self.confidence) else 0.0, 3),
            "mean_tracked_points": round(float(np.mean(self.tracked_points)) if len(self.tracked_points) else 0.0, 1),
        }


def _detect(gray: np.ndarray, settings: AnalysisSettings, mask: np.ndarray | None) -> np.ndarray:
    h, w = gray.shape
    pts = []
    cw, ch = w / settings.grid_cols, h / settings.grid_rows
    for r in range(settings.grid_rows):
        for c in range(settings.grid_cols):
            x0, y0 = int(c * cw), int(r * ch)
            x1, y1 = int((c + 1) * cw), int((r + 1) * ch)
            cell = gray[y0:y1, x0:x1]
            cell_mask = None if mask is None else mask[y0:y1, x0:x1]
            if cell.size < 64 or (cell_mask is not None and not cell_mask.any()):
                continue
            found = cv2.goodFeaturesToTrack(
                cell, maxCorners=settings.features_per_cell, qualityLevel=0.01, minDistance=5, mask=cell_mask
            )
            if found is not None:
                pts.append(found.reshape(-1, 2) + np.array([x0, y0], dtype=np.float32))
    return np.concatenate(pts).astype(np.float32) if pts else np.zeros((0, 2), np.float32)


def _feature_mask(width: int, height: int, projection: str, pole_limit_deg: float) -> np.ndarray | None:
    if projection == "flat":
        mask = np.zeros((height, width), np.uint8)
        b = max(2, int(0.03 * width))
        mask[b:-b, b:-b] = 255
        return mask
    mask = np.zeros((height, width), np.uint8)
    lim = pole_limit_deg / 90.0
    y0 = int((0.5 - lim / 2) * height)
    y1 = int((0.5 + lim / 2) * height)
    mask[max(0, y0) : min(height, y1), :] = 255
    return mask


def tracking_collapsed(fraction: float, history: deque[float]) -> bool:
    """True if feature survival collapsed severely *and* suddenly (see COLLAPSE_*); records it."""
    recent = float(np.median(history)) if history else 1.0
    history.append(fraction)
    return fraction < COLLAPSE_FRACTION and fraction < COLLAPSE_RELATIVE * recent


def _histogram(gray: np.ndarray) -> np.ndarray:
    hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
    return cv2.normalize(hist, hist).flatten()


def analyse(
    info: VideoInfo,
    settings: AnalysisSettings | None = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    tap: Callable[[float, np.ndarray], None] | None = None,
) -> Analysis:
    """Camera motion of a video. `tap` sees every analysed picture (time, left eye, BGR) on the way -
    scene detection uses it; it has no part in the motion analysis."""
    s = settings or AnalysisSettings()
    width, height = analysis_size(info, s.max_width)
    mask = _feature_mask(width, height, info.projection, s.pole_limit_deg)
    thr = 2.5 * pixel_angle(width, info.projection, info.hfov_deg)
    rng = np.random.default_rng(s.seed)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01)

    times: list[float] = []
    omega: list[np.ndarray] = []
    vel: list[np.ndarray] = []
    conf: list[float] = []
    par: list[float] = []
    counts: list[int] = []
    luma: list[float] = []
    cuts: list[float] = []
    survival: deque[float] = deque(maxlen=max(3, round(min(s.max_fps, info.fps))))
    prev = None
    prev_hist = None
    prev_t = 0.0
    total = max(info.frame_count, 1)
    for idx, t, gray in iterate_frames(info, s.max_width, s.max_fps, cancel, tap):
        hist = _histogram(gray)
        if prev is None:
            prev, prev_hist, prev_t = gray, hist, t
            continue
        dt = t - prev_t
        cut = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) > s.cut_threshold  # type: ignore[arg-type]
        w_rot = np.zeros(3)
        v_cam = np.zeros(3)
        c = 0.0
        p = 0.0
        n_pts = 0
        if not cut:
            p0 = _detect(prev, s, mask)
            if len(p0) >= 8:
                p1, st1, _ = cv2.calcOpticalFlowPyrLK(
                    prev, gray, p0, None, winSize=(21, 21), maxLevel=3, criteria=criteria
                )  # type: ignore[call-overload]
                p0r, st2, _ = cv2.calcOpticalFlowPyrLK(
                    gray, prev, p1, None, winSize=(21, 21), maxLevel=3, criteria=criteria
                )  # type: ignore[call-overload]
                fb = np.linalg.norm(p0 - p0r, axis=1)
                good = (st1.ravel() == 1) & (st2.ravel() == 1) & (fb < 1.0)
                a, b = p0[good], p1[good]
                if info.projection == "360" and len(b):  # wrap across the left/right seam
                    dx = b[:, 0] - a[:, 0]
                    b = b.copy()
                    b[:, 0] = np.where(
                        dx > width / 2, b[:, 0] - width, np.where(dx < -width / 2, b[:, 0] + width, b[:, 0])
                    )
                n_pts = len(a)
                if tracking_collapsed(n_pts / len(p0), survival):
                    cut = True  # an edit the histogram did not show
                elif n_pts >= 8:
                    em = estimate(
                        bearings(a, width, height, info.projection, info.hfov_deg),
                        bearings(b, width, height, info.projection, info.hfov_deg),
                        thr,
                        rng,
                    )
                    if em.valid and dt > 0:
                        w_rot = to_platform(em.rotation) / dt
                        v_cam = to_platform(em.direction) * em.speed / dt
                        c = float(min(1.0, em.inliers * min(1.0, n_pts / 60.0)))
                        p = em.parallax / dt
        if cut:
            cuts.append(t)
        times.append(t)
        omega.append(w_rot)
        vel.append(v_cam)
        conf.append(c)
        par.append(p)
        counts.append(n_pts)
        luma.append(float(gray.mean()) / 255.0)
        prev, prev_hist, prev_t = gray, hist, t
        if progress and len(times) % 10 == 0:
            progress(min(1.0, idx / total), f"analysing frame {idx}/{total}")
    if not times:
        raise ValueError("video too short to analyse (need at least two frames)")
    times_a = np.array(times)
    rate = 1.0 / float(np.median(np.diff(times_a))) if len(times_a) > 1 else info.fps
    result = Analysis(
        times=times_a,
        angular_velocity=np.array(omega),
        velocity=np.array(vel),
        confidence=np.array(conf),
        parallax=np.array(par),
        cuts=cuts,
        frame_rate=rate,
        tracked_points=np.array(counts, dtype=np.float64),
        settings={
            "analysis_width": width,
            "analysis_height": height,
            "max_fps": s.max_fps,
            "projection": info.projection,
            "stereo": info.stereo,
            "hfov_deg": info.hfov_deg if info.projection == "flat" else None,
        },
        luma=np.array(luma),
    )
    if progress:
        progress(1.0, f"analysis complete: {len(times_a)} frames, {len(cuts)} cuts")
    return result


def rotation_magnitude_deg(analysis: Analysis) -> float:
    return (
        math.degrees(float(np.max(np.linalg.norm(analysis.angular_velocity, axis=1)))) if len(analysis.times) else 0.0
    )
