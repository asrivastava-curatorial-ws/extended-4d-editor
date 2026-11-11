"""Controller configuration: server connection, control rate and the platforms hosted by this process."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .platform_config import PlatformConfig, load_platform_config


class ControllerConfigError(ValueError):
    pass


@dataclass
class PlatformEntry:
    config: PlatformConfig
    device_key: str


@dataclass
class EffectsSettings:
    """Optional [effects] section. Without it the controller plays motion only."""

    patch_path: Path
    enabled: bool = True
    reference_platform: str = ""   # theatre fixtures follow this platform's timeline when it is playing


@dataclass
class ControllerConfig:
    server_url: str
    control_rate_hz: float = 100.0
    cache_dir: Path = Path("./cache")
    status_interval_s: float = 0.5
    time_sync_interval_s: float = 2.0
    server_loss_timeout_s: float = 10.0
    latency_compensation_s: float = 0.0
    log_level: str = "INFO"
    log_format: str = "text"
    platforms: list[PlatformEntry] = field(default_factory=list)
    source: str = ""
    effects: EffectsSettings | None = None


def _key(entry: dict[str, Any], base: Path) -> str:
    if entry.get("device_key"):
        return str(entry["device_key"]).strip()
    if entry.get("device_key_file"):
        return (base / str(entry["device_key_file"])).read_text().strip()
    raise ControllerConfigError("each [[platforms]] entry needs device_key or device_key_file")


def load_controller_config(path: str | Path) -> ControllerConfig:
    p = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ControllerConfigError(f"{p}: {exc}") from exc
    base = p.parent
    ctl = data.get("controller", {})
    if "server_url" not in ctl:
        raise ControllerConfigError("[controller].server_url is required (ws://<server>:8080/ws/v1/device)")
    url = str(ctl["server_url"])
    if not url.startswith(("ws://", "wss://")):
        raise ControllerConfigError("server_url must start with ws:// or wss://")
    rate = float(ctl.get("control_rate_hz", 100.0))
    if not 50.0 <= rate <= 200.0:
        raise ControllerConfigError("control_rate_hz must be between 50 and 200")
    cache = Path(str(ctl.get("cache_dir", "./cache")))
    cfg = ControllerConfig(
        server_url=url,
        control_rate_hz=rate,
        cache_dir=cache if cache.is_absolute() else (base / cache),
        status_interval_s=float(ctl.get("status_interval_s", 0.5)),
        time_sync_interval_s=float(ctl.get("time_sync_interval_s", 2.0)),
        server_loss_timeout_s=float(ctl.get("server_loss_timeout_s", 10.0)),
        latency_compensation_s=float(ctl.get("latency_compensation_s", 0.0)),
        log_level=str(ctl.get("log_level", "INFO")),
        log_format=str(ctl.get("log_format", "text")),
        source=str(p),
    )
    if not 0.0 <= cfg.latency_compensation_s <= 0.2:
        raise ControllerConfigError("latency_compensation_s must be between 0 and 0.2 s")
    fx = data.get("effects")
    if fx is not None:
        if not isinstance(fx, dict) or not fx.get("patch"):
            raise ControllerConfigError('[effects] needs patch = "effects/patch.toml" (or remove the section)')
        unknown = sorted(set(fx) - {"patch", "enabled", "reference_platform"})
        if unknown:
            raise ControllerConfigError(f"[effects]: unknown key(s) {', '.join(unknown)}")
        patch_path = Path(str(fx["patch"])).expanduser()
        cfg.effects = EffectsSettings(
            patch_path=patch_path if patch_path.is_absolute() else (base / patch_path),
            enabled=bool(fx.get("enabled", True)),
            reference_platform=str(fx.get("reference_platform", "")),
        )
    entries = data.get("platforms", [])
    if not entries:
        raise ControllerConfigError("at least one [[platforms]] entry is required")
    seen: set[str] = set()
    for entry in entries:
        pc = load_platform_config(base / str(entry["config"]))
        if "id" in entry:
            pc = _with_id(pc, str(entry["id"]), int(entry.get("link_platform_id", pc.link_platform_id)), entry)
        if pc.platform_id in seen:
            raise ControllerConfigError(f"duplicate platform id {pc.platform_id}")
        seen.add(pc.platform_id)
        cfg.platforms.append(PlatformEntry(pc, _key(entry, base)))
    return cfg


def _with_id(pc: PlatformConfig, platform_id: str, link_id: int, entry: dict[str, Any]) -> PlatformConfig:
    """Allow one shared profile file for many identical platforms (id/address/driver per entry)."""
    from dataclasses import replace

    return replace(
        pc,
        platform_id=platform_id,
        name=str(entry.get("name", platform_id)),
        link_platform_id=link_id,
        address=str(entry.get("address", pc.address)),
        driver=str(entry.get("driver", pc.driver)),
    )
