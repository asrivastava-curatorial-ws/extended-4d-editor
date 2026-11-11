"""Command-line interface: fourdvr-extract <command>."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from fourdvr_motion import motionfile
from fourdvr_motion.platform_config import load_platform_config

from . import PRODUCT_TITLE, __version__
from .pipeline import DEFAULT_PROFILE, ExtractionSettings, load_settings, save_settings
from .validation import validate
from .video import probe


def _progress(quiet: bool):  # type: ignore[no-untyped-def]
    last = [0.0]

    def report(fraction: float, message: str) -> None:
        if quiet:
            return
        now = time.monotonic()
        if now - last[0] > 1.0 or fraction >= 1.0:
            last[0] = now
            print(f"  [{fraction * 100:5.1f}%] {message}", file=sys.stderr)

    return report


def _settings(args: argparse.Namespace) -> ExtractionSettings:
    s = load_settings(args.settings) if getattr(args, "settings", None) else ExtractionSettings()
    if getattr(args, "programme_id", None) is not None:
        s.programme_id = args.programme_id
    if getattr(args, "projection", None):
        s.projection = args.projection
    if getattr(args, "stereo", None):
        s.stereo = args.stereo
    if getattr(args, "hfov", None):
        s.hfov_deg = args.hfov
    if getattr(args, "rate", None):
        s.sample_rate_hz = args.rate
    if getattr(args, "gain", None) is not None:
        s.cueing.global_gain = args.gain
    s.check()
    return s


def cmd_info(args: argparse.Namespace) -> int:
    info = probe(args.video, with_hash=not args.no_hash)
    print(f"{args.video}")
    print(f"  {info.width}x{info.height} @ {info.fps:.3f} fps, {info.frame_count} frames, {info.duration_s:.3f} s")
    print(f"  guessed format: projection={info.projection} stereo={info.stereo} (confirm with --projection/--stereo)")
    if info.sha256:
        print(f"  sha256 {info.sha256}")
    return 0


def _print_report(report) -> None:  # type: ignore[no-untyped-def]
    width = max(len(r[0]) for r in report.rows())
    for name, value, limit, status in report.rows():
        print(f"  {name:{width}s}  {value:>16s}  limit {limit:>16s}  {status}")
    print(f"validation {'PASSED' if report.passed else 'FAILED'} against profile '{report.profile}'")
    for v in report.violations:
        print(f"  ! {v}")
    print("  note: a passed check is not a passenger-safety certification (see README.md)")


def _prepare_output(path: Path) -> None:
    """Create the output's directory and check that it is writable *before* the long analysis."""
    if path.is_dir():
        raise ValueError(f"output {path} is a directory; give a file name, e.g. {path / 'programme.motion'}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(path.parent, os.W_OK):
        raise PermissionError(f"output directory {path.parent} is not writable")


def cmd_generate(args: argparse.Namespace) -> int:
    from fourdvr_fx.embed import read_effects
    from fourdvr_studio.project import StudioProject

    settings = _settings(args)
    studio = StudioProject()
    project = studio.extraction
    project.settings = settings
    if args.profile:
        studio.load_profile(args.profile)
    want_sound = bool(args.bass or args.effects == "suggest")
    studio.bass_wanted = bool(args.bass)
    if args.bass:                                 # asked for by name: say so now, not after the analysis
        from .soundtrack import NO_DECODER, available_decoder

        if not available_decoder():
            raise ValueError(f"--bass: {NO_DECODER}")
    start = time.monotonic()
    out = Path(args.output or Path(args.video).with_suffix(".motion"))
    _prepare_output(out)
    if args.report:
        _prepare_output(Path(args.report))
    print(f"{PRODUCT_TITLE} {__version__}: {args.video} -> {out}")
    studio.open_video(args.video)
    info = project.video
    assert info is not None
    print(
        f"  {info.width}x{info.height} {info.fps:.2f} fps {info.duration_s:.2f} s, {info.projection}/{info.stereo}, sha256 {info.sha256[:16]}…"
    )
    imported = None
    if args.fx:                                   # check the effects file before the long analysis
        imported = read_effects(args.fx)
        if imported is None:
            raise ValueError(f"{args.fx} has no effects")
        if abs(imported.duration_s - info.duration_s) > 0.5:
            raise ValueError(f"{args.fx} is {imported.duration_s:.2f} s long, the video {info.duration_s:.2f} s")
    if want_sound:
        studio.detect_scenes = args.effects == "suggest" and not args.no_scenes
        studio.analyse(_progress(args.quiet))     # camera motion, the sound track and - for suggestions - the scenes
    else:
        project.analyse(_progress(args.quiet))
    project.generate()
    project.process()
    report = project.validate()
    _print_report(report)
    if not report.passed and not args.allow_violations:
        print("not exported (use --allow-violations to export anyway for testing)", file=sys.stderr)
        return 1
    if imported is not None:
        imported.duration_s = info.duration_s
        imported.bass = imported.bass if imported.bass is not None else studio.effects.bass
        studio.effects = imported
    if args.effects == "suggest":
        studio.suggest()
        if studio.scene is not None:
            found = studio.scene.summary()

            def listing(seconds: object) -> str:
                assert isinstance(seconds, dict)
                return ", ".join(f"{k} {v:g} s" for k, v in sorted(seconds.items(), key=lambda kv: -kv[1])) or "nothing"

            print(f"  scenes: seen {listing(found['seconds_seen'])}; heard {listing(found['seconds_heard']) if found['sound'] else 'no sound'}"
                  f" ({found['pictures']} pictures looked at)")
            if studio.scene.note:
                print(f"  note: {studio.scene.note}")
        elif not args.no_scenes:
            print(f"  note: {studio.scene_note or 'scenes were not looked for'} - no rain, fog, heat, bubbles, snow, water or scent suggestions")
    if want_sound and studio.soundtrack is None:
        print(f"  note: {studio.soundtrack_note or 'no sound track'} - no bass track, no sound-based suggestions")
    sha = studio.export(out, allow_violations=args.allow_violations)
    ev = project.events
    assert ev is not None
    classes = ", ".join(f"{k} {v * 100:.0f}%" for k, v in ev.class_fractions().items() if v > 0)
    print(f"  classes: {classes}; events: {len(ev.events)}; impacts: {len(ev.impacts)}")
    doc = studio.effects_for_export()
    if doc is None:
        print("  effects: none (motion-only programme)")
    else:
        from fourdvr_fx.cli import describe

        for line in describe(doc):
            print("  " + line)
    print(
        f"wrote {out} (programme {settings.programme_id}, {settings.sample_rate_hz:g} Hz, sha256 {sha}) in {time.monotonic() - start:.1f} s"
    )
    if args.report:
        Path(args.report).write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    mf = motionfile.read_motion(args.file)
    profile = load_platform_config(args.profile or DEFAULT_PROFILE)
    report = validate(mf.samples, mf.sample_rate_hz, profile)
    print(f"{args.file}: programme {mf.programme_id}, {mf.duration_s:.2f} s @ {mf.sample_rate_hz:g} Hz")
    _print_report(report)
    return 0 if report.passed else 1


def cmd_settings(args: argparse.Namespace) -> int:
    save_settings(ExtractionSettings(), args.output)
    print(f"default settings written to {args.output}")
    return 0


def cmd_profiles(_args: argparse.Namespace) -> int:
    folder = DEFAULT_PROFILE.parent
    for p in sorted(folder.glob("*.toml")):
        cfg = load_platform_config(p)
        lims = ", ".join(
            f"{a} ±{min(abs(lo), hi) * (180 / math.pi if i >= 3 else 1000):.0f}{'°' if i >= 3 else 'mm'}"
            for i, (a, lo, hi) in enumerate(
                zip(
                    ("surge", "sway", "heave", "roll", "pitch", "yaw"),
                    cfg.limits.position_min,
                    cfg.limits.position_max,
                    strict=True,
                )
            )
        )
        print(f"{p}\n  {cfg.name}: {lims}")
    return 0


def build_parser(prog: str = "fourdvr-extract") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog, description=f"{PRODUCT_TITLE} {__version__}: video to motion, vibration and effects - offline, nothing leaves this computer"
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info", help="show video metadata and the guessed VR format")
    s.add_argument("video")
    s.add_argument("--no-hash", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("generate", help="analyse a video and export a validated .motion file")
    s.add_argument("video")
    s.add_argument("-o", "--output")
    s.add_argument("--programme-id", type=int)
    s.add_argument("--projection", choices=("flat", "180", "360"))
    s.add_argument("--stereo", choices=("mono", "sbs", "ou"))
    s.add_argument("--hfov", type=float, help="horizontal field of view of flat video (degrees)")
    s.add_argument("--rate", type=float, help="motion sample rate 50-200 Hz (default 100)")
    s.add_argument("--gain", type=float, help="global cue gain (default 1.0)")
    s.add_argument("--profile", help="platform profile TOML (same file as the Motion Controller)")
    s.add_argument("--settings", help="settings JSON (see: fourdvr-extract settings)")
    s.add_argument("--report", help="write the validation report as JSON")
    s.add_argument("--allow-violations", action="store_true", help="export even if validation fails (testing only)")
    s.add_argument("--effects", choices=("none", "suggest"), default="none",
                   help="suggest: draft effect tracks - wind, vibration, air blasts and strobe from the camera motion and the sound; "
                        "rain, fog, heat, bubbles, snow, water and scent from the scenes in the picture and the sound "
                        "(default: motion only)")
    s.add_argument("--no-scenes", action="store_true",
                   help="with --effects suggest: do not look for rain, fog, fire, bubbles, snow and scent scenes (faster)")
    s.add_argument("--bass", action="store_true", help="embed the sound track's bass for subwoofers / bass shakers")
    s.add_argument("--fx", help="embed this effects file (.fx.json, or a .motion file to copy the effects from)")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("validate", help="validate an existing .motion file against a platform profile")
    s.add_argument("file")
    s.add_argument("--profile")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("settings", help="write the default settings JSON for editing")
    s.add_argument("output")
    s.set_defaults(func=cmd_settings)

    s = sub.add_parser("profiles", help="list bundled platform profiles")
    s.set_defaults(func=cmd_profiles)
    return p


def main(argv: list[str] | None = None, prog: str = "fourdvr-extract") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        code = int(args.func(args))
        sys.stdout.flush()                    # inside the try: a reader that has gone shows here
        return code
    except BrokenPipeError:
        from fourdvr_studio import reader_gone

        return reader_gone()
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
