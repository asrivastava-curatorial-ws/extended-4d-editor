"""Effects service: a background thread that renders the engine at the DMX refresh rate, sends the
frames, and feeds the bass output.

It is deliberately isolated from whatever drives the show (the motion control loop in the controller,
the playhead in the studio): the owner only supplies ``state_provider(now) -> {key: ShowState}``.
Nothing that goes wrong here - a bad document, a dead network interface, a missing sound card - is
ever raised to the owner; it is counted, logged and reported in ``status()``, and the outputs go dark.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .audio import AudioOutput, AudioState, BassRenderer
from .bridge import BridgeOutput, create_output
from .dmx import DmxOutput
from .engine import THEATRE, EffectsEngine, ShowState
from .patch import Patch

log = logging.getLogger(__name__)
StateProvider = Callable[[float], Mapping[str, ShowState]]


class EffectsService:
    def __init__(
        self,
        patch: Patch,
        platform_ids: list[str],
        state_provider: StateProvider,
        output: DmxOutput | None = None,
        audio_backend: str = "",
        clock: Callable[[], float] = time.monotonic,
        hosts: dict[str, str] | None = None,
    ) -> None:
        """`hosts`: the ESP32 of each platform (platform id -> address), for the "esp32" output - the
        motion controller passes the hosts of its motion links; a patch may name others."""
        self.patch = patch
        self.engine = EffectsEngine(patch, platform_ids, clock)
        self.output = output if output is not None else create_output(patch, platform_ids, hosts)
        self.provider = state_provider
        self.clock = clock
        self.period = 1.0 / patch.output.refresh_hz
        self.errors = 0
        self.last_error = ""
        self.overruns = 0
        self.universes_sent: dict[int, bytes] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._audio_state = AudioState()
        self.renderer: BassRenderer | None = None
        self.audio: AudioOutput | None = None
        self.audio_error = ""
        if patch.audio.enabled:
            self.renderer = BassRenderer(patch.audio)
            self.audio = AudioOutput(self.renderer, lambda: self._audio_state, audio_backend)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        try:
            self.output.open()
        except OSError as exc:
            self._note(f"DMX output could not be opened: {exc}")
        if self.audio is not None and not self.audio.start():
            self.audio_error = self.audio.error
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="effects-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._audio_state = AudioState()
        if self.audio is not None:
            self.audio.stop()
        try:
            for _ in range(3):   # make sure every receiver has seen the dark frame
                for universe, data in self.engine.blackout().items():
                    self.output.send(universe, data)
                self.output.flush()
            self.output.close()
        except OSError as exc:
            self._note(f"closing the DMX output: {exc}")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def prepare(self, doc: Any) -> None:
        """Resample a programme's bass track ahead of the show (call when the programme is loaded)."""
        bass = getattr(doc, "bass", None)
        if bass is None:
            return
        renderers = [self.renderer] if self.renderer is not None else []
        if isinstance(self.output, BridgeOutput):
            renderers += [n.audio.renderer for n in self.output.nodes.values() if n.audio is not None]
        for renderer in renderers:
            try:
                renderer.prepare(bass)
            except Exception as exc:  # noqa: BLE001 - a bad bass track must not stop the programme
                self._note(f"bass track: {exc}")

    # ------------------------------------------------------------------ one frame
    def tick(self, now: float | None = None) -> dict[int, bytes]:
        """Render and send one frame (called by the thread; also usable directly, e.g. in tests)."""
        now = self.clock() if now is None else now
        shows: Mapping[str, ShowState] = {}
        try:
            shows = self.provider(now)
            frames = self.engine.render(shows, now)
            self._publish_audio(shows.get(THEATRE), now)
        except Exception as exc:  # noqa: BLE001 - the show must go on: go dark and report
            self._note(f"{type(exc).__name__}: {exc}")
            frames = self.engine.blackout()
            self._audio_state = AudioState()
            shows = {}
        for universe, data in frames.items():
            self.output.send(universe, data)
        try:
            self.output.flush(now)
            if isinstance(self.output, BridgeOutput):
                self._stream_to_nodes(self.output, shows, now)
        except Exception as exc:  # noqa: BLE001 - nor may a node or its sound stop the frame loop
            self._note(f"{type(exc).__name__}: {exc}")
        self.universes_sent = frames
        return frames

    def _state_for(self, show: ShowState | None, renderer: BassRenderer, latency_offset_s: float, now: float) -> AudioState:
        """What a renderer should play for a show: its position, the bass track, the vibration level."""
        state = AudioState(stamp=now)
        if show is not None and show.active and show.doc is not None:
            eng = self.engine
            state.active = True
            state.position_s = show.position_s
            state.loop_s = show.duration_s if show.loop else 0.0
            if show.doc.bass is not None and eng.enabled("bass"):
                state.track_id = renderer.prepare(show.doc.bass)
                state.bass_gain = eng.master("bass")
            feed = getattr(show.doc, "feed", None)
            if feed is not None and eng.enabled("bass"):        # a live source: no track, sound as it happens
                state.feed = feed
                state.bass_gain = eng.master("bass")
            if eng.enabled("vibration"):
                t = show.position_s + latency_offset_s
                state.vibration = show.doc.level("vibration", max(0.0, t)) * eng.master("vibration")
        return state

    def _publish_audio(self, theatre: ShowState | None, now: float) -> None:
        if self.renderer is not None:
            self._audio_state = self._state_for(theatre, self.renderer, self.patch.audio.latency_offset_s, now)

    def _stream_to_nodes(self, bridge: BridgeOutput, shows: Mapping[str, ShowState], now: float) -> None:
        """Bass and vibration for every ESP32 that has an audio output: its platform's show, or the theatre's."""
        for node in bridge.audio_nodes(now):
            audio = node.audio
            if audio is None:
                continue
            show = shows.get(node.platform) if node.platform else shows.get(THEATRE)
            lead = audio.latency_s + audio.renderer.cfg.latency_offset_s
            bridge.stream(node, self._state_for(show, audio.renderer, lead, now), now)

    def _run(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            self.tick()
            next_t += self.period
            delay = next_t - time.monotonic()
            if delay < -self.period:
                self.overruns += 1
                next_t = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    def _note(self, text: str) -> None:
        self.errors += 1
        if text != self.last_error:
            log.warning("effects service: %s", text)
        self.last_error = text

    # ------------------------------------------------------------------ reporting
    def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {
            "running": self.running,
            "output": self.output.describe(),
            "refresh_hz": round(1.0 / self.period, 1),
            "errors": self.errors,
            "last_error": self.last_error or None,
            "overruns": self.overruns,
            "limited": self.engine.limited(),
            "levels": {k: round(v, 3) for k, v in self.engine.type_levels.items()},
        }
        if self.patch.audio.enabled:
            st["audio"] = {
                "running": bool(self.audio and self.audio.running),
                "device": self.audio.device_name if self.audio else "",
                "error": (self.audio.error if self.audio else "") or self.audio_error or None,
                "resyncs": self.renderer.resyncs if self.renderer else 0,
            }
        return st
