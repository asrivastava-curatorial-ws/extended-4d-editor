"""The videos a stream server offers, and what is known about each without decoding it.

Nothing here needs more than the standard library: a show PC can serve and check videos without the
workstation packages. MP4 / MOV files are read directly (length, picture size, codecs, and whether
the index is at the front); for other containers PyAV is asked if it is installed.
"""

from __future__ import annotations

import hashlib
import re
import struct
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts", ".mpg", ".mpeg", ".ogv")
MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".ogv": "video/ogg",
}
PROJECTIONS = ("flat", "180", "360")
STEREO_MODES = ("mono", "sbs", "ou")
SETTINGS_FILE = "stream.toml"       # optional, in a served folder: titles, projections, programme numbers

# codec names as PyAV / FFmpeg give them, and the sample-entry codes of MP4
H264, HEVC, VP8, VP9, AV1 = "h264", "hevc", "vp8", "vp9", "av1"
_MP4_VIDEO = {"avc1": H264, "avc3": H264, "hvc1": HEVC, "hev1": HEVC, "vp08": VP8, "vp09": VP9, "av01": AV1,
              "mp4v": "mpeg4", "jpeg": "mjpeg", "apch": "prores", "apcn": "prores", "apcs": "prores", "ap4h": "prores"}
_MP4_AUDIO = {"mp4a": "aac", "Opus": "opus", "opus": "opus", "ac-3": "ac3", "ec-3": "eac3", "fLaC": "flac", ".mp3": "mp3",
              "sowt": "pcm", "twos": "pcm", "lpcm": "pcm"}


class LibraryError(ValueError):
    pass


@dataclass
class MediaItem:
    id: str                               # the name in URLs
    path: Path
    title: str
    size: int
    mime: str
    projection: str = "flat"
    stereo: str = "mono"
    duration_s: float = 0.0               # 0: not known
    width: int = 0
    height: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    faststart: bool | None = None         # MP4: the index before the data (None: not an MP4, or not known)
    programme_id: int | None = None       # the theatre's programme this is the video of
    sha256: str = ""                      # computed on demand (following the Control Server)
    notes: list[str] = field(default_factory=list)

    @property
    def bitrate(self) -> float:
        """Bits per second over the whole file (0 if the length is not known)."""
        return self.size * 8.0 / self.duration_s if self.duration_s > 0.0 else 0.0

    def browsers(self) -> tuple[str, str]:
        """("yes" | "some" | "no", why) - whether a web browser can play the file as it is."""
        suffix = self.path.suffix.lower()
        v, a = self.video_codec, self.audio_codec
        if suffix in (".mp4", ".m4v", ".mov"):
            if not v:
                return "some", "the codec could not be read; H.264 with AAC sound plays everywhere"
            if a and a not in ("aac", "mp3", "opus", "flac"):
                return "some", f"the picture ({v}) may play, the sound ({a}) will not in most browsers: AAC is the safe choice"
            if v == H264:
                if max(self.width, self.height) > 4096:
                    return "some", (f"H.264 at {self.width} x {self.height}: more than the decoders of most phones, "
                                    "headsets and browsers take (4096 wide); make a 3840-wide version for them")
                return "yes", "H.264" + (f" with {a.upper()}" if a else ", no sound") + ": plays in every browser"
            if v == HEVC:
                return "some", "HEVC (H.265): Safari and devices with a hardware decoder (most headsets); not Firefox"
            if v in (AV1, VP9):
                return "some", f"{v.upper()} in MP4: current Chrome, Edge and Firefox, newer Safari"
            return "no", f"{v} is not a codec browsers play; a player such as VLC plays it from the URL"
        if suffix == ".webm":
            if v in ("", VP8, VP9, AV1):
                return "some", "WebM: Chrome, Edge, Firefox and Safari from 2021 on; not older iPhones and iPads"
            return "no", f"{v} in WebM is not something browsers play"
        return "no", f"{suffix[1:].upper()} files are not played by browsers; a player such as VLC plays them from the URL"

    def to_dict(self) -> dict[str, Any]:
        verdict, why = self.browsers()
        return {
            "id": self.id, "title": self.title, "url": f"/media/{self.id}", "mime": self.mime, "size": self.size,
            "projection": self.projection, "stereo": self.stereo, "duration_s": round(self.duration_s, 3),
            "width": self.width, "height": self.height, "video_codec": self.video_codec, "audio_codec": self.audio_codec,
            "bitrate": round(self.bitrate), "faststart": self.faststart, "programme_id": self.programme_id,
            "browsers": verdict, "browsers_why": why, "notes": list(self.notes),
        }


