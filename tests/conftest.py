"""For every test run."""

from __future__ import annotations

from typing import Any


def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    """Destroy the Qt objects the tests left behind - here, one after the other.

    Left to the end of the process, PySide destroys what Python still owns in no particular order, and
    with the windows of a whole test run that has ended in a crash after every test had passed. The
    application does the same for its own windows when it ends (fourdvr_studio.gui.theme.dispose)."""
    try:
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QObject, QThread
    except ImportError:
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    for obj in shiboken6.getAllValidWrappers():
        if not isinstance(obj, QObject) or obj is app or not shiboken6.isValid(obj) or not shiboken6.ownedByPython(obj):
            continue
        if isinstance(obj, QThread) and obj.isRunning():
            continue
        shiboken6.delete(obj)
