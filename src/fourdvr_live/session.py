"""Recorded live sessions (``.4dlive``).

A session is what a live source delivered, on the control grid: one row per control period with the
(checked, direction-corrected) accelerations and rotation rates, speed, engine and surface values,
the envelope, the pose offset the live cueing made of it and the effect levels the rules asked for.
It is the raw material for a programme: ``fourdvr live convert`` and the Editor make a ``.motion``
file of it with the non-causal, better-looking offline cueing (docs/LIVE.md, "From a session to a
programme").

The file is a NumPy ``.npz`` archive (ZIP of plain arrays, no Python objects) with a JSON header.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from .sample import LiveSample

FORMAT = "4D-LIVE-SESSION"
VERSION = 1
SUFFIX = ".4dlive"
MAX_HOURS = 6.0
CHUNK = 8192
SCALARS = ("speed", "rpm", "rpm_max", "rpm_idle", "throttle", "brake", "rumble", "slip", "impact", "envelope")


class SessionError(ValueError):
    """The session file cannot be used."""


class SessionRecorder:
    """Collects rows in memory; `save` may be called from another thread while rows are added."""

    def __init__(self, rate_hz: float, info: dict[str, Any] | None = None) -> None:
        self.rate_hz = float(rate_hz)
        self.info = dict(info or {})
        self.limit = int(MAX_HOURS * 3600 * rate_hz)
        self.full = False
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._fx_chunks: list[np.ndarray] = []
        self._fx_names: list[str] = []
        self._n = 0
        self.width = 6 + 2 + len(SCALARS) + 6          # accel+rates, attitude, scalars, pose
        self.started = dt.datetime.now(dt.UTC)

    def __len__(self) -> int:
        return self._n

    @property
    def seconds(self) -> float:
        return self._n / self.rate_hz

    def add(self, sample: LiveSample | None, envelope: float, pose: np.ndarray, levels: dict[str, float]) -> None:
        if self._n >= self.limit:
            self.full = True
            return
        i, j = divmod(self._n, CHUNK)
        if j == 0:
            with self._lock:
                self._chunks.append(np.zeros((CHUNK, self.width), dtype=np.float32))
                self._fx_chunks.append(np.zeros((CHUNK, 16), dtype=np.uint8))
        row = self._chunks[i][j]
        if sample is not None:
            row[0:3] = sample.accel
            row[3:6] = sample.rates
            row[6:8] = sample.attitude if sample.attitude is not None else (np.nan, np.nan)
            row[8:8 + len(SCALARS) - 1] = (sample.speed, sample.rpm, sample.rpm_max, sample.rpm_idle, sample.throttle,
                                           sample.brake, sample.rumble, sample.slip, sample.impact)
        else:
            row[6:8] = np.nan
        row[8 + len(SCALARS) - 1] = envelope
        row[8 + len(SCALARS):] = pose
        if levels:
            fx = self._fx_chunks[i][j]
            for name, value in levels.items():
                if name not in self._fx_names:
                    if len(self._fx_names) >= 16:
                        continue
                    self._fx_names.append(name)
                fx[self._fx_names.index(name)] = round(max(0.0, min(1.0, value)) * 255.0)
        self._n += 1

    def snapshot(self) -> Session:
        with self._lock:
            n = self._n
            chunks, fx_chunks, names = list(self._chunks), list(self._fx_chunks), list(self._fx_names)
        data = np.concatenate(chunks)[:n] if chunks else np.zeros((0, self.width), dtype=np.float32)
        fx = np.concatenate(fx_chunks)[:n, : len(names)] if fx_chunks else np.zeros((0, 0), dtype=np.uint8)
        k = 8
        scalars = {name: data[:, k + i].copy() for i, name in enumerate(SCALARS)}
        header = {
            "format": FORMAT, "version": VERSION, "generator": f"Extended 4D Editor {__version__}",
            "created_at": self.started.strftime("%Y-%m-%dT%H:%M:%SZ"), "rate_hz": self.rate_hz, "samples": int(n),
            "duration_s": round(max(0, n - 1) / self.rate_hz, 4), "effects": names, **self.info,
        }
        return Session(header, data[:, 0:3].copy(), data[:, 3:6].copy(), data[:, 6:8].copy(), scalars,
                       data[:, k + len(SCALARS):].copy(), fx.astype(np.float32) / 255.0)

    def save(self, path: str | Path) -> Path:
        return self.snapshot().save(path)


@dataclass
class Session:
    header: dict[str, Any]
    accel: np.ndarray                     # (N, 3) surge, sway, heave (m/s^2)
    rates: np.ndarray                     # (N, 3) roll, pitch, yaw (rad/s)
    attitude: np.ndarray                  # (N, 2) roll, pitch (rad); NaN where unknown
    scalars: dict[str, np.ndarray]        # each (N,)
    pose: np.ndarray                      # (N, 6) what the live cueing made of it (offset from neutral)
    effects: np.ndarray                   # (N, K) levels 0..1; the names are header["effects"]
    path: Path | None = field(default=None, compare=False)

    @property
    def rate_hz(self) -> float:
        return float(self.header["rate_hz"])

    @property
    def samples(self) -> int:
        return len(self.accel)

    @property
    def duration_s(self) -> float:
        return max(0, self.samples - 1) / self.rate_hz

    @property
    def effect_names(self) -> list[str]:
        return [str(n) for n in self.header.get("effects", [])]

    @property
    def active(self) -> np.ndarray:
        """Where the source was running (not paused, stalled or held)."""
        return self.scalars["envelope"] > 0.5

    def level(self, name: str) -> np.ndarray:
        names = self.effect_names
        return self.effects[:, names.index(name)] if name in names else np.zeros(self.samples, dtype=np.float32)

    def summary(self) -> dict[str, Any]:
        act = self.active
        src = self.header.get("source", {})
        return {
            "source": src.get("title") or src.get("type") or "unknown",
            "duration_s": round(self.duration_s, 2),
            "rate_hz": self.rate_hz,
            "running_s": round(float(np.sum(act)) / self.rate_hz, 2),
            "top_speed_m_s": round(float(np.max(self.scalars["speed"])) if self.samples else 0.0, 1),
            "peak_accel_m_s2": [round(float(v), 2) for v in (np.max(np.abs(self.accel), axis=0) if self.samples else np.zeros(3))],
            "effects": {n: round(float(np.max(self.level(n))), 2) for n in self.effect_names},
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        if p.suffix != SUFFIX:
            p = p.with_name(p.name + SUFFIX)
        tmp = p.with_name(p.name + ".part")
        arrays: dict[str, np.ndarray] = {
            "header": np.array(json.dumps(self.header)),
            "accel": self.accel.astype(np.float32), "rates": self.rates.astype(np.float32),
            "attitude": self.attitude.astype(np.float32), "pose": self.pose.astype(np.float32),
            "effects": np.round(self.effects * 255.0).astype(np.uint8),
            **{f"s_{k}": v.astype(np.float32) for k, v in self.scalars.items()},
        }
        with tmp.open("wb") as fh:
            np.savez_compressed(fh, **arrays)  # type: ignore[arg-type]
        os.replace(tmp, p)
        self.path = p
        return p

    @classmethod
    def load(cls, path: str | Path) -> Session:
        p = Path(path)
        try:
            with np.load(p, allow_pickle=False) as z:
                header = json.loads(str(z["header"]))
                if header.get("format") != FORMAT:
                    raise SessionError(f"{p.name}: not a live session file")
                if int(header.get("version", 0)) > VERSION:
                    raise SessionError(f"{p.name}: made by a newer version of this program (session format "
                                       f"{header.get('version')})")
                accel = np.asarray(z["accel"], dtype=np.float64)
                n = len(accel)
                scalars = {k: np.asarray(z[f"s_{k}"], dtype=np.float64) if f"s_{k}" in z.files else np.zeros(n)
                           for k in SCALARS}
                session = cls(header, accel, np.asarray(z["rates"], dtype=np.float64),
                              np.asarray(z["attitude"], dtype=np.float64), scalars,
                              np.asarray(z["pose"], dtype=np.float64),
                              np.asarray(z["effects"], dtype=np.float32) / 255.0, p)
        except SessionError:
            raise
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise SessionError(f"{p.name}: cannot be read as a live session ({exc})") from exc
        arrays = [session.rates, session.attitude, session.pose, session.effects, *session.scalars.values()]
        if any(len(a) != n for a in arrays) or session.rate_hz <= 0 or not np.all(np.isfinite(accel)):
            raise SessionError(f"{p.name}: the session data are inconsistent")
        return session
