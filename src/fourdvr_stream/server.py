"""The stream server: videos over HTTP for any device, and one clock for all viewers.

    GET  /                     the viewer's page: the show, in step with everybody else
    GET  /library              the videos, to watch one by oneself (unless switched off)
    GET  /control?key=...      the operator's page
    GET  /media/<id>           a video file; byte ranges are honoured, so players can seek
    GET  /api/time             the server's clock (for the viewers' clock comparison)
    GET  /api/state            the timeline: which video, playing or not, and since when
    GET  /api/events           the same, pushed whenever it changes (server-sent events)
    GET  /api/library          the list of videos
    POST /api/report           a viewer says how it is doing (how far off, whether it stalls)
    GET  /api/status?key=...   viewers, throughput
    POST /api/control?key=...  play / pause / seek / stop / loop

A video is sent as it is: the server neither decodes nor re-encodes, it copies bytes from the file
to the sockets (with the operating system's sendfile where there is one), so its processor hardly
matters and one copy of the file in memory serves everybody. What limits the number of viewers is
the network: viewers x bitrate.

"One clock": the timeline says at which instant *of the server's clock* position 0 of the video is
(or was) on the screen. Every viewer compares its own clock with the server's, a few times, taking
the exchange with the shortest round trip, and then steers its player - by speed, a few per cent,
or by a jump if it is far off - so that it shows what the timeline says. Nothing is sent in real
time; a viewer that buffers falls behind and catches up by itself.

Only the standard library is used. This is a server for a venue's own network: it has no user
accounts; the operator's page is protected by a key in its address, and TLS is optional.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import queue
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .library import Library, MediaItem

log = logging.getLogger(__name__)

BLOCK = 2 * 1024 * 1024           # how much of a file goes to a socket per call
SEND_TIMEOUT_S = 75.0             # a viewer that takes nothing for this long is let go (a paused player reconnects)
START_LEAD_S = 3.0                # play: this far in the future, so that every viewer can get ready
SEEK_LEAD_S = 1.5
STOPPED, PLAYING, PAUSED = "stopped", "playing", "paused"


class Clock:
    """Wall-clock seconds that never jump: anchored once, then counted with the monotonic clock."""

    def __init__(self) -> None:
        self._wall = time.time()
        self._mono = time.monotonic()

    def now(self) -> float:
        return self._wall + (time.monotonic() - self._mono)


@dataclass
class Timeline:
    video: str | None = None          # id of the video, None: nothing chosen
    state: str = STOPPED
    epoch: float = 0.0                # playing: the server time at which position 0 is (or was) shown
    start_at: float = 0.0             # playing: the server time at which it starts (viewers stand ready until then)
    position_s: float = 0.0           # paused: where
    duration_s: float = 0.0           # 0: not known (no loop, no end)
    loop: bool = False
    black: bool = False               # the theatre has blanked the screens
    source: str = "operator"          # who sets it: "operator" or "theatre" (the Control Server's show)
    note: str = ""
    seq: int = 0                      # counts the changes

    def position_at(self, now: float) -> float:
        if self.state == PLAYING:
            position = now - self.epoch
            if position < 0.0:
                return position                   # negative: it starts in that many seconds
            if self.duration_s > 0.0:
                return position % self.duration_s if self.loop else min(position, self.duration_s)
            return position
        return self.position_s if self.state == PAUSED else 0.0

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "video": self.video, "state": self.state, "epoch": round(self.epoch, 4), "start_at": round(self.start_at, 4),
            "position_s": round(self.position_at(now), 3),
            "duration_s": round(self.duration_s, 3), "loop": self.loop, "black": self.black, "source": self.source,
            "note": self.note, "seq": self.seq, "server_time": round(now, 4),
        }


@dataclass
class Viewer:
    id: str
    address: str
    agent: str
    kind: str = "show"                # show | library | operator
    since: float = 0.0
    last_report: float = 0.0
    report: dict[str, Any] = field(default_factory=dict)
    queue: queue.Queue[str | None] = field(default_factory=lambda: queue.Queue(maxsize=64))

    def to_dict(self, now: float) -> dict[str, Any]:
        return {"id": self.id, "address": self.address, "agent": self.agent, "kind": self.kind,
                "connected_s": round(now - self.since, 1), **self.report}


class ControlError(ValueError):
    pass


class StreamServer:
    """Create, `start()`, later `stop()`. `port=0` takes a free port (see `.port` afterwards)."""

    def __init__(self, library: Library, host: str = "0.0.0.0", port: int = 8080, key: str | None = None,
                 cert: str | None = None, cert_key: str | None = None, max_viewers: int = 100, on_demand: bool = True,
                 start_lead_s: float = START_LEAD_S, title: str = "") -> None:
        self.library = library
        self.clock = Clock()
        self.key = key if key else secrets.token_urlsafe(9)
        self.max_viewers = max_viewers
        self.on_demand = on_demand
        self.start_lead_s = start_lead_s
        self.title = title or "Extended 4D Editor - stream"
        self.timeline = Timeline()
        self.viewers: dict[str, Viewer] = {}
        self.bytes_sent = 0
        self.media_requests = 0
        self.refused = 0
        self._rate: list[tuple[float, int]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.tls = bool(cert)
        self.httpd = _Httpd((host, port), _Handler)
        self.httpd.stream = self
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, cert_key or None)
            # (the handshake is done by the connection's own thread, not while accepting)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.host, self.port = host, int(self.httpd.server_address[1])
        self.pages = {name: resources.files("fourdvr_stream").joinpath("web", name).read_bytes()
                      for name in ("player.html", "control.html")}
        self.font = _font()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for target, name in ((self.httpd.serve_forever, "stream-http"), (self._watch, "stream-timeline")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for viewer in self.viewers.values():
                with contextlib.suppress(queue.Full):
                    viewer.queue.put_nowait(None)
        self.httpd.shutdown()
        self.httpd.server_close()

    def _watch(self) -> None:
        """The end of a video that is not looped; and a sign of life for viewers behind proxies."""
        beat = 0
        while not self._stop.wait(0.25):
            with self._lock:
                tl = self.timeline
                if (tl.source == "operator" and tl.state == PLAYING and not tl.loop and tl.duration_s > 0.0
                        and self.clock.now() - tl.epoch >= tl.duration_s + 0.3):
                    self._set(state=STOPPED, position_s=0.0, note="the video has ended")
            beat += 1
            if beat % 40 == 0:
                self.broadcast()

    # ------------------------------------------------------------------ the timeline
    def state(self) -> dict[str, Any]:
        with self._lock:
            return self.timeline.to_dict(self.clock.now())

    def _set(self, **changes: Any) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(self.timeline, name, value)
            self.timeline.seq += 1
        self.broadcast()

    def broadcast(self) -> None:
        message = json.dumps(self.state(), separators=(",", ":"))
        with self._lock:
            viewers = list(self.viewers.values())
        for viewer in viewers:
            try:
                viewer.queue.put_nowait(message)
            except queue.Full:                    # it does not read: its connection is dropped, it comes back
                with contextlib.suppress(queue.Empty):
                    viewer.queue.get_nowait()
                with contextlib.suppress(queue.Full):
                    viewer.queue.put_nowait(None)

    def _item(self, video: str | None) -> MediaItem:
        with self._lock:
            name = video if video is not None else self.timeline.video
        if name is None:
            raise ControlError("no video is chosen")
        item = self.library.get(name)
        if item is None:
            raise ControlError(f"there is no video {name!r} (videos: {', '.join(self.library.items) or 'none'})")
        return item

    def control(self, action: str, video: str | None = None, position_s: float | None = None,
                loop: bool | None = None, lead_s: float | None = None) -> dict[str, Any]:
        """The operator's commands: play, pause, resume, seek, stop, loop, load."""
        with self._lock:
            tl = self.timeline
            if tl.source != "operator":
                raise ControlError("the timeline follows the theatre's show: it is controlled at the Control Server")
            now = self.clock.now()
            if position_s is not None and not (0.0 <= position_s < 360000.0):
                raise ControlError("the position must be 0 or more (seconds)")
            if lead_s is not None and not math.isfinite(lead_s):
                raise ControlError("the lead must be a number of seconds")
            if action in ("play", "load"):
                item = self._item(video)
                start = position_s if position_s is not None else (
                    tl.position_s if action == "play" and tl.state == PAUSED and item.id == tl.video else 0.0)
                if item.duration_s > 0.0 and start >= item.duration_s:
                    raise ControlError(f"the position is beyond the end of the video ({item.duration_s:.1f} s)")
                changes: dict[str, Any] = {"video": item.id, "duration_s": item.duration_s, "note": "", "black": False,
                                           "loop": tl.loop if loop is None else bool(loop)}
                if action == "play":
                    lead = self.start_lead_s if lead_s is None else min(600.0, max(0.0, float(lead_s)))
                    changes.update(state=PLAYING, epoch=now + lead - start, start_at=now + lead, position_s=start)
                else:
                    changes.update(state=PAUSED, position_s=start)
                self._set(**changes)
            elif action == "pause":
                if tl.state == PLAYING:
                    self._set(state=PAUSED, position_s=max(0.0, tl.position_at(now)))
            elif action == "resume":
                if tl.state == PAUSED:
                    self._set(state=PLAYING, epoch=now + SEEK_LEAD_S - tl.position_s, start_at=now + SEEK_LEAD_S)
            elif action == "seek":
                if tl.video is None or position_s is None:
                    raise ControlError("seek needs a video and a position")
                if tl.duration_s > 0.0 and position_s >= tl.duration_s:
                    raise ControlError(f"the position is beyond the end of the video ({tl.duration_s:.1f} s)")
                if tl.state == PLAYING:
                    self._set(epoch=now + SEEK_LEAD_S - position_s, start_at=now + SEEK_LEAD_S, position_s=position_s)
                else:
                    self._set(state=PAUSED, position_s=position_s)
            elif action == "stop":
                self._set(state=STOPPED, position_s=0.0, note="")
            elif action == "loop":
                if loop is None:
                    raise ControlError("loop needs on or off")
                if tl.state == PLAYING and tl.loop and not loop and tl.duration_s > 0.0:
                    self._set(loop=False, epoch=now - tl.position_at(now))      # go on from here, then end
                else:
                    self._set(loop=bool(loop))
            else:
                raise ControlError(f"unknown action {action!r} (play, pause, resume, seek, stop, loop, load)")
            return self.timeline.to_dict(self.clock.now())

    def follow(self, video: str | None, state: str, epoch: float = 0.0, position_s: float = 0.0, duration_s: float = 0.0,
               loop: bool = False, black: bool = False, note: str = "", start_at: float = 0.0) -> None:
        """Set the timeline from outside (the theatre's show). Only a change is passed on to the viewers."""
        with self._lock:
            tl = self.timeline
            same = (tl.source == "theatre" and tl.video == video and tl.state == state and tl.loop == loop
                    and tl.black == black and tl.note == note and abs(tl.duration_s - duration_s) < 1e-3
                    and (abs(tl.epoch - epoch) < 0.02 if state == PLAYING else abs(tl.position_s - position_s) < 1e-3))
            if not same:
                self._set(source="theatre", video=video, state=state, epoch=epoch, position_s=position_s,
                          duration_s=duration_s, loop=loop, black=black, note=note, start_at=start_at)

    # ------------------------------------------------------------------ viewers and numbers
    def count(self, sent: int) -> None:
        with self._lock:
            self.bytes_sent += sent

    def throughput(self) -> float:
        """Bits per second sent as video over the last few seconds."""
        now = time.monotonic()
        with self._lock:
            self._rate.append((now, self.bytes_sent))
            self._rate = [(t, b) for t, b in self._rate if now - t <= 6.0]
            (t0, b0), (t1, b1) = self._rate[0], self._rate[-1]
        return (b1 - b0) * 8.0 / (t1 - t0) if t1 - t0 > 0.5 else 0.0

    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        with self._lock:
            viewers = [v.to_dict(now) for v in self.viewers.values()]
            sent, requests, refused = self.bytes_sent, self.media_requests, self.refused
        show = [v for v in viewers if v["kind"] == "show"]
        drifts = [abs(float(v["drift_ms"])) for v in show if v.get("drift_ms") is not None and v.get("playing")]
        return {
            "timeline": self.state(), "viewers": viewers, "watching": len(show),
            "in_step": sum(1 for d in drifts if d <= 80.0), "worst_drift_ms": round(max(drifts), 1) if drifts else None,
            "stalled": sum(1 for v in show if v.get("stalled")), "bits_per_second": round(self.throughput()),
            "bytes_sent": sent, "media_requests": requests, "refused": refused, "max_viewers": self.max_viewers,
            "library": self.library.listing(), "on_demand": self.on_demand, "tls": self.tls, "title": self.title,
        }

    def urls(self) -> list[str]:
        """Addresses under which other devices reach this server."""
        scheme = "https" if self.tls else "http"
        if self.host not in ("0.0.0.0", "::", ""):
            return [f"{scheme}://{self.host}:{self.port}"]
        return [f"{scheme}://{address}:{self.port}" for address in local_addresses()]


