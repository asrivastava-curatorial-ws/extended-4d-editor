"""The programme's sound track: decoding, level envelopes for the editor, and the bass track that
drives subwoofers and bass shakers.

Decoding uses PyAV (FFmpeg libraries inside a Python package - nothing to install on the PC) and
falls back to an ``ffmpeg`` program on the PATH. Without either, everything else still works; only
the bass track and the audio-based suggestions are unavailable.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal

from fourdvr_fx import BassTrack

ANALYSIS_RATE = 8000          # Hz, mono: all that bass, envelopes and the waveform display need
FEATURE_RATE = 100.0          # envelope values per second
BASS_BAND_HZ = (25.0, 120.0)
SILENT = 0.003                # peak below this (-50 dBFS): no usable bass content
MAX_NORMALISE = 4.0           # never amplify the bass track by more than +12 dB

ProgressFn = Callable[[float, str], None]


class SoundtrackError(RuntimeError):
    """The sound track could not be decoded."""


@dataclass
class Soundtrack:
    rate_hz: int
    mono: np.ndarray                       # float32, mix of the full-range channels
    lfe: np.ndarray | None = None          # float32, the LFE channel of a surround source
    decoder: str = ""
    layout: str = ""
    level: np.ndarray = field(default_factory=lambda: np.zeros(0))     # 0..1 loudness envelope
    bass: np.ndarray = field(default_factory=lambda: np.zeros(0))      # 0..1 envelope of 25-120 Hz
    peak_lo: np.ndarray = field(default_factory=lambda: np.zeros(0))   # waveform extremes (display)
    peak_hi: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def duration_s(self) -> float:
        return len(self.mono) / float(self.rate_hz)

    def low_end(self) -> np.ndarray:
        """What a subwoofer should get: the full-range mix plus the LFE channel."""
        return self.mono if self.lfe is None else self.mono + self.lfe

    def summary(self) -> dict[str, object]:
        return {
            "decoder": self.decoder,
            "layout": self.layout,
            "lfe_channel": self.lfe is not None,
            "duration_s": round(self.duration_s, 3),
            "peak": round(float(np.max(np.abs(self.mono))) if len(self.mono) else 0.0, 4),
        }


NO_DECODER = "no audio decoder: install the 'av' Python package (pip install av) or the ffmpeg program"


def available_decoder() -> str:
    """'pyav', 'ffmpeg' or '' (no way to read audio on this installation)."""
    try:
        import av  # noqa: F401

        return "pyav"
    except ImportError:
        return "ffmpeg" if shutil.which("ffmpeg") else ""


def _decode_pyav(
    path: Path, rate: int, progress: ProgressFn | None, cancel: Callable[[], bool] | None
) -> tuple[np.ndarray, np.ndarray | None, str] | None:
    import av

    with av.open(str(path)) as container:
        if not container.streams.audio:
            return None
        stream = container.streams.audio[0]
        names = [c.name for c in stream.layout.channels]
        lfe_index = names.index("LFE") if "LFE" in names and len(names) > 2 else None
        to_mono = av.AudioResampler(format="s16", layout="mono", rate=rate)
        to_all = av.AudioResampler(format="s16p", layout=stream.layout.name, rate=rate) if lfe_index is not None else None
        total = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / 1e6
        offset = float(stream.start_time * stream.time_base) if stream.start_time else 0.0
        mono: list[np.ndarray] = []
        lfe: list[np.ndarray] = []
        count = 0
        for frame in container.decode(stream):
            if to_all is not None and lfe_index is not None:
                lfe.extend(out.to_ndarray()[lfe_index].copy() for out in to_all.resample(frame))
            mono.extend(out.to_ndarray().reshape(-1).copy() for out in to_mono.resample(frame))
            count += 1
            if count % 200 == 0:
                if cancel is not None and cancel():
                    raise SoundtrackError("cancelled")
                if progress is not None and total > 0 and frame.time is not None:
                    progress(min(1.0, frame.time / total), "reading the sound track")
        mono.extend(out.to_ndarray().reshape(-1).copy() for out in to_mono.resample(None))
        if to_all is not None and lfe_index is not None:
            lfe.extend(out.to_ndarray()[lfe_index].copy() for out in to_all.resample(None))
        layout = stream.layout.name
    x = np.concatenate(mono).astype(np.float32) / 32768.0 if mono else np.zeros(0, np.float32)
    sub = np.concatenate(lfe).astype(np.float32) / 32768.0 if lfe else None
    if offset > 0.001:       # the audio starts later than the picture: keep it aligned with the timeline
        pad = np.zeros(round(offset * rate), np.float32)
        x = np.concatenate([pad, x])
        sub = np.concatenate([pad, sub]) if sub is not None else None
    if sub is not None:
        n = min(len(x), len(sub))
        x, sub = x[:n], sub[:n]
    return x, sub, layout


def _decode_ffmpeg(path: Path, rate: int) -> tuple[np.ndarray, np.ndarray | None, str] | None:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise SoundtrackError(NO_DECODER)
    cmd = [exe, "-v", "error", "-nostdin", "-i", str(path), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
           "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        if "matches no streams" in message or "does not contain any stream" in message:
            return None
        raise SoundtrackError(f"ffmpeg could not read the sound track: {message[-300:]}")
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0, None, "mono mix (ffmpeg)"


def decode_mono(path: str | Path, rate: int, cancel: Callable[[], bool] | None = None) -> np.ndarray | None:
    """The mono mix of a media file's first audio stream at `rate` Hz, -1..1. None: the file has no sound."""
    p = Path(path)
    try:
        if available_decoder() == "pyav":
            result = _decode_pyav(p, rate, None, cancel)
        else:
            result = _decode_ffmpeg(p, rate)
    except SoundtrackError:
        raise
    except Exception as exc:
        raise SoundtrackError(f"could not read the sound track of {p.name}: {exc}") from exc
    return None if result is None else result[0]


