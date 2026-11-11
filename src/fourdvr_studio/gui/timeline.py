"""Timeline editor: motion, vibration / sound and effects over the programme's time axis.

Lanes
    view    read-only: the sound track, or the motion of one axis of a finished .motion file
    curve   motion: the intensity of the automatic motion and one lane per axis - the lane shows what the
            platform does on that axis, and the eased curve through its points is what is added by hand
    fx      one effect track: bursts ("cues", drawn as the trapezoid that is output) and/or a level curve

Lanes are shown in groups (Motion, Vibration and sound, Effects); a click on a group's heading folds
it away or shows it again.

Mouse
    ruler                 click / drag: move the playhead
    empty lane area       click: playhead here; drag: select items in a rectangle
    double click          add a cue or a curve point (Alt: the other kind on an effect lane)
    cue                   drag: move; edges: resize; top corners: fade in / out; top centre: level
    curve point           drag: move (Shift: one direction only)
    Ctrl + click          add to / remove from the selection
    wheel                 scroll; Shift + wheel: along time; Ctrl + wheel: zoom at the pointer
    right click           context menu (built by the window)

Keys: Delete, Ctrl+A/C/X/V/D, arrows (nudge; Shift = coarse; Up/Down = level), +/-, F (fit), S (snap), Space.

The widget edits the document objects it is given in place and announces every change with
``editStarted`` (before the first modification of a gesture - the window takes its undo snapshot
there) and ``edited`` (after it).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QGridLayout, QScrollBar, QWidget

from fourdvr_extractor.manual import ease
from fourdvr_fx import Cue, Track, effect_type

from . import theme

Point = tuple[float, float]

HEADER_W = 184
RULER_H = 26
GROUP_H = 18
PAD = 5
HANDLE = 4.0
GRAB = 7.0                     # pixels within which a handle / edge is hit
MIN_CUE_S = 0.04
MIN_POINT_GAP_S = 0.01
TICK_STEPS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 3600)
MAX_PPS = 600.0


@dataclass
class Lane:
    key: str
    label: str
    colour: str
    kind: str                                  # "view" | "curve" | "fx"
    group: str = ""
    height: int = 46
    track: Track | None = None                 # fx lanes
    points: list[Point] | None = None          # curve lanes (edited in place)
    lo: float = 0.0
    hi: float = 1.0
    default: float = 0.0
    eased: bool = False
    add: str = "cue"                           # what a double click adds on an fx lane: "cue" | "point"
    view: str = ""                             # view lanes: "translation" | "rotation" | "audio" | "axis"
    hint: str = ""
    axis: int = -1                             # 0..5: the lane shows the motion of this axis (behind a curve, or alone)

    @property
    def editable(self) -> bool:
        return self.kind in ("curve", "fx")

    @property
    def enabled(self) -> bool:
        return self.track.enabled if self.track is not None else True

    def pts(self) -> list[Point]:
        if self.track is not None:
            return self.track.points
        return self.points if self.points is not None else []

    def set_pts(self, new: list[Point]) -> None:
        if self.track is not None:
            self.track.points = new
        elif self.points is not None:
            self.points[:] = new               # the list object belongs to the document: keep it

    def value_at(self, t: float) -> float:
        if self.track is not None:
            return self.track.value_at(t)
        pts = self.pts()
        if not pts:
            return self.default
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        i = bisect.bisect_right(pts, (t, math.inf))
        (t0, v0), (t1, v1) = pts[i - 1], pts[i]
        u = (t - t0) / (t1 - t0)
        return v0 + (v1 - v0) * (ease(u) if self.eased else u)

    def format(self, v: float) -> str:
        if self.kind == "fx":
            return f"{v * 100:.0f} %"
        if self.key == "motion:gain":
            return f"×{v:.2f}"
        return f"{v * 100:+.0f} %"


@dataclass
class _Drag:
    kind: str                                   # scrub | band | cue | point
    part: str = ""                              # cue: move | left | right | level | fade_in | fade_out
    lane: Lane | None = None
    top: float = 0.0
    x: float = 0.0
    y: float = 0.0
    t: float = 0.0
    cue: Cue | None = None
    cues: list[tuple[Cue, float, float, float, float, float]] = field(default_factory=list)
    points: dict[str, tuple[list[Point], list[int]]] = field(default_factory=dict)
    moved: bool = False
    band: QRectF | None = None
    additive: bool = False


class TimelineCanvas(QWidget):
    seekRequested = Signal(float)
    editStarted = Signal()
    edited = Signal()
    selectionChanged = Signal()
    menuRequested = Signal(str, float, float, QPoint)
    playToggled = Signal()
    viewChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.lanes: list[Lane] = []
        self.duration = 0.0
        self.playhead = 0.0
        self.pps = 10.0                 # pixels per second
        self.t0 = 0.0                   # time at the left edge of the lane area
        self.y0 = 0                     # vertical scroll (pixels)
        self.snap = True
        self.frame_s = 1.0 / 30.0
        self.current_lane = ""
        self.sel_cues: list[Cue] = []
        self.sel_points: dict[str, list[int]] = {}
        self.motion: np.ndarray | None = None
        self.motion_fs = 100.0
        self.motion_limits: np.ndarray | None = None
        self.motion_final = True
        self.wave: tuple[np.ndarray, np.ndarray, np.ndarray, float] | None = None   # lo, hi, bass, rate
        self.cuts: list[float] = []
        self.impacts: list[float] = []
        self.held: dict[str, list[tuple[float, float, str]]] = {}
        self.clipboard: list[tuple[str, str, Any]] = []
        self.collapsed: set[str] = set()                # groups folded away by a click on their heading
        self._drag: _Drag | None = None
        self._cache: QPixmap | None = None
        self._tag = ""
        self._fitted = False

    # ================================================================== data
    def set_lanes(self, lanes: list[Lane], keep_selection: bool = False) -> None:
        self.lanes = lanes
        if not keep_selection:
            self.sel_cues, self.sel_points = [], {}
        else:
            alive = {id(c) for ln in lanes if ln.track is not None for c in ln.track.cues}
            self.sel_cues = [c for c in self.sel_cues if id(c) in alive]
            keys = {ln.key: len(ln.pts()) for ln in lanes}
            self.sel_points = {k: [i for i in v if i < keys.get(k, 0)] for k, v in self.sel_points.items() if k in keys}
        if self.current_lane not in {ln.key for ln in lanes}:
            self.current_lane = next((ln.key for ln in lanes if ln.editable), "")
        self.y0 = max(0, min(self.y0, self._max_scroll()))
        self.refresh()
        self.viewChanged.emit()
        self.selectionChanged.emit()

    def set_duration(self, duration: float, frame_s: float = 1.0 / 30.0) -> None:
        changed = abs(duration - self.duration) > 1e-6
        self.duration, self.frame_s = max(0.0, duration), max(1e-3, frame_s)
        if changed or not self._fitted:
            self.zoom_fit()
        self.refresh()

    def set_reference(
        self,
        motion: np.ndarray | None,
        fs: float,
        limits: np.ndarray | None,
        final: bool,
        wave: tuple[np.ndarray, np.ndarray, np.ndarray, float] | None,
        cuts: list[float],
        impacts: list[float],
    ) -> None:
        self.motion, self.motion_fs, self.motion_limits, self.motion_final = motion, fs, limits, final
        self.wave, self.cuts, self.impacts = wave, list(cuts), list(impacts)
        self.refresh()

    def set_held(self, held: dict[str, list[tuple[float, float, str]]]) -> None:
        self.held = held
        self.refresh()

    def set_playhead(self, t: float, follow: bool = False) -> None:
        self.playhead = t
        if follow and self.duration > 0:
            x = self.x_of(t)
            if x > self.width() - 40 or x < HEADER_W:
                self.t0 = max(0.0, min(t - 40 / self.pps, self._max_t0()))
                self._cache = None
                self.viewChanged.emit()
        self.update()

    def refresh(self) -> None:
        self._cache = None
        self.update()

    def lane(self, key: str) -> Lane | None:
        return next((ln for ln in self.lanes if ln.key == key), None)

    # ================================================================== geometry
    def x_of(self, t: float) -> float:
        return HEADER_W + (t - self.t0) * self.pps

    def t_of(self, x: float) -> float:
        return self.t0 + (x - HEADER_W) / self.pps

    def _span(self) -> float:
        return max(1.0, self.width() - HEADER_W)

    def _max_t0(self) -> float:
        return max(0.0, self.duration - (self._span() - 24) / self.pps)

    def _rows(self) -> list[tuple[str, Any, float]]:
        """From top to bottom: ("group", name, top) for every group heading and ("lane", Lane, top) for
        every lane that is shown. The lanes of a folded group are left out; its heading stays."""
        out: list[tuple[str, Any, float]] = []
        y = float(RULER_H - self.y0)
        group = None
        for ln in self.lanes:
            if ln.group and ln.group != group:
                out.append(("group", ln.group, y))
                y += GROUP_H
            group = ln.group or group
            if ln.group in self.collapsed:
                continue
            out.append(("lane", ln, y))
            y += ln.height
        return out

    def _layout(self) -> list[tuple[Lane, float]]:
        return [(item, top) for kind, item, top in self._rows() if kind == "lane"]

    def content_height(self) -> int:
        rows = self._rows()
        if not rows:
            return 0
        kind, item, top = rows[-1]
        return int(top + (GROUP_H if kind == "group" else item.height) - (RULER_H - self.y0))

    def _group_at(self, y: float) -> str | None:
        if y < RULER_H:
            return None
        return next((item for kind, item, top in self._rows() if kind == "group" and top <= y < top + GROUP_H), None)

    def toggle_group(self, name: str) -> None:
        """Fold a group of lanes away, or show it again (what is on the lanes is not touched)."""
        self.collapsed ^= {name}
        self.y0 = max(0, min(self.y0, self._max_scroll()))
        self.refresh()
        self.viewChanged.emit()

    def _max_scroll(self) -> int:
        return max(0, self.content_height() - max(0, self.height() - RULER_H))

    def _lane_at(self, y: float) -> tuple[Lane | None, float]:
        if y < RULER_H:
            return None, 0.0
        for ln, top in self._layout():
            if top <= y < top + ln.height:
                return ln, top
        return None, 0.0

    def _top_of(self, lane: Lane) -> float:
        return next((top for ln, top in self._layout() if ln is lane), 0.0)

    @staticmethod
    def _y_of(lane: Lane, top: float, v: float) -> float:
        usable = lane.height - 2 * PAD
        return top + PAD + usable * (1.0 - (v - lane.lo) / (lane.hi - lane.lo))

    @staticmethod
    def _v_of(lane: Lane, top: float, y: float) -> float:
        usable = lane.height - 2 * PAD
        v = lane.lo + (lane.hi - lane.lo) * (1.0 - (y - top - PAD) / usable)
        return min(lane.hi, max(lane.lo, v))

    # ================================================================== zoom / scroll
    def zoom_fit(self) -> None:
        if self.duration > 0 and self.width() > HEADER_W + 50:
            self.pps = min(MAX_PPS, (self._span() - 24) / self.duration)
            self.t0 = 0.0
            self._fitted = True
            self.refresh()
            self.viewChanged.emit()

    def min_pps(self) -> float:
        return (self._span() - 24) / self.duration if self.duration > 0 else 1.0

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        if self.duration <= 0:
            return
        x = anchor_x if anchor_x is not None else self.x_of(self.playhead)
        if not HEADER_W <= x <= self.width():
            x = HEADER_W + self._span() / 2
        t = self.t_of(x)
        self.pps = min(MAX_PPS, max(self.min_pps(), self.pps * factor))
        self.t0 = max(0.0, min(t - (x - HEADER_W) / self.pps, self._max_t0()))
        self.refresh()
        self.viewChanged.emit()

    def scroll_to(self, t0: float | None = None, y0: int | None = None) -> None:
        if t0 is not None:
            self.t0 = max(0.0, min(t0, self._max_t0()))
        if y0 is not None:
            self.y0 = max(0, min(y0, self._max_scroll()))
        self.refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:
        if not self._fitted or self.pps < self.min_pps():
            self.zoom_fit()
        self.y0 = max(0, min(self.y0, self._max_scroll()))
        self._cache = None
        self.viewChanged.emit()
        super().resizeEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta()
        mods = event.modifiers()
        steps = (delta.y() or delta.x()) / 120.0
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.25**steps, event.position().x())
        elif mods & Qt.KeyboardModifier.ShiftModifier or delta.x() or not self._max_scroll():
            self.scroll_to(t0=self.t0 - steps * 90.0 / self.pps)
            self.viewChanged.emit()
        else:
            self.scroll_to(y0=int(self.y0 - steps * 40))
            self.viewChanged.emit()
        event.accept()

    # ================================================================== snapping
    def _snap_targets(self, skip: set[int]) -> list[float]:
        targets = [0.0, self.duration, self.playhead, *self.cuts, *self.impacts]
        for ln in self.lanes:
            if ln.track is None:
                continue
            for c in ln.track.cues:
                if id(c) not in skip:
                    targets.extend((c.t, c.end))
        return targets

    def snap_time(self, t: float, skip: set[int] | None = None, enabled: bool = True) -> float:
        t = min(self.duration, max(0.0, t))
        if not (self.snap and enabled):
            return t
        best, dist = t, GRAB / self.pps
        for target in self._snap_targets(skip or set()):
            d = abs(target - t)
            if d < dist:
                best, dist = target, d
        if best == t and self.frame_s * self.pps >= 6.0:      # zoomed in far enough: whole video frames
            best = round(t / self.frame_s) * self.frame_s
        return min(self.duration, max(0.0, best))

    # ================================================================== selection
    def selected_cues(self) -> list[tuple[Lane, Cue]]:
        out: list[tuple[Lane, Cue]] = []
        for ln in self.lanes:
            if ln.track is not None:
                out.extend((ln, c) for c in ln.track.cues if any(c is s for s in self.sel_cues))
        return out

    def selected_points(self) -> list[tuple[Lane, int]]:
        out: list[tuple[Lane, int]] = []
        for key, idx in self.sel_points.items():
            ln = self.lane(key)
            if ln is not None:
                out.extend((ln, i) for i in sorted(idx) if i < len(ln.pts()))
        return out

    def has_selection(self) -> bool:
        return bool(self.sel_cues) or any(self.sel_points.values())

    def clear_selection(self) -> None:
        if self.has_selection():
            self.sel_cues, self.sel_points = [], {}
            self.selectionChanged.emit()
            self.refresh()

    def choose_lane(self, key: str) -> None:
        """Make a lane the current one with nothing selected on it: the window then shows the lane's options."""
        if self.lane(key) is None:
            return
        self.current_lane = key
        self.sel_cues, self.sel_points = [], {}
        self.refresh()
        self.selectionChanged.emit()

    def select_cue(self, cue: Cue, additive: bool = False) -> None:
        if additive:
            if any(cue is c for c in self.sel_cues):
                self.sel_cues = [c for c in self.sel_cues if c is not cue]
            else:
                self.sel_cues.append(cue)
        elif not any(cue is c for c in self.sel_cues):
            self.sel_cues, self.sel_points = [cue], {}
        self.selectionChanged.emit()
        self.refresh()

    def select_point(self, lane: Lane, index: int, additive: bool = False) -> None:
        current = self.sel_points.get(lane.key, [])
        if additive:
            self.sel_points[lane.key] = [i for i in current if i != index] if index in current else [*current, index]
        elif index not in current:
            self.sel_cues, self.sel_points = [], {lane.key: [index]}
        self.selectionChanged.emit()
        self.refresh()

    def select_all(self) -> None:
        """Everything on the current lane; a second time: everything on every lane."""
        def everything(lanes: list[Lane]) -> tuple[list[Cue], dict[str, list[int]]]:
            return ([c for ln in lanes if ln.track is not None for c in ln.track.cues],
                    {ln.key: list(range(len(ln.pts()))) for ln in lanes if ln.pts()})

        editable = [ln for ln in self.lanes if ln.editable]
        current = self.lane(self.current_lane)
        cues, points = everything([current] if current is not None and current.editable else editable)
        already = (len(cues) == len(self.sel_cues) and all(any(c is s for s in self.sel_cues) for c in cues)
                   and points == {k: sorted(v) for k, v in self.sel_points.items() if v})
        if already or not (cues or points):
            cues, points = everything(editable)
        self.sel_cues, self.sel_points = cues, points
        self.selectionChanged.emit()
        self.refresh()

    # ================================================================== editing
    def _begin(self) -> None:
        self.editStarted.emit()

    def _done(self) -> None:
        for ln in self.lanes:
            if ln.track is not None:
                ln.track.cues.sort(key=lambda c: (c.t, c.duration))
        self.refresh()
        self.edited.emit()
        self.selectionChanged.emit()

    def add_cue(self, lane: Lane, t: float) -> Cue | None:
        if lane.track is None or self.duration <= 0:
            return None
        et = effect_type(lane.track.type)
        t = min(max(0.0, t), max(0.0, self.duration - MIN_CUE_S))
        duration = max(MIN_CUE_S, min(et.default_duration_s, self.duration - t))
        fade = min(et.default_fade_s, duration / 2) if et.editing == "curve" else 0.0
        params = {spec.name: lane.track.params.get(spec.name, spec.default) for spec in et.params}
        cue = Cue(round(t, 3), round(duration, 3), 1.0, fade, fade, params)
        self._begin()
        lane.track.cues.append(cue)
        self.sel_cues, self.sel_points = [cue], {}
        self.current_lane = lane.key
        self._done()
        return cue

    def add_point(self, lane: Lane, t: float, v: float) -> int | None:
        if not lane.editable or self.duration <= 0:
            return None
        pts = list(lane.pts())
        t = round(min(self.duration, max(0.0, t)), 3)
        v = round(min(lane.hi, max(lane.lo, v)), 3)
        if any(abs(p[0] - t) < MIN_POINT_GAP_S for p in pts):
            return None
        self._begin()
        if not pts:
            # a first point alone would shift the whole lane: pin the default at both ends
            pts = [(0.0, lane.default), (round(self.duration, 3), lane.default)]
            pts = [p for p in pts if abs(p[0] - t) >= MIN_POINT_GAP_S]
        pts.append((t, v))
        pts.sort(key=lambda p: p[0])
        lane.set_pts(pts)
        index = pts.index((t, v))
        self.sel_cues, self.sel_points = [], {lane.key: [index]}
        self.current_lane = lane.key
        self._done()
        return index

    def delete_selection(self) -> bool:
        if not self.has_selection():
            return False
        self._begin()
        doomed = {id(c) for c in self.sel_cues}
        for ln in self.lanes:
            if ln.track is not None and doomed:
                ln.track.cues = [c for c in ln.track.cues if id(c) not in doomed]
            idx = set(self.sel_points.get(ln.key, []))
            if idx:
                ln.set_pts([p for i, p in enumerate(ln.pts()) if i not in idx])
        self.sel_cues, self.sel_points = [], {}
        self._done()
        return True

    def copy_selection(self) -> int:
        cues, points = self.selected_cues(), self.selected_points()
        if not cues and not points:
            return 0
        start = min([c.t for _ln, c in cues] + [ln.pts()[i][0] for ln, i in points])
        self.clipboard = [
            (ln.key, "cue", (c.t - start, c.duration, c.level, c.fade_in, c.fade_out, dict(c.params), c.label)) for ln, c in cues
        ] + [(ln.key, "point", (ln.pts()[i][0] - start, ln.pts()[i][1])) for ln, i in points]
        return len(self.clipboard)

    def paste(self, at: float | None = None) -> int:
        if not self.clipboard or self.duration <= 0:
            return 0
        at = self.playhead if at is None else at
        cues: list[tuple[Lane, Cue]] = []
        points: list[tuple[Lane, Point]] = []
        for key, kind, data in self.clipboard:
            ln = self.lane(key)
            if ln is None and kind == "cue":            # the track is gone: the current lane, if it holds cues
                ln = self.lane(self.current_lane)
            if ln is None:
                continue
            if kind == "cue" and ln.track is not None:
                dt, duration, level, fade_in, fade_out, params, label = data
                t = at + dt
                if t < self.duration - MIN_CUE_S:
                    cues.append((ln, Cue(round(t, 3), round(min(duration, self.duration - t), 3), level, fade_in, fade_out,
                                         dict(params), label)))
            elif kind == "point" and ln.editable:
                dt, v = data
                t = round(at + dt, 3)
                taken = [p[0] for p in ln.pts()] + [p[0] for other, p in points if other is ln]
                if t <= self.duration and not any(abs(x - t) < MIN_POINT_GAP_S for x in taken):
                    points.append((ln, (t, min(ln.hi, max(ln.lo, v)))))
        if not cues and not points:
            return 0                                    # nothing fits there: the document is not touched
        self._begin()
        for ln, cue in cues:
            assert ln.track is not None
            ln.track.cues.append(cue)
        for ln in {id(ln): ln for ln, _p in points}.values():
            ln.set_pts(sorted([*ln.pts(), *(p for other, p in points if other is ln)], key=lambda p: p[0]))
        self.sel_cues = [cue for _ln, cue in cues]
        self.sel_points = {}
        for ln, point in points:
            self.sel_points.setdefault(ln.key, []).append(ln.pts().index(point))
        self._done()
        return len(cues) + len(points)

    def nudge(self, dt: float = 0.0, dv: float = 0.0) -> bool:
        cues, points = self.selected_cues(), self.selected_points()
        if not cues and not points:
            return False
        self._begin()
        if cues:
            lo = min(c.t for _ln, c in cues)
            hi = max(c.end for _ln, c in cues)
            shift = min(max(dt, -lo), self.duration - hi)
            for _ln, c in cues:
                c.t = round(c.t + shift, 4)
                c.level = round(min(1.0, max(0.02, c.level + dv)), 3)
        self._move_points({ln.key: (list(ln.pts()), sorted(self.sel_points[ln.key])) for ln, _i in points}, dt, dv)
        self._done()
        return True

    def _move_points(self, snapshot: dict[str, tuple[list[Point], list[int]]], dt: float, dv: float) -> None:
        """Move the selected points of each lane from their snapshot positions; unselected neighbours fence them in."""
        for key, (orig, idx) in snapshot.items():
            ln = self.lane(key)
            if ln is None or not idx:
                continue
            chosen = set(idx)
            lo_room, hi_room = -math.inf, math.inf
            for i in idx:
                left = next((orig[j][0] for j in range(i - 1, -1, -1) if j not in chosen), None)
                right = next((orig[j][0] for j in range(i + 1, len(orig)) if j not in chosen), None)
                lo_room = max(lo_room, (left + MIN_POINT_GAP_S if left is not None else 0.0) - orig[i][0])
                hi_room = min(hi_room, (right - MIN_POINT_GAP_S if right is not None else self.duration) - orig[i][0])
            shift = min(max(dt, lo_room), hi_room) if lo_room <= hi_room else 0.0
            new = list(orig)
            for i in idx:
                t, v = orig[i]
                new[i] = (round(t + shift, 4), round(min(ln.hi, max(ln.lo, v + dv)), 4))
            ln.set_pts(new)

    # ================================================================== hit testing
    def _cue_rect(self, lane: Lane, top: float, cue: Cue) -> tuple[float, float, float, float]:
        x0, x1 = self.x_of(cue.t), self.x_of(cue.end)
        if x1 - x0 < 3.0:
            x1 = x0 + 3.0
        base = top + lane.height - PAD
        return x0, x1, base - cue.level * (lane.height - 2 * PAD), base

    def _hit(self, lane: Lane, top: float, x: float, y: float) -> tuple[str, Any, str] | None:
        pts = lane.pts()
        best: tuple[float, int] | None = None
        for i, (t, v) in enumerate(pts):
            d = math.hypot(self.x_of(t) - x, self._y_of(lane, top, v) - y)
            if d <= GRAB and (best is None or d < best[0]):
                best = (d, i)
        if best is not None:
            return "point", best[1], "move"
        if lane.track is None:
            return None
        for cue in reversed(lane.track.cues):                      # later cues are drawn on top
            x0, x1, y_top, _base = self._cue_rect(lane, top, cue)
            if not (x0 - GRAB / 2 <= x <= x1 + GRAB / 2 and top <= y <= top + lane.height):
                continue
            selected = any(cue is c for c in self.sel_cues)
            wide = x1 - x0 >= 26
            if selected and wide and abs(y - y_top) <= GRAB:
                if abs(x - (x0 + cue.fade_in * self.pps)) <= GRAB and x < (x0 + x1) / 2 - 6:
                    return "cue", cue, "fade_in"
                if abs(x - (x1 - cue.fade_out * self.pps)) <= GRAB and x > (x0 + x1) / 2 + 6:
                    return "cue", cue, "fade_out"
                if abs(x - (x0 + x1) / 2) <= GRAB + 3:
                    return "cue", cue, "level"
            if wide and x - x0 <= GRAB - 2:
                return "cue", cue, "left"
            if wide and x1 - x <= GRAB - 2:
                return "cue", cue, "right"
            return "cue", cue, "move"
        return None

    # ================================================================== mouse
    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        pos = event.position()
        x, y = pos.x(), pos.y()
        additive = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        lane, top = self._lane_at(y)
        if event.button() == Qt.MouseButton.RightButton:
            if lane is not None and x >= HEADER_W:
                hit = self._hit(lane, top, x, y)
                if hit and hit[0] == "cue":
                    self.select_cue(hit[1])
                elif hit and hit[0] == "point":
                    self.select_point(lane, hit[1])
                self.current_lane = lane.key
            if lane is not None:
                self.menuRequested.emit(lane.key, self.t_of(max(x, HEADER_W)), self._v_of(lane, top, y), event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if y < RULER_H:
            if x >= HEADER_W:
                self._drag = _Drag("scrub")
                self.seekRequested.emit(min(self.duration, max(0.0, self.t_of(x))))
            return
        if lane is None:
            group = self._group_at(y)
            if group is not None:
                self.toggle_group(group)
            else:
                self.clear_selection()
            return
        if lane.key != self.current_lane:                       # any lane can be the current one: the window
            self.current_lane = lane.key                        # shows the options of the lane that was clicked
            self.refresh()
            self.selectionChanged.emit()
        if x < HEADER_W:
            if lane.track is not None and x < 30:
                self._begin()
                lane.track.enabled = not lane.track.enabled
                self._done()
            return
        hit = self._hit(lane, top, x, y) if lane.editable else None
        t = self.t_of(x)
        if hit is None:
            self._drag = _Drag("band", lane=lane, x=x, y=y, t=t, additive=additive)
            return
        if hit[0] == "cue":
            cue: Cue = hit[1]
            self.select_cue(cue, additive)
            if not any(cue is c for c in self.sel_cues):
                return
            part = hit[2]
            chosen = self.sel_cues if part in ("move", "level") else [cue]
            self._drag = _Drag(
                "cue", part, lane, top, x, y, t, cue,
                cues=[(c, c.t, c.duration, c.level, c.fade_in, c.fade_out) for c in chosen],
                points={ln.key: (list(ln.pts()), sorted(self.sel_points[ln.key])) for ln, _i in self.selected_points()} if part == "move" else {},
            )
        else:
            self.select_point(lane, hit[1], additive)
            if hit[1] not in self.sel_points.get(lane.key, []):
                return
            self._drag = _Drag(
                "point", "move", lane, top, x, y, t,
                cues=[(c, c.t, c.duration, c.level, c.fade_in, c.fade_out) for c in self.sel_cues],
                points={ln.key: (list(ln.pts()), sorted(self.sel_points[ln.key])) for ln, _i in self.selected_points()},
            )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        x, y = pos.x(), pos.y()
        d = self._drag
        if d is None:
            self._hover(x, y)
            return
        if d.kind == "scrub":
            self.seekRequested.emit(min(self.duration, max(0.0, self.t_of(x))))
            return
        if not d.moved and math.hypot(x - d.x, y - d.y) < 3.0:
            return
        if d.kind == "band":
            d.moved = True
            d.band = QRectF(QPointF(max(HEADER_W, min(d.x, x)), min(d.y, y)), QPointF(max(d.x, x), max(d.y, y)))
            self.update()
            return
        if not d.moved:
            d.moved = True
            self._begin()
        free = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        dt = self.t_of(x) - d.t
        assert d.lane is not None
        usable = d.lane.height - 2 * PAD
        if d.kind == "cue" and d.cue is not None:
            self._drag_cue(d, dt, (d.y - y) / usable, not free)
        else:
            dv = (d.y - y) / usable * (d.lane.hi - d.lane.lo)
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if abs(x - d.x) > abs(y - d.y):
                    dv = 0.0
                else:
                    dt = 0.0
            orig, idx = d.points[d.lane.key]
            anchor = orig[idx[0]][0]
            if dt:
                dt = self.snap_time(anchor + dt, enabled=not free) - anchor
            self._move_points(d.points, dt, dv)
            for cue, t0, *_rest in d.cues:
                cue.t = round(min(max(0.0, t0 + dt), self.duration - cue.duration), 4)
            pts = d.lane.pts()
            i = idx[0]
            self._tag = f"{_clock(pts[i][0])}  {d.lane.format(pts[i][1])}"
        self._cache = None
        self.update()

    def _drag_cue(self, d: _Drag, dt: float, dlevel: float, snap: bool) -> None:
        cue = d.cue
        assert cue is not None
        skip = {id(c) for c, *_ in d.cues}
        t0, dur0, _level0, fin0, fout0 = next(v[1:] for v in d.cues if v[0] is cue)
        if d.part == "move":
            # either edge of the dragged cue may catch a snap target: take the smaller correction
            fix_start = self.snap_time(t0 + dt, skip, snap) - (t0 + dt)
            fix_end = self.snap_time(t0 + dur0 + dt, skip, snap) - (t0 + dur0 + dt)
            if fix_start == 0.0 or (fix_end != 0.0 and abs(fix_end) < abs(fix_start)):
                shift = dt + fix_end
            else:
                shift = dt + fix_start
            lo = min(v[1] for v in d.cues)
            hi = max(v[1] + v[2] for v in d.cues)
            shift = min(max(shift, -lo), self.duration - hi)
            for c, ct, *_rest in d.cues:
                c.t = round(ct + shift, 4)
            self._move_points(d.points, shift, 0.0)
        elif d.part == "left":
            start = min(self.snap_time(t0 + dt, skip, snap), t0 + dur0 - MIN_CUE_S)
            cue.t, cue.duration = round(start, 4), round(t0 + dur0 - start, 4)
            cue.fade_in = min(fin0, max(0.0, cue.duration - fout0))
        elif d.part == "right":
            end = max(self.snap_time(t0 + dur0 + dt, skip, snap), t0 + MIN_CUE_S)
            cue.duration = round(end - t0, 4)
            cue.fade_out = min(fout0, max(0.0, cue.duration - fin0))
        elif d.part == "level":
            for c, _t, _d, level, *_f in d.cues:
                c.level = round(min(1.0, max(0.02, level + dlevel)), 2)
        elif d.part == "fade_in":
            cue.fade_in = round(min(max(0.0, fin0 + dt), cue.duration - cue.fade_out), 3)
        elif d.part == "fade_out":
            cue.fade_out = round(min(max(0.0, fout0 - dt), cue.duration - cue.fade_in), 3)
        self._tag = f"{_clock(cue.t)}  {cue.duration:.2f} s  {cue.level * 100:.0f} %"

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        d, self._drag = self._drag, None
        self._tag = ""
        if d is None:
            return
        if d.kind == "band":
            if d.band is None:                                   # a plain click: playhead here
                if not d.additive:
                    self.clear_selection()
                self.seekRequested.emit(min(self.duration, max(0.0, d.t)))
            else:
                self._select_in(d.band, d.additive)
            self.update()
            return
        if d.moved:
            for key in d.points:                                 # times may have crossed nothing: order is kept
                ln = self.lane(key)
                if ln is not None:
                    ln.set_pts(sorted(ln.pts(), key=lambda p: p[0]))
            self._done()
        self.update()

    def _select_in(self, band: QRectF, additive: bool) -> None:
        if not additive:
            self.sel_cues, self.sel_points = [], {}
        for ln, top in self._layout():
            if not ln.editable or top + ln.height < band.top() or top > band.bottom():
                continue
            for i, (t, v) in enumerate(ln.pts()):
                if band.contains(QPointF(self.x_of(t), self._y_of(ln, top, v))):
                    chosen = self.sel_points.setdefault(ln.key, [])
                    if i not in chosen:
                        chosen.append(i)
            if ln.track is not None:
                for cue in ln.track.cues:
                    x0, x1, _y, _b = self._cue_rect(ln, top, cue)
                    if x1 >= band.left() and x0 <= band.right() and not any(cue is c for c in self.sel_cues):
                        self.sel_cues.append(cue)
        self.selectionChanged.emit()
        self.refresh()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        x, y = pos.x(), pos.y()
        lane, top = self._lane_at(y)
        if lane is None or x < HEADER_W or event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag = None
        if not lane.editable:
            self.seekRequested.emit(min(self.duration, max(0.0, self.t_of(x))))
            return
        if self._hit(lane, top, x, y) is not None:
            return
        other = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        t = self.snap_time(self.t_of(x))
        if lane.kind == "curve" or ((lane.add == "point") != other):
            self.add_point(lane, t, self._v_of(lane, top, y))
        else:
            self.add_cue(lane, t)

    def _hover(self, x: float, y: float) -> None:
        shape = Qt.CursorShape.ArrowCursor
        lane, top = self._lane_at(y)
        if lane is not None and lane.editable and x >= HEADER_W:
            hit = self._hit(lane, top, x, y)
            if hit is not None:
                part = hit[2]
                shape = {
                    "left": Qt.CursorShape.SizeHorCursor, "right": Qt.CursorShape.SizeHorCursor,
                    "fade_in": Qt.CursorShape.SizeHorCursor, "fade_out": Qt.CursorShape.SizeHorCursor,
                    "level": Qt.CursorShape.SizeVerCursor,
                }.get(part, Qt.CursorShape.PointingHandCursor if hit[0] == "point" else Qt.CursorShape.OpenHandCursor)
        elif lane is not None and lane.track is not None and x < 30:
            shape = Qt.CursorShape.PointingHandCursor
        elif lane is None and self._group_at(y) is not None:
            shape = Qt.CursorShape.PointingHandCursor
        if self.cursor().shape() != shape:
            self.setCursor(QCursor(shape))

    # ================================================================== keyboard
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        coarse = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        step = self.frame_s * (10 if coarse else 1)
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selection()
        elif ctrl and key == Qt.Key.Key_A:
            self.select_all()
        elif ctrl and key == Qt.Key.Key_C:
            self.copy_selection()
        elif ctrl and key == Qt.Key.Key_X:
            if self.copy_selection():
                self.delete_selection()
        elif ctrl and key == Qt.Key.Key_V:
            self.paste()
        elif ctrl and key == Qt.Key.Key_D:
            if self.copy_selection():
                cues, points = self.selected_cues(), self.selected_points()
                end = max([c.end for _l, c in cues] + [ln.pts()[i][0] + MIN_POINT_GAP_S for ln, i in points])
                self.paste(end)
        elif key == Qt.Key.Key_Left and self.has_selection():
            self.nudge(dt=-step)
        elif key == Qt.Key.Key_Right and self.has_selection():
            self.nudge(dt=step)
        elif key == Qt.Key.Key_Up and self.has_selection():
            self.nudge(dv=0.10 if coarse else 0.02)
        elif key == Qt.Key.Key_Down and self.has_selection():
            self.nudge(dv=-0.10 if coarse else -0.02)
        elif key == Qt.Key.Key_Left:
            self.seekRequested.emit(max(0.0, self.playhead - step))
        elif key == Qt.Key.Key_Right:
            self.seekRequested.emit(min(self.duration, self.playhead + step))
        elif key == Qt.Key.Key_Home:
            self.seekRequested.emit(0.0)
        elif key == Qt.Key.Key_End:
            self.seekRequested.emit(self.duration)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(1.4)
        elif key == Qt.Key.Key_Minus:
            self.zoom(1 / 1.4)
        elif key == Qt.Key.Key_F:
            self.zoom_fit()
        elif key == Qt.Key.Key_S and not ctrl:
            self.snap = not self.snap
            self.viewChanged.emit()
        elif key == Qt.Key.Key_Space:
            self.playToggled.emit()
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ================================================================== painting
    def paintEvent(self, _event: QPaintEvent) -> None:
        dpr = self.devicePixelRatioF()
        if self._cache is None or self._cache.width() != round(self.width() * dpr) or self._cache.height() != round(self.height() * dpr):
            self._cache = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
            self._cache.setDevicePixelRatio(dpr)
            self._cache.fill(QColor(theme.CANVAS))
            painter = QPainter(self._cache)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_static(painter)
            painter.end()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_overlay(p)
        p.end()

    @staticmethod
    def _hint(ln: Lane) -> str:
        """Second header line: what is on the lane right now."""
        if ln.track is None:
            return ln.hint
        parts = [ln.hint] if ln.hint else []
        if ln.track.cues:
            parts.append(f"{len(ln.track.cues)} cue{'s' if len(ln.track.cues) != 1 else ''}")
        if any(v > 0 for _t, v in ln.track.points):
            parts.append("curve")
        if "slot" in ln.track.params:
            parts.append(f"slot {ln.track.params['slot']:g}")
        if not ln.track.enabled:
            parts.append("off")
        return " · ".join(parts) or "empty"

    def _tick_step(self) -> float:
        return next((s for s in TICK_STEPS if s * self.pps >= 84), TICK_STEPS[-1])

    def _paint_static(self, p: QPainter) -> None:
        w, h = self.width(), self.height()
        step = self._tick_step()
        first = math.floor(self.t0 / step) * step
        ticks = [first + k * step for k in range(int(self._span() / (step * self.pps)) + 3)]
        ticks = [t for t in ticks if -1e-9 <= t <= self.duration + 1e-9]
        font_small = theme.font(8.0)
        font_label = theme.font(9.0, medium=True)

        # ---- group headings (click: fold / unfold) and lanes
        for kind, item, top in self._rows():
            if kind != "group":
                continue
            if RULER_H - GROUP_H < top < h:
                folded = item in self.collapsed
                p.fillRect(QRectF(0, top, w, GROUP_H), QColor(theme.INSET))
                p.setPen(QPen(QColor(theme.BORDER_MUTED), 1))
                p.drawLine(QPointF(0, round(top + GROUP_H) - 0.5), QPointF(w, round(top + GROUP_H) - 0.5))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.FG_SUBTLE))
                cy = top + GROUP_H / 2
                p.drawPolygon(QPolygonF([QPointF(9, cy - 4), QPointF(15, cy), QPointF(9, cy + 4)] if folded
                                        else [QPointF(8, cy - 3), QPointF(16, cy - 3), QPointF(12, cy + 3)]))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QColor(theme.FG_MUTED if folded else theme.FG_SUBTLE))
                p.setFont(theme.font(7.5, bold=True))
                hidden = sum(1 for ln in self.lanes if ln.group == item) if folded else 0
                text = item.upper() + (f"   ·   {hidden} lane{'s' if hidden != 1 else ''} folded away - click to show" if folded else "")
                p.drawText(QRectF(24, top, w - 34, GROUP_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        for ln, top in self._layout():
            if top + ln.height < RULER_H or top > h:
                continue
            area = QRectF(HEADER_W, top, w - HEADER_W, ln.height)
            p.save()
            p.setClipRect(area)
            p.fillRect(area, QColor(theme.INSET if ln.kind == "view" else theme.CANVAS))
            if ln.key == self.current_lane and ln.editable:
                p.fillRect(area, theme.colour(theme.ACCENT, 10))
            p.setPen(QPen(theme.colour(theme.BORDER_MUTED, 150), 1))
            for t in ticks:
                x = round(self.x_of(t)) + 0.5
                p.drawLine(QPointF(x, top), QPointF(x, top + ln.height))
            if self.duration > 0:                                  # beyond the end of the programme
                x_end = self.x_of(self.duration)
                if x_end < w:
                    p.fillRect(QRectF(x_end, top, w - x_end, ln.height), theme.colour(theme.INSET, 200))
            if ln.kind == "view":
                self._paint_view(p, ln, top)
            else:
                if ln.axis >= 0:                                   # what the platform does on this axis, behind the curve
                    self._paint_axis(p, ln, top, faint=True)
                self._paint_curve(p, ln, top)
                if ln.track is not None:
                    self._paint_cues(p, ln, top, font_small)
                    self._paint_held(p, ln, top)
                if not ln.enabled:
                    p.fillRect(area, theme.colour(theme.CANVAS, 170))
            p.restore()
            p.setPen(QPen(QColor(theme.BORDER_MUTED), 1))
            p.drawLine(QPointF(0, round(top + ln.height) - 0.5), QPointF(w, round(top + ln.height) - 0.5))
            # header
            head = QRectF(0, top, HEADER_W, ln.height - 1)
            p.fillRect(head, QColor(theme.SUBTLE))
            if ln.key == self.current_lane:
                p.fillRect(head, theme.colour(theme.ACCENT, 22))
            chip = QColor(ln.colour)
            if not ln.enabled:
                chip.setAlpha(70)
            p.fillRect(QRectF(0, top, 3, ln.height - 1), chip)
            if ln.track is not None:                               # enable switch
                p.setPen(QPen(chip, 1.5))
                p.setBrush(chip if ln.enabled else Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(17, top + ln.height / 2), 4.5, 4.5)
                p.setBrush(Qt.BrushStyle.NoBrush)
            p.setFont(font_label)
            p.setPen(QColor(theme.FG if ln.enabled else theme.FG_SUBTLE))
            text_x = 30 if ln.track is not None else 12
            label = p.fontMetrics().elidedText(ln.label, Qt.TextElideMode.ElideRight, HEADER_W - text_x - 52)
            hint_text = self._hint(ln)
            two_lines = ln.height >= 40 and bool(hint_text)
            p.drawText(QRectF(text_x, top + (5 if two_lines else 0), HEADER_W - text_x - 50, (ln.height - 1) / (2 if two_lines else 1)),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            if two_lines:
                p.setFont(font_small)
                p.setPen(QColor(theme.FG_SUBTLE))
                hint = p.fontMetrics().elidedText(hint_text, Qt.TextElideMode.ElideRight, HEADER_W - text_x - 8)
                p.drawText(QRectF(text_x, top + ln.height / 2 - 2, HEADER_W - text_x - 6, ln.height / 2 - 3),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, hint)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QPointF(HEADER_W - 0.5, 0), QPointF(HEADER_W - 0.5, h))

        # ---- ruler (drawn last: lanes scroll underneath it)
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor(theme.INSET))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QPointF(0, RULER_H - 0.5), QPointF(w, RULER_H - 0.5))
        p.setFont(font_small)
        p.save()
        p.setClipRect(QRectF(HEADER_W, 0, w - HEADER_W, RULER_H))
        for t in ticks:
            x = round(self.x_of(t)) + 0.5
            p.setPen(QPen(QColor(theme.FG_SUBTLE), 1))
            p.drawLine(QPointF(x, RULER_H - 7), QPointF(x, RULER_H - 1))
            p.setPen(QColor(theme.FG_MUTED))
            p.drawText(QPointF(x + 4, RULER_H - 9), _clock(t, step < 1))
        minor = step / 5
        if minor * self.pps >= 8:
            p.setPen(QPen(QColor(theme.BORDER), 1))
            t = math.floor(self.t0 / minor) * minor
            while t <= min(self.duration, self.t_of(w)):
                x = round(self.x_of(t)) + 0.5
                p.drawLine(QPointF(x, RULER_H - 4), QPointF(x, RULER_H - 1))
                t += minor
        p.setPen(Qt.PenStyle.NoPen)
        for t in self.cuts:                                        # scene cuts
            x = self.x_of(t)
            p.setBrush(QColor(theme.DANGER))
            p.drawPolygon(QPolygonF([QPointF(x - 3.5, 1), QPointF(x + 3.5, 1), QPointF(x, 7)]))
        for t in self.impacts:                                     # impact-like motion events
            x = self.x_of(t)
            p.setBrush(QColor(theme.ATTENTION))
            p.drawPolygon(QPolygonF([QPointF(x, 1), QPointF(x + 3, 4), QPointF(x, 7), QPointF(x - 3, 4)]))
        p.restore()
        p.fillRect(QRectF(0, 0, HEADER_W - 1, RULER_H - 1), QColor(theme.INSET))
        p.setPen(QColor(theme.FG_SUBTLE))
        p.setFont(theme.font(7.5, bold=True))
        p.drawText(QRectF(10, 0, HEADER_W - 20, RULER_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   "SNAP ON" if self.snap else "SNAP OFF")

    def _paint_axis(self, p: QPainter, ln: Lane, top: float, faint: bool = False) -> None:
        """The motion of one axis over time, full lane height = the axis' travel in the platform profile."""
        if self.motion is None or not len(self.motion) or not 0 <= ln.axis < self.motion.shape[1]:
            return
        cols = int(self.width() - HEADER_W)
        if cols <= 0:
            return
        times = self.t0 + np.arange(cols) / self.pps
        visible = times <= (len(self.motion) - 1) / self.motion_fs
        if not visible.any():
            return
        idx = np.clip(np.round(times[visible] * self.motion_fs).astype(int), 0, len(self.motion) - 1)
        data = self.motion[idx, ln.axis]
        full = float(self.motion_limits[ln.axis]) if self.motion_limits is not None else float(np.abs(self.motion[:, ln.axis]).max())
        mid = top + ln.height / 2
        half = ln.height / 2 - PAD
        xs = HEADER_W + np.nonzero(visible)[0] + 0.5
        ys = mid - np.clip(data / max(full, 1e-9), -1.0, 1.0) * half
        line = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)])
        if faint:
            area = QPolygonF(line)
            area.append(QPointF(float(xs[-1]), mid))
            area.append(QPointF(float(xs[0]), mid))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.colour(ln.colour, 26 if self.motion_final else 12))
            p.drawPolygon(area)
            p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(theme.colour(ln.colour, (120 if faint else 230) if self.motion_final else 70), 1.0 if faint else 1.2)
        if not self.motion_final:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawPolyline(line)

    def _paint_view(self, p: QPainter, ln: Lane, top: float) -> None:
        w = self.width()
        mid = top + ln.height / 2
        half = ln.height / 2 - 3
        cols = int(w - HEADER_W)
        if cols <= 0:
            return
        edges = self.t0 + np.arange(cols + 1) / self.pps
        p.setPen(QPen(theme.colour(theme.BORDER, 200), 1))
        p.drawLine(QPointF(HEADER_W, mid), QPointF(w, mid))
        if ln.view == "axis":
            self._paint_axis(p, ln, top)
            return
        if ln.view == "audio":
            if self.wave is None:
                return
            lo, hi, bass, rate = self.wave
            idx = np.clip((edges * rate).astype(int), 0, len(lo))
            a, b = idx[:-1], np.maximum(idx[1:], idx[:-1] + 1)
            valid = a < len(lo)
            if not valid.any():
                return
            cs = np.nonzero(valid)[0]
            top_v = np.array([hi[a[c]:b[c]].max() for c in cs])
            bot_v = np.array([lo[a[c]:b[c]].min() for c in cs])
            scale = max(1e-6, float(max(np.abs(hi).max(), np.abs(lo).max())))
            p.setPen(QPen(theme.colour(theme.FG_SUBTLE, 200), 1))
            for c, tv, bv in zip(cs, top_v, bot_v, strict=True):
                x = HEADER_W + c + 0.5
                p.drawLine(QPointF(x, mid - tv / scale * half), QPointF(x, mid - bv / scale * half + 0.5))
            if len(bass):
                env = np.array([bass[a[c]:b[c]].max() if a[c] < len(bass) else 0.0 for c in cs])
                path = QPainterPath(QPointF(HEADER_W + cs[0], top + ln.height - 2))
                for c, e in zip(cs, env, strict=True):
                    path.lineTo(QPointF(HEADER_W + c + 0.5, top + ln.height - 2 - e * (ln.height - 5)))
                path.lineTo(QPointF(HEADER_W + cs[-1], top + ln.height - 2))
                p.fillPath(path, theme.colour(theme.PINK, 40))
                p.setPen(QPen(theme.colour(theme.PINK, 220), 1.2))
                p.drawPath(path)
            return
        if self.motion is None or not len(self.motion):
            return
        first = 0 if ln.view == "translation" else 3
        idx = np.clip(np.round(edges[:-1] * self.motion_fs).astype(int), 0, len(self.motion) - 1)
        visible = edges[:-1] <= (len(self.motion) - 1) / self.motion_fs
        if not visible.any():
            return
        data = self.motion[idx[visible], first : first + 3]
        if self.motion_limits is not None:
            full = np.maximum(self.motion_limits[first : first + 3], 1e-9)
        else:
            full = np.full(3, max(1e-9, float(np.abs(self.motion[:, first : first + 3]).max())))
        xs = HEADER_W + np.nonzero(visible)[0] + 0.5
        for k in range(3):
            pen = QPen(theme.colour(theme.AXIS_COLOURS[k], 230 if self.motion_final else 120), 1.2)
            if not self.motion_final:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            ys = mid - np.clip(data[:, k] / full[k], -1.0, 1.0) * half
            p.drawPolyline(QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]))

    def _curve_path(self, ln: Lane, top: float) -> QPainterPath | None:
        pts = ln.pts()
        if not pts:
            return None
        w = self.width()
        lo_t, hi_t = self.t_of(HEADER_W), self.t_of(w)
        i0 = max(0, bisect.bisect_left(pts, (lo_t, -math.inf)) - 1)
        i1 = min(len(pts), bisect.bisect_right(pts, (hi_t, math.inf)) + 1)
        first = pts[i0]
        x_first, y_first = self.x_of(first[0]), self._y_of(ln, top, first[1])
        if i0 == 0 and x_first > HEADER_W - 2:                    # before the first point its value is held
            path = QPainterPath(QPointF(HEADER_W - 2, y_first))
            path.lineTo(QPointF(x_first, y_first))
        else:
            path = QPainterPath(QPointF(x_first, y_first))
        prev = first
        for t, v in pts[i0 + 1 : i1]:
            x = self.x_of(t)
            if ln.eased and x - self.x_of(prev[0]) > 3:
                for s in range(1, 14):
                    u = s / 14
                    path.lineTo(QPointF(self.x_of(prev[0] + (t - prev[0]) * u), self._y_of(ln, top, prev[1] + (v - prev[1]) * ease(u))))
            path.lineTo(QPointF(x, self._y_of(ln, top, v)))
            prev = (t, v)
        if i1 >= len(pts) and self.x_of(self.duration) > self.x_of(prev[0]):   # ... and after the last one
            path.lineTo(QPointF(self.x_of(self.duration), self._y_of(ln, top, prev[1])))
        return path

    def _paint_curve(self, p: QPainter, ln: Lane, top: float) -> None:
        w = self.width()
        colour = QColor(ln.colour)
        base_y = self._y_of(ln, top, ln.default if ln.kind == "curve" else 0.0)
        if ln.kind == "curve":
            p.setPen(QPen(theme.colour(theme.BORDER, 230), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(HEADER_W, base_y), QPointF(w, base_y))
        path = self._curve_path(ln, top)
        if path is None:
            return
        fill = QPainterPath(path)
        fill.lineTo(QPointF(path.currentPosition().x(), base_y))
        fill.lineTo(QPointF(path.elementAt(0).x, base_y))
        fill.closeSubpath()
        p.fillPath(fill, theme.colour(ln.colour, 38))
        p.setPen(QPen(colour, 1.6))
        p.drawPath(path)
        chosen = set(self.sel_points.get(ln.key, []))
        pts = ln.pts()
        if len(pts) * 9 > (self.x_of(pts[-1][0]) - self.x_of(pts[0][0])) and not chosen:
            return                                                 # too dense to show handles at this zoom
        for i, (t, v) in enumerate(pts):
            x = self.x_of(t)
            if x < HEADER_W - 6 or x > w + 6:
                continue
            centre = QPointF(x, self._y_of(ln, top, v))
            if i in chosen:
                p.setPen(QPen(QColor("#ffffff"), 1.6))
                p.setBrush(colour)
                p.drawEllipse(centre, HANDLE + 1.2, HANDLE + 1.2)
            else:
                p.setPen(QPen(colour, 1.4))
                p.setBrush(QColor(theme.CANVAS))
                p.drawEllipse(centre, HANDLE - 0.5, HANDLE - 0.5)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_cues(self, p: QPainter, ln: Lane, top: float, font: Any) -> None:
        assert ln.track is not None
        w = self.width()
        colour = QColor(ln.colour)
        et = effect_type(ln.track.type)
        p.setFont(font)
        for cue in ln.track.cues:
            x0, x1, y_top, base = self._cue_rect(ln, top, cue)
            if x1 < HEADER_W or x0 > w:
                continue
            fin = min(cue.fade_in * self.pps, x1 - x0)
            fout = min(cue.fade_out * self.pps, x1 - x0 - fin)
            shape = QPolygonF([QPointF(x0, base), QPointF(x0 + fin, y_top), QPointF(x1 - fout, y_top), QPointF(x1, base)])
            selected = any(cue is c for c in self.sel_cues)
            p.setPen(QPen(QColor("#ffffff") if selected else colour, 1.6 if selected else 1.2))
            p.setBrush(theme.colour(ln.colour, 150 if selected else 95))
            p.drawPolygon(shape)
            p.setBrush(Qt.BrushStyle.NoBrush)
            text = cue.label or " ".join(
                f"{cue.params.get(s.name, ln.track.params.get(s.name, s.default)):g}{' ' + s.unit if s.unit else ''}"
                if s.name != "slot" else f"slot {cue.params.get('slot', ln.track.params.get('slot', s.default)):g}"
                for s in et.params
            )
            if text and x1 - x0 - fin - fout > p.fontMetrics().horizontalAdvance(text) + 8 and base - y_top > 13:
                p.setPen(QColor("#ffffff" if selected else theme.FG))
                p.drawText(QRectF(x0 + fin + 4, y_top + 1, x1 - x0 - fin - fout - 8, min(14.0, base - y_top)),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            if selected and x1 - x0 >= 26:
                p.setPen(QPen(QColor("#ffffff"), 1.2))
                p.setBrush(colour)
                for hx in (x0 + fin, x1 - fout):
                    p.drawRect(QRectF(hx - 3, y_top - 3, 6, 6))
                p.drawEllipse(QPointF((x0 + x1) / 2, y_top), 4.0, 4.0)
                p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_held(self, p: QPainter, ln: Lane, top: float) -> None:
        assert ln.track is not None
        spans = self.held.get(ln.track.type)
        if not spans:
            return
        y = top + ln.height - 3.5
        p.setPen(QPen(QColor(theme.DANGER), 3))
        for t0, t1, _why in spans:
            p.drawLine(QPointF(self.x_of(t0), y), QPointF(max(self.x_of(t1), self.x_of(t0) + 2), y))

    def _paint_overlay(self, p: QPainter) -> None:
        w, h = self.width(), self.height()
        if not self.lanes:
            p.setFont(theme.font(9.5))
            p.setPen(QColor(theme.FG_SUBTLE))
            p.drawText(QRectF(0, RULER_H, w, h - RULER_H), Qt.AlignmentFlag.AlignCenter,
                       "Open a video (or a .motion file): its motion, vibration and sound, and one lane per effect appear here.")
            return
        # current values at the playhead
        p.setFont(theme.font(8.0))
        for ln, top in self._layout():
            if top + ln.height < RULER_H or top > h:
                continue
            if ln.axis >= 0:                                       # an axis lane: where the platform is on that axis
                if self.motion is None or not len(self.motion) or ln.axis >= self.motion.shape[1]:
                    continue
                v = float(self.motion[min(len(self.motion) - 1, max(0, round(self.playhead * self.motion_fs))), ln.axis])
                text = f"{v * 1000:+.0f} mm" if ln.axis < 3 else f"{math.degrees(v):+.1f}°"
            elif not ln.editable or (ln.kind == "curve" and not ln.pts()):
                continue
            else:
                text = ln.format(ln.value_at(self.playhead))
            p.setPen(QColor(theme.FG_MUTED if ln.enabled else theme.FG_SUBTLE))
            two_lines = ln.height >= 40 and bool(self._hint(ln))
            wide = 58 if ln.axis >= 0 else 50
            band = QRectF(HEADER_W - wide - 6, top + (5 if two_lines else 0), wide, (ln.height - 1) / (2 if two_lines else 1))
            if band.center().y() > RULER_H + 7:
                p.drawText(band, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
        # playhead
        x = self.x_of(self.playhead)
        if HEADER_W <= x <= w:
            xr = round(x) + 0.5
            p.setPen(QPen(QColor(theme.FG), 1))
            p.drawLine(QPointF(xr, RULER_H - 8), QPointF(xr, h))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.FG))
            p.drawPolygon(QPolygonF([QPointF(xr - 5, RULER_H - 13), QPointF(xr + 5, RULER_H - 13), QPointF(xr + 5, RULER_H - 8),
                                     QPointF(xr, RULER_H - 2), QPointF(xr - 5, RULER_H - 8)]))
            p.setBrush(Qt.BrushStyle.NoBrush)
        d = self._drag
        if d is not None and d.band is not None:
            p.setPen(QPen(QColor(theme.ACCENT), 1))
            p.setBrush(theme.colour(theme.ACCENT, 40))
            p.drawRect(d.band)
            p.setBrush(Qt.BrushStyle.NoBrush)
        if self._tag:
            pos = self.mapFromGlobal(QCursor.pos())
            p.setFont(theme.font(8.5))
            tw = p.fontMetrics().horizontalAdvance(self._tag) + 12
            box = QRectF(min(max(HEADER_W + 2, pos.x() + 12), w - tw - 2), max(RULER_H + 2, pos.y() - 26), tw, 18)
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.setBrush(QColor(theme.OVERLAY))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor(theme.FG))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self._tag)


