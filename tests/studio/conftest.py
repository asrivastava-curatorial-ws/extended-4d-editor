"""Test media made on the fly: a short 360 pan with a picture flash and a sound track with bass."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

av = pytest.importorskip("av")
cv2 = pytest.importorskip("cv2")

FPS = 30
AUDIO_RATE = 48000


def _texture(w: int, h: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = cv2.resize(rng.random((h // 4, w // 4)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    img += 0.5 * cv2.resize(rng.random((h // 16, w // 16)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    img = cv2.normalize(img, None, 40, 150, cv2.NORM_MINMAX).astype(np.uint8)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def tone(seconds: float, hz: float, level: float, start: float = 0.0, stop: float | None = None) -> np.ndarray:
    t = np.arange(int(seconds * AUDIO_RATE)) / AUDIO_RATE
    x = level * np.sin(2 * np.pi * hz * t)
    x[(t < start) | (t >= (seconds if stop is None else stop))] = 0.0
    return x.astype(np.float32)


def make_media(path: Path, seconds: float = 4.0, audio: str | None = "stereo", flash_at: float | None = 1.5,
               deg_per_s: float = 30.0, width: int = 480) -> Path:
    """Equirectangular pan (camera turning left). Audio: a 60 Hz rumble from 1.0 to 2.5 s plus a quiet 1 kHz tone;
    with audio="5.1" the rumble is ONLY in the LFE channel."""
    h = width // 2
    base = _texture(width, h)
    with av.open(str(path), "w") as out:
        vs = out.add_stream("mpeg4", rate=FPS)
        vs.width, vs.height, vs.pix_fmt = width, h, "yuv420p"
        vs.bit_rate = 2_000_000
        if audio:
            layout = "stereo" if audio == "stereo" else "5.1"
            a_s = out.add_stream("aac", rate=AUDIO_RATE, layout=layout)
        for i in range(int(seconds * FPS)):
            t = i / FPS
            shift = deg_per_s * t / 360.0 * width
            img = cv2.warpAffine(base, np.float32([[1, 0, shift], [0, 1, 0]]), (width, h), borderMode=cv2.BORDER_WRAP)
            if flash_at is not None and flash_at <= t < flash_at + 2.0 / FPS:
                img = cv2.add(img, np.full_like(img, 110))
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            frame.pts = i
            frame.time_base = Fraction(1, FPS)
            for packet in vs.encode(frame):
                out.mux(packet)
        for packet in vs.encode(None):
            out.mux(packet)
        if audio:
            rumble = tone(seconds, 60.0, 0.5, 1.0, 2.5)
            hiss = tone(seconds, 1000.0, 0.05)
            if audio == "stereo":
                planes = np.stack([rumble + hiss, rumble + hiss])
            else:  # FL FR FC LFE BL BR
                planes = np.stack([hiss, hiss, hiss, rumble, 0 * hiss, 0 * hiss])
            step = 1024
            for start in range(0, planes.shape[1], step):
                chunk = np.ascontiguousarray(planes[:, start : start + step])
                frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout=layout)
                frame.sample_rate = AUDIO_RATE
                frame.pts = start
                frame.time_base = Fraction(1, AUDIO_RATE)
                for packet in a_s.encode(frame):
                    out.mux(packet)
            for packet in a_s.encode(None):
                out.mux(packet)
    return path


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:  # type: ignore[no-untyped-def]
    d = tmp_path_factory.mktemp("media")
    return {
        "stereo": make_media(d / "pan_stereo.mp4"),
        "surround": make_media(d / "pan_surround.mp4", seconds=3.0, audio="5.1", flash_at=None),
        "silent": make_media(d / "pan_silent.mp4", seconds=3.0, audio=None, flash_at=None),
    }
