"""Making a video fit for streaming, and a test video. These need the workstation packages (PyAV; OpenCV
for the test video); serving and checking do not.

    prepare     the same picture and sound in an MP4 whose index is at the front - nothing is re-encoded, it takes
                seconds - or, with transcode=True, re-encoded to H.264 with AAC sound, which every browser plays
    test video  a clock, a bar that crosses the picture once a second and a beep on every full second: two screens
                side by side show at a glance how far apart they are
"""

from __future__ import annotations

import math
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

Progress = Callable[[float, str], None]


class MediaError(RuntimeError):
    pass


def _av() -> Any:
    try:
        import av
    except ImportError as exc:
        raise MediaError(f"this needs the workstation packages ({exc}): pip install 'extended-4d-editor[studio]'") from exc
    return av


def ffmpeg_command(source: Path, target: Path, transcode: bool, height: int = 0, bitrate_mbit: float = 0.0) -> str:
    """The same as `prepare` for those who have the ffmpeg program: a command to paste."""
    if not transcode:
        return f'ffmpeg -i "{source}" -map 0:v:0 -map 0:a? -c copy -movflags +faststart "{target}"'
    scale = f' -vf "scale=-2:{height}"' if height else ""
    rate = f" -b:v {bitrate_mbit:g}M -maxrate {bitrate_mbit * 1.5:g}M -bufsize {bitrate_mbit * 3:g}M" if bitrate_mbit else " -crf 20"
    return (f'ffmpeg -i "{source}" -map 0:v:0 -map 0:a?{scale} -c:v libx264 -preset medium{rate} -pix_fmt yuv420p '
            f'-c:a aac -b:a 160k -movflags +faststart "{target}"')


def prepare(source: str | Path, target: str | Path, transcode: bool = False, height: int = 0, bitrate_mbit: float = 0.0,
            progress: Progress | None = None) -> dict[str, Any]:
    """Write `target` (.mp4). Without `transcode` the streams are copied as they are."""
    av = _av()
    src, dst = Path(source), Path(target)
    if dst.suffix.lower() not in (".mp4", ".m4v"):
        raise MediaError("the result is an MP4 file: give a name ending in .mp4")
    if src.resolve() == dst.resolve():
        raise MediaError("the result must be another file than the source")
    part = dst.with_name(dst.name + ".part")
    say = progress or (lambda _f, _t: None)
    try:
        with av.open(str(src)) as inp:
            video = next((s for s in inp.streams if s.type == "video"), None)
            audio = next((s for s in inp.streams if s.type == "audio"), None)
            if video is None:
                raise MediaError(f"{src.name} has no picture")
            total = float(inp.duration / 1_000_000.0) if inp.duration else 0.0
            with av.open(str(part), "w", format="mp4", options={"movflags": "+faststart"}) as out:
                if not transcode:
                    mapping = {video.index: out.add_stream_from_template(video)}
                    if audio is not None:
                        mapping[audio.index] = out.add_stream_from_template(audio)
                    for packet in inp.demux([s for s in (video, audio) if s is not None]):
                        if packet.dts is None:
                            continue
                        if total and packet.stream.index == video.index and packet.pts is not None:
                            say(min(1.0, float(packet.pts * packet.time_base) / total), "copying")
                        packet.stream = mapping[packet.stream.index]
                        out.mux(packet)
                else:
                    _transcode(av, inp, out, video, audio, height, bitrate_mbit, total, say)
        part.replace(dst)
    except MediaError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        hint = "" if transcode else " (some codecs cannot be put into MP4 as they are: try --transcode)"
        raise MediaError(f"{src.name}: {type(exc).__name__}: {exc}{hint}") from exc
    say(1.0, "done")
    return {"path": str(dst), "size": dst.stat().st_size, "transcoded": transcode}


