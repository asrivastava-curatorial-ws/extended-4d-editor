"""Hard software limits, enforced before any actuator command is generated.

1. ``PoseLimiter``      - per-axis position, velocity, acceleration and jerk limits (all 6 axes).
                         Uses square-root (time-optimal braking) shaping so a step in the target
                         never causes overshoot or oscillation; smooth targets are tracked exactly.
2. ``ActuatorGuard``    - inverse kinematics, actuator stroke limits (workspace), actuator velocity
                         and acceleration limits. Infeasible poses are scaled toward neutral; too-fast
                         actuator motion is reduced by shortening the pose step, which keeps the six
                         lengths consistent with a rigid-body pose.

These limits are numerical safeguards only. They do not make a passenger-carrying platform safe;
see the safety statement in README.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .kinematics import StewartGeometry
from .platform_config import ActuatorLimits, AxisLimits


def sqrt_shaper(error: np.ndarray, gain: float, second_order_limit: np.ndarray, dt: float) -> np.ndarray:
    """Velocity (or acceleration) demand that can be cancelled within ``second_order_limit``.

    Linear (gain * error) close to the target and sqrt(2 * limit * |error|) further away, so the
    demand always allows braking to rest exactly at the target. Clamped so one step cannot jump
    over the target.
    """
    lim = np.maximum(second_order_limit, 1e-12)
    linear = lim / (gain * gain)
    out = gain * error
    far = np.abs(error) > linear
    out = np.where(far, np.sign(error) * np.sqrt(2.0 * lim * np.maximum(np.abs(error) - linear / 2.0, 0.0)), out)
    step_cap = np.abs(error) / dt
    return np.clip(out, -step_cap, step_cap)


@dataclass
class LimiterCounters:
    position_clamps: int = 0
    velocity_clamps: int = 0
    acceleration_clamps: int = 0
    jerk_clamps: int = 0
    workspace_scalings: int = 0
    actuator_rate_scalings: int = 0
    actuator_accel_excess: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class PoseLimiter:
    """Third-order (jerk-limited) tracking of a 6-DoF reference with hard limits.

    ``step(r_now, r_next)`` receives the reference at the current and the next control instant.
    When the reference is kinematically feasible, its jerk is fed forward and the output equals the
    reference exactly (no lag). Errors (after start-up, steps, infeasible input) are removed by a
    cascaded square-root controller that brakes in time, so it neither overshoots nor oscillates.
    Velocity, acceleration and jerk are clamped every step; position is guarded by a braking-distance
    check and, as a last resort, a hard clamp.
    """

    limits: AxisLimits
    dt: float
    position_gain: float = 4.0
    velocity_gain: float = 15.0
    counters: LimiterCounters = field(default_factory=LimiterCounters)

    def __post_init__(self) -> None:
        self.x = np.zeros(6)
        self.v = np.zeros(6)
        self.a = np.zeros(6)
        self._r = np.zeros(6)
        self._vr = np.zeros(6)
        self._ar = np.zeros(6)
        self._prev_state = (self.x.copy(), self.v.copy())

    def reset(self, pose: np.ndarray) -> None:
        self.x = np.asarray(pose, dtype=np.float64).copy()
        self.v[:] = 0.0
        self.a[:] = 0.0
        self._r = self.x.copy()
        self._vr[:] = 0.0
        self._ar[:] = 0.0
        self._prev_state = (self.x.copy(), self.v.copy())

    def sync(self, pose: np.ndarray) -> None:
        """Adopt the pose actually commanded this step (after actuator guarding) as the new state."""
        pose = np.asarray(pose, dtype=np.float64)
        x_prev, v_prev = self._prev_state
        v_new = (pose - x_prev) / self.dt
        self.a = (v_new - v_prev) / self.dt
        self.v = v_new
        self.x = pose.copy()

    def step(self, r_now: np.ndarray, r_next: np.ndarray | None = None, scale: float = 1.0) -> np.ndarray:
        lim = self.limits
        dt = self.dt
        self._prev_state = (self.x.copy(), self.v.copy())
        vmax = lim.velocity * scale
        amax = lim.acceleration * scale
        jmax = lim.jerk * scale
        r_now = np.asarray(r_now, dtype=np.float64)
        r_next = r_now if r_next is None else np.asarray(r_next, dtype=np.float64)
        rc_now = np.clip(r_now, lim.position_min, lim.position_max)
        rc_next = np.clip(r_next, lim.position_min, lim.position_max)
        if np.any(rc_next != r_next):
            self.counters.position_clamps += 1

        # reference derivatives (backward differences on the control grid)
        vr_now = (rc_now - self._r) / dt
        vr_next = (rc_next - rc_now) / dt
        ar_now = (vr_now - self._vr) / dt
        ar_next = (vr_next - vr_now) / dt
        jr_next = (ar_next - ar_now) / dt
        # Tracking mode: the reference moves at a feasible speed and roughly feasible acceleration;
        # its (clamped) jerk is fed forward. Otherwise (steps, jumps) the axis regulates toward the
        # next reference point as a fixed target, which the square-root cascade reaches without overshoot.
        tracking = (
            (np.abs(vr_next) <= vmax)
            & (np.abs(vr_now) <= vmax)
            & (np.abs(ar_next) <= 3.0 * amax)
            & (np.abs(ar_now) <= 3.0 * amax)
        )
        vr_e = np.where(tracking, vr_now, 0.0)
        ar_e = np.where(tracking, np.clip(ar_now, -amax, amax), 0.0)
        r_e = np.where(tracking, rc_now, rc_next)
        j_ff = np.where(tracking, np.clip(jr_next, -jmax, jmax), 0.0)
        self._r, self._vr, self._ar = rc_now, vr_now, ar_now

        ex = self.x - r_e
        ev = self.v - vr_e
        ev_des = sqrt_shaper(-ex, self.position_gain, 0.7 * amax, dt)
        ea_des = sqrt_shaper(ev_des - ev, self.velocity_gain, 0.7 * jmax, dt)
        a_target = ar_e + j_ff * dt + ea_des
        # anticipatory guards: accelerate only as much as can be undone (jerk-limited) before
        # reaching the velocity limit, or the braking speed allowed near a position bound
        room_up = np.maximum(lim.position_max - self.x, 0.0)
        room_dn = np.maximum(self.x - lim.position_min, 0.0)
        v_hi = np.minimum(vmax, np.sqrt(1.4 * amax * room_up))
        v_lo = -np.minimum(vmax, np.sqrt(1.4 * amax * room_dn))
        a_hi = sqrt_shaper(v_hi - self.v, self.velocity_gain, 0.7 * jmax, dt)
        a_lo = sqrt_shaper(v_lo - self.v, self.velocity_gain, 0.7 * jmax, dt)
        guarded = np.clip(a_target, np.minimum(a_lo, a_hi), np.maximum(a_lo, a_hi))
        if np.any(np.abs(guarded - a_target) > 1e-9):
            self.counters.velocity_clamps += 1
        if np.any(np.abs(guarded) > amax + 1e-9):
            self.counters.acceleration_clamps += 1
        a_target = np.clip(guarded, -amax, amax)
        j = (a_target - self.a) / dt
        if np.any(np.abs(j) > jmax + 1e-9):
            self.counters.jerk_clamps += 1
        a_new = np.clip(self.a + np.clip(j, -jmax, jmax) * dt, -amax, amax)
        v_new = self.v + a_new * dt
        if np.any(v_new > v_hi + 1e-9) or np.any(v_new < v_lo - 1e-9):  # last resort (should not happen)
            self.counters.velocity_clamps += 1
            v_new = np.clip(v_new, v_lo, v_hi)
            a_new = (v_new - self.v) / dt
        x_new = self.x + v_new * dt
        over = (x_new > lim.position_max) | (x_new < lim.position_min)
        if np.any(over):  # last-resort hard stop at the bound
            self.counters.position_clamps += 1
            x_new = np.clip(x_new, lim.position_min, lim.position_max)
            v_new = np.where(over, 0.0, v_new)
            a_new = np.where(over, 0.0, a_new)
        self.x, self.v, self.a = x_new, v_new, a_new
        return self.x.copy()


@dataclass
class ActuatorGuard:
    geometry: StewartGeometry
    actuators: ActuatorLimits
    dt: float
    counters: LimiterCounters = field(default_factory=LimiterCounters)

    def __post_init__(self) -> None:
        self.lengths = self.geometry.neutral_lengths()
        self.velocity = np.zeros(6)
        self.pose = np.zeros(6)

    def reset(self, pose: np.ndarray) -> None:
        self.pose = np.asarray(pose, dtype=np.float64).copy()
        self.lengths = self.geometry.leg_lengths(self.pose)
        self.velocity[:] = 0.0

    def feasible(self, lengths: np.ndarray) -> bool:
        return bool(np.all(lengths >= self.actuators.usable_min()) and np.all(lengths <= self.actuators.usable_max()))

    def workspace_project(self, pose: np.ndarray, reference: np.ndarray) -> tuple[np.ndarray, bool]:
        """Scale ``pose`` toward ``reference`` (a known-feasible pose) until inside the stroke."""
        lengths = self.geometry.leg_lengths(pose)
        if self.feasible(lengths):
            return pose, False
        lo, hi = 0.0, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            if self.feasible(self.geometry.leg_lengths(reference + mid * (pose - reference))):
                lo = mid
            else:
                hi = mid
        self.counters.workspace_scalings += 1
        return reference + lo * (pose - reference), True

    def apply(
        self,
        pose: np.ndarray,
        neutral_ref: np.ndarray,
        pose_min: np.ndarray | None = None,
        pose_max: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (pose, lengths) inside the stroke and within actuator velocity/acceleration limits.

        The pose step requested by the pose limiter is scaled by a factor alpha (pose = previous +
        alpha * step), which keeps the six lengths consistent with one rigid-body pose. alpha < 1
        slows actuators that would be too fast or accelerate too hard; alpha > 1 (at most 1.5, never
        beyond the pose and stroke limits) lets an actuator brake gently instead of stopping abruptly.
        """
        pose, _ = self.workspace_project(np.asarray(pose, dtype=np.float64), neutral_ref)
        lengths = self.geometry.leg_lengths(pose)
        vel = (lengths - self.lengths) / self.dt
        vmax = self.actuators.max_velocity
        dv_max = self.actuators.max_acceleration * self.dt
        if np.any(np.abs(vel) > vmax + 1e-12) or np.any(np.abs(vel - self.velocity) > dv_max + 1e-12):
            lo, hi = -np.inf, np.inf
            for i in range(6):
                if abs(vel[i]) < 1e-12:
                    if abs(self.velocity[i]) > dv_max:  # needs to move on to brake: no linear solution
                        lo, hi = 1.0, 1.0
                    continue
                b = sorted(((self.velocity[i] - dv_max) / vel[i], (self.velocity[i] + dv_max) / vel[i]))
                lo = max(lo, b[0])
                hi = min(hi, b[1])
                vb = vmax / abs(vel[i])
                lo = max(lo, -vb)
                hi = min(hi, vb)
            if lo <= hi:
                alpha = float(np.clip(1.0, lo, hi))
            else:
                alpha = float(np.clip(1.0, -np.inf, hi)) if hi < 1.0 else 1.0
            alpha = max(0.0, min(alpha, 1.5))
            candidate = self.pose + alpha * (pose - self.pose)
            if alpha > 1.0:
                inside_pose = (
                    pose_min is None
                    or pose_max is None
                    or (np.all(candidate >= pose_min) and np.all(candidate <= pose_max))
                )
                if not inside_pose or not self.feasible(self.geometry.leg_lengths(candidate)):
                    alpha = 1.0
                    candidate = pose
            pose = candidate
            lengths = self.geometry.leg_lengths(pose)
            if not self.feasible(lengths):
                pose, lengths = self.pose.copy(), self.lengths.copy()
            vel = (lengths - self.lengths) / self.dt
            self.counters.actuator_rate_scalings += 1
        if np.any(np.abs(vel - self.velocity) > dv_max * (1 + 1e-6)):
            self.counters.actuator_accel_excess += 1  # measured, reported in STATUS
        self.velocity = vel
        self.lengths = lengths
        self.pose = pose
        return pose, lengths


