"""Live sound for bass shakers and subwoofers.

A programme carries its bass as a recorded track; a live source has none, so the sound is made here
and handed to the unchanged bass outputs (the sound card of the show PC and the ESP32 audio nodes)
as a *feed*: an object with ``read(key, frames, rate, now)`` that returns the next mono block for
one output. Every output has its own position in the feed (``key``), so several outputs with
different sample rates can play it at the same time.

* ``SynthFeed``   - made from telemetry: an engine tone that follows the revs, road rumble, a thump
  at every hit;
* ``CaptureFeed`` - the low end of captured sound (the game's own audio), a few hundredths of a
  second behind the capture - or as far behind as the picture is delayed;
* ``MixFeed``     - several of them together.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .sample import LiveSample

RUMBLE_RATE = 1000.0       # Hz: the road rumble is made at this rate (it has next to nothing above 100 Hz)


@dataclass
class ShakerSettings:
    engine: bool = True
    engine_level: float = 0.5           # 0..1 at full revs and full throttle
    engine_low_hz: float = 28.0         # tone at idle
    engine_high_hz: float = 85.0        # tone at the top of the rev range
    road: bool = True
    road_level: float = 0.6             # 0..1 on the roughest surface
    impacts: bool = True
    impact_level: float = 0.9
    impact_hz: float = 42.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShakerSettings:
        s = cls()
        for key, value in data.items():
            if not hasattr(s, key):
                raise ValueError(f"unknown shaker setting {key!r}")
            setattr(s, key, bool(value) if isinstance(getattr(s, key), bool) else float(value))
        if not 10.0 <= s.engine_low_hz < s.engine_high_hz <= 200.0:
            raise ValueError("engine tone must rise from engine_low_hz to engine_high_hz within 10-200 Hz")
        return s


class _Voice:
    """The state of one output that plays a SynthFeed."""

    def __init__(self) -> None:
        self.phase = 0.0
        self.freq = 0.0
        self.engine = 0.0
        self.road = 0.0
        self.lp = [0.0, 0.0, 0.0, 0.0]   # four low-pass stages of the rumble
        self.hp = 0.0
        self.tail = [0.0, 0.0]          # the two newest samples of the low-rate rumble
        self.frac = 0.0                 # position between them
        self.hits: list[tuple[float, float]] = []      # (strength, seconds since the hit) still ringing
        self.seen = 0
        self.rng = np.random.default_rng(0x4D)


class SynthFeed:
    def __init__(self, settings: ShakerSettings | None = None) -> None:
        self.s = settings or ShakerSettings()
        self._lock = threading.Lock()
        self._freq = self.s.engine_low_hz
        self._engine = 0.0
        self._road = 0.0
        self._hits: list[float] = []
        self._count = 0
        self._voices: dict[int, _Voice] = {}

    def update(self, sample: LiveSample | None, envelope: float) -> None:
        """The newest state of the source (called once per control period)."""
        s = self.s
        freq, engine, road, hit = self._freq, 0.0, 0.0, 0.0
        if sample is not None and envelope > 0.0:
            if s.engine and sample.rpm > 0.0:
                top = sample.rpm_max if sample.rpm_max > 0.0 else 8000.0
                idle = sample.rpm_idle if 0.0 < sample.rpm_idle < top else 0.1 * top
                revs = min(1.0, max(0.0, (sample.rpm - idle) / max(top - idle, 1.0)))
                freq = s.engine_low_hz + (s.engine_high_hz - s.engine_low_hz) * revs
                engine = s.engine_level * (0.25 + 0.75 * sample.throttle) * (0.5 + 0.5 * revs) * envelope
            if s.road:
                road = s.road_level * max(sample.rumble, 0.5 * sample.slip) * envelope
            if s.impacts and sample.impact > 0.05:
                hit = s.impact_level * sample.impact * envelope
        with self._lock:
            self._freq, self._engine, self._road = freq, engine, road
            if hit > 0.0:
                self._hits.append(hit)
                self._count += 1
                del self._hits[:-8]

    def read(self, key: int, frames: int, rate: int, now: float) -> np.ndarray:
        with self._lock:
            freq, engine, road = self._freq, self._engine, self._road
            v = self._voices.get(key)
            if v is None:
                v = self._voices[key] = _Voice()
                v.freq, v.seen = freq, self._count
            new = self._hits[len(self._hits) - min(self._count - v.seen, len(self._hits)):] if self._count > v.seen else []
            v.seen = self._count
        dt = 1.0 / rate
        n = np.arange(1, frames + 1)
        out = np.zeros(frames, dtype=np.float64)
        # engine: fundamental and a quieter octave, level and pitch gliding across the block
        if engine > 1e-4 or v.engine > 1e-4:
            f = v.freq + (freq - v.freq) * n / frames
            level = v.engine + (engine - v.engine) * n / frames
            phase = v.phase + 2.0 * math.pi * np.cumsum(f) * dt
            out += level * (0.8 * np.sin(phase) + 0.2 * np.sin(2.0 * phase))
            v.phase = float(phase[-1] % (2.0 * math.pi))
        v.freq, v.engine = freq, engine
        # road: noise between about 15 and 70 Hz, made at a low rate and interpolated up
        if road > 1e-4 or v.road > 1e-4:
            level = v.road + (road - v.road) * n / frames
            out += level * self._rumble(v, frames, rate)
        v.road = road
        # hits: a short decaying thump each
        v.hits += [(h, 0.0) for h in new]
        if v.hits:
            t = n * dt
            keep: list[tuple[float, float]] = []
            for strength, age in v.hits:
                tt = age + t
                out += strength * np.exp(-tt / 0.12) * np.sin(2.0 * math.pi * self.s.impact_hz * tt)
                if age + frames * dt < 0.7:
                    keep.append((strength, age + frames * dt))
            v.hits = keep
        return out.astype(np.float32)


    @staticmethod
    def _rumble(v: _Voice, frames: int, rate: int) -> np.ndarray:
        ratio = RUMBLE_RATE / float(rate)
        pos = v.frac + ratio * np.arange(1, frames + 1)
        last = int(pos[-1])
        a_lp = 1.0 - math.exp(-2.0 * math.pi * 90.0 / RUMBLE_RATE)
        a_hp = 1.0 - math.exp(-2.0 * math.pi * 15.0 / RUMBLE_RATE)
        low = list(v.tail)
        l1, l2, l3, l4 = v.lp
        hp = v.hp
        for x in v.rng.standard_normal(max(0, last)):
            l1 += a_lp * (float(x) - l1)
            l2 += a_lp * (l1 - l2)
            l3 += a_lp * (l2 - l3)
            l4 += a_lp * (l3 - l4)
            hp += a_hp * (l4 - hp)
            low.append((l4 - hp) * 4.0)           # about full scale at level 1
        v.lp, v.hp = [l1, l2, l3, l4], hp
        arr = np.asarray(low)
        i0 = np.minimum(pos.astype(np.int64), len(arr) - 2)
        frac = pos - i0
        v.tail, v.frac = [float(arr[last]), float(arr[last + 1])], float(pos[-1] - last)
        return arr[i0] + (arr[i0 + 1] - arr[i0]) * frac


class CaptureFeed:
    """A ring of captured, low-passed sound. The writer appends blocks as they are captured; every
    reader follows `delay_s` behind the newest sample and is put back there if it falls out of step."""

    def __init__(self, rate: int = 4000, seconds: float = 6.0, delay_s: float = 0.06) -> None:
        self.rate = int(rate)
        self.delay_s = delay_s
        self._ring = np.zeros(int(seconds * rate), dtype=np.float32)
        self._written = 0                       # total samples written
        self._lock = threading.Lock()
        self._readers: dict[int, float] = {}    # key -> absolute position (feed samples, fractional)
        self.level = 0.0                        # recent peak, for meters
        self.resyncs = 0

    def write(self, block: np.ndarray) -> None:
        block = np.asarray(block, dtype=np.float32).ravel()
        n = len(self._ring)
        if len(block) >= n:
            block = block[-n:]
        with self._lock:
            pos = self._written % n
            first = min(len(block), n - pos)
            self._ring[pos : pos + first] = block[:first]
            self._ring[: len(block) - first] = block[first:]
            self._written += len(block)
        if len(block):
            self.level = max(0.9 * self.level, float(np.max(np.abs(block))))

    def read(self, key: int, frames: int, rate: int, now: float) -> np.ndarray:
        step = self.rate / float(rate)
        need = frames * step
        with self._lock:
            written, n = self._written, len(self._ring)
            target = written - self.delay_s * self.rate - need
            pos = self._readers.get(key)
            slack = max(0.05 * self.rate, 2.0 * need)
            if pos is None or abs(pos - target) > slack or pos < written - n + 2:
                if pos is not None:
                    self.resyncs += 1
                pos = target
            if pos < max(0.0, written - n + 2.0) or pos + need + 1 > written:
                self._readers[key] = pos if pos + need + 1 <= written else target
                return np.zeros(frames, dtype=np.float32)
            idx = pos + step * np.arange(frames)
            i0 = np.floor(idx).astype(np.int64)
            frac = (idx - i0).astype(np.float32)
            a = self._ring[i0 % n]
            b = self._ring[(i0 + 1) % n]
            self._readers[key] = pos + need
        return (a + (b - a) * frac).astype(np.float32)


class MixFeed:
    def __init__(self, *feeds: Any) -> None:
        self.feeds = [f for f in feeds if f is not None]

    def read(self, key: int, frames: int, rate: int, now: float) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        for f in self.feeds:
            out += f.read(key, frames, rate, now)
        return out
