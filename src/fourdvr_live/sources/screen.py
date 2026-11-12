"""Motion from a live picture: a captured screen, a capture card or camera, a network stream - or a
video file played in real time.

There is no telemetry here, so the camera's motion is estimated from the picture, frame by frame,
with the extractor's own tracking and ego-motion estimate (docs/MOTION_ALGORITHM.md), followed by
causal smoothing. That takes time - the frame has to arrive, be analysed and be smoothed - so the
motion is later than the picture. Two ways of living with that (docs/LIVE.md, "The delay"):

``mode = "delay"``   the picture is shown by this program ``delay_s`` late (the live window), and the
                     motion is sent ``motion_lead_s`` before the frame it belongs to appears: motion
                     and picture are in step, and the smoothing has had time to look ahead.
``mode = "direct"``  the picture is not in our hands (the rider looks at the game's own screen).
                     The motion comes as early as it can - and is still late, so the fast part of
                     the cueing is turned down (``comfort``) and only what tolerates lag is full.

What the picture cannot give: a scale. "How strong" is learned from the picture's own recent past
(the strongest tenth of the last minute counts as ``strength`` m/s^2) and set by the gain.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import cv2
import numpy as np

from fourdvr_extractor.analysis import (
    AnalysisSettings,
    _detect,
    _feature_mask,
    _histogram,
    tracking_collapsed,
)
from fourdvr_extractor.egomotion import bearings, estimate, pixel_angle, to_platform
from fourdvr_extractor.video import eye_view

from ..sample import LiveSample
from .base import PICTURE_INPUT_LP_HZ, SCREEN, LiveSource, SourceError

TAU_RATES = 0.03          # s: smoothing of the rotation rates
TAU_VELOCITY = 0.04       # s, twice in a row: smoothing of the (scale-free) velocity before it is differentiated
TAU_ACCEL = 0.04          # s, twice in a row: smoothing of the acceleration made from it
SMOOTHING_LAG_S = 0.16    # what that smoothing costs the accelerations, compensated in "delay" mode
INPUT_LP_HZ = PICTURE_INPUT_LP_HZ     # what the cueing's own input smoothing is set to for a picture (0.05 s more)
MIN_CONFIDENCE = 0.15
LOST_AFTER_S = 1.5        # no usable picture for this long (menu, loading screen, black): rest
SCALE_WINDOW_S = 60.0
ACCEL_FLOOR = 0.3         # 1/s^2: a picture that moves less than this is not amplified to full strength
PEAK_SHARE = 0.25         # the strongest moment of the last while counts at least this much in the scale


def _alpha(dt: float, tau: float) -> float:
    return 1.0 - math.exp(-dt / tau)


class PictureMotion:
    """Frame after frame in, samples out. No threads, no clock of its own: ``step(frame, t)``."""

    def __init__(self, projection: str = "flat", hfov_deg: float = 90.0, stereo: str = "mono", width: int = 480,
                 masks: list[tuple[float, float, float, float]] | None = None, strength: float = 7.0,
                 predict_s: float = 0.0, align_s: float = 0.0, seed: int = 7) -> None:
        if projection not in ("flat", "180", "360"):
            raise SourceError('projection must be "flat", "180" or "360"')
        if not 20.0 <= hfov_deg <= 170.0:
            raise SourceError("hfov_deg must be 20-170")
        self.projection, self.hfov_deg, self.stereo = projection, hfov_deg, stereo
        self.width = int(min(960, max(160, width)))
        self.masks = list(masks or [])
        self.strength = strength
        self.predict_s = predict_s
        self.align_s = align_s            # hold the rotation rates back this long, so that they are as late
        self._held: deque[tuple[float, np.ndarray]] = deque()   # as the (more heavily smoothed) accelerations
        self._w_raw = np.zeros(3)         # the unsmoothed values of the previous frame (hit detector)
        self._v_raw = np.zeros(3)
        self.settings = AnalysisSettings(max_width=self.width, grid_cols=10, grid_rows=6, features_per_cell=4)
        self.rng = np.random.default_rng(seed)
        self._criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.02)
        self._size: tuple[int, int, int, int] | None = None      # analysis width, height; picture width, height
        self._mask: np.ndarray | None = None
        self._threshold = 0.0
        self._prev: np.ndarray | None = None
        self._prev_hist: np.ndarray | None = None
        self._prev_t = 0.0
        self._survival: deque[float] = deque(maxlen=30)
        self._w = np.zeros(3)
        self._w_last = np.zeros(3)
        self._v = np.zeros(3)
        self._v_last = np.zeros(3)
        self._acc = np.zeros(3)
        self._v2 = np.zeros(3)            # second stages of the two-stage smoothing
        self._acc2 = np.zeros(3)
        self._peak = 0.0                  # the strongest acceleration lately (fades over 20 s)
        self._w_low = np.zeros(3)
        self._vib = 0.0
        self._scale: deque[tuple[float, float, float]] = deque()      # (t, |acc|, vibration)
        self._ref = (0.0, 0.0)
        self._ref_at = -1e18
        self._changes: deque[float] = deque(maxlen=300)
        self._last_hit = -1e18
        self._quiet_since: float | None = None
        self._started: float | None = None
        self.cuts = 0
        self.confidence = 0.0
        self.points = 0

    def _prepare(self, frame: np.ndarray) -> np.ndarray:
        eye = eye_view(frame, self.stereo)
        h, w = eye.shape[:2]
        if self._size is None or self._size[2:] != (w, h):
            scale = min(1.0, self.width / max(w, 1))
            sw, sh = max(16, round(w * scale)), max(16, round(h * scale))
            self._size = (sw, sh, w, h)
            mask = _feature_mask(sw, sh, self.projection, self.settings.pole_limit_deg)
            if mask is None:
                mask = np.full((sh, sw), 255, np.uint8)
            for x, y, mw, mh in self.masks:                       # HUD, cockpit, mirrors: not part of the scene
                x0, y0 = int(max(0.0, x) * sw), int(max(0.0, y) * sh)
                x1, y1 = int(min(1.0, x + mw) * sw), int(min(1.0, y + mh) * sh)
                mask[y0:y1, x0:x1] = 0
            self._mask = mask
            self._threshold = 2.5 * pixel_angle(sw, self.projection, self.hfov_deg)
            self._prev = None
        sw, sh = self._size[0], self._size[1]
        small = cv2.resize(eye, (sw, sh), interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small

    def _track(self, prev: np.ndarray, gray: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray, float, bool]:
        """(rotation rate, scale-free velocity, confidence, cut) between two grey frames."""
        zero = np.zeros(3)
        p0 = _detect(prev, self.settings, self._mask)
        if len(p0) < 8:
            return zero, zero, 0.0, False
        p1, st1, _ = cv2.calcOpticalFlowPyrLK(prev, gray, p0, None, winSize=(17, 17), maxLevel=3,
                                              criteria=self._criteria)  # type: ignore[call-overload]
        p0r, st2, _ = cv2.calcOpticalFlowPyrLK(gray, prev, p1, None, winSize=(17, 17), maxLevel=3,
                                               criteria=self._criteria)  # type: ignore[call-overload]
        good = (st1.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm(p0 - p0r, axis=1) < 1.0)
        a, b = p0[good], p1[good]
        sw, sh = gray.shape[1], gray.shape[0]
        if self.projection == "360" and len(b):
            dx = b[:, 0] - a[:, 0]
            b = b.copy()
            b[:, 0] = np.where(dx > sw / 2, b[:, 0] - sw, np.where(dx < -sw / 2, b[:, 0] + sw, b[:, 0]))
        self.points = len(a)
        if tracking_collapsed(len(a) / len(p0), self._survival):
            return zero, zero, 0.0, True
        if len(a) < 8:
            return zero, zero, 0.0, False
        em = estimate(bearings(a, sw, sh, self.projection, self.hfov_deg),
                      bearings(b, sw, sh, self.projection, self.hfov_deg), self._threshold, self.rng, iterations=60)
        if not em.valid:
            return zero, zero, 0.0, False
        conf = float(min(1.0, em.inliers * min(1.0, len(a) / 60.0)))
        return to_platform(em.rotation) / dt, to_platform(em.direction) * em.speed / dt, conf, False

    def step(self, frame: np.ndarray, t: float) -> LiveSample | None:
        """One captured frame (BGR or grey) taken at time t. None for the first frame."""
        gray = self._prepare(frame)
        hist = _histogram(gray)
        if self._started is None:
            self._started = t
        if self._prev is None or self._prev_hist is None:
            self._prev, self._prev_hist, self._prev_t = gray, hist, t
            return None
        dt = t - self._prev_t
        if not 1e-3 < dt < 0.5:
            self._prev, self._prev_hist, self._prev_t = gray, hist, t
            self._v_last = self._v.copy()
            return None
        cut = cv2.compareHist(self._prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) > self.settings.cut_threshold  # type: ignore[arg-type]
        w, v, conf = np.zeros(3), np.zeros(3), 0.0
        if not cut:
            w, v, conf, cut = self._track(self._prev, gray, dt)
        self._prev, self._prev_hist, self._prev_t = gray, hist, t
        self.confidence = conf
        if cut:                                   # an edit, a respawn, a menu: nothing carries across it
            self.cuts += 1
            self._w[:] = 0.0
            self._v[:] = 0.0
            self._v2[:] = 0.0
            self._v_last[:] = 0.0
            self._acc[:] = 0.0
            self._acc2[:] = 0.0
            w, v, conf = np.zeros(3), np.zeros(3), 0.0
        elif conf < MIN_CONFIDENCE:
            w, v = np.zeros(3), np.zeros(3)
        # how long has there been no usable picture?
        if conf >= MIN_CONFIDENCE:
            self._quiet_since = None
        elif self._quiet_since is None:
            self._quiet_since = t
        lost = self._quiet_since is not None and t - self._quiet_since > LOST_AFTER_S

        change = float(np.linalg.norm(np.concatenate([w - self._w_raw, v - self._v_raw]))) / dt
        self._w_raw, self._v_raw = w.copy(), v.copy()
        self._w += (w - self._w) * _alpha(dt, TAU_RATES)
        self._v += (v - self._v) * _alpha(dt, TAU_VELOCITY)
        self._v2 += (self._v - self._v2) * _alpha(dt, TAU_VELOCITY)
        raw = (self._v2 - self._v_last) / dt
        self._v_last = self._v2.copy()
        forward = self._v2[0]
        raw[1] += forward * self._w[2]            # turning: sideways, towards the inside of the bend
        raw[2] -= forward * self._w[1]            # pitching: a dip presses into the seat, a crest lifts
        self._acc += (raw - self._acc) * _alpha(dt, TAU_ACCEL)
        self._acc2 += (self._acc - self._acc2) * _alpha(dt, TAU_ACCEL)
        # vibration: what the rotation does above 3 Hz
        self._w_low += (self._w - self._w_low) * _alpha(dt, 1.0 / (2.0 * math.pi * 3.0))
        high = float(np.sum((self._w - self._w_low) ** 2))
        self._vib += (high - self._vib) * _alpha(dt, 0.3)
        vib = math.sqrt(max(self._vib, 0.0))
        mag = float(np.linalg.norm(self._acc2))
        self._peak = max(self._peak * math.exp(-dt / 20.0), mag)
        self._scale.append((t, mag, vib))
        while self._scale and self._scale[0][0] < t - SCALE_WINDOW_S:
            self._scale.popleft()
        if t - self._ref_at >= 1.0 and len(self._scale) >= 20:
            arr = np.array(self._scale)
            self._ref = (float(np.percentile(arr[:, 1], 95.0)), float(np.percentile(arr[:, 2], 95.0)))
            self._ref_at = t
        warm = min(1.0, max(0.0, (t - self._started - 1.0) / 3.0))        # nothing for a second, then in over three
        # the scale: the strongest tenth of the last minute counts as `strength`; a moment stronger than
        # anything before lowers the gain at once (it is never amplified beyond 1 / PEAK_SHARE of that)
        gain = self.strength / max(self._ref[0], PEAK_SHARE * self._peak, ACCEL_FLOOR) * warm
        rates = self._w + (self._w - self._w_last) * (self.predict_s / dt) if self.predict_s > 0.0 else self._w
        self._w_last = self._w.copy()
        if self.align_s > 0.0:
            self._held.append((t, rates.copy()))
            while len(self._held) > 1 and self._held[1][0] <= t - self.align_s:
                self._held.popleft()
            rates = self._held[0][1] if self._held[0][0] <= t - self.align_s + 1e-9 else np.zeros(3)
        sample = LiveSample(
            accel=(float(self._acc2[0] * gain), float(self._acc2[1] * gain), float(self._acc2[2] * gain)),
            rates=(float(rates[0] * warm), float(rates[1] * warm), float(rates[2] * warm)),
            speed=0.0, rumble=min(1.0, vib / (1.5 * self._ref[1])) * warm if self._ref[1] > 1e-6 else 0.0,
            active=not lost, captured=t,
        )
        # a hit: a change of motion far outside what the last ten seconds showed (at most one a second)
        if len(self._changes) >= 30 and not cut and conf >= MIN_CONFIDENCE and t - self._last_hit > 1.0:
            arr = np.array(self._changes)
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med))) * 1.4826 + 1e-9
            high = float(np.percentile(arr, 95.0))
            if (change - med) / mad > 10.0 and change > 4.0 * high:
                sample.impact = min(1.0, change / (8.0 * high)) * warm
                self._last_hit = t
        self._changes.append(change)
        return sample


def _rects(value: Any, what: str) -> list[tuple[float, float, float, float]]:
    out: list[tuple[float, float, float, float]] = []
    for item in value or []:
        if not isinstance(item, list | tuple) or len(item) != 4:
            raise SourceError(f"{what}: each entry is [x, y, width, height] as fractions of the picture (0-1)")
        x, y, w, h = (float(c) for c in item)
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            raise SourceError(f"{what}: values are fractions of the picture, 0-1")
        out.append((x, y, w, h))
    return out


class ScreenSource(LiveSource):
    info = SCREEN
    OPTIONS = ("capture", "screen", "region", "device", "url", "file", "loop", "fps", "width", "projection",
               "hfov_deg", "stereo", "mask", "mode", "delay_s", "motion_lead_s", "strength", "predict_s", "sound",
               "sound_device", "sound_cutoff_hz", "scenes")

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__()
        o = dict(options)
        self.capture = str(o.get("capture", "file" if o.get("file") else "screen"))
        if self.capture not in ("screen", "camera", "stream", "file"):
            raise SourceError('[source] capture must be "screen", "camera", "stream" or "file"')
        self.screen = int(o.get("screen", 0))
        region = o.get("region")
        if region is not None and (not isinstance(region, list | tuple) or len(region) != 4):
            raise SourceError("[source] region is [x, y, width, height] in pixels of the screen")
        self.region = tuple(int(c) for c in region) if region is not None else None
        self.device = o.get("device", 0)
        self.url = str(o.get("url", ""))
        self.file = str(o.get("file", ""))
        self.loop = bool(o.get("loop", False))
        self.fps = float(o.get("fps", 30.0))
        if not 5.0 <= self.fps <= 60.0:
            raise SourceError("[source] fps must be 5-60")
        self.mode = str(o.get("mode", "delay"))
        if self.mode not in ("delay", "direct"):
            raise SourceError('[source] mode must be "delay" or "direct"')
        self.delay_s = float(o.get("delay_s", 0.35)) if self.mode == "delay" else 0.0
        self.motion_lead_s = float(o.get("motion_lead_s", 0.05))
        if not 0.0 <= self.delay_s <= 3.0 or not 0.0 <= self.motion_lead_s <= 0.5:
            raise SourceError("[source] delay_s must be 0-3 s and motion_lead_s 0-0.5 s")
        if self.capture == "file" and not self.file:
            raise SourceError('[source] capture = "file" needs file = "video.mp4"')
        if self.capture == "stream" and not self.url:
            raise SourceError('[source] capture = "stream" needs url = "rtsp://..." (or udp://, http://)')
        self.motion = PictureMotion(
            str(o.get("projection", "flat")), float(o.get("hfov_deg", 90.0)), str(o.get("stereo", "mono")),
            int(o.get("width", 480)), _rects(o.get("mask"), "[source] mask"), float(o.get("strength", 7.0)),
            float(o.get("predict_s", 0.03 if self.mode == "direct" else 0.0)),
            SMOOTHING_LAG_S - TAU_RATES if self.mode == "delay" else 0.0,
        )
        self.suggested_comfort = 0.5 if self.mode == "direct" else None
        self.suggested_input_lp_hz = INPUT_LP_HZ
        self.needs_gui = self.capture == "screen"
        self.on_frame: Callable[[np.ndarray, float], None] | None = None     # calibration taps the frames here
        from .sound import tap_for_picture

        self._sound: Any = tap_for_picture(o, self.delay_s)
        self.feed: Any = self._sound.feed if self._sound is not None else None
        self.scenes: Any = None
        self._want_scenes = bool(o.get("scenes", False))
        self._cap: Any = None
        self._cond = threading.Condition()
        self._newest: tuple[np.ndarray, float] | None = None
        self._pictures: deque[tuple[float, np.ndarray]] = deque()
        self._pending: deque[tuple[float, LiveSample]] = deque()
        self._capture_thread: threading.Thread | None = None
        self.frames = 0
        self.dropped = 0
        self.late = 0
        self.analysis_ms = 0.0
        self.capture_fps = 0.0
        self._frame_times: deque[float] = deque(maxlen=60)
        self.ended = False

    # ------------------------------------------------------------------ frames in
    def push_frame(self, frame: np.ndarray, t: float) -> None:
        """A captured frame (BGR) taken at monotonic time t - from our own capture thread, or from the
        live window when the screen itself is grabbed."""
        tap = self.on_frame
        if tap is not None:
            tap(frame, t)
        self._frame_times.append(t)
        if len(self._frame_times) > 2:
            self.capture_fps = (len(self._frame_times) - 1) / max(self._frame_times[-1] - self._frame_times[0], 1e-6)
        with self._cond:
            if self._newest is not None:
                self.dropped += 1                 # the analysis is behind: the older frame is not analysed
            self._newest = (frame, t)
            if self.mode == "delay":
                self._pictures.append((t, frame))
                while self._pictures and self._pictures[0][0] < t - self.delay_s - 1.0:
                    self._pictures.popleft()
            else:
                self._pictures.clear()
                self._pictures.append((t, frame))
            self._cond.notify()

    def picture_at(self, now: float) -> np.ndarray | None:
        """The frame to show at `now`: the newest one that is at least ``delay_s`` old."""
        with self._cond:
            best = None
            for t, frame in self._pictures:
                if t <= now - self.delay_s:
                    best = frame
                else:
                    break
            return best

    def set_delay(self, delay_s: float) -> float:
        """Change the picture delay while running (the operator's [ and ] keys)."""
        if self.mode != "delay":
            return 0.0
        new = min(3.0, max(0.1, delay_s))
        if self.feed is not None:
            self.feed.delay_s += new - self.delay_s
        self.delay_s = new
        return new

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        self.ended = False
        if self.capture == "screen":
            return                                # the live window grabs and pushes
        target: Any = self.file if self.capture == "file" else self.url if self.capture == "stream" else self.device
        if self.capture == "camera":
            cap = cv2.VideoCapture(target, cv2.CAP_V4L2) if isinstance(target, int) else cv2.VideoCapture(str(target))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # the newest frame, not a queue of old ones
        else:
            cap = cv2.VideoCapture(str(target))
        if not cap.isOpened():
            cap.release()
            raise SourceError(f"cannot open the {self.capture} {target!r}")
        self._cap = cap
        if self._sound is not None:
            self._sound.start()
        if self._want_scenes:
            from .scenes import LiveScenes

            self.scenes = LiveScenes(self.motion.projection)
        self._capture_thread = threading.Thread(target=self._capture_loop, name="live-capture", daemon=True)
        self._capture_thread.start()

    def close(self) -> None:
        if self._capture_thread is not None and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2.0)
        self._capture_thread = None
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
        if self._sound is not None:
            self._sound.stop()
        if self.scenes is not None:
            self.scenes.stop()

    def _capture_loop(self) -> None:
        cap = self._cap
        period = 1.0 / self.fps
        file_fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0 if self.capture == "file" else 0.0
        start = time.monotonic()
        index = 0
        next_at = start
        while not self._stop.is_set():
            if self.capture == "file":
                # play the file in real time: the frame that is due now, skipping what the rate does not need
                due = int((time.monotonic() - start) * file_fps)
                while index < due:
                    if not cap.grab():
                        break
                    index += 1
                ok, frame = cap.read()
                index += 1
                if not ok:
                    if self.loop:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        start, index = time.monotonic(), 0
                        continue
                    self.ended = True
                    break
                self.push_frame(frame, start + (index - 1) / file_fps)
            else:
                ok, frame = cap.read()
                if not ok:
                    self.last_error = f"the {self.capture} delivers no picture"
                    self._stop.wait(0.2)
                    continue
                self.push_frame(frame, time.monotonic())
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_at = time.monotonic()

    def run(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                now = time.monotonic()
                wait = 0.05
                if self._pending:
                    wait = min(wait, max(0.0, self._pending[0][0] - now))
                if self._newest is None and wait > 0.0:
                    self._cond.wait(wait)
                item, self._newest = self._newest, None
            now = time.monotonic()
            while self._pending and self._pending[0][0] <= now:
                _due, due_sample = self._pending.popleft()
                self.emit(due_sample, now)
            if item is None:
                continue
            frame, t = item
            started = time.monotonic()
            sample = self.motion.step(frame, t)
            self.frames += 1
            took = (time.monotonic() - started) * 1000.0
            self.analysis_ms += (took - self.analysis_ms) * 0.1
            if self.scenes is not None:
                self.scenes.offer(frame, t)
            if sample is None:
                continue
            if self.scenes is not None:
                sample.effects.update(self.scenes.levels(t))
                sample.params.update(self.scenes.params())
            if self.mode == "delay":
                # the motion that belongs to the frame taken at t is due when that frame is shown, less
                # the lead; the smoothing's lag is taken out of the wait, as long as there is wait to take
                due = t + self.delay_s - self.motion_lead_s - SMOOTHING_LAG_S - 0.05
                now = time.monotonic()
                if due < now:
                    self.late += 1                # the analysis took longer than the delay leaves room for
                self._pending.append((max(due, now), sample))
            else:
                self.emit(sample, time.monotonic())

    def status(self) -> dict[str, Any]:
        st = super().status()
        st.update({
            "capture": self.capture, "mode": self.mode, "delay_ms": round(self.delay_s * 1000.0),
            "analysis_ms": round(self.analysis_ms, 1), "capture_fps": round(self.capture_fps, 1),
            "frames": self.frames, "dropped": self.dropped, "late": self.late, "cuts": self.motion.cuts,
            "confidence": round(self.motion.confidence, 2), "points": self.motion.points,
        })
        if self.scenes is not None:
            st["scenes"] = self.scenes.status()
        if self.ended:
            st["error"] = "the video file has ended"
        return st
