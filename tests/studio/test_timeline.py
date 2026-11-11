"""The timeline widget on its own: geometry, snapping, selection and every editing gesture."""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from fourdvr_fx import Cue, EffectsDocument
from fourdvr_studio.gui import theme
from fourdvr_studio.gui.timeline import HEADER_W, PAD, RULER_H, Lane, TimelineCanvas, TimelinePanel

LEFT = Qt.MouseButton.LeftButton
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


@pytest.fixture(scope="module")
def app():  # type: ignore[no-untyped-def]
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


def send(tl: TimelineCanvas, kind: str, x: float, y: float, mods=NONE, button=LEFT) -> None:  # type: ignore[no-untyped-def]
    etype = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove, "release": QEvent.Type.MouseButtonRelease,
             "double": QEvent.Type.MouseButtonDblClick}[kind]
    pos = QPointF(x, y)
    buttons = Qt.MouseButton.NoButton if kind == "release" else button
    QApplication.sendEvent(tl, QMouseEvent(etype, pos, tl.mapToGlobal(pos), button, buttons, mods))


def drag(tl: TimelineCanvas, x0: float, y0: float, x1: float, y1: float, mods=NONE, steps: int = 6) -> None:  # type: ignore[no-untyped-def]
    send(tl, "press", x0, y0, mods)
    for k in range(1, steps + 1):
        send(tl, "move", x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps, mods)
    send(tl, "release", x1, y1, mods)


class Rig:
    """A canvas with one effect document: water (cues), wind (curve), and a manual-motion lane."""

    def __init__(self) -> None:
        self.doc = EffectsDocument(20.0)
        self.water = self.doc.new_track("water")
        self.wind = self.doc.new_track("wind")
        self.heave: list[tuple[float, float]] = []
        self.panel = TimelinePanel()
        self.tl = self.panel.canvas
        self.panel.resize(HEADER_W + 1024, 320)          # 1000 px for 20 s after the 24 px margin: 50 px per second
        self.panel.show()
        self.lanes = [
            Lane("view:translation", "Translation", theme.FG_MUTED, "view", "Reference", 30, view="translation"),
            Lane("motion:heave", "Heave", theme.ATTENTION, "curve", "Manual motion", 50, points=self.heave, lo=-1.0, hi=1.0, eased=True),
            Lane("fx:water", "Water", "#39c5cf", "fx", "Effects", 50, track=self.water, add="cue"),
            Lane("fx:wind", "Wind", "#58a6ff", "fx", "Effects", 50, track=self.wind, add="point"),
        ]
        self.tl.set_lanes(self.lanes)
        self.tl.set_duration(20.0, 1 / 25)
        self.tl.set_reference(np.zeros((2001, 6)), 100.0, np.ones(6), True, None, [5.0], [12.0])
        self.started, self.changed, self.seeks = [], [], []
        self.tl.editStarted.connect(lambda: self.started.append(1))
        self.tl.edited.connect(lambda: self.changed.append(1))
        self.tl.seekRequested.connect(self.seeks.append)
        QApplication.processEvents()

    def x(self, t: float) -> float:
        return self.tl.x_of(t)

    def mid(self, key: str) -> float:
        lane = self.tl.lane(key)
        return self.tl._top_of(lane) + lane.height / 2

    def top(self, key: str) -> float:
        return self.tl._top_of(self.tl.lane(key))


@pytest.fixture
def rig(app) -> Rig:  # type: ignore[no-untyped-def]
    return Rig()


