"""The venue window: the patch, the DMX that goes out, and the devices as stand-ins.

Left, the venue patch is made and changed here instead of in a text editor: the output, the ESP32
boards (found on the network and given to platforms), the fixtures - generic ones, or real devices
from the fixture library - with every address checked as it is typed.

Right, what the patch does with the programme: one tile per device (a fan turns, fog rises, a strobe
flashes - generic stand-ins, by effect type) and the 512 channels of a universe. Both follow the
playhead. Without LIVE this is a simulation: the same effects service as in the theatre, with the
same limits, sending to nobody. With LIVE it is what goes to the devices.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from fourdvr_fx import BUILTIN_TYPES, effect_type
from fourdvr_fx.dmx import CaptureOutput
from fourdvr_fx.model import ID_RE, RAW_DMX
from fourdvr_fx.patch import (
    ESP32_PLATFORM_UNIVERSE,
    ESP32_THEATRE_UNIVERSES,
    PROFILES,
    AudioConfig,
    Limits,
    Patch,
    PatchError,
    default_limits,
    patch_from_dict,
)
from fourdvr_fx.patchfile import read_data, write_data

from .. import PRODUCT_TITLE, __version__
from ..live import LiveSession
from . import theme

if TYPE_CHECKING:
    from .main_window import StudioWindow

COLS, ROWS = 32, 16
TILE_W, TILE_H, TILE_GAP = 168, 92, 8
PATCH_FILTER = "Venue patch (*.toml);;All files (*)"
PROTOCOLS = (("none", "No DMX output"), ("sacn", "sACN (E1.31) over the network"), ("artnet", "Art-Net over the network"),
             ("enttec-pro", "USB DMX interface (Enttec Pro compatible)"), ("esp32", "Through the ESP32 boards"))
# the stand-in a device is drawn as, by what it is used for
KINDS = {"wind": "fan", "fog": "fog", "strobe": "strobe", "water": "drops", "rain": "rain", "air": "air", "heat": "heat",
         "bubbles": "bubbles", "snow": "snow", "scent": "scent"}
LIMIT_FIELDS = (("max_level", "Highest level (0–1)"), ("ramp_up_s", "Ramp up (s for 0 → 1)"), ("ramp_down_s", "Ramp down (s)"),
                ("max_on_s", "Longest time on (s)"), ("min_off_s", "Pause after it (s)"), ("duty_window_s", "Duty window (s)"),
                ("max_duty", "Share of the window on (0–1)"), ("max_total_on_s", "Total per show (s)"),
                ("max_rate_hz", "Strobe: flashes per second"))
CHANNEL_COLUMNS = ("+", "Name", "Role", "Min", "Max", "Off", "On", "Value", "When", "Hz min", "Hz max")
CHANNEL_KEYS = ("offset", "name", "role", "min", "max", "off", "on", "value", "when", "hz_min", "hz_max")
EFFECT_CHOICES = [n for n in BUILTIN_TYPES if n not in (RAW_DMX, "vibration")]


def kind_of(effect: str) -> str:
    return KINDS.get(effect, "lamp")


def universe_name(patch: Patch, platform_ids: list[str], universe: int) -> str:
    """How a universe is called on the screen - through ESP32 boards a universe is a board's DMX line."""
    if patch.output.protocol != "esp32":
        return f"Universe {universe}"
    if universe in ESP32_THEATRE_UNIVERSES:
        return f"Theatre board, DMX line {ESP32_THEATRE_UNIVERSES.index(universe) + 1}"
    k = universe - ESP32_PLATFORM_UNIVERSE - 1
    return f"Board of {platform_ids[k]}" if 0 <= k < len(platform_ids) else f"Universe {universe}"


# ====================================================================================== the 512 channels
class DmxGrid(QWidget):
    """One universe: 512 cells, the value of each as a bar and a number, the cells of a fixture in its colour."""

    def __init__(self) -> None:
        super().__init__()
        self.values = bytes(512)
        self.owners: dict[int, tuple[str, str, str]] = {}       # channel 1..512 -> (fixture, effect, name of the channel)
        self.title = ""
        self.setMouseTracking(True)
        self.setMinimumSize(COLS * 15 + 34, ROWS * 15 + 2)

    def set_owners(self, owners: dict[int, tuple[str, str, str]], title: str) -> None:
        self.owners, self.title = owners, title
        self.update()

    def set_values(self, data: bytes) -> None:
        data = bytes(data[:512]).ljust(512, b"\x00")
        if data != self.values:
            self.values = data
            self.update()

    def _cells(self) -> tuple[float, float, float]:
        left = 32.0
        return left, (self.width() - left - 1) / COLS, (self.height() - 1) / ROWS

    def channel_at(self, x: float, y: float) -> int | None:
        left, w, h = self._cells()
        col, row = int((x - left) // w) if x >= left else -1, int(y // h)
        return row * COLS + col + 1 if 0 <= col < COLS and 0 <= row < ROWS else None

    def describe(self, channel: int) -> str:
        value = self.values[channel - 1]
        text = f"{self.title} · channel {channel} · {value} ({value / 2.55:.0f} %)"
        owner = self.owners.get(channel)
        if owner is not None:
            text += f"\n{owner[0]} ({effect_type(owner[1]).label})" + (f": {owner[2]}" if owner[2] else "")
        return text

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        channel = self.channel_at(event.position().x(), event.position().y())
        if channel is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), self.describe(channel), self)

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.INSET))
        left, w, h = self._cells()
        small = theme.font(max(6.0, min(8.0, h * 0.42)))
        p.setFont(small)
        numbers = w >= 21 and h >= 13
        for row in range(ROWS):
            p.setPen(QColor(theme.FG_SUBTLE))
            p.drawText(QRectF(0, row * h, left - 5, h), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(row * COLS + 1))
            for col in range(COLS):
                channel = row * COLS + col + 1
                value = self.values[channel - 1]
                owner = self.owners.get(channel)
                cell = QRectF(left + col * w, row * h, w, h)
                colour = QColor(effect_type(owner[1]).colour if owner is not None else theme.FG_MUTED)
                if owner is not None:
                    tint = QColor(colour)
                    tint.setAlpha(46)
                    p.fillRect(cell.adjusted(0.5, 0.5, -0.5, -0.5), tint)
                if value:
                    bar = QColor(colour)
                    bar.setAlpha(210)
                    height = (h - 2) * value / 255.0
                    p.fillRect(QRectF(cell.left() + 1, cell.bottom() - 1 - height, w - 2, height), bar)
                if numbers and (value or owner is not None):
                    p.setPen(QColor(theme.FG if value else theme.FG_SUBTLE))
                    p.drawText(cell, Qt.AlignmentFlag.AlignCenter, str(value))
        p.setPen(QPen(QColor(theme.BORDER_MUTED), 1))
        for col in range(COLS + 1):
            p.drawLine(QPointF(left + col * w, 0), QPointF(left + col * w, ROWS * h))
        for row in range(ROWS + 1):
            p.drawLine(QPointF(left, row * h), QPointF(left + COLS * w, row * h))
        p.end()


