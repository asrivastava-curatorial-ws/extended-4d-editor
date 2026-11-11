"""Look of the Editor: GitHub's dark palette, JetBrains Mono (bundled, SIL Open Font License), flat and quiet.

One place for every colour, so the custom-painted widgets (timeline, 3D preview, plots) and the
style sheet cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

# GitHub dark (Primer) colours
CANVAS = "#0d1117"
SUBTLE = "#161b22"
INSET = "#010409"
OVERLAY = "#1c2128"
BORDER = "#30363d"
BORDER_MUTED = "#21262d"
FG = "#e6edf3"
FG_MUTED = "#8b949e"
FG_SUBTLE = "#6e7681"
ACCENT = "#58a6ff"
ACCENT_EMPHASIS = "#1f6feb"
SUCCESS = "#3fb950"
SUCCESS_EMPHASIS = "#238636"
ATTENTION = "#d29922"
DANGER = "#f85149"
DANGER_EMPHASIS = "#da3633"
DONE = "#a371f7"
PINK = "#f778ba"
CYAN = "#39c5cf"

AXIS_COLOURS = (ACCENT, SUCCESS, ATTENTION)          # surge/sway/heave and roll/pitch/yaw
FONT_FAMILY = "JetBrains Mono"
FONT_DIR = Path(__file__).resolve().parent / "fonts"

_loaded_family: str | None = None


def load_fonts() -> str:
    """Register the bundled fonts; returns the family to use (a system monospace if they are missing)."""
    global _loaded_family
    if _loaded_family is None:
        families: list[str] = []
        for ttf in sorted(FONT_DIR.glob("*.ttf")):
            font_id = QFontDatabase.addApplicationFont(str(ttf))
            if font_id >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
        _loaded_family = FONT_FAMILY if FONT_FAMILY in families else "monospace"
    return _loaded_family


def font(size: float = 10.0, bold: bool = False, medium: bool = False) -> QFont:
    f = QFont(load_fonts())
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSizeF(size)
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Medium if medium else QFont.Weight.Normal)
    return f


def colour(hex_code: str, alpha: int = 255) -> QColor:
    c = QColor(hex_code)
    c.setAlpha(alpha)
    return c


STYLE = f"""
* {{ outline: none; }}
QWidget {{ background: {CANVAS}; color: {FG}; font-family: "{FONT_FAMILY}", monospace; font-size: 12px; }}
QMainWindow::separator {{ background: {BORDER_MUTED}; width: 1px; height: 1px; }}
QToolTip {{ background: {OVERLAY}; color: {FG}; border: 1px solid {BORDER}; padding: 4px 6px; }}