# =========================================================================== reading a file's own description
def _boxes(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[str, int, int]]:
    """(type, payload start, payload end) of the boxes in data[start:end]."""
    out = []
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1 and pos + 16 <= end:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        out.append((kind.decode("latin-1"), pos + header, pos + size))
        pos += size
    return out


def _find(data: bytes, boxes: list[tuple[str, int, int]], kind: str) -> tuple[int, int] | None:
    for name, a, b in boxes:
        if name == kind:
            return a, b
    return None


def read_mp4(path: Path, limit: int = 96 * 1024 * 1024) -> dict[str, Any]:
    """Length, picture size, codecs and index position of an MP4 / MOV file, from its boxes."""
    info: dict[str, Any] = {}
    size = path.stat().st_size
    order: list[str] = []
    moov: bytes | None = None
    with path.open("rb") as fh:
        pos = 0
        while pos + 8 <= size and len(order) < 64:
            fh.seek(pos)
            head = fh.read(16)
            if len(head) < 8:
                break
            box, kind = struct.unpack_from(">I4s", head)
            header = 8
            if box == 1 and len(head) == 16:
                box = struct.unpack_from(">Q", head, 8)[0]
                header = 16
            elif box == 0:
                box = size - pos
            if box < header:
                break
            name = kind.decode("latin-1")
            order.append(name)
            if name == "moov" and box <= limit:
                fh.seek(pos + header)
                moov = fh.read(box - header)
            pos += box
    if "ftyp" not in order and "moov" not in order:
        raise LibraryError("not an MP4 / MOV file")
    if "moov" in order and "mdat" in order:
        info["faststart"] = order.index("moov") < order.index("mdat")
    if moov is None:
        return info
    top = _boxes(moov)
    mvhd = _find(moov, top, "mvhd")
    if mvhd is not None:
        a = mvhd[0]
        if moov[a] == 1:
            scale, duration = struct.unpack_from(">IQ", moov, a + 20)
        else:
            scale, duration = struct.unpack_from(">II", moov, a + 12)
        if scale:
            info["duration_s"] = duration / scale
    for name, a, b in top:
        if name != "trak":
            continue
        trak = _boxes(moov, a, b)
        mdia = _find(moov, trak, "mdia")
        if mdia is None:
            continue
        inside = _boxes(moov, *mdia)
        hdlr = _find(moov, inside, "hdlr")
        handler = moov[hdlr[0] + 8 : hdlr[0] + 12].decode("latin-1") if hdlr else ""
        minf = _find(moov, inside, "minf")
        stbl = _find(moov, _boxes(moov, *minf), "stbl") if minf else None
        stsd = _find(moov, _boxes(moov, *stbl), "stsd") if stbl else None
        entry = moov[stsd[0] + 12 : stsd[0] + 16].decode("latin-1") if stsd and stsd[1] - stsd[0] >= 16 else ""
        if handler == "vide" and "video_codec" not in info:
            info["video_codec"] = _MP4_VIDEO.get(entry, entry.strip().lower())
            if stsd and stsd[1] - stsd[0] >= 8 + 8 + 28:
                info["width"], info["height"] = struct.unpack_from(">HH", moov, stsd[0] + 8 + 8 + 24)
        elif handler == "soun" and "audio_codec" not in info:
            info["audio_codec"] = _MP4_AUDIO.get(entry, entry.strip().lower())
    return info


