"""Stewart platform configuration (geometry, actuators, limits, calibration) loaded from TOML.

SHARED MODULE. Identical copies live in the Motion Controller (fourdvr_motion/platform_config.py)
and the Motion Extractor (fourdvr_extractor/platform_config.py) so that the extractor validates
motion against exactly the same platform profile the controller enforces.
See PLATFORM_CONFIGURATION.md and CALIBRATION.md.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .kinematics import AXES, StewartGeometry

DRIVER_TYPES = ("servo_electric", "simulation")
FUTURE_DRIVER_TYPES = ("hydraulic", "pneumatic")


class PlatformConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AxisLimits:
    position_min: np.ndarray  # (6,)
    position_max: np.ndarray  # (6,)
    velocity: np.ndarray  # (6,) symmetric magnitude
    acceleration: np.ndarray  # (6,)
    jerk: np.ndarray  # (6,)

    def clip_position(self, pose: np.ndarray) -> np.ndarray:
        return np.clip(pose, self.position_min, self.position_max)


@dataclass(frozen=True)
class ActuatorLimits:
    min_length: np.ndarray  # (6,) pin-to-pin length fully retracted (m)
    max_length: np.ndarray  # (6,) fully extended (m)
    max_velocity: float  # m/s
    max_acceleration: float  # m/s^2
    soft_margin: float = 0.005  # m kept clear of both stroke ends

    @property
    def stroke(self) -> np.ndarray:
        return self.max_length - self.min_length

    def usable_min(self) -> np.ndarray:
        return self.min_length + self.soft_margin

    def usable_max(self) -> np.ndarray:
        return self.max_length - self.soft_margin


@dataclass(frozen=True)
class Calibration:
    zero_offsets: np.ndarray = field(default_factory=lambda: np.zeros(6))  # pose added to every command
    actuator_offsets: np.ndarray = field(default_factory=lambda: np.zeros(6))  # m added per actuator
    neutral_pose: np.ndarray = field(default_factory=lambda: np.zeros(6))
    park_pose: np.ndarray = field(default_factory=lambda: np.zeros(6))


@dataclass(frozen=True)
class MotionSettings:
    min_blend_s: float = 1.0
    park_speed_scale: float = 0.35
    pause_behaviour: str = "hold"  # hold | neutral
    fault_recovery: str = "manual"  # manual | auto


@dataclass(frozen=True)
class PlatformConfig:
    platform_id: str
    name: str
    driver: str
    address: str
    link_platform_id: int
    geometry: StewartGeometry
    actuators: ActuatorLimits
    limits: AxisLimits
    calibration: Calibration
    motion: MotionSettings
    link_timeout_ms: int = 250
    source: str = ""
    sha256: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "name": self.name,
            "driver": self.driver,
            "address": self.address,
            "profile_sha256": self.sha256,
            "stroke_m": [round(float(s), 4) for s in self.actuators.stroke],
        }


def _vec(value: Any, n: int, name: str) -> np.ndarray:
    if isinstance(value, (int, float)):
        arr = np.full(n, float(value))
    else:
        arr = np.asarray(value, dtype=np.float64)
    if arr.shape != (n,):
        raise PlatformConfigError(f"{name} must be a number or a list of {n} numbers")
    if not np.all(np.isfinite(arr)):
        raise PlatformConfigError(f"{name} contains non-finite values")
    return arr


def _axes(table: dict[str, Any], name: str, *, pair: bool = False) -> Any:
    missing = [a for a in AXES if a not in table]
    if missing:
        raise PlatformConfigError(f"[{name}] missing axes: {', '.join(missing)}")
    if pair:
        lo, hi = [], []
        for a in AXES:
            v = table[a]
            if not (isinstance(v, list) and len(v) == 2):
                raise PlatformConfigError(f"[{name}].{a} must be [min, max]")
            if not v[0] < 0 < v[1]:
                raise PlatformConfigError(f"[{name}].{a} must satisfy min < 0 < max")
            lo.append(float(v[0]))
            hi.append(float(v[1]))
        return np.array(lo), np.array(hi)
    arr = np.array([float(table[a]) for a in AXES])
    if np.any(arr <= 0):
        raise PlatformConfigError(f"[{name}] values must be positive")
    return arr


def parse_platform_config(data: dict[str, Any], source: str = "", sha256: str = "") -> PlatformConfig:
    try:
        plat = data["platform"]
        geo = data["geometry"]
        act = data["actuators"]
        lim = data["limits"]
    except KeyError as exc:
        raise PlatformConfigError(f"missing section [{exc.args[0]}]") from exc
    driver = str(plat.get("driver", "servo_electric"))
    if driver in FUTURE_DRIVER_TYPES:
        raise PlatformConfigError(
            f"driver '{driver}' is not implemented in this release (only {', '.join(DRIVER_TYPES)}); "
            "see the ActuatorDriver interface in STEWART_KINEMATICS.md"
        )
    if driver not in DRIVER_TYPES:
        raise PlatformConfigError(f"unknown driver '{driver}'")
    try:
        geometry = StewartGeometry(
            base_joints=np.asarray(geo["base_joints"], dtype=np.float64),
            platform_joints=np.asarray(geo["platform_joints"], dtype=np.float64),
            neutral_position=np.asarray(geo["neutral_position"], dtype=np.float64),
            neutral_orientation=np.asarray(geo.get("neutral_orientation", [0, 0, 0]), dtype=np.float64),
            rotation_centre=np.asarray(geo.get("rotation_centre", [0, 0, 0]), dtype=np.float64),
        )
    except (KeyError, ValueError) as exc:
        raise PlatformConfigError(f"[geometry]: {exc}") from exc
    actuators = ActuatorLimits(
        min_length=_vec(act["min_length"], 6, "actuators.min_length"),
        max_length=_vec(act["max_length"], 6, "actuators.max_length"),
        max_velocity=float(act["max_velocity"]),
        max_acceleration=float(act["max_acceleration"]),
        soft_margin=float(act.get("soft_margin", 0.005)),
    )
    if np.any(actuators.max_length - actuators.min_length <= 2 * actuators.soft_margin):
        raise PlatformConfigError("actuators.max_length must exceed min_length by more than 2 x soft_margin")
    if actuators.max_velocity <= 0 or actuators.max_acceleration <= 0:
        raise PlatformConfigError("actuator max_velocity and max_acceleration must be positive")
    pmin, pmax = _axes(lim.get("position", {}), "limits.position", pair=True)
    limits = AxisLimits(
        position_min=pmin,
        position_max=pmax,
        velocity=_axes(lim.get("velocity", {}), "limits.velocity"),
        acceleration=_axes(lim.get("acceleration", {}), "limits.acceleration"),
        jerk=_axes(lim.get("jerk", {}), "limits.jerk"),
    )
    cal = data.get("calibration", {})
    calibration = Calibration(
        zero_offsets=_vec(cal.get("zero_offsets", 0.0), 6, "calibration.zero_offsets"),
        actuator_offsets=_vec(cal.get("actuator_offsets", 0.0), 6, "calibration.actuator_offsets"),
        neutral_pose=_vec(cal.get("neutral_pose", 0.0), 6, "calibration.neutral_pose"),
        park_pose=_vec(cal.get("park_pose", 0.0), 6, "calibration.park_pose"),
    )
    mot = data.get("motion", {})
    motion = MotionSettings(
        min_blend_s=float(mot.get("min_blend_s", 1.0)),
        park_speed_scale=float(mot.get("park_speed_scale", 0.35)),
        pause_behaviour=str(mot.get("pause_behaviour", "hold")),
        fault_recovery=str(mot.get("fault_recovery", "manual")),
    )
    if motion.pause_behaviour not in ("hold", "neutral"):
        raise PlatformConfigError("motion.pause_behaviour must be hold or neutral")
    if motion.fault_recovery not in ("manual", "auto"):
        raise PlatformConfigError("motion.fault_recovery must be manual or auto")
    if not 0.05 <= motion.park_speed_scale <= 1.0:
        raise PlatformConfigError("motion.park_speed_scale must be in [0.05, 1]")
    link_id = int(plat.get("link_platform_id", 1))
    if not 1 <= link_id <= 250:
        raise PlatformConfigError("platform.link_platform_id must be 1-250")
    cfg = PlatformConfig(
        platform_id=str(plat["id"]),
        name=str(plat.get("name", plat["id"])),
        driver=driver,
        address=str(plat.get("address", "")),
        link_platform_id=link_id,
        geometry=geometry,
        actuators=actuators,
        limits=limits,
        calibration=calibration,
        motion=motion,
        link_timeout_ms=int(plat.get("link_timeout_ms", 250)),
        source=source,
        sha256=sha256,
    )
    check_platform_config(cfg)
    return cfg


def check_platform_config(cfg: PlatformConfig) -> None:
    """Consistency checks that need kinematics (neutral, park and limit poses reachable)."""
    act = cfg.actuators
    for name, pose in (("neutral", cfg.calibration.neutral_pose), ("park", cfg.calibration.park_pose)):
        lengths = cfg.geometry.leg_lengths(pose + cfg.calibration.zero_offsets)
        if np.any(lengths < act.usable_min()) or np.any(lengths > act.usable_max()):
            raise PlatformConfigError(
                f"{name} pose needs actuator lengths {np.round(lengths, 4).tolist()} outside the usable stroke "
                f"[{act.usable_min().min():.4f}, {act.usable_max().max():.4f}] m"
            )
    if np.any(cfg.calibration.park_pose < cfg.limits.position_min) or np.any(
        cfg.calibration.park_pose > cfg.limits.position_max
    ):
        raise PlatformConfigError("calibration.park_pose lies outside limits.position")
    for i, axis in enumerate(AXES):
        for bound in (cfg.limits.position_min[i], cfg.limits.position_max[i]):
            pose = np.zeros(6)
            pose[i] = bound
            lengths = cfg.geometry.leg_lengths(pose)
            if np.any(lengths < act.min_length) or np.any(lengths > act.max_length):
                raise PlatformConfigError(
                    f"single-axis limit {axis}={bound:+.4f} is outside the actuator stroke; reduce limits.position"
                )


def load_platform_config(path: str | Path) -> PlatformConfig:
    p = Path(path)
    raw = p.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PlatformConfigError(f"{p}: {exc}") from exc
    try:
        return parse_platform_config(data, source=str(p), sha256=hashlib.sha256(raw).hexdigest())
    except PlatformConfigError as exc:
        raise PlatformConfigError(f"{p}: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise PlatformConfigError(f"{p}: missing or invalid value {exc}") from exc
