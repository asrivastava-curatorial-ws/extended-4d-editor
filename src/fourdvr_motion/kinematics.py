"""Kinematics of a motion platform moved by linear actuators (rigid-body transformations).

A Stewart platform has six actuators and moves on all six axes. A motion seat may have two to six
actuators and fewer axes: three under the seat around a central guided pivot (heave, roll, pitch),
two beside a fixed pivot (roll, pitch). The same formula gives every actuator's length; the axes a
platform does not have (``dof_mask``) stay at zero. See STEWART_KINEMATICS.md.

Conventions
-----------
* Base frame: X forward, Y left, Z up (metres). Joint coordinates are measured in this frame
  (base joints) or in the platform frame (platform joints, relative to the platform origin).
* A *pose* is ``[surge, sway, heave, roll, pitch, yaw]`` = offset from the neutral pose.
  Rotation matrix ``R(roll, pitch, yaw) = Rz(yaw) @ Ry(pitch) @ Rx(roll)``.
* Rotations are about ``rotation_centre`` (platform frame), typically the passenger's head,
  so a pure rotation keeps that point fixed.

Inverse kinematics for leg i::

    R      = R0 @ R(roll, pitch, yaw)                 (R0 = neutral orientation)
    origin = n + R0 @ c + R0 @ t - R @ c              (n = neutral position, c = rotation centre, t = translation)
    leg_i  = origin + R @ p_i - b_i ;   L_i = |leg_i|
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

AXES = ("surge", "sway", "heave", "roll", "pitch", "yaw")


def rotation_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def rotation_matrices(rpy: np.ndarray) -> np.ndarray:
    """Vectorised rotation matrices for an (N, 3) array of roll, pitch, yaw -> (N, 3, 3)."""
    rpy = np.asarray(rpy, dtype=np.float64)
    cr, sr = np.cos(rpy[:, 0]), np.sin(rpy[:, 0])
    cp, sp = np.cos(rpy[:, 1]), np.sin(rpy[:, 1])
    cy, sy = np.cos(rpy[:, 2]), np.sin(rpy[:, 2])
    out = np.empty((rpy.shape[0], 3, 3))
    out[:, 0, 0] = cy * cp
    out[:, 0, 1] = cy * sp * sr - sy * cr
    out[:, 0, 2] = cy * sp * cr + sy * sr
    out[:, 1, 0] = sy * cp
    out[:, 1, 1] = sy * sp * sr + cy * cr
    out[:, 1, 2] = sy * sp * cr - cy * sr
    out[:, 2, 0] = -sp
    out[:, 2, 1] = cp * sr
    out[:, 2, 2] = cp * cr
    return out


def matrix_to_rpy(r: np.ndarray) -> tuple[float, float, float]:
    """Inverse of rotation_matrix (valid for |pitch| < 90 degrees)."""
    pitch = -math.asin(max(-1.0, min(1.0, float(r[2, 0]))))
    roll = math.atan2(float(r[2, 1]), float(r[2, 2]))
    yaw = math.atan2(float(r[1, 0]), float(r[0, 0]))
    return roll, pitch, yaw


MIN_LEGS, MAX_LEGS = 2, 6


@dataclass(frozen=True)
class StewartGeometry:
    base_joints: np.ndarray  # (n, 3) base frame, n = 2..6 actuators
    platform_joints: np.ndarray  # (n, 3) platform frame
    neutral_position: np.ndarray  # (3,) platform origin in base frame at neutral
    neutral_orientation: np.ndarray = field(default_factory=lambda: np.zeros(3))  # roll, pitch, yaw
    rotation_centre: np.ndarray = field(default_factory=lambda: np.zeros(3))  # platform frame (the pivot of a seat)
    dof_mask: np.ndarray = field(default_factory=lambda: np.ones(6, dtype=bool))  # the axes the platform can move

    def __post_init__(self) -> None:
        base = np.asarray(self.base_joints, dtype=np.float64)
        n = base.shape[0] if base.ndim == 2 else -1
        if not MIN_LEGS <= n <= MAX_LEGS:
            raise ValueError(f"base_joints must list {MIN_LEGS} to {MAX_LEGS} actuators, got shape {base.shape}")
        for name, shape in (
            ("base_joints", (n, 3)),
            ("platform_joints", (n, 3)),
            ("neutral_position", (3,)),
            ("neutral_orientation", (3,)),
            ("rotation_centre", (3,)),
        ):
            arr = np.asarray(getattr(self, name), dtype=np.float64)
            if arr.shape != shape:
                raise ValueError(f"{name} must have shape {shape}, got {arr.shape}")
            if not np.all(np.isfinite(arr)):
                raise ValueError(f"{name} contains non-finite values")
            object.__setattr__(self, name, arr)
        mask = np.asarray(self.dof_mask, dtype=bool)
        if mask.shape != (6,) or not mask.any():
            raise ValueError("dof_mask must name at least one of the six axes")
        if int(mask.sum()) > n:
            raise ValueError(f"{n} actuators cannot move {int(mask.sum())} axes")
        object.__setattr__(self, "dof_mask", mask)
        object.__setattr__(self, "_r0", rotation_matrix(*self.neutral_orientation))

    @property
    def r0(self) -> np.ndarray:
        return self._r0  # type: ignore[attr-defined,no-any-return]

    @property
    def legs(self) -> int:
        return int(self.base_joints.shape[0])

    @property
    def dof(self) -> tuple[str, ...]:
        return tuple(a for a, on in zip(AXES, self.dof_mask, strict=True) if on)

    def transform(self, pose: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Platform origin (base frame) and rotation matrix for a pose."""
        pose = np.asarray(pose, dtype=np.float64)
        r = self.r0 @ rotation_matrix(pose[3], pose[4], pose[5])
        c = self.rotation_centre
        origin = self.neutral_position + self.r0 @ c + self.r0 @ pose[:3] - r @ c
        return origin, r

    def joint_positions(self, pose: np.ndarray) -> np.ndarray:
        origin, r = self.transform(pose)
        return origin + self.platform_joints @ r.T

    def leg_vectors(self, pose: np.ndarray) -> np.ndarray:
        return self.joint_positions(pose) - self.base_joints

    def leg_lengths(self, pose: np.ndarray) -> np.ndarray:
        """Inverse kinematics: the actuator (pin-to-pin) lengths for a pose."""
        return np.linalg.norm(self.leg_vectors(pose), axis=1)

    def leg_lengths_many(self, poses: np.ndarray) -> np.ndarray:
        """Vectorised inverse kinematics for an (N, 6) pose array -> (N, n) lengths."""
        poses = np.atleast_2d(np.asarray(poses, dtype=np.float64))
        rm = np.einsum("ij,njk->nik", self.r0, rotation_matrices(poses[:, 3:]))
        c = self.rotation_centre
        origin = self.neutral_position + self.r0 @ c + poses[:, :3] @ self.r0.T - np.einsum("nij,j->ni", rm, c)
        joints = origin[:, None, :] + np.einsum("nij,kj->nki", rm, self.platform_joints)
        return np.linalg.norm(joints - self.base_joints[None, :, :], axis=2)

    def neutral_lengths(self) -> np.ndarray:
        return self.leg_lengths(np.zeros(6))

    def jacobian(self, pose: np.ndarray, h: float = 1e-6) -> np.ndarray:
        """d(lengths)/d(pose), central differences (n x 6)."""
        pose = np.asarray(pose, dtype=np.float64)
        jac = np.empty((self.legs, 6))
        for k in range(6):
            dp = np.zeros(6)
            dp[k] = h
            jac[:, k] = (self.leg_lengths(pose + dp) - self.leg_lengths(pose - dp)) / (2 * h)
        return jac

    def forward(
        self, lengths: np.ndarray, guess: np.ndarray | None = None, tol: float = 1e-10, max_iter: int = 60
    ) -> np.ndarray:
        """Forward kinematics: the pose (on the platform's own axes) whose actuator lengths are ``lengths``.

        Gauss-Newton on the axes of ``dof_mask``; the others stay zero. With as many actuators as axes
        the lengths are met exactly; with more (a seat on four cylinders) the pose that fits best is
        returned if no actuator is more than a millimetre off.
        """
        target = np.asarray(lengths, dtype=np.float64)
        if target.shape != (self.legs,):
            raise ValueError(f"{self.legs} actuator lengths are needed, got {target.shape}")
        free = np.flatnonzero(self.dof_mask)
        x = np.zeros(6) if guess is None else np.asarray(guess, dtype=np.float64).copy()
        x[~self.dof_mask] = 0.0
        for _ in range(max_iter):
            residual = self.leg_lengths(x) - target
            worst = float(np.max(np.abs(residual)))
            if worst < tol:
                return x
            step, *_ = np.linalg.lstsq(self.jacobian(x)[:, free], residual, rcond=None)
            x[free] -= step
            if not np.all(np.isfinite(x)):
                break
            if float(np.max(np.abs(step))) < 1e-13:          # least-squares optimum (more actuators than axes)
                if float(np.max(np.abs(self.leg_lengths(x) - target))) < 1e-3:
                    return x
                break
        raise ValueError("forward kinematics did not converge (lengths outside the reachable workspace?)")

    def dof_problem(self) -> str:
        """Why the actuators cannot move the platform on all of its axes at neutral, or ""."""
        free = np.flatnonzero(self.dof_mask)
        jac = self.jacobian(np.zeros(6))[:, free]
        _u, s, vt = np.linalg.svd(jac, full_matrices=False)
        if s.size and s[-1] > 1e-4 and s[-1] / s[0] > 1e-3:
            return ""
        weakest = vt[-1]
        axis = AXES[int(free[int(np.argmax(np.abs(weakest)))])]
        return (f"the actuators cannot move the platform on '{axis}' (the joints are placed so that this axis changes no "
                "actuator's length): check base_joints, platform_joints and rotation_centre, or leave the axis out of dof")


