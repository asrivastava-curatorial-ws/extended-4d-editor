"""Simulated actuators: what an electric, hydraulic or pneumatic actuator does with its set-point.

Not a physical model of a particular product - a plausible stand-in that lags, overshoots and
saturates the way each kind does, so that the show service's supervision (following error, link
loss, park) is exercised against something that behaves like the real thing:

* electric: first-order lag (20 ms) and a speed limit - a servo drive with its own position loop;
* hydraulic: second order (6 Hz, damping 0.7) and a speed limit - a servo valve with a position card;
* pneumatic: second order (2.5 Hz, damping 0.55: a little overshoot) and a speed limit - a
  servo-pneumatic positioner;
* plain valves: the cylinder runs to the end its valve says, in its travel time.

``jammed`` freezes an actuator where it is (a test of the supervision).
"""

from __future__ import annotations

import numpy as np

from .platform_config import PlatformConfig


class Plant:
    def __init__(self, kind: str, stroke: np.ndarray, start: np.ndarray, max_speed: float, tau: float = 0.02,
                 wn_hz: float = 6.0, zeta: float = 0.7, travel_s: float = 0.5) -> None:
        if kind not in ("first_order", "second_order", "valves"):
            raise ValueError(f"unknown plant {kind}")
        self.kind = kind
        self.stroke = np.asarray(stroke, dtype=np.float64)
        self.pos = np.asarray(start, dtype=np.float64).copy()
        self.vel = np.zeros_like(self.pos)
        self.max_speed = float(max_speed)
        self.tau = tau
        self.wn = 2.0 * np.pi * wn_hz
        self.zeta = zeta
        self.travel_s = travel_s
        self.jammed = np.zeros(len(self.pos), dtype=bool)

    def step(self, target: np.ndarray, dt: float) -> np.ndarray:
        """Advance by dt seconds towards the set-points (metres); returns the positions."""
        if dt <= 0.0:
            return self.pos
        target = np.clip(np.asarray(target, dtype=np.float64), 0.0, self.stroke)
        free = ~self.jammed
        steps = max(1, int(np.ceil(dt / 0.001)))
        h = dt / steps
        for _ in range(steps):
            if self.kind == "first_order":
                vel = np.clip((target - self.pos) / max(self.tau, h), -self.max_speed, self.max_speed)
            elif self.kind == "second_order":
                acc = self.wn * self.wn * (target - self.pos) - 2.0 * self.zeta * self.wn * self.vel
                vel = np.clip(self.vel + acc * h, -self.max_speed, self.max_speed)
            else:
                goal = np.where(target >= self.stroke / 2.0, self.stroke, 0.0)
                speed = self.stroke / self.travel_s
                vel = np.clip((goal - self.pos) / h, -speed, speed)
            self.vel = np.where(free, vel, 0.0)
            self.pos = np.clip(self.pos + self.vel * h, 0.0, self.stroke)
        return self.pos


def plant_for(cfg: PlatformConfig, start: np.ndarray) -> Plant:
    """The stand-in for the actuators a profile describes, starting at ``start`` (strokes, metres)."""
    stroke, vmax = cfg.actuators.stroke, cfg.actuators.max_velocity * 1.25
    if cfg.control == "onoff":
        return Plant("valves", stroke, start, vmax, travel_s=cfg.onoff.travel_s if cfg.onoff else 0.5)
    if cfg.actuator == "hydraulic":
        return Plant("second_order", stroke, start, vmax, wn_hz=6.0, zeta=0.7)
    if cfg.actuator == "pneumatic":
        return Plant("second_order", stroke, start, vmax, wn_hz=2.5, zeta=0.55)
    return Plant("first_order", stroke, start, vmax, tau=0.02)
