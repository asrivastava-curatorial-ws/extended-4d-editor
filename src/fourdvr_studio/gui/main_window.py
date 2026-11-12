"""Extended 4D Editor - the desktop application.

One window for the whole job of making a programme:

  Open a video  ->  Analyse (motion on six axes, vibration and bass from the sound, effects such as wind,
  rain, fog, heat, bubbles, snow, strobe and scent from what the picture and the sound show)
  ->  edit everything on ONE timeline  ->  Check (platform limits, venue limits)  ->  Export ONE .motion file

The timeline has three groups of lanes: MOTION (intensity and the six axes), VIBRATION AND SOUND (the
sound track with its bass, the seat-vibration level) and EFFECTS (one lane per effect). Clicking a lane
shows its options next to the timeline. Everything can be tried on the real devices from the Live tab.
A programme without effects is exported as a plain motion file; effects are optional everywhere.
"""

from __future__ import annotations

import math
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QPoint, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fourdvr_extractor import scene as scene_mod
from fourdvr_extractor.cueing import AXES
from fourdvr_extractor.gui.preview3d import PlatformView
from fourdvr_extractor.manual import GESTURES, MotionEdits, insert_shape
from fourdvr_extractor.pipeline import DEFAULT_PROFILE
from fourdvr_extractor.signals import derivatives
from fourdvr_extractor.soundtrack import FEATURE_RATE, available_decoder
from fourdvr_extractor.suggest import SUGGESTABLE
from fourdvr_extractor.video import FrameReader
from fourdvr_fx import BUILTIN_TYPES, EffectsDocument, effect_type
from fourdvr_fx.dmx import DmxOutput
from fourdvr_fx.embed import read_effects, write_effects
from fourdvr_fx.model import ID_RE, RAW_DMX
from fourdvr_fx.patch import ESP32_PLATFORM_UNIVERSE, ESP32_THEATRE_UNIVERSES, Patch, default_limits, load_patch
from fourdvr_fx.preflight import default_patch, preflight

from .. import PRODUCT_NAME, PRODUCT_TITLE, __version__
from ..live import LiveSession, preview_patch
from ..project import PROJECT_SUFFIX, StudioProject
from . import theme
from .timeline import Lane, TimelineCanvas, TimelinePanel
from .venue import VenueWindow

CLASS_COLOURS = {
    "cut": (248, 81, 73, 70),
    "handheld": (139, 148, 158, 45),
    "vehicle": (88, 166, 255, 50),
    "flight": (63, 185, 80, 50),
    "rapid": (210, 153, 34, 60),
}
AUTO_PROCESS_MAX_SAMPLES = 60000        # hand-made motion is re-processed automatically up to this length
UNDO_LIMIT = 200
MEDIA_FILTER = "Video (*.mp4 *.m4v *.mov *.mkv *.webm *.ogv *.avi);;All files (*)"
OPEN_FILTER = ("Video, project, motion file or live session (*.mp4 *.m4v *.mov *.mkv *.webm *.ogv *.avi *.4dvr *.motion *.4dlive);;"
               "Video (*.mp4 *.m4v *.mov *.mkv *.webm *.ogv *.avi);;Editor project (*.4dvr);;Motion file (*.motion);;"
               "Recorded live session (*.4dlive);;All files (*)")
SESSION_SUFFIX = ".4dlive"
LIVE_CONFIG_FILTER = "Live show configuration (*.toml);;All files (*)"
STREAM_START = "Stream the video to the devices on the network…"
STREAM_STOP = "Stop streaming"
GROUP_MOTION = "Motion"
GROUP_SOUND = "Vibration and sound"
GROUP_FX = "Effects"
# what each axis is, in the words of a passenger (second line of the lane's heading)
AXIS_HINTS = {"surge": "forward / back", "sway": "left / right", "heave": "up / down",
              "roll": "tilt left / right", "pitch": "nose up / down", "yaw": "turn left / right"}
SCENE_WORDS = {"rain": "rain", "fog": "fog or smoke", "fire": "fire", "bubbles": "bubbles / underwater", "snow": "snow",
               "forest": "forest", "sea": "sea", "flowers": "flowers", "splash": "splashing water",
               "thunder": "thunder", "explosion": "explosions", "wind": "wind"}


EMPTY_TEXT = ("1  Open a video\n2  Analyse it: motion, vibration and effects are worked out\n"
              "3  Edit on the timeline, Check\n4  Export one .motion file\n\n"
              "(or open a .motion file to change its effects,\n"
              "or a recorded live session to make a programme of it)")


class Worker(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any]) -> None:
        super().__init__()
        self.fn = fn
        self.cancelled = False

    def run(self) -> None:
        try:
            result = self.fn(lambda f, m: self.progress.emit(f, m), lambda: self.cancelled)
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")


