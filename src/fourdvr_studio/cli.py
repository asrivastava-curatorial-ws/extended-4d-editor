"""``fourdvr`` - the one command of Extended 4D Editor.

    fourdvr [FILE]              open the Editor (desktop application); FILE = project, video or .motion
    fourdvr extract ...         motion extraction on the command line        (= fourdvr-extract)
    fourdvr controller ...      show service for the motion platforms + effects, platform tools
                                                                             (= fourdvr-motion)
    fourdvr fx ...              effects files, venue DMX patch, live DMX tools

Every part is imported only when it is used: the show service (``fourdvr controller run``) never
loads the desktop toolkit, video analysis or the editor.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import PRODUCT_TITLE, __version__, reader_gone

USAGE = f"""{PRODUCT_TITLE} {__version__}

usage: fourdvr [FILE]                 open the Editor; FILE is a project (.4dvr), a video or a .motion file
       fourdvr extract COMMAND ...    video -> .motion on the command line (info, generate, validate, ...)
       fourdvr controller COMMAND ... show service: run, check-config, check-platform, play-local, ...
       fourdvr fx COMMAND ...         effects: info, embed, check-patch, check, play, fire, monitor, ...
       fourdvr version

'fourdvr <part> --help' lists the commands of a part. The former commands still work:
fourdvr-extract = 'fourdvr extract', fourdvr-motion = 'fourdvr controller'.
"""

STUDIO = ("studio", "gui")
CONTROLLER = ("controller", "motion", "show")
EXTRACT = ("extract", "extractor")
FX = ("fx", "effects")


NEEDS_STUDIO = (
    "error: {what} needs the workstation packages, which are not installed here ({exc}).\n"
    "       Install them with:  pip install 'extended-4d-editor[studio]'   (the show service and 'fourdvr fx' work without them)"
)


def _studio(argv: list[str]) -> int:
    try:
        from .gui.main_window import main as gui_main
    except ImportError as exc:  # a show PC: installed without the desktop packages
        print(NEEDS_STUDIO.format(what="the Editor window", exc=exc), file=sys.stderr)
        return 2
    return gui_main(["fourdvr", *argv])


def _extract(argv: list[str], prog: str) -> int:
    try:
        from fourdvr_extractor.cli import main as extract_main
    except ImportError as exc:
        print(NEEDS_STUDIO.format(what="motion extraction", exc=exc), file=sys.stderr)
        return 2
    return extract_main(argv, prog=prog)


def extract_entry() -> int:
    """The former `fourdvr-extract` command."""
    return _extract(sys.argv[1:], "fourdvr-extract")


def studio_entry() -> int:
    """The desktop launcher (`extended-4d-editor`; formerly `fourdvr-studio` and `fourdvr-extractor-gui`)."""
    return _studio(sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    try:
        code = _dispatch(argv)
        sys.stdout.flush()                        # inside the try: a reader that has gone (| head) shows here
        return code
    except BrokenPipeError:
        return reader_gone()


def _dispatch(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _studio([])
    head, rest = args[0], args[1:]
    if head in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if head in ("-V", "--version", "version"):
        print(__version__)
        return 0
    if head in STUDIO:
        return _studio(rest)
    if head in CONTROLLER:
        from fourdvr_motion.cli import main as controller_main

        return controller_main(rest, prog=f"fourdvr {head}")
    if head in EXTRACT:
        return _extract(rest, f"fourdvr {head}")
    if head in FX:
        from fourdvr_fx.cli import main as fx_main

        return fx_main(rest, prog=f"fourdvr {head}")
    if not head.startswith("-") and Path(head).exists():
        return _studio(args)
    print(f"fourdvr: unknown command or file '{head}'\n\n{USAGE}", end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