# ====================================================================================== the devices
def _alpha(colour: QColor, alpha: float) -> QColor:
    c = QColor(colour)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def draw_device(p: QPainter, box: QRectF, kind: str, level: float, rate_hz: float, phase: float, colour: QColor) -> None:
    """A generic device of this kind at this level. `phase` runs with time (in turns, flashes, drops)."""
    on = level > 0.004
    cx, cy, r = box.center().x(), box.center().y(), min(box.width(), box.height()) / 2.0 - 2.0
    line = QPen(_alpha(colour, 0.35 + 0.65 * level) if on else QColor(theme.FG_SUBTLE), 1.6)
    p.setPen(line)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "fan":
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_alpha(colour, 0.25 + 0.75 * level) if on else QColor(theme.BORDER)))
        for blade in range(3):
            angle = (phase * 360.0 + blade * 120.0) % 360.0
            p.drawPie(QRectF(cx - r + 3, cy - r + 3, 2 * r - 6, 2 * r - 6), int(angle * 16), 50 * 16)
        p.setBrush(QBrush(QColor(theme.CANVAS)))
        p.drawEllipse(QPointF(cx, cy), r * 0.2, r * 0.2)
    elif kind == "fog":
        p.setPen(Qt.PenStyle.NoPen)
        for k, (dx, dy, size) in enumerate(((-0.45, 0.25, 0.5), (0.0, 0.1, 0.62), (0.45, 0.28, 0.48), (-0.15, -0.3, 0.42), (0.3, -0.25, 0.36))):
            drift = math.sin(phase * 1.3 + k) * r * 0.08 if on else 0.0
            p.setBrush(QBrush(_alpha(colour, 0.12 + 0.6 * level) if on else QColor(theme.BORDER_MUTED)))
            p.drawEllipse(QPointF(cx + dx * r + drift, cy + dy * r), size * r, size * r * 0.8)
    elif kind == "strobe":
        lit = on and (rate_hz <= 0.0 or (phase % 1.0) < 0.3)
        if lit:
            p.setPen(QPen(QColor("#ffffff"), 2.0))
            for ray in range(8):
                a = ray * math.pi / 4.0
                p.drawLine(QPointF(cx + math.cos(a) * r * 0.55, cy + math.sin(a) * r * 0.55),
                           QPointF(cx + math.cos(a) * r, cy + math.sin(a) * r))
            p.setBrush(QBrush(_alpha(QColor("#ffffff"), 0.5 + 0.5 * level)))
            p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), r * 0.42, r * 0.42)
    elif kind in ("drops", "rain", "snow", "bubbles"):
        count = {"drops": 5, "rain": 9, "snow": 8, "bubbles": 6}[kind]
        shown = max(1, round(count * level)) if on else count
        up = kind == "bubbles"
        for k in range(shown):
            x = box.left() + box.width() * ((k * 0.618 + 0.13) % 1.0)
            t = (phase * (0.35 if kind == "snow" else 0.7 if up else 1.4) + k * 0.37) % 1.0 if on else (k * 0.37) % 1.0
            y = box.bottom() - t * box.height() if up else box.top() + t * box.height()
            sway = math.sin(phase * 2.0 + k) * 3.0 if kind in ("snow", "bubbles") and on else 0.0
            if kind == "rain":
                p.drawLine(QPointF(x, y), QPointF(x - 2.0, y + 7.0))
            elif kind == "bubbles":
                p.drawEllipse(QPointF(x + sway, y), 2.0 + (k % 3), 2.0 + (k % 3))
            else:
                p.setBrush(QBrush(line.color()))
                size = 1.7 if kind == "snow" else 2.3
                p.drawEllipse(QPointF(x + sway, y), size, size * (1.0 if kind == "snow" else 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "air":
        for k in range(5):
            y = box.top() + box.height() * (0.18 + 0.16 * k)
            reach = box.width() * (0.25 + (0.7 * level * (0.6 + 0.4 * math.sin(phase * 9.0 + k)) if on else 0.0))
            p.drawLine(QPointF(box.left() + 3, y), QPointF(box.left() + 3 + reach, y))
    elif kind == "heat":
        p.setPen(QPen(_alpha(QColor(theme.DANGER), 0.3 + 0.7 * level) if on else QColor(theme.FG_SUBTLE), 2.2))
        p.drawLine(QPointF(box.left() + 4, box.bottom() - 4), QPointF(box.right() - 4, box.bottom() - 4))
        p.setPen(line)
        for k in range(3):
            x = box.left() + box.width() * (0.25 + 0.25 * k)
            path = QPainterPath(QPointF(x, box.bottom() - 9))
            for step in range(1, 7):
                wave = math.sin(step * 1.6 + (phase * 4.0 if on else 0.0) + k) * 3.0
                path.lineTo(QPointF(x + wave, box.bottom() - 9 - step * (box.height() - 14) / 6.0))
            p.drawPath(path)
    elif kind == "scent":
        for k in range(3):
            grow = ((phase * 0.6 + k / 3.0) % 1.0) if on else (k + 1) / 3.5
            p.setPen(QPen(_alpha(colour, (1.0 - grow) * (0.4 + 0.6 * level)) if on else QColor(theme.FG_SUBTLE), 1.6))
            p.drawArc(QRectF(cx - r * grow, cy - r * grow, 2 * r * grow, 2 * r * grow), -50 * 16, 100 * 16)
        p.setBrush(QBrush(line.color()))
        p.drawEllipse(QPointF(cx - r * 0.1, cy), 2.5, 2.5)
    else:                                               # a lamp: anything that is simply more or less on
        p.drawEllipse(QPointF(cx, cy), r * 0.8, r * 0.8)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_alpha(colour, 0.15 + 0.85 * level)))
            p.drawEllipse(QPointF(cx, cy), r * 0.8, r * 0.8)


