from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class EntityBinding:
    entity_id: str
    unit_multiplier: float = 1.0


DEFAULT_ENTITY_MAP: dict[str, EntityBinding] = {
    "current_l1": EntityBinding("sensor.meter_current_l1", 1.0),
    "current_l2": EntityBinding("sensor.meter_current_l2", 1.0),
    "current_l3": EntityBinding("sensor.meter_current_l3", 1.0),
    "voltage_l1_n": EntityBinding("sensor.meter_voltage_l1_n", 1.0),
    "voltage_l2_n": EntityBinding("sensor.meter_voltage_l2_n", 1.0),
    "voltage_l3_n": EntityBinding("sensor.meter_voltage_l3_n", 1.0),
    "frequency": EntityBinding("sensor.meter_frequency", 1.0),
    "power_l1": EntityBinding("sensor.meter_power_l1", 1.0),
    "power_l2": EntityBinding("sensor.meter_power_l2", 1.0),
    "power_l3": EntityBinding("sensor.meter_power_l3", 1.0),
    "power_total": EntityBinding("sensor.meter_power_total", 1.0),
    "energy_export_total": EntityBinding("sensor.meter_energy_export_total", 1000.0),
    "energy_import_total": EntityBinding("sensor.meter_energy_import_total", 1000.0),
    "apparent_l1": EntityBinding("sensor.meter_apparent_l1", 1.0),
    "apparent_l2": EntityBinding("sensor.meter_apparent_l2", 1.0),
    "apparent_l3": EntityBinding("sensor.meter_apparent_l3", 1.0),
    "apparent_total": EntityBinding("sensor.meter_apparent_total", 1.0),
    "reactive_l1": EntityBinding("sensor.meter_reactive_l1", 1.0),
    "reactive_l2": EntityBinding("sensor.meter_reactive_l2", 1.0),
    "reactive_l3": EntityBinding("sensor.meter_reactive_l3", 1.0),
    "pf_total": EntityBinding("sensor.meter_pf_total", 1.0),
    "pf_l1": EntityBinding("sensor.meter_pf_l1", 1.0),
    "pf_l2": EntityBinding("sensor.meter_pf_l2", 1.0),
    "pf_l3": EntityBinding("sensor.meter_pf_l3", 1.0),
}


def merge_entity_map_data(data: dict[str, Any] | None, *, base_map: dict[str, EntityBinding] | None = None) -> dict[str, EntityBinding]:
    merged = (base_map or DEFAULT_ENTITY_MAP).copy()
    if not data:
        return merged

    for logical_key, value in data.items():
        if isinstance(value, str):
            merged[logical_key] = EntityBinding(value, merged.get(logical_key, EntityBinding(value)).unit_multiplier)
            continue

        if isinstance(value, dict):
            entity_id = str(value.get("entity_id", merged.get(logical_key, EntityBinding("")).entity_id)).strip()
            if not entity_id:
                continue
            multiplier_raw: Any = value.get("unit_multiplier", merged.get(logical_key, EntityBinding(entity_id)).unit_multiplier)
            multiplier = float(multiplier_raw)
            merged[logical_key] = EntityBinding(entity_id, multiplier)

    return merged


def load_entity_map(path: str | None) -> dict[str, EntityBinding]:
    if path is None:
        return DEFAULT_ENTITY_MAP.copy()

    cfg_path = Path(path)
    if not cfg_path.exists():
        return DEFAULT_ENTITY_MAP.copy()

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return merge_entity_map_data(data)
