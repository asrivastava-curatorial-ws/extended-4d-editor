"""The live window: the (delayed) picture, what the show is doing, and the operator's keys.

For a captured picture in "delay" mode this window *is* the screen the audience watches: it shows
each frame ``delay_s`` after it was captured, which is when the motion that belongs to it arrives.
For every other source it is a monitor - state, the six axes, the effect levels, the platforms.

    Space  hold / release          + -   strength           [ ]  picture delay -/+ 10 ms
    E      engage after a fault    R     record on / off    H    hide / show the overlay
    F      full screen             Q, Esc  end

Grabbing the screen itself (capture = "screen") is done here, in the GUI thread, because that is
where the desktop toolkit allows it. It works on X11; a Wayland session does not let a program read
the screen this way - use a capture card, a stream, or an X11 session (docs/LIVE.md).

The window is not where the show runs. Grabbing a screen and painting a large picture keep every
other thread of a Python process waiting (tens of milliseconds at a time), and the platforms need
a command every control period: `run_window` starts the show in a process of its own and this
window is its client (remote.py). A picture source lives here - captured, analysed, shown - and
its samples go to that process.
"""

from __future__ import annotations

import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeyEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QApplication, QWidget

from fourdvr_studio import PRODUCT_TITLE
from fourdvr_studio.gui import theme

from .config import LiveConfig
from .safety import HOLD, PAUSED, RESUMING, RUNNING, STALLED
from .sample import AXES

STATE_COLOURS = {RUNNING: theme.SUCCESS, RESUMING: theme.ATTENTION, HOLD: theme.ATTENTION, PAUSED: theme.FG_MUTED,
                 STALLED: theme.DANGER}
NOMINAL_RANGE = np.array([0.10, 0.10, 0.08, 0.15, 0.15, 0.15])       # bars without a platform: m, m, m, rad, rad, rad
KEYS = "Space hold · + − strength · [ ] delay · E engage · R record · H overlay · F full screen · Q end"
TRACE_S = 20.0            # how much of the past the traces show (sources without a picture)


def grab_screen(index: int = 0, region: tuple[int, ...] | None = None) -> np.ndarray | None:
    """The screen (or a region of it) as a BGR array; None where the session does not allow it."""
    screens = QGuiApplication.screens()
    if not screens:
        return None
    screen = screens[min(max(index, 0), len(screens) - 1)]
    if region is None:
        pixmap = screen.grabWindow(0)
    else:
        pixmap = screen.grabWindow(0, int(region[0]), int(region[1]), int(region[2]), int(region[3]))
    if pixmap.isNull():
        return None
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_BGR888)
    w, h = image.width(), image.height()
    if w < 16 or h < 16:
        return None
    data = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes())
    return data.reshape(h, image.bytesPerLine())[:, : w * 3].reshape(h, w, 3).copy()      # (the image's memory goes with it)