def _fmt(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


class StudioWindow(QMainWindow):
    timeline: TimelineCanvas

    def __init__(self) -> None:
        super().__init__()
        self.resize(1480, 940)
        self.project = StudioProject()
        self.reader: FrameReader | None = None
        self.current_t = 0.0
        self.playing = False
        self._anchor = (0.0, 0.0)                     # (monotonic time, programme position) of the running playback
        self._thread: QThread | None = None
        self.worker: Worker | None = None
        self._done: Callable[[Any], None] = lambda _r: None
        self._deriv: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self.dirty = False
        self.patch: Patch | None = None               # the venue patch (None: sound-only preview when live)
        self.live: LiveSession | None = None
        self.venue: VenueWindow | None = None    # the venue window: patch, devices, DMX
        self.live_show: Any = None            # the live show (fourdvr live run), started from here as its own process
        self.stream_process: Any = None       # the stream server (fourdvr stream serve), likewise
        self.attached_video: str | None = None    # the video attached to a .motion file for the preview
        self.stream_urls: tuple[str, str] = ("", "")
        self.live_output: Callable[[], DmxOutput | None] = lambda: None   # tests: capture instead of the network
        self.audio_backend = ""
        self.ask: Callable[[str, str], bool] = self._ask
        self._loading_settings = False
        self._undo: list[tuple[EffectsDocument, MotionEdits]] = []
        self._redo: list[tuple[EffectsDocument, MotionEdits]] = []
        self._undo_key: tuple[Any, float] = (None, 0.0)
        self._processed_edits: dict[str, Any] = MotionEdits().to_dict()
        self._loading_inspector = False
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self._reprocess = self._single_shot(450, self._auto_process)
        self._live_sync = self._single_shot(120, self._push_live_document)
        self._check_timer = self._single_shot(800, self.run_preflight)
        self._regenerate = self._single_shot(500, self._auto_regenerate)
        self._monitor = QTimer(self)
        self._monitor.setInterval(200)
        self._monitor.timeout.connect(self._update_monitor)
        self._build_actions()
        self._build()
        self._rebuild_lanes()
        self._refresh_buttons()
        self._refresh_effects_panel()
        self._refresh_live_panel()
        self._load_inspector()
        self._update_title()
        theme.adopt(self)

    def _single_shot(self, ms: int, slot: Callable[[], object]) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(slot)
        return t

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    # ================================================================ actions / menus
    def _action(self, text: str, slot: Callable[..., Any], shortcut: str | QKeySequence.StandardKey | None = None,
                checkable: bool = False) -> QAction:
        act = QAction(text, self)
        if shortcut is not None:
            act.setShortcut(QKeySequence(shortcut))
        act.setCheckable(checkable)
        act.triggered.connect(slot)
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction(self._action("New", self.new_project, "Ctrl+N"))
        m.addAction(self._action("Open…  (video, project, .motion file or live session)", self.open_dialog, "Ctrl+O"))
        m.addAction(self._action("Attach video for preview…", self.attach_media_dialog))
        m.addSeparator()
        self.act_save = self._action("Save project", self.save_project, "Ctrl+S")
        m.addAction(self.act_save)
        m.addAction(self._action("Save project as…", self.save_project_as, "Ctrl+Shift+S"))
        m.addSeparator()
        m.addAction(self._action("Import effects (.fx.json / .motion)…", self.import_effects_dialog))
        m.addAction(self._action("Export effects (.fx.json)…", self.export_effects_dialog))
        self.act_export = self._action("Export .motion…", self.export_dialog, "Ctrl+E")
        m.addAction(self.act_export)
        m.addSeparator()
        m.addAction(self._action("Quit", self.close, QKeySequence.StandardKey.Quit))

        m = mb.addMenu("&Edit")
        self.act_undo = self._action("Undo", self.undo, "Ctrl+Z")
        self.act_redo = self._action("Redo", self.redo, "Ctrl+Shift+Z")
        m.addAction(self.act_undo)
        m.addAction(self.act_redo)
        m.addSeparator()
        m.addAction(self._action("Cut", lambda: self._timeline_key("cut"), "Ctrl+X"))
        m.addAction(self._action("Copy", lambda: self._timeline_key("copy"), "Ctrl+C"))
        m.addAction(self._action("Paste at playhead", lambda: self._timeline_key("paste"), "Ctrl+V"))
        m.addAction(self._action("Duplicate", lambda: self._timeline_key("duplicate"), "Ctrl+D"))
        m.addAction(self._action("Delete", lambda: self._timeline_key("delete"), "Del"))
        m.addAction(self._action("Select all", lambda: self._timeline_key("all"), "Ctrl+A"))

        m = mb.addMenu("&Motion")
        self.act_auto = self._action("Automatic motion from the video", self._auto_motion_toggled, checkable=True)
        self.act_auto.setChecked(True)
        m.addAction(self.act_auto)
        self.menu_gesture = m.addMenu("Insert gesture at playhead")
        for g in GESTURES.values():
            self.menu_gesture.addAction(self._action(f"{g.label}  ({g.axis})", lambda _=False, n=g.name: self.insert_gesture(n)))
        m.addAction(self._action("Remove all manual motion", self.clear_manual_motion))
        m.addSeparator()
        m.addAction(self._action("Update the motion now", self.update_motion, "Ctrl+R"))
        m.addAction(self._action("Analyse the video again without drafting effects", lambda: self.start_analysis()))

        m = mb.addMenu("E&ffects")
        self.menu_add = m.addMenu("Add effect")
        self._fill_add_menu(self.menu_add)
        m.addAction(self._action("Suggest effects from the video again", self.suggest_effects))
        self.act_scenes = self._action("Look for rain, fog, fire, bubbles, snow and scent scenes when analysing",
                                       self._scenes_toggled, checkable=True)
        self.act_scenes.setChecked(True)
        m.addAction(self.act_scenes)
        m.addSeparator()
        m.addAction(self._action("Bass track from another audio file…", self.bass_from_file_dialog))
        m.addAction(self._action("Check the effects against the venue's limits", self.run_preflight))

        m = mb.addMenu("&Live")
        m.addAction(self._action("Venue: patch, devices and DMX…", self.show_venue, "Ctrl+Shift+D"))
        m.addAction(self._action("Is the venue ready?…", self.show_venue_ready))
        m.addSeparator()
        m.addAction(self._action("Load venue patch…", self.load_patch_dialog))
        m.addAction(self._action("Use the sound-only preview (no patch)", self.clear_patch))
        self.act_live = self._action("Go live", self.toggle_live, "Ctrl+L", checkable=True)
        m.addAction(self.act_live)
        m.addAction(self._action("Blackout", self.blackout, "Ctrl+B"))
        m.addSeparator()
        m.addAction(self._action("Live show from a game or a picture…  (live.toml)", self.start_live_show_dialog))
        m.addAction(self._action("Live show: demonstration drive (no game needed)", lambda: self.start_live_show(None)))
        m.addSeparator()
        self.act_stream = self._action(STREAM_START, self.toggle_stream)
        m.addAction(self.act_stream)

        m = mb.addMenu("&View")
        m.addAction(self._action("Play / pause", self.toggle_play, "Space"))
        m.addAction(self._action("Zoom in", lambda: self.timeline.zoom(1.4), "Ctrl++"))
        m.addAction(self._action("Zoom out", lambda: self.timeline.zoom(1 / 1.4), "Ctrl+-"))
        m.addAction(self._action("Whole programme", lambda: self.timeline.zoom_fit(), "Ctrl+0"))
        m.addAction(self._action("Fold / unfold the motion lanes", lambda: self.timeline.toggle_group(GROUP_MOTION), "M"))
        m.addAction(self._action("Fold / unfold the effect lanes", lambda: self.timeline.toggle_group(GROUP_FX), "E"))
        self.act_snap = self._action("Snap", self._toggle_snap, checkable=True)
        self.act_snap.setChecked(True)
        m.addAction(self.act_snap)

        m = mb.addMenu("&Help")
        m.addAction(self._action("How to make a programme", self.show_guide))
        m.addAction(self._action("Keys and mouse on the timeline", self.show_help, "F1"))
        m.addAction(self._action("About", self.show_about))

    def _fill_add_menu(self, menu: QMenu) -> None:
        for et in BUILTIN_TYPES.values():
            act = menu.addAction(f"{et.label}" if et.name != RAW_DMX else "DMX channel (lighting, auxiliaries)")
            act.setToolTip(et.description)
            act.triggered.connect(lambda _=False, n=et.name: self.add_track(n))
        menu.addSeparator()
        menu.addAction("Other effect type…").triggered.connect(self.add_custom_track)

    # ================================================================ layout
    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("bar")
        steps = QHBoxLayout(bar)
        steps.setContentsMargins(10, 7, 10, 7)
        steps.setSpacing(6)
        self.btn_open = QPushButton("1  Open…")
        self.btn_open.setToolTip("A video to make a programme from, a saved project, or a .motion file to change its effects  [Ctrl+O]")
        self.btn_analyse = QPushButton("2  Analyse video")
        self.btn_analyse.setObjectName("primary")
        self.btn_analyse.setToolTip("Works out everything from the video, on this computer: motion on six axes, vibration and bass,\n"
                                    "and effects (wind, rain, fog, heat, bubbles, snow, water, scent, air blasts, strobe).\n"
                                    "What you have already put on a lane is kept.")
        self.btn_check = QPushButton("3  Check")
        self.btn_check.setToolTip("Motion against the platform's limits, effects against the venue's limits")
        self.btn_export = QPushButton("4  Export .motion…")
        self.btn_export.setToolTip("ONE file with motion, vibration, bass and effects for the theatre  [Ctrl+E]")
        for b in (self.btn_open, self.btn_analyse, self.btn_check, self.btn_export):
            steps.addWidget(b)
        self.lbl_next = QLabel("")
        self.lbl_next.setObjectName("muted")
        self.lbl_next.setMinimumWidth(10)
        steps.addSpacing(8)
        steps.addWidget(self.lbl_next, 1)
        self.lbl_mode = QLabel("no programme")
        self.lbl_mode.setObjectName("badge")
        steps.addWidget(self.lbl_mode)
        self.btn_live = QPushButton("●  LIVE")
        self.btn_live.setObjectName("live")
        self.btn_live.setCheckable(True)
        self.btn_live.setToolTip("Send the effects to the venue's DMX devices (and bass to the sound card) while playing  [Ctrl+L]")
        self.btn_blackout = QPushButton("Blackout")
        self.btn_blackout.setObjectName("danger")
        self.btn_blackout.setToolTip("All effects off now  [Ctrl+B]")
        steps.addWidget(self.btn_live)
        steps.addWidget(self.btn_blackout)
        root.addWidget(bar)
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_analyse.clicked.connect(self.analyse_all)
        self.btn_check.clicked.connect(self.check_all)
        self.btn_export.clicked.connect(self.export_dialog)
        self.btn_live.clicked.connect(self.toggle_live)
        self.btn_blackout.clicked.connect(self.blackout)

        main = QSplitter(Qt.Orientation.Vertical)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self._side_tabs())
        self.video_label = QLabel(EMPTY_TEXT)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(360, 200)
        self.video_label.setStyleSheet(f"background: {theme.INSET}; color: {theme.FG_SUBTLE};")
        top.addWidget(self.video_label)
        self.view3d = PlatformView()
        self.view3d.set_profile(self.project.extraction.profile)
        top.addWidget(self.view3d)
        top.setSizes([400, 640, 440])
        main.addWidget(top)
        main.addWidget(self._bottom())
        main.setSizes([370, 560])
        main.setChildrenCollapsible(False)
        root.addWidget(main, 1)
        self.setCentralWidget(central)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.status_text = QLabel("Ready")
        self.lbl_live = QLabel("")
        self.statusBar().addWidget(self.status_text, 1)
        self.statusBar().addPermanentWidget(self.lbl_live)
        self.statusBar().addPermanentWidget(self.progress)

    def _side_tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._programme_tab(), "Programme")
        self.tabs.addTab(self._validation_tab(), "Validation")
        self.tabs.addTab(self._effects_tab(), "Effects")
        self.tabs.addTab(self._live_tab(), "Live")
        self.tabs.setMinimumWidth(340)
        return self.tabs

    def _scroll(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    def _dspin(self, lo: float, hi: float, val: float, step: float, suffix: str = "", decimals: int = 2) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(decimals)
        sp.setSingleStep(step)
        sp.setValue(val)
        sp.setSuffix(suffix)
        sp.setKeyboardTracking(False)
        return sp

    def _programme_tab(self) -> QWidget:
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setVerticalSpacing(6)
        s = self.project.extraction.settings
        self.in_pid = QSpinBox()
        self.in_pid.setRange(1, 9999)
        self.in_pid.setValue(s.programme_id)
        self.in_proj = QComboBox()
        self.in_proj.addItems(["flat", "180", "360"])
        self.in_stereo = QComboBox()
        self.in_stereo.addItems(["mono", "sbs", "ou"])
        self.in_hfov = self._dspin(20, 170, s.hfov_deg, 1, " °", 0)
        self.in_rate = QComboBox()
        self.in_rate.addItems(["50", "60", "100", "120", "200"])
        self.in_rate.setCurrentText("100")
        self.lbl_profile = QLabel(Path(DEFAULT_PROFILE).name)
        self.lbl_profile.setWordWrap(True)
        self.lbl_profile.setObjectName("muted")
        btn_profile = QPushButton("Platform profile…")
        btn_profile.clicked.connect(self.load_profile_dialog)
        c = s.cueing
        self.in_gain = self._dspin(0, 3, c.global_gain, 0.05)
        self.in_axis = {a: self._dspin(0, 3, c.axis_gains[a], 0.05) for a in AXES}
        self.in_rot = self._dspin(0, 2, c.rotation_gain, 0.05)
        self.in_force = self._dspin(0.1, 6, c.peak_specific_force, 0.1, " m/s²")
        self.in_tilt = QCheckBox("tilt coordination")
        self.in_tilt.setChecked(c.tilt_coordination)
        self.in_maxtilt = self._dspin(0, 15, c.max_tilt_deg, 0.5, " °")
        self.in_hp = self._dspin(0.05, 3, c.translation_hp_hz, 0.05, " Hz")
        self.in_vib = self._dspin(0, 3, c.vibration_gain, 0.1)
        self.in_imp = self._dspin(0, 3, c.impact_gain, 0.1)
        self.in_fade = self._dspin(0, 10, c.fade_s, 0.25, " s")
        self.in_width = QSpinBox()
        self.in_width.setRange(160, 1920)
        self.in_width.setValue(s.analysis.max_width)
        rows: list[tuple[Any, QWidget]] = [
            ("Programme ID", self.in_pid), ("Projection", self.in_proj), ("Stereo layout", self.in_stereo),
            ("Flat video HFOV", self.in_hfov), ("Motion rate (Hz)", self.in_rate), ("Analysis width (px)", self.in_width),
            (btn_profile, self.lbl_profile), ("Global gain", self.in_gain),
        ]
        rows += [(f"  {a} gain", self.in_axis[a]) for a in AXES]
        rows += [
            ("Rotation gain", self.in_rot), ("Peak specific force", self.in_force), ("", self.in_tilt),
            ("Max tilt", self.in_maxtilt), ("Washout high-pass", self.in_hp), ("Vibration gain", self.in_vib),
            ("Impact gain", self.in_imp), ("Fade in/out", self.in_fade),
        ]
        for label, widget in rows:
            form.addRow(label, widget)
        for sp in (self.in_gain, *self.in_axis.values(), self.in_rot, self.in_force, self.in_maxtilt, self.in_hp, self.in_vib,
                   self.in_imp, self.in_fade):
            sp.valueChanged.connect(lambda _v: self._motion_setting_changed())
        self.in_tilt.toggled.connect(lambda _on: self._motion_setting_changed())
        self.in_rate.currentTextChanged.connect(lambda _t: self._motion_setting_changed())
        return self._scroll(form_w)

    def _validation_tab(self) -> QWidget:
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Quantity", "Value", "Limit", "Status"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(12, 12, 12, 12)
        self.val_summary = QLabel("Not validated yet.")
        self.val_summary.setWordWrap(True)
        vl.addWidget(self.val_summary)
        vl.addWidget(self.table, 1)
        note = QLabel("Passing validation means the trajectory is inside the configured software limits. "
                      "It is not a passenger-safety certification.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        vl.addWidget(note)
        return w

    def _effects_tab(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(9)
        self.lbl_fx_summary = QLabel("")
        self.lbl_fx_summary.setWordWrap(True)
        vl.addWidget(self.lbl_fx_summary)
        found = QGroupBox("Found in the video")
        fl = QVBoxLayout(found)
        self.lbl_scene = QLabel("")
        self.lbl_scene.setWordWrap(True)
        fl.addWidget(self.lbl_scene)
        self.chk_scenes = QCheckBox("Look for effect scenes")
        self.chk_scenes.setToolTip("Rain, fog or smoke, fire, bubbles, snow and scent scenes (forest, sea, flowers) are looked for in the\n"
                                   "picture and the sound while the video is analysed. It takes a little longer and happens on this\n"
                                   "computer: nothing is sent anywhere.")
        self.chk_scenes.setChecked(self.project.detect_scenes)
        self.chk_scenes.toggled.connect(self._scenes_toggled)
        fl.addWidget(self.chk_scenes)
        self.btn_suggest = QPushButton("Suggest effects again")
        self.btn_suggest.setToolTip("Drafts the effect lanes again from the analysis: wind, seat vibration, air blasts and strobe from the\n"
                                    "camera motion and the sound; rain, fog, heat, bubbles, snow, water and scent from the scenes.\n"
                                    "It asks before it replaces anything you have on those lanes.")
        self.btn_suggest.clicked.connect(self.suggest_effects)
        row = QHBoxLayout()
        row.addWidget(self.btn_suggest)
        row.addStretch(1)
        fl.addLayout(row)
        vl.addWidget(found)

        venue = QGroupBox("Limits")
        gl = QVBoxLayout(venue)
        btn_check = QPushButton("Check the effects against the limits")
        btn_check.clicked.connect(self.run_preflight)
        gl.addWidget(btn_check)
        self.txt_check = QPlainTextEdit()
        self.txt_check.setReadOnly(True)
        self.txt_check.setPlaceholderText("Which cues the limits shorten (longest time on, pause, share of time, total per show, "
                                          "flash rate). With the venue's patch loaded (Live tab): that venue's own limits and devices.")
        self.txt_check.setMinimumHeight(110)
        self.txt_check.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.txt_check.setFont(theme.font(8.5))
        gl.addWidget(self.txt_check)
        vl.addWidget(venue, 1)
        note = QLabel("Suggested effects are a draft: look at every lane before the programme goes to an audience. Each venue "
                      "limits how long and how often its devices run; the limits are applied at playback whatever the file "
                      "asks for. DMX is not a safety function - see the effects guide.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        vl.addWidget(note)
        return self._scroll(w)

    def _live_tab(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(9)
        self.lbl_patch = QLabel("")
        self.lbl_patch.setWordWrap(True)
        vl.addWidget(self.lbl_patch)
        row = QHBoxLayout()
        btn_patch = QPushButton("Load venue patch…")
        btn_patch.clicked.connect(self.load_patch_dialog)
        btn_preview = QPushButton("Sound-only preview")
        btn_preview.clicked.connect(self.clear_patch)
        btn_venue = QPushButton("Venue…")
        btn_venue.setToolTip("Make or change the venue patch, see its devices and its DMX with the film - simulated, or LIVE  [Ctrl+Shift+D]")
        btn_venue.clicked.connect(self.show_venue)
        row.addWidget(btn_patch)
        row.addWidget(btn_preview)
        row.addWidget(btn_venue)
        row.addStretch(1)
        vl.addLayout(row)
        form = QFormLayout()
        self.in_platforms = QLineEdit(", ".join(self.project.platform_ids))
        self.in_platforms.setToolTip("Platform ids that have per-platform fixtures (wind, water, air), in the order of the patch")
        self.in_platforms.editingFinished.connect(self._platforms_changed)
        form.addRow("Platforms", self.in_platforms)
        vl.addLayout(form)
        self.chk_hold = QCheckBox("Hold levels while paused")
        self.chk_hold.setToolTip("While paused, keep the levels under the playhead on the devices - to set a fan speed or a\n"
                                 "light level by eye. The limits still apply.")
        self.chk_hold.toggled.connect(self._hold_changed)
        vl.addWidget(self.chk_hold)
        fire = QHBoxLayout()
        fire.addWidget(QLabel("Fire for"))
        self.in_fire_s = self._dspin(0.1, 10.0, 0.5, 0.1, " s", 1)
        self.in_fire_level = self._dspin(5, 100, 100, 5, " %", 0)
        fire.addWidget(self.in_fire_s)
        fire.addWidget(QLabel("at"))
        fire.addWidget(self.in_fire_level)
        fire.addStretch(1)
        vl.addLayout(fire)
        warn = QLabel("LIVE drives the real devices of the patch: fog, water, rain, heat, scent and strobe units will operate. "
                      "Clear the room of people who do not expect them.")
        warn.setWordWrap(True)
        warn.setObjectName("warning")
        vl.addWidget(warn)
        self.fixtures = QTableWidget(0, 5)
        self.fixtures.setHorizontalHeaderLabels(["Fixture", "DMX", "Level", "State", ""])
        fh = self.fixtures.horizontalHeader()
        fh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        fh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.fixtures.verticalHeader().setVisible(False)
        self.fixtures.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.fixtures.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.fixtures.cellClicked.connect(self._fixture_clicked)
        self.fixtures.setMinimumHeight(150)
        vl.addWidget(self.fixtures, 1)
        self.nodes = QTableWidget(0, 3)
        self.nodes.setHorizontalHeaderLabels(["ESP32 board of", "Address", "Has · state"])
        nh = self.nodes.horizontalHeader()
        nh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        nh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.nodes.verticalHeader().setVisible(False)
        self.nodes.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.nodes.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.nodes.setFixedHeight(84)
        self.nodes.setToolTip("The ESP32 boards of the patch. One that does not answer, or has no DMX line or no sound output,\n"
                              "is left out without an error; everything else keeps running.")
        self.nodes.setVisible(False)
        vl.addWidget(self.nodes)
        self.lbl_live_status = QLabel("")
        self.lbl_live_status.setWordWrap(True)
        self.lbl_live_status.setObjectName("muted")
        vl.addWidget(self.lbl_live_status)
        return self._scroll(w)

    def _bottom(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("bar")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(10, 5, 10, 5)
        hl.setSpacing(6)
        self.btn_play = QPushButton("▶  Play")
        self.btn_play.setMinimumWidth(92)
        self.btn_play.clicked.connect(self.toggle_play)
        hl.addWidget(self.btn_play)
        self.time_label = QLabel("00:00.00 / 00:00.00")
        self.time_label.setFont(theme.font(10.5, medium=True))
        self.time_label.setMinimumWidth(170)
        hl.addWidget(self.time_label)
        for text, tip, slot in (("−", "Zoom out", lambda: self.timeline.zoom(1 / 1.4)), ("+", "Zoom in", lambda: self.timeline.zoom(1.4)),
                                ("Fit", "Whole programme", lambda: self.timeline.zoom_fit())):
            b = QPushButton(text)
            b.setObjectName("tight")
            b.setToolTip(tip)
            b.setFixedWidth(32 if len(text) == 1 else 44)
            b.clicked.connect(slot)
            hl.addWidget(b)
        self.btn_snap = QPushButton("Snap")
        self.btn_snap.setCheckable(True)
        self.btn_snap.setChecked(True)
        self.btn_snap.setToolTip("Snap to the playhead, cue edges, scene cuts and impacts (hold Alt to move freely)  [S]")
        self.btn_snap.clicked.connect(self._toggle_snap)
        hl.addWidget(self.btn_snap)
        self.btn_add_track = QToolButton()
        self.btn_add_track.setText("+ Add effect")
        self.btn_add_track.setToolTip("A new lane on the timeline: wind, water, rain, fog, heat, bubbles, snow, air blast, strobe, scent,\n"
                                      "seat vibration, a plain DMX channel, or a type of your own")
        self.btn_add_track.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.btn_add_track)
        self._fill_add_menu(menu)
        self.btn_add_track.setMenu(menu)
        hl.addWidget(self.btn_add_track)
        self.readout = QLabel("")
        self.readout.setObjectName("muted")
        self.readout.setFont(theme.font(8.5))
        self.readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.readout.setMinimumWidth(10)
        hl.addWidget(self.readout, 1)
        vl.addWidget(bar)

        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setDocumentMode(True)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.timeline_panel = TimelinePanel()
        self.timeline = self.timeline_panel.canvas
        split.addWidget(self.timeline_panel)
        split.addWidget(self._inspector())
        split.setSizes([1180, 290])
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self.bottom_tabs.addTab(split, "Timeline")
        self.bottom_tabs.addTab(self._plots_panel(), "Motion plots")
        vl.addWidget(self.bottom_tabs, 1)
        tl = self.timeline
        tl.seekRequested.connect(self.seek)
        tl.editStarted.connect(self._push_undo)
        tl.edited.connect(self._on_edited)
        tl.selectionChanged.connect(self._load_inspector)
        tl.menuRequested.connect(self._lane_menu)
        tl.playToggled.connect(self.toggle_play)
        tl.viewChanged.connect(self._view_changed)
        return w

    def _inspector(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(250)
        vl = QVBoxLayout(w)
        vl.setContentsMargins(12, 10, 12, 10)
        vl.setSpacing(8)
        self.insp_title = QLabel("Nothing selected")
        self.insp_title.setObjectName("heading")
        self.insp_title.setWordWrap(True)
        vl.addWidget(self.insp_title)
        self.insp_about = QLabel("")                      # what this lane is and the limit that applies to it
        self.insp_about.setWordWrap(True)
        vl.addWidget(self.insp_about)
        self.insp_hint = QLabel("")
        self.insp_hint.setObjectName("muted")
        self.insp_hint.setWordWrap(True)
        vl.addWidget(self.insp_hint)

        # --- a cue (or several)
        self.box_cue = QWidget()
        f = QFormLayout(self.box_cue)
        f.setContentsMargins(0, 0, 0, 0)
        self.cue_start = self._dspin(0, 86400, 0, 0.1, " s", 3)
        self.cue_duration = self._dspin(0.04, 86400, 1, 0.1, " s", 3)
        self.cue_level = self._dspin(2, 100, 100, 5, " %", 0)
        self.cue_fade_in = self._dspin(0, 3600, 0, 0.1, " s", 3)
        self.cue_fade_out = self._dspin(0, 3600, 0, 0.1, " s", 3)
        self.cue_param = self._dspin(0, 100, 0, 1, "", 1)
        self.cue_param_label = QLabel("")
        self.cue_label = QLineEdit()
        self.cue_label.setPlaceholderText("note shown on the cue")
        f.addRow("Start", self.cue_start)
        f.addRow("Duration", self.cue_duration)
        f.addRow("Level", self.cue_level)
        f.addRow("Fade in", self.cue_fade_in)
        f.addRow("Fade out", self.cue_fade_out)
        f.addRow(self.cue_param_label, self.cue_param)
        f.addRow("Label", self.cue_label)
        for name, sp in (("t", self.cue_start), ("duration", self.cue_duration), ("level", self.cue_level),
                         ("fade_in", self.cue_fade_in), ("fade_out", self.cue_fade_out), ("param", self.cue_param)):
            sp.valueChanged.connect(lambda _v, n=name: self._apply_cue(n))
        self.cue_label.editingFinished.connect(lambda: self._apply_cue("label"))
        vl.addWidget(self.box_cue)

        # --- a curve point
        self.box_point = QWidget()
        f = QFormLayout(self.box_point)
        f.setContentsMargins(0, 0, 0, 0)
        self.pt_time = self._dspin(0, 86400, 0, 0.1, " s", 3)
        self.pt_value = self._dspin(-100, 200, 0, 5, " %", 0)
        f.addRow("Time", self.pt_time)
        f.addRow("Value", self.pt_value)
        self.pt_time.valueChanged.connect(lambda _v: self._apply_point())
        self.pt_value.valueChanged.connect(lambda _v: self._apply_point())
        vl.addWidget(self.box_point)

        # --- the track of the current lane
        self.box_track = QWidget()
        f = QFormLayout(self.box_track)
        f.setContentsMargins(0, 0, 0, 0)
        self.trk_label = QLineEdit()
        self.trk_enabled = QCheckBox("enabled")
        self.trk_param = self._dspin(0, 100, 1, 1, "", 1)
        self.trk_param_label = QLabel("")
        self.trk_universe = QSpinBox()
        self.trk_universe.setRange(1, 63999)
        self.trk_channel = QSpinBox()
        self.trk_channel.setRange(1, 512)
        self.trk_universe_label = QLabel("DMX universe")
        self.trk_channel_label = QLabel("DMX channel")
        f.addRow("Track", self.trk_label)
        f.addRow("", self.trk_enabled)
        f.addRow(self.trk_param_label, self.trk_param)
        f.addRow(self.trk_universe_label, self.trk_universe)
        f.addRow(self.trk_channel_label, self.trk_channel)
        self.trk_label.editingFinished.connect(lambda: self._apply_track("label"))
        self.trk_enabled.clicked.connect(lambda: self._apply_track("enabled"))
        self.trk_param.valueChanged.connect(lambda _v: self._apply_track("param"))
        self.trk_universe.valueChanged.connect(lambda _v: self._apply_track("dmx"))
        self.trk_channel.valueChanged.connect(lambda _v: self._apply_track("dmx"))
        row = QHBoxLayout()
        self.btn_trk_scale = QPushButton("Scale…")
        self.btn_trk_scale.setToolTip("Make everything on this lane stronger or weaker by a percentage")
        self.btn_trk_clear = QPushButton("Clear")
        self.btn_trk_delete = QPushButton("Delete")
        self.btn_trk_delete.setToolTip("Remove this lane and what is on it")
        self.btn_trk_delete.setObjectName("danger")
        self.btn_trk_scale.clicked.connect(self.scale_lane_dialog)
        self.btn_trk_clear.clicked.connect(self.clear_current_lane)
        self.btn_trk_delete.clicked.connect(self.delete_current_track)
        row.addWidget(self.btn_trk_scale)
        row.addWidget(self.btn_trk_clear)
        row.addWidget(self.btn_trk_delete)
        row.addStretch(1)
        f.addRow(row)
        vl.addWidget(self.box_track)

        # --- a motion lane (the intensity, or one of the six axes)
        self.box_motion = QWidget()
        ml = QVBoxLayout(self.box_motion)
        ml.setContentsMargins(0, 0, 0, 0)
        mf = QFormLayout()
        mf.setContentsMargins(0, 0, 0, 0)
        self.axis_amount = self._dspin(0, 3, 1, 0.05, " ×", 2)
        self.axis_amount.setToolTip("How strongly the automatic motion from the video moves this axis (0 switches it off).\n"
                                    "The platform's limits apply whatever is set here.")
        self.axis_amount_label = QLabel("Automatic motion")
        mf.addRow(self.axis_amount_label, self.axis_amount)
        self.axis_amount.valueChanged.connect(lambda _v: self._apply_axis_amount())
        ml.addLayout(mf)
        self.btn_gesture = QToolButton()
        self.btn_gesture.setText("Insert gesture at playhead")
        self.btn_gesture.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_gesture.setMenu(self.menu_gesture)
        ml.addWidget(self.btn_gesture)
        btn_clear_lane = QPushButton("Clear what was drawn here")
        btn_clear_lane.clicked.connect(self.clear_current_lane)
        ml.addWidget(btn_clear_lane)
        vl.addWidget(self.box_motion)

        # --- the sound lane: the bass track for subwoofers / bass shakers
        self.box_sound = QWidget()
        bl = QVBoxLayout(self.box_sound)
        bl.setContentsMargins(0, 0, 0, 0)
        self.chk_bass = QCheckBox("Bass track in the file")
        self.chk_bass.setToolTip("The low part of the sound track, played by subwoofers or bass shakers in step with the show")
        self.chk_bass.setChecked(self.project.bass_wanted)
        self.chk_bass.toggled.connect(self._bass_changed)
        bl.addWidget(self.chk_bass)
        cut = QHBoxLayout()
        cut.addWidget(QLabel("Low-pass"))
        self.in_bass_cut = self._dspin(40, 200, self.project.bass_cutoff_hz, 10, " Hz", 0)
        self.in_bass_cut.valueChanged.connect(self._bass_changed)
        cut.addWidget(self.in_bass_cut)
        cut.addStretch(1)
        bl.addLayout(cut)
        btn_bass_file = QPushButton("Bass from another file…")
        btn_bass_file.clicked.connect(self.bass_from_file_dialog)
        bl.addWidget(btn_bass_file)
        self.lbl_bass = QLabel("")
        self.lbl_bass.setWordWrap(True)
        self.lbl_bass.setObjectName("muted")
        bl.addWidget(self.lbl_bass)
        vl.addWidget(self.box_sound)
        vl.addStretch(1)
        return self._scroll(w)

    def _plots_panel(self) -> QWidget:
        pg.setConfigOptions(antialias=True, background=theme.CANVAS, foreground=theme.FG_MUTED)
        self.plots = pg.GraphicsLayoutWidget()
        style = {"color": theme.FG_MUTED, "size": "9pt"}
        self.p_trans = self.plots.addPlot(row=0, col=0)
        self.p_rot = self.plots.addPlot(row=1, col=0)
        self.p_util = self.plots.addPlot(row=2, col=0)
        self.p_trans.setTitle("Translation (mm): surge / sway / heave", **style)
        self.p_rot.setTitle("Rotation (°): roll / pitch / yaw", **style)
        self.p_util.setTitle("Peak use of limits (%): velocity / acceleration / jerk", **style)
        for p in (self.p_rot, self.p_util):
            p.setXLink(self.p_trans)
        pens = [pg.mkPen(c, width=1.4) for c in theme.AXIS_COLOURS]
        self.curves = {
            "trans": [self.p_trans.plot(pen=pen) for pen in pens],
            "rot": [self.p_rot.plot(pen=pen) for pen in pens],
            "util": [self.p_util.plot(pen=pen) for pen in pens],
        }
        self.playheads = []
        for p in (self.p_trans, self.p_rot, self.p_util):
            line = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(theme.FG, width=1))
            line.sigPositionChanged.connect(lambda ln: self.seek(float(ln.value()), from_plot=True))
            p.addItem(line)
            self.playheads.append(line)
            p.showGrid(x=True, y=True, alpha=0.12)
        self.regions: list[Any] = []
        return self.plots

    # ================================================================ small state helpers
    def _update_title(self) -> None:
        self.setWindowTitle(f"{self.project.title()}{' *' if self.dirty else ''}  —  {PRODUCT_TITLE} {__version__}")

    def _set_dirty(self, dirty: bool = True) -> None:
        if dirty != self.dirty:
            self.dirty = dirty
            self._update_title()

    def _duration(self) -> float:
        return self.project.duration_s

    def _motion(self) -> np.ndarray | None:
        return self.project.motion

    def _platform_ids(self) -> list[str]:
        return list(self.project.platform_ids)

    def _refresh_buttons(self, busy: bool = False) -> None:
        p = self.project
        ex = p.extraction
        video = p.mode == "video"
        self.btn_open.setEnabled(not busy)
        self.btn_analyse.setEnabled(not busy and video)
        ready = (video and ex.raw is not None) or p.mode == "motion"
        self.btn_check.setEnabled(not busy and ready)
        self.btn_export.setEnabled(not busy and ready)
        self.act_export.setEnabled(not busy and ready)
        self.btn_play.setEnabled(not busy and p.mode != "empty")
        self.btn_add_track.setEnabled(not busy and p.mode != "empty")
        self.lbl_mode.setText({"empty": "no programme", "video": "video project", "motion": "effects on an existing .motion"}[p.mode])
        if busy:
            step = "Working… the progress is shown at the bottom right."
        elif p.mode == "empty":
            step = "Start with 1: open a video."
        elif p.mode == "motion":
            step = "The motion of this file is fixed. Add or change effects on the timeline, then 4 Export."
        elif ex.raw is None:
            step = "Next: 2 Analyse video  (check the projection on the Programme tab first: flat / 180 / 360)."
        else:
            step = "Edit on the timeline below; click a lane for its options. Then 3 Check and 4 Export."
        self.lbl_next.setText(step)
        self.act_undo.setEnabled(bool(self._undo))
        self.act_redo.setEnabled(bool(self._redo))

    # ================================================================ settings (programme tab)
    def _apply_settings(self) -> None:
        s = self.project.extraction.settings
        s.programme_id = self.in_pid.value()
        s.sample_rate_hz = float(self.in_rate.currentText())
        s.hfov_deg = self.in_hfov.value()
        s.analysis.max_width = self.in_width.value()
        c = s.cueing
        c.global_gain = self.in_gain.value()
        c.axis_gains = {a: self.in_axis[a].value() for a in AXES}
        c.rotation_gain = self.in_rot.value()
        c.peak_specific_force = self.in_force.value()
        c.tilt_coordination = self.in_tilt.isChecked()
        c.max_tilt_deg = self.in_maxtilt.value()
        c.translation_hp_hz = self.in_hp.value()
        c.vibration_gain = self.in_vib.value()
        c.impact_gain = self.in_imp.value()
        c.fade_s = self.in_fade.value()
        ex = self.project.extraction
        if ex.video is not None:
            v = ex.video
            if (self.in_proj.currentText(), self.in_stereo.currentText()) != (v.projection, v.stereo) or v.hfov_deg != s.hfov_deg:
                ex.apply_format(self.in_proj.currentText(), self.in_stereo.currentText(), s.hfov_deg)
                if self.reader:
                    self.reader.stereo = self.in_stereo.currentText()

    def _settings_to_widgets(self) -> None:
        s = self.project.extraction.settings
        c = s.cueing
        self._loading_settings = True              # filling the form is not a change made by the user
        try:
            self.in_pid.setValue(s.programme_id)
            self.in_rate.setCurrentText(f"{s.sample_rate_hz:g}")
            self.in_hfov.setValue(s.hfov_deg)
            self.in_width.setValue(s.analysis.max_width)
            self.in_gain.setValue(c.global_gain)
            for a in AXES:
                self.in_axis[a].setValue(c.axis_gains[a])
            self.in_rot.setValue(c.rotation_gain)
            self.in_force.setValue(c.peak_specific_force)
            self.in_tilt.setChecked(c.tilt_coordination)
            self.in_maxtilt.setValue(c.max_tilt_deg)
            self.in_hp.setValue(c.translation_hp_hz)
            self.in_vib.setValue(c.vibration_gain)
            self.in_imp.setValue(c.impact_gain)
            self.in_fade.setValue(c.fade_s)
        finally:
            self._loading_settings = False

    def _motion_setting_changed(self) -> None:
        """A number that shapes the automatic motion was changed: the motion follows by itself."""
        if self._loading_settings or self.project.mode != "video" or self.project.extraction.raw is None:
            return
        self._set_dirty()
        self._regenerate.start()

    def _auto_regenerate(self) -> None:
        p = self.project
        if p.mode != "video" or p.extraction.raw is None:
            return
        if self._thread is not None:               # an analysis is running: try again after it
            self._regenerate.start()
            return
        if len(p.extraction.raw) > AUTO_PROCESS_MAX_SAMPLES:
            self.status_text.setText("Setting changed — Motion ▸ Update the motion now (Ctrl+R) to see the result")
            return
        self.update_motion()

    def update_motion(self) -> None:
        """Generate the motion again from the analysis with the current settings and apply the platform's limits."""
        p = self.project
        if p.mode != "video" or self._thread is not None:
            return
        if p.extraction.analysis is None and self.act_auto.isChecked():
            self.status_text.setText("Analyse the video first (button 2)")
            return
        if self._guarded(self.do_generate) and self._guarded(self.do_process):
            self._load_inspector()
            self.status_text.setText("Motion updated (3 Check validates it against the platform's limits)")

    # ================================================================ opening / saving
    def _guarded(self, fn: Callable[[], None]) -> bool:
        try:
            self._apply_settings()
            fn()
            return True
        except Exception as exc:  # noqa: BLE001 - user-facing error dialog
            QMessageBox.critical(self, "Error", str(exc))
            return False
        finally:
            self._refresh_buttons()

    def _may_discard(self) -> bool:
        return not self.dirty or self.ask("Unsaved changes", "Discard the changes to this programme?")

    def _reset_view(self) -> None:
        """After the project object changed: every panel follows."""
        if self.playing:
            self.toggle_play()
        self._undo, self._redo = [], []
        self._processed_edits = self.project.edits.to_dict()
        self._deriv = None
        self.view3d.set_profile(self.project.extraction.profile)
        self.lbl_profile.setText(Path(self.project.profile_path or DEFAULT_PROFILE).name)
        self._settings_to_widgets()
        self.chk_bass.blockSignals(True)
        self.in_bass_cut.blockSignals(True)
        self.chk_bass.setChecked(self.project.bass_wanted)
        self.in_bass_cut.setValue(self.project.bass_cutoff_hz)
        self.chk_bass.blockSignals(False)
        self.in_bass_cut.blockSignals(False)
        self.in_platforms.setText(", ".join(self.project.platform_ids))
        self.project.detect_scenes = self.chk_scenes.isChecked()      # the choice belongs to the window, not to one project
        self.table.setRowCount(0)
        self.val_summary.setText("Not validated yet.")
        self.txt_check.clear()
        self.timeline.set_held({})
        self._clear_plots()
        self._plot(self._motion())
        self._rebuild_lanes()
        self._push_reference()
        self._refresh_effects_panel()
        self._refresh_buttons()
        self._set_dirty(False)
        self._update_title()
        self.seek(0.0)

    def new_project(self) -> None:
        if not self._may_discard():
            return
        self._close_reader()
        self.project = StudioProject()
        self.video_label.setPixmap(QPixmap())
        self.video_label.setText(EMPTY_TEXT)
        self._reset_view()
        self.status_text.setText("Ready")

    def _close_reader(self) -> None:
        if self.reader:
            self.reader.close()
        self.reader = None

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open a video, a project, a .motion file or a live session", "",
                                              OPEN_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        """Whatever the file is: a project, a finished .motion file, a recorded live session, or else a video."""
        suffix = Path(path).suffix.lower()
        if suffix == PROJECT_SUFFIX:
            self.open_project(path)
        elif suffix == ".motion":
            self.open_motion(path)
        elif suffix == SESSION_SUFFIX:
            self.open_session(path)
        else:
            self.open_video(path)

    # ================================================================ live sessions and the live show
    def session_choice(self, target: Path) -> str:
        """A programme of this session exists already: "open" it, "make" it again, or "cancel"."""
        box = QMessageBox(self)
        box.setWindowTitle("Live session")
        box.setText(f"{target.name} was already made of this session.")
        box.setInformativeText("Open it as it is (with the changes made to its effects since), or make it again from "
                               "the recording? Making it again replaces the file.")
        opened = box.addButton("Open it", QMessageBox.ButtonRole.AcceptRole)
        again = box.addButton("Make it again", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return "open" if box.clickedButton() is opened else "make" if box.clickedButton() is again else "cancel"

    def session_video(self) -> str | None:
        """The video the programme is for: a path, "" for none, None to cancel."""
        box = QMessageBox(self)
        box.setWindowTitle("Live session")
        box.setText("Is there a video of this session?")
        box.setInformativeText(
            "A programme for the theatre belongs to a video: the Control Server accepts a motion file only together "
            "with the video it was made for. If the session was recorded on video (a screen or 360° capture made at "
            "the same time), choose it - the programme is made for that video and gets its length; where the video "
            "begins in the session is found by comparing the picture with the recording.\n\n"
            "Without a video the programme can be looked at, edited and tried here, but not published.")
        choose = box.addButton("Choose the video…", QMessageBox.ButtonRole.AcceptRole)
        none = box.addButton("No video", QMessageBox.ButtonRole.NoRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is none:
            return ""
        if box.clickedButton() is not choose:
            return None
        path, _ = QFileDialog.getOpenFileName(self, "Video of the session", "", MEDIA_FILTER)
        return path or None

    def open_session(self, path: str) -> None:
        """A recorded live session becomes a programme: motion with the offline cueing for the platform
        chosen in the Programme tab, the session's effects as tracks, the shakers' sound as bass track -
        written next to the session as a .motion file, which is then opened like any other."""
        if not self._may_discard():
            return
        target = Path(path).with_suffix(".motion")
        if target.exists():
            choice = self.session_choice(target)
            if choice == "cancel":
                return
            if choice == "open":
                self.dirty = False
                self.open_motion(str(target))
                return
        try:
            self._apply_settings()
        except Exception as exc:  # noqa: BLE001 - user-facing error dialog
            QMessageBox.critical(self, "Error", str(exc))
            return
        video = self.session_video()
        if video is None:
            return
        profile = self.project.extraction.profile
        programme_id = self.project.extraction.settings.programme_id

        def work(progress: Callable[[float, str], None], _cancelled: Callable[[], bool]) -> Any:
            from fourdvr_live.convert import ConvertOptions, convert_session, find_offset
            from fourdvr_live.session import Session

            session = Session.load(path)
            options = ConvertOptions(profile=profile, programme_id=programme_id)
            note = "no video: it cannot be published like this"
            if video:
                progress(0.05, f"looking for where {Path(video).name} belongs in the session")
                offset, confidence = find_offset(session, video, progress=lambda f, m: progress(0.05 + 0.5 * f, m))
                if confidence >= 0.4:
                    note = f"for {Path(video).name}, which begins at second {offset:.2f} of the session"
                else:
                    offset = 0.0
                    note = (f"for {Path(video).name} — where it begins in the session could NOT be found, it was taken to "
                            "begin with the session ('fourdvr live convert --offset' sets it)")
                options.video, options.video_offset_s = video, offset
            return convert_session(session, target, options, lambda f, m: progress(0.55 + 0.45 * f, m)), note

        def done(outcome: Any) -> None:
            result, note = outcome
            self.dirty = False
            self.open_motion(str(target))
            if video:
                self.attach_media(video)
            self.status_text.setText(
                f"{target.name}: made of the live session for {profile.platform_id}, {note} — {result.duration_s:.1f} s, "
                + (", ".join(result.effects) if result.effects else "no effects")
                + (", bass track" if result.bass else "") + ". Change the effects on the timeline, then Check and Export.")

        self._run_async(work, done, f"Making a programme of {Path(path).name}")

    def live_show_command(self, config: str | None) -> list[str]:
        """The live show runs as a process of its own: its control loop must not wait for this window."""
        base = [sys.executable, *(["-I"] if sys.flags.isolated else []), "-m", "fourdvr_studio", "live", "run", "--window"]
        return [*base, "-c", config] if config else [*base, "--source", "demo"]

    def start_live_show_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Configuration of the live show (live.toml)", "", LIVE_CONFIG_FILTER)
        if path:
            self.start_live_show(path)

    def start_live_show(self, config: str | None) -> None:
        import subprocess

        try:
            self.live_show = subprocess.Popen(self.live_show_command(config))
        except OSError as exc:
            QMessageBox.critical(self, "Live show", str(exc))
            return
        self.status_text.setText("Live show started in a window of its own" + (f" ({Path(config).name})" if config else
                                 " (demonstration drive)") + ". 'fourdvr live --help' lists what else it can do.")

    # ------------------------------------------------------------------ the video for devices on the network
    def stream_source(self) -> str | None:
        """The video that is open here: a project's own, or the one attached to a .motion file for the preview."""
        if self.project.video is not None:
            return str(self.project.video.path)
        return self.attached_video if self.reader is not None else None

    def stream_command(self, video: str, port: int, key: str) -> list[str]:
        """The stream server runs as a process of its own: it goes on while this window analyses or exports."""
        return [sys.executable, *(["-I"] if sys.flags.isolated else []), "-m", "fourdvr_studio", "stream", "serve", video,
                "--projection", self.in_proj.currentText(), "--stereo", self.in_stereo.currentText(), "--port", str(port),
                "--key", key, "--quiet"]

    def toggle_stream(self) -> None:
        if self.stream_process is not None and self.stream_process.poll() is None:
            self.stop_stream()
            self.status_text.setText("The stream has ended.")
            return
        video = self.stream_source()
        if video is None:
            QMessageBox.information(self, "Stream", "Open a video first (or attach one to the programme: File ▸ Attach video for "
                                                    "preview…). That video is then offered to the devices on the network.")
            return
        self.start_stream(video)
        if self.stream_process is not None:
            viewers, operator = self.stream_urls
            box = QMessageBox(self)
            box.setWindowTitle("Stream")
            box.setText(f"{Path(video).name} is offered to the devices on this network.")
            box.setInformativeText(
                f"Viewers open, in any browser:\n{viewers}\n\nThe operator's page - play, pause, jump, who is watching:\n{operator}\n\n"
                "Everybody who has the first page open sees the video at the same moment. 'fourdvr stream check' says whether "
                "browsers play this file as it is, and how many viewers a network carries; docs/STREAMING.md has the rest.\n\n"
                "Live ▸ Stop streaming ends it; so does closing this window.")
            box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.exec()

    def start_stream(self, video: str, port: int = 0) -> None:
        import secrets
        import socket
        import subprocess

        from fourdvr_stream.server import local_addresses

        if not port:
            for candidate in (8080, 8081, 8082, 8088, 0):         # the usual one, or the next that is free
                with socket.socket() as probe:
                    try:
                        probe.bind(("0.0.0.0", candidate))
                    except OSError:
                        continue
                    port = int(probe.getsockname()[1])
                    break
        key = secrets.token_urlsafe(6)
        try:
            self.stream_process = subprocess.Popen(self.stream_command(video, port, key))
        except OSError as exc:
            QMessageBox.critical(self, "Stream", str(exc))
            self.stream_process = None
            return
        base = f"http://{local_addresses()[0]}:{port}"
        self.stream_urls = (f"{base}/", f"{base}/control?key={key}")
        self.act_stream.setText(STREAM_STOP)
        self.status_text.setText(f"Streaming {Path(video).name}: viewers open {base}/ — operator: {base}/control?key={key}")

    def stop_stream(self) -> None:
        proc, self.stream_process = self.stream_process, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3.0)
            except Exception:  # noqa: BLE001 - it is asked to end; if it does not, it is ended
                proc.kill()
        self.act_stream.setText(STREAM_START)

    def open_video(self, path: str) -> None:
        if not self._may_discard():
            return

        def fn(progress: Callable[[float, str], None], _cancel: Callable[[], bool]) -> Any:
            progress(0.1, "reading metadata and computing SHA-256")
            self._apply_settings_safely()
            self.project.open_video(path)
            return self.project.extraction.video

        self._run_async(fn, self._video_opened, "Opening video")

    def _apply_settings_safely(self) -> None:
        s = self.project.extraction.settings
        s.projection = s.stereo = None           # a new video: guess its format again

    def _video_opened(self, info: Any) -> None:
        self.in_proj.setCurrentText(info.projection)
        self.in_stereo.setCurrentText(info.stereo)
        self._close_reader()
        self.reader = FrameReader(info.path, info.stereo)
        self._reset_view()
        self.status_text.setText(
            f"{Path(info.path).name}: {info.width}×{info.height} @ {info.fps:.2f} fps, {info.duration_s:.2f} s — "
            f"format guessed as {info.projection}/{info.stereo} (check it), SHA-256 {info.sha256[:12]}…"
        )

    def open_motion(self, path: str) -> None:
        if not self._may_discard():
            return
        if self._guarded(lambda: self.project.open_motion(path)):
            self._close_reader()
            self.video_label.setPixmap(QPixmap())
            self.video_label.setText(f"{Path(path).name}\n\nmotion from the file (not editable here)\n"
                                     "File ▸ Attach video for preview…")
            self._reset_view()
            n = len(self.project.effects.tracks)
            self.status_text.setText(f"{Path(path).name}: {self._duration():.2f} s, " + (f"{n} effect track(s)" if n else "motion only — add effects on the timeline"))

    def attach_media_dialog(self) -> None:
        if self.project.mode == "empty":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Video for preview", "", MEDIA_FILTER)
        if path:
            self.attach_media(path)

    def attach_media(self, path: str) -> None:
        """Picture and waveform for a .motion project (the motion itself stays as it is)."""
        def work() -> None:
            self._close_reader()
            self.reader = FrameReader(path, "mono")
            self.attached_video = path
            self.project.read_soundtrack(path, rebuild_bass=False)

        if self._guarded(work):
            self._rebuild_lanes()
            self._push_reference()
            self._refresh_effects_panel()
            self.seek(self.current_t)

    def open_project(self, path: str) -> None:
        if not self._may_discard():
            return

        def fn(progress: Callable[[float, str], None], cancel: Callable[[], bool]) -> Any:
            progress(0.1, "opening the project and checking its video")
            project = StudioProject.load(path)
            progress(0.6, "rebuilding the motion")
            project.restore_motion()
            if project.mode == "video":
                progress(0.8, "reading the sound track")
                project.read_soundtrack(rebuild_bass=False, cancel=cancel)
            return project

        self._run_async(fn, self._project_opened, "Opening project")

    def _project_opened(self, project: StudioProject) -> None:
        self.project = project
        self._close_reader()
        info = project.extraction.video
        if info is not None:
            self.in_proj.setCurrentText(info.projection)
            self.in_stereo.setCurrentText(info.stereo)
            self.reader = FrameReader(info.path, info.stereo)
        else:
            self.video_label.setPixmap(QPixmap())
            self.video_label.setText(f"{project.title()}\n\nFile ▸ Attach video for preview…")
        self.act_auto.setChecked(project.automatic_motion)
        if project.patch_path and Path(project.patch_path).is_file():
            try:
                self.patch = load_patch(project.patch_path)
            except (ValueError, OSError):
                self.patch = None
        self._reset_view()
        self._refresh_live_panel()
        self.status_text.setText(f"Opened {project.title()}")

    def save_project(self) -> bool:
        if self.project.mode == "empty":
            return False
        if self.project.path is None:
            return self.save_project_as()
        return self._save_to(str(self.project.path))

    def save_project_as(self) -> bool:
        if self.project.mode == "empty":
            return False
        source = self.project.motion_path or (Path(self.project.extraction.video.path) if self.project.extraction.video else Path("programme"))
        path, _ = QFileDialog.getSaveFileName(self, "Save project", str(source.with_suffix(PROJECT_SUFFIX)), f"Editor project (*{PROJECT_SUFFIX})")
        return bool(path) and self._save_to(path)

    def _save_to(self, path: str) -> bool:
        def work() -> None:
            self._apply_settings()
            saved = self.project.save(path)
            self.status_text.setText(f"Saved {saved}")

        ok = self._guarded(work)
        if ok:
            self._set_dirty(False)
            self._update_title()
        return ok

    def import_effects_dialog(self) -> None:
        if self.project.mode == "empty":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import effects", "", "Effects (*.fx.json *.json *.motion);;All files (*)")
        if path:
            self._guarded(lambda: self.import_effects(path))

    def import_effects(self, path: str) -> None:
        doc = read_effects(path)
        if doc is None:
            raise ValueError(f"{Path(path).name} has no effects (motion only)")
        self._push_undo()
        doc.duration_s = self._duration()
        if doc.bass is None:
            doc.bass = self.project.effects.bass
        self.project.effects = doc
        self._rebuild_lanes()
        self._on_edited()
        self.status_text.setText(f"Imported {len(doc.tracks)} effect track(s) from {Path(path).name}")

    def export_effects_dialog(self) -> None:
        doc = self.project.effects_for_export()
        if doc is None:
            QMessageBox.information(self, "Export effects", "This programme has no effects.")
            return
        base = self.project.motion_path or Path(self.project.extraction.video.path if self.project.extraction.video else "programme")
        path, _ = QFileDialog.getSaveFileName(self, "Export effects", str(base.with_suffix(".fx.json")), "Effects (*.fx.json)")
        if path:
            self._guarded(lambda: write_effects(path, doc))

    # ================================================================ workflow stages
    def start_analysis(self, then: Callable[[], None] | None = None) -> None:
        try:
            self._apply_settings()
        except ValueError as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return

        def done(_result: Any) -> None:
            self._analysed()
            if then is not None:
                then()

        self.project.detect_scenes = self.chk_scenes.isChecked()
        self._run_async(lambda p, c: self.project.analyse(p, c), done, "Analysing video")

    def _analysed(self) -> None:
        a = self.project.extraction.analysis
        if a is not None:
            sound = "sound track read" if self.project.soundtrack is not None else (self.project.soundtrack_note or "no sound track")
            scenes = "scenes looked for" if self.project.scene is not None else (self.project.scene_note or "scenes not looked for")
            self.status_text.setText(
                f"Analysis: {len(a.times)} frames, {len(a.cuts)} scene cuts, mean confidence {a.confidence.mean():.2f}; {sound}; {scenes}"
            )
        self._rebuild_lanes(keep_selection=True)
        self._push_reference()
        self._refresh_effects_panel()
        self._refresh_buttons()

    def do_generate(self) -> None:
        self.project.generate(automatic=self.act_auto.isChecked())
        self._plot(self.project.extraction.raw)
        self._push_reference()
        ev = self.project.extraction.events
        if ev is not None:
            self.status_text.setText("Generated cues — classes: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in ev.class_fractions().items() if v))
        else:
            self.status_text.setText("No automatic motion: draw the motion on the manual lanes (Motion menu, key M)")

    def do_process(self) -> None:
        self.project.process()
        self._processed_edits = self.project.edits.to_dict()
        self._plot(self.project.extraction.motion)
        self._push_reference()
        cr = self.project.extraction.constraint_result
        if cr is not None:
            self.status_text.setText(
                f"Processed in {cr.iterations} passes" + ("" if cr.converged else
                " (the safety margins were not fully reached everywhere; Validate shows the actual use of the limits)"))
        self.seek(self.current_t)

    def do_validate(self, show: bool = True) -> None:
        if self.project.extraction.motion is None or self._motion_stale():
            self.do_process()
        rep = self.project.validate()
        rows = rep.rows()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if c == 3:
                    item.setForeground(pg.mkColor(theme.SUCCESS if text == "OK" else theme.DANGER))
                self.table.setItem(r, c, item)
        self.val_summary.setText(
            f"<b>{'PASSED' if rep.passed else 'FAILED'}</b> against profile “{rep.profile}”. "
            + (" ".join(rep.violations) if rep.violations else "All values inside the configured limits.")
        )
        if show or not rep.passed:
            self.tabs.setCurrentIndex(1)

    def export_dialog(self) -> None:
        p = self.project
        if p.mode == "empty" or (p.mode == "video" and p.extraction.raw is None):
            return
        source = p.motion_path or Path(p.extraction.video.path)  # type: ignore[union-attr]
        default = source.with_name(source.stem + "-fx.motion") if p.mode == "motion" else source.with_suffix(".motion")
        path, _ = QFileDialog.getSaveFileName(self, "Export .motion", str(default), "Motion (*.motion)")
        if path:
            self._guarded(lambda: self._export(path))

    def _export(self, path: str) -> None:
        p = self.project
        if p.mode == "video" and (p.extraction.report is None or p.extraction.motion is None or self._motion_stale()):
            self.do_validate()
        sha = p.export(path)
        doc = p.effects_for_export()
        if doc is None:
            what = "motion only"
        else:
            what = f"{len(doc.tracks)} effect track(s)" + (" + bass track" if doc.bass is not None else "")
        self.status_text.setText(f"Exported {path} — {what} — SHA-256 {sha[:16]}…")

    def load_profile_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Platform profile", str(DEFAULT_PROFILE.parent), "TOML (*.toml)")
        if path:
            self._guarded(lambda: self._load_profile(path))

    def _load_profile(self, path: str) -> None:
        self.project.load_profile(path)
        self.view3d.set_profile(self.project.extraction.profile)
        self.lbl_profile.setText(Path(path).name)
        self._push_reference()

    def analyse_all(self) -> None:
        """Button 2: everything that can be worked out from the video, in one go - the motion (generated,
        brought inside the platform's limits and validated) and a draft of every effect lane that is still empty."""
        def after_analysis() -> None:
            if not (self._guarded(self.do_generate) and self._guarded(self.do_process)):
                return
            drafted = self._draft_effects()
            self._guarded(lambda: self.do_validate(show=False))
            rep = self.project.extraction.report
            motion = "motion not validated" if rep is None else ("motion inside the platform's limits" if rep.passed else "MOTION OUTSIDE THE LIMITS (Validation tab)")
            self.status_text.setText(f"Analysed — {motion}; " + (f"drafted: {', '.join(drafted)}" if drafted else "no effects drafted")
                                     + " — look at the lanes, then 3 Check and 4 Export")
            if rep is None or rep.passed:
                self.tabs.setCurrentIndex(2)

        if self.project.mode == "video":
            self.start_analysis(then=after_analysis)

    def run_all(self) -> None:                     # the name this had before 3.0
        self.analyse_all()

    def _draft_effects(self) -> list[str]:
        """Suggestions go onto every suggestable lane that is still empty; what a person put on a lane stays."""
        p = self.project
        free = [name for name in SUGGESTABLE if (track := p.effects.track(name)) is None or track.is_empty()]
        if not free:
            return []
        self._push_undo()
        written = p.suggest(free)
        if not written:
            self._undo.pop()
            return []
        self._rebuild_lanes()
        self._on_edited()
        self.bottom_tabs.setCurrentIndex(0)
        return written

    def check_all(self) -> None:
        """Button 3: the motion against the platform's limits, the effects against the venue's (or the default) limits."""
        p = self.project
        if p.mode == "empty":
            return
        motion = "motion: from the file, not changed here"
        failed = False
        if p.mode == "video":
            if p.extraction.raw is None or not self._guarded(lambda: self.do_validate(show=False)):
                return
            rep = p.extraction.report
            failed = rep is None or not rep.passed
            motion = "MOTION OUTSIDE THE PLATFORM'S LIMITS" if failed else "motion inside the platform's limits"
        effects, limited = self.run_preflight()
        self.status_text.setText(f"Check — {motion}; {effects}")
        self.tabs.setCurrentIndex(1 if failed or (p.mode == "video" and not limited) else 2)

    def _auto_motion_toggled(self) -> None:
        self._refresh_buttons()
        if self.project.mode == "video" and self.project.extraction.raw is not None:
            if self.act_auto.isChecked() and self.project.extraction.analysis is None:
                return
            if self._guarded(self.do_generate) and self._guarded(self.do_process):
                self._set_dirty()

    # ================================================================ background work
    def _run_async(self, fn: Callable[..., Any], done: Callable[[Any], None], title: str) -> None:
        if self._thread is not None:
            return
        self.status_text.setText(title + "…")
        self.progress.setValue(0)
        self._done = done
        self._thread = QThread(self)
        self.worker = Worker(fn)
        self.worker.moveToThread(self._thread)
        self._thread.started.connect(self.worker.run)
        # bound slots of this (GUI-thread) object => queued connections, handled on the GUI thread
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self._refresh_buttons(busy=True)
        self._thread.start()

    @Slot(float, str)
    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.status_text.setText(message)

    @Slot(object)
    def _on_finished(self, result: Any) -> None:
        self._end_worker()
        self._done(result)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._end_worker()
        self.status_text.setText("Failed")
        QMessageBox.critical(self, "Error", message)

    def _end_worker(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self.worker = None
        self.progress.setValue(0)
        self._refresh_buttons()

    # ================================================================ plots
    def _clear_plots(self) -> None:
        for group in self.curves.values():
            for c in group:
                c.setData([], [])
        for r in self.regions:
            self.p_trans.removeItem(r)
        self.regions = []

    def _plot(self, motion: np.ndarray | None) -> None:
        if motion is None:
            return
        fs = self.project.sample_rate_hz
        t = np.arange(len(motion)) / fs
        for k in range(3):
            self.curves["trans"][k].setData(t, motion[:, k] * 1000)
            self.curves["rot"][k].setData(t, np.degrees(motion[:, 3 + k]))
        v, a, j = derivatives(motion, fs)
        self._deriv = (v, a, j)
        lim = self.project.extraction.profile.limits
        for k, (d, lv) in enumerate(((v, lim.velocity), (a, lim.acceleration), (j, lim.jerk))):
            self.curves["util"][k].setData(t, np.max(np.abs(d) / lv, axis=1) * 100)
        for r in self.regions:
            self.p_trans.removeItem(r)
        self.regions = []
        ev = self.project.extraction.events
        if ev is not None:
            for t0, t1, cls in ev.segments():
                if cls not in CLASS_COLOURS:
                    continue
                region = pg.LinearRegionItem((t0, max(t1, t0 + 0.05)), movable=False, brush=pg.mkBrush(*CLASS_COLOURS[cls]))
                region.setZValue(-10)
                for line in region.lines:
                    line.setPen(pg.mkPen(None))
                self.p_trans.addItem(region)
                self.regions.append(region)

    # ================================================================ timeline content
    def _rebuild_lanes(self, keep_selection: bool = False) -> None:
        """Three groups: MOTION (what the platform does), VIBRATION AND SOUND (what is felt and heard), EFFECTS."""
        p = self.project
        lanes: list[Lane] = []
        if p.mode == "video":                      # the motion can be shaped: intensity, and one lane per axis
            lanes.append(Lane("motion:gain", "Intensity", theme.FG, "curve", GROUP_MOTION, 40, points=p.edits.lane("gain"),
                              lo=0.0, hi=2.0, default=1.0, eased=True, hint="× automatic motion"))
            for k, axis in enumerate(AXES):
                lanes.append(Lane(f"motion:{axis}", axis.capitalize(), theme.AXIS_COLOURS[k % 3], "curve", GROUP_MOTION, 40,
                                  points=p.edits.lane(axis), lo=-1.0, hi=1.0, default=0.0, eased=True, hint=AXIS_HINTS[axis], axis=k))
        elif p.mode == "motion":                   # a finished file: its motion is shown, not changed
            for k, axis in enumerate(AXES):
                lanes.append(Lane(f"view:{axis}", axis.capitalize(), theme.AXIS_COLOURS[k % 3], "view", GROUP_MOTION, 30,
                                  view="axis", axis=k))
        if p.mode != "empty":
            lanes.append(Lane("view:audio", "Sound · bass", theme.FG_MUTED, "view", GROUP_SOUND, 34, view="audio"))
        for group in (GROUP_SOUND, GROUP_FX):
            for track in p.effects.tracks:
                if (track.type == "vibration") != (group == GROUP_SOUND):
                    continue
                et = effect_type(track.type)
                hint = f"DMX {track.universe}/{track.channel}" if track.type == RAW_DMX else ""
                lanes.append(Lane(f"fx:{track.id}", track.label or et.label, et.colour, "fx", group, 50, track=track,
                                  add="point" if et.editing == "curve" else "cue", hint=hint))
        self.timeline.set_lanes(lanes, keep_selection)

    def _push_reference(self) -> None:
        p = self.project
        ex = p.extraction
        lim = ex.profile.limits
        limits = np.maximum(np.abs(lim.position_min), np.abs(lim.position_max))
        st = p.soundtrack
        wave = (st.peak_lo, st.peak_hi, st.bass, FEATURE_RATE) if st is not None and len(st.peak_hi) else None
        cuts = list(ex.analysis.cuts) if ex.analysis is not None else []
        impacts = [t for t, _m in ex.events.impacts] if ex.events is not None else []
        fps = ex.video.fps if ex.video is not None else 30.0
        self.timeline.set_duration(p.duration_s, 1.0 / max(fps, 1.0))
        self.timeline.set_reference(self._motion(), p.sample_rate_hz, limits, p.motion_is_final and not self._motion_stale(), wave, cuts, impacts)

    def _motion_stale(self) -> bool:
        return self.project.mode == "video" and self.project.edits.to_dict() != self._processed_edits

    def _view_changed(self) -> None:
        self.btn_snap.setChecked(self.timeline.snap)
        self.act_snap.setChecked(self.timeline.snap)

    def _toggle_snap(self) -> None:
        self.timeline.snap = not self.timeline.snap
        self.timeline.refresh()
        self._view_changed()

    def _timeline_key(self, what: str) -> None:
        tl = self.timeline
        if what == "delete":
            tl.delete_selection()
        elif what == "copy":
            tl.copy_selection()
        elif what == "cut":
            if tl.copy_selection():
                tl.delete_selection()
        elif what == "paste":
            tl.paste()
        elif what == "duplicate":
            if tl.copy_selection():
                ends = [c.end for _l, c in tl.selected_cues()] + [ln.pts()[i][0] + 0.02 for ln, i in tl.selected_points()]
                tl.paste(max(ends))
        elif what == "all":
            tl.select_all()

    # ================================================================ undo / change tracking
    def _push_undo(self, key: Any = None) -> None:
        """Remember the document before a change. Successive changes of one inspector field count as one step."""
        now = time.monotonic()
        if key is not None and self._undo_key[0] == key and now - self._undo_key[1] < 1.5:
            self._undo_key = (key, now)
            return
        self._undo_key = (key, now)
        self._undo.append((self.project.effects.copy(), self.project.edits.copy()))
        del self._undo[:-UNDO_LIMIT]
        self._redo = []

    def _restore(self, snapshot: tuple[EffectsDocument, MotionEdits]) -> None:
        snapshot[0].bass = self.project.effects.bass      # the bass track follows its own setting, not undo
        self.project.effects, self.project.edits = snapshot
        self._undo_key = (None, 0.0)
        self._rebuild_lanes()
        self._on_edited()

    def undo(self) -> None:
        if self._undo:
            self._redo.append((self.project.effects.copy(), self.project.edits.copy()))
            self._restore(self._undo.pop())

    def redo(self) -> None:
        if self._redo:
            self._undo.append((self.project.effects.copy(), self.project.edits.copy()))
            self._restore(self._redo.pop())

    def _on_edited(self) -> None:
        """Something in the effects or the manual motion changed."""
        self._set_dirty()
        self._refresh_effects_panel()
        self._refresh_buttons()
        self._load_inspector()
        if self._motion_stale():
            self._push_reference()
            n = len(self.project.extraction.raw) if self.project.extraction.raw is not None else 0
            if 0 < n <= AUTO_PROCESS_MAX_SAMPLES:
                self._reprocess.start()
            else:
                self.status_text.setText("Manual motion changed — Process (Ctrl+R) to see the result")
        if self.live is not None or self.venue is not None:
            self._live_sync.start()
        if self._duration() <= 1800:
            self._check_timer.start()

    def _auto_process(self) -> None:
        if self.project.mode == "video" and self.project.extraction.raw is not None and self._motion_stale() and self._thread is None:
            try:
                self.do_process()
                self.status_text.setText("Motion updated with the manual edits (still inside the platform limits: Validate to confirm)")
            except Exception as exc:  # noqa: BLE001 - shown in the status line, the edit itself stays
                self.status_text.setText(f"Could not process the manual motion: {exc}")

    # ================================================================ tracks and gestures
    def add_track(self, type_name: str) -> None:
        if self.project.mode == "empty":
            return
        self._push_undo()
        try:
            track = self.project.effects.new_track(type_name)
        except ValueError as exc:
            self._undo.pop()
            QMessageBox.critical(self, "Add effect", str(exc))
            return
        self._rebuild_lanes()
        self.timeline.current_lane = f"fx:{track.id}"
        self.timeline.refresh()
        self._on_edited()
        self.bottom_tabs.setCurrentIndex(0)

    def add_custom_track(self) -> None:
        name, ok = QInputDialog.getText(self, "Other effect type", "Name used in the venue patch (a-z, 0-9, - and _), e.g. bubbles:")
        if not ok:
            return
        name = name.strip().lower()
        if not ID_RE.match(name):
            QMessageBox.critical(self, "Other effect type", "Use lower-case letters, digits, '-' and '_' (at most 32 characters).")
            return
        self.add_track(name)

    def _current_lane(self) -> Lane | None:
        return self.timeline.lane(self.timeline.current_lane)

    def clear_current_lane(self) -> None:
        lane = self._current_lane()
        if lane is None or not lane.editable:
            return
        self._push_undo()
        if lane.track is not None:
            lane.track.cues = []
            lane.track.points = [(0.0, 0.0), (round(self._duration(), 4), 0.0)] if lane.add == "point" else []
        else:
            lane.set_pts([])
        self._rebuild_lanes()
        self._on_edited()

    def scale_lane_dialog(self) -> None:
        lane = self._current_lane()
        if lane is None or lane.track is None:
            return
        percent, ok = QInputDialog.getDouble(self, "Scale the lane", "Levels of every cue and curve point on this lane, in percent of what they are now:",
                                             100.0, 0.0, 400.0, 0)
        if ok:
            self.scale_current_lane(percent / 100.0)

    def scale_current_lane(self, factor: float) -> None:
        """Every level on the lane times `factor` (cues and curve points; nothing goes above 100 %)."""
        lane = self._current_lane()
        if lane is None or lane.track is None or factor < 0 or abs(factor - 1.0) < 1e-9:
            return
        self._push_undo()
        for cue in lane.track.cues:
            cue.level = round(min(1.0, max(0.02, cue.level * factor)), 3)
        lane.track.points = [(t, round(min(1.0, v * factor), 4)) for t, v in lane.track.points]
        self.timeline.refresh()
        self._on_edited()

    def delete_current_track(self) -> None:
        lane = self._current_lane()
        if lane is None or lane.track is None:
            return
        self._push_undo()
        self.project.effects.tracks = [t for t in self.project.effects.tracks if t is not lane.track]
        self._rebuild_lanes()
        self._on_edited()

    def clear_manual_motion(self) -> None:
        if self.project.edits.is_empty() and not self.project.edits.gain and not any(self.project.edits.offsets.values()):
            return
        self._push_undo()
        self.project.edits = MotionEdits()
        self._rebuild_lanes()
        self._on_edited()

    def insert_gesture(self, name: str, at: float | None = None) -> None:
        if self.project.mode != "video":
            self.status_text.setText("Gestures need a video project (the motion of a .motion file is fixed)")
            return
        g = GESTURES[name]
        t = self.current_t if at is None else at
        if t + g.shape[-1][0] > self._duration():
            self.status_text.setText("Not enough time left in the programme for this gesture")
            return
        self._push_undo()
        pts = self.project.edits.lane(g.axis)
        if not pts:
            pts.extend([(0.0, 0.0), (round(self._duration(), 3), 0.0)])
        pts[:] = insert_shape(pts, round(t, 3), g.shape, 0.0)
        self._rebuild_lanes()
        self.timeline.current_lane = f"motion:{g.axis}"
        self.timeline.refresh()
        self._on_edited()
        self.status_text.setText(f"{g.label} on {g.axis} at {_fmt(t)} — drag its points to shape it")

    def suggest_effects(self) -> None:
        p = self.project
        if p.mode == "empty":
            return
        if p.extraction.events is None and p.extraction.analysis is None and p.soundtrack is None:
            QMessageBox.information(self, "Suggest effects", "Analyse the video first (or attach a video with sound): "
                                    "suggestions come from the camera motion, the picture and the sound track.")
            return
        busy = [t.id for t in p.effects.tracks if t.id in SUGGESTABLE and not t.is_empty()]
        if busy and not self.ask("Suggest effects", f"Replace what is on the lane(s) {', '.join(busy)} with new suggestions?"):
            return
        self._push_undo()
        written = p.suggest()
        self._rebuild_lanes()
        self._on_edited()
        notes = "; ".join(f"{s.type}: {len(s.cues)} cue(s)" if s.cues else f"{s.type}: curve, {s.note}" for s in p.suggestions)
        self.status_text.setText(f"Suggested {notes}" if written else
                                 "Nothing to suggest for this programme (no forward motion, shake, impacts, flashes or effect scenes found)")
        self.bottom_tabs.setCurrentIndex(0)

    def _scenes_toggled(self, on: bool) -> None:
        for w in (self.act_scenes, self.chk_scenes):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.project.detect_scenes = on
        self._refresh_effects_panel()

    def _lane_menu(self, key: str, t: float, value: float, where: QPoint) -> None:
        lane = self.timeline.lane(key)
        if lane is None:
            return
        tl = self.timeline
        menu = QMenu(self)
        if lane.kind == "view":
            menu.addAction("Playhead here").triggered.connect(lambda: self.seek(t))
        else:
            snapped = tl.snap_time(t)
            if lane.track is not None:
                menu.addAction("Add cue here").triggered.connect(lambda: tl.add_cue(lane, snapped))
            menu.addAction("Add curve point here").triggered.connect(lambda: tl.add_point(lane, snapped, value))
            if lane.kind == "curve" and lane.key != "motion:gain":
                sub = menu.addMenu("Insert gesture here")
                axis = lane.key.split(":", 1)[1]
                for g in GESTURES.values():
                    if g.axis == axis:
                        sub.addAction(g.label).triggered.connect(lambda _=False, n=g.name: self.insert_gesture(n, snapped))
            menu.addSeparator()
            act = menu.addAction("Delete selected")
            act.setEnabled(tl.has_selection())
            act.triggered.connect(tl.delete_selection)
            menu.addAction("Paste here").triggered.connect(lambda: tl.paste(snapped))
            menu.addAction("Select everything on this lane").triggered.connect(tl.select_all)
            menu.addSeparator()
            if lane.track is not None:
                act = menu.addAction("Enabled")
                act.setCheckable(True)
                act.setChecked(lane.track.enabled)
                act.triggered.connect(lambda: self._toggle_track(lane))
                menu.addAction("Clear track").triggered.connect(self.clear_current_lane)
                menu.addAction("Delete track").triggered.connect(self.delete_current_track)
            else:
                menu.addAction("Clear lane").triggered.connect(self.clear_current_lane)
        menu.exec(where)

    def _toggle_track(self, lane: Lane) -> None:
        if lane.track is not None:
            self._push_undo()
            lane.track.enabled = not lane.track.enabled
            self.timeline.refresh()
            self._on_edited()

    # ================================================================ inspector
    def _load_inspector(self) -> None:
        tl = self.timeline
        cues, points, lane = tl.selected_cues(), tl.selected_points(), self._current_lane()
        self._loading_inspector = True
        try:
            self.box_cue.setVisible(bool(cues))
            self.box_point.setVisible(not cues and len(points) == 1)
            is_track = not cues and not points and lane is not None and lane.track is not None
            self.box_track.setVisible(bool(is_track))
            is_motion = not cues and len(points) != 1 and lane is not None and lane.kind == "curve"
            self.box_motion.setVisible(is_motion)
            is_sound = not cues and not points and lane is not None and lane.key == "view:audio"
            self.box_sound.setVisible(is_sound)
            about = self._about(cues[0][0] if cues else points[0][0] if len(points) == 1 else lane if not points else None)
            self.insp_about.setText(about)
            self.insp_about.setVisible(bool(about))
            self.insp_hint.setText("")
            self.insp_hint.setVisible(True)
            if cues:
                ln, cue = cues[0]
                assert ln.track is not None
                et = effect_type(ln.track.type)
                many = len(cues) > 1
                self.insp_title.setText(f"{len(cues)} cues" if many else f"{ln.label} · cue")
                self.insp_hint.setText("Level, fades and the parameter apply to all selected cues." if many else "")
                self.cue_start.setEnabled(not many)
                self.cue_start.setMaximum(max(0.0, self._duration()))
                self.cue_duration.setEnabled(not many)
                self.cue_label.setEnabled(not many)
                self.cue_start.setValue(cue.t)
                self.cue_duration.setValue(cue.duration)
                self.cue_level.setValue(cue.level * 100)
                self.cue_fade_in.setValue(cue.fade_in)
                self.cue_fade_out.setValue(cue.fade_out)
                self.cue_label.setText(cue.label)
                spec = et.params[0] if et.params else None
                self.cue_param.setVisible(spec is not None)
                self.cue_param_label.setVisible(spec is not None)
                if spec is not None:
                    self.cue_param_label.setText(spec.label)
                    self.cue_param.setRange(spec.minimum, spec.maximum)
                    self.cue_param.setSingleStep(spec.step)
                    self.cue_param.setDecimals(0 if float(spec.step).is_integer() else 1)
                    self.cue_param.setSuffix(f" {spec.unit}" if spec.unit else "")
                    self.cue_param.setValue(cue.params.get(spec.name, ln.track.params.get(spec.name, spec.default)))
                    if spec.name == "rate_hz":
                        self.insp_hint.setText((self.insp_hint.text() + " " if many else "") +
                                               "The venue patch caps the flash rate (3 per second unless assessed otherwise).")
            elif len(points) == 1:
                ln, i = points[0]
                t, v = ln.pts()[i]
                self.insp_title.setText(f"{ln.label} · curve point")
                self.pt_time.setMaximum(max(0.0, self._duration()))
                self.pt_time.setValue(t)
                if ln.key == "motion:gain":
                    self.pt_value.setRange(0, 200)
                elif ln.kind == "curve":
                    self.pt_value.setRange(-100, 100)
                else:
                    self.pt_value.setRange(0, 100)
                self.pt_value.setValue(v * 100)
            elif points:
                self.insp_title.setText(f"{len(points)} curve points")
                self.insp_hint.setText("Drag to move them together; arrow keys nudge (Shift: coarse).")
            elif is_track and lane is not None and lane.track is not None:
                tr = lane.track
                et = effect_type(tr.type)
                self.insp_title.setText(f"{et.label} · track")
                self.insp_hint.setText(("Double-click the lane to add a curve point (Alt: a cue)." if lane.add == "point"
                                        else "Double-click the lane to add a cue (Alt: a curve point).")
                                       + ("" if tr.type in BUILTIN_TYPES else " Venue-defined type: it plays where the patch has a fixture with this effect name."))
                self.trk_label.setText(tr.label)
                self.trk_enabled.setChecked(tr.enabled)
                spec = et.params[0] if et.params else None
                self.trk_param.setVisible(spec is not None)
                self.trk_param_label.setVisible(spec is not None)
                if spec is not None:
                    self.trk_param_label.setText(f"{spec.label} (new cues)")
                    self.trk_param.setRange(spec.minimum, spec.maximum)
                    self.trk_param.setSingleStep(spec.step)
                    self.trk_param.setDecimals(0 if float(spec.step).is_integer() else 1)
                    self.trk_param.setSuffix(f" {spec.unit}" if spec.unit else "")
                    self.trk_param.setValue(tr.params.get(spec.name, spec.default))
                dmx = tr.type == RAW_DMX
                for wdg in (self.trk_universe, self.trk_channel, self.trk_universe_label, self.trk_channel_label):
                    wdg.setVisible(dmx)
                if dmx:
                    self.trk_universe.setValue(max(1, tr.universe))
                    self.trk_channel.setValue(max(1, tr.channel))
            elif lane is not None and lane.kind == "curve":
                name = lane.key.split(":", 1)[1]
                self.insp_title.setText(f"{lane.label} · motion")
                self.insp_hint.setText("Double-click the lane to add a point: the curve scales the automatic motion over time." if name == "gain"
                                       else "The lane shows what the platform does on this axis. Double-click to add a point, or insert a "
                                            "ready-made gesture: what you draw is added (±100 % = the axis' travel).")
                self.btn_gesture.setVisible(name != "gain")
                self.axis_amount_label.setText("Automatic motion, all axes" if name == "gain" else "Automatic motion")
                self.axis_amount.setValue((self.in_gain if name == "gain" else self.in_axis[name]).value())
                self.axis_amount.setEnabled(self.act_auto.isChecked())
            elif is_sound:
                self.insp_title.setText("Sound · bass")
            elif lane is not None and lane.kind == "view" and lane.axis >= 0:
                self.insp_title.setText(f"{lane.label} · motion")
                self.insp_hint.setText("Open the video itself (1 Open…) to make the motion anew and edit it.")
            else:
                self.insp_title.setText("Nothing selected")
                self.insp_hint.setText("Click a lane to see its options, or a cue or a curve point to edit it.\n"
                                       "+ Add effect starts a new lane." if self.project.mode != "empty" else "")
            self.insp_hint.setVisible(bool(self.insp_hint.text()))
        finally:
            self._loading_inspector = False

    def _about(self, lane: Lane | None) -> str:
        """What the lane is, in a sentence, and the limit that applies to it."""
        if lane is None:
            return ""
        p = self.project
        if lane.track is not None:
            et = effect_type(lane.track.type)
            what = et.description[:1].upper() + et.description[1:] if et.description else "An effect type of this venue"
            return f"{what}.<br><span style='color:{theme.FG_MUTED}'>{self._limit_text(lane.track.type)}</span>"
        if lane.key == "view:audio":
            return ("The sound of the video (grey) and its bass (pink). The bass is what subwoofers and bass shakers play; "
                    "the seat-vibration lane adds a rumble of its own." if p.soundtrack is not None else
                    "No sound track is loaded. A bass track for subwoofers and bass shakers can be made from the video's sound or from another file.")
        if lane.key == "motion:gain":
            return ("How much of the automatic motion is used over time: 0 = the platform rests, 1 = as analysed, 2 = twice as much. "
                    "The platform's limits apply to the result.")
        if lane.axis >= 0:
            lim = p.extraction.profile.limits
            k = lane.axis
            travel = min(abs(float(lim.position_min[k])), abs(float(lim.position_max[k])))
            unit = (1000.0, "mm") if k < 3 else (180.0 / math.pi, "°")
            text = (f"{AXIS_HINTS[AXES[k]].capitalize()}. This platform: travel ±{travel * unit[0]:.0f} {unit[1]}, "
                    f"speed ≤ {float(lim.velocity[k]) * unit[0]:.0f} {unit[1]}/s, acceleration ≤ {float(lim.acceleration[k]) * unit[0]:.0f} {unit[1]}/s².")
            return text + (" From the .motion file: it is not changed here." if lane.kind == "view" else "")
        return ""

    def _limit_text(self, type_name: str) -> str:
        if type_name == RAW_DMX:
            return "A plain DMX channel: no limit is applied to it. Use it for lighting, not for fog, water or heat."
        if type_name == "vibration":
            return "Played by bass shakers through the sound output; its level is set by the venue's gain."
        if self.patch is not None:
            fixtures = [f for f in self.patch.fixtures if f.effect == type_name]
            if not fixtures:
                return "This venue's patch has no device for this effect: it is left out there, everything else plays."
            limits = sorted({f.limits.describe() for f in fixtures})
            return "Limit at this venue: " + "; ".join(limits) + "."
        if type_name not in BUILTIN_TYPES:
            return "The limit comes from the venue's patch."
        return f"Default limit: {default_limits(type_name).describe()}. A venue's patch can set its own."

    def _apply_axis_amount(self) -> None:
        """The amount of automatic motion on the lane's axis (the same number as on the Programme tab)."""
        if self._loading_inspector:
            return
        lane = self._current_lane()
        if lane is None or lane.kind != "curve":
            return
        name = lane.key.split(":", 1)[1]
        box = self.in_gain if name == "gain" else self.in_axis[name]
        if abs(box.value() - self.axis_amount.value()) > 1e-9:
            box.setValue(self.axis_amount.value())             # -> _motion_setting_changed: the motion follows

    def _apply_cue(self, name: str) -> None:
        if self._loading_inspector:
            return
        cues = self.timeline.selected_cues()
        if not cues:
            return
        self._push_undo(("cue", name, tuple(id(c) for _l, c in cues)))
        total = self._duration()
        for ln, cue in cues:
            assert ln.track is not None
            if name == "t" and len(cues) == 1:
                cue.t = round(min(self.cue_start.value(), max(0.0, total - cue.duration)), 4)
            elif name == "duration" and len(cues) == 1:
                cue.duration = round(max(0.04, min(self.cue_duration.value(), total - cue.t)), 4)
                cue.fade_in = min(cue.fade_in, cue.duration)
                cue.fade_out = min(cue.fade_out, cue.duration - cue.fade_in)
            elif name == "level":
                cue.level = round(self.cue_level.value() / 100.0, 3)
            elif name == "fade_in":
                cue.fade_in = round(min(self.cue_fade_in.value(), cue.duration - cue.fade_out), 3)
            elif name == "fade_out":
                cue.fade_out = round(min(self.cue_fade_out.value(), cue.duration - cue.fade_in), 3)
            elif name == "param":
                specs = effect_type(ln.track.type).params
                if specs:
                    cue.params[specs[0].name] = float(self.cue_param.value())
            elif name == "label" and len(cues) == 1:
                cue.label = self.cue_label.text().strip()[:60]
        self.timeline.refresh()
        self._on_edited()

    def _apply_point(self) -> None:
        if self._loading_inspector:
            return
        points = self.timeline.selected_points()
        if len(points) != 1:
            return
        ln, i = points[0]
        pts = list(ln.pts())
        self._push_undo(("point", ln.key, i))
        lo = pts[i - 1][0] + 0.01 if i > 0 else 0.0
        hi = pts[i + 1][0] - 0.01 if i + 1 < len(pts) else self._duration()
        t = round(min(max(self.pt_time.value(), lo), max(lo, hi)), 4)
        pts[i] = (t, round(min(ln.hi, max(ln.lo, self.pt_value.value() / 100.0)), 4))
        ln.set_pts(pts)
        self.timeline.refresh()
        self._on_edited()

    def _apply_track(self, name: str) -> None:
        if self._loading_inspector:
            return
        lane = self._current_lane()
        if lane is None or lane.track is None:
            return
        tr = lane.track
        self._push_undo(("track", name, tr.id))
        if name == "label":
            tr.label = self.trk_label.text().strip()[:40]
        elif name == "enabled":
            tr.enabled = self.trk_enabled.isChecked()
        elif name == "param":
            specs = effect_type(tr.type).params
            if specs:
                tr.params[specs[0].name] = float(self.trk_param.value())
        elif name == "dmx":
            tr.universe, tr.channel = self.trk_universe.value(), self.trk_channel.value()
        self._rebuild_lanes(keep_selection=True)
        self._on_edited()

    # ================================================================ effects panel / bass / preflight
    def _refresh_effects_panel(self) -> None:
        p = self.project
        doc = p.effects
        cues = sum(len(t.cues) for t in doc.tracks)
        curves = sum(1 for t in doc.tracks if any(v > 0 for _t, v in t.points))
        if p.mode == "empty":
            self.lbl_fx_summary.setText("Open a video or a .motion file to add effects.")
        elif not doc.tracks and doc.bass is None:
            self.lbl_fx_summary.setText("<b>Motion only.</b> No effects: the programme plays on any platform as it is.")
        else:
            self.lbl_fx_summary.setText(f"<b>{len(doc.tracks)} effect track(s)</b> · {cues} cue(s) · {curves} level curve(s)"
                                        + (" · bass track" if doc.bass is not None else ""))
        self.lbl_scene.setText(self._scene_text())
        if doc.bass is not None:
            b = doc.bass
            self.lbl_bass.setText(f"{b.duration_s:.1f} s, mono {b.sample_rate_hz} Hz, {len(b.samples) * 2 // 1024} KiB — {b.source}")
        elif p.soundtrack_note:
            self.lbl_bass.setText(p.soundtrack_note)
        elif p.soundtrack is None and p.mode == "video":
            self.lbl_bass.setText("Read when the video is analysed (button 2)." if available_decoder() else
                                  "No audio decoder on this installation: install the 'av' package or ffmpeg to get a bass track.")
        else:
            self.lbl_bass.setText("No bass track.")
        self.btn_suggest.setEnabled(p.mode != "empty")

    def _scene_text(self) -> str:
        """What the picture and the sound of the video show, as far as the bundled models can tell."""
        p = self.project
        if p.mode == "empty":
            return "Open a video: rain, fog, fire, bubbles, snow and scent scenes are looked for when it is analysed."
        if p.mode == "motion":
            return "This is a finished .motion file. Scenes are looked for in a video (1 Open… the video itself)."
        if p.scene is None:
            if p.extraction.analysis is not None:
                return (p.scene_note or "Scenes were not looked for.").capitalize()
            if not self.chk_scenes.isChecked():
                return "Not analysed yet. Scenes will not be looked for (switched off below)."
            ok, why = scene_mod.availability()
            return ("Not analysed yet. Scenes will be looked for in the picture and the sound, on this computer." if ok
                    else f"Not analysed yet. Scenes cannot be looked for here: {why}.")
        s = p.scene.summary()
        seen: dict[str, float] = s["seconds_seen"]      # type: ignore[assignment]
        heard: dict[str, float] = s["seconds_heard"]    # type: ignore[assignment]

        def listing(found: dict[str, float]) -> str:
            return ", ".join(f"{SCENE_WORDS.get(k, k)} {v:g} s" for k, v in sorted(found.items(), key=lambda kv: -kv[1]))

        parts = [f"<b>Seen:</b> {listing(seen)}." if seen else
                 f"Nothing of rain, fog, fire, bubbles, snow, forest, sea or flowers was seen in {s['pictures']} pictures (one a second)."]
        if heard:
            parts.append(f"<b>Heard:</b> {listing(heard)}.")
        elif s["sound"]:
            parts.append("Nothing of it was heard in the sound.")
        if p.scene.note:
            parts.append(p.scene.note.capitalize() + ".")
        return " ".join(parts)

    def _bass_changed(self) -> None:
        p = self.project
        wanted, cutoff = self.chk_bass.isChecked(), float(self.in_bass_cut.value())
        if (wanted, cutoff) == (p.bass_wanted, p.bass_cutoff_hz):
            return
        p.bass_wanted, p.bass_cutoff_hz = wanted, cutoff
        self._guarded(p.refresh_bass)
        self._on_edited()

    def bass_from_file_dialog(self) -> None:
        if self.project.mode == "empty":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Audio for the bass track", "", "Media (*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.mp4 *.mov *.mkv);;All files (*)")
        if path:
            self.bass_from_file(path)

    def bass_from_file(self, path: str) -> None:
        def work() -> None:
            self.project.bass_wanted = True
            self.project.read_soundtrack(path)
            if self.project.soundtrack is None:
                raise ValueError(self.project.soundtrack_note or "no sound in that file")

        if self._guarded(work):
            self.chk_bass.blockSignals(True)
            self.chk_bass.setChecked(True)
            self.chk_bass.blockSignals(False)
            self._rebuild_lanes(keep_selection=True)
            self._push_reference()
            self._on_edited()

    def run_preflight(self) -> tuple[str, int]:
        """Play the effects through the venue's patch on paper - or, while none is loaded, through the default
        limits - and mark on the timeline what the limits will shorten. Returns (summary, devices limited)."""
        p = self.project
        doc = p.effects_for_export() if p.mode != "empty" else None
        if doc is None:
            self.txt_check.setPlainText("No effects in this programme." if p.mode != "empty" else "")
            self.timeline.set_held({})
            return "no effects", 0
        venue = self.patch
        try:
            report = preflight(doc, venue if venue is not None else default_patch(t.type for t in doc.tracks), self._platform_ids())
        except ValueError as exc:
            self.txt_check.setPlainText(f"The patch cannot be used: {exc}")
            return "the venue patch cannot be used", 0
        if venue is None:
            report.unpatched = []                   # no venue yet: no device is "missing"
        held: dict[str, list[tuple[float, float, str]]] = {}
        for f in report.fixtures:
            held.setdefault(f.effect, []).extend(f.intervals)
        self.timeline.set_held(held)
        where = "this venue's patch" if venue is not None else "the default limits (no venue patch is loaded - Live tab)"
        head = f"Limited by {where} - red marks on the timeline:" if report.limited else f"Nothing is limited by {where}."
        self.txt_check.setPlainText(head + "\n\n" + "\n".join(report.lines()))
        n = len(report.limited)
        return (f"{n} effect device(s) limited (red marks on the timeline, Effects tab)" if n else "no effect is limited"), n

    # ================================================================ live output
    def load_patch_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Venue patch", "", "Patch (*.toml);;All files (*)")
        if path:
            self._guarded(lambda: self.load_patch(path))

    def load_patch(self, path: str) -> None:
        patch = load_patch(path)
        problems = patch.problems(self._platform_ids())
        if problems:
            raise ValueError("The patch has errors:\n" + "\n".join(problems))
        if self.live is not None:
            self.set_live(False)
        self.patch = patch
        self.project.patch_path = str(path)
        self._set_dirty()
        self._refresh_live_panel()
        self.run_preflight()

    def clear_patch(self) -> None:
        if self.live is not None:
            self.set_live(False)
        self.patch = None
        self.project.patch_path = ""
        self._refresh_live_panel()
        self.run_preflight()

    def _platforms_changed(self) -> None:
        ids = [x.strip() for x in self.in_platforms.text().split(",") if x.strip()] or ["PLATFORM-01"]
        if ids == self.project.platform_ids:
            return
        if self.patch is not None and self.patch.problems(ids):
            QMessageBox.critical(self, "Platforms", "\n".join(self.patch.problems(ids)))
            self.in_platforms.setText(", ".join(self.project.platform_ids))
            return
        if self.live is not None:
            self.set_live(False)
        self.project.platform_ids = ids
        self._set_dirty()
        self._refresh_live_panel()
        self.run_preflight()

    def _hold_changed(self) -> None:
        if self.live is not None:
            self.live.hold = self.chk_hold.isChecked()
            self._sync_transport()

    def _refresh_live_panel(self) -> None:
        patch = self.patch
        if patch is None:
            self.lbl_patch.setText("<b>Sound-only preview.</b> No venue patch: LIVE plays the bass track and the seat vibration "
                                   "on this computer's sound output and sends no DMX.")
        else:
            out = patch.output
            where = {"sacn": "sACN (E1.31)", "artnet": "Art-Net", "enttec-pro": "USB DMX", "esp32": "through ESP32 boards",
                     "none": "no DMX output", "capture": "capture"}.get(out.protocol, out.protocol)
            sound = patch.audio.enabled or (out.esp32_audio is not None and out.esp32_audio.enabled)
            self.lbl_patch.setText(f"<b>{Path(patch.source or self.project.patch_path).name}</b> · {where} · "
                                   f"{len(patch.fixtures)} fixture type(s)" + (" · bass output" if sound else ""))
        self._show_nodes(None)
        instances = patch.instances(self._platform_ids()) if patch is not None else []
        self.fixtures.setRowCount(len(instances))
        for r, inst in enumerate(instances):
            chans = inst.channels()
            where = f"U{inst.universe} · {min(chans)}" + (f"–{max(chans)}" if len(chans) > 1 else "")
            cells = [QTableWidgetItem(text) for text in (inst.key, where, "", "")]
            cells[0].setToolTip(f"{inst.fixture.effect}: {inst.fixture.limits.describe()}")
            for c, cell in enumerate(cells):
                self.fixtures.setItem(r, c, cell)
            fire = QTableWidgetItem("▶ Fire")                 # a cell to click (handled in _fixture_clicked)
            fire.setToolTip("Click: switch this fixture on for a moment (only while LIVE; its limits still apply)")
            fire.setForeground(pg.mkColor(theme.ACCENT if self.live is not None else theme.FG_SUBTLE))
            fire.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.fixtures.setItem(r, 4, fire)
        on = self.live is not None
        self.btn_live.setChecked(on)
        self.act_live.setChecked(on)
        self.lbl_live.setText(f"<span style='color:{theme.DANGER}'>● LIVE</span>" if on else "")
        if not on:
            self.lbl_live_status.setText("Not live: nothing is sent.")
        if self.venue is not None:
            self.venue.editor_changed()

    def toggle_live(self) -> None:
        self.set_live(self.live is None)

    def set_live(self, on: bool) -> None:
        if on and self.live is None:
            patch = self.patch if self.patch is not None else preview_patch()
            output = self.live_output()
            real = patch.output.protocol in ("sacn", "artnet", "enttec-pro", "esp32") and output is None
            if real and not self.ask("Go live", "This sends DMX to the real devices of the venue patch.\n"
                                     "Fog, water, rain, heat, scent and strobe units will operate while you play.\n\nGo live?"):
                self._refresh_live_panel()
                return
            try:
                live = LiveSession(patch, self._platform_ids(), output=output, audio_backend=self.audio_backend)
                live.hold = self.chk_hold.isChecked()
                live.set_document(self.project.effects)
                live.start()
            except (ValueError, OSError) as exc:
                QMessageBox.critical(self, "Go live", str(exc))
                self._refresh_live_panel()
                return
            self.live = live
            self._sync_transport()
            self._monitor.start()
        elif not on and self.live is not None:
            self._monitor.stop()
            self.live.stop()
            self.live = None
        self._refresh_live_panel()

    def blackout(self) -> None:
        if self.playing:
            self.toggle_play()
        if self.live is not None:
            self.chk_hold.setChecked(False)
            self.live.blackout()
        self.status_text.setText("Blackout: all effects off" if self.live is not None else "Not live — nothing is being sent")

    def fire(self, key: str) -> None:
        if self.live is not None:
            self.live.fire(key, self.in_fire_level.value() / 100.0, self.in_fire_s.value())

    def _fixture_clicked(self, row: int, column: int) -> None:
        name = self.fixtures.item(row, 0)
        if column != 4 or name is None:
            return
        if self.live is None:
            self.status_text.setText("Not live: switch ● LIVE on to fire a fixture")
            return
        self.fire(name.text())
        self.status_text.setText(f"Fired {name.text()} for {self.in_fire_s.value():g} s at {self.in_fire_level.value():.0f} %")

    def _push_live_document(self) -> None:
        if self.live is not None:
            self.live.set_document(self.project.effects)
        if self.venue is not None:
            self.venue.sync_document()

    def show_venue(self) -> VenueWindow:
        """The venue window: the patch, its devices as stand-ins and its DMX, with the playhead."""
        if self.venue is None:
            self.venue = VenueWindow(self)
        self.venue.show()
        self.venue.raise_()
        self.venue.activateWindow()
        return self.venue

    def show_venue_ready(self) -> None:
        self.show_venue().show_ready()

    def _sync_transport(self) -> None:
        if self.live is not None:
            if self.playing:
                self.live.transport(True, self._anchor[1], loop=True, stamp=self._anchor[0])
            else:
                self.live.transport(False, self.current_t)
        if self.venue is not None:
            self.venue.sync_transport()

    def _update_monitor(self) -> None:
        if self.live is None:
            return
        st = self.live.status()
        rows = {f["key"]: f for f in st["fixtures"]}
        for r in range(self.fixtures.rowCount()):
            name, level, state = (self.fixtures.item(r, c) for c in (0, 2, 3))
            row = rows.get(name.text()) if name is not None else None
            if row is None or level is None or state is None:
                continue
            level.setText(f"{row['level'] * 100:3.0f} %  {'/'.join(str(v) for v in row['dmx'])}")
            state.setText(row["limit"] or ("on" if row["level"] > 0.01 else ""))
            state.setForeground(pg.mkColor(theme.DANGER if row["limit"] else theme.SUCCESS))
        out = st["output"]
        self._show_nodes(out.get("nodes"))
        text = f"{out['output']}: {out['frames']} frames sent, {out['errors']} error(s)"
        if "nodes" in out:
            text += f" — {out['nodes_present']} of {out['nodes_configured']} ESP32 board(s) answer"
        if out["errors"] and out["last_error"]:
            text += f" — {out['last_error']}"
        audio = st.get("audio")
        if audio:
            text += f"\nsound: {'playing on ' + (audio['device'] or 'default output') if audio['running'] else audio['error'] or 'off'}"
        if st["errors"]:
            text += f"\nservice errors: {st['errors']} ({st['last_error']})"
        self.lbl_live_status.setText(text)

    def _show_nodes(self, nodes: list[dict[str, Any]] | None) -> None:
        """The ESP32 boards of an "esp32" patch: from the running output while live, else as configured."""
        patch = self.patch
        if patch is None or patch.output.protocol != "esp32":
            self.nodes.setVisible(False)
            return
        ids = self._platform_ids()
        if nodes is None:                           # not live: who would be addressed
            wired: dict[str, list[str]] = {}
            for universe, (host, _port) in sorted(patch.esp32_lines(ids, {}).items()):
                what = "theatre" if universe in ESP32_THEATRE_UNIVERSES else ids[universe - ESP32_PLATFORM_UNIVERSE - 1]
                if what not in wired.setdefault(host, []):
                    wired[host].append(what)
            nodes = [{"host": host, "present": None, "name": " + ".join(sorted(names, key=lambda n: n == "theatre"))}
                     for host, names in wired.items()]
        self.nodes.setVisible(True)
        self.nodes.setRowCount(len(nodes))
        self.nodes.setFixedHeight(32 + 27 * min(max(len(nodes), 1), 5))
        for r, node in enumerate(nodes):
            present = node.get("present")
            if present is None:
                has, state, colour = "", "not live", theme.FG_MUTED
            elif not present:
                has, state, colour = "—", "no answer: left out", theme.ATTENTION
            else:
                parts = [w for w in node.get("has", []) if w != "platform"]
                has = " + ".join({"dmx": f"DMX ×{node.get('dmx_ports', 1)}", "audio": "sound"}.get(w, w) for w in parts) or "nothing to drive"
                stopped = bool(node.get("inhibited"))
                state, colour = ("emergency stop: dark and silent", theme.DANGER) if stopped else ("answers", theme.SUCCESS)
            name = node.get("name")
            if name is None:
                theatre = any("theatre" in str(w) for w in node.get("lines", {}).values())
                name = " + ".join(n for n in (node.get("platform"), "theatre" if theatre or not node.get("platform") else None) if n)
            for c, text in enumerate((str(name), str(node.get("host", "")), f"{has} · {state}" if present else state)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if c == 2:
                    item.setForeground(pg.mkColor(colour))
                self.nodes.setItem(r, c, item)

    # ================================================================ transport / preview
    def toggle_play(self) -> None:
        if self.project.mode == "empty":
            return
        self.playing = not self.playing
        self.btn_play.setText("❚❚  Pause" if self.playing else "▶  Play")
        if self.playing:
            if self.current_t >= self._duration() - 0.05:
                self.current_t = 0.0
            self._anchor = (time.monotonic(), self.current_t)
            self.timer.start()
        else:
            self.timer.stop()
        self._sync_transport()

    def _tick(self) -> None:
        now = time.monotonic()
        t = self._anchor[1] + (now - self._anchor[0])
        if t >= self._duration():                      # loop, like the theatre does
            self._anchor = (now, 0.0)
            t = 0.0
            self._sync_transport()
        self.seek(t, from_tick=True)

    def seek(self, t: float, from_plot: bool = False, from_tick: bool = False) -> None:
        self.current_t = max(0.0, min(t, self._duration()))
        if not from_tick:
            if self.playing:
                self._anchor = (time.monotonic(), self.current_t)
            self._sync_transport()
        if self.reader is not None:
            frame = self.reader.frame_at(self.current_t)
            if frame is not None:
                h, w = frame.shape[:2]
                img = QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
                self.video_label.setPixmap(QPixmap.fromImage(img).scaled(
                    self.video_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        motion = self._motion()
        fs = self.project.sample_rate_hz
        profile = self.project.extraction.profile
        if motion is not None and len(motion):
            i = min(len(motion) - 1, int(self.current_t * fs))
            lo, hi = max(0, i - int(2 * fs)), min(len(motion), i + int(2 * fs))
            geo = profile.geometry
            trail = np.array([geo.transform(motion[k])[0] + geo.transform(motion[k])[1] @ geo.rotation_centre for k in range(lo, hi, 5)])
            self.view3d.set_state(motion[i], trail)
            if self._deriv is None or len(self._deriv[0]) != len(motion):
                self._deriv = derivatives(motion, fs)
            v, a, j = (d[i] for d in self._deriv)
            lim = profile.limits
            use = np.abs(motion[i]) / np.maximum(np.abs(lim.position_max), np.abs(lim.position_min))
            lengths = geo.leg_lengths(motion[i] + profile.calibration.zero_offsets)
            act = profile.actuators
            stroke_use = np.max(np.abs(lengths - (act.min_length + act.max_length) / 2) / (act.stroke / 2))
            self.readout.setText(
                "pose " + " ".join(f"{a_}={motion[i][k] * (1000 if k < 3 else 180 / math.pi):+.1f}{'mm' if k < 3 else '°'}" for k, a_ in enumerate(AXES))
                + f"   v {np.max(np.abs(v) / lim.velocity) * 100:3.0f}%  a {np.max(np.abs(a) / lim.acceleration) * 100:3.0f}%"
                + f"  j {np.max(np.abs(j) / lim.jerk) * 100:3.0f}%  workspace {np.max(use) * 100:3.0f}%  stroke {stroke_use * 100:3.0f}%"
            )
        else:
            self.readout.setText("")
        if not from_plot:
            for line in self.playheads:
                line.blockSignals(True)
                line.setValue(self.current_t)
                line.blockSignals(False)
        self.timeline.set_playhead(self.current_t, follow=self.playing)
        self.time_label.setText(f"{_fmt(self.current_t)} / {_fmt(self._duration())}")

    # ================================================================ help / about / close
    def show_guide(self) -> None:
        QMessageBox.information(self, "How to make a programme", (
            "1  Open…   a video (flat, 180° or 360°; mono or stereo).\n"
            "    Check the projection on the Programme tab - it is guessed from the file.\n\n"
            "2  Analyse video   works out, on this computer:\n"
            "    · the motion of the platform on its six axes, inside the platform's limits;\n"
            "    · vibration and the bass track from the sound;\n"
            "    · effects: wind, air blasts, strobe - and, from what the picture and the sound show,\n"
            "      rain, fog, heat (fire), bubbles, snow, water spray and scent.\n\n"
            "    Edit on the timeline. MOTION: draw on an axis lane to add motion, on Intensity to calm\n"
            "    or strengthen a passage. VIBRATION AND SOUND: the bass track and the seat vibration.\n"
            "    EFFECTS: one lane per effect; + Add effect makes a new one. Click a lane for its options.\n\n"
            "3  Check   the motion against the platform's limits, the effects against the venue's limits.\n"
            "    Red marks on a lane show what a limit will shorten.\n\n"
            "4  Export   ONE .motion file with everything. The show service plays it: the platforms, the DMX\n"
            "    devices and the bass shakers - from the PC's outputs or through ESP32 boards. Hardware that is\n"
            "    not there is left out without an error.\n\n"
            "Live tab: try the effects on the real devices while you edit."))

    def show_help(self) -> None:
        QMessageBox.information(self, "Timeline", (
            "Ruler: click or drag to move the playhead.\n"
            "Group heading (MOTION, VIBRATION AND SOUND, EFFECTS): click to fold the group away or show it.\n"
            "Click a lane: its options appear on the right.\n"
            "Double click a lane: add a cue or a curve point (Alt: the other kind).\n"
            "Cue: drag to move, edges to resize, top corners for fades, top centre for the level.\n"
            "Curve point: drag (Shift: one direction).  Alt while dragging: no snapping.\n"
            "Drag on empty space: select in a rectangle.  Ctrl+click: add to the selection.\n"
            "Delete, Ctrl+C / X / V / D, Ctrl+A, Ctrl+Z / Ctrl+Shift+Z.\n"
            "Arrows: nudge the selection (Shift: coarse); Up / Down change the level.\n"
            "Wheel: scroll;  Shift+wheel: along time;  Ctrl+wheel: zoom.  F: whole programme.  S: snap.\n"
            "Space: play / pause.  M / E: fold the motion / effect lanes.  Ctrl+R: update the motion.\n"
            "Ctrl+L: live.  Ctrl+B: blackout."))

    def show_about(self) -> None:
        QMessageBox.about(self, "About", (
            f"<b>{PRODUCT_TITLE} {__version__}</b><br><br>"
            "Motion, vibration and effects from video; one timeline for all of them; show service for "
            "Stewart platforms, DMX devices and bass shakers.<br>"
            "Everything runs on this computer: no video, picture or sound leaves it.<br><br>"
            "Free software (MIT licence). User interface font: JetBrains Mono (SIL Open Font License 1.1). "
            "Scene detection uses a CLIP ViT-B/32 picture model (OpenCLIP, MIT) and an audio tagging model "
            "(icefall / sherpa-onnx, Apache-2.0) - see THIRD_PARTY_LICENSES.md.<br><br>"
            "A passed validation is a numerical limit check, not a passenger-safety certification. "
            "DMX output is not a safety function."))

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._may_discard() or (self.venue is not None and not self.venue._may_discard()):
            event.ignore()
            return
        self.timer.stop()
        for t in (self._reprocess, self._live_sync, self._check_timer, self._monitor):
            t.stop()
        if self.venue is not None:
            venue, self.venue = self.venue, None
            venue.dirty = False
            venue.close()
            theme.dispose(venue)
        if self.live is not None:
            self.live.stop()
            self.live = None
        self.stop_stream()
        if self.worker is not None:
            self.worker.cancelled = True
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._close_reader()
        super().closeEvent(event)


MainWindow = StudioWindow       # the name the former Motion Extractor window had


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName(PRODUCT_NAME)
    theme.apply(app)  # type: ignore[arg-type]
    win = StudioWindow()
    win.show()
    if len(args) > 1 and Path(args[1]).exists():
        win.open_path(args[1])
    code = int(app.exec())  # type: ignore[attr-defined]
    theme.dispose(win.venue, win)     # here and in this order, not somewhere in the end of the program
    return code


if __name__ == "__main__":
    raise SystemExit(main())
