"""Bass for subwoofers and bass shakers.

Two sources are mixed into the configured output channels:

* the programme's **bass track** (low-passed audio stored at a low sample rate in the effects
  document), played locked to the show position, and
* a **synthesised vibration** (sine at the configured frequency) whose amplitude follows the
  ``vibration`` effect track.

``BassRenderer`` is pure numpy and deterministic (it renders any number of frames for a given show
position), so it is tested without sound hardware. ``AudioOutput`` plays it through miniaudio
(ALSA / PulseAudio / PipeWire / WASAPI / CoreAudio) when that optional package is installed.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import numpy as np

from .model import BassTrack
from .patch import AudioConfig

log = logging.getLogger(__name__)

RESYNC_TOLERANCE_S = 0.030   # drift between the audio cursor and the show position before a re-sync
FADE_S = 0.015               # crossfade at starts, stops and re-syncs


def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


def resample(x: np.ndarray, rate_in: float, rate_out: float) -> np.ndarray:
    """Band-limited resampling of a whole track (FFT method); exact for content below both Nyquist
    frequencies. Used once per programme, not in the audio callback."""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n == 0 or rate_in == rate_out:
        return x.astype(np.float32)
    m = max(1, round(n * rate_out / rate_in))
    spectrum = np.fft.rfft(x)
    out_bins = m // 2 + 1
    resized = np.zeros(out_bins, dtype=np.complex128)
    keep = min(len(spectrum), out_bins)
    resized[:keep] = spectrum[:keep]
    if m < n and m % 2 == 0:
        resized[-1] = resized[-1].real          # keep the down-sampled Nyquist bin real
    return (np.fft.irfft(resized, m) * (m / n)).astype(np.float32)


@dataclass
class AudioState:
    """What the audio thread needs to know; published by the effects service every frame."""

    active: bool = False
    position_s: float = 0.0     # show position at `stamp`
    stamp: float = 0.0          # monotonic time of the position
    track_id: int = 0           # identity of the prepared bass track (0 = none)
    vibration: float = 0.0      # 0..1 level of the vibration effect
    bass_gain: float = 1.0      # type master * enable for "bass"
    loop_s: float = 0.0         # programme duration when looping (0 = no loop)


class BassRenderer:
    def __init__(self, cfg: AudioConfig) -> None:
        self.cfg = cfg
        self.rate = int(cfg.sample_rate_hz)
        self.channels = int(cfg.channels)
        self._tracks: dict[int, np.ndarray] = {}
        self._lock = threading.Lock()
        self._cursor = 0              # sample index (output rate) of the next sample of the playing track
        self._playing = 0             # track id being played (0 = silence)
        self._vib = 0.0               # smoothed vibration level
        self._phase = 0.0
        self.resyncs = 0
        self._bass_mask = np.zeros(self.channels, dtype=np.float32)
        self._vib_mask = np.zeros(self.channels, dtype=np.float32)
        for ch in cfg.bass_channels:
            if 1 <= ch <= self.channels:
                self._bass_mask[ch - 1] = db_to_gain(cfg.bass_gain_db)
        for ch in cfg.vibration_channels:
            if 1 <= ch <= self.channels:
                self._vib_mask[ch - 1] = db_to_gain(cfg.vibration_gain_db)

    def prepare(self, track: BassTrack) -> int:
        """Resample a bass track to the output rate; returns its id for AudioState.track_id."""
        tid = id(track.samples)
        with self._lock:
            if tid in self._tracks:
                return tid
        pcm = resample(track.as_float() * db_to_gain(track.gain_db), track.sample_rate_hz, self.rate)
        with self._lock:
            self._tracks[tid] = pcm
            while len(self._tracks) > 4:    # keep the most recent programmes only
                self._tracks.pop(next(iter(self._tracks)))
        return tid

    def _slice(self, pcm: np.ndarray, start: int, frames: int, loop: int) -> np.ndarray:
        """`frames` samples of `pcm` from sample index `start` (silence outside; wrapping when loop > 0)."""
        out = np.zeros(frames, dtype=np.float32)
        if loop > 0:
            idx = (start + np.arange(frames)) % loop
            valid = idx < len(pcm)
            out[valid] = pcm[idx[valid]]
            return out
        lo, hi = max(start, 0), min(start + frames, len(pcm))
        if hi > lo:
            out[lo - start : hi - start] = pcm[lo:hi]
        return out

    def render(self, frames: int, state: AudioState, now: float) -> np.ndarray:
        """Next `frames` output frames, shape (frames, channels), float32 in -1..1."""
        dt = frames / self.rate
        with self._lock:
            want = state.track_id if (state.active and state.track_id in self._tracks) else 0
            old = self._tracks.get(self._playing)
            new = self._tracks.get(want)
        loop = round(state.loop_s * self.rate) if state.loop_s > 0 else 0
        target = 0
        if state.active:
            target = round((state.position_s + (now - state.stamp) + self.cfg.latency_offset_s) * self.rate)
        error = target - self._cursor
        if loop > 0:
            error = (error + loop // 2) % loop - loop // 2
        mono = np.zeros(frames, dtype=np.float32)
        if want != self._playing or (want and abs(error) > RESYNC_TOLERANCE_S * self.rate):
            fade = min(frames, max(1, int(FADE_S * self.rate)))
            ramp = np.ones(frames, dtype=np.float32)
            ramp[:fade] = np.linspace(0.0, 1.0, fade, endpoint=False, dtype=np.float32)
            if old is not None:        # fade the old material out ...
                mono += self._slice(old, self._cursor, frames, loop) * (1.0 - ramp)
            if new is not None:        # ... and the new position in
                mono += self._slice(new, target, frames, loop) * ramp
                if self._playing == want:
                    self.resyncs += 1
            self._playing, self._cursor = want, target + frames
        elif new is not None:
            mono += self._slice(new, self._cursor, frames, loop)
            self._cursor += frames
        mono *= state.bass_gain
        # synthesised vibration, level smoothed over ~30 ms to avoid zipper noise
        vib_target = state.vibration if state.active else 0.0
        if vib_target > 0.0 or self._vib > 1e-4:
            alpha = 1.0 - math.exp(-dt / 0.03)
            level = self._vib + (vib_target - self._vib) * np.linspace(alpha / frames, alpha, frames, dtype=np.float32)
            self._vib = float(level[-1])
            step = 2.0 * math.pi * self.cfg.vibration_frequency_hz / self.rate
            phase = self._phase + step * np.arange(1, frames + 1)
            self._phase = float(phase[-1] % (2.0 * math.pi))
            vib = (np.sin(phase) * level).astype(np.float32)
        else:
            vib = np.zeros(frames, dtype=np.float32)
        out = mono[:, None] * self._bass_mask[None, :] + vib[:, None] * self._vib_mask[None, :]
        return soft_clip(out)


def soft_clip(x: np.ndarray, knee: float = 0.8) -> np.ndarray:
    """Linear up to `knee`, then a smooth approach to +/-1: loud material never wraps or hard-clips."""
    a = np.abs(x)
    over = a > knee
    if not np.any(over):
        return x.astype(np.float32)
    y = x.astype(np.float32).copy()
    y[over] = np.sign(x[over]) * (knee + (1.0 - knee) * np.tanh((a[over] - knee) / (1.0 - knee)))
    return y


def miniaudio_available() -> bool:
    try:
        import miniaudio  # noqa: F401
    except ImportError:
        return False
    return True


def list_output_devices() -> list[str]:
    """Names of the playback devices (empty when miniaudio is missing or no backend works)."""
    try:
        import miniaudio

        return [str(d["name"]) for d in miniaudio.Devices().get_playbacks()]
    except Exception:  # noqa: BLE001 - any backend failure just means "no devices"
        return []


class AudioOutput:
    """Plays a BassRenderer on a sound device. Every failure is reported, none is raised into the show."""

    def __init__(self, renderer: BassRenderer, state_fn: Callable[[], AudioState], backend: str = "") -> None:
        self.renderer = renderer
        self.state_fn = state_fn
        self.backend = backend       # "" = system default; "null" = miniaudio's null device (tests)
        self.error = ""
        self.device_name = ""
        self.callbacks = 0
        self._device: Any = None

    def _generator(self) -> Generator[bytes, int, None]:
        frames = yield b""
        while True:
            try:
                block = self.renderer.render(int(frames), self.state_fn(), time.monotonic())
                self.callbacks += 1
                frames = yield block.tobytes()
            except Exception as exc:  # noqa: BLE001 - never let the audio thread die silently
                self.error = f"{type(exc).__name__}: {exc}"
                frames = yield bytes(int(frames) * self.renderer.channels * 4)

    def start(self) -> bool:
        cfg = self.renderer.cfg
        try:
            import miniaudio
        except ImportError:
            self.error = "bass output needs the miniaudio package (pip install miniaudio)"
            return False
        try:
            backends = [miniaudio.Backend.NULL] if self.backend == "null" else None
            device_id = None
            if cfg.device and self.backend != "null":
                for d in miniaudio.Devices().get_playbacks():
                    if cfg.device.lower() in str(d["name"]).lower():
                        device_id, self.device_name = d["id"], str(d["name"])
                        break
                else:
                    self.error = f"audio device matching {cfg.device!r} not found"
                    return False
            self._device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=cfg.channels,
                sample_rate=cfg.sample_rate_hz,
                buffersize_msec=cfg.buffer_ms,
                device_id=device_id,
                backends=backends,
                app_name="4D VR Theatre",
            )
            gen = self._generator()
            next(gen)
            self._device.start(gen)
            self.device_name = self.device_name or ("null device" if self.backend == "null" else "default output")
            log.info("bass output started", extra={"device": self.device_name, "rate": cfg.sample_rate_hz})
            return True
        except Exception as exc:  # noqa: BLE001 - miniaudio raises its own exception types
            self.error = f"audio output failed: {exc}"
            self._device = None
            log.warning("bass output unavailable", extra={"error": self.error})
            return False

    def stop(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("closing audio device: %s", exc)
        self._device = None

    @property
    def running(self) -> bool:
        return self._device is not None
