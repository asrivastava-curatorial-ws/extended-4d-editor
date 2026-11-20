"""Motion platform profile (geometry, actuators, limits, calibration, how it is reached) loaded from TOML.

One implementation for the Editor and the show service, so that a programme is checked against
exactly the profile the show service enforces (fourdvr_extractor.platform_config re-exports it).

A platform has 2 to 6 linear actuators - electric, hydraulic or pneumatic - and moves on the axes
its mechanics allow (``[geometry] dof``): a Stewart platform on all six, a seat on three (heave,
roll, pitch) or two (roll, pitch). See PLATFORM_CONFIGURATION.md, ACTUATORS.md and CALIBRATION.md.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .interfaces import InterfaceError, ModbusSettings, UdpSettings, parse_modbus, parse_udp
from .kinematics import AXES, StewartGeometry

DRIVER_TYPES = ("esp32", "modbus_tcp", "udp", "simulation")
DRIVER_ALIASES = {"servo_electric": "esp32"}          # the name of releases before 3.4
ACTUATOR_TYPES = ("electric", "hydraulic", "pneumatic")
CONTROL_TYPES = ("position", "onoff")
FUTURE_DRIVER_TYPES: tuple[str, ...] = ()               # kept for older imports: every driver type exists now


class PlatformConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AxisLimits:
    position_min: np.ndarray  # (6,) an axis the platform does not have: 0
    position_max: np.ndarray  # (6,)
    velocity: np.ndarray  # (6,) symmetric magnitude
    acceleration: np.ndarray  # (6,)
    jerk: np.ndarray  # (6,)

    def clip_position(self, pose: np.ndarray) -> np.ndarray:
        return np.clip(pose, self.position_min, self.position_max)


@dataclass(frozen=True)
class ActuatorLimits:
    min_length: np.ndarray  # (n,) pin-to-pin length fully retracted (m)
    max_length: np.ndarray  # (n,) fully extended (m)
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
    actuator_offsets: np.ndarray = field(default_factory=lambda: np.zeros(6))  # m added per actuator (n values)
    neutral_pose: np.ndarray = field(default_factory=lambda: np.zeros(6))
    park_pose: np.ndarray = field(default_factory=lambda: np.zeros(6))


@dataclass(frozen=True)
class MotionSettings:
    min_blend_s: float = 1.0
    park_speed_scale: float = 0.35
    pause_behaviour: str = "hold"  # hold | neutral
    fault_recovery: str = "manual"  # manual | auto


@dataclass(frozen=True)
class OnOffSettings:
    """Cylinders on plain solenoid valves: each is either retracted or extended (docs/ACTUATORS.md)."""

    rest: str = "retracted"   # where a cylinder is when its valve is off: retracted | extended
    on_level: float = 0.25    # the motion asks this far from neutral (fraction of half the stroke): switch over
    off_level: float = 0.10   # ... and back below this (hysteresis)
    min_dwell_s: float = 0.25  # a valve keeps its state at least this long
    travel_s: float = 0.5     # a cylinder's time for its full stroke (expected position, simulation)


@dataclass(frozen=True)
class SupervisionSettings:
    """Actuators whose controller reports measured positions are watched: one that does not follow is a fault."""

    following_error: float = 0.02   # m beyond what the commands of the last lag_s allow; 0 = not watched
    following_error_s: float = 0.3  # ... for this long
    lag_s: float = 0.08             # how far an actuator may lag behind its command


def default_supervision(actuator: str, control: str, onoff: OnOffSettings | None, stroke: float) -> SupervisionSettings:
    if control == "onoff":
        travel = onoff.travel_s if onoff is not None else 0.5
        return SupervisionSettings(round(0.25 * stroke, 4), max(0.5, travel), round(1.5 * travel, 3))
    return {
        "electric": SupervisionSettings(0.02, 0.3, 0.08),
        "hydraulic": SupervisionSettings(0.015, 0.3, 0.1),
        "pneumatic": SupervisionSettings(0.03, 0.5, 0.2),
    }[actuator]


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
    actuator: str = "electric"           # electric | hydraulic | pneumatic
    control: str = "position"            # position | onoff
    onoff: OnOffSettings | None = None
    supervision: SupervisionSettings = field(default_factory=SupervisionSettings)
    modbus: ModbusSettings | None = None
    udp: UdpSettings | None = None

    @property
    def legs(self) -> int:
        return self.geometry.legs

    @property
    def dof(self) -> tuple[str, ...]:
        return self.geometry.dof

    def to_summary(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "name": self.name,
            "driver": self.driver,
            "address": self.address,
            "actuator": self.actuator,
            "control": self.control,
            "actuators": self.legs,
            "dof": list(self.dof),
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


def _axes(table: dict[str, Any], name: str, mask: np.ndarray, *, pair: bool = False) -> Any:
    """Per-axis values of the axes the platform has; the others are 0 (position) or 1 (rates, never used)."""
    wanted = [a for a, on in zip(AXES, mask, strict=True) if on]
    missing = [a for a in wanted if a not in table]
    if missing:
        raise PlatformConfigError(f"[{name}] missing axes: {', '.join(missing)}")
    if pair:
        lo, hi = np.zeros(6), np.zeros(6)
        for i, a in enumerate(AXES):
            if not mask[i]:
                continue
            v = table[a]
            if not (isinstance(v, list) and len(v) == 2):
                raise PlatformConfigError(f"[{name}].{a} must be [min, max]")
            if not v[0] < 0 < v[1]:
                raise PlatformConfigError(f"[{name}].{a} must satisfy min < 0 < max")
            lo[i], hi[i] = float(v[0]), float(v[1])
        return lo, hi
    arr = np.array([float(table[a]) if mask[i] else 1.0 for i, a in enumerate(AXES)])
    if np.any(arr <= 0):
        raise PlatformConfigError(f"[{name}] values must be positive")
    return arr


def _choice(table: dict[str, Any], key: str, default: str, allowed: tuple[str, ...], where: str) -> str:
    value = str(table.get(key, default))
    if value not in allowed:
        raise PlatformConfigError(f"[{where}] {key} must be one of {', '.join(allowed)}")
    return value


def parse_platform_config(data: dict[str, Any], source: str = "", sha256: str = "") -> PlatformConfig:
    try:
        plat = data["platform"]
        geo = data["geometry"]
        act = data["actuators"]
        lim = data["limits"]
    except KeyError as exc:
        raise PlatformConfigError(f"missing section [{exc.args[0]}]") from exc
    driver = str(plat.get("driver", "esp32"))
    if driver in ("hydraulic", "pneumatic"):
        raise PlatformConfigError(
            f'driver = "{driver}" names the kind of actuator: write actuator = "{driver}" and choose how its controller '
            'is reached with driver = "esp32", "modbus_tcp" or "udp" (docs/ACTUATORS.md)'
        )
    driver = DRIVER_ALIASES.get(driver, driver)
    if driver not in DRIVER_TYPES:
        raise PlatformConfigError(f"unknown driver '{driver}' (esp32, modbus_tcp, udp or simulation)")
    actuator = _choice(plat, "actuator", "electric", ACTUATOR_TYPES, "platform")
    control = _choice(plat, "control", "position", CONTROL_TYPES, "platform")
    if control == "onoff" and actuator == "electric":
        raise PlatformConfigError('control = "onoff" is for cylinders on solenoid valves: actuator = "pneumatic" or "hydraulic"')

    dof_names = geo.get("dof", list(AXES))
    if not isinstance(dof_names, list) or not dof_names or any(a not in AXES for a in dof_names) or len(set(dof_names)) != len(dof_names):
        raise PlatformConfigError(f"[geometry] dof must list axes of {', '.join(AXES)}, each once")
    mask = np.array([a in dof_names for a in AXES])
    try:
        geometry = StewartGeometry(
            base_joints=np.asarray(geo["base_joints"], dtype=np.float64),
            platform_joints=np.asarray(geo["platform_joints"], dtype=np.float64),
            neutral_position=np.asarray(geo["neutral_position"], dtype=np.float64),
            neutral_orientation=np.asarray(geo.get("neutral_orientation", [0, 0, 0]), dtype=np.float64),
            rotation_centre=np.asarray(geo.get("rotation_centre", [0, 0, 0]), dtype=np.float64),
            dof_mask=mask,
        )
    except (KeyError, ValueError) as exc:
        raise PlatformConfigError(f"[geometry]: {exc}") from exc
    problem = geometry.dof_problem()
    if problem:
        raise PlatformConfigError(f"[geometry]: {problem}")
    n = geometry.legs
    actuators = ActuatorLimits(
        min_length=_vec(act["min_length"], n, "actuators.min_length"),
        max_length=_vec(act["max_length"], n, "actuators.max_length"),
        max_velocity=float(act["max_velocity"]),
        max_acceleration=float(act["max_acceleration"]),
        soft_margin=float(act.get("soft_margin", 0.005)),
    )
    if np.any(actuators.max_length - actuators.min_length <= 2 * actuators.soft_margin):
        raise PlatformConfigError("actuators.max_length must exceed min_length by more than 2 x soft_margin")
    if actuators.max_velocity <= 0 or actuators.max_acceleration <= 0:
        raise PlatformConfigError("actuator max_velocity and max_acceleration must be positive")
    pmin, pmax = _axes(lim.get("position", {}), "limits.position", mask, pair=True)
    limits = AxisLimits(
        position_min=pmin,
        position_max=pmax,
        velocity=_axes(lim.get("velocity", {}), "limits.velocity", mask),
        acceleration=_axes(lim.get("acceleration", {}), "limits.acceleration", mask),
        jerk=_axes(lim.get("jerk", {}), "limits.jerk", mask),
    )
    cal = data.get("calibration", {})
    calibration = Calibration(
        zero_offsets=_vec(cal.get("zero_offsets", 0.0), 6, "calibration.zero_offsets"),
        actuator_offsets=_vec(cal.get("actuator_offsets", 0.0), n, "calibration.actuator_offsets"),
        neutral_pose=_vec(cal.get("neutral_pose", 0.0), 6, "calibration.neutral_pose"),
        park_pose=_vec(cal.get("park_pose", 0.0), 6, "calibration.park_pose"),
    )
    for name in ("zero_offsets", "neutral_pose", "park_pose"):
        if np.any(getattr(calibration, name)[~mask] != 0.0):
            off = [a for a, on in zip(AXES, mask, strict=True) if not on]
            raise PlatformConfigError(f"calibration.{name}: this platform does not move on {', '.join(off)} - those values must be 0")
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

    onoff = None
    if control == "onoff":
        t = data.get("onoff", {})
        unknown = sorted(set(t) - {"rest", "on_level", "off_level", "min_dwell_s", "travel_s"})
        if unknown:
            raise PlatformConfigError(f"[onoff]: unknown key(s) {', '.join(unknown)}")
        onoff = OnOffSettings(
            rest=_choice(t, "rest", "retracted", ("retracted", "extended"), "onoff"),
            on_level=float(t.get("on_level", 0.25)), off_level=float(t.get("off_level", 0.10)),
            min_dwell_s=float(t.get("min_dwell_s", 0.25)), travel_s=float(t.get("travel_s", 0.5)),
        )
        if not 0.0 <= onoff.off_level < onoff.on_level <= 1.0:
            raise PlatformConfigError("[onoff] needs 0 <= off_level < on_level <= 1")
        if not 0.0 <= onoff.min_dwell_s <= 5.0 or not 0.05 <= onoff.travel_s <= 10.0:
            raise PlatformConfigError("[onoff] min_dwell_s must be 0-5 s and travel_s 0.05-10 s")
    elif "onoff" in data:
        raise PlatformConfigError('[onoff] is for control = "onoff"')
    sup = data.get("supervision", {})
    unknown = sorted(set(sup) - {"following_error", "following_error_s", "lag_s"})
    if unknown:
        raise PlatformConfigError(f"[supervision]: unknown key(s) {', '.join(unknown)}")
    base = default_supervision(actuator, control, onoff, float(np.min(actuators.stroke)))
    supervision = SupervisionSettings(
        following_error=float(sup.get("following_error", base.following_error)),
        following_error_s=float(sup.get("following_error_s", base.following_error_s)),
        lag_s=float(sup.get("lag_s", base.lag_s)),
    )
    if supervision.following_error < 0 or not 0.02 <= supervision.following_error_s <= 5.0 or not 0.0 <= supervision.lag_s <= 5.0:
        raise PlatformConfigError("[supervision] following_error >= 0 m, following_error_s 0.02-5 s, lag_s 0-5 s")

    modbus = udp = None
    try:
        if driver == "modbus_tcp":
            modbus = parse_modbus(data.get("modbus", {}))
        elif "modbus" in data and driver != "simulation":
            raise PlatformConfigError('[modbus] is for driver = "modbus_tcp"')
        if driver == "udp":
            udp = parse_udp(data.get("udp", {}), geometry.dof)
            if udp.send == "valves" and control != "onoff":
                raise PlatformConfigError('[udp] send = "valves" is for control = "onoff"')
            if udp.send == "pose" and control == "onoff":
                raise PlatformConfigError('[udp] a pose cannot carry valve states: send = "valves" or "positions"')
        elif "udp" in data and driver != "simulation":
            raise PlatformConfigError('[udp] is for driver = "udp"')
    except InterfaceError as exc:
        raise PlatformConfigError(str(exc)) from exc
    address = str(plat.get("address", ""))
    if driver == "modbus_tcp" and not address.startswith("tcp://"):
        raise PlatformConfigError('driver = "modbus_tcp" needs address = "tcp://HOST:502"')
    if driver == "udp" and not address.startswith("udp://"):
        raise PlatformConfigError('driver = "udp" needs address = "udp://HOST:PORT"')
    cfg = PlatformConfig(
        platform_id=str(plat["id"]),
        name=str(plat.get("name", plat["id"])),
        driver=driver,
        address=address,
        link_platform_id=link_id,
        geometry=geometry,
        actuators=actuators,
        limits=limits,
        calibration=calibration,
        motion=motion,
        link_timeout_ms=int(plat.get("link_timeout_ms", 250)),
        source=source,
        sha256=sha256,
        actuator=actuator,
        control=control,
        onoff=onoff,
        supervision=supervision,
        modbus=modbus,
        udp=udp,
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
        if not cfg.geometry.dof_mask[i]:
            continue
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