class DeviceTiles(QWidget):
    """One tile per device of the patch."""

    clicked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.phase: dict[str, float] = {}
        self.setMinimumHeight(TILE_H + 2 * TILE_GAP)
        self.setToolTip("The devices of the patch as generic stand-ins. Click one to switch it on for a second (in the simulation).")

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        if [r["key"] for r in rows] != [r["key"] for r in self.rows]:
            self.phase = {r["key"]: self.phase.get(r["key"], 0.0) for r in rows}
            self.rows = rows
            self._fit()
        else:
            self.rows = rows
        self.update()

    def advance(self, dt: float) -> None:
        for row in self.rows:
            level = float(row["level"])
            if level > 0.004:
                speed = float(row["rate_hz"]) if kind_of(row["effect"]) == "strobe" else 0.4 + 2.2 * level
                self.phase[row["key"]] = self.phase.get(row["key"], 0.0) + speed * dt

    def _columns(self) -> int:
        return max(1, (self.width() - TILE_GAP) // (TILE_W + TILE_GAP))

    def _fit(self) -> None:
        lines = max(1, math.ceil(len(self.rows) / self._columns()))
        self.setMinimumHeight(lines * (TILE_H + TILE_GAP) + TILE_GAP)

    def resizeEvent(self, _event: QResizeEvent) -> None:
        self._fit()

    def tile(self, index: int) -> QRectF:
        cols = self._columns()
        width = max(float(TILE_W), (self.width() - TILE_GAP * (cols + 1)) / cols)
        return QRectF(TILE_GAP + (index % cols) * (width + TILE_GAP), TILE_GAP + (index // cols) * (TILE_H + TILE_GAP), width, TILE_H)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        for k, row in enumerate(self.rows):
            if self.tile(k).contains(event.position()):
                self.clicked.emit(str(row["key"]))
                return

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.CANVAS))
        if not self.rows:
            p.setPen(QColor(theme.FG_SUBTLE))
            p.setFont(theme.font(9.5))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No device in the patch yet")
        for k, row in enumerate(self.rows):
            box = self.tile(k)
            level, held = float(row["level"]), str(row.get("limit") or "")
            colour = QColor(effect_type(row["effect"]).colour)
            p.setPen(QPen(QColor(theme.DANGER) if held else colour if level > 0.004 else QColor(theme.BORDER), 1.2))
            p.setBrush(QBrush(QColor(theme.SUBTLE)))
            p.drawRoundedRect(box, 6, 6)
            p.save()
            p.setClipRect(QRectF(box.left() + 6, box.top() + 22, 60, 60))
            draw_device(p, QRectF(box.left() + 8, box.top() + 24, 56, 56), kind_of(row["effect"]), level, float(row["rate_hz"]),
                        self.phase.get(row["key"], 0.0), colour)
            p.restore()
            p.setPen(QColor(theme.FG))
            p.setFont(theme.font(8.5, medium=True))
            name = p.fontMetrics().elidedText(str(row["key"]), Qt.TextElideMode.ElideMiddle, int(box.width() - 14))
            p.drawText(QRectF(box.left() + 8, box.top() + 4, box.width() - 14, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
            text = QRectF(box.left() + 74, box.top() + 24, box.width() - 80, 60)
            p.setFont(theme.font(13.0, medium=True))
            p.setPen(QColor(theme.FG if level > 0.004 else theme.FG_SUBTLE))
            p.drawText(QRectF(text.left(), text.top(), text.width(), 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{level * 100:.0f} %")
            p.setFont(theme.font(7.5))
            p.setPen(QColor(theme.FG_MUTED))
            values = "/".join(str(v) for v in row["dmx"][:6]) + ("…" if len(row["dmx"]) > 6 else "")
            sent = p.fontMetrics().elidedText(f"{row['where']}  {values}", Qt.TextElideMode.ElideRight, int(text.width()))
            p.drawText(QRectF(text.left(), text.top() + 22, text.width(), 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sent)
            state = f"held: {held}" if held else (f"{row['rate_hz']:g} flashes/s" if kind_of(row["effect"]) == "strobe" and level > 0.004
                                                  else effect_type(row["effect"]).label)
            p.setPen(QColor(theme.DANGER if held else theme.FG_SUBTLE))
            state = p.fontMetrics().elidedText(state, Qt.TextElideMode.ElideRight, int(text.width()))
            p.drawText(QRectF(text.left(), text.top() + 37, text.width(), 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, state)
        p.end()


# ====================================================================================== one fixture
def _number(text: str, what: str, whole: bool = False) -> int | float:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError as exc:
        raise PatchError(f"{what}: '{text}' is not a number") from exc
    if whole:
        if value != int(value):
            raise PatchError(f"{what}: a whole number is needed, not {text}")
        return int(value)
    return value


class FixtureDialog(QDialog):
    """One [[fixtures]] entry: where it is, what drives which channel, its limits."""

    def __init__(self, parent: QWidget | None, entry: dict[str, Any], taken: set[str], new: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a device" if new else f"Device {entry.get('id', '')}")
        self.entry = copy.deepcopy(entry)
        self.taken = taken
        self.result_entry: dict[str, Any] | None = None
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.in_id = QLineEdit(str(entry.get("id", "")))
        self.in_effect = QComboBox()
        self.in_effect.setEditable(True)
        for name in EFFECT_CHOICES:
            self.in_effect.addItem(name)
        self.in_effect.setCurrentText(str(entry.get("effect", "wind")))
        self.in_effect.setToolTip("The effect type whose lane on the timeline drives this device. Any name will do: a lane of the same name plays it.")
        self.in_label = QLineEdit(str(entry.get("label", "")))
        self.in_scope = QComboBox()
        self.in_scope.addItem("One for the whole theatre", "theatre")
        self.in_scope.addItem("One on every platform", "platform")
        self.in_scope.setCurrentIndex(1 if entry.get("scope") == "platform" else 0)
        self.in_universe = QSpinBox()
        self.in_universe.setRange(1, 63999)
        self.in_universe.setValue(int(entry.get("universe", 1)))
        self.in_address = QSpinBox()
        self.in_address.setRange(1, 512)
        self.in_address.setValue(int(entry.get("address", 1)))
        self.in_step = QSpinBox()
        self.in_step.setRange(0, 512)
        self.in_step.setValue(int(entry.get("address_step", 0)))
        self.in_step.setToolTip("Platform devices on one DMX line: the address of the second platform's device is this much higher, and so on.\n"
                                "Through ESP32 boards every platform has a line of its own: leave 0.")
        self.in_lead = QDoubleSpinBox()
        self.in_lead.setRange(0.0, 30.0)
        self.in_lead.setDecimals(2)
        self.in_lead.setSingleStep(0.1)
        self.in_lead.setSuffix(" s")
        self.in_lead.setValue(float(entry.get("lead_s", 0.0)))
        self.in_lead.setToolTip("Send this much earlier than the timeline says: the time the device needs to be felt (a fan spinning up, fog arriving).")
        self.in_flash = QComboBox()
        self.in_flash.addItem("The device flashes itself (rate channel)", "fixture")
        self.in_flash.addItem("We switch its level on and off", "software")
        self.in_flash.setCurrentIndex(1 if entry.get("flash") == "software" else 0)
        self.in_match = QLineEdit(", ".join(f"{k}={v:g}" for k, v in (entry.get("match") or {}).items()))
        self.in_match.setPlaceholderText("e.g. slot=2: only cues with this cartridge number")
        where = QHBoxLayout()
        for label, widget in (("Universe", self.in_universe), ("Address", self.in_address), ("Step per platform", self.in_step)):
            where.addWidget(QLabel(label))
            where.addWidget(widget)
        where.addStretch(1)
        form.addRow("Name (id)", self.in_id)
        form.addRow("Used for", self.in_effect)
        form.addRow("Description", self.in_label)
        form.addRow("How many", self.in_scope)
        form.addRow("DMX", where)
        form.addRow("Send earlier by", self.in_lead)
        form.addRow("Strobe", self.in_flash)
        form.addRow("Only cues with", self.in_match)
        lay.addLayout(form)

        self.channels = QTableWidget(0, len(CHANNEL_COLUMNS))
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Channels</b>  what each DMX channel of the device gets"))
        head.addStretch(1)
        self.in_profile = QComboBox()
        self.in_profile.addItem("Start again from…", "")
        self.in_profile.addItem("a dimmer: one channel, 0 to 100 %", "dimmer")
        self.in_profile.addItem("a switch: one channel, on or off (relay)", "switch")
        self.in_profile.addItem("level and flash rate: two channels", "level-rate")
        self.in_profile.activated.connect(self._from_profile)
        head.addWidget(self.in_profile)
        add = QPushButton("+ channel")
        add.clicked.connect(lambda: self._add_channel({"role": "static", "offset": self.channels.rowCount()}))
        drop = QPushButton("− channel")
        drop.clicked.connect(lambda: self.channels.removeRow(self.channels.currentRow() if self.channels.currentRow() >= 0
                                                             else self.channels.rowCount() - 1))
        head.addWidget(add)
        head.addWidget(drop)
        lay.addLayout(head)
        self.channels.setHorizontalHeaderLabels(list(CHANNEL_COLUMNS))
        self.channels.verticalHeader().setVisible(False)
        self.channels.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.channels.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 8):                          # the two columns with a choice: wide enough for its words
            self.channels.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.channels.setColumnWidth(column, 104)
        self.channels.setMinimumHeight(170)
        self.channels.setToolTip(
            "+       the channel's place after the address (0 = the address itself)\n"
            "level   follows the effect: Off while it is off, Min at the smallest level, Max at full\n"
            "switch  On above a small level, else Off\n"
            "rate    the flash rate of a strobe cue: Hz min at Min … Hz max at Max\n"
            "static  a fixed Value - while the effect is on (When = active), or always")
        lay.addWidget(self.channels, 1)
        raw = entry.get("channels") or PROFILES.get(str(entry.get("profile", "dimmer")), PROFILES["dimmer"])
        for channel in raw:
            self._add_channel(channel)

        lay.addWidget(QLabel("<b>Limits</b>  empty = the default for this kind of effect (shown in grey)"))
        grid = QGridLayout()
        self.limits: dict[str, QLineEdit] = {}
        own = entry.get("limits") or {}
        for k, (key, label) in enumerate(LIMIT_FIELDS):
            box = QLineEdit("" if key not in own else f"{own[key]:g}")
            box.setMaximumWidth(90)
            self.limits[key] = box
            grid.addWidget(QLabel(label), k // 3, (k % 3) * 2)
            grid.addWidget(box, k // 3, (k % 3) * 2 + 1)
        lay.addLayout(grid)
        self.in_effect.currentTextChanged.connect(self._defaults)
        self._defaults()
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setObjectName("warning")
        lay.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.resize(860, 640)
        theme.adopt(self)

    def _defaults(self) -> None:
        effect = self.in_effect.currentText().strip()
        try:
            limits: Limits = default_limits(effect)
        except PatchError:
            return
        for key, box in self.limits.items():
            box.setPlaceholderText(f"{getattr(limits, key):g}")
        self.in_flash.setEnabled(effect == "strobe")

    def _from_profile(self, index: int) -> None:
        name = self.in_profile.itemData(index)
        self.in_profile.setCurrentIndex(0)
        if name:
            self.channels.setRowCount(0)
            for channel in PROFILES[name]:
                self._add_channel(channel)

    def _add_channel(self, channel: dict[str, Any]) -> None:
        row = self.channels.rowCount()
        self.channels.insertRow(row)
        for col, key in enumerate(CHANNEL_KEYS):
            if key in ("role", "when"):
                box = QComboBox()
                for choice in (("level", "switch", "rate", "static") if key == "role" else ("active", "always")):
                    box.addItem(choice)
                box.setCurrentText(str(channel.get(key, "level" if key == "role" else "active")))
                self.channels.setCellWidget(row, col, box)
            else:
                value = channel.get(key, row if key == "offset" else "")
                item = QTableWidgetItem(f"{value:g}" if isinstance(value, float) else str(value))
                if key == "offset":                   # what the table does not show stays with the channel
                    item.setData(Qt.ItemDataRole.UserRole, {k: channel[k] for k in ("gamma", "threshold") if k in channel})
                self.channels.setItem(row, col, item)

    def _cell(self, row: int, col: int) -> str:
        widget = self.channels.cellWidget(row, col)
        if isinstance(widget, QComboBox):
            return widget.currentText()
        item = self.channels.item(row, col)
        return item.text().strip() if item is not None else ""

    def read(self) -> dict[str, Any]:
        """The entry as it stands in the dialog - PatchError if something cannot be."""
        out = {k: v for k, v in self.entry.items() if k not in ("profile", "channels", "limits", "match", "lead_s", "flash",
                                                                 "address_step", "scope", "label")}
        fixture_id = self.in_id.text().strip()
        if not ID_RE.match(fixture_id):
            raise PatchError("the name may hold letters, digits, - and _ (it is how the device is called everywhere)")
        if fixture_id in self.taken:
            raise PatchError(f"another device is already called {fixture_id}")
        out |= {"id": fixture_id, "effect": self.in_effect.currentText().strip()}
        if self.in_label.text().strip():
            out["label"] = self.in_label.text().strip()
        if self.in_scope.currentData() == "platform":
            out["scope"] = "platform"
            if self.in_step.value():
                out["address_step"] = self.in_step.value()
        out |= {"universe": self.in_universe.value(), "address": self.in_address.value()}
        if self.in_lead.value() > 0.0:
            out["lead_s"] = round(self.in_lead.value(), 3)
        if out["effect"] == "strobe":
            out["flash"] = self.in_flash.currentData()
        match: dict[str, float] = {}
        for part in filter(None, (x.strip() for x in self.in_match.text().split(","))):
            key, _, value = part.partition("=")
            match[key.strip()] = float(_number(value, "Only cues with"))
        if match:
            out["match"] = match
        channels: list[dict[str, Any]] = []
        for row in range(self.channels.rowCount()):
            role = self._cell(row, 2)
            channel: dict[str, Any] = {"role": role, "offset": int(_number(self._cell(row, 0) or "0", f"channel {row + 1}", whole=True))}
            wanted = {"level": ("min", "max", "off"), "switch": ("on", "off"), "rate": ("min", "max", "off", "hz_min", "hz_max"),
                      "static": ("value", "when", "off")}[role]
            for col, key in enumerate(CHANNEL_KEYS):
                text = self._cell(row, col)
                if key not in wanted or not text:
                    continue
                if key == "when":
                    if text != "active":
                        channel[key] = text
                else:
                    channel[key] = _number(text, f"channel {row + 1}, {CHANNEL_COLUMNS[col]}", whole=key not in ("hz_min", "hz_max"))
            first = self.channels.item(row, 0)
            kept = (first.data(Qt.ItemDataRole.UserRole) if first is not None else None) or {}
            channel |= {k: v for k, v in kept.items() if (k == "gamma") == (role == "level") and role in ("level", "switch")}
            if self._cell(row, 1):
                channel["name"] = self._cell(row, 1)
            channels.append(channel)
        out["channels"] = channels
        limits = {key: float(_number(box.text(), label)) for (key, label), box in zip(LIMIT_FIELDS, self.limits.values(), strict=True)
                  if box.text().strip()}
        if limits:
            out["limits"] = limits
        patch_from_dict({"fixtures": [copy.deepcopy(out)]})          # everything else that can be wrong with it
        return out

    def accept(self) -> None:
        try:
            self.result_entry = self.read()
        except (PatchError, ValueError) as exc:
            self.message.setText(str(exc))
            return
        super().accept()


# ====================================================================================== the fixture library
class LibraryDialog(QDialog):
    """Choose a real device from the fixture library, its mode, and what it is used for."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from fourdvr_fx.fixtures import library

        self.setWindowTitle("Fixture library")
        self.library = library()
        self.plan: Any = None
        self.model: Any = None
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.in_search = QLineEdit()
        self.in_search.setPlaceholderText("Maker or name, e.g. hazer cameo")
        self.in_category = QComboBox()
        self.in_category.addItem("All kinds", "")
        for name, count in self.library.categories().items():
            self.in_category.addItem(f"{name} ({count})", name)
        self.results = QListWidget()
        self.count = QLabel("")
        self.count.setObjectName("muted")
        left.addWidget(self.in_search)
        left.addWidget(self.in_category)
        left.addWidget(self.results, 1)
        left.addWidget(self.count)
        lay.addLayout(left, 2)
        right = QVBoxLayout()
        self.title = QLabel("Choose a device on the left")
        self.title.setWordWrap(True)
        form = QFormLayout()
        self.in_mode = QComboBox()
        self.in_mode.setToolTip("The DMX mode the device itself is set to (its menu or its DIP switches)")
        self.in_effect = QComboBox()
        self.in_effect.setEditable(True)
        for name in EFFECT_CHOICES:
            self.in_effect.addItem(name)
        self.in_output = QSpinBox()
        self.in_output.setRange(1, 32)
        self.in_output.setToolTip("A dimmer or relay pack: which of its outputs this device is on")
        form.addRow("Mode", self.in_mode)
        form.addRow("Used for", self.in_effect)
        form.addRow("Output of a pack", self.in_output)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(theme.font(8.5))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        note = QLabel('From the <a href="https://open-fixture-library.org">Open Fixture Library</a> (MIT licence). '
                      "A device that is missing: describe it with the library's online Fixture Editor and add the file with "
                      "<code>fourdvr fx fixtures library --add</code>.")
        note.setWordWrap(True)
        note.setOpenExternalLinks(True)
        note.setObjectName("muted")
        right.addWidget(self.title)
        right.addLayout(form)
        right.addWidget(self.preview, 1)
        right.addWidget(note)
        right.addWidget(self.buttons)
        lay.addLayout(right, 3)
        self.in_search.textChanged.connect(self.search)
        self.in_category.currentIndexChanged.connect(self.search)
        self.results.currentItemChanged.connect(lambda *_: self.choose())
        self.in_mode.currentIndexChanged.connect(lambda *_: self.show_plan())
        self.in_effect.currentTextChanged.connect(lambda *_: self.show_plan())
        self.in_output.valueChanged.connect(lambda *_: self.show_plan())
        self.resize(980, 620)
        theme.adopt(self)
        self.search()
        self.show_plan()

    def search(self) -> None:
        found = self.library.search(self.in_search.text(), self.in_category.currentData() or "")
        self.results.clear()
        for e in found[:400]:
            item = QListWidgetItem(f"{e.title}   ·   {', '.join(e.categories)}")
            item.setData(Qt.ItemDataRole.UserRole, e.key)
            self.results.addItem(item)
        self.count.setText(f"{len(found)} device(s)" + (" - the first 400 are shown: type more" if len(found) > 400 else ""))

    def choose(self, key: str = "") -> None:
        from fourdvr_fx.fixtures import FixtureError

        item = self.results.currentItem()
        key = key or (item.data(Qt.ItemDataRole.UserRole) if item is not None else "")
        if not key:
            return
        try:
            self.model = self.library.get(key)
        except FixtureError as exc:
            self.model = None
            self.title.setText(str(exc))
            return
        self.title.setText(f"<b>{self.model.title}</b>  ·  {', '.join(self.model.categories)}"
                           + (f"<br><span style='color:{theme.FG_MUTED}'>{self.model.comment[:260]}</span>" if self.model.comment else ""))
        self.in_mode.blockSignals(True)
        self.in_mode.clear()
        for mode in self.model.modes:
            self.in_mode.addItem(f"{mode.name} ({mode.footprint} channels)" + (f" - {mode.unsupported}" if mode.unsupported else ""), mode.short)
        first = next((k for k, m in enumerate(self.model.modes) if not m.unsupported), 0)
        self.in_mode.setCurrentIndex(first)
        self.in_mode.blockSignals(False)
        guess = {"Smoke": "fog", "Hazer": "fog", "Fan": "wind", "Strobe": "strobe"}
        for category in self.model.categories:
            if category in guess:
                self.in_effect.setCurrentText(guess[category])
                break
        self.show_plan()

    def show_plan(self) -> None:
        from fourdvr_fx.fixtures import FixtureError, describe, plan

        self.plan = None
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self.model is None or self.in_mode.currentIndex() < 0:
            ok.setEnabled(False)
            return
        mode = self.model.modes[self.in_mode.currentIndex()]
        effect = self.in_effect.currentText().strip()
        try:
            if not ID_RE.match(effect) or effect == RAW_DMX:
                raise FixtureError("say what the device is used for (an effect type: wind, fog, strobe, …)")
            try:
                self.plan = plan(self.model, mode, effect, self.in_output.value())
            except FixtureError:
                if self.in_output.value() == 1:
                    raise
                self.plan = plan(self.model, mode, effect, 1)        # one device, not a pack: its output number does not matter
            lines = describe(self.model, mode, self.plan) + [""] + [f"Note: {n}" for n in self.plan.notes]
            self.preview.setPlainText(f"The effect drives: {self.plan.driven}\n\n" + "\n".join(lines))
        except FixtureError as exc:
            self.preview.setPlainText(str(exc))
        self.in_output.setEnabled(self.plan is not None and self.plan.outputs > 1)
        ok.setEnabled(self.plan is not None)


# ====================================================================================== is the venue ready?
class ReadyDialog(QDialog):
    """The venue check, with the Control Server's account if its devices are to be listed."""

    finished_report = Signal(object)

    def __init__(self, venue: VenueWindow) -> None:
        super().__init__(venue)
        self.venue = venue
        self.report: Any = None
        self.setWindowTitle("Is the venue ready?")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.in_server = QLineEdit()
        self.in_server.setPlaceholderText("http://192.168.10.2:8080  (empty: the Control Server is not asked)")
        self.in_user = QLineEdit()
        self.in_user.setPlaceholderText("a viewer account will do: lists headsets and platform controllers")
        self.in_password = QLineEdit()
        self.in_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.chk_signal = QCheckBox("Test that the ESP32 boards go dark when the signal stops (sends the frame of a venue at rest)")
        self.chk_signal.setChecked(True)
        self.chk_programme = QCheckBox("Check the open programme against this venue")
        self.chk_programme.setChecked(True)
        form.addRow("Control Server", self.in_server)
        form.addRow("User", self.in_user)
        form.addRow("Password", self.in_password)
        lay.addLayout(form)
        lay.addWidget(self.chk_signal)
        lay.addWidget(self.chk_programme)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(theme.font(9.0))
        self.text.setPlaceholderText("Nothing is switched on by this check.")
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.btn_run = QPushButton("Check now")
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self.run)
        self.btn_copy = QPushButton("Copy")
        self.btn_copy.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(self.report.lines()) if self.report else ""))
        self.btn_save = QPushButton("Save…")
        self.btn_save.clicked.connect(self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        self.progress = QLabel("")
        self.progress.setObjectName("muted")
        row.addWidget(self.btn_run)
        row.addWidget(self.progress, 1)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_save)
        row.addWidget(close)
        lay.addLayout(row)
        self.finished_report.connect(self._show)
        self._said = ""
        self._poll = QTimer(self)
        self._poll.setInterval(120)
        self._poll.timeout.connect(lambda: self.progress.setText(self._said))
        self.resize(820, 600)
        theme.adopt(self)

    def arguments(self) -> dict[str, Any]:
        main = self.venue.main
        doc = None
        if self.chk_programme.isChecked() and main.project.mode != "empty":
            doc = main.project.effects_for_export()
        return {"server_url": self.in_server.text().strip(), "user": self.in_user.text().strip(), "password": self.in_password.text(),
                "programme": doc, "signal_loss": self.chk_signal.isChecked(), "sending": main.live is not None}

    def check(self) -> Any:
        """Run the check here and now (the button does the same without holding the window)."""
        from fourdvr_fx.ready import FAIL, Report, venue_ready

        patch = self.venue.current
        if patch is None:
            report = Report(title=Path(self.venue.path).name if self.venue.path else "")
            for text in self.venue.problems or ["the patch cannot be read"]:
                report.add(FAIL, "Venue patch", text)
            return report
        return venue_ready(patch, self.venue.platform_ids(), progress=self._say, **self.arguments())

    def _say(self, text: str) -> None:
        self._said = text + " …"

    def run(self) -> None:
        self.btn_run.setEnabled(False)
        self._said = "checking …"
        self._poll.start()
        self.venue.pause_simulation(True)                 # the test frames and the simulation do not mix on a real line
        threading.Thread(target=lambda: self.finished_report.emit(self.check()), name="venue-ready", daemon=True).start()

    def _show(self, report: Any) -> None:
        self._poll.stop()
        self.venue.pause_simulation(False)
        self.report = report
        colours = {"ok": theme.SUCCESS, "warn": theme.ATTENTION, "fail": theme.DANGER, "info": theme.FG_MUTED}
        marks = {"ok": "ok", "warn": "WARN", "fail": "FAIL", "info": "·"}
        html = [f"<p style='color:{theme.FG_MUTED}'>{report.lines()[0]}</p>"]
        area = ""
        for item in report.items:
            if item.area != area:
                area = item.area
                html.append(f"<p style='margin-top:10px'><b>{area}</b></p>")
            text = item.text.replace("&", "&amp;").replace("<", "&lt;")
            html.append(f"<div style='margin-left:14px'><span style='color:{colours[item.status]}'>{marks[item.status]}</span>&nbsp; {text}</div>")
        verdict = theme.SUCCESS if report.ready else theme.DANGER
        html.append(f"<p style='margin-top:14px;color:{verdict}'><b>{report.verdict()}</b></p>")
        self.text.setHtml("".join(html))
        self.progress.setText("")
        self.btn_run.setEnabled(True)

    def save(self) -> None:
        if self.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save the report", "venue-check.txt", "Text (*.txt)")
        if path:
            Path(path).write_text("\n".join(self.report.lines()) + "\n", encoding="utf-8")


# ====================================================================================== the window
class VenueWindow(QWidget):
    """Patch on the left; devices and DMX on the right, with the playhead."""

    boards_found = Signal(object)

    def __init__(self, main: StudioWindow) -> None:
        super().__init__(None)
        self.main = main
        self.data: dict[str, Any] = {"output": {"protocol": "none"}, "fixtures": []}
        self.path = ""
        self.dirty = False
        self.current: Patch | None = None
        self.problems: list[str] = []
        self.sim: LiveSession | None = None
        self._paused = False
        self._shown_patch: Patch | None = None
        self._universes: list[int] = []
        self._last_tick = time.monotonic()
        self._filling = False
        self._note = ("", 0.0)
        self._doc: Any = None
        self.found: dict[str, Any] = {}
        self.ask_library = self._ask_library
        self.ask_fixture = self._ask_fixture
        self._build()
        theme.adopt(self)
        self._restart = QTimer(self)
        self._restart.setSingleShot(True)
        self._restart.setInterval(250)
        self._restart.timeout.connect(self.restart_simulation)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.tick)
        self.boards_found.connect(self._show_found)
        self.resize(1320, 800)
        try:
            if not (main.project.patch_path and Path(main.project.patch_path).exists()):
                raise PatchError("no patch loaded")
            self.open_path(main.project.patch_path)
        except PatchError:                            # none, or one that cannot be read: start with an empty one
            self.data, self.path = {"output": {"protocol": "none"}, "fixtures": []}, ""
            self._fill()
            self._changed(dirty=False)

    # ------------------------------------------------------------------ building
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        bar = QHBoxLayout()
        for text, slot in (("New", self.new), ("Open…", self.open_dialog)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("primary")
        self.btn_save.setToolTip("Write the patch file and use it in the Editor (limits check, LIVE)")
        self.btn_save.clicked.connect(self.save)
        bar.addWidget(self.btn_save)
        save_as = QPushButton("Save as…")
        save_as.clicked.connect(self.save_as)
        bar.addWidget(save_as)
        self.lbl_file = QLabel("")
        bar.addWidget(self.lbl_file, 1)
        self.lbl_state = QLabel("")
        bar.addWidget(self.lbl_state)
        ready = QPushButton("Is the venue ready?…")
        ready.clicked.connect(self.show_ready)
        bar.addWidget(ready)
        outer.addLayout(bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._patch_side())
        split.addWidget(self._show_side())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([640, 760])
        outer.addWidget(split, 1)

    def _patch_side(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 8, 0)
        out = QGroupBox("Output")
        form = QFormLayout(out)
        self.in_protocol = QComboBox()
        for key, label in PROTOCOLS:
            self.in_protocol.addItem(label, key)
        self.in_protocol.currentIndexChanged.connect(self._output_edited)
        form.addRow("DMX goes out", self.in_protocol)
        self.stack = QStackedWidget()
        self.stack.addWidget(QLabel("The patch is only checked and simulated; nothing is sent."))
        # sACN
        page = QWidget()
        f = QFormLayout(page)
        f.setContentsMargins(0, 0, 0, 0)
        self.in_multicast = QCheckBox("Multicast (the usual way)")
        self.in_unicast = QLineEdit()
        self.in_unicast.setPlaceholderText("and / or to these addresses, comma separated")
        self.in_sacn_if = QLineEdit()
        self.in_sacn_if.setPlaceholderText("this PC's address on the DMX network (empty: the default interface)")
        f.addRow(self.in_multicast)
        f.addRow("Unicast to", self.in_unicast)
        f.addRow("Interface", self.in_sacn_if)
        self.stack.addWidget(page)
        # Art-Net
        page = QWidget()
        f = QFormLayout(page)
        f.setContentsMargins(0, 0, 0, 0)
        self.in_targets = QLineEdit()
        self.in_targets.setPlaceholderText("255.255.255.255 (everyone) or the nodes' addresses, comma separated")
        self.in_base = QSpinBox()
        self.in_base.setRange(0, 32767)
        self.in_base.setToolTip("Art-Net counts universes from 0: universe 1 of the patch is sent as this number")
        f.addRow("Send to", self.in_targets)
        f.addRow("Universe 1 is Art-Net", self.in_base)
        self.stack.addWidget(page)
        # USB
        page = QWidget()
        f = QFormLayout(page)
        f.setContentsMargins(0, 0, 0, 0)
        self.in_port = QLineEdit()
        self.in_port.setPlaceholderText("/dev/ttyUSB0 or COM3  (fourdvr fx devices lists them)")
        f.addRow("Serial port", self.in_port)
        self.stack.addWidget(page)
        # ESP32
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.in_theatre_how = QComboBox()
        self.in_theatre_how.addItem("Theatre devices: on a board of their own at", "theatre")
        self.in_theatre_how.addItem("Theatre devices: on the board of platform", "theatre_platform")
        self.in_theatre = QLineEdit()
        row.addWidget(self.in_theatre_how)
        row.addWidget(self.in_theatre, 1)
        v.addLayout(row)
        self.boards = QTableWidget(0, 2)
        self.boards.setHorizontalHeaderLabels(["Platform", "Address of its ESP32 board"])
        self.boards.verticalHeader().setVisible(False)
        self.boards.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.boards.setFixedHeight(112)
        self.boards.itemChanged.connect(lambda *_: self._output_edited())
        v.addWidget(self.boards)
        row = QHBoxLayout()
        self.btn_find = QPushButton("Find boards on the network")
        self.btn_find.clicked.connect(self.find_boards)
        self.lbl_found = QLabel("")
        self.lbl_found.setObjectName("muted")
        row.addWidget(self.btn_find)
        row.addWidget(self.lbl_found, 1)
        v.addLayout(row)
        self.found_list = QListWidget()
        self.found_list.setFixedHeight(70)
        self.found_list.setToolTip("Boards that answered. Double-click one to give it to the platform selected above\n"
                                   "(or, with no platform selected, to the theatre devices).")
        self.found_list.itemDoubleClicked.connect(self._assign_found)
        self.found_list.setVisible(False)
        v.addWidget(self.found_list)
        self.stack.addWidget(page)
        for widget in (self.in_unicast, self.in_sacn_if, self.in_targets, self.in_port, self.in_theatre):
            widget.editingFinished.connect(self._output_edited)
        self.in_multicast.toggled.connect(self._output_edited)
        self.in_base.valueChanged.connect(self._output_edited)
        self.in_theatre_how.currentIndexChanged.connect(self._output_edited)
        form.addRow(self.stack)
        self.in_refresh = QSpinBox()
        self.in_refresh.setRange(1, 44)
        self.in_refresh.setSuffix(" frames per second")
        self.in_refresh.valueChanged.connect(self._output_edited)
        form.addRow("DMX refresh", self.in_refresh)
        lay.addWidget(out)

        fx = QGroupBox("Devices")
        v = QVBoxLayout(fx)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Used for", "Where", "DMX", "What it is"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda *_: self.edit_fixture())
        self.table.setMinimumHeight(200)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("Add…")
        menu = QMenu(add)
        menu.addAction("A device from the fixture library…").triggered.connect(self.add_from_library)
        menu.addAction("A generic device (dimmer, switch, strobe)…").triggered.connect(self.add_generic)
        add.setMenu(menu)
        row.addWidget(add)
        for text, slot in (("Edit…", self.edit_fixture), ("Duplicate", self.duplicate_fixture), ("Remove", self.remove_fixture)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(fx, 1)
        self.messages = QLabel("")
        self.messages.setWordWrap(True)
        self.messages.setTextFormat(Qt.TextFormat.RichText)
        self.messages.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.messages)
        scroll.setFixedHeight(120)
        lay.addWidget(scroll)
        return inner

    def _show_side(self) -> QWidget:
        side = QWidget()
        lay = QVBoxLayout(side)
        lay.setContentsMargins(8, 0, 0, 0)
        head = QLabel(f"<b>Devices</b> &nbsp;<span style='color:{theme.FG_MUTED}'>generic stand-ins, by what each is used for - "
                      "click one to switch it on for a second</span>")
        lay.addWidget(head)
        self.tiles = DeviceTiles()
        self.tiles.clicked.connect(self.fire)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tiles)
        lay.addWidget(scroll, 5)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>DMX</b>"))
        self.in_universe = QComboBox()
        self.in_universe.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.in_universe.setMinimumWidth(230)
        self.in_universe.currentIndexChanged.connect(self._universe_chosen)
        row.addWidget(self.in_universe)
        self.lbl_dmx = QLabel("")
        self.lbl_dmx.setObjectName("muted")
        row.addWidget(self.lbl_dmx, 1)
        lay.addLayout(row)
        self.grid = DmxGrid()
        self.grid.setMaximumHeight(ROWS * 27 + 2)
        lay.addWidget(self.grid, 4)
        return side

    # ------------------------------------------------------------------ what the Editor knows
    def platform_ids(self) -> list[str]:
        return list(self.main.project.platform_ids)

    def _title(self) -> None:
        name = Path(self.path).name if self.path else "new venue patch"
        self.setWindowTitle(f"{'• ' if self.dirty else ''}{name} — Venue — {PRODUCT_TITLE}")
        self.lbl_file.setText(f"  <b>{name}</b>" + ("  (not saved)" if self.dirty else ""))

    # ------------------------------------------------------------------ file
    def _may_discard(self) -> bool:
        return not self.dirty or self.main.ask("Venue patch", "Discard the changes to this venue patch?")

    def new(self) -> None:
        if self._may_discard():
            self.data, self.path = {"output": {"protocol": "none"}, "fixtures": []}, ""
            self._fill()
            self._changed(dirty=False)

    def open_dialog(self) -> None:
        if not self._may_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Venue patch", self.path, PATCH_FILTER)
        if path:
            try:
                self.open_path(path)
            except PatchError as exc:
                QMessageBox.critical(self, "Venue patch", str(exc))

    def open_path(self, path: str) -> None:
        self.data = read_data(path)
        self.data.setdefault("output", {"protocol": "none"})
        self.data.setdefault("fixtures", [])
        self.path = str(path)
        self._fill()
        self._changed(dirty=False)

    def save(self) -> bool:
        return self.save_to(self.path) if self.path else self.save_as()

    def save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Save the venue patch", self.path or "venue.toml", PATCH_FILTER)
        return bool(path) and self.save_to(path if path.lower().endswith(".toml") else path + ".toml")

    def save_to(self, path: str) -> bool:
        """Write the file and make it the Editor's venue patch. A patch with errors is not written."""
        if self.problems:
            QMessageBox.critical(self, "Venue patch", "The patch has errors and was not saved:\n\n" + "\n".join(self.problems[:12]))
            return False
        header = (f"Venue effects patch - written by {PRODUCT_TITLE} {__version__} on {time.strftime('%Y-%m-%d %H:%M')}.\n"
                  "docs/EFFECTS_GUIDE.md explains every key; fourdvr fx check-patch FILE prints the address table.")
        try:
            write_data(path, self.data, header)
        except (PatchError, OSError) as exc:
            QMessageBox.critical(self, "Venue patch", f"Not saved: {exc}")
            return False
        self.path = str(path)
        self._changed(dirty=False)
        self.main._guarded(lambda: self.main.load_patch(self.path))
        return True

    # ------------------------------------------------------------------ the forms
    def _fill(self) -> None:
        """The widgets from the data."""
        self._filling = True
        out = self.data.get("output") or {}
        index = max(0, self.in_protocol.findData(str(out.get("protocol", "none"))))
        self.in_protocol.setCurrentIndex(index)
        self._page(index)
        self.in_refresh.setValue(round(float(out.get("refresh_hz", 40))))
        sacn, artnet, enttec, esp = (out.get(k) or {} for k in ("sacn", "artnet", "enttec", "esp32"))
        self.in_multicast.setChecked(bool(sacn.get("multicast", True)))
        self.in_unicast.setText(", ".join(str(a) for a in sacn.get("unicast", [])))
        self.in_sacn_if.setText(str(sacn.get("interface_ip", "")))
        self.in_targets.setText(", ".join(str(a) for a in artnet.get("targets", [])))
        self.in_base.setValue(int(artnet.get("universe_base", 0)))
        self.in_port.setText(str(enttec.get("port", "")))
        on_platform = bool(esp.get("theatre_platform"))
        self.in_theatre_how.setCurrentIndex(1 if on_platform else 0)
        self.in_theatre.setText(str(esp.get("theatre_platform" if on_platform else "theatre", "")))
        hosts = esp.get("platforms") or {}
        names = list(dict.fromkeys([*self.platform_ids(), *hosts]))
        self.boards.setRowCount(len(names))
        for row, name in enumerate(names):
            label = QTableWidgetItem(name)
            label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.boards.setItem(row, 0, label)
            self.boards.setItem(row, 1, QTableWidgetItem(str(hosts.get(name, ""))))
        self._filling = False

    def _page(self, index: int) -> None:
        """Show the settings of one kind of output - the box is as high as these need, not as the largest."""
        for k in range(self.stack.count()):
            page = self.stack.widget(k)
            if page is not None:
                page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred if k == index else QSizePolicy.Policy.Ignored)
        self.stack.setCurrentIndex(index)
        self.stack.adjustSize()

    def _output_edited(self) -> None:
        """The data from the output widgets."""
        if self._filling:
            return
        old = self.data.get("output") or {}
        protocol = str(self.in_protocol.currentData())
        self._page(self.in_protocol.currentIndex())
        out: dict[str, Any] = {"protocol": protocol}

        def put(table: dict[str, Any], key: str, value: Any, default: Any) -> None:
            if value != default or key in table:        # a key the file did not have is not invented for a default
                table[key] = value

        def addresses(text: str) -> list[str]:
            return [a.strip() for a in text.split(",") if a.strip()]

        put(out, "refresh_hz", self.in_refresh.value(), 40) if "refresh_hz" not in old else out.update(
            refresh_hz=self.in_refresh.value() if self.in_refresh.value() != round(float(old["refresh_hz"])) else old["refresh_hz"])
        if protocol == "sacn":
            sacn = dict(old.get("sacn") or {})
            put(sacn, "multicast", self.in_multicast.isChecked(), True)
            put(sacn, "unicast", addresses(self.in_unicast.text()), [])
            put(sacn, "interface_ip", self.in_sacn_if.text().strip(), "")
            if sacn:
                out["sacn"] = sacn
        elif protocol == "artnet":
            artnet = dict(old.get("artnet") or {})
            put(artnet, "targets", addresses(self.in_targets.text()) or ["255.255.255.255"], ["255.255.255.255"])
            put(artnet, "universe_base", self.in_base.value(), 0)
            if artnet:
                out["artnet"] = artnet
        elif protocol == "enttec-pro":
            out["enttec"] = {**(old.get("enttec") or {}), "port": self.in_port.text().strip()}
        elif protocol == "esp32":
            esp = {k: v for k, v in (old.get("esp32") or {}).items() if k not in ("theatre", "theatre_platform", "platforms")}
            if self.in_theatre.text().strip():
                esp[str(self.in_theatre_how.currentData())] = self.in_theatre.text().strip()
            hosts = {}
            for row in range(self.boards.rowCount()):
                name, host = self.boards.item(row, 0), self.boards.item(row, 1)
                if name is not None and host is not None and host.text().strip():
                    hosts[name.text()] = host.text().strip()
            if hosts:
                esp["platforms"] = hosts
            out["esp32"] = esp
        for key in ("sacn", "artnet", "enttec", "esp32"):          # what was set for another protocol is kept for a change of mind
            if key not in out and old.get(key):
                out[key] = old[key]
        if out != old:
            self.data["output"] = out
            self._changed()

    def _changed(self, dirty: bool = True) -> None:
        """After any change: check the patch, show it, and let the simulation follow."""
        self.dirty = dirty
        self._title()
        ids = self.platform_ids()
        self.current, self.problems, warnings = None, [], []
        try:
            patch = patch_from_dict(copy.deepcopy(self.data), self.path)
            self.problems = patch.problems(ids)
            warnings = patch.warnings()
            if self.data.get("output", {}).get("protocol") == "enttec-pro" and not self.in_port.text().strip():
                self.problems.append('the USB DMX interface needs its serial port')
            if not self.problems:
                self.current = patch
        except PatchError as exc:
            patch = None
            self.problems = [str(exc)]
        lines = [f"<span style='color:{theme.DANGER}'>✕ {t}</span>" for t in self.problems]
        lines += [f"<span style='color:{theme.ATTENTION}'>! {t}</span>" for t in warnings]
        if not lines:
            count = len(patch.instances(ids)) if patch is not None else 0
            lines = [f"<span style='color:{theme.SUCCESS}'>The patch is complete: {count} device(s), no address used twice.</span>"]
        self.messages.setText("<br>".join(lines))
        self.btn_save.setEnabled(not self.problems)
        self._fill_table(patch)
        self._restart.start()

    def _fill_table(self, patch: Patch | None) -> None:
        fixtures = [f for f in self.data.get("fixtures") or [] if isinstance(f, dict)]
        keep = self.table.currentRow()
        self.table.setRowCount(len(fixtures))
        parsed = {f.id: f for f in patch.fixtures} if patch is not None else {}
        for row, f in enumerate(fixtures):
            known = parsed.get(str(f.get("id")))
            footprint = known.footprint if known is not None else len(f.get("channels") or [1])
            address = int(f.get("address", 1))
            dmx = f"U{f.get('universe', 1)} · {address}" + (f"–{address + footprint - 1}" if footprint > 1 else "")
            where = "each platform" if f.get("scope") == "platform" else "theatre"
            what = str(f.get("label") or f.get("model") or f.get("profile") or ("dimmer" if not f.get("channels") else "own channels"))
            cells = (str(f.get("id", "")), effect_type(str(f.get("effect", ""))).label, where, dmx, what)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 1:
                    item.setForeground(QColor(effect_type(str(f.get("effect", ""))).colour))
                self.table.setItem(row, col, item)
        if 0 <= keep < len(fixtures):
            self.table.selectRow(keep)

    # ------------------------------------------------------------------ fixtures
    def _ids(self, but: str = "") -> set[str]:
        return {str(f.get("id")) for f in self.data.get("fixtures") or [] if isinstance(f, dict)} - {but}

    def free_id(self, effect: str) -> str:
        taken = self._ids()
        return next(f"{effect}{n}" for n in range(1, 10000) if f"{effect}{n}" not in taken)

    def free_address(self, universe: int, footprint: int) -> int:
        """The first address in the universe with this many free channels in a row (1 if there is none)."""
        used: set[int] = set()
        try:
            patch = patch_from_dict(copy.deepcopy(self.data))
            used = {ch for inst in patch.instances(self.platform_ids()) if inst.universe == universe for ch in inst.channels()}
        except PatchError:
            pass
        for start in range(1, 514 - footprint):
            if not any(ch in used for ch in range(start, start + footprint)):
                return start
        return 1

    def _ask_library(self) -> Any:
        dialog = LibraryDialog(self)
        return dialog.plan if dialog.exec() == QDialog.DialogCode.Accepted else None

    def _ask_fixture(self, entry: dict[str, Any], taken: set[str], new: bool) -> dict[str, Any] | None:
        dialog = FixtureDialog(self, entry, taken, new)
        return dialog.result_entry if dialog.exec() == QDialog.DialogCode.Accepted else None

    def add_from_library(self) -> None:
        plan = self.ask_library()
        if plan is not None:
            self.add_plan(plan)

    def add_plan(self, plan: Any, universe: int = 1) -> dict[str, Any]:
        """A device of the fixture library, at the first free address of the universe."""
        footprint = max(c["offset"] for c in plan.channels) + 1
        entry: dict[str, Any] = plan.entry(self.free_id(plan.effect), universe, self.free_address(universe, footprint))
        self.data.setdefault("fixtures", []).append(entry)
        self._changed()
        self.table.selectRow(self.table.rowCount() - 1)
        return entry

    def add_generic(self) -> None:
        entry = {"id": self.free_id("wind"), "effect": "wind", "universe": 1, "address": self.free_address(1, 1),
                 "channels": [dict(c) for c in PROFILES["dimmer"]]}
        result = self.ask_fixture(entry, self._ids(), True)
        if result is not None:
            self.data.setdefault("fixtures", []).append(result)
            self._changed()
            self.table.selectRow(self.table.rowCount() - 1)

    def edit_fixture(self) -> None:
        row = self.table.currentRow()
        fixtures = self.data.get("fixtures") or []
        if not 0 <= row < len(fixtures):
            return
        result = self.ask_fixture(fixtures[row], self._ids(str(fixtures[row].get("id"))), False)
        if result is not None:
            fixtures[row] = result
            self._changed()

    def duplicate_fixture(self) -> None:
        row = self.table.currentRow()
        fixtures = self.data.get("fixtures") or []
        if not 0 <= row < len(fixtures):
            return
        twin = copy.deepcopy(fixtures[row])
        footprint = max((int(c.get("offset", k)) for k, c in enumerate(twin.get("channels") or [{}])), default=0) + 1
        twin["id"] = self.free_id(str(twin.get("effect", "device")))
        twin["address"] = self.free_address(int(twin.get("universe", 1)), footprint)
        fixtures.insert(row + 1, twin)
        self._changed()
        self.table.selectRow(row + 1)

    def remove_fixture(self) -> None:
        row = self.table.currentRow()
        fixtures = self.data.get("fixtures") or []
        if 0 <= row < len(fixtures):
            del fixtures[row]
            self._changed()

    # ------------------------------------------------------------------ ESP32 boards
    def find_boards(self) -> None:
        from fourdvr_fx.bridge import DEFAULT_PORT
        from fourdvr_fx.ready import find_nodes

        port = int((self.data.get("output", {}).get("esp32") or {}).get("port", DEFAULT_PORT))
        self.btn_find.setEnabled(False)
        self.lbl_found.setText("asking …")
        threading.Thread(target=lambda: self.boards_found.emit(find_nodes(port, 1.5)), name="find-boards", daemon=True).start()

    def _show_found(self, found: dict[str, Any]) -> None:
        self.found = found
        self.btn_find.setEnabled(True)
        self.found_list.clear()
        for address in sorted(found):
            probe = found[address]
            item = QListWidgetItem(f"{address}   has {probe.has()}" + ("   EMERGENCY STOP IN" if probe.info and probe.info.inhibited else ""))
            item.setData(Qt.ItemDataRole.UserRole, address)
            self.found_list.addItem(item)
        self.found_list.setVisible(bool(found))
        self.lbl_found.setText(f"{len(found)} board(s) answered - double-click one to use it" if found else
                               "none answered (boards in another network segment: type the address)")

    def _assign_found(self, item: QListWidgetItem) -> None:
        address = str(item.data(Qt.ItemDataRole.UserRole))
        row = self.boards.currentRow()
        if row >= 0:
            self.boards.setItem(row, 1, QTableWidgetItem(address))
        else:
            self.in_theatre_how.setCurrentIndex(0)
            self.in_theatre.setText(address)
        self._output_edited()

    # ------------------------------------------------------------------ simulation and display
    def showEvent(self, _event: Any) -> None:
        self._timer.start()
        self._restart.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._may_discard():
            event.ignore()
            return
        self.dirty = False
        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        self._timer.stop()
        self._restart.stop()
        self.stop_simulation()

    def stop_simulation(self) -> None:
        if self.sim is not None:
            self.sim.stop()
            self.sim = None

    def pause_simulation(self, paused: bool) -> None:
        self._paused = paused
        self.restart_simulation()

    def restart_simulation(self) -> None:
        """The patch as it stands here, played to nobody: the same service, limits and timing as in the theatre."""
        self.stop_simulation()
        patch = self.current
        if patch is None or self.main.live is not None or self._paused or not self.isVisible():
            return
        quiet = copy.copy(patch)
        quiet.audio = AudioConfig(enabled=False)
        try:
            sim = LiveSession(quiet, self.platform_ids(), output=CaptureOutput(), audio_backend="null")
            sim.hold = True
            sim.set_document(self.main.project.effects)
            sim.start()
        except (ValueError, OSError):
            return
        self.sim = sim
        self.sync_transport()

    def sync_document(self) -> None:
        self._doc = self.main.project.effects
        if self.sim is not None:
            self.sim.set_document(self._doc)

    def sync_transport(self) -> None:
        if self.sim is None:
            return
        main = self.main
        if main.playing:
            self.sim.transport(True, main._anchor[1], loop=True, stamp=main._anchor[0])
        else:
            self.sim.transport(False, main.current_t)

    def editor_changed(self) -> None:
        """LIVE was switched, the platforms changed: what is shown here follows."""
        self._fill()                                  # the platforms of the Editor are the rows of the boards table
        self._changed(dirty=self.dirty)

    def fire(self, key: str) -> None:
        if self.sim is not None:
            self.sim.fire(key, 1.0, 1.0)
            self._note = (f"{key} switched on for a second - in the simulation", time.monotonic() + 2.5)
        elif self.main.live is not None:
            self._note = ("LIVE: real devices are fired from the Editor's Live tab", time.monotonic() + 4.0)

    def session(self) -> LiveSession | None:
        return self.main.live if self.main.live is not None else self.sim

    def _universe_chosen(self) -> None:
        self._owners()

    def _owners(self) -> None:
        patch, universe = self._shown_patch, self.in_universe.currentData()
        owners: dict[int, tuple[str, str, str]] = {}
        if patch is not None and universe is not None:
            for inst in patch.instances(self.platform_ids()):
                if inst.universe == universe:
                    for c in inst.fixture.channels:
                        owners[inst.address + c.offset] = (inst.key, inst.fixture.effect, c.name)
        self.grid.set_owners(owners, self.in_universe.currentText())

    def tick(self) -> None:
        now = time.monotonic()
        dt, self._last_tick = min(now - self._last_tick, 0.5), now
        if self.main.project.effects is not self._doc:      # another programme, or an undo: the simulation plays what is open
            self._doc = self.main.project.effects
            self.sync_document()
            self.sync_transport()
        session = self.session()
        live = self.main.live is not None
        if session is None:
            self.lbl_state.setText(f"<span style='color:{theme.FG_MUTED}'>"
                                   + ("The patch has errors: nothing to show" if self.problems else "Nothing is simulated") + "</span>")
            self.tiles.set_rows([])
            self.grid.set_values(b"")
            return
        self.lbl_state.setText(f"<span style='color:{theme.DANGER}'>● LIVE: what goes to the devices</span>" if live else
                               f"<span style='color:{theme.SUCCESS}'>Simulation: follows the playhead, nothing is sent</span>")
        engine = session.service.engine
        frames = session.service.universes_sent
        universes = sorted(set(frames) | {it.inst.universe for it in engine.instances})
        if session.patch is not self._shown_patch or universes != self._universes:
            self._shown_patch, self._universes = session.patch, universes
            chosen = self.in_universe.currentData()
            self.in_universe.blockSignals(True)
            self.in_universe.clear()
            for u in universes:
                self.in_universe.addItem(universe_name(session.patch, self.platform_ids(), u), u)
            self.in_universe.setCurrentIndex(max(0, self.in_universe.findData(chosen)))
            self.in_universe.blockSignals(False)
            self._owners()
        rows = [{"key": it.inst.key, "effect": it.inst.fixture.effect, "level": it.level, "rate_hz": it.rate_hz,
                 "dmx": list(it.values), "limit": it.limiter.reason,
                 "where": f"U{it.inst.universe}·{it.inst.address}"} for it in engine.instances]
        self.tiles.set_rows(rows)
        self.tiles.advance(dt)
        universe = self.in_universe.currentData()
        data = frames.get(universe, b"") if universe is not None else b""
        self.grid.set_values(data)
        lit = sum(1 for v in data if v)
        self.lbl_dmx.setText(self._note[0] if now < self._note[1] else f"{lit} of 512 channels above zero" if universe is not None else "")

    def show_ready(self) -> ReadyDialog:
        dialog = ReadyDialog(self)
        dialog.show()
        return dialog
