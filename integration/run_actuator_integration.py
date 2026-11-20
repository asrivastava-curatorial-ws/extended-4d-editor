#!/usr/bin/env python3
"""End-to-end test of electric, hydraulic and pneumatic platforms, each through another kind of link.

    python integration/run_actuator_integration.py --server /path/to/fourdvr-server

What runs: a programme (made by `fourdvr extract generate`, its motion replaced by a test pattern on
all six axes) -> the unchanged Control Server -> one show service with four platforms -> four
simulated devices, each a process of its own with simulated actuators of its kind (plants.py):

    PLATFORM-01  hydraulic hexapod, valve amplifiers behind a PLC   Modbus TCP          fourdvr controller modbus-sim
    PLATFORM-02  pneumatic seat, 3 proportional valves              ESP32 board, 0-10 V  fourdvr controller esp32-sim --platform
    PLATFORM-03  pneumatic seat, 3 plain on/off valves              ESP32 board, valves  fourdvr controller esp32-sim --platform
    PLATFORM-04  electric seat, 2 drives with their own UDP format  UDP                  fourdvr controller udp-sim

The profiles are the examples in config/platforms/ with only their addresses changed.

Checked: every profile and the configuration are accepted; all four platforms play the programme
and their measured positions follow it (following error within each profile's limit; the valves of
the on/off seat switch both ways); the seats leave out the axes they cannot move; PAUSE holds them
still and RESUME continues; a device that goes away (the PLC is stopped) faults its platform alone
and parks nothing else, and after it is back an operator STOP makes all four ready to play again; STOP brings
every platform back to neutral; no errors in the logs.

Needs: this package with the "studio" extra (for the test video), the Control Server 1.0. No hardware:
what a real valve, drive or PLC does is not part of this test (docs/ACTUATORS.md, commissioning).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_effects_integration import ADMIN_PASSWORD, ROOT, Api, free_port, log, make_video, wait_for

from fourdvr_motion import motionfile
from fourdvr_motion.platform_config import load_platform_config

EXAMPLES = ROOT / "config" / "platforms"
# id: (example profile, simulator, socket kind, driver, actuator, control, axes it moves)
PLATFORMS: dict[str, tuple[str, str, int, str, str, str, int]] = {
    "PLATFORM-01": ("example-hydraulic-6dof.toml", "modbus-sim", socket.SOCK_STREAM, "modbus_tcp", "hydraulic", "position", 6),
    "PLATFORM-02": ("example-pneumatic-3dof.toml", "esp32-sim", socket.SOCK_DGRAM, "esp32", "pneumatic", "position", 3),
    "PLATFORM-03": ("example-pneumatic-onoff-3dof.toml", "esp32-sim", socket.SOCK_DGRAM, "esp32", "pneumatic", "onoff", 3),
    "PLATFORM-04": ("example-electric-2dof.toml", "udp-sim", socket.SOCK_DGRAM, "udp", "electric", "position", 2),
}


def test_pattern(duration: float, rate: float) -> np.ndarray:
    """All six axes at once, at different frequencies, easing in and out over 2 s."""
    n = motionfile.expected_sample_count(duration, rate)
    t = np.arange(n) / rate
    env = np.clip(t / 2.0, 0, 1) * np.clip((duration - t) / 2.0, 0, 1)
    env = env * env * (3 - 2 * env)
    s = np.zeros((n, 6))
    for k, (amp, hz) in enumerate([(0.04, 0.31), (0.03, 0.23), (0.03, 0.5), (0.05, 0.37), (0.05, 0.27), (0.04, 0.19)]):
        s[:, k] = amp * env * np.sin(2 * np.pi * hz * t)
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="fourdvr-server", help="Control Server command")
    ap.add_argument("--fourdvr", default=str(Path(sys.executable).with_name("fourdvr")), help="this product's command")
    ap.add_argument("--work", help="working directory (default: a temporary one, removed afterwards)")
    ap.add_argument("--report", help="write the result as JSON")
    args = ap.parse_args()
    if shutil.which(args.server) is None and not Path(args.server).is_file():
        log(f"error: Control Server command '{args.server}' not found (use --server)")
        return 2
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="fourdvr-actuators-it-"))
    shutil.rmtree(work, ignore_errors=True)
    (work / "logs").mkdir(parents=True)
    (work / "profiles").mkdir()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"ok": bool(ok), "check": name, "detail": detail})
        log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    def tool(*cmd: str) -> str:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode:
            raise RuntimeError(f"{' '.join(cmd[:3])} ... failed:\n{res.stdout[-1500:]}{res.stderr[-1500:]}")
        return res.stdout

    procs: list[subprocess.Popen[bytes]] = []
    devices: dict[str, subprocess.Popen[bytes]] = {}
    profiles: dict[str, Path] = {}
    ports: dict[str, int] = {}

    def start_device(pid: str) -> None:
        sim = PLATFORMS[pid][1]
        cmd = [args.fourdvr, "controller", sim, "--platform", str(profiles[pid]), "--port", str(ports[pid])]
        out = (work / "logs" / f"{pid}-{sim}.log").open("ab")
        devices[pid] = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        procs.append(devices[pid])

    try:
        log("1/7 profiles and simulated devices")
        for pid, (example, _sim, kind, *_rest) in PLATFORMS.items():
            ports[pid] = free_port(kind)
            scheme = "tcp" if kind == socket.SOCK_STREAM else "udp"
            text = (EXAMPLES / example).read_text()
            text = re.sub(r'^address = "[^"]*"', f'address = "{scheme}://127.0.0.1:{ports[pid]}"', text, count=1, flags=re.M)
            profiles[pid] = work / "profiles" / example
            profiles[pid].write_text(text)
            said = tool(args.fourdvr, "controller", "check-platform", str(profiles[pid]))
            (work / "logs" / f"{pid}-check-platform.txt").write_text(said)
            check(f"{pid}: check-platform accepts {example}", "configuration OK" in said, said.splitlines()[2].strip())
            start_device(pid)

        log("2/7 programme")
        video, motion = work / "test-ride.mp4", work / "programme1.motion"
        make_video(video, 30.0)
        tool(args.fourdvr, "extract", "generate", str(video), "-o", str(motion), "--projection", "360",
             "--profile", str(EXAMPLES / "simulation-6dof.toml"), "-q")
        mf = motionfile.read_motion(motion)
        rate = float(mf.header["sample_rate_hz"])
        motionfile.write_motion(motion, mf.header, test_pattern(float(mf.header["duration_s"]), rate))

        log("3/7 Control Server and show service")
        port = free_port()
        cfg = work / "server.toml"
        cfg.write_text(
            f'[server]\nhost = "127.0.0.1"\nport = {port}\ndata_dir = "{work / "server-data"}"\nlog_level = "WARNING"\nlog_format = "text"\n'
            f'[security]\ninitial_admin_password = "{ADMIN_PASSWORD}"\n[devices]\nheartbeat_interval_s = 1.0\noffline_timeout_s = 5.0\n'
            "[playback]\nstart_lead_s = 3.0\ntransition_gap_s = 1.5\nprepare_timeout_s = 60.0\n"
        )
        tool(args.server, "-c", str(cfg), "init")
        procs.append(subprocess.Popen([args.server, "-c", str(cfg), "serve"], stdout=(work / "logs/server.log").open("wb"),
                                      stderr=subprocess.STDOUT))
        api = Api(f"http://127.0.0.1:{port}")
        wait_for(lambda: api.call("GET", "/api/v1/health", timeout=2), 30, "the Control Server")
        api.token = api.call("POST", "/api/v1/auth/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]
        api.call("POST", "/api/v1/programmes", {"programme_id": 1, "name": "Test pattern", "projection": "360", "stereo": "mono", "loop": True})
        api.call("PUT", f"/api/v1/programmes/1/video?filename={video.name}", raw=video.read_bytes())
        api.call("PUT", f"/api/v1/programmes/1/motion?filename={motion.name}", raw=motion.read_bytes())
        check("the Control Server accepts the programme", bool(api.call("POST", "/api/v1/programmes/1/validate")["passed"]))
        api.call("POST", "/api/v1/programmes/1/publish")
        keys = dict(line.split("\t") for line in tool(args.server, "-c", str(cfg), "device-key", *PLATFORMS).strip().splitlines())
        ctl = work / "controller.toml"
        ctl.write_text(
            f'[controller]\nserver_url = "ws://127.0.0.1:{port}/ws/v1/device"\ncontrol_rate_hz = 100.0\ncache_dir = "{work / "cache"}"\n'
            'status_interval_s = 0.25\ntime_sync_interval_s = 2.0\nlog_level = "INFO"\n'
            + "".join(f'\n[[platforms]]\nconfig = "{profiles[pid]}"\nid = "{pid}"\ndevice_key = "{keys[pid]}"\n' for pid in PLATFORMS)
        )
        said = tool(args.fourdvr, "controller", "check-config", "-c", str(ctl))
        (work / "logs/check-config.txt").write_text(said)
        check("fourdvr controller check-config accepts four platforms on four kinds of link", "configuration OK" in said)
        procs.append(subprocess.Popen([args.fourdvr, "controller", "run", "-c", str(ctl)], stdout=(work / "logs/controller.log").open("wb"),
                                      stderr=subprocess.STDOUT))

        def platforms() -> dict[str, dict[str, Any]]:
            return {p["device_id"]: p for p in api.call("GET", "/api/v1/devices", timeout=5)["motion_platforms"] if p["connection"] == "CONNECTED"}

        def all_in(*states: str) -> dict[str, dict[str, Any]] | None:
            ps = platforms()
            return ps if len(ps) == len(PLATFORMS) and all(p["state"] in states for p in ps.values()) else None

        wait_for(lambda: all_in("READY"), 60, "four platforms READY")
        ready = wait_for(lambda: (lambda ps: ps if ps and all((p.get("details") or {}).get("link") for p in ps.values()) else None)(
            all_in("READY")), 15, "the first full status of each platform")
        for pid, (_ex, _sim, _kind, driver, actuator, control, _axes) in PLATFORMS.items():
            d = ready[pid].get("details") or {}
            check(f"{pid}: {actuator} actuators ({control}) through '{driver}', positions measured",
                  d.get("driver") == driver and d.get("actuator") == actuator and d.get("control") == control
                  and bool((d.get("link") or {}).get("measured")), f"device {(d.get('link') or {}).get('device_state')}")

        log("4/7 PLAY: 12 s of the test pattern on all four platforms")
        api.call("POST", "/api/v1/playback/play", {"programme_id": 1})
        wait_for(lambda: all_in("RUNNING"), 60, "four platforms RUNNING")
        time.sleep(2.5)                                                 # past the easing-in of the pattern
        samples: dict[str, list[dict[str, Any]]] = {pid: [] for pid in PLATFORMS}
        end = time.time() + 12.0
        while time.time() < end:
            for pid, p in platforms().items():
                samples[pid].append(dict(p.get("details") or {}, state=p["state"]))
            time.sleep(0.2)
        for pid, (_ex, _sim, _kind, _drv, actuator, control, axes) in PLATFORMS.items():
            pc = load_platform_config(profiles[pid])
            rows = samples[pid]
            meas = np.array([r["measured_strokes_m"] for r in rows if r.get("measured_strokes_m")])
            errs = [max(r["following_error_mm"]) for r in rows if r.get("following_error_mm")]
            faults = sorted({str(r.get("fault")) for r in rows if r.get("fault")})
            check(f"{pid}: RUNNING the whole time, no fault", bool(rows) and all(r["state"] == "RUNNING" for r in rows) and not faults,
                  "; ".join(faults) or f"{len(rows)} status reports")
            if control == "onoff":
                stroke = pc.actuators.stroke
                both = bool(len(meas)) and bool(np.all(meas.min(axis=0) < 0.2 * stroke)) and bool(np.all(meas.max(axis=0) > 0.8 * stroke))
                check(f"{pid}: every cylinder is driven out and back by its valve", both,
                      "" if not len(meas) else f"min {np.round(meas.min(axis=0) * 1000).astype(int)} max {np.round(meas.max(axis=0) * 1000).astype(int)} mm")
            else:
                span = meas.max(axis=0) - meas.min(axis=0) if len(meas) else np.zeros(1)
                check(f"{pid}: every {actuator} actuator moves with the programme", bool(len(meas)) and bool(np.all(span > 0.01)),
                      f"travel {np.round(span * 1000).astype(int)} mm")
            limit = pc.supervision.following_error * 1000
            check(f"{pid}: measured positions follow the commands (following error below {limit:.0f} mm)",
                  bool(errs) and max(errs) < limit, f"worst {max(errs) if errs else float('nan'):.1f} mm")
            left_out = int((rows[-1].get("limit_events") or {}).get("axes_left_out", -1)) if rows else -1
            check(f"{pid}: {'no axis left out' if axes == 6 else f'the {6 - axes} axes the platform cannot move are left out'}",
                  left_out == 0 if axes == 6 else left_out > 0, f"axes_left_out {left_out}")

        log("5/7 PAUSE and RESUME")
        api.call("POST", "/api/v1/playback/pause")
        time.sleep(2.5)
        a = {pid: np.array((p.get("details") or {}).get("measured_strokes_m") or [np.nan]) for pid, p in platforms().items()}
        time.sleep(1.0)
        b = {pid: np.array((p.get("details") or {}).get("measured_strokes_m") or [np.nan]) for pid, p in platforms().items()}
        moved = {pid: float(np.max(np.abs(b[pid] - a[pid]))) * 1000 for pid in PLATFORMS if pid in a and pid in b}
        check("PAUSE: every platform holds still", len(moved) == len(PLATFORMS) and all(v < 2.0 for v in moved.values()),
              ", ".join(f"{pid} {v:.2f} mm" for pid, v in moved.items()))
        api.call("POST", "/api/v1/playback/resume")
        wait_for(lambda: all_in("RUNNING"), 30, "RESUME")
        check("RESUME: all four RUNNING again", True)

        log("6/7 the PLC of PLATFORM-01 goes away during the show")
        devices["PLATFORM-01"].terminate()
        devices["PLATFORM-01"].wait(timeout=10)
        lost = wait_for(lambda: (lambda ps: ps if ps.get("PLATFORM-01", {}).get("state") not in (None, "RUNNING") else None)(platforms()), 10,
                        "PLATFORM-01 to notice")
        reason = str((lost["PLATFORM-01"].get("details") or {}).get("fault") or "")
        check("PLATFORM-01 faults when its device stops answering", lost["PLATFORM-01"]["state"] == "ERROR" and bool(reason), reason)
        time.sleep(2.0)
        others = platforms()
        check("the other three keep playing", all(others[pid]["state"] == "RUNNING" for pid in ("PLATFORM-02", "PLATFORM-03", "PLATFORM-04")),
              ", ".join(f"{pid} {p['state']}" for pid, p in others.items()))
        start_device("PLATFORM-01")
        time.sleep(1.5)
        api.call("POST", "/api/v1/playback/stop")
        wait_for(lambda: all_in("STOPPED", "READY"), 60, "four platforms STOPPED after the operator's STOP")
        check("the device is back: after the operator's STOP all four are ready to play again", True)

        log("7/7 PLAY, then STOP: back to neutral")
        api.call("POST", "/api/v1/playback/play", {"programme_id": 1})
        wait_for(lambda: all_in("RUNNING"), 60, "four platforms RUNNING")
        time.sleep(6.0)
        api.call("POST", "/api/v1/playback/stop")
        wait_for(lambda: all_in("STOPPED", "READY"), 60, "four platforms STOPPED")
        time.sleep(2.0)
        final = platforms()
        far = {pid: max(abs(float(v)) for v in (final[pid].get("details") or {}).get("pose", [1.0])) for pid in PLATFORMS}
        errs = {pid: max((final[pid].get("details") or {}).get("following_error_mm") or [99.0]) for pid in PLATFORMS}
        check("STOP: every platform back at neutral, its actuators where they were told to be",
              all(v < 1e-3 for v in far.values()) and all(errs[pid] < load_platform_config(profiles[pid]).supervision.following_error * 1000
                                                          for pid in PLATFORMS),
              ", ".join(f"{pid} {errs[pid]:.1f} mm" for pid in PLATFORMS))
        texts = [(work / "logs/controller.log").read_text(errors="replace")] + [
            (work / "logs" / f"{pid}-{PLATFORMS[pid][1]}.log").read_text(errors="replace") for pid in PLATFORMS]
        check("no errors in the logs of the show service and the four devices", all("Traceback" not in t for t in texts))
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    passed = bool(checks) and all(c["ok"] for c in checks)
    report = {"result": "PASSED" if passed else "FAILED", "passed": sum(c["ok"] for c in checks), "checks": checks}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    log(f"RESULT: {report['result']} ({report['passed']}/{len(checks)} checks)")
    if not args.work and passed:
        shutil.rmtree(work, ignore_errors=True)
    elif not passed:
        log(f"working directory kept: {work}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
