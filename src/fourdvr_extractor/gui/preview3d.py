"""3D Stewart-platform preview drawn with QPainter (no OpenGL driver needed)."""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import QWidget

from fourdvr_motion.platform_config import PlatformConfig

BG = QColor("#0d1117")
GRID = QColor("#21262d")
BASE = QColor("#30363d")
TOP = QColor(56, 139, 253, 110)
TOP_EDGE = QColor("#58a6ff")
TRAIL = QColor("#3fb950")
TEXT = QColor("#e6edf3")
MUTED = QColor("#8b949e")


def stroke_colour(u: float) -> QColor:
    """Green -> amber -> red as an actuator approaches either end of its stroke (u = 0..1)."""
    u = max(0.0, min(1.0, u))
    if u < 0.6:
        return QColor("#3fb950")
    if u < 0.85:
        return QColor("#d29922")
    return QColor("#f85149")


class PlatformView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 260)
        self.profile: PlatformConfig | None = None
        self.pose = np.zeros(6)
        self.lengths: np.ndarray | None = None
        self.trail: np.ndarray | None = None
        self.yaw = math.radians(-35.0)
        self.elev = math.radians(22.0)
        self.zoom = 1.0
        self._drag: QPointF | None = None
        self.setMouseTracking(False)

    def set_profile(self, profile: PlatformConfig) -> None:
        self.profile = profile
        self.lengths = profile.geometry.leg_lengths(np.zeros(6))
        self.update()

    def set_state(self, pose: np.ndarray, trail: np.ndarray | None = None) -> None:
        self.pose = np.asarray(pose, dtype=np.float64)
        if self.profile is not None:
            self.lengths = self.profile.geometry.leg_lengths(self.pose + self.profile.calibration.zero_offsets)
        self.trail = trail
        self.update()

    # ------------------------------------------------------------ camera
    def _project(self, pts: np.ndarray) -> list[QPointF]:
        """World (X fwd, Y left, Z up) -> screen, with an orbiting perspective camera."""
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        ce, se = math.cos(self.elev), math.sin(self.elev)
        out = []
        w, h = self.width(), self.height()
        scale = 0.55 * min(w, h) * self.zoom
        centre = np.array([0.0, 0.0, 0.45])
        for p in np.atleast_2d(pts):
            x, y, z = p - centre
            # rotate about Z (yaw), then tilt about the camera's horizontal axis
            xr = cy * x - sy * y
            yr = sy * x + cy * y
            depth = xr * ce + z * se
            up = -xr * se + z * ce
            persp = 6.0 / (6.0 + depth)
            out.append(QPointF(w / 2 - yr * scale * persp, h * 0.58 - up * scale * persp))
        return out

    # ------------------------------------------------------------- input
    def mousePressEvent(self, e: QMouseEvent) -> None:
        self._drag = e.position()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag is not None:
            d = e.position() - self._drag
            self._drag = e.position()
            self.yaw -= d.x() * 0.01
            self.elev = max(-0.2, min(1.4, self.elev + d.y() * 0.01))
            self.update()

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        self._drag = None

    def wheelEvent(self, e: QWheelEvent) -> None:
        self.zoom = max(0.4, min(3.0, self.zoom * (1.1 if e.angleDelta().y() > 0 else 1 / 1.1)))
        self.update()

    # ------------------------------------------------------------ drawing
    def paintEvent(self, _event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        if self.profile is None:
            p.setPen(MUTED)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Load a platform profile")
            p.end()
            return
        geo = self.profile.geometry
        # floor grid
        p.setPen(QPen(GRID, 1))
        for k in np.linspace(-1.0, 1.0, 9):
            a, b = self._project(np.array([[k, -1.0, 0.0], [k, 1.0, 0.0]]))
            p.drawLine(a, b)
            a, b = self._project(np.array([[-1.0, k, 0.0], [1.0, k, 0.0]]))
            p.drawLine(a, b)
        # base plate
        base = geo.base_joints
        order = np.argsort(np.arctan2(base[:, 1], base[:, 0]))
        p.setPen(QPen(BASE.lighter(130), 2))
        p.setBrush(QBrush(BASE))
        p.drawPolygon(QPolygonF(self._project(base[order])))
        # actuators
        joints = geo.joint_positions(self.pose + self.profile.calibration.zero_offsets)
        act = self.profile.actuators
        mid = (act.min_length + act.max_length) / 2
        half = act.stroke / 2
        lengths = self.lengths if self.lengths is not None else geo.neutral_lengths()
        for i in range(6):
            util = abs(lengths[i] - mid[i]) / half[i]
            a, b = self._project(np.array([base[i], joints[i]]))
            p.setPen(QPen(stroke_colour(util), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(a, b)
        # trail of the rotation centre
        if self.trail is not None and len(self.trail) > 1:
            path = QPainterPath()
            pts = self._project(self.trail)
            path.moveTo(pts[0])
            for q in pts[1:]:
                path.lineTo(q)
            p.setPen(QPen(TRAIL, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        # moving platform
        porder = np.argsort(np.arctan2(geo.platform_joints[:, 1], geo.platform_joints[:, 0]))
        p.setPen(QPen(TOP_EDGE, 2))
        p.setBrush(QBrush(TOP))
        p.drawPolygon(QPolygonF(self._project(joints[porder])))
        origin, rot = geo.transform(self.pose + self.profile.calibration.zero_offsets)
        nose = origin + rot @ np.array([0.45, 0.0, 0.0])
        a, b = self._project(np.array([origin, nose]))
        p.setPen(QPen(TEXT, 2))
        p.drawLine(a, b)
        p.drawText(b + QPointF(4, -4), "forward")
        # readout
        p.setPen(TEXT)
        p.setFont(QFont("monospace", 9))
        s = self.pose
        lines = [
            f"surge {s[0] * 1000:+7.1f} mm   roll  {math.degrees(s[3]):+6.2f}°",
            f"sway  {s[1] * 1000:+7.1f} mm   pitch {math.degrees(s[4]):+6.2f}°",
            f"heave {s[2] * 1000:+7.1f} mm   yaw   {math.degrees(s[5]):+6.2f}°",
            "legs  "
            + " ".join(f"{(ln - mn) * 1000:5.0f}" for ln, mn in zip(lengths, act.min_length, strict=True))
            + " mm",
        ]
        for i, text in enumerate(lines):
            p.drawText(10, 18 + 15 * i, text)
        p.setPen(MUTED)
        p.drawText(10, self.height() - 10, "drag: orbit · wheel: zoom")
        p.end()
