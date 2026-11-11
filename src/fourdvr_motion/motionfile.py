"""Reader/writer for the 4D VR Theatre ``.motion`` file format 1.0.

SHARED MODULE. Identical copies of this file live in:
  * 4D-VR-Theatre-Control-Server     (fourdvr_server/motionfile.py)   - validation
  * 4D-VR-Theatre-Motion-Controller  (fourdvr_motion/motionfile.py)   - playback
  * 4D-VR-Theatre-Motion-Extractor   (fourdvr_extractor/motionfile.py) - generation
The specification is MOTION_FORMAT.md.

Binary layout (all integers little-endian)::

    offset 0   8 bytes  magic  b"4DVRMOT\\x00"
    offset 8   u16      format major version (1)
    offset 10  u16      format minor version (0)
    offset 12  u32      H = length of the JSON header in bytes (space padded so
                        that 16 + H is a multiple of 8)
    offset 16  H bytes  UTF-8 JSON header
    offset 16+H         sample block: N x 6 float32 little-endian
                        (surge, sway, heave, roll, pitch, yaw) per sample,
                        optionally zlib-compressed ("compression": "zlib").

Sample i is at time t_i = i / sample_rate_hz. Units: metres and radians.
Coordinate system: X forward, Y left, Z up; R = Rz(yaw)·Ry(pitch)·Rx(roll).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

MAGIC = b"4DVRMOT\x00"
FORMAT_NAME = "4D-VR-THEATRE-MOTION"
FORMAT_MAJOR = 1
FORMAT_MINOR = 0
FORMAT_VERSION = f"{FORMAT_MAJOR}.{FORMAT_MINOR}.0"
CHANNELS = ("surge", "sway", "heave", "roll", "pitch", "yaw")
UNITS = {"surge": "m", "sway": "m", "heave": "m", "roll": "rad", "pitch": "rad", "yaw": "rad"}
COORDINATE_SYSTEM: dict[str, str] = {
    "name": "4DVR-PLATFORM-ISO8855",
    "x": "forward",
    "y": "left",
    "z": "up",
    "rotation_order": "intrinsic Z-Y-X: R = Rz(yaw) * Ry(pitch) * Rx(roll)",
    "positive_roll": "right side down",
    "positive_pitch": "nose down",
    "positive_yaw": "nose left (counter-clockwise seen from above)",
    "origin": "offset from the platform neutral pose; rotations about the configured rotation centre",
}
SAMPLE_ENCODING = "float32-le"
COMPRESSIONS = ("none", "zlib")
MIN_SAMPLE_RATE_HZ = 50.0
MAX_SAMPLE_RATE_HZ = 200.0
DEFAULT_SAMPLE_RATE_HZ = 100.0
_PREFIX = struct.Struct("<8sHHI")
REQUIRED_KEYS = (
    "format",
    "format_version",
    "programme_id",
    "video",
    "duration_s",
    "sample_rate_hz",
    "sample_count",
    "channels",
    "units",
    "coordinate_system",
    "sample_encoding",
    "compression",
    "data_sha256",
    "generation",
)


class MotionFormatError(ValueError):
    """The file is not a valid .motion file (corrupt, truncated, wrong version...)."""


@dataclass
class MotionFile:
    header: dict[str, Any]
    samples: np.ndarray  # shape (N, 6), float64

    @property
    def programme_id(self) -> int:
        return int(self.header["programme_id"])

    @property
    def sample_rate_hz(self) -> float:
        return float(self.header["sample_rate_hz"])

    @property
    def duration_s(self) -> float:
        return float(self.header["duration_s"])

    @property
    def sample_count(self) -> int:
        return int(self.samples.shape[0])

    @property
    def video_sha256(self) -> str:
        return str(self.header.get("video", {}).get("sha256", ""))

    def times(self) -> np.ndarray:
        return np.arange(self.sample_count, dtype=np.float64) / self.sample_rate_hz


def expected_sample_count(duration_s: float, sample_rate_hz: float) -> int:
    return math.floor(duration_s * sample_rate_hz + 1e-9) + 1


def build_header(
    *,
    programme_id: int,
    duration_s: float,
    sample_rate_hz: float,
    video: dict[str, Any],
    generation: dict[str, Any],
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a header with all descriptive fields; data fields are filled by encode_motion()."""
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "programme_id": int(programme_id),
        "video": dict(video),
        "duration_s": float(duration_s),
        "sample_rate_hz": float(sample_rate_hz),
        "sample_count": 0,
        "channels": list(CHANNELS),
        "units": dict(UNITS),
        "coordinate_system": dict(COORDINATE_SYSTEM),
        "sample_encoding": SAMPLE_ENCODING,
        "compression": "none",
        "data_sha256": "",
        "timestamps": "implicit: t_i = i / sample_rate_hz, first sample at t = 0",
        "generation": dict(generation),
        "validation": dict(validation or {}),
    }