class LiveWindow(QWidget):
    """`engine` is the show: a RemoteEngine (the show in its own process - how `run_window` does it) or
    a LiveEngine in this process (tests, experiments). The window uses its status(), its `source` (for
    a picture) and the operator's calls: hold, set_gain, engage, start_recording, stop_recording."""

    def __init__(self, engine: Any, record: str | None = None) -> None:
        super().__init__()
        self.engine = engine
        self.record_to = record
        self.saved: Path | None = None
        self.overlay = True
        self.grab_failed = False
        self._status: dict[str, Any] = {}
        self._image: QImage | None = None
        self._shown: np.ndarray | None = None     # the frame `_image` shows (kept: the image uses its memory)
        self._frame_id = 0
        self._beat = 0
        self._trace: deque[tuple[float, np.ndarray]] = deque(maxlen=int(TRACE_S * 32))
        self.setWindowTitle(f"{PRODUCT_TITLE} - live")
        self.resize(1120, 660)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)
        src: Any = engine.source
        self.has_picture = hasattr(src, "picture_at")
        self._paint = QTimer(self)
        self._paint.timeout.connect(self._refresh)
        self._paint.start(16)
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_status)
        self._poll.start(50)
        self._grab = QTimer(self)
        if getattr(src, "needs_gui", False):
            self._grab.timeout.connect(self._grab_frame)
            self._grab.start(max(10, round(1000.0 / src.fps)))
        self._poll_status()

    # ------------------------------------------------------------------ timers
    def _grab_frame(self) -> None:
        src: Any = self.engine.source
        frame = grab_screen(src.screen, src.region)
        if frame is None:
            self.grab_failed = True
            return
        self.grab_failed = False
        src.push_frame(frame, time.monotonic())

    def _poll_status(self) -> None:
        alive = getattr(self.engine, "alive", None)
        if alive is not None:
            alive()                               # this window answers: the show's process holds if it stops
        self._status = self.engine.status()
        if not self.has_picture:                  # one more point of the traces
            pose = self._status["pose"]
            self._trace.append((time.monotonic(), np.array([pose[name] for name in AXES])))
        self._beat += 1
        if not self.has_picture or self._beat % 3 == 0:
            self.update()

    def _refresh(self) -> None:
        """Show the frame that is due now. Nothing is repainted while nothing has changed."""
        if not self.has_picture:
            return
        frame = self.engine.source.picture_at(time.monotonic())  # type: ignore[attr-defined]
        if frame is not None and id(frame) != self._frame_id:
            self._frame_id = id(frame)
            h, w = frame.shape[:2]
            # shown from the frame's own memory (frames are never changed once captured): no copy of a large picture
            self._shown = np.ascontiguousarray(frame if frame.ndim == 3 else np.stack([frame] * 3, axis=2))
            self._image = QImage(self._shown.data, w, h, 3 * w, QImage.Format.Format_BGR888)
            self.update()

    # ------------------------------------------------------------------ keys
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, text = event.key(), event.text()
        eng = self.engine
        if key in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
            self.close()
        elif key == Qt.Key.Key_Space:
            eng.hold(not self._status.get("hold", False))
        elif text in ("+", "="):
            eng.set_gain(self._status["gain"] + 0.05)
        elif text in ("-", "_"):
            eng.set_gain(self._status["gain"] - 0.05)
        elif text in ("[", "]") and hasattr(eng.source, "set_delay"):
            picture: Any = eng.source
            picture.set_delay(picture.delay_s + (0.01 if text == "]" else -0.01))
            if hasattr(eng, "picture_delay_changed"):
                eng.picture_delay_changed(picture.delay_s)        # the shakers' sound follows, in the show's process
        elif key == Qt.Key.Key_E:
            eng.engage()
        elif key == Qt.Key.Key_R:
            self.toggle_recording()
        elif key == Qt.Key.Key_H:
            self.overlay = not self.overlay
        elif key == Qt.Key.Key_F:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        else:
            super().keyPressEvent(event)
            return
        self._poll_status()
        self.update()

    def toggle_recording(self) -> None:
        if self._status.get("recording_s") is None:
            self.engine.start_recording()
        else:
            self.saved = self.engine.stop_recording(self.record_to or None)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.INSET if self.has_picture else theme.CANVAS))
        if self._image is not None:
            iw, ih = self._image.width(), self._image.height()
            scale = min(self.width() / iw, self.height() / ih)
            w, h = round(iw * scale), round(ih * scale)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h), self._image)
        if self.overlay:
            self._paint_overlay(p)
            if not self.has_picture:
                self._paint_traces(p)
        p.end()

    def _paint_traces(self, p: QPainter) -> None:
        """What the platform has been asked to do in the last seconds: translations above, rotations below."""
        left, top = 530, 14
        width, height = self.width() - left - 14, (self.height() - 14 - 40 - top - 10) // 2
        if width < 240 or height < 90 or len(self._trace) < 2:
            return
        now = self._trace[-1][0]
        times = np.array([t for t, _pose in self._trace]) - now
        poses = np.array([pose for _t, pose in self._trace])
        for row, (title, first, scale, unit, floor) in enumerate((("moves", 0, 1000.0, "mm", 20.0),
                                                                  ("turns", 3, 57.2958, "°", 3.0))):
            box = QRect(left, top + row * (height + 10), width, height)
            self._panel(p, box)
            values = poses[:, first : first + 3] * scale
            span = max(floor, float(np.max(np.abs(values))) * 1.15)
            plot = QRect(box.x() + 12, box.y() + 26, box.width() - 92, box.height() - 38)
            p.setFont(theme.font(9.0))
            p.setPen(QColor(theme.FG_MUTED))
            p.drawText(box.x() + 12, box.y() + 18, f"{title}, last {TRACE_S:.0f} s  (± {span:.0f} {unit})")
            p.setPen(QPen(QColor(theme.BORDER_MUTED), 1))
            p.drawLine(plot.left(), plot.center().y(), plot.right(), plot.center().y())
            xs = plot.right() + times / TRACE_S * plot.width()
            ends: list[tuple[float, int]] = []
            for k in range(3):
                ys = plot.center().y() - values[:, k] / span * (plot.height() / 2.0)
                p.setPen(QPen(QColor(theme.AXIS_COLOURS[k]), 1.6))
                p.drawPolyline([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True) if x >= plot.left()])
                ends.append((float(ys[-1]), k))
            # the names at the right end of their lines, kept apart
            ends.sort()
            y_last = -1e9
            for y, k in ends:
                y_text = max(y + 4.0, y_last + 13.0)
                y_last = y_text
                p.setPen(QColor(theme.AXIS_COLOURS[k]))
                p.drawText(plot.right() + 8, int(min(max(y_text, plot.top() + 8), plot.bottom() + 4)), AXES[first + k])

    def _panel(self, p: QPainter, rect: QRect) -> None:
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(theme.colour(theme.CANVAS, 215))
        p.drawRoundedRect(rect, 6, 6)

    def _bar(self, p: QPainter, x: int, y: int, w: int, value: float, colour: str, centred: bool) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.BORDER_MUTED))
        p.drawRoundedRect(QRect(x, y, w, 8), 3, 3)
        p.setBrush(QColor(colour))
        if centred:
            half = w // 2
            length = round(min(1.0, abs(value)) * half)
            p.drawRoundedRect(QRect(x + half - (length if value < 0 else 0), y, max(length, 1), 8), 3, 3)
            p.setBrush(QColor(theme.FG_SUBTLE))
            p.drawRect(QRect(x + half, y - 2, 1, 12))
        else:
            p.drawRoundedRect(QRect(x, y, max(1, round(min(1.0, max(0.0, value)) * w)), 8), 3, 3)

    def _paint_overlay(self, p: QPainter) -> None:
        st = self._status
        if not st:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        src = st["source"]
        m = 14
        # ---- state
        state = st["state"]
        line = []
        if "delay_ms" in src and src.get("mode") == "delay":
            line.append(f"picture {src['delay_ms']} ms late")
        if st.get("delay_ms") is not None:
            line.append(f"motion {st['delay_ms']:.0f} ms after capture")
        if "analysis_ms" in src:
            line.append(f"analysis {src['analysis_ms']:.0f} ms")
        if src.get("late"):
            line.append(f"{src['late']} late (raise the delay: ])")
        if st.get("age_ms") is not None and not line:
            line.append(f"newest sample {st['age_ms']:.0f} ms old")
        if st.get("recording_s") is not None:
            line.append(f"● rec {st['recording_s']:.0f} s")
        first = f"{src['title']} · {src['rate_hz']:g} samples/s · strength {st['gain']:.2f}"
        second = " · ".join(line)
        problem = str(src.get("error") or ("this session does not let a program read the screen (Wayland?): use a "
                                           "capture card, a stream or an X11 session" if self.grab_failed else "")
                      or st.get("problem") or "")
        p.setFont(theme.font(9.5))
        metrics = p.fontMetrics()
        widest = max(metrics.horizontalAdvance(t) for t in (first, second, problem))
        head = QRect(m, m, min(max(330, widest + 30), self.width() - 2 * m), 96 if problem else 78)
        self._panel(p, head)
        colour = STATE_COLOURS.get(state, theme.FG_MUTED)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colour))
        p.drawEllipse(head.x() + 14, head.y() + 16, 12, 12)
        p.setPen(QColor(theme.FG))
        p.setFont(theme.font(12.5, bold=True))
        p.drawText(head.x() + 36, head.y() + 28, f"{state}  {round(st['envelope'] * 100)} %")
        p.setFont(theme.font(9.5))
        p.setPen(QColor(theme.FG_MUTED))
        p.drawText(head.x() + 14, head.y() + 48, first)
        p.drawText(head.x() + 14, head.y() + 66, second)
        if problem:
            p.setPen(QColor(theme.DANGER))
            p.drawText(head.x() + 14, head.y() + 84, metrics.elidedText(problem, Qt.TextElideMode.ElideRight, head.width() - 28))
        # ---- the six axes
        rng = NOMINAL_RANGE
        if st.get("ranges"):
            rng = np.asarray(st["ranges"], dtype=np.float64)
        axes = QRect(m, head.bottom() + 10, 300, 24 + 22 * 6)
        self._panel(p, axes)
        p.setFont(theme.font(9.0))
        for i, name in enumerate(AXES):
            y = axes.y() + 20 + 22 * i
            value = st["pose"][name]
            p.setPen(QColor(theme.FG_MUTED))
            p.drawText(axes.x() + 12, y + 8, name)
            self._bar(p, axes.x() + 70, y, 150, value / rng[i], theme.AXIS_COLOURS[i % 3], centred=True)
            p.setPen(QColor(theme.FG))
            unit = f"{value * 1000:+5.0f} mm" if i < 3 else f"{np.degrees(value):+5.1f}°"
            p.drawText(axes.x() + 228, y + 8, unit)
        # ---- effects and platforms
        levels = st["levels"]
        rows = len(levels) + len(st["platforms"]) + (1 if st.get("input") else 0)
        if rows:
            inp = st.get("input")
            said = ""
            if inp:
                parts = [f"{inp['speed'] * 3.6:.0f} km/h"] if inp["speed"] > 0.0 else []
                parts += [f"{inp['rpm']} rpm"] if inp["rpm"] else []
                if inp["says"]:
                    parts.append(", ".join(inp["says"][:2]))
                said = " · ".join(parts) or "-"
            texts = [said] + [f"{plat['id']}  {plat['phase']}" + (f": {plat['fault']} - press E" if plat["fault"] is not None else "")
                              for plat in st["platforms"]]
            widest = max(p.fontMetrics().horizontalAdvance(t) for t in texts)
            fx = QRect(m, axes.bottom() + 10, min(max(300, widest + 24), max(300, self.width() // 2 - 2 * m)), 16 + 20 * rows)
            self._panel(p, fx)
            y = fx.y() + 14
            for name, level in sorted(levels.items()):
                p.setPen(QColor(theme.FG_MUTED))
                p.drawText(fx.x() + 12, y + 8, name)
                self._bar(p, fx.x() + 90, y, 130, level, theme.PINK if name == "vibration" else theme.CYAN, centred=False)
                p.setPen(QColor(theme.FG))
                p.drawText(fx.x() + 228, y + 8, f"{round(level * 100):3d} %")
                y += 20
            fit = p.fontMetrics()
            if inp:
                p.setPen(QColor(theme.FG_MUTED))
                p.drawText(fx.x() + 12, y + 8, fit.elidedText(said, Qt.TextElideMode.ElideRight, fx.width() - 24))
                y += 20
            for plat, text in zip(st["platforms"], texts[1:], strict=True):
                p.setPen(QColor(theme.DANGER if plat["fault"] is not None else theme.FG_MUTED))
                p.drawText(fx.x() + 12, y + 8, fit.elidedText(text, Qt.TextElideMode.ElideRight, fx.width() - 24))
                y += 20
        # ---- keys
        p.setFont(theme.font(8.5))
        keys = QRect(m, self.height() - m - 24, min(p.fontMetrics().horizontalAdvance(KEYS) + 24, self.width() - 2 * m), 24)
        self._panel(p, keys)
        p.setPen(QColor(theme.FG_MUTED))
        p.drawText(keys.x() + 12, keys.y() + 16, KEYS)

    def closeEvent(self, event: Any) -> None:
        self._paint.stop()
        self._poll.stop()
        self._grab.stop()
        super().closeEvent(event)


def _app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(app)  # type: ignore[arg-type]
    return app  # type: ignore[return-value]


def open_window(cfg: LiveConfig, argv: list[str], record: str | None = None,
                audio_backend: str = "") -> tuple[Any, LiveWindow]:
    """The live window and the show behind it, running: the show in a process of its own (started with
    the same arguments, `argv`, as this program), a picture source in this one."""
    from .remote import RemoteEngine
    from .sources import INFOS, canonical, create
    from .sources.sound import PICTURE_SOUND_KEYS

    kind = canonical(str(cfg.source.get("type", "open")))
    picture = None
    if kind == "screen":                          # the sound for the shakers is captured where it is played
        picture = create({k: v for k, v in cfg.source.items() if k not in PICTURE_SOUND_KEYS})
    engine = RemoteEngine(argv, picture, audio_backend, INFOS[kind].title)
    window = LiveWindow(engine, record)
    window.show()
    QApplication.processEvents()                  # on the screen before anything is commanded
    try:
        engine.start()
    except BaseException:
        window.close()
        raise
    if record is not None or cfg.record_dir is not None:
        engine.start_recording()
    return engine, window


def run_window(cfg: LiveConfig, argv: list[str], seconds: float = 0.0, record: str | None = None,
               audio_backend: str = "") -> int:
    app = _app()
    engine, window = open_window(cfg, argv, record, audio_backend)
    if seconds:
        QTimer.singleShot(int(seconds * 1000), window.close)
    saved = None
    previous = signal.signal(signal.SIGINT, lambda *_: window.close())     # Ctrl+C in the terminal: end like Q
    try:
        app.exec()
    finally:
        signal.signal(signal.SIGINT, previous)
        if engine.status().get("recording_s") is not None:
            saved = engine.stop_recording(record or None)
        report = engine.stop()
    print("\n".join(report))
    saved = saved or window.saved
    theme.dispose(window)
    if saved is not None:
        print(f"recorded: {saved}")
    return 0


# =========================================================================== measuring the picture delay
class _Pattern(QWidget):
    """A window that turns from black to white and back at known instants."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{PRODUCT_TITLE} - delay test pattern")
        self.resize(640, 360)
        self.white = False
        self.shown_at: list[tuple[float, bool]] = []

    def flip(self) -> None:
        self.white = not self.white
        self.shown_at.append((time.monotonic(), self.white))
        self.repaint()

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#ffffff" if self.white else "#000000"))
        p.end()


def measure_delay(flips: list[tuple[float, bool]], seen: list[tuple[float, float]]) -> list[float]:
    """Delays between each flip of the pattern and the first captured frame that shows it.
    `seen` is (capture time, mean brightness 0..1) of every captured frame. A flip counts only if
    the captured picture was seen to change: it still showed the other state around the flip - a
    picture that is dark (or bright) all the time is not "the black pattern, seen at once"."""
    delays: list[float] = []
    for k, (t_flip, white) in enumerate(flips):
        t_next = flips[k + 1][0] if k + 1 < len(flips) else t_flip + 2.0
        was_other = False
        for t, luma in seen:
            if t < t_flip - 0.3 or t >= t_next:
                continue
            new = (luma > 0.6) if white else (luma < 0.4)
            old = (luma < 0.4) if white else (luma > 0.6)
            if t < t_flip:
                was_other = old                   # the last frame before the flip decides
            elif old:
                was_other = True
            elif new:
                if was_other:
                    delays.append(t - t_flip)
                break
    return delays


def pattern_share(seen: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The brightness of the captured frames on the scale of the pattern: 0 = as dark, 1 = as bright as
    the picture got. The pattern need not fill the captured picture - a window that covers a part of it
    changes the mean brightness by that part. Empty if the picture hardly changed at all."""
    if len(seen) < 10:
        return []
    values = np.array([luma for _t, luma in seen])
    lo, hi = np.percentile(values, [5.0, 95.0])
    if hi - lo < 0.04:
        return []
    return [(t, float((luma - lo) / (hi - lo))) for t, luma in seen]


def run_calibration(cfg: LiveConfig, seconds: float = 12.0) -> int:
    """Show the pattern, capture it with the configured screen source, report the delay."""
    from .sources import SourceError, canonical, create

    if canonical(str(cfg.source.get("type", ""))) != "screen":
        print('error: the delay test is for a "screen" source (screen grab, capture card or stream)', file=sys.stderr)
        return 2
    app = _app()
    try:
        source: Any = create(cfg.source)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    seen: list[tuple[float, float]] = []

    def tap(frame: np.ndarray, t: float) -> None:
        seen.append((t, float(frame[::4, ::4].mean()) / 255.0))

    source.on_frame = tap
    pattern = _Pattern()
    pattern.showMaximized()
    print("Showing a black / white test pattern. It must be in what the source captures: put this window on the "
          "captured screen, or feed that screen's output to the capture card. Leave the rest of the screen alone meanwhile.")
    try:
        source.start(lambda _sample, _now: None)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    flip = QTimer()
    flip.timeout.connect(pattern.flip)
    flip.start(700)
    grab = QTimer()
    if source.needs_gui:
        def take() -> None:
            frame = grab_screen(source.screen, source.region)
            if frame is not None:
                source.push_frame(frame, time.monotonic())
        grab.timeout.connect(take)
        grab.start(max(5, round(1000.0 / source.fps)))
    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()
    flip.stop()
    grab.stop()
    source.stop()
    pattern.close()
    captured = len(seen)
    seen = pattern_share(seen)
    delays = measure_delay(pattern.shown_at, seen)
    if len(delays) < 3:
        if seen:
            print("The captured picture shows the pattern, but more than 0.7 s after it was put on the screen (a network "
                  "stream?):\nlonger than this test measures. \"delay\" mode works with such a picture - the shown picture "
                  "and the motion are both\ntimed from the captured frame - \"direct\" mode does not.")
        else:
            print(f"The pattern was not seen in the captured picture ({captured} frames captured, "
                  f"{len(pattern.shown_at)} changes shown). Is the pattern window inside what is captured?")
        return 1
    d = np.array(delays) * 1000.0
    print(f"The captured picture shows a change {np.median(d):.0f} ms after it was put on the screen "
          f"(median of {len(d)}; from {d.min():.0f} to {d.max():.0f} ms; frames arrive every "
          f"{1000.0 / max(source.capture_fps, 1e-6):.0f} ms).")
    print("That is how old the picture already is when it is captured. In \"direct\" mode the motion is that much "
          "later\nthan the live window says. In \"delay\" mode it makes no difference to the match: the shown picture and "
          "the motion\nare both timed from the captured frame.")
    return 0