def test_geometry_zoom_scroll_and_painting(rig: Rig) -> None:
    tl = rig.tl
    assert tl.pps == pytest.approx(50.0) and tl.t0 == 0.0 and tl.x_of(2.0) == HEADER_W + 100 and tl.t_of(HEADER_W + 100) == 2.0
    lane, top = tl._lane_at(rig.mid("fx:water"))
    assert lane is tl.lane("fx:water") and tl._lane_at(5)[0] is None
    assert tl._v_of(lane, top, top + PAD) == 1.0 and tl._v_of(lane, top, top + lane.height - PAD) == 0.0
    assert tl._y_of(lane, top, 0.5) == pytest.approx(top + lane.height / 2)
    tl.zoom(2.0, tl.x_of(10.0))
    assert tl.pps == pytest.approx(100.0) and tl.x_of(10.0) == pytest.approx(HEADER_W + 500)      # the anchor stays put
    assert rig.panel.hbar.maximum() > 0 and rig.panel.hbar.value() == int(tl.t0 * tl.pps)
    rig.panel.hbar.setValue(0)
    assert tl.t0 == 0.0
    tl.zoom(0.01)
    assert tl.pps == pytest.approx(50.0)                                                            # never smaller than the whole programme
    tl.zoom(1000.0)
    assert tl.pps == 600.0
    tl.zoom_fit()
    rig.water.cues = [Cue(2.0, 1.0, 0.8, 0.2, 0.3), Cue(6.0, 0.05)]
    rig.wind.points = [(0.0, 0.0), (5.0, 1.0), (20.0, 0.2)]
    rig.heave.extend([(0.0, 0.0), (3.0, -0.5), (6.0, 0.0)])
    tl.select_cue(rig.water.cues[0])
    tl.set_held({"water": [(2.5, 3.0, "maximum on-time")]})
    tl.set_playhead(4.0)
    image = rig.panel.grab().toImage()
    colours = {image.pixelColor(x, y).name() for x in range(0, image.width(), 5) for y in range(0, image.height(), 3)}
    assert {"#39c5cf", "#58a6ff", theme.DANGER} <= colours or len(colours) > 25                     # cues, curve, limit mark drawn
    assert tl.lane("motion:heave").value_at(1.5) == pytest.approx(-0.25) and tl.lane("fx:wind").value_at(2.5) == pytest.approx(0.5)
    assert tl.lane("motion:heave").format(-0.25) == "-25 %" and tl.lane("fx:wind").format(0.5) == "50 %"


def test_ruler_scrub_click_and_double_click_add(rig: Rig) -> None:
    tl = rig.tl
    drag(tl, rig.x(3.0), 10, rig.x(7.0), 10)
    assert rig.seeks[0] == pytest.approx(3.0) and rig.seeks[-1] == pytest.approx(7.0) and not rig.changed
    send(tl, "press", rig.x(9.0), rig.mid("fx:water"))
    send(tl, "release", rig.x(9.0), rig.mid("fx:water"))
    assert rig.seeks[-1] == pytest.approx(9.0) and tl.current_lane == "fx:water"                   # a click moves the playhead
    send(tl, "double", rig.x(4.0), rig.mid("fx:water"))
    cue = rig.water.cues[0]
    assert (cue.t, cue.duration, cue.level) == (4.0, 0.4, 1.0) and tl.sel_cues == [cue] and len(rig.started) == len(rig.changed) == 1
    send(tl, "double", rig.x(4.2), rig.mid("fx:water"))                                            # on an existing cue: nothing new
    assert len(rig.water.cues) == 1
    send(tl, "double", rig.x(8.0), rig.top("fx:water") + PAD, ALT)                                 # Alt: the other kind (a curve point)
    assert rig.water.points == [(0.0, 0.0), (8.0, 1.0), (20.0, 0.0)]
    send(tl, "double", rig.x(10.0), rig.mid("fx:wind"))                                            # curve lane: a point
    assert (10.0, 0.5) in [(t, round(v, 2)) for t, v in rig.wind.points]
    send(tl, "double", rig.x(6.0), rig.top("motion:heave") + PAD)                                  # empty motion lane: ends are pinned
    assert rig.heave == [(0.0, 0.0), (6.0, 1.0), (20.0, 0.0)]
    send(tl, "double", rig.x(3.0), rig.mid("view:translation"))
    assert rig.seeks[-1] == pytest.approx(3.0)
    scent = rig.doc.new_track("scent")
    tl.set_lanes([*rig.lanes, Lane("fx:scent", "Scent", "#bc8cff", "fx", "Effects", 50, track=scent)])
    assert tl.add_cue(tl.lane("fx:scent"), 19.9).params == {"slot": 1} and scent.cues[0].end <= 20.0   # explicit default parameter


