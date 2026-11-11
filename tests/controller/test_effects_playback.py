"""Optional 4D effects in the controller: in sync with motion when configured, absent otherwise,
and never able to disturb motion."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio.server import serve

from controller_helpers import ROOT, SIM, FakeClock, demo_samples, make_runtime, run_for
from fourdvr_fx import BassTrack, Cue, EffectsDocument
from fourdvr_fx.dmx import CaptureOutput
from fourdvr_fx.embed import attach_effects
from fourdvr_fx.patch import load_patch
from fourdvr_fx.service import EffectsService
from fourdvr_motion import motionfile
from fourdvr_motion.app import ControllerApp
from fourdvr_motion.config import (
    ControllerConfig,
    ControllerConfigError,
    EffectsSettings,
    PlatformEntry,
    load_controller_config,
)
from fourdvr_motion.effects_link import EffectsLink
from fourdvr_motion.platform_config import load_platform_config
from fourdvr_motion.protocol import PROTOCOL_ID, make_message
from fourdvr_motion.runtime import Phase

PATCH = ROOT / "config" / "effects" / "patch.example.toml"
IDS = ["PLATFORM-01", "PLATFORM-02"]


def effects_doc(duration: float = 6.0) -> EffectsDocument:
    doc = EffectsDocument(duration)
    doc.new_track("wind").points = [(0.0, 0.0), (1.0, 1.0), (duration, 1.0)]
    doc.new_track("water").cues = [Cue(2.0, 0.5)]
    doc.new_track("fog").cues = [Cue(3.0, 1.0)]
    return doc


def write_motion_fx(path: Path, doc: EffectsDocument | None, programme_id: int = 1, duration: float = 6.0) -> str:
    header = motionfile.build_header(programme_id=programme_id, duration_s=duration, sample_rate_hz=100.0,
                                     video={"sha256": "c" * 64, "filename": "demo.mp4"}, generation={"generator": "tests"})
    return motionfile.write_motion(path, attach_effects(header, doc), demo_samples(duration, 100.0))


def rig(platform_cfg, tmp_path: Path, clock: FakeClock, doc: EffectsDocument | None, with_effects: bool = True):  # type: ignore[no-untyped-def]
    """Two platforms sharing one cache; an effects link with a capturing DMX output on the fake clock."""
    rts = []
    for pid in IDS:
        rt = make_runtime(replace(platform_cfg, platform_id=pid), tmp_path, clock)
        rts.append(rt)
    cache = rts[0].cache
    rts[1].cache = cache
    sha = write_motion_fx(cache.path("x"), doc)
    cache.path("x").rename(cache.path(sha))
    link, cap = None, CaptureOutput()
    if with_effects:
        link = EffectsLink(None, rts, cache)
        link.service = EffectsService(load_patch(PATCH), IDS, link.states, output=cap, clock=clock)
    for rt in rts:
        run_for(rt, clock, 0.0)
    return rts, link, cap, sha


def step(rts, link, clock: FakeClock, seconds: float, record: list | None = None) -> None:  # type: ignore[no-untyped-def]
    """Control ticks at 100 Hz, one DMX frame every 20 ms."""
    for k in range(round(seconds * 100)):
        clock.advance(0.01)
        for rt in rts:
            rt.tick(clock())
        if record is not None:
            record.append(np.concatenate([rt.pipeline.pose for rt in rts]))
        if link is not None and k % 2 == 1:
            link.service.tick(clock())


def start_show(rts, clock: FakeClock, sha: str, link=None) -> float:  # type: ignore[no-untyped-def]
    step(rts, link, clock, 0.2)
    for rt in rts:
        rt.register_programme(1, sha, 6.0)
    start_at = rts[0].clock.master_time(clock()) + 0.5
    for rt in rts:
        rt.cmd_play(1, 1, start_at, 6.0, True)
    return start_at


def test_effects_follow_each_platform_and_the_theatre(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rts, link, cap, sha = rig(platform_cfg, tmp_path, clock, effects_doc())
    start_show(rts, clock, sha, link)
    step(rts, link, clock, 0.3)
    assert rts[0].phase == Phase.SCHEDULED and max(cap.latest[2]) == 0 and max(cap.latest[1]) == 0   # nothing before the start
    step(rts, link, clock, 0.2 + 2.2)                                       # show position 2.2 s
    assert rts[0].phase in (Phase.ENGAGING, Phase.PLAYING)
    assert cap.channel(2, 1) == 255 and cap.channel(2, 9) == 255            # wind at 100 % on both platforms
    assert cap.channel(2, 2) == 255 and cap.channel(2, 10) == 255           # water burst (2.0-2.5 s) on both
    assert cap.channel(1, 1) == 255                                         # fog: cue at 3.0 s, sent 1 s early (lead)
    st = rts[0].status()["effects"]
    assert st["running"] is False and st["playing"] and st["programme_has_effects"] and st["theatre_follows"] == "PLATFORM-01"
    # platform 2 pauses: its own effects stop, platform 1 and the theatre fixtures carry on
    pos = rts[1].clock.master_time(clock()) - rts[1].timeline.epoch
    rts[1].cmd_pause(1, 2, rts[1].clock.master_time(clock()), pos)
    step(rts, link, clock, 0.8)
    assert rts[1].phase == Phase.PAUSED and cap.channel(2, 9) == 0 and cap.channel(2, 1) == 255
    assert cap.channel(1, 1) == 255 and rts[1].status()["effects"]["playing"] is False
    # platform 1 faults: the theatre follows nobody now (platform 2 is paused) - everything is off
    rts[0]._enter_fault("test fault")
    step(rts, link, clock, 0.1)
    assert cap.channel(1, 1) == 0 and link.states(clock())["theatre"].active is False
    assert 0 < cap.channel(2, 1) < 255                                      # the fan is spun down over its 0.5 s ramp ...
    step(rts, link, clock, 0.6)
    assert cap.channel(2, 1) == 0                                           # ... and is off
    # platform 2 resumes: the theatre fixtures follow it
    resume_at = rts[1].clock.master_time(clock()) + 0.1
    rts[1].cmd_resume(1, 3, resume_at, pos)
    step(rts, link, clock, 1.0)
    assert rts[1].phase in (Phase.ENGAGING, Phase.PLAYING) and cap.channel(2, 9) == 255 and cap.channel(2, 1) == 0
    assert rts[1].status()["effects"]["theatre_follows"] == "PLATFORM-02"
    # stop: dark
    rts[1].cmd_stop(4, rts[1].clock.master_time(clock()))
    step(rts, link, clock, 0.7)
    assert max(cap.latest[1]) == 0 and max(cap.latest[2]) == 0


def test_motion_is_bit_identical_with_and_without_effects(platform_cfg, tmp_path: Path) -> None:
    traces = []
    for k, (doc, fx) in enumerate(((None, False), (effects_doc(), True), (effects_doc(), False))):
        clock = FakeClock()
        rts, link, _cap, sha = rig(platform_cfg, tmp_path / str(k), clock, doc, with_effects=fx)
        record: list = []
        start_show(rts, clock, sha, link)
        step(rts, link, clock, 7.0, record)
        traces.append(np.array(record))
        assert rts[0].phase == Phase.PLAYING
        assert ("effects" in rts[0].status()) is fx                         # STATUS unchanged without an effects patch
    assert np.array_equal(traces[0], traces[1]) and np.array_equal(traces[0], traces[2])
    assert np.max(np.abs(traces[0])) > 0.01                                 # the platforms did move


def test_programme_without_effects_and_broken_effects_play_motion_only(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    rts, link, cap, sha = rig(platform_cfg, tmp_path / "plain", clock, None)
    start_show(rts, clock, sha, link)
    step(rts, link, clock, 3.0)
    assert rts[0].phase == Phase.PLAYING and all(max(f) == 0 for f in cap.latest.values())
    st = rts[0].status()["effects"]
    assert st["programme_has_effects"] is False and st["playing"] is False and link.service.errors == 0
    # a damaged effects section: reported, ignored, motion plays
    clock = FakeClock()
    rts, link, cap, _sha = rig(platform_cfg, tmp_path / "bad", clock, None)
    header = motionfile.build_header(programme_id=1, duration_s=6.0, sample_rate_hz=100.0,
                                     video={"sha256": "c" * 64}, generation={})
    header["effects"] = {"format": "4D-VR-THEATRE-EFFECTS", "version": "1.0.0", "duration_s": 6, "tracks": [{"id": "BAD"}]}
    bad_sha = motionfile.write_motion(rts[0].cache.path("y"), header, demo_samples(6.0, 100.0))
    rts[0].cache.path("y").rename(rts[0].cache.path(bad_sha))
    start_show(rts, clock, bad_sha, link)
    step(rts, link, clock, 3.0)
    assert rts[0].phase == Phase.PLAYING and all(max(f) == 0 for f in cap.latest.values())
    assert "tracks[0]" in rts[0].status()["effects"]["programme_error"] and not rts[0].errors


def test_effects_configuration_never_blocks_the_controller(tmp_path: Path) -> None:
    (tmp_path / "platforms").mkdir()
    (tmp_path / "platforms" / "sim.toml").write_text(SIM.read_text())
    base = f"""