def seat_geometry(
    legs: int,
    radius: float,
    neutral_leg_length: float,
    pivot_height: float = 0.0,
    base_z: float = 0.0,
) -> StewartGeometry:
    """A motion seat on 2 or 3 upright actuators around a central pivot (the usual seat layouts).

    3 actuators: one at the front, two at the rear corners (0, 120, 240 deg); the pivot can rise and
    fall: heave, roll, pitch. 2 actuators: at the rear corners (150 and 210 deg) behind a fixed pivot:
    roll and pitch. ``pivot_height`` is the pivot's height above the platform's joint plane.
    """
    angles: tuple[float, ...]
    dof: tuple[str, ...]
    if legs == 3:
        angles, dof = (0.0, 120.0, 240.0), ("heave", "roll", "pitch")
    elif legs == 2:
        angles, dof = (150.0, 210.0), ("roll", "pitch")
    else:
        raise ValueError("a seat layout has 2 or 3 actuators (use symmetric_hexapod for six)")
    pts = np.array([(radius * math.cos(math.radians(a)), radius * math.sin(math.radians(a)), 0.0) for a in angles])
    base = pts + np.array([0.0, 0.0, base_z])
    return StewartGeometry(
        base_joints=base,
        platform_joints=pts,
        neutral_position=np.array([0.0, 0.0, base_z + neutral_leg_length]),
        rotation_centre=np.array([0.0, 0.0, pivot_height]),
        dof_mask=np.array([a in dof for a in AXES]),
    )


