"""Where an effects document lives: inside a ``.motion`` file (header key ``effects``) or in a
standalone ``.fx.json`` file.

Embedding keeps a programme in ONE file that the unchanged Control Server validates, stores and
distributes: the ``.motion`` reader ignores unknown header keys, so a file with effects is still a
valid ``.motion`` 1.0 file for every existing reader, and a file without the key is a motion-only
programme.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fourdvr_motion import motionfile

from .model import EffectsDocument, EffectsError

HEADER_KEY = "effects"
MAX_HEADER_BYTES = 16 * 1024 * 1024  # limit of the .motion reader (motionfile._parse_prefix)


def effects_from_header(header: dict[str, Any]) -> EffectsDocument | None:
    """The embedded effects of a .motion header, or None for a motion-only programme."""
    raw = header.get(HEADER_KEY)
    if raw is None:
        return None
    return EffectsDocument.from_dict(raw)


def attach_effects(header: dict[str, Any], doc: EffectsDocument | None) -> dict[str, Any]:
    """Copy of `header` with the effects embedded (or removed when doc is None or empty)."""
    out = dict(header)
    out.pop(HEADER_KEY, None)
    if doc is not None and not doc.is_empty():
        out[HEADER_KEY] = doc.to_dict()
        size = len(json.dumps(out, separators=(",", ":")).encode("utf-8"))
        if size > MAX_HEADER_BYTES - 4096:
            raise EffectsError(
                f"effects section too large for a .motion header ({size / 1e6:.1f} MB of {MAX_HEADER_BYTES / 1e6:.0f} MB): "
                "lower the bass sample rate or shorten the bass track"
            )
    return out


def read_effects(path: str | Path) -> EffectsDocument | None:
    """Read effects from a .motion file (embedded) or a standalone JSON file."""
    p = Path(path)
    with p.open("rb") as fh:
        magic = fh.read(len(motionfile.MAGIC))
    if magic == motionfile.MAGIC:
        return effects_from_header(motionfile.read_header(p))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EffectsError(f"{p.name}: neither a .motion file nor effects JSON ({exc})") from exc
    return EffectsDocument.from_dict(data)


def write_effects(path: str | Path, doc: EffectsDocument) -> None:
    """Write a standalone effects file (.fx.json)."""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc.to_dict(), separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(target)


def embed_into_motion(motion_path: str | Path, doc: EffectsDocument | None, out_path: str | Path | None = None) -> str:
    """Rewrite a .motion file with `doc` embedded (None removes the effects). Returns the new file SHA-256.

    The motion samples are copied bit-exactly; only the header changes.
    """
    mf = motionfile.read_motion(motion_path, verify=True)
    header = attach_effects(mf.header, doc)
    return motionfile.write_motion(out_path or motion_path, header, mf.samples, compression=str(mf.header["compression"]))
