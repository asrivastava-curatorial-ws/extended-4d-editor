"""Extended 4D Editor: one program for authoring (motion extraction, scene detection, the timeline for
motion, vibration and effects) and for the show-time controller (synchronised motion and effects playback)."""

__version__ = "3.3.0"
PRODUCT_NAME = "Extended-4D-Editor"         # in file names and in the files this program writes
PRODUCT_TITLE = "Extended 4D Editor"        # as people read it


def frozen() -> bool:
    """True in a build made with PyInstaller (the Windows installer): one program file, no Python beside it."""
    import sys

    return bool(getattr(sys, "frozen", False))


def self_command() -> list[str]:
    """The command that starts this program again - for the parts that run as a process of their own
    (a live show's engine, the stream server). Follow it with the arguments, e.g. ``"live", "run"``."""
    import sys
    from pathlib import Path

    if frozen():                                # the command-line program lies next to the window's program
        exe = Path(sys.executable)
        console = exe.with_name("fourdvr.exe" if exe.suffix.lower() == ".exe" else "fourdvr")
        return [str(console if console.exists() else exe)]
    return [sys.executable, *(["-I"] if sys.flags.isolated else []), "-m", "fourdvr_studio"]


def child_flags() -> int:
    """``creationflags`` for ``subprocess.Popen`` of such a process: on Windows it gets no console window of
    its own (elsewhere there is nothing to ask for: 0)."""
    import subprocess
    import sys

    if sys.platform == "win32":
        return int(subprocess.CREATE_NO_WINDOW)
    return 0


def setup_console() -> None:
    """Make writing to the terminal safe everywhere. A program without a console (the window's program
    on Windows) has no output streams at all: it gets ones that go nowhere. On Windows, text goes out as
    UTF-8 whatever the console's code page is - a quotation mark or an arrow must not end the program -
    and the console is told to understand the cursor movements of the status lines."""
    import os
    import sys

    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        for which in (-11, -12):                # the standard output and error handles
            handle = kernel.GetStdHandle(which)
            mode = ctypes.c_uint32()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)      # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError, ValueError):
        pass


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