def symmetric_hexapod(
    base_radius: float,
    platform_radius: float,
    base_pair_angle_deg: float,
    platform_pair_angle_deg: float,
    neutral_leg_length: float,
    base_z: float = 0.0,
    rotation_centre: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> StewartGeometry:
    """Classic 6-6 hexapod: three base joint pairs at 0/120/240 deg, platform pairs at 60/180/300 deg.

    Each leg connects a base pair to the adjacent platform pair (the familiar zig-zag truss).
    The neutral height is chosen so every leg has ``neutral_leg_length`` at the neutral pose.
    """
    hb = math.radians(base_pair_angle_deg) / 2
    hp = math.radians(platform_pair_angle_deg) / 2
    base, plat = [], []
    for k in range(3):
        c = math.radians(120 * k)
        pairs = ((c + hb, c + math.radians(60) - hp), (c + math.radians(120) - hb, c + math.radians(60) + hp))
        for ab, ap in pairs:
            base.append((base_radius * math.cos(ab), base_radius * math.sin(ab), base_z))
            plat.append((platform_radius * math.cos(ap), platform_radius * math.sin(ap), 0.0))
    b = np.array(base)
    p = np.array(plat)
    d_xy = float(np.linalg.norm(p[0, :2] - b[0, :2]))
    if neutral_leg_length <= d_xy:
        raise ValueError("neutral leg length too short for the chosen radii")
    height = base_z + math.sqrt(neutral_leg_length**2 - d_xy**2)
    return StewartGeometry(
        base_joints=b,
        platform_joints=p,
        neutral_position=np.array([0.0, 0.0, height]),
        rotation_centre=np.array(rotation_centre, dtype=np.float64),
    )
