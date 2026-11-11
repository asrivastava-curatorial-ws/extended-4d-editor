from __future__ import annotations

import hashlib
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from controller_helpers import EXAMPLE, SIM, write_motion
from fourdvr_motion import motionfile
from fourdvr_motion.cli import main
from fourdvr_motion.config import ControllerConfigError, load_controller_config
from fourdvr_motion.content_cache import ContentError, MotionCache


def test_cli_platform_tools(capsys) -> None:
    assert main(["check-platform", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "configuration OK" in out and "surge" in out
    assert main(["ik", "--platform", str(EXAMPLE), "--pose", "0.02,0,0.01,0.03,0,0"]) == 0
    assert "within stroke" in capsys.readouterr().out
    assert main(["ik", "--platform", str(EXAMPLE), "--pose", "0.3,0,0.2,0,0,0"]) == 1
    assert main(["geometry", "--base-radius", "0.7", "--platform-radius", "0.5", "--neutral-length", "0.8"]) == 0
    assert "base_joints" in capsys.readouterr().out


def test_cli_motion_info_and_play_local(tmp_path: Path, capsys) -> None:
    path = tmp_path / "demo.motion"
    write_motion(path, duration=4.0)
    assert main(["motion-info", str(path), "--platform", str(SIM)]) == 0
    assert "0 samples outside position limits" in capsys.readouterr().out
    assert main(["play-local", str(path), "--platform", str(SIM)]) == 0
    out = capsys.readouterr().out
    assert "simulated ESP32: received" in out and "lost 0" in out


def test_controller_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        f"""
[controller]
server_url = "ws://127.0.0.1:8080/ws/v1/device"
control_rate_hz = 120
[[platforms]]
config = "{SIM}"
id = "PLATFORM-01"
link_platform_id = 1
device_key = "abc"
[[platforms]]
config = "{SIM}"
id = "PLATFORM-02"
link_platform_id = 2
device_key = "def"
"""
    )
    cfg = load_controller_config(cfg_path)
    assert [p.config.platform_id for p in cfg.platforms] == ["PLATFORM-01", "PLATFORM-02"]
    assert cfg.platforms[1].config.link_platform_id == 2 and cfg.control_rate_hz == 120
    cfg_path.write_text(cfg_path.read_text().replace("control_rate_hz = 120", "control_rate_hz = 500"))
    with pytest.raises(ControllerConfigError, match="50 and 200"):
        load_controller_config(cfg_path)


def test_motion_cache_download_verify_and_corruption(tmp_path: Path) -> None:
    served = tmp_path / "served" / "content" / "v1" / "motion"
    served.mkdir(parents=True)
    src = tmp_path / "x.motion"
    sha = write_motion(src)
    (served / sha).write_bytes(src.read_bytes())
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path / "served"))
    handler.log_message = lambda *a: None  # type: ignore[method-assign]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cache = MotionCache(tmp_path / "cache")
    path = cache.fetch(base, f"/content/v1/motion/{sha}", sha, "tok")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    track, mf = cache.load(sha)
    assert mf.programme_id == 1 and track.rate == 100.0
    # corrupt cached copy is detected and replaced
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    cache.fetch(base, f"/content/v1/motion/{sha}", sha, "tok")
    assert motionfile.read_motion(path).programme_id == 1
    with pytest.raises(ContentError, match="checksum"):
        cache.fetch(base, f"/content/v1/motion/{sha}", "f" * 64, "tok")
    srv.shutdown()
