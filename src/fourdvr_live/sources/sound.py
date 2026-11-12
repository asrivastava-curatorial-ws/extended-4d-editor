"""Sound as a live source: the low end of whatever is playing goes to the shakers as it happens.

``SoundTap`` listens to a sound input - a microphone or line input, or (on PulseAudio / PipeWire) the
"Monitor of ..." device that carries what the computer itself plays - keeps what is below the
cut-off, and feeds it to the bass outputs a few hundredths of a second behind (``CaptureFeed``).
It also follows how loud the low end is (seat vibration) and marks sudden thumps.

``SoundSource`` makes a live source of that for content with no telemetry and no usable picture:
bass, vibration, a small jolt of the platform at every thump.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np

from ..filters import bilinear
from ..sample import LiveSample
from ..shaker import CaptureFeed
from .base import SOUND, LiveSource, SourceError

CAPTURE_RATE = 48000
FEED_RATE = 4000
BLOCK_S = 0.02


def list_capture_devices() -> list[str]:
    try:
        import miniaudio

        return [str(d["name"]) for d in miniaudio.Devices().get_captures()]
    except Exception:  # noqa: BLE001 - no backend: no devices
        return []


class _Biquads:
    """A 4th-order Butterworth low-pass as two second-order sections, sample by sample (the stream is
    only 4 kHz by then, so plain Python is fast enough and SciPy is not needed on a show PC)."""

    def __init__(self, cutoff_hz: float, rate: float) -> None:
        w = 2.0 * math.pi * cutoff_hz
        self.sections = []
        for q in (0.5411961, 1.3065630):           # the two pole pairs of a 4th-order Butterworth
            b, a = bilinear([w * w], [1.0, w / q, w * w], 1.0 / rate)
            self.sections.append([float(b[0]), float(b[1]), float(b[2]), float(a[1]), float(a[2]), 0.0, 0.0])

    def run(self, x: np.ndarray) -> np.ndarray:
        y = x.astype(np.float64).tolist()
        for s in self.sections:
            b0, b1, b2, a1, a2, z1, z2 = s
            for i, v in enumerate(y):
                out = b0 * v + z1
                z1 = b1 * v - a1 * out + z2
                z2 = b2 * v - a2 * out
                y[i] = out
            s[5], s[6] = z1, z2
        return np.asarray(y, dtype=np.float32)


class BassBand:
    """The part of a sound stream that shakers can use, and what it is doing: mono in at any rate,
    low-passed 4 kHz blocks out to the feed; `level` 0..1 and thumps for the sample."""

    def __init__(self, feed: CaptureFeed, cutoff_hz: float = 120.0, rate_in: int = CAPTURE_RATE) -> None:
        if not 30.0 <= cutoff_hz <= 250.0:
            raise SourceError("sound cut-off must be 30-250 Hz")
        if rate_in % feed.rate:
            raise SourceError(f"capture rate {rate_in} is not a multiple of {feed.rate}")
        self.feed = feed
        self.factor = rate_in // feed.rate
        self._low = _Biquads(cutoff_hz, feed.rate)
        self._rest = np.zeros(0, dtype=np.float32)
        self._peak = 0.05                  # running loudest low end (decays over half a minute)
        self._slow = 0.0
        self.level = 0.0                   # 0..1 loudness of the low end, relative to its recent loudest
        self._thump = 0.0
        self._heard = 0.0                  # seconds of sound so far
        self._last_thump = -1e18
        self._lock = threading.Lock()
        self.blocks = 0

    def process(self, mono: np.ndarray) -> None:
        x = np.concatenate([self._rest, np.asarray(mono, dtype=np.float32).ravel()])
        n = len(x) // self.factor
        self._rest = x[n * self.factor :]
        if n == 0:
            return
        low = self._low.run(x[: n * self.factor].reshape(n, self.factor).mean(axis=1))
        self.feed.write(low)
        self.blocks += 1
        dt = n / self.feed.rate
        rms = float(np.sqrt(np.mean(low.astype(np.float64) ** 2)))
        self._heard += dt
        self._peak = max(self._peak * math.exp(-dt / 30.0), rms, 0.02)
        # a thump: suddenly much louder than the last third of a second - once per bang, not in the first moment
        if (rms > 2.5 * max(self._slow, 0.02 * self._peak) and rms > 0.15 * self._peak
                and self._heard > 0.5 and self._heard - self._last_thump > 0.3):
            self._last_thump = self._heard
            with self._lock:
                self._thump = max(self._thump, min(1.0, 0.3 + (rms / max(self._slow, 1e-6) - 2.5) / 6.0))
        self._slow += (rms - self._slow) * (1.0 - math.exp(-dt / 0.3))
        self.level = min(1.0, rms / self._peak)

    def take_thump(self) -> float:
        with self._lock:
            value, self._thump = self._thump, 0.0
        return value


PICTURE_SOUND_KEYS = ("sound", "sound_device", "sound_cutoff_hz")


def tap_for_picture(options: dict[str, Any], delay_s: float) -> SoundTap | None:
    """The sound that goes with a picture source, for the shakers: a sound input (``sound_device``) or
    a video file's own sound (``sound = true``), played as late as the picture is shown. None if the
    options ask for neither."""
    capture = str(options.get("capture", "file" if options.get("file") else "screen"))
    if not (options.get("sound_device") or (options.get("sound") and capture == "file")):
        return None
    own = capture == "file" and not options.get("sound_device")
    tap = SoundTap(str(options.get("sound_device", "")), float(options.get("sound_cutoff_hz", 120.0)),
                   str(options.get("file", "")) if own else "")
    tap.feed.delay_s += delay_s
    return tap


class SoundTap:
    """A sound input (or a file's sound, played in real time) going into a BassBand."""

    def __init__(self, device: str = "", cutoff_hz: float = 120.0, file: str = "", loop: bool = False,
                 backend: str = "") -> None:
        self.device, self.file, self.loop, self.backend = device, file, loop, backend
        self.feed = CaptureFeed(FEED_RATE)
        self.band = BassBand(self.feed, cutoff_hz)
        self.error = ""
        self.device_name = ""
        self._capture: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.ended = False

    def _generator(self) -> Any:
        while True:
            data = yield
            try:
                self.band.process(np.frombuffer(data, dtype=np.float32) if isinstance(data, bytes | bytearray)
                                  else np.asarray(data, dtype=np.float32))
            except Exception as exc:  # noqa: BLE001 - never let the capture thread die
                self.error = f"{type(exc).__name__}: {exc}"

    def start(self) -> None:
        self._stop.clear()
        self.ended = False
        if self.file:
            try:
                from fourdvr_extractor.soundtrack import decode_mono
            except ImportError as exc:
                raise SourceError(f"playing a file's sound needs the workstation packages ({exc})") from exc
            try:
                pcm = decode_mono(self.file, 2 * FEED_RATE)
            except RuntimeError as exc:           # the decoder's own error type is a RuntimeError
                raise SourceError(f"{self.file}: no sound track that can be read ({exc})") from exc
            if pcm is None or len(pcm) == 0:
                raise SourceError(f"{self.file}: no sound track that can be read")
            self.band = BassBand(self.feed, 120.0, 2 * FEED_RATE)
            self._thread = threading.Thread(target=self._play, args=(np.asarray(pcm, dtype=np.float32),),
                                            name="live-sound-file", daemon=True)
            self._thread.start()
            self.device_name = "sound of the file"
            return
        try:
            import miniaudio
        except ImportError as exc:
            raise SourceError("capturing sound needs the miniaudio package (pip install miniaudio)") from exc
        try:
            backends = [miniaudio.Backend.NULL] if self.backend == "null" else None
            device_id = None
            if self.device and self.backend != "null":
                names = []
                for d in miniaudio.Devices().get_captures():
                    names.append(str(d["name"]))
                    if self.device.lower() in str(d["name"]).lower():
                        device_id, self.device_name = d["id"], str(d["name"])
                        break
                else:
                    raise SourceError(f"no sound input matching {self.device!r} (there are: {', '.join(names) or 'none'})")
            self._capture = miniaudio.CaptureDevice(
                input_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=CAPTURE_RATE,
                buffersize_msec=int(BLOCK_S * 1000), device_id=device_id, backends=backends, app_name="4D VR Theatre",
            )
            gen = self._generator()
            next(gen)
            self._capture.start(gen)
            self.device_name = self.device_name or ("null device" if self.backend == "null" else "default input")
        except SourceError:
            raise
        except Exception as exc:
            self._capture = None
            raise SourceError(f"sound capture failed: {exc}") from exc

    def _play(self, pcm: np.ndarray) -> None:
        rate = 2 * FEED_RATE
        block = int(BLOCK_S * rate)
        start = time.monotonic()
        pos = 0
        while not self._stop.is_set():
            if pos >= len(pcm):
                if not self.loop:
                    self.ended = True
                    return
                start, pos = time.monotonic(), 0
            self.band.process(pcm[pos : pos + block])
            pos += block
            delay = start + pos / rate - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        cap, self._capture = self._capture, None
        if cap is not None:
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass


class SoundSource(LiveSource):
    info = SOUND
    OPTIONS = ("device", "cutoff_hz", "file", "loop", "kick", "delay_s", "backend")

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__()
        o = dict(options)
        self.tap = SoundTap(str(o.get("device", "")), float(o.get("cutoff_hz", 120.0)), str(o.get("file", "")),
                            bool(o.get("loop", False)), str(o.get("backend", "")))
        self.feed = self.tap.feed
        self.feed.delay_s += float(o.get("delay_s", 0.0))
        self.kick = float(o.get("kick", 6.0))      # m/s^2 of downward jolt at the strongest thump
        if not 0.0 <= self.kick <= 30.0:
            raise SourceError("[source] kick must be 0-30 m/s^2")

    def open(self) -> None:
        self.tap.start()

    def close(self) -> None:
        self.tap.stop()

    def run(self) -> None:
        period = 0.02
        next_at = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            thump = self.tap.band.take_thump()
            sample = LiveSample(accel=(0.0, 0.0, -self.kick * thump), rumble=self.tap.band.level, impact=thump,
                                active=not self.tap.ended, captured=now - self.feed.delay_s)
            if self.tap.band.blocks:
                self.emit(sample, now)
            if self.tap.error:
                self.last_error = self.tap.error
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_at = time.monotonic()

    def status(self) -> dict[str, Any]:
        return {**super().status(), "device": self.tap.device_name, "level": round(self.tap.band.level, 2),
                "feed_level": round(self.feed.level, 3)}