def _envelope(x: np.ndarray, hop: int) -> np.ndarray:
    n = len(x) // hop
    if n == 0:
        return np.zeros(0)
    return np.sqrt(np.mean(np.square(x[: n * hop].reshape(n, hop), dtype=np.float64), axis=1))


def _normalised(env: np.ndarray, floor: float) -> np.ndarray:
    """Envelope relative to the programme's loud passages (99.5th percentile); silence stays silence."""
    if not len(env):
        return env
    top = float(np.percentile(env, 99.5))
    return np.clip(env / top, 0.0, 1.0) if top > floor else np.zeros(len(env))


def load_soundtrack(
    path: str | Path,
    rate: int = ANALYSIS_RATE,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Soundtrack | None:
    """Decode and measure the first audio stream of a media file. None: the file has no sound."""
    p = Path(path)
    try:
        import av  # noqa: F401

        decoder = "pyav"
    except ImportError:
        decoder = "ffmpeg"
    try:
        result = _decode_pyav(p, rate, progress, cancel) if decoder == "pyav" else _decode_ffmpeg(p, rate)
    except SoundtrackError:
        raise
    except Exception as exc:
        raise SoundtrackError(f"could not read the sound track of {p.name}: {exc}") from exc
    if result is None:
        return None
    mono, lfe, layout = result
    st = Soundtrack(rate, mono, lfe, decoder, layout)
    hop = round(rate / FEATURE_RATE)
    n = len(mono) // hop
    if n:
        frames = mono[: n * hop].reshape(n, hop)
        st.peak_lo, st.peak_hi = frames.min(axis=1), frames.max(axis=1)
        st.level = _normalised(_envelope(mono, hop), 1e-4)
        if len(mono) > 64:
            sos = signal.butter(4, BASS_BAND_HZ, btype="band", fs=rate, output="sos")
            st.bass = _normalised(_envelope(signal.sosfiltfilt(sos, st.low_end()), hop), 5e-4)
        else:
            st.bass = np.zeros(n)
    if progress is not None:
        progress(1.0, "sound track read")
    return st


def default_bass_rate(duration_s: float) -> int:
    """Sample rate of the embedded bass track: 2 kHz, lower for long programmes (header size limit)."""
    return 2000 if duration_s <= 1200 else 1000 if duration_s <= 2700 else 500


def make_bass_track(
    st: Soundtrack, duration_s: float | None = None, cutoff_hz: float = 120.0, sample_rate_hz: int | None = None
) -> BassTrack | None:
    """Low-pass, band-limit and normalise the sound track for subwoofers / bass shakers.

    Returns None when the sound track has no usable low-frequency content.
    """
    if not 20.0 <= cutoff_hz <= 250.0:
        raise ValueError("bass cut-off must be 20-250 Hz")
    total = duration_s if duration_s is not None else st.duration_s
    out_rate = sample_rate_hz or default_bass_rate(total)
    if out_rate < 3.2 * cutoff_hz:
        raise ValueError(f"bass sample rate {out_rate} Hz is too low for a {cutoff_hz:g} Hz cut-off")
    x = st.low_end().astype(np.float64)
    if len(x) < 256:
        return None
    low = signal.butter(4, cutoff_hz, btype="low", fs=st.rate_hz, output="sos")
    rumble = signal.butter(2, 18.0, btype="high", fs=st.rate_hz, output="sos")   # no DC / infrasound into the shakers
    y = signal.sosfiltfilt(rumble, signal.sosfiltfilt(low, x))
    g = math.gcd(out_rate, st.rate_hz)
    y = signal.resample_poly(y, out_rate // g, st.rate_hz // g)
    want = round(total * out_rate)
    y = y[:want] if len(y) >= want else np.concatenate([y, np.zeros(want - len(y))])
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak < SILENT:
        return None
    gain = min(0.9 / peak, MAX_NORMALISE)
    return BassTrack.from_float(
        y * gain, out_rate, cutoff_hz=cutoff_hz,
        source=f"sound track ({st.layout or 'audio'}), low-pass {cutoff_hz:g} Hz, level {20 * math.log10(gain):+.1f} dB",
    )