def read_with_pyav(path: Path) -> dict[str, Any]:
    """The same through PyAV, for any container it knows. Raises ImportError where PyAV is not installed."""
    import av

    info: dict[str, Any] = {}
    with av.open(str(path)) as container:
        if container.duration:
            info["duration_s"] = container.duration / 1_000_000.0
        for stream in container.streams:
            if stream.type == "video" and "video_codec" not in info:
                context: Any = stream.codec_context
                info["video_codec"] = context.name
                info["width"], info["height"] = context.width, context.height
            elif stream.type == "audio" and "audio_codec" not in info:
                info["audio_codec"] = stream.codec_context.name
    return info


def guess_format(width: int, height: int, name: str = "") -> tuple[str, str]:
    """(projection, stereo) from the file name's hints and the picture's shape - a guess to be confirmed.
    Names: 360 / 180 / vr180, sbs / lr / 3dh (side by side), ou / tb / 3dv (over-under), mono, flat."""
    words = set(re.split(r"[^a-z0-9]+", name.lower()))
    stereo = "sbs" if words & {"sbs", "lr", "3dh", "hsbs"} else "ou" if words & {"ou", "tb", "3dv", "overunder"} else ""
    projection = ("360" if words & {"360", "vr360", "equirect", "equirectangular"} else
                  "180" if words & {"180", "vr180"} else "flat" if "flat" in words else "")
    if not projection and width and height:
        aspect = width / max(height, 1)
        if abs(aspect - 2.0) < 0.05:
            projection, stereo = "360", stereo or "mono"
        elif abs(aspect - 1.0) < 0.05:
            projection, stereo = ("360", stereo or "ou") if height >= 2160 else ("180", stereo or "mono")
        elif abs(aspect - 4.0) < 0.1:
            projection, stereo = "360", stereo or "sbs"
    if "mono" in words:
        stereo = "mono"
    return projection or "flat", stereo or "mono"


def describe(path: Path, item_id: str = "") -> MediaItem:
    """What can be said about a video file without decoding it."""
    path = Path(path)
    if not path.is_file():
        raise LibraryError(f"{path} is not a file")
    suffix = path.suffix.lower()
    item = MediaItem(item_id or slug(path.stem), path, path.stem.replace("_", " "), path.stat().st_size,
                     MIME.get(suffix, "application/octet-stream"))
    info: dict[str, Any] = {}
    if suffix in (".mp4", ".m4v", ".mov"):
        try:
            info = read_mp4(path)
        except (LibraryError, OSError, struct.error, UnicodeDecodeError) as exc:
            item.notes.append(f"its description could not be read ({exc})")
    if not info.get("video_codec") or not info.get("duration_s"):
        try:
            info = {**read_with_pyav(path), **{k: v for k, v in info.items() if k == "faststart"}}
        except ImportError:
            if suffix not in (".mp4", ".m4v", ".mov"):
                item.notes.append("length and codecs are not known here (the workstation packages read them)")
        except Exception as exc:  # noqa: BLE001 - whatever a damaged file makes a decoder say
            item.notes.append(f"it could not be opened as a video ({type(exc).__name__}: {exc})")
    item.duration_s = float(info.get("duration_s", 0.0) or 0.0)
    item.width, item.height = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
    item.video_codec, item.audio_codec = str(info.get("video_codec", "")), str(info.get("audio_codec", ""))
    item.faststart = info.get("faststart")
    item.projection, item.stereo = guess_format(item.width, item.height, path.stem)
    if item.faststart is False:
        item.notes.append("the index is at the end of the file: playback starts later and seeks cost more "
                          "('fourdvr stream prepare' moves it to the front without re-encoding)")
    return item