def test_cue_move_resize_level_and_fades(rig: Rig) -> None:
    tl = rig.tl
    rig.water.cues = [Cue(4.0, 2.0), Cue(10.0, 1.0)]
    a, b = rig.water.cues
    y = rig.mid("fx:water")
    tl.snap = False
    drag(tl, rig.x(5.0), y, rig.x(6.5), y)                                                         # body: move
    assert a.t == pytest.approx(5.5) and a.duration == 2.0 and len(rig.started) == 1 and len(rig.changed) == 1
    drag(tl, rig.x(7.5) - 1, y, rig.x(8.5) - 1, y)                                                 # right edge: longer
    assert a.duration == pytest.approx(3.0) and a.t == pytest.approx(5.5)
    drag(tl, rig.x(5.5) + 1, y, rig.x(6.0) + 1, y)                                                 # left edge: later start, same end
    assert a.t == pytest.approx(6.0) and a.end == pytest.approx(8.5)
    drag(tl, rig.x(6.0) + 1, y, rig.x(9.5), y)                                                     # cannot be squeezed to nothing
    assert a.duration == pytest.approx(0.04) and a.end == pytest.approx(8.5)
    a.t, a.duration = 4.0, 2.0
    tl.refresh()
    top = rig.top("fx:water")
    usable = 50 - 2 * PAD
    drag(tl, rig.x(5.0), top + PAD, rig.x(5.0), top + PAD + usable / 2)                            # top centre: level
    assert a.level == pytest.approx(0.5, abs=0.02)
    y_top = top + PAD + usable / 2
    drag(tl, rig.x(4.0), y_top, rig.x(4.5), y_top)                                                 # top left: fade in
    assert a.fade_in == pytest.approx(0.5, abs=0.03) and a.t == 4.0
    drag(tl, rig.x(6.0), y_top, rig.x(5.0), y_top)                                                 # top right: fade out
    assert a.fade_out == pytest.approx(1.0, abs=0.03) and a.duration == 2.0
    drag(tl, rig.x(4.5), y_top, rig.x(9.0), y_top)                                                 # fades never overlap
    assert a.fade_in == pytest.approx(1.0, abs=0.03)
    # two cues move together and stop at the ends of the programme
    send(tl, "press", rig.x(10.5), y, CTRL)
    send(tl, "release", rig.x(10.5), y, CTRL)
    assert len(tl.sel_cues) == 2
    drag(tl, rig.x(10.5), y, rig.x(30.0), y)
    assert b.end == pytest.approx(20.0) and a.t == pytest.approx(13.0)
    drag(tl, rig.x(13.5), y, rig.x(-50.0), y)
    assert a.t == 0.0 and b.t == pytest.approx(6.0)
    assert len(rig.started) == len(rig.changed)


def test_snapping_to_playhead_cuts_cues_and_frames(rig: Rig) -> None:
    tl = rig.tl
    rig.water.cues = [Cue(2.0, 1.0), Cue(8.0, 1.0)]
    a, _b = rig.water.cues
    y = rig.mid("fx:water")
    tl.set_playhead(15.0)
    assert tl.snap_time(14.93) == 15.0 and tl.snap_time(5.08) == 5.0 and tl.snap_time(12.1) == 12.0   # playhead, cut, impact
    assert tl.snap_time(7.9) == 8.0 and tl.snap_time(7.9, skip={id(rig.water.cues[1])}) == 7.9        # other cues' edges
    assert tl.snap_time(14.93, enabled=False) == 14.93 and tl.snap_time(-3) == 0.0 and tl.snap_time(99) == 20.0
    drag(tl, rig.x(2.5), y, rig.x(7.4), y)                                                         # its END catches the next cue's start
    assert a.end == pytest.approx(8.0) and a.t == pytest.approx(7.0)
    drag(tl, rig.x(7.5), y, rig.x(4.4), y, ALT)                                                    # Alt: free
    assert a.t == pytest.approx(3.9, abs=0.01)
    tl.zoom(8.0, rig.x(4.0))                                                                       # 400 px/s: 25 fps frames are 16 px
    assert tl.snap_time(4.013) == pytest.approx(4.0) and tl.snap_time(4.03) == pytest.approx(4.04)
    QTest.keyClick(tl, Qt.Key.Key_S)
    assert tl.snap is False and tl.snap_time(4.013) == 4.013


