"""Validation of a 6-DoF trajectory against a platform profile (before export).

NOTE: passing validation means the trajectory is numerically inside the configured software
limits. It does NOT mean the motion is physically safe or comfortable for passengers; that
requires the independent safety measures described in README.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from fourdvr_motion.platform_config import PlatformConfig

from .signals import derivatives

AXES = ("surge", "sway", "heave", "roll", "pitch", "yaw")


@dataclass
class ValidationReport:
    profile: str
    profile_sha256: str
    passed: bool
    axes: list[dict[str, Any]] = field(default_factory=list)
    actuators: dict[str, Any] = field(default_factory=dict)
    workspace_violations: int = 0
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "profile_sha256": self.profile_sha256,
            "passed": self.passed,
            "axes": self.axes,
            "actuators": self.actuators,
            "workspace_violations": self.workspace_violations,
            "violations": self.violations,
            "disclaimer": "numerical limit check only; not a passenger-safety certification",
        }

    def rows(self) -> list[tuple[str, str, str, str]]:
        """(quantity, value, limit, status) rows for tables."""
        out = []
        for ax in self.axes:
            unit, k = ("deg", 180 / math.pi) if ax["axis"] in ("roll", "pitch", "yaw") else ("mm", 1000.0)
            for key, lim_key, label in (
                ("max_position", "position_limit", "position"),
                ("max_velocity", "velocity_limit", "velocity"),
                ("max_acceleration", "acceleration_limit", "acceleration"),
                ("max_jerk", "jerk_limit", "jerk"),
            ):
                suffix = {"position": "", "velocity": "/s", "acceleration": "/s²", "jerk": "/s³"}[label]
                ok = ax[key] <= ax[lim_key] * (1 + 1e-9)
                out.append(
                    (
                        f"{ax['axis']} {label}",
                        f"{ax[key] * k:.2f} {unit}{suffix}",
                        f"{ax[lim_key] * k:.2f} {unit}{suffix}",
                        "OK" if ok else "VIOLATION",
                    )
                )
        a = self.actuators
        out.append(
            (
                "actuator max extension",
                f"{a['max_extension_m'] * 1000:.1f} mm",
                f"{a['extension_available_m'] * 1000:.1f} mm",
                "OK" if a["stroke_ok"] else "VIOLATION",
            )
        )
        out.append(
            (
                "actuator max compression",
                f"{a['max_compression_m'] * 1000:.1f} mm",
                f"{a['compression_available_m'] * 1000:.1f} mm",
                "OK" if a["stroke_ok"] else "VIOLATION",
            )
        )
        out.append(
            (
                "actuator velocity",
                f"{a['max_velocity_m_s']:.3f} m/s",
                f"{a['velocity_limit_m_s']:.3f} m/s",
                "OK" if a["velocity_ok"] else "VIOLATION",
            )
        )
        out.append(
            (
                "actuator acceleration",
                f"{a['max_acceleration_m_s2']:.2f} m/s²",
                f"{a['acceleration_limit_m_s2']:.2f} m/s²",
                "OK" if a["acceleration_ok"] else "VIOLATION",
            )
        )
        out.append(
            (
                "stroke utilisation",
                f"{a['stroke_utilisation'] * 100:.1f} %",
                "100 %",
                "OK" if a["stroke_ok"] else "VIOLATION",
            )
        )
        out.append(
            (
                "workspace violations",
                str(self.workspace_violations),
                "0",
                "OK" if self.workspace_violations == 0 else "VIOLATION",
            )
        )
        return out


def validate(motion: np.ndarray, fs: float, profile: PlatformConfig) -> ValidationReport:
    x = np.asarray(motion, dtype=np.float64)
    lim = profile.limits
    act = profile.actuators
    v, a, j = derivatives(x, fs)
    tol = 1e-9
    report = ValidationReport(profile=profile.name, profile_sha256=profile.sha256, passed=True)
    for k, axis in enumerate(AXES):
        pos_lim = float(min(abs(lim.position_min[k]), abs(lim.position_max[k])))
        entry: dict[str, Any] = {
            "axis": axis,
            "min_position": float(x[:, k].min()),
            "max_position": float(np.abs(x[:, k]).max()),
            "max_velocity": float(np.abs(v[:, k]).max()),
            "max_acceleration": float(np.abs(a[:, k]).max()),
            "max_jerk": float(np.abs(j[:, k]).max()),
            "position_limit": pos_lim,
            "velocity_limit": float(lim.velocity[k]),
            "acceleration_limit": float(lim.acceleration[k]),
            "jerk_limit": float(lim.jerk[k]),
        }
        entry["utilisation"] = round(entry["max_position"] / pos_lim, 4)
        report.axes.append(entry)
        if np.any(x[:, k] < lim.position_min[k] - tol) or np.any(x[:, k] > lim.position_max[k] + tol):
            report.violations.append(f"{axis} position outside [{lim.position_min[k]}, {lim.position_max[k]}]")
        for key, lk in (
            ("max_velocity", "velocity_limit"),
            ("max_acceleration", "acceleration_limit"),
            ("max_jerk", "jerk_limit"),
        ):
            if entry[key] > entry[lk] * (1 + 1e-6):
                report.violations.append(f"{axis} {key.split('_')[1]} {entry[key]:.4g} > {entry[lk]:.4g}")
    lengths = profile.geometry.leg_lengths_many(x + profile.calibration.zero_offsets)
    neutral = profile.geometry.leg_lengths(profile.calibration.zero_offsets)
    lv = np.gradient(lengths, 1.0 / fs, axis=0)
    la = np.gradient(lv, 1.0 / fs, axis=0)
    out_of_stroke = np.any((lengths < act.usable_min() - tol) | (lengths > act.usable_max() + tol), axis=1)
    out_of_pos = np.any((x < lim.position_min - tol) | (x > lim.position_max + tol), axis=1)
    report.workspace_violations = int(np.sum(out_of_stroke | out_of_pos))
    mid = (act.min_length + act.max_length) / 2
    half = act.stroke / 2
    report.actuators = {
        "min_length_m": float(lengths.min()),
        "max_length_m": float(lengths.max()),
        "max_extension_m": float(np.max(lengths - neutral)),
        "max_compression_m": float(np.max(neutral - lengths)),
        "extension_available_m": float(np.min(act.usable_max() - neutral)),
        "compression_available_m": float(np.min(neutral - act.usable_min())),
        "max_velocity_m_s": float(np.abs(lv).max()),
        "max_acceleration_m_s2": float(np.abs(la).max()),
        "velocity_limit_m_s": act.max_velocity,
        "acceleration_limit_m_s2": act.max_acceleration,
        "stroke_utilisation": float(np.max(np.abs(lengths - mid) / half)),
        "stroke_ok": not bool(np.any(out_of_stroke)),
        "velocity_ok": bool(np.abs(lv).max() <= act.max_velocity * (1 + 1e-6)),
        "acceleration_ok": bool(np.abs(la).max() <= act.max_acceleration * (1 + 1e-6)),
    }
    if np.any(out_of_stroke):
        report.violations.append(
            f"{int(np.sum(out_of_stroke))} samples need actuator lengths outside the usable stroke"
        )
    if not report.actuators["velocity_ok"]:
        report.violations.append("actuator velocity limit exceeded")
    if not report.actuators["acceleration_ok"]:
        report.violations.append("actuator acceleration limit exceeded")
    report.passed = not report.violations
    return report