def slug(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return out or "video"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# =========================================================================== the set of videos a server offers
class Library:
    def __init__(self) -> None:
        self.items: dict[str, MediaItem] = {}

    def add(self, path: str | Path, **settings: Any) -> MediaItem:
        p = Path(path).expanduser().resolve()
        base = slug(p.stem)
        item_id, n = base + p.suffix.lower(), 2
        while item_id in self.items:              # two files of the same name in different folders
            item_id, n = f"{base}-{n}{p.suffix.lower()}", n + 1
        item = describe(p, item_id)
        self.apply(item, settings)
        self.items[item.id] = item
        if not settings.get("title"):             # two videos of the same name (ride.mp4, ride.webm): say which is which
            same = [other for other in self.items.values() if other.path.stem == p.stem]
            if len(same) > 1:
                for other in same:
                    if other.title == other.path.stem.replace("_", " "):
                        other.title = other.path.name
        return item

    @staticmethod
    def apply(item: MediaItem, settings: dict[str, Any]) -> None:
        unknown = sorted(set(settings) - {"title", "projection", "stereo", "programme_id", "duration_s"})
        if unknown:
            raise LibraryError(f"{item.path.name}: unknown setting(s) {', '.join(unknown)}")
        projection, stereo = settings.get("projection"), settings.get("stereo")
        if projection is not None:
            if str(projection) not in PROJECTIONS:
                raise LibraryError(f"{item.path.name}: projection must be one of {', '.join(PROJECTIONS)}")
            item.projection = str(projection)
        if stereo is not None:
            if str(stereo) not in STEREO_MODES:
                raise LibraryError(f"{item.path.name}: stereo must be one of {', '.join(STEREO_MODES)}")
            item.stereo = str(stereo)
        if settings.get("title"):
            item.title = str(settings["title"])
        if settings.get("programme_id") is not None:
            item.programme_id = int(settings["programme_id"])
        if settings.get("duration_s"):
            item.duration_s = float(settings["duration_s"])

    def add_path(self, path: str | Path, **settings: Any) -> list[MediaItem]:
        """A file, or every video in a folder (with the folder's stream.toml, if it has one)."""
        p = Path(path).expanduser()
        if p.is_dir():
            per_file = folder_settings(p)
            files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_SUFFIXES)
            unknown = sorted(set(per_file) - {f.name for f in files})
            if unknown:
                raise LibraryError(f"{p / SETTINGS_FILE}: no such video in this folder: {', '.join(unknown)}")
            return [self.add(f, **per_file.get(f.name, {})) for f in files]
        if not p.is_file():
            raise LibraryError(f"{p} does not exist")
        return [self.add(p, **settings)]

    def get(self, item_id: str) -> MediaItem | None:
        return self.items.get(item_id)

    def for_programme(self, programme_id: int, filename: str = "", sha256: str = "") -> MediaItem | None:
        """The video of one of the theatre's programmes: by its number in stream.toml, else by the checksum the
        Control Server has for it, else by the file's name."""
        for item in self.items.values():
            if item.programme_id == programme_id:
                return item
        if sha256:
            for item in self.items.values():
                if not item.sha256:
                    item.sha256 = file_sha256(item.path)
                if item.sha256 == sha256:
                    return item
        for item in self.items.values():
            if filename and item.path.name == filename:
                return item
        return None

    def listing(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.items.values()]


def folder_settings(folder: Path) -> dict[str, dict[str, Any]]:
    """``stream.toml`` of a folder: ``[video."ride.mp4"]`` tables with title, projection, stereo, programme_id."""
    path = folder / SETTINGS_FILE
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LibraryError(f"{path}: {exc}") from exc
    unknown = sorted(set(data) - {"video"})
    if unknown:
        raise LibraryError(f"{path}: unknown section(s) {', '.join(unknown)} (only [video.\"name\"] tables)")
    videos = data.get("video", {})
    if not isinstance(videos, dict) or not all(isinstance(v, dict) for v in videos.values()):
        raise LibraryError(f'{path}: write one table per file: [video."ride.mp4"]')
    return {str(name): dict(table) for name, table in videos.items()}
