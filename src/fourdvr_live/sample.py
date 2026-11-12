"""The one data model every live source delivers.

Axes are those of the motion platform (docs/LIVE.md, "Directions"):

=========  ==========================================  =========
value      positive when                               unit
=========  ==========================================  =========
surge      the vehicle accelerates forwards            m/s^2
sway       it accelerates to the left (a left turn)    m/s^2
heave      it accelerates upwards (a dip, a pull-up)   m/s^2
roll       the right side goes down                    rad/s
pitch      the nose goes down                          rad/s
yaw        the nose turns left                         rad/s
=========  ==========================================  =========

``accel`` is what a passenger feels beyond resting weight: zero when standing still or cruising
straight on level ground. Sources that include gravity in the vertical value may leave it in - the
heave channel only passes changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

G = 9.80665
AXES = ("surge", "sway", "heave", "roll", "pitch", "yaw")
ACCEL_LIMIT = 80.0        # m/s^2: anything beyond is a glitch (8 g), and is cut to this
RATE_LIMIT = 12.0         # rad/s


@dataclass
class LiveSample:
    accel: tuple[float, float, float] = (0.0, 0.0, 0.0)     # surge, sway, heave (m/s^2)
    rates: tuple[float, float, float] = (0.0, 0.0, 0.0)     # roll, pitch, yaw (rad/s)
    attitude: tuple[float, float] | None = None              # roll, pitch (rad), same signs; None: not known
    speed: float = 0.0                # m/s (never negative)
    rpm: float = 0.0                  # engine speed; 0: not known / stopped
    rpm_max: float = 0.0              # 0: not known
    rpm_idle: float = 0.0
    throttle: float = 0.0             # 0..1
    brake: float = 0.0                # 0..1
    gear: int = 0                     # -1 reverse, 0 neutral
    rumble: float = 0.0               # 0..1 road surface / kerbs / suspension working
    slip: float = 0.0                 # 0..1 tyres sliding
    impact: float = 0.0               # 0..1 a hit in this sample (collision, landing)
    airborne: bool = False
    active: bool = True               # False: paused, in a menu, in a replay - motion and effects rest
    effects: dict[str, float] = field(default_factory=dict)   # levels a source states itself (rain, fog, ...)
    params: dict[str, dict[str, float]] = field(default_factory=dict)   # e.g. {"scent": {"slot": 2}}
    time: float | None = None         # the source's own clock (s), where it has one
    captured: float | None = None     # monotonic time the underlying picture / sound was captured (delay display)

    def finite(self) -> bool:
        return all(math.isfinite(v) for v in (*self.accel, *self.rates, self.speed, self.rpm, self.throttle,
                                              self.brake, self.rumble, self.slip, self.impact))

    def clamped(self) -> LiveSample:
        """The same sample with every value inside its plausible range (glitches cut, levels 0..1)."""

        def lim(v: float, m: float) -> float:
            return max(-m, min(m, v))

        def unit(v: float) -> float:
            return max(0.0, min(1.0, v))

        self.accel = (lim(self.accel[0], ACCEL_LIMIT), lim(self.accel[1], ACCEL_LIMIT), lim(self.accel[2], ACCEL_LIMIT))
        self.rates = (lim(self.rates[0], RATE_LIMIT), lim(self.rates[1], RATE_LIMIT), lim(self.rates[2], RATE_LIMIT))
        self.speed = max(0.0, min(self.speed, 2000.0))
        self.rpm = max(0.0, min(self.rpm, 100000.0))
        self.throttle, self.brake = unit(self.throttle), unit(self.brake)
        self.rumble, self.slip, self.impact = unit(self.rumble), unit(self.slip), unit(self.impact)
        self.effects = {k: unit(float(v)) for k, v in self.effects.items() if math.isfinite(float(v))}
        return self


def describe(sample: LiveSample, threshold: float = 0.6) -> list[str]:
    """What the sample says, in plain words - for checking the directions of a new source against
    what is happening in the game ("braking", "turning left", ...)."""
    words: list[str] = []
    a, r = sample.accel, sample.rates
    if a[0] > threshold:
        words.append("speeding up")
    elif a[0] < -threshold:
        words.append("braking")
    if a[1] > threshold:
        words.append("pushed left (left turn)")
    elif a[1] < -threshold:
        words.append("pushed right (right turn)")
    if a[2] > 1.5 * threshold:
        words.append("pressed into the seat")
    elif a[2] < -1.5 * threshold:
        words.append("lifted")
    rt = 0.08
    if r[2] > rt:
        words.append("nose turning left")
    elif r[2] < -rt:
        words.append("nose turning right")
    if r[1] > rt:
        words.append("nose going down")
    elif r[1] < -rt:
        words.append("nose going up")
    if r[0] > rt:
        words.append("rolling right")
    elif r[0] < -rt:
        words.append("rolling left")
    return words
