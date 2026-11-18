"""Interactive shows ("5D / 6D / 7D"): every seat its own motion and effects, events, scores, prompts.

    fourdvr play run -c play.toml        the session: hub, seats, operator page
    fourdvr play ...                     see 'fourdvr play --help' and docs/PLAY.md
"""

from __future__ import annotations

PROTOCOL = 1                 # "4DPLAY": JSON messages over one WebSocket (docs/PLAY_PROTOCOL.md)
DEFAULT_PORT = 47020         # TCP: hub, pages and WebSocket; UDP: per-seat telemetry in the open live format

__all__ = ["DEFAULT_PORT", "PROTOCOL"]
