"""Following the theatre's show: the stream's timeline is the Control Server's.

The Control Server is not changed for this. A stream server signs in like a person with the lowest
role ("viewer"), reads the show's state a few times a second and compares clocks with it:

    POST /api/v1/auth/login          {"username", "password"} -> a token
    GET  /api/v1/playback/state      the timeline: programme, playing / paused / stopped, epoch, start_at
    GET  /api/v1/time                the server's clock
    GET  /api/v1/programmes          which video belongs to a programme (file name, checksum, projection)

PLAY is announced by the Control Server a few seconds before it happens (its start lead), so the
viewers of the stream stand ready at the first frame and start with the headsets and the platforms.
The video itself is taken from this computer: the programme's file must be among the served ones
(matched by its number in stream.toml, by checksum, or by file name).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .server import PAUSED, PLAYING, STOPPED, StreamServer

log = logging.getLogger(__name__)


class TheatreError(RuntimeError):
    pass


class TheatreFollower(threading.Thread):
    def __init__(self, stream: StreamServer, url: str, user: str, password: str, interval_s: float = 0.25,
                 timeout_s: float = 4.0, loss_timeout_s: float = 10.0) -> None:
        super().__init__(name="stream-follows-theatre", daemon=True)
        self.stream = stream
        self.url = url.rstrip("/")
        self.user, self.password = user, password
        self.interval_s, self.timeout_s, self.loss_timeout_s = interval_s, timeout_s, loss_timeout_s
        self.token = ""
        self.offset: float | None = None          # the Control Server's clock minus ours
        self.offset_rtt = float("inf")
        self.offset_at = 0.0
        self.connected = False
        self.last_error = ""
        self.last_ok = 0.0
        self.polls = 0
        self.programmes: dict[int, dict[str, Any]] = {}
        self.missing: set[int] = set()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ talking to the Control Server
    def _call(self, method: str, path: str, body: Any = None, auth: bool = True) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.token = ""
            raise TheatreError(f"{method} {path}: the Control Server answers {exc.code}"
                               + (" (user name or password?)" if exc.code in (401, 403) else "")) from exc
        except (OSError, ValueError) as exc:
            raise TheatreError(f"the Control Server at {self.url} does not answer ({exc})") from exc

    def login(self) -> None:
        answer = self._call("POST", "/api/v1/auth/login", {"username": self.user, "password": self.password}, auth=False)
        self.token = str(answer["token"])

    def compare_clocks(self, rounds: int = 6) -> None:
        for _ in range(rounds):
            t0 = time.monotonic()
            ours0 = self.stream.clock.now()
            answer = self._call("GET", "/api/v1/time", auth=False)
            rtt = time.monotonic() - t0
            self._clock_sample(float(answer["master_time"]) - (ours0 + rtt / 2.0), rtt)

    def _clock_sample(self, offset: float, rtt: float) -> None:
        """Keep the estimate of the exchange with the shortest round trip; let it age."""
        now = time.monotonic()
        if self.offset is None or rtt <= self.offset_rtt or now - self.offset_at > 30.0:
            self.offset, self.offset_rtt, self.offset_at = offset, rtt, now

    def load_programmes(self) -> None:
        listed = self._call("GET", "/api/v1/programmes")
        self.programmes = {int(p["programme_id"]): p for p in listed}

    # ------------------------------------------------------------------ one look at the show
    def poll(self) -> None:
        if not self.token:
            self.login()
            self.compare_clocks()
            self.load_programmes()
        t0 = time.monotonic()
        ours0 = self.stream.clock.now()
        snap = self._call("GET", "/api/v1/playback/state")
        rtt = time.monotonic() - t0
        self._clock_sample(float(snap["server_time"]) - (ours0 + rtt / 2.0), rtt)
        self.polls += 1
        self.apply(snap)

    def apply(self, snap: dict[str, Any]) -> None:
        tl = snap["timeline"]
        state, pid = str(tl.get("state", "IDLE")), tl.get("programme_id")
        offset = self.offset or 0.0
        black = bool(snap.get("black_screen"))
        if pid is None or state == "IDLE":
            self.stream.follow(None, STOPPED, black=black)
            return
        pid = int(pid)
        if pid not in self.programmes:
            self.load_programmes()
        prog = self.programmes.get(pid, {})
        video = prog.get("video") or {}
        item = self.stream.library.for_programme(pid, str(video.get("filename") or ""), str(video.get("sha256") or ""))
        name = str(prog.get("name") or f"programme {pid}")
        if item is None:
            if pid not in self.missing:
                self.missing.add(pid)
                log.warning("programme %d (%s, video %s) is not among the served videos", pid, name, video.get("filename"))
            self.stream.follow(None, STOPPED, black=black,
                               note=f"{name}: its video ({video.get('filename') or 'no file'}) is not on the stream server")
            return
        if prog.get("projection") in ("flat", "180", "360"):       # what the theatre says the video is
            item.projection = str(prog["projection"])
        if prog.get("stereo") in ("mono", "sbs", "ou"):
            item.stereo = str(prog["stereo"])
        duration = float(tl.get("duration_s") or item.duration_s or 0.0)
        loop = bool(tl.get("loop"))
        position = float(tl.get("position_s") or 0.0)
        if state == "PLAYING" and tl.get("epoch") is not None:
            start_at = float(tl["start_at"]) - offset if tl.get("start_at") else 0.0
            self.stream.follow(item.id, PLAYING, epoch=float(tl["epoch"]) - offset, position_s=position, duration_s=duration,
                               loop=loop, black=black, start_at=start_at)
        elif state in ("PAUSED", "PREPARING"):
            self.stream.follow(item.id, PAUSED, position_s=position, duration_s=duration, loop=loop, black=black,
                               note="the show is being prepared" if state == "PREPARING" else "")
        else:
            self.stream.follow(item.id, STOPPED, duration_s=duration, loop=loop, black=black)

    # ------------------------------------------------------------------ the thread
    def run(self) -> None:
        wait = self.interval_s
        while not self._stop.wait(wait):
            try:
                self.poll()
                if not self.connected:
                    log.info("following the show of the Control Server at %s", self.url)
                self.connected, self.last_error, self.last_ok, wait = True, "", time.monotonic(), self.interval_s
            except (TheatreError, KeyError, TypeError, ValueError) as exc:
                message = str(exc) if isinstance(exc, TheatreError) else f"an answer of the Control Server was not understood ({exc!r})"
                if message != self.last_error:
                    log.warning("%s", message)
                self.connected, self.last_error = False, message
                wait = min(5.0, max(1.0, wait * 2.0))
                if self.last_ok and time.monotonic() - self.last_ok > self.loss_timeout_s:
                    self.stream.follow(None, STOPPED, note="the Control Server does not answer")
                elif not self.last_ok:
                    self.stream.follow(None, STOPPED, note=message)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {"url": self.url, "connected": self.connected, "error": self.last_error or None, "polls": self.polls,
                "clock_offset_s": None if self.offset is None else round(self.offset, 4),
                "clock_round_trip_ms": None if self.offset is None else round(self.offset_rtt * 1000.0, 1)}