def test_points_drag_fences_and_rubber_band(rig: Rig) -> None:
    tl = rig.tl
    tl.snap = False
    rig.wind.points = [(0.0, 0.0), (4.0, 0.5), (8.0, 0.5), (12.0, 0.5), (20.0, 0.0)]
    lane = tl.lane("fx:wind")
    top = rig.top("fx:wind")
    y = tl._y_of(lane, top, 0.5)
    drag(tl, rig.x(8.0), y, rig.x(9.0), top + PAD)                                                 # one point: time and value
    assert rig.wind.points[2] == pytest.approx((9.0, 1.0), abs=0.02)
    drag(tl, rig.x(9.0), top + PAD, rig.x(18.0), top + PAD)                                        # cannot pass its neighbour
    assert rig.wind.points[2][0] == pytest.approx(11.99) and [p[0] for p in rig.wind.points] == sorted(p[0] for p in rig.wind.points)
    drag(tl, rig.x(4.0), y, rig.x(6.0), y + 30, SHIFT)                                             # Shift: the dominant direction only
    assert rig.wind.points[1] == pytest.approx((6.0, 0.5))
    # rubber band over the middle three points, then move them together
    drag(tl, rig.x(5.0), top + 1, rig.x(13.0), top + 49)
    assert tl.sel_points == {"fx:wind": [1, 2, 3]} and not tl.sel_cues
    before = list(rig.wind.points)
    drag(tl, rig.x(6.0), y, rig.x(4.0), y)
    assert [round(p[0] - q[0], 3) for p, q in zip(rig.wind.points[1:4], before[1:4], strict=True)] == [-2.0, -2.0, -2.0]
    drag(tl, rig.x(4.0), y, rig.x(-20.0), y)                                                       # ... as a group up to the first point
    assert rig.wind.points[1][0] == pytest.approx(0.01) and rig.wind.points[3][0] == pytest.approx(6.0, abs=0.02)
    # a band across lanes takes cues and points; Ctrl adds to the selection
    rig.water.cues = [Cue(1.0, 1.0), Cue(15.0, 1.0)]
    tl.refresh()
    drag(tl, rig.x(0.5), rig.top("fx:water") + 2, rig.x(6.5), top + 48)
    assert len(tl.sel_cues) == 1 and tl.sel_points == {"fx:wind": [2, 3]}
    drag(tl, rig.x(14.0), rig.top("fx:water") + 2, rig.x(17.0), rig.top("fx:water") + 40, CTRL)
    assert len(tl.sel_cues) == 2
    QTest.keyClick(tl, Qt.Key.Key_Escape)
    assert not tl.has_selection()


