"""Smoke test of the desktop GUI (runs with the Qt offscreen platform, no display needed)."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():  # type: ignore[no-untyped-def]
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _wait(app, cond, timeout: float = 60.0) -> None:  # type: ignore[no-untyped-def]
    start = time.time()
    while not cond() and time.time() - start < timeout:
        app.processEvents()
        time.sleep(0.01)
    assert cond(), "timed out"


def test_gui_full_workflow(app, videos, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from fourdvr_extractor import motionfile
    from fourdvr_extractor.gui.app import MainWindow

    win = MainWindow()
    win.resize(1280, 800)
    win.show()
    win.open_video(str(videos["yaw360"]))
    _wait(app, lambda: win.project.video is not None and win._thread is None)
    assert win.in_proj.currentText() == "360" and win.btn_analyse.isEnabled()
    win.start_analysis()
    _wait(app, lambda: win.project.analysis is not None and win._thread is None)
    assert win._guarded(win.do_generate) and win._guarded(win.do_process) and win._guarded(win.do_validate)
    assert win.table.rowCount() > 20 and "PASSED" in win.val_summary.text()
    assert len(win.curves["rot"][2].getData()[0]) == len(win.project.motion)
    win.seek(1.5)
    app.processEvents()
    assert "pose" in win.readout.text()
    image = win.view3d.grab().toImage()
    colours = {image.pixelColor(x, y).name() for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)}
    assert len(colours) > 5  # the platform was drawn
    win.toggle_play()
    for _ in range(5):
        win._tick()
    win.toggle_play()
    assert win.current_t > 1.5
    out = tmp_path / "gui.motion"
    assert win._guarded(lambda: win._export(str(out)))
    assert motionfile.read_motion(out).sample_count == len(win.project.motion)
    assert np.allclose(motionfile.read_motion(out).samples, win.project.motion, atol=1e-6)
    win.close()