def _transcode(av: Any, inp: Any, out: Any, video: Any, audio: Any, height: int, bitrate_mbit: float, total: float,
               say: Progress) -> None:
    rate = video.average_rate or video.guessed_rate or Fraction(30, 1)
    width, high = video.codec_context.width, video.codec_context.height
    if height and height < high:
        width, high = round(width * height / high / 2.0) * 2, int(height // 2) * 2
    width, high = width - width % 2, high - high % 2
    v_out = out.add_stream("libx264", rate=rate)
    v_out.width, v_out.height, v_out.pix_fmt = width, high, "yuv420p"
    options = {"preset": "medium"}
    if bitrate_mbit:
        v_out.bit_rate = int(bitrate_mbit * 1_000_000)
        options.update(maxrate=f"{bitrate_mbit * 1.5:g}M", bufsize=f"{bitrate_mbit * 3:g}M")
    else:
        options["crf"] = "20"
    v_out.options = options
    a_out = None
    resampler = None
    if audio is not None:
        a_out = out.add_stream("aac", rate=48000)
        a_out.bit_rate = 160_000
        a_out.layout = "stereo"
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
    for frame in inp.decode(*[s for s in (video, audio) if s is not None]):
        if isinstance(frame, av.VideoFrame):
            if total and frame.time is not None:
                say(min(1.0, frame.time / total), "encoding")
            if frame.width != width or frame.height != high or frame.format.name != "yuv420p":
                frame = frame.reformat(width=width, height=high, format="yuv420p")
            for packet in v_out.encode(frame):
                out.mux(packet)
        elif a_out is not None and resampler is not None:
            for chunk in resampler.resample(frame):
                for packet in a_out.encode(chunk):
                    out.mux(packet)
    for packet in v_out.encode(None):
        out.mux(packet)
    if a_out is not None and resampler is not None:
        for chunk in resampler.resample(None):
            for packet in a_out.encode(chunk):
                out.mux(packet)
        for packet in a_out.encode(None):
            out.mux(packet)


# =========================================================================== the test video
CODECS = {".mp4": ("h264", "vp9"), ".webm": ("vp8", "vp9")}       # the first is the default


def test_video(target: str | Path, seconds: float = 30.0, width: int = 1280, height: int = 720, fps: int = 30,
               projection: str = "flat", progress: Progress | None = None, codec: str = "") -> dict[str, Any]:
    """A video for judging by eye and ear whether screens are in step. `.mp4` (H.264 with AAC, which plays
    everywhere - or VP9 with Opus, the open codecs) or `.webm` (VP8 or VP9, Opus). For projection "360" the picture
    is an equirectangular grid with the directions written on it."""
    av = _av()
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise MediaError(f"the test video needs the workstation packages ({exc})") from exc
    dst = Path(target)
    suffix = dst.suffix.lower()
    if suffix not in (".mp4", ".webm"):
        raise MediaError("give a name ending in .mp4 or .webm")
    if projection not in ("flat", "360"):
        raise MediaError("the test video is flat or 360")
    codec = codec or CODECS[suffix][0]
    if codec not in CODECS[suffix]:
        raise MediaError(f"a {suffix} test video is made with {' or '.join(CODECS[suffix])}")
    if not (1.0 <= seconds <= 3600.0 and 320 <= width <= 7680 and 180 <= height <= 4320 and 10 <= fps <= 60):
        raise MediaError("seconds 1-3600, size 320x180 to 7680x4320, 10-60 pictures a second")
    width, height = width - width % 2, height - height % 2
    say = progress or (lambda _f, _t: None)
    rate = 48000
    scale = height / 720.0
    font = cv2.FONT_HERSHEY_SIMPLEX
    back = np.zeros((height, width, 3), np.uint8)
    back[:] = (23, 17, 13)                         # the Editor's background, as BGR
    if projection == "360":
        for k in range(0, 360, 30):                # meridians and parallels of the sphere, and where they are
            x = round(k / 360.0 * width)
            cv2.line(back, (x, 0), (x, height), (61, 54, 48), max(1, int(2 * scale)))
        for k in range(30, 180, 30):
            y = round(k / 180.0 * height)
            cv2.line(back, (0, y), (width, y), (61, 54, 48), max(1, int(2 * scale)))
        for label, lon in (("FRONT", 0), ("RIGHT", 90), ("BACK", 180), ("LEFT", -90)):
            x = int(((lon / 360.0 + 0.5) % 1.0) * width)
            size = cv2.getTextSize(label, font, 1.6 * scale, max(2, int(3 * scale)))[0]
            cv2.putText(back, label, (min(max(4, x - size[0] // 2), width - size[0] - 4), int(height * 0.62)), font, 1.6 * scale,
                        (255, 166, 88), max(2, int(3 * scale)), cv2.LINE_AA)
    else:
        for k in range(1, 10):
            x = round(k / 10.0 * width)
            cv2.line(back, (x, int(height * 0.80)), (x, int(height * 0.86)), (61, 54, 48), max(1, int(2 * scale)))
    frames = round(seconds * fps)
    part = dst.with_name(dst.name + ".part")
    try:
        with av.open(str(part), "w", format="mp4" if suffix == ".mp4" else "webm",
                     options={"movflags": "+faststart"} if suffix == ".mp4" else {}) as out:
            if codec == "h264":
                v = out.add_stream("libx264", rate=fps)
                v.options = {"preset": "veryfast", "crf": "23", "g": str(fps)}
                a = out.add_stream("aac", rate=rate)
            else:
                v = out.add_stream("libvpx" if codec == "vp8" else "libvpx-vp9", rate=fps)
                v.bit_rate = int(2_000_000 * max(1.0, scale * scale))
                v.options = {"deadline": "realtime", "cpu-used": "8", "g": str(fps)}
                a = out.add_stream("libopus", rate=rate)
            v.width, v.height, v.pix_fmt = width, height, "yuv420p"
            a.layout = "stereo"
            for i in range(frames):
                t = i / fps
                img = back.copy()
                within = t - math.floor(t)
                x = int(within * width)            # the bar: across the picture once a second
                cv2.rectangle(img, (max(0, x - int(6 * scale)), int(height * 0.80)), (min(width, x + int(6 * scale)), int(height * 0.86)),
                              (255, 166, 88), -1)
                if within < 2.0 / fps:             # the flash on the full second (with the beep)
                    cv2.rectangle(img, (int(width * 0.42), int(height * 0.12)), (int(width * 0.58), int(height * 0.12) + int(width * 0.16 * 0.4)),
                                  (255, 255, 255), -1)
                text = f"{int(t // 60):02d}:{t % 60:04.1f}"
                size = cv2.getTextSize(text, font, 4.0 * scale, max(2, int(8 * scale)))[0]
                cv2.putText(img, text, ((width - size[0]) // 2, int(height * 0.50)), font, 4.0 * scale, (243, 237, 230),
                            max(2, int(8 * scale)), cv2.LINE_AA)
                cv2.putText(img, f"frame {i}", (int(width * 0.04), int(height * 0.95)), font, 0.9 * scale, (158, 148, 139),
                            max(1, int(2 * scale)), cv2.LINE_AA)
                frame = av.VideoFrame.from_ndarray(img, format="bgr24")
                frame.pts, frame.time_base = i, Fraction(1, fps)
                for packet in v.encode(frame):
                    out.mux(packet)
                if i % fps == 0:
                    say(i / max(frames, 1), "drawing")
            for packet in v.encode(None):
                out.mux(packet)
            n = round(seconds * rate)
            ts = np.arange(n) / rate
            beep = (np.sin(2 * np.pi * 1000.0 * ts) * ((ts % 1.0) < 0.05) * 0.5).astype(np.float32)
            resampler = av.AudioResampler(format=a.codec_context.format.name if a.codec_context.format else "fltp",
                                          layout="stereo", rate=rate)
            for start in range(0, n, 960):
                block = np.ascontiguousarray(np.stack([beep[start : start + 960]] * 2))
                chunk = av.AudioFrame.from_ndarray(block, format="fltp", layout="stereo")
                chunk.sample_rate, chunk.pts, chunk.time_base = rate, start, Fraction(1, rate)
                for piece in resampler.resample(chunk):
                    for packet in a.encode(piece):
                        out.mux(packet)
            for piece in resampler.resample(None):
                for packet in a.encode(piece):
                    out.mux(packet)
            for packet in a.encode(None):
                out.mux(packet)
        part.replace(dst)
    except Exception as exc:
        part.unlink(missing_ok=True)
        if isinstance(exc, MediaError):
            raise
        raise MediaError(f"the test video could not be made ({type(exc).__name__}: {exc})") from exc
    say(1.0, "done")
    return {"path": str(dst), "size": dst.stat().st_size, "seconds": seconds, "width": width, "height": height, "fps": fps,
            "codec": codec}