QMenuBar {{ background: {INSET}; border-bottom: 1px solid {BORDER_MUTED}; padding: 2px 4px; }}
QMenuBar::item {{ background: transparent; padding: 4px 9px; border-radius: 6px; color: {FG}; }}
QMenuBar::item:selected {{ background: {SUBTLE}; }}
QMenu {{ background: {SUBTLE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_EMPHASIS}; color: #ffffff; }}
QMenu::item:disabled {{ color: {FG_SUBTLE}; }}
QMenu::separator {{ height: 1px; background: {BORDER_MUTED}; margin: 5px 4px; }}

QPushButton, QToolButton {{ background: #21262d; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 12px;
    color: {FG}; }}
QPushButton:hover, QToolButton:hover {{ background: {BORDER}; border-color: {FG_MUTED}; }}
QPushButton:pressed, QToolButton:pressed {{ background: #282e33; }}
QPushButton:disabled, QToolButton:disabled {{ color: {FG_SUBTLE}; background: {SUBTLE}; border-color: {BORDER_MUTED}; }}
QPushButton:checked, QToolButton:checked {{ background: rgba(56, 139, 253, 0.18); border-color: {ACCENT_EMPHASIS}; color: {ACCENT}; }}
QPushButton#primary {{ background: {SUCCESS_EMPHASIS}; border-color: rgba(240, 246, 252, 0.1); color: #ffffff; font-weight: 600; }}
QPushButton#primary:hover {{ background: #2ea043; }}
QPushButton#primary:disabled {{ background: rgba(35, 134, 54, 0.4); color: rgba(255, 255, 255, 0.5); }}
QPushButton#danger {{ color: {DANGER}; }}
QPushButton#danger:hover {{ background: {DANGER_EMPHASIS}; border-color: {DANGER_EMPHASIS}; color: #ffffff; }}
QPushButton#live:checked {{ background: {DANGER_EMPHASIS}; border-color: {DANGER}; color: #ffffff; font-weight: 600; }}
QPushButton#tight {{ padding: 5px 4px; }}
QToolButton::menu-indicator {{ image: none; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{ background: {CANVAS}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 4px 6px; selection-background-color: {ACCENT_EMPHASIS}; selection-color: #ffffff; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {FG_SUBTLE}; background: {SUBTLE}; }}
QSpinBox, QDoubleSpinBox {{ padding: 4px 8px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0px; border: none; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {SUBTLE}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_EMPHASIS}; }}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {FG_SUBTLE}; border-radius: 3px; background: {CANVAS}; }}
QCheckBox::indicator:checked {{ background: {ACCENT_EMPHASIS}; border-color: {ACCENT_EMPHASIS}; }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; }}

QTabWidget::pane {{ border: 1px solid {BORDER_MUTED}; border-radius: 6px; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {FG_MUTED}; padding: 6px 11px; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {FG}; }}
QTabBar::tab:selected {{ color: {FG}; border-bottom: 2px solid #f78166; font-weight: 600; }}

QTableWidget {{ background: {CANVAS}; gridline-color: {BORDER_MUTED}; border: 1px solid {BORDER_MUTED}; border-radius: 6px; }}
QTableWidget::item {{ padding: 2px 4px; }}
QTableWidget::item:selected {{ background: rgba(56, 139, 253, 0.2); color: {FG}; }}
QHeaderView::section {{ background: {SUBTLE}; color: {FG_MUTED}; padding: 5px 6px; border: none; border-bottom: 1px solid {BORDER_MUTED}; }}
QTableCornerButton::section {{ background: {SUBTLE}; border: none; }}

QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER}; border-radius: 4px; min-width: 24px; min-height: 24px; margin: 1px; }}
QScrollBar::handle:hover {{ background: #484f58; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; border: none; width: 0; height: 0; }}

QSplitter::handle {{ background: {BORDER_MUTED}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QProgressBar {{ background: {SUBTLE}; border: 1px solid {BORDER_MUTED}; border-radius: 5px; text-align: center; color: {FG_MUTED}; height: 12px; }}
QProgressBar::chunk {{ background: {ACCENT_EMPHASIS}; border-radius: 4px; }}
QStatusBar {{ background: {INSET}; border-top: 1px solid {BORDER_MUTED}; color: {FG_MUTED}; }}
QStatusBar::item {{ border: none; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0; border-radius: 6px; background: {FG_MUTED}; }}
QSlider::handle:horizontal:hover {{ background: {FG}; }}
QGroupBox {{ border: 1px solid {BORDER_MUTED}; border-radius: 6px; margin-top: 9px; padding: 8px 6px 6px 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {FG_MUTED}; }}

QLabel {{ background: transparent; }}
QLabel#muted {{ color: {FG_MUTED}; }}
QLabel#heading {{ color: {FG}; font-weight: 600; }}
QLabel#warning {{ color: {ATTENTION}; }}
QLabel#danger {{ color: {DANGER}; }}
QLabel#ok {{ color: {SUCCESS}; }}
QLabel#badge {{ background: {SUBTLE}; border: 1px solid {BORDER}; border-radius: 9px; padding: 1px 8px; color: {FG_MUTED}; }}
QFrame#bar {{ background: {INSET}; border-bottom: 1px solid {BORDER_MUTED}; }}
QFrame#panel {{ background: {CANVAS}; border: 1px solid {BORDER_MUTED}; border-radius: 6px; }}
"""


def apply(app: QApplication) -> None:
    """Fonts, palette and style sheet for the whole application."""
    family = load_fonts()
    app.setStyle("Fusion")
    base = QFont(family)
    base.setStyleHint(QFont.StyleHint.Monospace)
    base.setPointSizeF(9.5)
    app.setFont(base)
    pal = QPalette()
    for role, value in (
        (QPalette.ColorRole.Window, CANVAS), (QPalette.ColorRole.Base, CANVAS), (QPalette.ColorRole.AlternateBase, SUBTLE),
        (QPalette.ColorRole.WindowText, FG), (QPalette.ColorRole.Text, FG), (QPalette.ColorRole.ButtonText, FG),
        (QPalette.ColorRole.Button, "#21262d"), (QPalette.ColorRole.Highlight, ACCENT_EMPHASIS),
        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, OVERLAY),
        (QPalette.ColorRole.ToolTipText, FG), (QPalette.ColorRole.PlaceholderText, FG_SUBTLE), (QPalette.ColorRole.Link, ACCENT),
    ):
        pal.setColor(role, QColor(value))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
