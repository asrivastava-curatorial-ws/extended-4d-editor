"""Connection from one platform to the Control Server (4D-VR-THEATRE/1 over WebSocket)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlparse

import websockets

from . import __version__
from .content_cache import ContentError, MotionCache
from .protocol import (
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    DeviceType,
    Envelope,
    MsgType,
    ProtocolError,
    check_version_compatible,
    decode,
    encode,
    make_message,
    parse_payload,
)
from .runtime import PlatformRuntime

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    pass


def device_mac(key: str, device_id: str, nonce: str) -> str:
    return hmac.new(key.encode(), f"{PROTOCOL_ID}|{device_id}|{nonce}".encode(), hashlib.sha256).hexdigest()


class ServerLink:
    def __init__(
        self,
        url: str,
        runtime: PlatformRuntime,
        device_key: str,
        cache: MotionCache,
        status_interval_s: float = 0.5,
        time_sync_interval_s: float = 2.0,
    ) -> None:
        self.url = url
        self.runtime = runtime
        self.device_id = runtime.cfg.platform_id
        self.key = device_key
        self.cache = cache
        self.status_interval = status_interval_s
        self.time_sync_interval = time_sync_interval_s
        parsed = urlparse(url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        self.http_base = f"{scheme}://{parsed.netloc}"
        self.content_token = ""
        self.connected = False
        self._stop = asyncio.Event()
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._prefetch: set[str] = set()
        self._background: set[asyncio.Task[None]] = set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def send(self, msg_type: MsgType, payload: dict[str, Any], request_id: str | None = None) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            await ws.send(encode(make_message(msg_type, payload, ts=time.monotonic(), request_id=request_id)))

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except AuthError as exc:
                log.error("authentication rejected", extra={"device_id": self.device_id, "error": str(exc)})
                self.runtime.add_error(f"server rejected authentication: {exc}")
                backoff = 15.0
            except (OSError, websockets.WebSocketException, ProtocolError, TimeoutError) as exc:
                log.warning("server connection lost", extra={"device_id": self.device_id, "error": str(exc)})
            finally:
                self.connected = False
                self._ws = None
                self.runtime.on_server_disconnected()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 10.0)

    async def _session(self) -> None:
        async with websockets.connect(
            self.url, open_timeout=5, ping_interval=5, ping_timeout=5, max_size=8 * 1024 * 1024
        ) as ws:
            self._ws = ws
            caps = {
                "platform_id": self.device_id,
                "driver": self.runtime.driver.kind,
                "control_rate_hz": self.runtime.rate,
                "dof": 6,
                "motion_format": "1.0",
                "profile_sha256": self.runtime.cfg.sha256,
            }
            await self.send(
                MsgType.REGISTER,
                {
                    "device_id": self.device_id,
                    "device_type": str(DeviceType.MOTION_CONTROLLER),
                    "app_version": __version__,
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": caps,
                    "hardware": {"name": self.runtime.cfg.name},
                },
            )
            env = decode(await asyncio.wait_for(ws.recv(), timeout=10))
            if env.type == MsgType.ERROR:
                raise AuthError(env.payload.get("message", "error"))
            challenge = parse_payload(env, "s2c")
            if env.type != MsgType.AUTHENTICATE or challenge.stage != "challenge":
                raise ProtocolError("BAD_MESSAGE", "expected AUTHENTICATE challenge")
            if not check_version_compatible(challenge.protocol_version):
                raise AuthError(f"server protocol {challenge.protocol_version} is incompatible with {PROTOCOL_VERSION}")
            await self.send(
                MsgType.AUTHENTICATE,
                {
                    "stage": "response",
                    "device_id": self.device_id,
                    "nonce": challenge.nonce,
                    "mac": device_mac(self.key, self.device_id, challenge.nonce),
                },
            )
            env = decode(await asyncio.wait_for(ws.recv(), timeout=10))
            if env.type == MsgType.ERROR:
                raise AuthError(env.payload.get("message", "rejected"))
            result = parse_payload(env, "s2c")
            if getattr(result, "stage", "") != "result":
                raise ProtocolError("BAD_MESSAGE", "expected AUTHENTICATE result")
            self.content_token = result.content_token
            self.connected = True
            log.info("connected to control server", extra={"device_id": self.device_id, "url": self.url})
            tasks = [
                asyncio.create_task(self._receiver(ws)),
                asyncio.create_task(self._heartbeat(result.heartbeat_interval_s)),
                asyncio.create_task(self._time_sync()),
                asyncio.create_task(self._status_loop()),
            ]
            self.runtime.on_server_connected()
            try:
                done, _pending = await asyncio.wait(
                    [*tasks, asyncio.create_task(self._stop.wait())], return_when=asyncio.FIRST_COMPLETED
                )
                for d in done:
                    if not d.cancelled() and d.exception() is not None:
                        raise d.exception()  # type: ignore[misc]
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self, interval: float) -> None:
        start = time.monotonic()
        while True:
            await self.send(
                MsgType.HEARTBEAT,
                {
                    "state": self.runtime.status()["state"],
                    "programme_id": self.runtime.timeline.programme_id,
                    "uptime_s": round(time.monotonic() - start, 1),
                },
            )
            await asyncio.sleep(interval)

    async def _time_sync(self) -> None:
        for _ in range(8):  # initial burst
            await self.send(MsgType.TIME_SYNC, {"t0": time.monotonic()})
            await asyncio.sleep(0.05)
        while True:
            await asyncio.sleep(self.time_sync_interval)
            await self.send(MsgType.TIME_SYNC, {"t0": time.monotonic()})

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.status_interval)
            await self.send(MsgType.STATUS, self.runtime.status())

    async def _receiver(self, ws: Any) -> None:
        async for raw in ws:
            t3 = time.monotonic()
            try:
                env = decode(raw)
                await self._handle(env, t3)
            except ProtocolError as exc:
                self.runtime.add_error(f"protocol error: {exc.message}")
                await self.send(MsgType.ERROR, {"code": exc.code, "message": exc.message})

    async def _handle(self, env: Envelope, t3: float) -> None:
        rt = self.runtime
        if env.request_id:
            rt.last_request_id = env.request_id
        if env.type == MsgType.TIME_SYNC:
            p = parse_payload(env, "s2c")
            rt.clock.add(p.t0, p.t1, p.t2, t3)
            return
        if env.type in (MsgType.HEARTBEAT, MsgType.BLACK_SCREEN):
            return
        if env.type == MsgType.MANIFEST:
            manifest = parse_payload(env, "s2c")
            wanted = {p.motion.sha256 for p in manifest.programmes if p.motion}
            for prog in manifest.programmes:
                if prog.motion and prog.motion.sha256 not in self._prefetch:
                    self._prefetch.add(prog.motion.sha256)
                    self._spawn(self._fetch(prog.motion.url, prog.motion.sha256))
            await asyncio.to_thread(self.cache.cleanup, wanted)
            return
        if env.type == MsgType.PREPARE:
            self._spawn(self._prepare(env))
            return
        if env.type == MsgType.ERROR:
            rt.add_error(f"server: {env.payload.get('code')}: {env.payload.get('message')}")
            return
        p = parse_payload(env, "s2c")
        if env.type == MsgType.PLAY:
            rt.cmd_play(p.programme_id, p.timeline_id, p.start_at, p.duration_s, p.loop)
        elif env.type == MsgType.RESTART:
            rt.cmd_play(p.programme_id, p.timeline_id, p.start_at, p.duration_s, p.loop)
        elif env.type == MsgType.PAUSE:
            rt.cmd_pause(p.programme_id, p.timeline_id, p.pause_at, p.position_s)
        elif env.type == MsgType.RESUME:
            rt.cmd_resume(p.programme_id, p.timeline_id, p.resume_at, p.position_s)
        elif env.type == MsgType.STOP:
            rt.cmd_stop(p.timeline_id, p.stop_at)
        elif env.type == MsgType.SYNC:
            if p.force:  # operator SYNC ALL: refresh the clock estimate too
                self._spawn(self._sync_burst())
            rt.cmd_sync(p.timeline, p.force)

    async def _sync_burst(self) -> None:
        for _ in range(5):
            await self.send(MsgType.TIME_SYNC, {"t0": time.monotonic()})
            await asyncio.sleep(0.05)

    async def _fetch(self, url: str, sha256: str) -> None:
        try:
            await asyncio.to_thread(self.cache.fetch, self.http_base, url, sha256, self.content_token)
        except ContentError as exc:
            self.runtime.add_error(str(exc))
        finally:
            self._prefetch.discard(sha256)

    async def _prepare(self, env: Envelope) -> None:
        p = parse_payload(env, "s2c")
        prog = p.programme
        ok, detail, sha = True, "", None
        try:
            if prog.motion is not None:
                sha = prog.motion.sha256
                await asyncio.to_thread(self.cache.fetch, self.http_base, prog.motion.url, sha, self.content_token)
            loaded = await asyncio.to_thread(
                self.runtime.register_programme, prog.programme_id, sha or "", prog.duration_s
            )
            if loaded.track is None:
                detail = "programme has no motion profile; platform stays neutral"
            elif abs(loaded.duration_s - prog.duration_s) > 0.5:
                detail = f"motion duration {loaded.duration_s:.2f}s differs from programme {prog.duration_s:.2f}s"
        except (ContentError, OSError, ValueError) as exc:
            ok, detail = False, str(exc)
            self.runtime.add_error(f"prepare {prog.programme_id} failed: {exc}")
        await self.send(
            MsgType.READY,
            {
                "programme_id": prog.programme_id,
                "timeline_id": p.timeline_id,
                "ok": ok,
                "detail": detail,
                "motion_sha256": sha,
            },
            request_id=env.request_id,
        )
