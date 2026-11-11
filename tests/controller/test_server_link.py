"""Controller <-> Control Server protocol, against a scripted in-test server (no server package needed)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from controller_helpers import SIM, write_motion
from fourdvr_motion.app import ControllerApp
from fourdvr_motion.config import ControllerConfig, PlatformEntry
from fourdvr_motion.platform_config import load_platform_config
from fourdvr_motion.protocol import PROTOCOL_ID, make_message

KEY = "k" * 40
OFFSET = 1000.0  # the scripted server's master clock runs 1000 s ahead of time.monotonic()


def _content_server(directory: Path, token: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            sha = self.path.rsplit("/", 1)[-1]
            data = (directory / f"{sha}.motion").read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.asyncio
async def test_register_auth_timesync_prepare_play(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    sha = write_motion(content / "tmp.motion", programme_id=4, duration=6.0)
    (content / "tmp.motion").rename(content / f"{sha}.motion")
    token = "content-token-123"
    http = _content_server(content, token)
    log: dict[str, list] = {"ready": [], "status": [], "types": []}
    start_at_box: list[float] = []

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        reg = json.loads(await ws.recv())
        assert reg["type"] == "REGISTER" and reg["payload"]["device_type"] == "motion_controller"
        dev = reg["payload"]["device_id"]
        nonce = "ab" * 16
        await ws.send(
            json.dumps(make_message("AUTHENTICATE", {"stage": "challenge", "nonce": nonce, "server_version": "1.0.0"}))
        )
        auth = json.loads(await ws.recv())
        expected = hmac.new(KEY.encode(), f"{PROTOCOL_ID}|{dev}|{nonce}".encode(), hashlib.sha256).hexdigest()
        assert auth["payload"]["mac"] == expected
        await ws.send(
            json.dumps(
                make_message(
                    "AUTHENTICATE",
                    {
                        "stage": "result",
                        "session_id": "s1",
                        "content_token": token,
                        "heartbeat_interval_s": 0.5,
                        "offline_timeout_s": 3,
                        "server_version": "1.0.0",
                        "server_time": time.monotonic() + OFFSET,
                    },
                )
            )
        )
        asset = {
            "programme_id": 4,
            "name": "Demo",
            "revision": 1,
            "duration_s": 6.0,
            "projection": "360",
            "stereo": "mono",
            "loop": True,
            "video": {"url": "/content/v1/video/" + "0" * 64, "sha256": "0" * 64, "size": 1},
            "motion": {
                "url": f"/content/v1/motion/{sha}",
                "sha256": sha,
                "size": 1,
                "sample_rate_hz": 100,
                "duration_s": 6,
            },
        }
        await ws.send(json.dumps(make_message("PREPARE", {"programme": asset, "timeline_id": 1, "deadline_s": 30})))
        async for raw in ws:
            msg = json.loads(raw)
            log["types"].append(msg["type"])
            if msg["type"] == "TIME_SYNC":
                now = time.monotonic() + OFFSET
                await ws.send(json.dumps(make_message("TIME_SYNC", {"t0": msg["payload"]["t0"], "t1": now, "t2": now})))
            elif msg["type"] == "READY":
                log["ready"].append(msg["payload"])
                start_at = time.monotonic() + OFFSET + 1.0
                start_at_box.append(start_at)
                play = {"programme_id": 4, "timeline_id": 1, "start_at": start_at, "duration_s": 6.0, "loop": True}
                await ws.send(json.dumps(make_message("PLAY", play)))
            elif msg["type"] == "STATUS":
                log["status"].append(msg["payload"])

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        pcfg = load_platform_config(SIM)
        cfg = ControllerConfig(
            server_url=f"ws://127.0.0.1:{port}/ws/v1/device",
            control_rate_hz=100.0,
            cache_dir=tmp_path / "cache",
            status_interval_s=0.2,
            platforms=[PlatformEntry(pcfg, KEY)],
        )
        app = ControllerApp(cfg)
        # the scripted HTTP server stands in for the Control Server's content endpoint
        app.links[0].http_base = f"http://127.0.0.1:{http.server_address[1]}"
        stop = asyncio.Event()
        task = asyncio.create_task(app.run(stop))
        await asyncio.sleep(5.0)
        stop.set()
        await task
    http.shutdown()

    assert log["ready"] and log["ready"][0]["ok"] is True and log["ready"][0]["motion_sha256"] == sha
    assert {"HEARTBEAT", "TIME_SYNC", "STATUS", "READY"} <= set(log["types"])
    rt = app.runtimes[0]
    assert abs(rt.clock.offset - OFFSET) < 0.01
    last = log["status"][-1]
    assert last["state"] == "RUNNING" and last["programme_id"] == 4
    assert last["link"]["connected"] is True and last["driver"] == "simulation"
    expected = (rt.clock.master_time() - start_at_box[0]) % 6.0
    assert last["position_s"] is not None and abs(last["position_s"] - expected) < 0.5
    assert (tmp_path / "cache" / f"{sha}.motion").exists()