[controller]
server_url = "ws://127.0.0.1:1/ws/v1/device"
cache_dir = "{tmp_path / 'cache'}"
[[platforms]]
config = "platforms/sim.toml"
id = "PLATFORM-01"
device_key = "{'k' * 40}"
"""
    plain = tmp_path / "plain.toml"
    plain.write_text(base)
    cfg = load_controller_config(plain)
    assert cfg.effects is None and ControllerApp(cfg).effects is None       # no [effects]: nothing is created
    good = tmp_path / "good.toml"
    good.write_text(base + f'[effects]\npatch = "{PATCH}"\nreference_platform = "PLATFORM-01"\n')
    cfg = load_controller_config(good)
    assert isinstance(cfg.effects, EffectsSettings) and cfg.effects.patch_path == PATCH
    app = ControllerApp(cfg, effects_output=CaptureOutput())
    assert app.effects is not None and app.effects.service is not None and app.effects.error == ""
    assert len(app.effects.service.engine.instances) == 4 + 3               # theatre fixtures + platform 1 only
    missing = tmp_path / "missing.toml"
    missing.write_text(base + '[effects]\npatch = "effects/nowhere.toml"\n')
    app = ControllerApp(load_controller_config(missing))                    # starts; reports the problem
    assert app.effects is not None and app.effects.service is None and "nowhere.toml" in app.effects.error
    st = app.runtimes[0].status()["effects"]
    assert st["running"] is False and "nowhere.toml" in st["error"]
    off = tmp_path / "off.toml"
    off.write_text(base + f'[effects]\npatch = "{PATCH}"\nenabled = false\n')
    assert ControllerApp(load_controller_config(off)).effects.service is None
    for bad in ('[effects]\nenabled = true\n', f'[effects]\npatch = "{PATCH}"\nspeed = 2\n'):
        broken = tmp_path / "broken.toml"
        broken.write_text(base + bad)
        with pytest.raises(ControllerConfigError):
            load_controller_config(broken)


def test_bass_track_is_prepared_with_the_programme(platform_cfg, tmp_path: Path) -> None:
    clock = FakeClock()
    doc = effects_doc()
    doc.bass = BassTrack.from_float(0.3 * np.sin(2 * np.pi * 40 * np.arange(12000) / 2000.0), 2000)
    rts, link, _cap, sha = rig(platform_cfg, tmp_path, clock, doc)
    patch = load_patch(PATCH)
    patch.audio.enabled = True
    link.service = EffectsService(patch, IDS, link.states, output=CaptureOutput(), clock=clock)
    step(rts, None, clock, 0.2)
    rts[0].register_programme(1, sha, 6.0)                                  # PREPARE: resampled ahead of the show
    assert len(link.service.renderer._tracks) == 1
    rts[1].register_programme(1, sha, 6.0)
    start_at = rts[0].clock.master_time(clock()) + 0.5
    for rt in rts:
        rt.cmd_play(1, 1, start_at, 6.0, True)
    step(rts, link, clock, 2.5)
    audio = link.service._audio_state
    assert audio.active and audio.track_id != 0 and audio.loop_s == 6.0
    assert audio.position_s == pytest.approx(rts[0].clock.master_time(clock()) - start_at, abs=0.03)


# ------------------------------------------------------------------------------ whole process
KEY = "k" * 40
OFFSET = 1000.0


@pytest.mark.asyncio
async def test_controller_process_plays_effects_from_the_server_timeline(tmp_path: Path) -> None:
    """Real threads end to end: scripted Control Server -> PREPARE (download) -> PLAY -> DMX frames."""
    content = tmp_path / "content"
    content.mkdir()
    sha = write_motion_fx(content / "tmp.motion", effects_doc(), programme_id=4)
    (content / "tmp.motion").rename(content / f"{sha}.motion")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            data = (content / f"{self.path.rsplit('/', 1)[-1]}.motion").read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    box: dict[str, list] = {"start": [], "status": []}

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        reg = json.loads(await ws.recv())
        dev = reg["payload"]["device_id"]
        nonce = "ab" * 16
        await ws.send(json.dumps(make_message("AUTHENTICATE", {"stage": "challenge", "nonce": nonce, "server_version": "1.0.0"})))
        auth = json.loads(await ws.recv())
        assert auth["payload"]["mac"] == hmac.new(KEY.encode(), f"{PROTOCOL_ID}|{dev}|{nonce}".encode(), hashlib.sha256).hexdigest()
        await ws.send(json.dumps(make_message("AUTHENTICATE", {
            "stage": "result", "session_id": "s1", "content_token": "t", "heartbeat_interval_s": 0.5,
            "offline_timeout_s": 3, "server_version": "1.0.0", "server_time": time.monotonic() + OFFSET})))
        asset = {"programme_id": 4, "name": "Demo", "revision": 1, "duration_s": 6.0, "projection": "360", "stereo": "mono",
                 "loop": True, "video": {"url": "/content/v1/video/" + "0" * 64, "sha256": "0" * 64, "size": 1},
                 "motion": {"url": f"/content/v1/motion/{sha}", "sha256": sha, "size": 1, "sample_rate_hz": 100, "duration_s": 6}}
        await ws.send(json.dumps(make_message("PREPARE", {"programme": asset, "timeline_id": 1, "deadline_s": 30})))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "TIME_SYNC":
                now = time.monotonic() + OFFSET
                await ws.send(json.dumps(make_message("TIME_SYNC", {"t0": msg["payload"]["t0"], "t1": now, "t2": now})))
            elif msg["type"] == "READY":
                assert msg["payload"]["ok"] is True
                box["start"].append(time.monotonic() + OFFSET + 0.6)
                await ws.send(json.dumps(make_message("PLAY", {"programme_id": 4, "timeline_id": 1, "start_at": box["start"][0],
                                                               "duration_s": 6.0, "loop": True})))
            elif msg["type"] == "STATUS":
                box["status"].append(msg["payload"])

    cap = CaptureOutput(history=100000)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        pcfg = replace(load_platform_config(SIM), platform_id="PLATFORM-01")
        cfg = ControllerConfig(server_url=f"ws://127.0.0.1:{port}/ws/v1/device", control_rate_hz=100.0, cache_dir=tmp_path / "cache",
                               status_interval_s=0.2, platforms=[PlatformEntry(pcfg, KEY)], effects=EffectsSettings(PATCH))
        app = ControllerApp(cfg, effects_output=cap)
        app.links[0].http_base = f"http://127.0.0.1:{http.server_address[1]}"
        stop = asyncio.Event()
        task = asyncio.create_task(app.run(stop))
        await asyncio.sleep(4.2)
        running = app.effects.service.running
        stop.set()
        await task
    http.shutdown()
    assert running and box["start"] and app.effects.service.errors == 0 and not cap.is_open
    rt = app.runtimes[0]
    start_local = box["start"][0] - rt.clock.offset                         # show start on this process's clock
    water = [t - start_local for t, u, d in cap.history if u == 2 and d[1] == 255]
    fog = [t - start_local for t, u, d in cap.history if u == 1 and d[0] == 255]
    wind = [(t - start_local, d[0]) for t, u, d in cap.history if u == 2]
    assert water and min(water) == pytest.approx(2.0, abs=0.08) and max(water) == pytest.approx(2.5, abs=0.08)
    assert fog and min(fog) == pytest.approx(2.0, abs=0.08)                 # cue at 3.0 s minus the 1 s lead
    assert all(v == 0 for t, v in wind if t < -0.05)                        # dark until the show starts
    assert max(v for t, v in wind if 1.8 < t < 3.0) == 255                  # wind fully on after its ramp
    assert max(cap.latest[1]) == 0 and max(cap.latest[2]) == 0              # dark again after shutdown
    last = [s for s in box["status"] if s.get("effects")][-1]
    assert last["state"] == "RUNNING" and last["effects"]["running"] and last["effects"]["playing"]
    assert last["effects"]["output"] == "capture" and last["effects"]["errors"] == 0