def encode_motion(header: dict[str, Any], samples: np.ndarray, compression: str = "none") -> bytes:
    if compression not in COMPRESSIONS:
        raise MotionFormatError(f"unknown compression {compression!r}")
    arr = np.ascontiguousarray(np.asarray(samples, dtype="<f4"))
    if arr.ndim != 2 or arr.shape[1] != 6:
        raise MotionFormatError(f"samples must have shape (N, 6), got {arr.shape}")
    if arr.shape[0] < 2:
        raise MotionFormatError("at least two samples are required")
    if not np.all(np.isfinite(arr)):
        raise MotionFormatError("samples contain NaN or infinite values")
    rate = float(header["sample_rate_hz"])
    if not (MIN_SAMPLE_RATE_HZ <= rate <= MAX_SAMPLE_RATE_HZ):
        raise MotionFormatError(f"sample rate {rate} Hz outside {MIN_SAMPLE_RATE_HZ}-{MAX_SAMPLE_RATE_HZ} Hz")
    raw = arr.tobytes()
    hdr = dict(header)
    hdr["sample_count"] = int(arr.shape[0])
    hdr["data_sha256"] = hashlib.sha256(raw).hexdigest()
    hdr["compression"] = compression
    hdr["sample_encoding"] = SAMPLE_ENCODING
    hdr["channels"] = list(CHANNELS)
    hdr.setdefault("format", FORMAT_NAME)
    hdr.setdefault("format_version", FORMAT_VERSION)
    body = zlib.compress(raw, 6) if compression == "zlib" else raw
    js = json.dumps(hdr, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    pad = (-(_PREFIX.size + len(js))) % 8
    js += b" " * pad
    return _PREFIX.pack(MAGIC, FORMAT_MAJOR, FORMAT_MINOR, len(js)) + js + body


def write_motion(
    path: str | Path | BinaryIO, header: dict[str, Any], samples: np.ndarray, compression: str = "none"
) -> str:
    """Write a .motion file; returns the SHA-256 of the complete file."""
    data = encode_motion(header, samples, compression)
    if isinstance(path, (str, Path)):
        target = Path(path)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    else:
        path.write(data)
    return hashlib.sha256(data).hexdigest()


def _parse_prefix(prefix: bytes) -> int:
    if len(prefix) < _PREFIX.size:
        raise MotionFormatError("file too short for a .motion header")
    magic, major, _minor, hlen = _PREFIX.unpack(prefix[: _PREFIX.size])
    if magic != MAGIC:
        raise MotionFormatError("bad magic: not a .motion file")
    if major != FORMAT_MAJOR:
        raise MotionFormatError(f"unsupported .motion major version {major} (supported: {FORMAT_MAJOR})")
    if hlen <= 0 or hlen > 16 * 1024 * 1024:
        raise MotionFormatError(f"implausible header length {hlen}")
    return int(hlen)


def _parse_header(js: bytes) -> dict[str, Any]:
    try:
        header = json.loads(js.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MotionFormatError(f"header is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise MotionFormatError("header must be a JSON object")
    missing = [k for k in REQUIRED_KEYS if k not in header]
    if missing:
        raise MotionFormatError(f"header missing keys: {', '.join(missing)}")
    if header["format"] != FORMAT_NAME:
        raise MotionFormatError(f"unexpected format name {header['format']!r}")
    if list(header["channels"]) != list(CHANNELS):
        raise MotionFormatError(f"channels must be {list(CHANNELS)}")
    if header["sample_encoding"] != SAMPLE_ENCODING:
        raise MotionFormatError(f"unsupported sample encoding {header['sample_encoding']!r}")
    if header["compression"] not in COMPRESSIONS:
        raise MotionFormatError(f"unsupported compression {header['compression']!r}")
    rate = float(header["sample_rate_hz"])
    if not (MIN_SAMPLE_RATE_HZ <= rate <= MAX_SAMPLE_RATE_HZ):
        raise MotionFormatError(f"sample rate {rate} Hz outside {MIN_SAMPLE_RATE_HZ}-{MAX_SAMPLE_RATE_HZ} Hz")
    if float(header["duration_s"]) <= 0:
        raise MotionFormatError("duration must be positive")
    if int(header["sample_count"]) < 2:
        raise MotionFormatError("sample_count must be >= 2")
    return header


def read_header(path: str | Path) -> dict[str, Any]:
    """Read only the JSON header (fast)."""
    with open(path, "rb") as fh:
        hlen = _parse_prefix(fh.read(_PREFIX.size))
        js = fh.read(hlen)
        if len(js) != hlen:
            raise MotionFormatError("truncated header")
    return _parse_header(js)


def decode_motion(data: bytes, verify: bool = True) -> MotionFile:
    hlen = _parse_prefix(data[: _PREFIX.size])
    end = _PREFIX.size + hlen
    if len(data) < end:
        raise MotionFormatError("truncated header")
    header = _parse_header(data[_PREFIX.size : end])
    body = data[end:]
    if header["compression"] == "zlib":
        try:
            body = zlib.decompress(body)
        except zlib.error as exc:
            raise MotionFormatError(f"corrupt zlib sample block: {exc}") from exc
    n = int(header["sample_count"])
    if len(body) != n * 24:
        raise MotionFormatError(f"sample block is {len(body)} bytes, expected {n * 24} for {n} samples")
    if verify and hashlib.sha256(body).hexdigest() != header["data_sha256"]:
        raise MotionFormatError("sample data checksum mismatch (file corrupted)")
    samples = np.frombuffer(body, dtype="<f4").reshape(n, 6).astype(np.float64)
    if verify and not np.all(np.isfinite(samples)):
        raise MotionFormatError("samples contain NaN or infinite values")
    return MotionFile(header=header, samples=samples)


def read_motion(path: str | Path | BinaryIO, verify: bool = True) -> MotionFile:
    if isinstance(path, (str, Path)):
        data = Path(path).read_bytes()
    else:
        data = path.read() if not isinstance(path, io.BytesIO) else path.getvalue()
    return decode_motion(data, verify=verify)


def sample_problems(
    samples: np.ndarray,
    sample_rate_hz: float,
    max_translation_m: float = 0.5,
    max_rotation_rad: float = 0.6,
) -> list[str]:
    """Generic sanity checks independent of a particular platform profile."""
    problems: list[str] = []
    s = np.asarray(samples, dtype=np.float64)
    if s.ndim != 2 or s.shape[1] != 6:
        return [f"bad sample shape {s.shape}"]
    if not np.all(np.isfinite(s)):
        problems.append("non-finite values present")
        return problems
    peak_t = float(np.max(np.abs(s[:, :3]))) if len(s) else 0.0
    peak_r = float(np.max(np.abs(s[:, 3:]))) if len(s) else 0.0
    if peak_t > max_translation_m:
        problems.append(f"translation peak {peak_t:.3f} m exceeds sanity bound {max_translation_m} m")
    if peak_r > max_rotation_rad:
        problems.append(f"rotation peak {peak_r:.3f} rad exceeds sanity bound {max_rotation_rad} rad")
    return problems