def implied_actuator_demand(
    geometry: StewartGeometry, actuators: ActuatorLimits, limits: AxisLimits, samples: int = 400
) -> tuple[float, float]:
    """Worst-case actuator speed and acceleration if every pose axis ran at its own limit at once.

    Evaluated with the kinematic Jacobian at the neutral pose and at random poses inside the pose
    limits (where reachable). Values above the actuator limits mean the pose limits are optimistic
    for combined motion; the runtime guard then scales motion down (see STEWART_KINEMATICS.md).
    """
    rng = np.random.default_rng(42)
    poses = [np.zeros(6)] + [rng.uniform(limits.position_min, limits.position_max) for _ in range(samples)]
    worst_v = worst_a = 0.0
    for pose in poses:
        lengths = geometry.leg_lengths(pose)
        if np.any(lengths < actuators.usable_min()) or np.any(lengths > actuators.usable_max()):
            continue
        jac = np.abs(geometry.jacobian(pose))
        worst_v = max(worst_v, float((jac @ limits.velocity).max()))
        worst_a = max(worst_a, float((jac @ limits.acceleration).max()))
    return worst_v, worst_a


def blend_time(delta: np.ndarray, limits: AxisLimits, scale: float, minimum: float) -> float:
    """Duration for a smootherstep transition covering ``delta`` without exceeding v/a/j limits."""
    d = np.abs(np.asarray(delta, dtype=np.float64))
    v = limits.velocity * scale
    a = limits.acceleration * scale
    j = limits.jerk * scale
    t_v = 1.875 * d / v
    t_a = np.sqrt(5.7735 * d / a)
    t_j = np.cbrt(60.0 * d / j)
    needed = float(np.max(np.concatenate([t_v, t_a, t_j]))) if d.size else 0.0
    return max(minimum, needed * 1.1)


def smootherstep(u: float) -> float:
    """C2-continuous 0..1 ramp: 6u^5 - 15u^4 + 10u^3 (zero velocity and acceleration at both ends)."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    return u * u * u * (u * (u * 6.0 - 15.0) + 10.0)


def within(value: float, low: float, high: float) -> bool:
    return low <= value <= high and math.isfinite(value)
