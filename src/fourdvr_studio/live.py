"""Live output from the Editor: the playhead drives the same effects service that runs the show.

"Going live" starts a :class:`fourdvr_fx.service.EffectsService` with the venue patch; while the
Editor plays, the service sees an active show at the playhead position - DMX and bass come out
exactly as they will in the theatre, with the same lead times and the same safety limits. While
the Editor is paused everything is off (or, with ``hold``, the levels under the playhead are held:
useful to set a fan speed or a light level by eye). Single fixtures can be fired for a moment at
any time. Without a venue patch a preview patch is used: no DMX, bass and seat vibration on this
computer's default sound output.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fourdvr_fx import EffectsDocument
from fourdvr_fx.dmx import DmxOutput
from fourdvr_fx.engine import THEATRE, ShowState
from fourdvr_fx.model import CompiledDocument
from fourdvr_fx.patch import AudioConfig, OutputConfig, Patch
from fourdvr_fx.service import EffectsService


def preview_patch() -> Patch:
    """No fixtures and no DMX: bass and seat vibration on the default sound output of this computer."""
    return Patch(output=OutputConfig(protocol="none"), audio=AudioConfig(enabled=True, vibration_channels=[1, 2]),
                 source="built-in preview (sound only)")


class LiveSession:
    def __init__(
        self,
        patch: Patch,
        platform_ids: list[str],
        output: DmxOutput | None = None,
        audio_backend: str = "",
    ) -> None:
        self.patch = patch
        self.platform_ids = list(platform_ids)
        self.service = EffectsService(patch, self.platform_ids, self._states, output=output, audio_backend=audio_backend)
        self.service.engine.test_enabled = True
        self.hold = False
        self._lock = threading.Lock()
        self._doc: CompiledDocument | None = None
        self._playing = False
        self._position = 0.0
        self._stamp = 0.0
        self._duration = 0.0
        self._loop = True
        self._run = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.service.start()

    def stop(self) -> None:
        with self._lock:
            self._playing = False
        self.service.engine.clear_overrides()
        self.service.stop()

    @property
    def running(self) -> bool:
        return self.service.running

    # ------------------------------------------------------------------ what to play
    def set_document(self, doc: EffectsDocument) -> None:
        compiled = doc.compile()
        self.service.prepare(compiled)            # resample the bass track now, not in the audio callback
        with self._lock:
            self._doc = compiled
            self._duration = doc.duration_s

    def transport(self, playing: bool, position_s: float, loop: bool = True, stamp: float | None = None) -> None:
        """The Editor's transport state: `position_s` is where the playhead was at `stamp` (monotonic clock)."""
        with self._lock:
            if playing and not self._playing:
                self._run += 1                    # a new run: per-show totals start again
            self._playing, self._position, self._loop = playing, position_s, loop
            self._stamp = time.monotonic() if stamp is None else stamp

    def _states(self, now: float) -> dict[str, ShowState]:
        with self._lock:
            doc, duration = self._doc, self._duration
            active = doc is not None and (self._playing or self.hold)
            position = self._position + (now - self._stamp if self._playing else 0.0)
            loop, run = self._loop, self._run
        if active and duration > 0.0 and position >= duration:
            if loop:
                position %= duration
            else:
                active = False
        state = ShowState(active, doc, max(0.0, position), duration, loop and self._playing, run)
        return {THEATRE: state, **{pid: state for pid in self.platform_ids}}

    # ------------------------------------------------------------------ tests / panic
    def fire(self, key: str, level: float = 1.0, seconds: float = 0.5) -> bool:
        return self.service.engine.fire(key, level, seconds)

    def blackout(self) -> None:
        """Everything off now: stop following the playhead and drop every manual test."""
        with self._lock:
            self._playing = False
        self.hold = False
        self.service.engine.clear_overrides()

    def status(self) -> dict[str, Any]:
        return {**self.service.status(), **self.service.engine.status()}
