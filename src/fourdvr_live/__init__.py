"""Live mode: motion, effects and bass from a running game or a live picture, not from a file.

A *source* (game telemetry, a captured screen, captured sound) delivers :class:`LiveSample` values;
the *cueing* turns them into platform motion in real time; *rules* turn them into effect levels; the
platforms, DMX devices and shakers are driven through the same limits as a recorded programme.
A session can be recorded and opened in the Editor to make a ``.motion`` programme of it.

Only :mod:`numpy` is needed for telemetry sources - a show PC without the workstation packages can
run them. Screen capture needs OpenCV (and the desktop toolkit for grabbing the screen).
"""

from fourdvr_motion import __version__

__all__ = ["__version__"]
