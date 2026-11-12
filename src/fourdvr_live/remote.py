"""The show in a process of its own, with the live window as its client.

A window in the same process as the control loop is a risk to the platforms. While a window grabs
a screen or paints a large picture, Python lets no other thread of the process run - measured:
40 to 80 ms for one grab of a 3840 x 2160 screen - and a board that misses its commands for its
time-out stops with a fault. So whenever there is a window, the show itself (source, control loop,
platform links, effects, sound) runs in a second process, which the window starts:

    the window's process                                the engine's process
    captures, analyses and shows a picture     --->     samples, one line each
    the operator's keys                        --->     commands
    the overlay                                <---     the status, 20 times a second

Both directions are lines of JSON on the engine's standard input and output. A telemetry source
lives in the engine's process; only a picture source lives in the window's, because that is where
the picture is. If the window is closed or dies, the engine fades to rest, brings the platforms
to neutral and ends; if the window stops answering, the engine holds until it answers again.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

from .sample import AXES, LiveSample
from .sources import SourceError
from .sources.base import PICTURE_INPUT_LP_HZ, SCREEN, LiveSource, Sink

log = logging.getLogger(__name__)

STATUS_HZ = 20.0
WATCHDOG_S = 3.0                # the window says "alive" several times a second; silent for this long: hold
_TUPLES = ("accel", "rates", "attitude")
_FIELDS = tuple(f.name for f in fields(LiveSample))
_BLANK = LiveSample()


# =========================================================================== samples on the wire
def to_wire(sample: LiveSample) -> dict[str, Any]:
    """A sample as a JSON object: only what differs from an empty sample."""
    out: dict[str, Any] = {}
    for name in _FIELDS:
        value = getattr(sample, name)
        if value != getattr(_BLANK, name):
            out[name] = list(value) if isinstance(value, tuple) else value
    return out


def from_wire(data: dict[str, Any]) -> LiveSample:
    values: dict[str, Any] = {}
    for name in _FIELDS:
        if name in data:
            value = data[name]
            values[name] = tuple(value) if name in _TUPLES and value is not None else value
    return LiveSample(**values)


# =========================================================================== the engine's side
class RemoteSource(LiveSource):
    """The engine's end of a picture source that lives in the window's process: the samples arrive
    as lines, the sound for the shakers (if any) is captured here, where it is played."""

    info = SCREEN

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__()
        from .sources.sound import tap_for_picture

        mode = str(options.get("mode", "delay"))
        self.delay_s = float(options.get("delay_s", 0.35)) if mode == "delay" else 0.0
        self.suggested_comfort = 0.5 if mode == "direct" else None
        self.suggested_input_lp_hz = PICTURE_INPUT_LP_HZ
        self._sound = tap_for_picture(options, self.delay_s)
        self.feed: Any = self._sound.feed if self._sound is not None else None

    def start(self, sink: Sink) -> None:
        self._sink = sink
        if self._sound is not None:
            self._sound.start()

    def stop(self) -> None:
        self._sink = None
        if self._sound is not None:
            self._sound.stop()

    def offer(self, sample: LiveSample) -> None:
        self.emit(sample, time.monotonic())

    def set_delay(self, delay_s: float) -> None:
        """The operator changed the picture's delay: the shakers' sound follows."""
        if self.feed is not None:
            self.feed.delay_s += delay_s - self.delay_s
        self.delay_s = delay_s


def serve(engine: Any, source_in: Any = None, out: Any = None, watchdog_s: float = WATCHDOG_S) -> int:
    """Run the show for a window in another process (``fourdvr live run ... --control``)."""
    from .cli import report_lines

    source_in = source_in if source_in is not None else sys.stdin
    out = out if out is not None else sys.stdout
    write_lock = threading.Lock()
    done = threading.Event()
    seen = {"alive": time.monotonic(), "held_by_watchdog": False}

    def send(message: dict[str, Any]) -> bool:
        try:
            with write_lock:
                out.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
                out.flush()
            return True
        except (OSError, ValueError):             # the window is gone
            done.set()
            return False

    def command(message: dict[str, Any]) -> None:
        name = message.get("c")
        value: Any = message.get("v")
        seen["alive"] = time.monotonic()
        if name == "hold":
            seen["held_by_watchdog"] = False
            engine.hold(bool(value))
        elif name == "gain":
            engine.set_gain(float(value))
        elif name == "engage":
            seen["held_by_watchdog"] = False
            engine.engage()
        elif name == "record":
            if value:
                if engine.recorder is None:
                    engine.start_recording()
            else:
                saved = engine.stop_recording(message.get("path") or None)
                send({"saved": str(saved) if saved is not None else None})
        elif name == "delay" and hasattr(engine.source, "set_delay"):
            engine.source.set_delay(float(value))
        elif name == "quit":
            done.set()

    def read() -> None:
        offer = getattr(engine.source, "offer", None)
        try:
            for line in source_in:
                try:
                    message = json.loads(line)
                    if "s" in message:
                        if offer is not None:
                            offer(from_wire(message["s"]))
                    else:
                        command(message)
                except (ValueError, TypeError, KeyError) as exc:
                    log.warning("a line from the live window was not understood: %s", exc)
                if done.is_set():
                    break
        except (OSError, ValueError):
            pass
        finally:
            done.set()                            # the window has closed, or is gone

    def report_status() -> None:
        while not done.wait(1.0 / STATUS_HZ):
            if not send({"status": engine.status()}):
                return

    try:
        engine.start()
    except (SourceError, OSError, ValueError) as exc:
        send({"error": str(exc)})
        return 1
    send({"ready": True})
    seen["alive"] = time.monotonic()
    threading.Thread(target=read, name="live-window-commands", daemon=True).start()
    threading.Thread(target=report_status, name="live-window-status", daemon=True).start()
    try:
        while not done.wait(0.1):
            silent = time.monotonic() - seen["alive"]
            if watchdog_s and silent > watchdog_s and not engine.guard.hold:
                log.warning("the live window has not answered for %.0f s: holding", silent)
                engine.hold(True)
                seen["held_by_watchdog"] = True
            elif seen["held_by_watchdog"] and silent < 1.0:
                engine.hold(False)
                seen["held_by_watchdog"] = False
    except KeyboardInterrupt:
        pass
    done.set()
    saved = engine.stop_recording() if engine.recorder is not None else None
    engine.stop()
    send({"report": report_lines(engine, saved)})
    return 0


# =========================================================================== the window's side
def blank_status(title: str = "") -> dict[str, Any]:
    return {
        "state": "starting", "envelope": 0.0, "hold": False, "gain": 1.0,
        "source": {"source": "", "title": title, "samples": 0, "rate_hz": 0.0, "error": None},
        "rejected": 0, "age_ms": None, "delay_ms": None, "pose": dict.fromkeys(AXES, 0.0), "levels": {},
        "platforms": [], "ticks": 0, "overruns": 0, "max_tick_ms": 0.0, "max_late_ms": 0.0, "recording_s": None,
        "ranges": None, "problem": "",
    }


class RemoteEngine:
    """The window's handle on the engine's process: the calls the window makes on an engine."""

    def __init__(self, argv: list[str], source: Any = None, audio_backend: str = "", title: str = "") -> None:
        self.source = source                      # a picture source, which lives in this process - or None
        self.command = [sys.executable, *(["-I"] if sys.flags.isolated else []), "-m", "fourdvr_studio", "live",
                        *argv, "--no-window", "--control"]
        if source is not None:
            self.command.append("--remote-source")
        if audio_backend == "null":
            self.command.append("--null-audio")
        self.error = ""
        self.report: list[str] = []
        self.ended = False
        self._proc: subprocess.Popen[str] | None = None
        self._status = blank_status(title)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._saved = threading.Event()
        self._saved_path: str | None = None

    # ------------------------------------------------------------------ the process
    def start(self, timeout: float = 30.0) -> None:
        """Start the engine's process and wait until the show runs. Raises SourceError with the
        engine's own message if it cannot."""
        self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                      encoding="utf-8", bufsize=1)
        threading.Thread(target=self._read, name="live-engine-status", daemon=True).start()
        if not self._ready.wait(timeout):
            self.stop(2.0)
            raise SourceError("the show did not start in time")
        if self.error or self.ended:
            message = self.error or "the show could not start (its message is above)"
            self.stop(2.0)
            raise SourceError(message)
        if self.source is not None:
            try:
                self.source.start(self.offer)
            except SourceError:
                self.stop(5.0)
                raise

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue                      # not ours: something printed into the channel
                if not isinstance(message, dict):
                    continue
                if "status" in message:
                    with self._lock:
                        self._status = message["status"]
                elif "ready" in message:
                    self._ready.set()
                elif "error" in message:
                    self.error = str(message["error"])
                    self._ready.set()
                elif "saved" in message:
                    self._saved_path = message["saved"]
                    self._saved.set()
                elif "report" in message:
                    self.report = [str(line) for line in message["report"]]
        except (OSError, ValueError):
            pass
        self.ended = True
        self._ready.set()
        self._saved.set()

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self.ended:
            return
        try:
            with self._write_lock:
                proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            self.ended = True

    def stop(self, timeout: float = 14.0) -> list[str]:
        """End the show: fade to rest, platforms to neutral. Returns the engine's closing report."""
        if self.source is not None:
            self.source.stop()
        proc = self._proc
        if proc is None:
            return self.report
        self._send({"c": "quit"})
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3.0)
        deadline = time.monotonic() + 1.0         # the reader takes the last lines (the report)
        while not self.ended and time.monotonic() < deadline:
            time.sleep(0.01)
        self._proc = None
        return self.report

    # ------------------------------------------------------------------ what the window asks and says
    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._status)
        if self.source is not None:
            st["source"] = self.source.status()   # the picture is captured and analysed here
        if self.ended and self._proc is not None:
            st["state"], st["envelope"] = "ended", 0.0
            st["problem"] = self.error or "the show's process has ended (its message is in the terminal)"
        return st

    def _patch(self, **values: Any) -> None:
        """What the operator just set, shown at once; the engine's next status confirms it."""
        with self._lock:
            self._status = {**self._status, **values}

    def offer(self, sample: LiveSample, _now: float = 0.0) -> None:
        self._send({"s": to_wire(sample)})

    def alive(self) -> None:
        self._send({"c": "alive"})

    def hold(self, on: bool = True) -> None:
        self._patch(hold=bool(on))
        self._send({"c": "hold", "v": bool(on)})

    def engage(self) -> None:
        self._patch(hold=False)
        self._send({"c": "engage"})

    def set_gain(self, gain: float) -> float:
        value = min(2.0, max(0.0, float(gain)))
        self._patch(gain=value)
        self._send({"c": "gain", "v": value})
        return value

    def picture_delay_changed(self, delay_s: float) -> None:
        self._send({"c": "delay", "v": float(delay_s)})

    def start_recording(self) -> None:
        self._patch(recording_s=0.0)
        self._send({"c": "record", "v": True})

    def stop_recording(self, path: str | Path | None = None, timeout: float = 8.0) -> Path | None:
        self._saved.clear()
        self._saved_path = None
        self._patch(recording_s=None)
        self._send({"c": "record", "v": False, "path": str(path) if path else None})
        self._saved.wait(timeout)
        return Path(self._saved_path) if self._saved_path else None
