"""Video loading: metadata, SHA-256, format detection and analysis-frame iteration (OpenCV/FFmpeg)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

PROJECTIONS = ("flat", "180", "360")
STEREO_MODES = ("mono", "sbs", "ou")


class VideoError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    frame_count: int
    duration_s: float
    sha256: str = ""
    projection: str = "flat"
    stereo: str = "mono"
    hfov_deg: float = 75.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": Path(self.path).name,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "frame_rate": round(self.fps, 4),
            "duration_s": round(self.duration_s, 4),
            "projection": self.projection,
            "stereo": self.stereo,
        }


def file_sha256(path: str | Path, progress: Callable[[float], None] | None = None) -> str:
    p = Path(path)
    total = max(p.stat().st_size, 1)
    h = hashlib.sha256()
    done = 0
    with p.open("rb") as fh:
        while chunk := fh.read(4 << 20):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done / total)
    return h.hexdigest()


def guess_format(width: int, height: int) -> tuple[str, str]:
    """Best guess of (projection, stereo) from the frame aspect ratio. Always confirm in the UI/CLI."""
    aspect = width / max(height, 1)
    if abs(aspect - 2.0) < 0.05:
        return "360", "mono"
    if abs(aspect - 1.0) < 0.05:
        return ("360", "ou") if height >= 2160 else ("180", "mono")
    if abs(aspect - 4.0) < 0.1:
        return "360", "sbs"
    return "flat", "mono"


def probe(path: str | Path, with_hash: bool = True) -> VideoInfo:
    p = Path(path)
    if not p.exists():
        raise VideoError(f"{p} does not exist")
    cap = cv2.VideoCapture(str(p))
    if not cap.isOpened():
        raise VideoError(f"cannot open {p} (unsupported container or codec)")
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, _frame = cap.read()
        if not ok:
            raise VideoError(f"{p}: no decodable video frames")
    finally:
        cap.release()
    if fps <= 0 or fps > 1000:
        raise VideoError(f"{p}: invalid frame rate {fps}")
    if count <= 0:
        count = _count_frames(p)
    projection, stereo = guess_format(width, height)
    info = VideoInfo(str(p), width, height, fps, count, count / fps, projection=projection, stereo=stereo)
    if with_hash:
        info.sha256 = file_sha256(p)
    return info


def _count_frames(p: Path) -> int:
    cap = cv2.VideoCapture(str(p))
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


def eye_view(frame: np.ndarray, stereo: str) -> np.ndarray:
    """Left eye of a stereo frame (SBS: left half, OU: top half)."""
    h, w = frame.shape[:2]
    if stereo == "sbs":
        return frame[:, : w // 2]
    if stereo == "ou":
        return frame[: h // 2]
    return frame


def analysis_size(info: VideoInfo, max_width: int) -> tuple[int, int]:
    w, h = info.width, info.height
    if info.stereo == "sbs":
        w //= 2
    elif info.stereo == "ou":
        h //= 2
    scale = min(1.0, max_width / max(w, 1))
    return max(16, round(w * scale)), max(16, round(h * scale))


def iterate_frames(
    info: VideoInfo,
    max_width: int = 640,
    max_fps: float = 30.0,
    cancel: Callable[[], bool] | None = None,
    tap: Callable[[float, np.ndarray], None] | None = None,
) -> Iterator[tuple[int, float, np.ndarray]]:
    """Yield (frame_index, time_s, grey analysis frame) for the left eye, decimated to <= max_fps.

    `tap`, if given, is shown every one of those frames first: (time_s, left-eye picture in full size, BGR).
    """
    cap = cv2.VideoCapture(info.path)
    if not cap.isOpened():
        raise VideoError(f"cannot open {info.path}")
    step = max(1, round(info.fps / max_fps)) if max_fps > 0 else 1
    size = analysis_size(info, max_width)
    idx = 0
    try:
        while True:
            if cancel and cancel():
                return
            if idx % step == 0:
                ok, frame = cap.read()
                if not ok:
                    return
                eye = eye_view(frame, info.stereo)
                if tap is not None:
                    tap(idx / info.fps, eye)
                small = cv2.resize(eye, size, interpolation=cv2.INTER_AREA)
                yield idx, idx / info.fps, cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            elif not cap.grab():
                return
            idx += 1
    finally:
        cap.release()


class FrameReader:
    """Random-access reader for the preview (sequential reads are fast; seeks re-position)."""

    def __init__(self, path: str, stereo: str = "mono", max_width: int = 960) -> None:
        self.cap = cv2.VideoCapture(path)
        self.stereo = stereo
        self.max_width = max_width
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS)) or 30.0
        self._next = 0

    def frame_at(self, t: float) -> np.ndarray | None:
        target = max(0, int(t * self.fps))
        if target != self._next:
            if target < self._next or target - self._next > 30:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            else:
                while self._next < target and self.cap.grab():
                    self._next += 1
            self._next = target
        ok, frame = self.cap.read()
        if not ok:
            return None
        self._next += 1
        eye = eye_view(frame, self.stereo)
        h, w = eye.shape[:2]
        if w > self.max_width:
            eye = cv2.resize(eye, (self.max_width, int(h * self.max_width / w)), interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(eye, cv2.COLOR_BGR2RGB)

    def close(self) -> None:
        self.cap.release()
