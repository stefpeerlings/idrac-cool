"""Load and validate dashboard configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

MODES = ("smart_auto", "manual", "dell_auto")
Mode = Literal["smart_auto", "manual", "dell_auto"]

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATHS = (
    ROOT / "config.yaml",
    ROOT / "config.local.yaml",
    Path(os.environ.get("IDRAC_DASHBOARD_CONFIG", "")),
)


@dataclass
class SmartAutoBand:
    below_c: float
    percent: int


@dataclass
class SmartAutoConfig:
    metric: str = "cpu_max"
    hysteresis_c: float = 3.0
    bands: list[SmartAutoBand] = field(default_factory=list)


@dataclass
class HostConfig:
    id: str
    name: str
    host: str
    username: str | None = None
    password: str | None = None


@dataclass
class AppConfig:
    poll_interval_seconds: float = 5.0
    fan_presets: list[int] = field(default_factory=lambda: [15, 20, 25, 30, 40, 50])
    fan_min_percent: int = 10
    fan_max_percent: int = 50
    # Max fan % for Dashboard Auto (smart curve); independent of manual max.
    smart_auto_max_percent: int = 30
    reapply_interval_seconds: float = 60.0
    reapply_on_host_online: bool = True
    default_mode: Mode = "smart_auto"
    default_percent: int = 20
    ipmi_timeout_seconds: float = 8.0
    bind_host: str = "0.0.0.0"
    bind_port: int = 8787
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    credentials_username: str = "root"
    credentials_password: str = ""
    smart_auto: SmartAutoConfig = field(default_factory=SmartAutoConfig)
    hosts: list[HostConfig] = field(default_factory=list)
    mock_ipmi: bool = False
    data_dir: Path = field(default_factory=lambda: ROOT / "data")


def _find_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for p in DEFAULT_CONFIG_PATHS:
        if p and str(p) and p.is_file():
            return p
    return None


def _default_bands() -> list[SmartAutoBand]:
    return [
        SmartAutoBand(45, 15),
        SmartAutoBand(55, 20),
        SmartAutoBand(65, 25),
        SmartAutoBand(999, 30),
    ]


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = _find_config_path(path)
    raw: dict[str, Any] = {}
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    creds = raw.get("credentials") or {}
    username = (
        os.environ.get("IDRAC_USERNAME")
        or creds.get("username")
        or "root"
    )
    password = (
        os.environ.get("IDRAC_PASSWORD")
        or creds.get("password")
        or ""
    )

    sa_raw = raw.get("smart_auto") or {}
    bands_raw = sa_raw.get("bands")
    if bands_raw:
        bands = [
            SmartAutoBand(float(b["below_c"]), int(b["percent"]))
            for b in bands_raw
        ]
        bands.sort(key=lambda b: b.below_c)
    else:
        bands = _default_bands()

    fan_min = int(raw.get("fan_min_percent", 10))
    fan_max = int(raw.get("fan_max_percent", 50))
    if fan_min < 1:
        fan_min = 1
    if fan_max > 100:
        fan_max = 100
    if fan_min > fan_max:
        fan_min, fan_max = fan_max, fan_min
    # Smart-auto bands stay at their configured % (default max 30 in bands);
    # manual presets / custom may go higher up to fan_max.
    for b in bands:
        b.percent = min(b.percent, fan_max)

    presets = [int(p) for p in raw.get("fan_presets", [15, 20, 25, 30, 40, 50])]
    presets = [p for p in presets if fan_min <= p <= fan_max]
    if not presets:
        presets = [p for p in (15, 20, 25, 30, 40, 50) if fan_min <= p <= fan_max]

    default_mode = str(raw.get("default_mode", "smart_auto"))
    if default_mode not in MODES:
        default_mode = "smart_auto"

    default_percent = int(raw.get("default_percent", 20))
    if default_percent not in presets:
        default_percent = presets[0] if presets else 20
    default_percent = min(default_percent, fan_max)

    hosts: list[HostConfig] = []
    for h in raw.get("hosts") or []:
        hosts.append(
            HostConfig(
                id=str(h["id"]),
                name=str(h.get("name") or h["id"]),
                host=str(h["host"]),
                username=h.get("username"),
                password=h.get("password"),
            )
        )

    # Mock hosts if none configured and mock mode
    mock = os.environ.get("MOCK_IPMI", "").lower() in ("1", "true", "yes")
    if not hosts and mock:
        hosts = [
            HostConfig(id="demo1", name="demo1 (mock R630)", host="127.0.0.1"),
            HostConfig(id="demo2", name="demo2 (mock R630)", host="127.0.0.2"),
        ]

    # TLS: env SSL_CERTFILE / SSL_KEYFILE override config
    ssl_cert = (
        os.environ.get("SSL_CERTFILE")
        or os.environ.get("IDRAC_SSL_CERTFILE")
        or raw.get("ssl_certfile")
        or None
    )
    ssl_key = (
        os.environ.get("SSL_KEYFILE")
        or os.environ.get("IDRAC_SSL_KEYFILE")
        or raw.get("ssl_keyfile")
        or None
    )
    if ssl_cert:
        ssl_cert = str(ssl_cert).strip() or None
    if ssl_key:
        ssl_key = str(ssl_key).strip() or None
    if bool(ssl_cert) != bool(ssl_key):
        raise ValueError("ssl_certfile and ssl_keyfile must both be set (or neither)")

    # Default Auto cap: top band of curve, or explicit smart_auto.max_percent
    curve_top = max((b.percent for b in bands), default=30)
    sa_max = sa_raw.get("max_percent", raw.get("smart_auto_max_percent", curve_top))
    try:
        smart_auto_max = int(sa_max)
    except (TypeError, ValueError):
        smart_auto_max = int(curve_top)
    smart_auto_max = max(fan_min, min(fan_max, smart_auto_max))

    return AppConfig(
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 5)),
        fan_presets=presets,
        fan_min_percent=fan_min,
        fan_max_percent=fan_max,
        smart_auto_max_percent=smart_auto_max,
        reapply_interval_seconds=float(raw.get("reapply_interval_seconds", 60)),
        reapply_on_host_online=bool(raw.get("reapply_on_host_online", True)),
        default_mode=default_mode,  # type: ignore[arg-type]
        default_percent=default_percent,
        ipmi_timeout_seconds=float(raw.get("ipmi_timeout_seconds", 8)),
        bind_host=str(raw.get("bind_host", "0.0.0.0")),
        bind_port=int(raw.get("bind_port", 8787)),
        ssl_certfile=ssl_cert,
        ssl_keyfile=ssl_key,
        credentials_username=username,
        credentials_password=password,
        smart_auto=SmartAutoConfig(
            metric=str(sa_raw.get("metric", "cpu_max")),
            hysteresis_c=float(sa_raw.get("hysteresis_c", 3)),
            bands=bands,
        ),
        hosts=hosts,
        mock_ipmi=mock,
        data_dir=ROOT / "data",
    )


def resolve_credentials(cfg: AppConfig, host: HostConfig) -> tuple[str, str]:
    user = host.username or cfg.credentials_username
    password = host.password or cfg.credentials_password
    return user, password
