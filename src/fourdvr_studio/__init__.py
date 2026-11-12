"""Extended 4D Editor: one program for authoring (motion extraction, scene detection, the timeline for
motion, vibration and effects) and for the show-time controller (synchronised motion and effects playback)."""

__version__ = "3.2.0"
PRODUCT_NAME = "Extended-4D-Editor"         # in file names and in the files this program writes
PRODUCT_TITLE = "Extended 4D Editor"        # as people read it


def reader_gone() -> int:
    """For the command-line entry points: whoever was reading the output (`head`, `grep -q`, a closed
    terminal) has stopped reading. That is not an error: end quietly, and point the standard output
    at nothing so that the interpreter's own shutdown does not complain either."""
    import os
    import sys

    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    return 0
