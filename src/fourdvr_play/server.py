"""The hub on the network: one port for the pages, the WebSocket and the seats' telemetry.

    http://HOST:47020/             the page for a seat's phone or tablet
    http://HOST:47020/operator     the operator's page (needs the operator key)
    http://HOST:47020/scoreboard   scores and the open question, for a shared screen
    ws://HOST:47020/ws             the protocol "4DPLAY" (docs/PLAY_PROTOCOL.md)
    UDP 47020                      a seat's telemetry in the open live format, with "seat": "A1"

All connections are served by one thread (asyncio). A client that does not read what it is sent is
disconnected once its queue is full - it can never hold up the others or the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import threading
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .config import PlayConfig
from .hub import Client, Hub, PlayError, To

log = logging.getLogger(__name__)
MAX_MESSAGE = 16 * 1024
QUEUE = 512                  # messages waiting for one client; beyond: disconnected
HELLO_TIMEOUT_S = 10.0
TICK_S = 0.02
STATUS_S = 0.5
PAGES = {"/": "controller.html", "/play": "controller.html", "/operator": "operator.html", "/scoreboard": "scoreboard.html",
         "/assets/play.css": "play.css", "/assets/play.js": "play.js"}
TYPES = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8",
         "json": "application/json", "ttf": "font/ttf"}
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self' ws: wss:; img-src 'self' data:; frame-ancestors 'none'")


def _asset(name: str) -> bytes:
    return resources.files("fourdvr_play").joinpath("web", name).read_bytes()


def _font() -> bytes:
    try:
        return resources.files("fourdvr_studio").joinpath("gui", "fonts", "JetBrainsMono-Regular.ttf").read_bytes()
    except (OSError, ModuleNotFoundError):
        return b""


class _Telemetry(asyncio.DatagramProtocol):
    def __init__(self, server: PlayServer) -> None:
        self.server = server

    def datagram_received(self, data: bytes, addr: Any) -> None:
        server = self.server
        try:
            if len(data) > 4096:
                raise ValueError("too long")
            msg = json.loads(data.decode("utf-8"))
            seat = msg.get("seat") if isinstance(msg, dict) else None
            if not isinstance(seat, str) or int(msg.get("4dlive", 0)) != 1:
                raise ValueError("not a seat's telemetry")
        except (ValueError, TypeError, UnicodeDecodeError):
            server.telemetry_bad += 1
            return
        if server.hub.motion(seat, msg):
            server.telemetry += 1
        else:
            server.telemetry_bad += 1


class PlayServer:
    def __init__(self, cfg: PlayConfig, hub: Hub, engine: Any = None, port: int | None = None, bind: str | None = None,
                 udp: bool = True) -> None:
        self.cfg = cfg
        self.hub = hub
        self.engine = engine
        self.bind = cfg.bind if bind is None else bind
        self.port = cfg.port if port is None else port
        self.udp = udp
        self.telemetry = 0
        self.telemetry_bad = 0
        self.connections_total = 0
        self.kicked = 0
        self.error = ""
        self._queues: dict[int, tuple[asyncio.Queue[str], ServerConnection, Client]] = {}
        self._assets: dict[str, tuple[str, bytes]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping: asyncio.Event | None = None

    # ================================================================== lifecycle
    def start(self) -> None:
        """Open the port and serve. Raises OSError if the port cannot be opened."""
        for path, name in PAGES.items():
            self._assets[path] = (TYPES[name.rsplit(".", 1)[1]], _asset(name))
        font = _font()
        if font:
            self._assets["/assets/font.ttf"] = (TYPES["ttf"], font)
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="play-hub", daemon=True)
        self._thread.start()
        self._ready.wait(10.0)
        if self.error:
            raise OSError(self.error)

    def stop(self) -> None:
        loop, stopping = self._loop, self._stopping
        if loop is not None and stopping is not None and loop.is_running():
            loop.call_soon_threadsafe(stopping.set)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:          # reported to whoever started the server
            self.error = self.error or f"{type(exc).__name__}: {exc}"
            log.exception("the hub's server stopped")
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        try:
            server: Server = await serve(self._client, self.bind, self.port, process_request=self._page,
                                         max_size=MAX_MESSAGE, max_queue=32, ping_interval=10.0, ping_timeout=10.0,
                                         close_timeout=2.0, server_header=None)
        except OSError as exc:
            self.error = f"port {self.port} cannot be opened ({exc.strerror or exc}) - is another show running, or use --port"
            return
        self.port = server.sockets[0].getsockname()[1]
        transport = None
        if self.udp:
            try:
                transport, _protocol = await self._loop.create_datagram_endpoint(
                    lambda: _Telemetry(self), local_addr=(self.bind, self.port))
            except OSError as exc:
                self.hub.note(f"seat telemetry over UDP is off: port {self.port} cannot be opened ({exc.strerror or exc})")
        pump = asyncio.create_task(self._pump())
        self._ready.set()
        try:
            await self._stopping.wait()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            if transport is not None:
                transport.close()
            server.close(close_connections=True)
            await server.wait_closed()

    # ================================================================== pages
    def _page(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        url = urlsplit(request.path)
        if url.path == "/api/info":
            body = json.dumps({"title": self.cfg.title, "play": 1, "seats": [s.id for s in self.cfg.seats],
                               "phase": self.hub.phase}).encode()
            return self._response(200, TYPES["json"], body)
        if url.path == "/api/status":
            key = parse_qs(url.query).get("key", [""])[0]
            if not self.cfg.operator_key or not hmac.compare_digest(key.encode(), self.cfg.operator_key.encode()):
                return self._response(403, TYPES["json"], b'{"error": "the operator key is needed: /api/status?key=..."}')
            return self._response(200, TYPES["json"], json.dumps(self.status()).encode())
        asset = self._assets.get(url.path)
        if asset is None:
            return self._response(404, "text/plain; charset=utf-8", b"not found\n")
        return self._response(200, asset[0], asset[1])

    @staticmethod
    def _response(status: int, content_type: str, body: bytes) -> Response:
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
        return Response(status, reason, Headers([
            ("Content-Type", content_type), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", CSP),
            ("Connection", "close")]), body)

    # ================================================================== clients
    async def _client(self, ws: ServerConnection) -> None:
        peer = str(ws.remote_address[0]) if ws.remote_address else ""
        self.connections_total += 1
        try:
            raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S)
            hello = json.loads(raw) if isinstance(raw, str) else None
            client, welcome = self.hub.join(hello, peer)
        except PlayError as exc:
            with contextlib.suppress(ConnectionClosed):
                await ws.send(json.dumps({"type": "error", "code": exc.code, "message": exc.message}))
                await ws.close(4001, exc.code)
            return
        except (TimeoutError, ValueError, ConnectionClosed):
            with contextlib.suppress(ConnectionClosed):
                await ws.close(4000, "hello expected")
            return
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE)
        queue.put_nowait(json.dumps(welcome, separators=(",", ":")))
        self._queues[client.id] = (queue, ws, client)
        writer = asyncio.create_task(self._writer(ws, queue))
        bad = 0
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    msg = None
                if msg is None:
                    bad += 1
                    if bad > 20:
                        await ws.close(4002, "not JSON")
                        break
                self.hub.handle(client, msg)
                self._deliver()
        except ConnectionClosed:
            pass
        finally:
            self._queues.pop(client.id, None)
            self.hub.leave(client)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer

    async def _writer(self, ws: ServerConnection, queue: asyncio.Queue[str]) -> None:
        with contextlib.suppress(ConnectionClosed):
            while True:
                await ws.send(await queue.get())

    def _deliver(self) -> None:
        for to, message in self.hub.drain():
            self._post(to, message)

    def _post(self, to: To, message: dict[str, Any]) -> None:
        text = ""
        for queue, ws, client in list(self._queues.values()):
            if not to.matches(client):
                continue
            text = text or json.dumps(message, separators=(",", ":"))
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:                      # it does not read: away with it, the others go on
                self.kicked += 1
                self._queues.pop(client.id, None)
                task = asyncio.ensure_future(ws.close(4003, "too slow"))
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _pump(self) -> None:
        next_status = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                self.hub.step()
                self._deliver()
                now = loop.time()
                if now >= next_status:
                    next_status = now + STATUS_S
                    if any(c.role == "operator" for _q, _ws, c in self._queues.values()):
                        self._post(To(roles=frozenset(("operator",))), {"type": "status", **self.status()})
            except Exception:             # one bad round must not end the session
                log.exception("hub round failed")
            await asyncio.sleep(TICK_S)

    # ================================================================== status
    def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {"session": self.hub.snapshot(),
                              "network": {"port": self.port, "connections": len(self._queues), "kicked": self.kicked,
                                          "telemetry": self.telemetry, "telemetry_bad": self.telemetry_bad}}
        engine = self.engine
        if engine is not None:
            full = engine.status()
            st["engine"] = {
                "state": full["state"], "hold": full["hold"], "gain": full["gain"], "problem": full["problem"],
                "source": full["source"], "platforms": full["platforms"], "play": full["play"],
                "late_ticks": full["late_ticks"], "max_tick_ms": full["max_tick_ms"],
                "effects": {k: full["effects"].get(k) for k in ("running", "output", "last_error", "error", "limited")
                            if k in full.get("effects", {})} if "effects" in full else None,
            }
        return st