def _clock(t: float, tenths: bool = True) -> str:
    if tenths:
        return f"{int(t // 60):02d}:{t % 60:05.2f}" if abs(t * 10 - round(t * 10)) > 1e-6 else f"{int(t // 60):02d}:{t % 60:04.1f}"
    return f"{int(t // 60):02d}:{round(t % 60):02d}"


class TimelinePanel(QWidget):
    """The canvas with its scroll bars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.canvas = TimelineCanvas(self)
        self.hbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.vbar = QScrollBar(Qt.Orientation.Vertical, self)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        grid.addWidget(self.canvas, 0, 0)
        grid.addWidget(self.vbar, 0, 1)
        grid.addWidget(self.hbar, 1, 0)
        self.canvas.viewChanged.connect(self._sync_bars)
        self.hbar.valueChanged.connect(self._h_moved)
        self.vbar.valueChanged.connect(self._v_moved)
        self._syncing = False

    def _sync_bars(self) -> None:
        c = self.canvas
        self._syncing = True
        total = int(c.duration * c.pps) + 24
        page = int(c.width() - HEADER_W)
        self.hbar.setRange(0, max(0, total - page))
        self.hbar.setPageStep(max(1, page))
        self.hbar.setSingleStep(40)
        self.hbar.setValue(int(c.t0 * c.pps))
        view = max(1, c.height() - RULER_H)
        self.vbar.setRange(0, max(0, c.content_height() - view))
        self.vbar.setPageStep(view)
        self.vbar.setSingleStep(24)
        self.vbar.setValue(c.y0)
        self.vbar.setVisible(c.content_height() > view)
        self._syncing = False

    def _h_moved(self, value: int) -> None:
        if not self._syncing:
            self.canvas.scroll_to(t0=value / self.canvas.pps)

    def _v_moved(self, value: int) -> None:
        if not self._syncing:
            self.canvas.scroll_to(y0=value)
