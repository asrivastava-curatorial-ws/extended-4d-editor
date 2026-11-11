"""The former Motion Extractor window is the Editor window now (same workflow, plus effects and live output).

This module keeps the old import path and the ``fourdvr-extractor-gui`` command working.
"""

from fourdvr_studio.gui.main_window import MainWindow, StudioWindow, Worker, main

__all__ = ["MainWindow", "StudioWindow", "Worker", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
