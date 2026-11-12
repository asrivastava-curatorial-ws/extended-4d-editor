"""Rain, fog, fire, snow, water and scent scenes of a live picture - the Editor's scene detection
(docs/SCENE_DETECTION.md), one picture a second, in a thread of its own so that it never holds up
the motion. It needs the scene models; without them the live show runs without this part."""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from fourdvr_extractor import scene as scene_mod

SEEN, GONE = 0.8, 0.5            # a concept is on from the first score, off again below the second
INTERVAL_S = 1.0
LEVELS = {"rain": ("rain", 0.7), "fog": ("fog", 0.6), "fire": ("heat", 1.0), "bubbles": ("bubbles", 0.8),
          "snow": ("snow", 0.7), "splash": ("water", 0.6)}
SCENTS = {"forest": 1, "sea": 2, "flowers": 3}


class LiveScenes:
    def __init__(self, projection: str = "flat") -> None:
        self.projection = projection
        self.note = ""
        self.tagger: Any = None
        models, why = scene_mod.find_models()
        if models is None:
            self.note = why
        else:
            try:
                self.tagger = scene_mod.PictureTagger(models, INTERVAL_S, batch=1)
            except (scene_mod.SceneError, OSError, ValueError, KeyError) as exc:
                self.note = str(exc)
        self._lock = threading.Lock()
        self._frame: tuple[np.ndarray, float] | None = None
        self._last_offer = -1e18
        self._on: dict[str, int] = {}             # concept -> pictures in a row it has been seen
        self._scores: dict[str, float] = {}
        self.pictures = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.tagger is not None:
            self._thread = threading.Thread(target=self._run, name="live-scenes", daemon=True)
            self._thread.start()

    def offer(self, frame: np.ndarray, t: float) -> None:
        if self.tagger is None or t - self._last_offer < INTERVAL_S:
            return
        self._last_offer = t
        with self._lock:
            self._frame = (frame, t)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                item, self._frame = self._frame, None
            if item is None:
                continue
            try:
                pixels = self.tagger.prepare(item[0], self.projection)
                scores = {k: float(v[0]) for k, v in self.tagger.score(pixels[None]).items()}
            except Exception as exc:  # noqa: BLE001 - a model problem ends scene detection, not the show
                self.note = f"{type(exc).__name__}: {exc}"
                return
            on = dict(self._on)
            for name, score in scores.items():
                count = on.get(name, 0)
                on[name] = count + 1 if score >= (GONE if count else SEEN) else 0
            self._on, self._scores = on, scores
            self.pictures += 1

    def levels(self, t: float) -> dict[str, float]:
        """Effect levels for what has been seen in at least two pictures in a row."""
        out: dict[str, float] = {}
        on = self._on
        for concept, (effect, level) in LEVELS.items():
            if on.get(concept, 0) >= 2:
                out[effect] = level
        if any(on.get(c, 0) >= 5 for c in SCENTS):
            out["scent"] = 1.0
        return out

    def params(self) -> dict[str, dict[str, float]]:
        on = self._on
        seen = [(on.get(c, 0), slot) for c, slot in SCENTS.items() if on.get(c, 0) >= 5]
        return {"scent": {"slot": float(max(seen)[1])}} if seen else {}

    def status(self) -> dict[str, Any]:
        seen = sorted(k for k, v in self._on.items() if v >= 2)
        return {"running": self.tagger is not None and self._thread is not None and self._thread.is_alive(),
                "pictures": self.pictures, "seen": seen, "note": self.note or None}

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
