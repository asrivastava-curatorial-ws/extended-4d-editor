"""Simulated viewers: does this network carry N of them?

Each simulated viewer opens the video's address and takes it the way a player does - a few seconds
ahead at once, then at the video's own pace - or as fast as it comes (`flat_out`). Run it on a
computer where the viewers will be (on their Wi-Fi), not on the server itself: then it measures the
network between the two. What it cannot simulate is the decoding in a real device.
"""

from __future__ import annotations

import http.client
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

AHEAD_S = 5.0                 # what a player fetches before it starts
BLOCK = 256 * 1024


@dataclass
class ViewerResult:
    received: int = 0
    seconds: float = 0.0
    first_byte_s: float | None = None
    behind_s: float = 0.0     # how long it had less than it would have shown by then (a real player would have stalled)
    error: str = ""

    @property
    def bits_per_second(self) -> float:
        return self.received * 8.0 / self.seconds if self.seconds > 0 else 0.0


def _connection(url: str, timeout: float, insecure: bool) -> tuple[http.client.HTTPConnection, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{url!r} is not an http(s) address")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if parts.scheme == "https":
        context = ssl.create_default_context()
        if insecure:                              # a venue's own certificate, made with 'fourdvr stream cert'
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=context), path
    return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout), path


def head(url: str, timeout: float = 8.0, insecure: bool = False) -> int:
    """The size of what is at `url` (bytes)."""
    conn, path = _connection(url, timeout, insecure)
    try:
        conn.request("HEAD", path)
        response = conn.getresponse()
        if response.status != 200:
            raise OSError(f"the server answers {response.status} for {url}")
        return int(response.getheader("Content-Length") or 0)
    finally:
        conn.close()


def _viewer(url: str, seconds: float, bitrate: float, flat_out: bool, start: threading.Event, result: ViewerResult,
            insecure: bool) -> None:
    try:
        conn, path = _connection(url, 15.0, insecure)
    except (OSError, ValueError) as exc:
        result.error = str(exc)
        return
    start.wait()
    t0 = time.monotonic()
    try:
        conn.request("GET", path, headers={"Range": "bytes=0-", "User-Agent": "fourdvr-stream-probe"})
        response = conn.getresponse()
        if response.status not in (200, 206):
            result.error = f"the server answers {response.status}"
            return
        pace = bitrate / 8.0                      # bytes a second
        last = t0
        while True:
            now = time.monotonic()
            elapsed = now - t0
            if elapsed >= seconds:
                break
            if not flat_out:
                allowed = pace * (elapsed + AHEAD_S)
                if result.received >= allowed:    # a player with a full buffer does not read
                    time.sleep(min(0.05, (result.received - allowed) / pace + 0.005))
                    last = time.monotonic()
                    continue
            data = response.read(BLOCK)
            if not data:
                break                             # the end of the file
            if result.first_byte_s is None:
                result.first_byte_s = time.monotonic() - t0
            result.received += len(data)
            now = time.monotonic()
            if not flat_out and result.received < pace * (now - t0 - 1.0):      # shown faster than it arrived
                result.behind_s += now - last
            last = now
    except (OSError, http.client.HTTPException) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.seconds = time.monotonic() - t0
        conn.close()


def run(url: str, viewers: int = 50, seconds: float = 20.0, bitrate: float = 0.0, flat_out: bool = False,
        insecure: bool = False) -> dict[str, Any]:
    """`viewers` connections to `url` for `seconds`. `bitrate` (bits a second) is the video's; 0 means flat out."""
    flat_out = flat_out or bitrate <= 0.0
    results = [ViewerResult() for _ in range(viewers)]
    start = threading.Event()
    threads = [threading.Thread(target=_viewer, args=(url, seconds, bitrate, flat_out, start, r, insecure), daemon=True)
               for r in results]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    start.set()
    for thread in threads:
        thread.join(seconds + 30.0)
    good = [r for r in results if not r.error]
    rates = sorted(r.bits_per_second for r in good)
    total = sum(r.received for r in good) * 8.0 / max(max((r.seconds for r in good), default=0.0), 1e-6)
    stalled = [r for r in good if r.behind_s > 0.5]
    return {
        "viewers": viewers, "answered": len(good), "errors": sorted({r.error for r in results if r.error}),
        "seconds": seconds, "flat_out": flat_out, "bitrate": bitrate, "total_bits_per_second": total,
        "slowest_bits_per_second": rates[0] if rates else 0.0, "median_bits_per_second": rates[len(rates) // 2] if rates else 0.0,
        "would_have_stalled": len(stalled), "longest_behind_s": max((r.behind_s for r in good), default=0.0),
        "slowest_first_byte_s": max((r.first_byte_s or 0.0 for r in good), default=0.0),
    }