def test_keyboard_clipboard_nudge_and_delete(rig: Rig) -> None:
    tl = rig.tl
    rig.water.cues = [Cue(2.0, 1.0, 0.6, params={"x": 1.0}), Cue(5.0, 0.5)]
    rig.wind.points = [(0.0, 0.0), (3.0, 0.4), (20.0, 0.0)]
    tl.current_lane = "fx:water"
    tl.refresh()
    QTest.keyClick(tl, Qt.Key.Key_A, CTRL)
    assert len(tl.sel_cues) == 2 and not tl.sel_points                                             # the current lane ...
    QTest.keyClick(tl, Qt.Key.Key_A, CTRL)
    assert tl.sel_points == {"fx:wind": [0, 1, 2]}                                                 # ... then everything
    tl.select_cue(rig.water.cues[0])
    tl.sel_cues = [rig.water.cues[0]]
    tl.sel_points = {"fx:wind": [1]}
    assert tl.copy_selection() == 2
    tl.set_playhead(10.0)
    QTest.keyClick(tl, Qt.Key.Key_V, CTRL)
    assert [(c.t, c.duration, c.level, c.params) for c in rig.water.cues][-1] == (10.0, 1.0, 0.6, {"x": 1.0})
    assert (11.0, 0.4) in rig.wind.points and tl.sel_cues == [rig.water.cues[-1]] and tl.sel_points == {"fx:wind": [2]}
    edits = len(rig.changed)
    assert tl.paste(19.99) == 0 and len(rig.water.cues) == 3 and len(rig.changed) == edits         # nothing fits at the very end
    assert tl.sel_cues == [rig.water.cues[-1]]                                                     # ... and nothing changed
    QTest.keyClick(tl, Qt.Key.Key_Right)
    assert rig.water.cues[-1].t == pytest.approx(10.04) and rig.wind.points[2][0] == pytest.approx(11.04)   # one frame
    QTest.keyClick(tl, Qt.Key.Key_Left, SHIFT)
    assert rig.water.cues[-1].t == pytest.approx(9.64)
    QTest.keyClick(tl, Qt.Key.Key_Down, SHIFT)
    assert rig.water.cues[-1].level == pytest.approx(0.5) and rig.wind.points[2][1] == pytest.approx(0.3)
    tl.sel_cues, tl.sel_points = [rig.water.cues[1]], {}
    QTest.keyClick(tl, Qt.Key.Key_D, CTRL)                                                         # duplicate right after
    assert sorted(c.t for c in rig.water.cues) == pytest.approx([2.0, 5.0, 5.5, 9.64])
    QTest.keyClick(tl, Qt.Key.Key_X, CTRL)
    assert len(rig.water.cues) == 3 and len(tl.clipboard) == 1
    tl.sel_points = {"fx:wind": [1, 2]}
    QTest.keyClick(tl, Qt.Key.Key_Delete)
    assert rig.wind.points == [(0.0, 0.0), (20.0, 0.0)] and not tl.has_selection()
    assert tl.delete_selection() is False and tl.nudge(dt=1.0) is False
    QTest.keyClick(tl, Qt.Key.Key_End)
    QTest.keyClick(tl, Qt.Key.Key_Home)
    assert rig.seeks[-2:] == [20.0, 0.0] and len(rig.started) == len(rig.changed)
    # the enable switch in the lane header
    send(tl, "press", 17, rig.mid("fx:water"))
    send(tl, "release", 17, rig.mid("fx:water"))
    assert rig.water.enabled is False
    QTest.keyClick(tl, Qt.Key.Key_F)
    QTest.keyClick(tl, Qt.Key.Key_Plus)
    QTest.keyClick(tl, Qt.Key.Key_Minus)
    assert tl.pps == pytest.approx(50.0)


def test_wheel_scrolling_and_follow(rig: Rig) -> None:
    tl = rig.tl
    tl.zoom(4.0, rig.x(0.0))
    assert tl.t0 == 0.0 and tl.pps == pytest.approx(200.0)
    tl.set_playhead(15.0, follow=True)                                                             # playback keeps the playhead in view
    assert HEADER_W <= tl.x_of(15.0) <= tl.width()
    tl.scroll_to(t0=999.0)
    assert tl.t0 == pytest.approx(20.0 - (tl.width() - HEADER_W - 24) / 200.0)
    rig.panel.resize(HEADER_W + 1024, 150)                                                         # too low for all lanes
    QApplication.processEvents()
    assert rig.panel.vbar.maximum() > 0
    tl.scroll_to(y0=10_000)
    assert tl.y0 == tl._max_scroll() > 0 and tl._lane_at(RULER_H + 1)[0] is not None
