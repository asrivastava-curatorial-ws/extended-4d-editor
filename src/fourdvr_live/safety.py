"""What stands between a live source and the cueing.

A recorded programme was checked before the show; a live source was not, so:

* a value that is not a finite number is refused, extreme values are cut (``LiveSample.clamped``);
* if no fresh sample arrives for ``stall_s`` (game closed, cable pulled, stream stuck), or the source
  says it is paused / in a menu, motion and effects **fade to rest** and the cueing forgets its
  state; they fade back in only after the source has been steady again for ``resume_s`` (a hiccup
  shorter than the fade costs a dip, not a restart);
* the operator's *hold* does the same on request.

The fade is an envelope 0..1 that multiplies the cue (and gates the effects). The platform's own
limiter and the fixtures' limits stay in force underneath, as for any programme.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .filters import smootherstep
from .sample import AXES, LiveSample


@dataclass
class SafetySettings:
    stall_s: float = 0.25            # no fresh sample for this long: fade to rest
    resume_s: float = 0.5            # steady again for this long before fading back in
    fade_out_s: float = 1.0
    fade_in_s: float = 2.5

    def check(self) -> None:
        if not 0.05 <= self.stall_s <= 5.0:
            raise ValueError("stall_s must be 0.05-5 s")
        if not 0.2 <= self.fade_out_s <= 10.0 or not 0.5 <= self.fade_in_s <= 20.0:
            raise ValueError("fade_out_s must be 0.2-10 s and fade_in_s 0.5-20 s")
        if not 0.0 <= self.resume_s <= 10.0:
            raise ValueError("resume_s must be 0-10 s")


class AxisMap:
    """Per-axis factor on a source's values (``[axes]`` in the configuration): -1 turns a direction
    round, 0 switches an axis off, other values scale it."""

    def __init__(self, factors: dict[str, float] | None = None) -> None:
        f = dict.fromkeys(AXES, 1.0)
        for name, value in (factors or {}).items():
            if name not in AXES:
                raise ValueError(f"unknown axis {name!r} (axes: {', '.join(AXES)})")
            f[name] = float(value)
        self.factors = f
        self.accel = (f["surge"], f["sway"], f["heave"])          # plain numbers: a sample stays plain numbers
        self.rates = (f["roll"], f["pitch"], f["yaw"])

    def apply(self, sample: LiveSample) -> LiveSample:
        a, r = self.accel, self.rates
        sample.accel = (sample.accel[0] * a[0], sample.accel[1] * a[1], sample.accel[2] * a[2])
        sample.rates = (sample.rates[0] * r[0], sample.rates[1] * r[1], sample.rates[2] * r[2])
        if sample.attitude is not None:
            sample.attitude = (sample.attitude[0] * r[0], sample.attitude[1] * r[1])
        return sample


REST = "rest"
WAITING = "waiting for the source"
STALLED = "source stalled"
PAUSED = "source paused"
HOLD = "held by the operator"
RESUMING = "resuming"
RUNNING = "running"


class InputGuard:
    """Holds the newest good sample of a source and decides, every control period, whether the show
    may move (`step`). `offer` is called from the source's thread, `step` from the control thread."""

    def __init__(self, settings: SafetySettings | None = None, axes: AxisMap | None = None) -> None:
        self.s = settings or SafetySettings()
        self.s.check()
        self.axes = axes or AxisMap()
        self._lock = threading.Lock()
        self._sample: LiveSample | None = None
        self._arrived = 0.0
        self.accepted = 0
        self.rejected = 0
        self.hold = False
        self.state = WAITING
        self.envelope = 0.0            # 0..1 after shaping
        self._ramp = 0.0               # 0..1 linear
        self._good_since: float | None = None
        self._last: float | None = None
        self.resets = 0                # how often the show came back from rest (the cueing is reset then)
        self._rested = True

    # ------------------------------------------------------------------ source thread
    def offer(self, sample: LiveSample, now: float) -> bool:
        if not sample.finite():
            with self._lock:
                self.rejected += 1
            return False
        sample = self.axes.apply(sample.clamped())
        with self._lock:
            self._sample, self._arrived = sample, now
            self.accepted += 1
        return True

    def latest(self) -> tuple[LiveSample | None, float]:
        with self._lock:
            return self._sample, self._arrived

    # ------------------------------------------------------------------ control thread
    def step(self, now: float) -> tuple[LiveSample | None, float, bool]:
        """(sample to use or None, envelope 0..1, reset): `reset` is True in the one step in which the
        show starts to come back from rest - the moment to clear the cueing's memory."""
        s = self.s
        dt = 0.0 if self._last is None else min(max(now - self._last, 0.0), 0.5)
        self._last = now
        sample, arrived = self.latest()
        fresh = sample is not None and now - arrived <= s.stall_s
        if self.hold:
            ok, why = False, HOLD
        elif sample is None:
            ok, why = False, WAITING
        elif not fresh:
            ok, why = False, STALLED
        elif not sample.active:
            ok, why = False, PAUSED
        else:
            ok, why = True, RUNNING
        reset = False
        if ok:
            if self._good_since is None:
                self._good_since = now
            if now - self._good_since >= s.resume_s:
                if self._rested:
                    reset, self._rested = True, False
                    self.resets += 1
                self._ramp = min(1.0, self._ramp + dt / s.fade_in_s)
                self.state = RUNNING if self._ramp >= 1.0 else RESUMING
            else:
                self.state = RESUMING          # back, but not yet trusted: neither further down nor up
        else:
            self._good_since = None
            self._ramp = max(0.0, self._ramp - dt / s.fade_out_s)
            self.state = why
        if self._ramp <= 0.0:
            self._rested = True
        self.envelope = smootherstep(self._ramp)
        use = sample if (ok and not self._rested) else None
        return use, self.envelope, reset
