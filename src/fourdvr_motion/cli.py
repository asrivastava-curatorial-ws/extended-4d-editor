"""Command-line interface: fourdvr-motion <command>."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np

from . import PRODUCT_TITLE, __version__, motionfile
from .kinematics import AXES, symmetric_hexapod
from .platform_config import PlatformConfig, load_platform_config


def _pose(text: str) -> np.ndarray:
    vals = [float(v) for v in text.replace(" ", "").split(",")]
    if len(vals) != 6:
        raise argparse.ArgumentTypeError(
            "pose needs 6 comma-separated values: surge,sway,heave,roll,pitch,yaw (m, rad)"
        )
    return np.array(vals)


def _fmt(arr: np.ndarray, digits: int = 4) -> str:
    return "[" + ", ".join(f"{v:.{digits}f}" for v in arr) + "]"


def reach_table(cfg: PlatformConfig) -> list[tuple[str, float, float]]:
    """Largest single-axis excursion (each direction) that keeps every actuator inside its stroke."""
    out = []
    act = cfg.actuators
    for i, axis in enumerate(AXES):
        res = []
        for sign in (1.0, -1.0):
            lo, hi = 0.0, 1.0
            for _ in range(40):
                mid = (lo + hi) / 2
                pose = np.zeros(6)
                pose[i] = sign * mid
                lengths = cfg.geometry.leg_lengths(pose)
                if np.all(lengths >= act.usable_min()) and np.all(lengths <= act.usable_max()):
                    lo = mid
                else:
                    hi = mid
            res.append(lo)
        out.append((axis, res[0], -res[1]))
    return out


def cmd_check_platform(args: argparse.Namespace) -> int:
    cfg = load_platform_config(args.platform)
    print(f"platform {cfg.platform_id} ({cfg.name}) driver={cfg.driver} address={cfg.address or '-'}")
    print(f"profile sha256 {cfg.sha256}")
    print(f"neutral leg lengths  {_fmt(cfg.geometry.neutral_lengths())} m")
    print(f"actuator stroke      {_fmt(cfg.actuators.stroke)} m")
    print("single-axis reach (stroke-limited) vs configured limits:")
    for (axis, plus, minus), lo, hi in zip(
        reach_table(cfg), cfg.limits.position_min, cfg.limits.position_max, strict=True
    ):
        unit, k = ("deg", 180 / math.pi) if axis in ("roll", "pitch", "yaw") else ("mm", 1000.0)
        print(
            f"  {axis:6s} reach {minus * k:+8.1f} .. {plus * k:+8.1f} {unit}"
            f"   limit {lo * k:+8.1f} .. {hi * k:+8.1f} {unit}"
        )
    from .limits import implied_actuator_demand

    v_w, a_w = implied_actuator_demand(cfg.geometry, cfg.actuators, cfg.limits)
    act = cfg.actuators
    print(
        f"worst-case combined demand: actuator speed {v_w:.3f} m/s (limit {act.max_velocity}), "
        f"acceleration {a_w:.2f} m/s² (limit {act.max_acceleration})"
    )
    if v_w > act.max_velocity or a_w > act.max_acceleration:
        print(
            "  note: all six axes at their limits simultaneously would exceed the actuator limits; the runtime "
            "guard scales such motion down. Validate content with the Motion Extractor against this profile."
        )
    print("configuration OK")
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    from .config import load_controller_config

    cfg = load_controller_config(args.config)
    print(f"server {cfg.server_url}, control rate {cfg.control_rate_hz:g} Hz, cache {cfg.cache_dir}")
    for entry in cfg.platforms:
        pc = entry.config
        print(
            f"  {pc.platform_id:12s} driver={pc.driver:15s} address={pc.address or '-':28s}"
            f" link id={pc.link_platform_id}"
        )
    if not _effects_summary(cfg):
        print("the effects configuration has errors - the controller would still start and run MOTION ONLY")
        return 1
    print("configuration OK")
    return 0


def _effects_summary(cfg: Any) -> bool:
    """Print what the optional [effects] section amounts to; False if it cannot be used as written."""
    fx = cfg.effects
    if fx is None:
        print("effects: not configured - motion only")
        return True
    from fourdvr_fx.cli import patch_table
    from fourdvr_fx.patch import load_patch

    ids = [entry.config.platform_id for entry in cfg.platforms]
    if not fx.enabled:
        print(f"effects: switched off (enabled = false), patch {fx.patch_path} - motion only")
        return True
    try:
        patch = load_patch(fx.patch_path)
    except (ValueError, OSError) as exc:
        print(f"effects: ERROR in patch {fx.patch_path}: {exc}")
        return False
    print(f"effects: patch {fx.patch_path}" + (f", theatre effects follow {fx.reference_platform}" if fx.reference_platform else ""))
    hosts = {}
    for entry in cfg.platforms:                      # the ESP32 of each platform: the host of its motion link
        url = urlparse(getattr(entry.config, "address", "") or "")
        if url.scheme == "udp" and url.hostname:
            hosts[entry.config.platform_id] = url.hostname
    for line in patch_table(patch, ids, hosts):
        print("  " + line)
    for warning in patch.warnings():
        print(f"  warning: {warning}")
    problems = patch.problems(ids)
    for problem in problems:
        print(f"  ERROR: {problem}")
    if fx.reference_platform and fx.reference_platform not in ids:
        print(f"  ERROR: reference_platform {fx.reference_platform} is not one of this controller's platforms")
        return False
    return not problems


def cmd_geometry(args: argparse.Namespace) -> int:
    geo = symmetric_hexapod(
        args.base_radius,
        args.platform_radius,
        args.base_pair_angle,
        args.platform_pair_angle,
        args.neutral_length,
        rotation_centre=tuple(float(v) for v in args.rotation_centre.split(",")),  # type: ignore[arg-type]
    )
    print("[geometry]")
    print("base_joints = [")
    for row in geo.base_joints:
        print(f"  [{row[0]:.5f}, {row[1]:.5f}, {row[2]:.5f}],")
    print("]")
    print("platform_joints = [")
    for row in geo.platform_joints:
        print(f"  [{row[0]:.5f}, {row[1]:.5f}, {row[2]:.5f}],")
    print("]")
    n = geo.neutral_position
    print(f"neutral_position = [{n[0]:.5f}, {n[1]:.5f}, {n[2]:.5f}]")
    print("neutral_orientation = [0.0, 0.0, 0.0]")
    c = geo.rotation_centre
    print(f"rotation_centre = [{c[0]:.4f}, {c[1]:.4f}, {c[2]:.4f}]")
    return 0


def cmd_ik(args: argparse.Namespace) -> int:
    cfg = load_platform_config(args.platform)
    pose = args.pose
    lengths = cfg.geometry.leg_lengths(pose + cfg.calibration.zero_offsets)
    strokes = lengths - cfg.actuators.min_length + cfg.calibration.actuator_offsets
    ok = np.all(lengths >= cfg.actuators.usable_min()) and np.all(lengths <= cfg.actuators.usable_max())
    print(f"pose            {_fmt(pose)}")
    print(f"leg lengths (m) {_fmt(lengths, 5)}")
    print(f"strokes (mm)    {_fmt(strokes * 1000, 2)}")
    print("within stroke" if ok else "OUTSIDE usable stroke")
    if ok:
        back = cfg.geometry.forward(lengths) - cfg.calibration.zero_offsets
        print(f"forward check   {_fmt(back)}")
    return 0 if ok else 1


def cmd_motion_info(args: argparse.Namespace) -> int:
    mf = motionfile.read_motion(args.file)
    h = mf.header
    print(f"{args.file}: format {h['format_version']}, programme {mf.programme_id}")
    print(
        f"  duration {mf.duration_s:.3f} s, {mf.sample_count} samples @ {mf.sample_rate_hz:g} Hz,"
        f" compression {h['compression']}"
    )
    print(f"  video sha256 {mf.video_sha256 or '-'}  ({h['video'].get('filename', '')})")
    print(f"  generator {h['generation'].get('generator', '?')} {h['generation'].get('generator_version', '')}")
    from fourdvr_fx.cli import describe
    from fourdvr_fx.embed import effects_from_header

    try:
        doc = effects_from_header(h)
    except ValueError as exc:
        print(f"  effects: DAMAGED section ({exc}) - ignored at show time, the motion still plays")
    else:
        for line in describe(doc) if doc is not None else ["effects: none (motion-only programme)"]:
            print("  " + line)
    s = mf.samples
    dt = 1.0 / mf.sample_rate_hz
    vel = np.diff(s, axis=0) / dt
    acc = np.diff(vel, axis=0) / dt
    for i, axis in enumerate(AXES):
        k, unit = (180 / math.pi, "deg") if i >= 3 else (1000.0, "mm")
        print(
            f"  {axis:6s} min {s[:, i].min() * k:+8.2f} max {s[:, i].max() * k:+8.2f} {unit}  "
            f"|v|max {np.abs(vel[:, i]).max() * k:8.2f} {unit}/s  |a|max {np.abs(acc[:, i]).max() * k:9.1f} {unit}/s²"
        )
    if args.platform:
        cfg = load_platform_config(args.platform)
        lengths = cfg.geometry.leg_lengths_many(s + cfg.calibration.zero_offsets)
        act = cfg.actuators
        bad = int(np.sum(np.any((lengths < act.usable_min()) | (lengths > act.usable_max()), axis=1)))
        over = int(np.sum(np.any((s < cfg.limits.position_min) | (s > cfg.limits.position_max), axis=1)))
        print(f"  against {cfg.platform_id}: {over} samples outside position limits, {bad} outside actuator stroke")
        return 0 if bad == 0 and over == 0 else 1
    return 0


def cmd_play_local(args: argparse.Namespace) -> int:
    """Dry-run a .motion file through the complete runtime pipeline with the simulation driver."""
    from dataclasses import replace

    from .drivers import ActuatorCommand, SimulationDriver
    from .esp32_protocol import CommandState
    from .interpolation import MotionTrack
    from .pipeline import PlatformPipeline
    from .sources import TrajectorySource

    cfg = replace(load_platform_config(args.platform), driver="simulation")
    mf = motionfile.read_motion(args.file)
    track = MotionTrack(mf.samples, mf.sample_rate_hz, mf.duration_s)
    pipe = PlatformPipeline(cfg, args.rate)
    driver = SimulationDriver(cfg)
    driver.open()
    pipe.reset(cfg.calibration.park_pose)
    pipe.transition(TrajectorySource(track, 0.0, mf.duration_s, False, mf.programme_id, 1), 0.0, gentle=True)
    n = int((mf.duration_s + 2.0) * args.rate)
    worst_util = 0.0
    errs = []
    start = time.monotonic()
    for k in range(n):
        t = k / args.rate
        strokes = pipe.step(t)
        driver.send(ActuatorCommand(CommandState.RUN, strokes, t))
        driver.poll(time.monotonic())
        worst_util = max(worst_util, pipe.stroke_utilisation())
        if t > cfg.motion.min_blend_s * 3:
            errs.append(np.abs(pipe.pose - track.sample(t + pipe.dt)))
        if args.realtime:
            time.sleep(max(0.0, start + (k + 1) / args.rate - time.monotonic()))
    err = np.max(np.array(errs), axis=0) if errs else np.zeros(6)
    print(f"played {mf.duration_s:.2f}s at {args.rate:g} Hz through {cfg.platform_id} pipeline (simulation driver)")
    print(f"  max tracking error {_fmt(err, 5)} (m / rad)")
    print(f"  peak stroke utilisation {worst_util * 100:.1f} %")
    print(f"  limit events {pipe.counters.as_dict()}")
    g = driver.device.guard
    print(
        f"  simulated ESP32: received {g.received}, lost {g.seq.lost}, invalid {g.invalid}, stale {g.stale},"
        f" faults {g.faults!r}"
    )
    return 0


def cmd_pose(args: argparse.Namespace) -> int:  # pragma: no cover - drives hardware
    """Commissioning helper: blend to a static pose, hold it, return to neutral."""
    from .drivers import ActuatorCommand, create_driver
    from .esp32_protocol import CommandState
    from .pipeline import PlatformPipeline
    from .sources import ConstantSource

    cfg = load_platform_config(args.platform)
    driver = create_driver(cfg)
    pipe = PlatformPipeline(cfg, 100.0)
    driver.open()
    t0 = time.monotonic()
    while True:
        link = driver.poll(time.monotonic())
        if link.positions_m is not None:
            pipe.reset(pipe.pose_from_strokes(link.positions_m))
            break
        driver.send(ActuatorCommand(CommandState.IDLE, pipe.strokes, time.monotonic()))
        if time.monotonic() - t0 > 5:
            print("no reply from the actuator controller", file=sys.stderr)
            return 2
        time.sleep(0.01)
    plan = [(ConstantSource(args.pose, "pose"), args.hold), (pipe.neutral, 3.0)]
    t = 0.0
    for source, hold in plan:
        duration = pipe.transition(source, t, gentle=True)
        end = t + duration + hold
        while t < end:
            strokes = pipe.step(t)
            driver.send(ActuatorCommand(CommandState.RUN, strokes, time.monotonic()))
            link = driver.poll(time.monotonic())
            if not link.connected:
                print("link lost; the controller firmware will park", file=sys.stderr)
                return 2
            time.sleep(0.01)
            t += 0.01
        print(f"reached {_fmt(pipe.pose)} lengths {_fmt(pipe.lengths, 5)}")
    driver.close()
    return 0


def cmd_esp32_sim(args: argparse.Namespace) -> int:  # pragma: no cover - long-running tool
    from .sim_esp32 import GuardConfig, run_udp_server

    stroke = int(args.stroke_mm * 1000)
    fx_node = None
    if args.dmx_ports or args.audio_channels:
        from fourdvr_fx.bridge import SimNode

        fx_node = SimNode(args.platform_id, args.dmx_ports, args.audio_channels, actuators=True, host="0.0.0.0",
                          port=args.fx_port, keep_history=False).start()
        print(f"effects node on UDP {args.fx_port}: {args.dmx_ports} DMX line(s), {args.audio_channels} audio channel(s)")
    try:
        run_udp_server(
            args.port,
            GuardConfig(
                platform_id=args.platform_id,
                stroke_um=(stroke,) * 6,
                park_um=(stroke // 2,) * 6,
                timeout_ms=args.timeout_ms,
            ),
            verbose=args.verbose,
        )
    except KeyboardInterrupt:  # Ctrl+C / SIGINT ends the bench simulator normally
        pass
    finally:
        if fx_node is not None:
            fx_node.stop()
    return 0


def cmd_run(args: argparse.Namespace) -> int:  # pragma: no cover - process entry point
    from .app import run_controller
    from .config import load_controller_config
    from .logging_setup import setup_logging

    cfg = load_controller_config(args.config)
    setup_logging(cfg.log_level, cfg.log_format)
    run_controller(cfg)
    return 0


def cmd_test_vectors(args: argparse.Namespace) -> int:
    from .esp32_protocol import (
        Command,
        CommandState,
        DeviceState,
        Fault,
        Status,
        cobs_encode,
        encode_command,
        encode_status,
    )

    lines = ["# 4D VR Theatre ESP32 link protocol 1.0 test vectors (generated by fourdvr-motion test-vectors)"]
    cmd = Command(7, 123456, 1_798_000_000_123_456, CommandState.RUN, (0, 1, 150_000, 299_999, 42, 123_456))
    lines.append("command " + encode_command(cmd).hex())
    cmd2 = Command(250, 0xFFFFFFFF, 0, CommandState.PARK, (1, 2, 3, 4, 5, 6), flags=0)
    lines.append("command " + encode_command(cmd2).hex())
    st = Status(
        7,
        123456,
        1_798_000_000_123_456,
        3_600_000,
        DeviceState.RUNNING,
        Fault.TIMEOUT | Fault.STALE,
        (100, 200, 300, 400, 500, 600),
        1000,
        3,
        2,
        1,
        4,
        5,
    )
    lines.append("status " + encode_status(st).hex())
    lines.append(
        "cobs "
        + bytes([0x11, 0x00, 0x22, 0x00, 0x00, 0x33]).hex()
        + " "
        + cobs_encode(bytes([0x11, 0x00, 0x22, 0x00, 0x00, 0x33])).hex()
    )
    text = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")
    return 0


def build_parser(prog: str = "fourdvr-motion") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog, description=f"{PRODUCT_TITLE} {__version__}: show service for motion platforms and 4D effects"
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="run the motion controller")
    s.add_argument("-c", "--config", required=True)
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("check-config", help="validate a controller configuration")
    s.add_argument("-c", "--config", required=True)
    s.set_defaults(func=cmd_check_config)
    s = sub.add_parser("check-platform", help="validate a platform profile and print its workspace")
    s.add_argument("platform")
    s.set_defaults(func=cmd_check_platform)
    s = sub.add_parser("geometry", help="generate [geometry] for a symmetric 6-6 hexapod")
    s.add_argument("--base-radius", type=float, required=True)
    s.add_argument("--platform-radius", type=float, required=True)
    s.add_argument("--base-pair-angle", type=float, default=20.0, help="degrees between the two joints of a base pair")
    s.add_argument("--platform-pair-angle", type=float, default=20.0)
    s.add_argument("--neutral-length", type=float, required=True, help="leg length at the neutral pose (m)")
    s.add_argument("--rotation-centre", default="0,0,0", help="x,y,z in the platform frame (m)")
    s.set_defaults(func=cmd_geometry)
    s = sub.add_parser("ik", help="inverse kinematics for one pose")
    s.add_argument("--platform", required=True)
    s.add_argument("--pose", type=_pose, required=True)
    s.set_defaults(func=cmd_ik)
    s = sub.add_parser("motion-info", help="summarise a .motion file (optionally against a platform)")
    s.add_argument("file")
    s.add_argument("--platform")
    s.set_defaults(func=cmd_motion_info)
    s = sub.add_parser("play-local", help="dry-run a .motion file through the pipeline (simulation driver)")
    s.add_argument("file")
    s.add_argument("--platform", required=True)
    s.add_argument("--rate", type=float, default=100.0)
    s.add_argument("--realtime", action="store_true")
    s.set_defaults(func=cmd_play_local)
    s = sub.add_parser("pose", help="commissioning: move to a pose, hold, return to neutral")
    s.add_argument("--platform", required=True)
    s.add_argument("--pose", type=_pose, required=True)
    s.add_argument("--hold", type=float, default=5.0)
    s.set_defaults(func=cmd_pose)
    s = sub.add_parser("esp32-sim", help="simulated ESP32 bridge on UDP (bench testing)")
    s.add_argument("--port", type=int, default=47000)
    s.add_argument("--platform-id", type=int, default=1)
    s.add_argument("--stroke-mm", type=float, default=300.0)
    s.add_argument("--timeout-ms", type=int, default=200)
    s.add_argument("--verbose", action="store_true")
    s.add_argument("--dmx-ports", type=int, default=0, choices=(0, 1, 2), help="also simulate an effects node with this many DMX lines")
    s.add_argument("--audio-channels", type=int, default=0, choices=(0, 1, 2), help="... and an audio output with this many channels")
    s.add_argument("--fx-port", type=int, default=47002, help="UDP port of the simulated effects node (default %(default)s)")
    s.set_defaults(func=cmd_esp32_sim)
    s = sub.add_parser("test-vectors", help="write ESP32 protocol test vectors")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_test_vectors)
    return p


def main(argv: list[str] | None = None, prog: str = "fourdvr-motion") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        code = int(args.func(args))
        sys.stdout.flush()                    # inside the try: a reader that has gone shows here
        return code
    except BrokenPipeError:
        from fourdvr_studio import reader_gone

        return reader_gone()
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