def local_addresses() -> list[str]:
    """This computer's addresses in its networks (the one towards the default route first)."""
    found: list[str] = []
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))          # nothing is sent: this only asks which interface would be used
        found.append(s.getsockname()[0])
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address not in found and not address.startswith("127."):
                found.append(address)
    return found or ["127.0.0.1"]


def _font() -> bytes:
    """The Editor's own typeface for the pages, if it is installed with this package."""
    with contextlib.suppress(Exception):
        return resources.files("fourdvr_studio").joinpath("gui", "fonts", "JetBrainsMono-Regular.ttf").read_bytes()
    return b""


# =========================================================================== HTTP
class _Httpd(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
    stream: StreamServer

    def handle_error(self, request: Any, client_address: Any) -> None:      # a viewer that went away is not an error
        log.debug("connection from %s ended with an error", client_address, exc_info=True)


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """(first, last) byte of a Range header for a file of `size` bytes; None if it cannot be satisfied.
    Only the first range of a list is served."""
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        return None
    first, dash, last = spec.split(",")[0].strip().partition("-")
    if not dash:
        return None
    try:
        if not first:                             # the last n bytes
            n = int(last)
            return (max(0, size - n), size - 1) if n > 0 and size > 0 else None
        a = int(first)
        b = min(int(last), size - 1) if last else size - 1
    except ValueError:
        return None
    return (a, b) if 0 <= a <= b < size else None


def clean_report(body: dict[str, Any]) -> dict[str, Any]:
    """What a viewer says about itself, reduced to what it may say: numbers that are numbers, short texts.
    (Anybody on the network can send anything here; the operator's page must not stumble over it.)"""
    out: dict[str, Any] = {}
    for name in ("drift_ms", "position_s", "offset_ms", "round_trip_ms"):
        value = body.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and abs(value) < 1e9:
            out[name] = float(value)
        elif name in body:
            out[name] = None
    for name in ("playing", "stalled"):
        if name in body:
            out[name] = body[name] is True
    stalls = body.get("stalls")
    if isinstance(stalls, int) and not isinstance(stalls, bool):
        out["stalls"] = min(max(stalls, 0), 1_000_000)
    for name in ("video", "view"):
        value = body.get(name)
        if isinstance(value, str):
            out[name] = value[:80]
    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fourdvr-stream"
    sys_version = ""
    timeout = 30.0                                # for reading a request; sending has its own
    server: _Httpd

    # ------------------------------------------------------------------ plumbing
    def setup(self) -> None:
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(10.0)
            self.request.do_handshake()           # a failure ends this connection (handle_error), nothing else
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, body: bytes, kind: str, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(json.dumps(data, separators=(",", ":")).encode(), "application/json", status)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= 65536:
            raise ControlError("the request is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ControlError("the request is not JSON") from exc
        if not isinstance(data, dict):
            raise ControlError("the request must be a JSON object")
        return data

    def _allowed(self, query: dict[str, list[str]]) -> bool:
        given = (query.get("key") or [self.headers.get("X-Stream-Key", "")])[0]
        return secrets.compare_digest(str(given), self.server.stream.key)

    # ------------------------------------------------------------------ requests
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        stream = self.server.stream
        url = urlsplit(self.path)
        path, query = unquote(url.path), parse_qs(url.query)
        try:
            if path.startswith("/media/"):
                self._media(path[len("/media/"):])
            elif path in ("/", "/index.html", "/show"):
                self._send(stream.pages["player.html"], "text/html; charset=utf-8", cache="no-cache")
            elif path == "/library":
                if not stream.on_demand:
                    self._error(HTTPStatus.NOT_FOUND, "watching by oneself is switched off on this server")
                else:
                    self._send(stream.pages["player.html"], "text/html; charset=utf-8", cache="no-cache")
            elif path == "/control":
                if not self._allowed(query):
                    self._send(b"The operator's page needs the key that 'fourdvr stream serve' printed: /control?key=...\n",
                               "text/plain; charset=utf-8", HTTPStatus.FORBIDDEN)
                else:
                    self._send(stream.pages["control.html"], "text/html; charset=utf-8", cache="no-cache")
            elif path == "/api/time":
                self._json({"t": stream.clock.now()})
            elif path == "/api/state":
                self._json(stream.state())
            elif path == "/api/library":
                self._json({"title": stream.title, "on_demand": stream.on_demand, "videos": stream.library.listing()})
            elif path == "/api/events":
                self._events(query)
            elif path == "/api/status":
                if self._allowed(query):
                    self._json(stream.status())
                else:
                    self._error(HTTPStatus.FORBIDDEN, "the key is missing or wrong")
            elif path == "/assets/font.ttf" and stream.font:
                self._send(stream.font, "font/ttf", cache="public, max-age=86400")
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            else:
                self._error(HTTPStatus.NOT_FOUND, "there is nothing at this address")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
            self.close_connection = True

    def do_POST(self) -> None:
        stream = self.server.stream
        url = urlsplit(self.path)
        path, query = unquote(url.path), parse_qs(url.query)
        try:
            body = self._body()
            if path == "/api/control":
                if not self._allowed(query):
                    self._error(HTTPStatus.FORBIDDEN, "the key is missing or wrong")
                    return
                position = body.get("position_s")
                lead = body.get("lead_s")
                loop = body.get("loop")
                self._json(stream.control(str(body.get("action", "")), body.get("video"),
                                          None if position is None else float(position),
                                          None if loop is None else bool(loop), None if lead is None else float(lead)))
            elif path == "/api/report":
                viewer = stream.viewers.get(str(body.get("id", "")))
                if viewer is not None:
                    viewer.last_report = stream.clock.now()
                    viewer.report = clean_report(body)
                self._json({})
            else:
                self._error(HTTPStatus.NOT_FOUND, "there is nothing at this address")
        except (ControlError, TypeError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
            self.close_connection = True

    # ------------------------------------------------------------------ the timeline, pushed
    def _events(self, query: dict[str, list[str]]) -> None:
        stream = self.server.stream
        kind = (query.get("kind") or ["show"])[0]
        kind = kind if kind in ("show", "library", "operator") else "show"
        viewer = Viewer((query.get("id") or [secrets.token_hex(4)])[0][:32], self.client_address[0],
                        (self.headers.get("User-Agent") or "")[:160], kind, stream.clock.now())
        with stream._lock:
            watching = sum(1 for v in stream.viewers.values() if v.kind != "operator")
            if kind != "operator" and watching >= stream.max_viewers and viewer.id not in stream.viewers:
                stream.refused += 1
                full = True
            else:
                stream.viewers[viewer.id] = viewer
                full = False
        if full:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, f"this server is set to {stream.max_viewers} viewers, and they are here")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            self.wfile.write(f"retry: 1000\ndata: {json.dumps(stream.state(), separators=(',', ':'))}\n\n".encode())
            self.wfile.flush()
            while True:
                message = viewer.queue.get()
                if message is None:
                    break
                self.wfile.write(f"data: {message}\n\n".encode())
                self.wfile.flush()
        except (OSError, ssl.SSLError):
            pass
        finally:
            with stream._lock:
                if stream.viewers.get(viewer.id) is viewer:
                    del stream.viewers[viewer.id]

    # ------------------------------------------------------------------ a video
    def _media(self, name: str) -> None:
        stream = self.server.stream
        item = stream.library.get(name)
        if item is None:
            self._error(HTTPStatus.NOT_FOUND, "there is no such video")
            return
        try:
            fh = item.path.open("rb")
        except OSError as exc:
            self._error(HTTPStatus.NOT_FOUND, f"the file cannot be read ({exc.strerror})")
            return
        with fh:
            info = item.path.stat()
            size = info.st_size
            first, last, status = 0, size - 1, HTTPStatus.OK
            wanted = self.headers.get("Range")
            if wanted:
                span = parse_range(wanted, size)
                if span is None:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                first, last = span
                status = HTTPStatus.PARTIAL_CONTENT
            length = last - first + 1
            self.send_response(status)
            self.send_header("Content-Type", item.mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.send_header("ETag", f'"{size:x}-{int(info.st_mtime):x}"')
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")      # (a WebGL texture needs this if the page is elsewhere)
            self.end_headers()
            if self.command == "HEAD" or length <= 0:
                return
            with stream._lock:
                stream.media_requests += 1
            self.wfile.flush()
            sock = self.connection
            sock.settimeout(SEND_TIMEOUT_S)
            plain = not isinstance(sock, ssl.SSLSocket)
            sent = 0
            try:
                while sent < length:
                    block = min(BLOCK, length - sent)
                    if plain:
                        done = sock.sendfile(fh, first + sent, block)        # the kernel copies: file -> socket
                    else:
                        fh.seek(first + sent)
                        data = fh.read(block)
                        sock.sendall(data)
                        done = len(data)
                    if done <= 0:
                        break
                    sent += done
                    stream.count(done)
            except (OSError, ssl.SSLError):       # the player has what it wants, seeks, or is gone
                pass
            if sent < length:
                self.close_connection = True


def page_path(name: str) -> Path:
    """Where a page of the web player is (for those who want to look at it)."""
    return Path(str(resources.files("fourdvr_stream").joinpath("web", name)))
