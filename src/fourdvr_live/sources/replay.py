"""Sources that need no game: the built-in demonstration drive and a recorded session played again."""

from __future__ import annotations

import time

from ..sample import LiveSample
from ..session import Session, SessionError
from .base import DEMO, REPLAY, LiveSource, SourceError


class DemoSource(LiveSource):
    """The simulator's drive, delivered at 60 samples a second as a game would."""

    info = DEMO

    def __init__(self, seconds: float = 0.0, loop: bool = True, rate_hz: float = 60.0) -> None:
        super().__init__()
        self.seconds, self.loop, self.rate = seconds, loop, rate_hz

    def run(self) -> None:
        from ..sim import LAP_S, Drive

        drive = Drive()
        start = time.monotonic()
        k = 0
        while not self._stop.is_set():
            t = k / self.rate
            if (self.seconds and t >= self.seconds) or (not self.loop and t >= LAP_S):
                break
            self.emit(drive.at(t, self.loop).sample, time.monotonic())
            k += 1
            delay = start + k / self.rate - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)


class ReplaySource(LiveSource):
    """A session file delivered again as it was recorded (the directions were already corrected then)."""

    info = REPLAY

    def __init__(self, path: str, loop: bool = False) -> None:
        super().__init__()
        self.path, self.loop = path, loop
        self.session: Session | None = None

    def open(self) -> None:
        try:
            self.session = Session.load(self.path)
        except SessionError as exc:
            raise SourceError(str(exc)) from exc

    def run(self) -> None:
        s = self.session
        assert s is not None
        sc = s.scalars
        names = s.effect_names
        start = time.monotonic()
        k = 0
        while not self._stop.is_set():
            i = k % s.samples if self.loop and s.samples else k
            if i >= s.samples:
                break
            att = s.attitude[i]
            sample = LiveSample(
                accel=(float(s.accel[i, 0]), float(s.accel[i, 1]), float(s.accel[i, 2])),
                rates=(float(s.rates[i, 0]), float(s.rates[i, 1]), float(s.rates[i, 2])),
                attitude=None if att[0] != att[0] else (float(att[0]), float(att[1])),
                speed=float(sc["speed"][i]), rpm=float(sc["rpm"][i]), rpm_max=float(sc["rpm_max"][i]),
                rpm_idle=float(sc["rpm_idle"][i]), throttle=float(sc["throttle"][i]), brake=float(sc["brake"][i]),
                rumble=float(sc["rumble"][i]), slip=float(sc["slip"][i]), impact=float(sc["impact"][i]),
                active=bool(sc["envelope"][i] > 0.0), time=i / s.rate_hz,
            )
            for j, name in enumerate(names):
                if name not in ("wind", "vibration", "air") and s.effects[i, j] > 0.0:
                    sample.effects[name] = float(s.effects[i, j])      # what the source itself had stated
            self.emit(sample, time.monotonic())
            k += 1
            delay = start + k / s.rate_hz - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
