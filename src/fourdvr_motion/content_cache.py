"""Local cache of .motion files downloaded from the Control Server (SHA-256 verified, resumable)."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from fourdvr_fx.embed import effects_from_header
from fourdvr_fx.model import CompiledDocument, EffectsError

from . import motionfile
from .interpolation import MotionTrack

log = logging.getLogger(__name__)


class ContentError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class MotionCache:
    def __init__(self, directory: Path, keep: int = 32) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._tracks: dict[str, tuple[MotionTrack, motionfile.MotionFile]] = {}
        self._effects: dict[str, CompiledDocument | None] = {}
        self.effects_errors: dict[str, str] = {}

    def path(self, sha256: str) -> Path:
        return self.dir / f"{sha256}.motion"

    def _lock(self, sha256: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(sha256, threading.Lock())

    def has(self, sha256: str) -> bool:
        return self.path(sha256).exists()

    def fetch(self, base_url: str, url: str, sha256: str, token: str, timeout: float = 30.0) -> Path:
        """Download (or reuse) a .motion file and verify its SHA-256. Blocking; call from a thread."""
        with self._lock(sha256):
            final = self.path(sha256)
            if final.exists():
                if _sha256(final) == sha256:
                    return final
                log.warning("cached motion file corrupt; re-downloading", extra={"sha256": sha256})
                final.unlink()
            part = final.with_suffix(".part")
            offset = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(base_url.rstrip("/") + url, headers={"Authorization": f"Bearer {token}"})
            if offset:
                req.add_header("Range", f"bytes={offset}-")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if offset and resp.status != 206:
                        offset = 0  # server ignored the range: restart
                    with part.open("ab" if offset else "wb") as fh:
                        while chunk := resp.read(1 << 16):
                            fh.write(chunk)
            except (urllib.error.URLError, OSError) as exc:
                raise ContentError(f"download failed: {exc}") from exc
            actual = _sha256(part)
            if actual != sha256:
                part.unlink(missing_ok=True)
                raise ContentError(f"checksum mismatch for motion file (expected {sha256[:12]}, got {actual[:12]})")
            os.replace(part, final)
            log.info("motion file cached", extra={"sha256": sha256, "bytes": final.stat().st_size})
            return final

    def load(self, sha256: str) -> tuple[MotionTrack, motionfile.MotionFile]:
        with self._guard:
            hit = self._tracks.get(sha256)
        if hit is not None:
            return hit
        mf = motionfile.read_motion(self.path(sha256), verify=True)
        track = MotionTrack(mf.samples, mf.sample_rate_hz, mf.duration_s)
        with self._guard:
            self._tracks[sha256] = (track, mf)
        return track, mf

    def effects(self, sha256: str) -> CompiledDocument | None:
        """The effects embedded in a cached .motion file, ready for playback; None for a motion-only
        programme. A damaged effects section never affects the motion: it is reported and ignored."""
        with self._guard:
            if sha256 in self._effects:
                return self._effects[sha256]
        compiled: CompiledDocument | None = None
        try:
            _track, mf = self.load(sha256)
            doc = effects_from_header(mf.header)
            if doc is not None and not doc.is_empty():
                compiled = doc.compile()
        except (EffectsError, motionfile.MotionFormatError, OSError) as exc:
            self.effects_errors[sha256] = str(exc)
            log.warning("effects section ignored", extra={"sha256": sha256, "error": str(exc)})
        with self._guard:
            self._effects[sha256] = compiled
        return compiled

    def cleanup(self, keep_sha: set[str]) -> int:
        removed = 0
        files = sorted(self.dir.glob("*.motion"), key=lambda p: p.stat().st_mtime, reverse=True)
        for idx, f in enumerate(files):
            sha = f.stem
            if sha not in keep_sha and idx >= self.keep:
                f.unlink(missing_ok=True)
                removed += 1
        for part in self.dir.glob("*.part"):
            if part.stem not in keep_sha:
                part.unlink(missing_ok=True)
        return removed
