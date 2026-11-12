"""Writing a venue patch as TOML (reading it is patch.load_patch).

The Editor's venue window and ``fourdvr fx fixtures`` build patch entries; this module writes them in
the form a person would write: one ``[[fixtures]]`` block per fixture, its channels one per line.
Comments of a hand-written file are not kept when it is written again - everything else is, also
keys this version does not know.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .patch import PatchError, patch_from_dict

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_FIXTURE_ORDER = ("id", "effect", "label", "model", "mode", "scope", "platforms", "universe", "address", "address_step",
                  "universe_step", "addresses", "lead_s", "flash", "flash_on_s", "match", "profile", "channels", "limits")
_OUTPUT_ORDER = ("protocol", "refresh_hz")


def _string(text: str) -> str:
    return '"' + "".join(_ESCAPES.get(c, c if c >= " " and c != "\x7f" else f"\\u{ord(c):04X}") for c in text) + '"'


def _key(key: Any) -> str:
    text = str(key)
    return text if _BARE.match(text) else _string(text)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PatchError(f"{value} cannot be written to a patch")
        text = repr(value)
        return text if ("." in text or "e" in text) else text + ".0"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }" if value else "{}"
    raise PatchError(f"{type(value).__name__} cannot be written to a patch")


def _scalars(table: dict[str, Any], first: tuple[str, ...] = ()) -> list[str]:
    keys = [k for k in first if k in table] + [k for k in table if k not in first]
    return [f"{_key(k)} = {_value(table[k])}" for k in keys if not isinstance(table[k], dict)]


def fixture_toml(fixture: dict[str, Any]) -> str:
    """One [[fixtures]] block."""
    lines = ["[[fixtures]]"]
    for key in [k for k in _FIXTURE_ORDER if k in fixture] + [k for k in fixture if k not in _FIXTURE_ORDER]:
        value = fixture[key]
        if key == "channels" and isinstance(value, list) and value and all(isinstance(c, dict) for c in value):
            lines.append("channels = [")
            lines.extend(f"  {_value(c)}," for c in value)
            lines.append("]")
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    return "\n".join(lines) + "\n"


def to_toml(data: dict[str, Any], header: str = "") -> str:
    """The whole patch."""
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    top = [f"{_key(k)} = {_value(v)}" for k, v in data.items() if not isinstance(v, (dict, list))]
    if top:
        lines += ([""] if lines else []) + top
    output = data.get("output")
    if isinstance(output, dict) and output:
        lines += ["", "[output]", *_scalars(output, _OUTPUT_ORDER)]
        for name, section in output.items():
            if not isinstance(section, dict) or not section:
                continue
            lines += ["", f"[output.{_key(name)}]", *_scalars(section)]
            for sub, table in section.items():
                if isinstance(table, dict) and table:
                    lines += ["", f"[output.{_key(name)}.{_key(sub)}]", *[f"{_key(k)} = {_value(v)}" for k, v in table.items()]]
    audio = data.get("audio")
    if isinstance(audio, dict) and audio:
        lines += ["", "[audio]", *[f"{_key(k)} = {_value(v)}" for k, v in audio.items()]]
    types = data.get("types")
    if isinstance(types, dict):
        for name, table in types.items():
            if isinstance(table, dict):
                lines += ["", f"[types.{_key(name)}]", *[f"{_key(k)} = {_value(v)}" for k, v in table.items()]]
    for key, value in data.items():                       # anything this version does not know: kept as it was
        if key in ("output", "audio", "types", "fixtures") or not isinstance(value, (dict, list)):
            continue
        lines += ["", f"{_key(key)} = {_value(value)}"] if isinstance(value, list) else ["", f"[{_key(key)}]", *_scalars(value)]
    for fixture in data.get("fixtures") or []:
        if isinstance(fixture, dict):
            lines += ["", fixture_toml(fixture).rstrip("\n")]
    return "\n".join(lines).lstrip("\n") + "\n"


def read_data(path: str | Path) -> dict[str, Any]:
    """A patch file as the tables it holds (for editing; load_patch gives the patch itself)."""
    p = Path(path).expanduser()
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PatchError(f"{p}: {exc}") from exc


def write_data(path: str | Path, data: dict[str, Any], header: str = "") -> None:
    """Write a patch - only one that can be read back as it is, and in one step: a crash leaves the old file."""
    text = to_toml(data, header)
    patch_from_dict(tomllib.loads(text), str(path))        # raises PatchError: nothing is written
    p = Path(path).expanduser()
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
