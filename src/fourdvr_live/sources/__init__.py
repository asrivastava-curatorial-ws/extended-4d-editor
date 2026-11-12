"""The live sources this program knows, by the name the configuration uses (``[source] type = ...``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import DEMO, REPLAY, SCREEN, SOUND, Decoder, LiveSource, SourceError, SourceInfo, UdpSource
from .flight import FLIGHTGEAR, XPLANE, FlightGearDecoder, XPlaneDecoder
from .openformat import OPEN, OpenDecoder
from .racing import (
    ASSETTO,
    BEAMNG,
    CODEMASTERS,
    F1,
    FORZA,
    GT7,
    OUTSIM,
    PCARS2,
    AssettoSource,
    BeamNGDecoder,
    CodemastersDecoder,
    F1Decoder,
    ForzaDecoder,
    GT7Source,
    OutSimDecoder,
    Pcars2Decoder,
)

DECODERS: dict[str, tuple[SourceInfo, Callable[[], Decoder]]] = {
    "open": (OPEN, OpenDecoder),
    "forza": (FORZA, ForzaDecoder),
    "f1": (F1, F1Decoder),
    "dirt": (CODEMASTERS, CodemastersDecoder),
    "pcars2": (PCARS2, Pcars2Decoder),
    "outsim": (OUTSIM, OutSimDecoder),
    "beamng": (BEAMNG, BeamNGDecoder),
    "xplane": (XPLANE, XPlaneDecoder),
    "flightgear": (FLIGHTGEAR, FlightGearDecoder),
}
ALIASES = {"4dlive": "open", "codemasters": "dirt", "dirtrally": "dirt", "ams2": "pcars2", "automobilista2": "pcars2",
           "lfs": "outsim", "ac": "assetto", "x-plane": "xplane", "fg": "flightgear", "granturismo7": "gt7",
           "capture": "screen", "video": "screen", "audio": "sound"}
INFOS: dict[str, SourceInfo] = {
    **{name: info for name, (info, _make) in DECODERS.items()},
    "assetto": ASSETTO, "gt7": GT7, "screen": SCREEN, "sound": SOUND, "replay": REPLAY, "demo": DEMO,
}
ORDER = ("open", "forza", "f1", "dirt", "pcars2", "assetto", "outsim", "beamng", "xplane", "flightgear", "gt7",
         "screen", "sound", "replay", "demo")


def canonical(name: str) -> str:
    key = name.strip().lower().replace("_", "").replace(" ", "")
    key = ALIASES.get(key, key)
    if key not in INFOS:
        raise SourceError(f"unknown source {name!r} - 'fourdvr live sources' lists them")
    return key


def info(name: str) -> SourceInfo:
    return INFOS[canonical(name)]


class _Udp(UdpSource):
    def __init__(self, source_info: SourceInfo, decoder: Decoder, port: int, bind: str, host: str) -> None:
        self.info = source_info
        super().__init__(decoder, port, bind, host)


def create(options: dict[str, Any]) -> LiveSource:
    """The source a ``[source]`` table describes. Unknown keys are an error, so that a typing mistake
    does not silently leave an option at its default."""
    opts = dict(options)
    name = canonical(str(opts.pop("type", "") or "open"))
    source_info = INFOS[name]

    def take(allowed: tuple[str, ...]) -> dict[str, Any]:
        unknown = sorted(set(opts) - set(allowed))
        if unknown:
            raise SourceError(f"[source] type {name!r}: unknown key(s) {', '.join(unknown)} "
                              f"(known: {', '.join(allowed) or 'none'})")
        return opts

    if name in DECODERS or name in ("assetto", "gt7"):
        o = take(("port", "bind", "host"))
        port = int(o.get("port", source_info.port))
        bind, host = str(o.get("bind", "0.0.0.0")), str(o.get("host", ""))
        if name == "assetto":
            return AssettoSource(port, bind, host)
        if name == "gt7":
            return GT7Source(port, bind, host)
        return _Udp(source_info, DECODERS[name][1](), port, bind, host)
    if name == "demo":
        from .replay import DemoSource

        o = take(("seconds", "loop"))
        return DemoSource(float(o.get("seconds", 0.0)), bool(o.get("loop", True)))
    if name == "replay":
        from .replay import ReplaySource

        o = take(("file", "loop"))
        if not o.get("file"):
            raise SourceError('[source] type "replay" needs file = "session.4dlive"')
        return ReplaySource(str(o["file"]), bool(o.get("loop", False)))
    if name == "screen":
        try:
            from .screen import ScreenSource
        except ImportError as exc:
            raise SourceError(f"screen capture needs the workstation packages (OpenCV): {exc}") from exc
        return ScreenSource(take(ScreenSource.OPTIONS))
    from .sound import SoundSource

    return SoundSource(take(SoundSource.OPTIONS))


__all__ = ["DECODERS", "INFOS", "ORDER", "LiveSource", "SourceError", "SourceInfo", "canonical", "create", "info"]
