from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALLOWED_METER_PORTS: tuple[int, ...] = (502, 1502, 2502, 3502, 4502, 5502)


@dataclass(frozen=True)
class MeterConfig:
    meter_id: str
    enabled: bool
    modbus_host: str
    public_port: int
    backend_bind_port: int
    alias_unit_ids: tuple[int, ...]
    unit_id: int
    device_modbus_address: int
    device_manufacturer: str
    device_model: str
    device_name: str
    device_version: str
    device_serial: str
    entity_map_path: str | None
    inline_entity_map: dict[str, Any] | None
    modbus_run_backend: bool
    modbus_backend_host: str
    modbus_backend_port: int
    modbus_raw_relay: bool
    modbus_enable_proxy: bool
    modbus_log_reads: bool
    modbus_gate_enable: bool


@dataclass(frozen=True)
class AppConfig:
    mode: str
    ha_base_url: str
    ha_token: str
    ws_url: str
    status_host: str
    status_port: int
    resync_interval_s: int
    config_reload_interval_s: int
    log_level: str
    meters_file: str | None
    meters: tuple[MeterConfig, ...]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int_list(name: str, default: str = "") -> tuple[int, ...]:
    raw = _env(name, default)
    if not raw:
        return tuple()
    values: list[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        values.append(int(part))
    return tuple(values)


def _normalize_ha_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/api"):
        return base_url
    return base_url + "/api"


def _derive_ws_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/api"):
        base_url = base_url[:-4]
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + "/api/websocket"
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + "/api/websocket"
    return base_url + "/api/websocket"


def _resolve_path(path_value: str | None, base_dir: Path) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = base_dir / path
    return str(path)


def _validate_meter_ports(meters: list[MeterConfig]) -> None:
    seen: set[int] = set()
    seen_ids: set[str] = set()
    seen_serials: set[str] = set()
    for meter in meters:
        if meter.public_port not in ALLOWED_METER_PORTS:
            raise ValueError(f"Meter {meter.meter_id}: port {meter.public_port} is not in allowed fixed ports {ALLOWED_METER_PORTS}")
        if meter.public_port in seen:
            raise ValueError(f"Duplicate meter port {meter.public_port}")
        if meter.meter_id in seen_ids:
            raise ValueError(f"Duplicate meter id {meter.meter_id}")
        if meter.device_serial in seen_serials:
            raise ValueError(f"Duplicate meter serial {meter.device_serial}")
        seen.add(meter.public_port)
        seen_ids.add(meter.meter_id)
        seen_serials.add(meter.device_serial)


def _meter_from_dict(raw: dict[str, Any], defaults: dict[str, Any], *, base_dir: Path) -> MeterConfig:
    merged = dict(defaults)
    merged.update(raw)
    meter_id = str(merged.get("id") or merged.get("name") or f"meter_{merged.get('port', 'unknown')}").strip()
    public_port = int(merged.get("port", 502))
    entity_map_path = _resolve_path(merged.get("entity_map_path"), base_dir)
    backend_bind_port = int(merged.get("backend_bind_port", public_port + 10000))
    backend_port = int(merged.get("modbus_backend_port", backend_bind_port))
    alias_raw = merged.get("alias_unit_ids", merged.get("aliases", []))
    if isinstance(alias_raw, str):
        alias_unit_ids = tuple(int(part.strip()) for part in alias_raw.split(',') if part.strip())
    elif isinstance(alias_raw, list):
        alias_unit_ids = tuple(int(v) for v in alias_raw)
    else:
        alias_unit_ids = tuple()
    return MeterConfig(
        meter_id=meter_id,
        enabled=bool(merged.get("enabled", True)),
        modbus_host=str(merged.get("modbus_host", merged.get("bind_host", "0.0.0.0"))),
        public_port=public_port,
        backend_bind_port=backend_bind_port,
        alias_unit_ids=alias_unit_ids,
        unit_id=int(merged.get("unit_id", 3)),
        device_modbus_address=int(merged.get("device_modbus_address", 241)),
        device_manufacturer=str(merged.get("device_manufacturer", merged.get("manufacturer", "Fronius"))),
        device_model=str(merged.get("device_model", merged.get("model", "Smart Meter 63A"))),
        device_name=str(merged.get("device_name", f"<{meter_id}>")),
        device_version=str(merged.get("device_version", "3.0.1-0002")),
        device_serial=str(merged.get("device_serial", "00000002")),
        entity_map_path=entity_map_path,
        inline_entity_map=merged.get("entities") if isinstance(merged.get("entities"), dict) else None,
        modbus_run_backend=bool(merged.get("modbus_run_backend", True)),
        modbus_backend_host=str(merged.get("modbus_backend_host", "127.0.0.1")),
        modbus_backend_port=backend_port,
        modbus_raw_relay=bool(merged.get("modbus_raw_relay", False)),
        modbus_enable_proxy=bool(merged.get("modbus_enable_proxy", True)),
        modbus_log_reads=bool(merged.get("modbus_log_reads", True)),
        modbus_gate_enable=bool(merged.get("modbus_gate_enable", True)),
    )


def _default_single_meter() -> MeterConfig:
    return MeterConfig(
        meter_id="sm1",
        enabled=True,
        modbus_host=_env("MODBUS_HOST", "0.0.0.0"),
        public_port=int(_env("MODBUS_PORT", "502")),
        backend_bind_port=int(_env("MODBUS_BIND_PORT", "1502")),
        alias_unit_ids=_env_int_list("MODBUS_ALIAS_UNIT_IDS", "1"),
        unit_id=int(_env("MODBUS_UNIT_ID", "3")),
        device_modbus_address=int(_env("DEVICE_MODBUS_ADDRESS", "241")),
        device_manufacturer=_env("DEVICE_MANUFACTURER", "Fronius"),
        device_model=_env("DEVICE_MODEL", "Smart Meter 63A"),
        device_name=_env("DEVICE_NAME", "meter1"),
        device_version=_env("DEVICE_VERSION", "3.0.1-0002"),
        device_serial=_env("DEVICE_SERIAL", "00000002"),
        entity_map_path=_env("ENTITY_MAP_PATH") or None,
        inline_entity_map=None,
        modbus_run_backend=_env_bool("MODBUS_RUN_BACKEND", True),
        modbus_backend_host=_env("MODBUS_BACKEND_HOST", "127.0.0.1"),
        modbus_backend_port=int(_env("MODBUS_BACKEND_PORT", _env("MODBUS_BIND_PORT", "1502"))),
        modbus_raw_relay=_env_bool("MODBUS_RAW_RELAY", False),
        modbus_enable_proxy=_env_bool("MODBUS_ENABLE_PROXY", True),
        modbus_log_reads=_env_bool("MODBUS_LOG_READS", True),
        modbus_gate_enable=_env_bool("MODBUS_GATE_ENABLE", True),
    )


def load_meters_from_file(path_value: str) -> tuple[MeterConfig, ...]:
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Meters file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    meters_raw = data.get("meters") if isinstance(data.get("meters"), list) else []
    meters = [_meter_from_dict(item, defaults, base_dir=path.parent) for item in meters_raw if isinstance(item, dict)]
    meters = [meter for meter in meters if meter.enabled]
    if not meters:
        raise ValueError("No enabled meters defined in meters file")
    _validate_meter_ports(meters)
    return tuple(meters)


def load_config() -> AppConfig:
    mode = _env("HA_MODE", "direct").lower()
    if mode not in {"direct", "supervisor"}:
        raise ValueError("HA_MODE must be 'direct' or 'supervisor'")

    if mode == "supervisor":
        base_url = "http://supervisor/core/api"
        ws_url = "ws://supervisor/core/websocket"
        token = _env("SUPERVISOR_TOKEN") or _env("HA_TOKEN")
    else:
        base_url = _normalize_ha_base_url(_env("HA_BASE_URL", "http://homeassistant:8123"))
        ws_url = _env("HA_WS_URL") or _derive_ws_url(base_url)
        token = _env("HA_TOKEN")

    meters_file = _env("METERS_FILE") or None
    if meters_file:
        meters = load_meters_from_file(meters_file)
    else:
        meters = (_default_single_meter(),)
        _validate_meter_ports(list(meters))

    return AppConfig(
        mode=mode,
        ha_base_url=base_url.rstrip('/'),
        ha_token=token,
        ws_url=ws_url,
        status_host=_env("STATUS_HOST", "0.0.0.0"),
        status_port=int(_env("STATUS_PORT", "8080")),
        resync_interval_s=int(_env("RESYNC_INTERVAL_S", "300")),
        config_reload_interval_s=int(_env("CONFIG_RELOAD_INTERVAL_S", "3")),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        meters_file=meters_file,
        meters=meters,
    )
