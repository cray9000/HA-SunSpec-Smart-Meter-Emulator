from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from pymodbus.datastore import ModbusSequentialDataBlock

from .config import MeterConfig
from .entity_map import EntityBinding
from .sunspec import (
    Encoding,
    REGISTER_BY_KEY,
    REGISTER_SPECS,
    REG_BLOCK_START,
    REG_COMMON_DA,
    REG_COMMON_MODEL_ID,
    REG_COMMON_MODEL_LENGTH,
    REG_END_BLOCK_ID,
    REG_END_BLOCK_LENGTH,
    REG_MODEL_ID,
    REG_MODEL_LENGTH,
    SUNSPEC_MODEL_AC_METER_INT_SF,
    SUNSPEC_MODEL_LENGTH_INT_SF,
    all_scale_factor_registers,
    choose_group_scale_factor,
    encode_acc32,
    encode_s16,
    pack_ascii_to_registers,
)

_LOGGER = logging.getLogger(__name__)
PYMODBUS_CALLBACK_ADDRESS_SHIFT = 1


def _canonical_unit_for_key(logical_key: str) -> str | None:
    if logical_key.startswith("power_"):
        return "W"
    if logical_key.startswith("apparent_"):
        return "VA"
    if logical_key.startswith("reactive_"):
        return "var"
    if logical_key.startswith("current_"):
        return "A"
    if logical_key.startswith("voltage_"):
        return "V"
    if logical_key.startswith("energy_"):
        return "Wh"
    if logical_key == "frequency":
        return "Hz"
    if logical_key.startswith("pf_"):
        return None
    return None


@dataclass
class StoreStats:
    modbus_read_requests: int = 0
    modbus_read_registers: int = 0
    modbus_write_requests: int = 0
    internal_register_updates: int = 0
    ha_updates: int = 0
    initial_syncs: int = 0
    websocket_reconnects: int = 0
    modbus_alias_reads: int = 0
    modbus_invalid_reads: int = 0
    modbus_gate_transitions: int = 0
    config_reloads: int = 0
    last_modbus_read_monotonic: float = 0.0
    last_ha_update_monotonic: float = 0.0
    last_reload_monotonic: float = 0.0
    last_invalid_read_address: int | None = None
    last_invalid_read_count: int | None = None
    last_alias_offset: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)


class StatsDataBlock(ModbusSequentialDataBlock):
    def __init__(self, address: int, values: list[int], stats: StoreStats, read_logger=None, log_reads: bool = True):
        super().__init__(address, values)
        self.stats = stats
        self._start = address
        self._end = address + len(values) - 1
        self._last_invalid: tuple[int, int] | None = None
        self._read_logger = read_logger
        self._log_reads = log_reads

    def _normalize_address(self, address: int, count: int) -> tuple[int | None, int]:
        logical = address - PYMODBUS_CALLBACK_ADDRESS_SHIFT
        end = logical + count - 1
        if logical >= self._start and end <= self._end:
            return logical, -PYMODBUS_CALLBACK_ADDRESS_SHIFT
        return None, -PYMODBUS_CALLBACK_ADDRESS_SHIFT

    def validate(self, address: int, count: int = 1):  # noqa: N802 - pymodbus API
        logical, offset = self._normalize_address(address, count)
        valid = logical is not None
        if not valid:
            requested = address + offset
            self.stats.modbus_invalid_reads += 1
            self.stats.last_invalid_read_address = requested
            self.stats.last_invalid_read_count = count
            marker = (requested, count)
            if marker != self._last_invalid:
                self._last_invalid = marker
                _LOGGER.warning(
                    "Illegal holding-register read: start=0x%04X count=%s outside 0x%04X..0x%04X",
                    requested,
                    count,
                    self._start,
                    self._end,
                )
        return valid

    def getValues(self, address: int, count: int = 1):  # noqa: N802 - pymodbus API
        logical, offset = self._normalize_address(address, count)
        if logical is None:
            return super().getValues(address, count)
        values = super().getValues(logical, count)
        self.stats.modbus_read_requests += 1
        self.stats.modbus_read_registers += count
        self.stats.last_modbus_read_monotonic = time.monotonic()
        if offset != 0:
            self.stats.modbus_alias_reads += 1
            self.stats.last_alias_offset = offset
        if self._read_logger is not None:
            self._read_logger(address=address + offset, translated=logical, count=count, values=list(values), offset=offset, log_reads=self._log_reads)
        return values

    def setValues(self, address: int, values):  # noqa: N802 - pymodbus API
        logical, _offset = self._normalize_address(address, len(values))
        self.stats.modbus_write_requests += 1
        return super().setValues(address if logical is None else logical, values)

    def setValuesInternal(self, address: int, values):  # noqa: N802 - helper for internal state updates
        self.stats.internal_register_updates += 1
        return super().setValues(address, values)


class RegisterStore:
    def __init__(
        self,
        *,
        meter_id: str,
        public_port: int,
        unit_id: int,
        manufacturer: str,
        model: str,
        device_name: str,
        version: str,
        serial: str,
        entity_bindings: dict[str, EntityBinding],
        modbus_log_reads: bool = True,
        modbus_gate_enable: bool = True,
    ) -> None:
        self._lock = asyncio.Lock()
        self.stats = StoreStats()
        self.meter_id = meter_id
        self.public_port = public_port
        self.identity = {
            "device_modbus_address": unit_id,
            "manufacturer": manufacturer,
            "model": model,
            "device_name": device_name,
            "version": version,
            "serial": serial,
        }
        self.entity_bindings: dict[str, EntityBinding] = {}
        self.entity_lookup: dict[str, str] = {}
        self.latest_values: dict[str, float | None] = {}
        self.last_entity_update_ts: dict[str, float] = {}
        self.raw_states: dict[str, str | None] = {}
        self.source_entity_units: dict[str, str | None] = {}
        self.entity_units: dict[str, str | None] = {}
        self.entity_friendly_names: dict[str, str | None] = {}
        self.recent_reads = deque(maxlen=100)
        self._modbus_log_reads = modbus_log_reads
        self._modbus_gate_enable = modbus_gate_enable
        self._modbus_gate_blocked = bool(modbus_gate_enable)
        self._modbus_gate_event = asyncio.Event()
        if not self._modbus_gate_blocked:
            self._modbus_gate_event.set()

        values = [0] * (REG_END_BLOCK_LENGTH - REG_BLOCK_START + 1)
        self.block = StatsDataBlock(REG_BLOCK_START, values, self.stats, read_logger=self._record_read, log_reads=modbus_log_reads)
        self._apply_entity_bindings(entity_bindings)
        self._clear_register_range()
        self._initialize_static_registers(
            unit_id=unit_id,
            manufacturer=manufacturer,
            model=model,
            device_name=device_name,
            version=version,
            serial=serial,
        )
        self._rebuild_derived_registers_nolock()

    def _apply_entity_bindings(self, entity_bindings: dict[str, EntityBinding]) -> None:
        prev_values = getattr(self, "latest_values", {})
        prev_raw = getattr(self, "raw_states", {})
        prev_units = getattr(self, "source_entity_units", {})
        prev_friendly = getattr(self, "entity_friendly_names", {})
        self.entity_bindings = dict(entity_bindings)
        self.entity_lookup = {binding.entity_id: logical_key for logical_key, binding in entity_bindings.items()}
        self.latest_values = {logical_key: prev_values.get(logical_key) for logical_key in entity_bindings}
        self.raw_states = {logical_key: prev_raw.get(logical_key) for logical_key in entity_bindings}
        self.source_entity_units = {logical_key: prev_units.get(logical_key) for logical_key in entity_bindings}
        self.entity_units = {logical_key: _canonical_unit_for_key(logical_key) for logical_key in entity_bindings}
        self.entity_friendly_names = {logical_key: prev_friendly.get(logical_key) for logical_key in entity_bindings}

    def _record_read(self, *, address: int, translated: int, count: int, values: list[int], offset: int, log_reads: bool) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "address": address,
            "translated": translated,
            "count": count,
            "offset": offset,
            "values": values,
            "values_hex": [f"0x{v:04X}" for v in values],
        }
        self.recent_reads.appendleft(entry)
        if log_reads:
            _LOGGER.info(
                "Modbus read start=0x%04X translated=0x%04X logical~=0x%04X count=%s values=%s",
                address,
                translated,
                translated,
                count,
                " ".join(entry["values_hex"]),
            )

    def _set_reg(self, register: int, value: int) -> None:
        self.block.setValuesInternal(register, [value & 0xFFFF])

    def _clear_register_range(self) -> None:
        for register in range(REG_BLOCK_START, REG_END_BLOCK_LENGTH + 1):
            self._set_reg(register, 0)

    def _initialize_static_registers(
        self,
        *,
        unit_id: int,
        manufacturer: str,
        model: str,
        device_name: str,
        version: str,
        serial: str,
    ) -> None:
        self._set_reg(REG_BLOCK_START, 0x5375)
        self._set_reg(REG_BLOCK_START + 1, 0x6E53)
        self._set_reg(REG_COMMON_MODEL_ID, 0x0001)
        self._set_reg(REG_COMMON_MODEL_LENGTH, 0x0041)

        cursor = REG_BLOCK_START + 4
        for text, byte_len in (
            (manufacturer, 32),
            (model, 32),
            (device_name, 16),
            (version, 16),
            (serial, 32),
        ):
            for reg in pack_ascii_to_registers(text, byte_len):
                self._set_reg(cursor, reg)
                cursor += 1

        self._set_reg(REG_COMMON_DA, unit_id)
        self._set_reg(REG_MODEL_ID, SUNSPEC_MODEL_AC_METER_INT_SF)
        self._set_reg(REG_MODEL_LENGTH, SUNSPEC_MODEL_LENGTH_INT_SF)

        for register, value in all_scale_factor_registers():
            self._set_reg(register, value)

        self._set_reg(REG_END_BLOCK_ID, 0xFFFF)
        self._set_reg(REG_END_BLOCK_LENGTH, 0x0000)

    def _gate_snapshot_nolock(self) -> tuple[float | None, float | None]:
        return self.latest_values.get("energy_import_total"), self.latest_values.get("energy_export_total")

    def _should_block_modbus_nolock(self) -> bool:
        if not self._modbus_gate_enable:
            return False
        import_total, export_total = self._gate_snapshot_nolock()
        return not (((import_total or 0.0) > 0.0) or ((export_total or 0.0) > 0.0))

    def _update_modbus_gate_state_nolock(self) -> None:
        blocked = self._should_block_modbus_nolock()
        if blocked == self._modbus_gate_blocked:
            return
        self._modbus_gate_blocked = blocked
        self.stats.modbus_gate_transitions += 1
        import_total, export_total = self._gate_snapshot_nolock()
        if blocked:
            self._modbus_gate_event.clear()
            _LOGGER.info(
                "%s: Modbus gate closed: valid import/export missing or <=0 (import=%s export=%s)",
                self.meter_id,
                import_total,
                export_total,
            )
        else:
            self._modbus_gate_event.set()
            _LOGGER.info(
                "%s: Modbus gate opened: valid import/export available (import=%s export=%s)",
                self.meter_id,
                import_total,
                export_total,
            )

    async def wait_until_modbus_ready(self) -> None:
        await self._modbus_gate_event.wait()

    def is_modbus_blocked(self) -> bool:
        return self._modbus_gate_blocked

    async def apply_initial_states(self, states: list[dict]) -> None:
        changed = 0
        async with self._lock:
            for item in states:
                entity_id = item.get("entity_id")
                if entity_id not in self.entity_lookup:
                    continue
                if self._update_from_state_obj_nolock(item):
                    changed += 1
            self._rebuild_derived_registers_nolock()
            self._update_modbus_gate_state_nolock()
            self.stats.initial_syncs += 1
        _LOGGER.info("%s: Initial sync applied: %s entity values updated", self.meter_id, changed)

    async def apply_state_change(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        if entity_id not in self.entity_lookup:
            return
        async with self._lock:
            payload = {"entity_id": entity_id}
            if isinstance(new_state, dict):
                payload.update(new_state)
            changed = self._update_from_state_obj_nolock(payload)
            if changed:
                self._rebuild_derived_registers_nolock()
                self._update_modbus_gate_state_nolock()

    async def reload_meter_config(self, meter: MeterConfig, entity_bindings: dict[str, EntityBinding]) -> None:
        async with self._lock:
            self.meter_id = meter.meter_id
            self.public_port = meter.public_port
            self.identity = {
                "device_modbus_address": meter.device_modbus_address,
                "manufacturer": meter.device_manufacturer,
                "model": meter.device_model,
                "device_name": meter.device_name,
                "version": meter.device_version,
                "serial": meter.device_serial,
            }
            self._modbus_log_reads = meter.modbus_log_reads
            self._modbus_gate_enable = meter.modbus_gate_enable
            self.block._log_reads = meter.modbus_log_reads
            self._apply_entity_bindings(entity_bindings)
            self._clear_register_range()
            self._initialize_static_registers(
                unit_id=meter.device_modbus_address,
                manufacturer=meter.device_manufacturer,
                model=meter.device_model,
                device_name=meter.device_name,
                version=meter.device_version,
                serial=meter.device_serial,
            )
            self._rebuild_derived_registers_nolock()
            self._update_modbus_gate_state_nolock()
            self.stats.config_reloads += 1
            self.stats.last_reload_monotonic = time.monotonic()
        _LOGGER.info("%s: Meter configuration reloaded", meter.meter_id)

    def _update_from_state_obj_nolock(self, state_obj: dict[str, Any]) -> bool:
        entity_id = state_obj.get("entity_id")
        if entity_id not in self.entity_lookup:
            return False
        logical_key = self.entity_lookup[entity_id]
        binding = self.entity_bindings[logical_key]
        state_value = state_obj.get("state")
        attributes = state_obj.get("attributes") if isinstance(state_obj.get("attributes"), dict) else {}
        parsed = _parse_float_state(state_value)
        engineering_value = None if parsed is None else parsed * binding.unit_multiplier
        previous = self.latest_values.get(logical_key)

        self.raw_states[logical_key] = None if state_value is None else str(state_value)
        self.source_entity_units[logical_key] = str(attributes.get("unit_of_measurement")) if attributes.get("unit_of_measurement") is not None else self.source_entity_units.get(logical_key)
        self.entity_units[logical_key] = _canonical_unit_for_key(logical_key)
        self.entity_friendly_names[logical_key] = str(attributes.get("friendly_name")) if attributes.get("friendly_name") is not None else self.entity_friendly_names.get(logical_key)

        if _same_numeric(previous, engineering_value):
            return False

        self.latest_values[logical_key] = engineering_value
        self.last_entity_update_ts[entity_id] = time.time()
        self.stats.ha_updates += 1
        self.stats.last_ha_update_monotonic = time.monotonic()
        return True

    def _write_logical_value_nolock(self, logical_key: str, raw_value: float, sf: int) -> None:
        spec = REGISTER_BY_KEY[logical_key]
        if spec.encoding == Encoding.S16:
            self._set_reg(spec.register, encode_s16(raw_value, sf))
        else:
            hi, lo = encode_acc32(raw_value, sf)
            self._set_reg(spec.register, hi)
            self._set_reg(spec.register + 1, lo)

    def _rebuild_derived_registers_nolock(self) -> None:
        derived: dict[str, float | None] = {
            "current_total": _sum_if_any(
                self.latest_values.get("current_l1"),
                self.latest_values.get("current_l2"),
                self.latest_values.get("current_l3"),
            ),
            "reactive_total": _sum_if_any(
                self.latest_values.get("reactive_l1"),
                self.latest_values.get("reactive_l2"),
                self.latest_values.get("reactive_l3"),
            ),
        }
        for logical_key, value in derived.items():
            if value is not None:
                self.latest_values[logical_key] = value

        groups = {}
        for spec in REGISTER_SPECS:
            groups.setdefault(spec.group, []).append(spec)

        for group, specs in groups.items():
            s16_values = [self.latest_values.get(spec.logical_key) for spec in specs if spec.encoding == Encoding.S16]
            sf = choose_group_scale_factor(group, s16_values)
            sf_register = {
                "A": 0x9C8B,
                "V": 0x9C94,
                "HZ": 0x9C96,
                "W": 0x9C9B,
                "VA": 0x9CA0,
                "VAR": 0x9CA5,
                "PF": 0x9CAA,
                "WH": 0x9CBB,
            }.get(group.value)
            if sf_register is not None:
                self._set_reg(sf_register, sf & 0xFFFF)
            for spec in specs:
                value = self.latest_values.get(spec.logical_key)
                if value is None:
                    if spec.encoding == Encoding.S16:
                        self._set_reg(spec.register, 0)
                    else:
                        self._set_reg(spec.register, 0)
                        self._set_reg(spec.register + 1, 0)
                    continue
                effective_sf = sf if spec.encoding == Encoding.S16 else 0
                self._write_logical_value_nolock(spec.logical_key, float(value), effective_sf)

    def _read_register(self, address: int) -> int:
        return int(self.block.getValues(address + PYMODBUS_CALLBACK_ADDRESS_SHIFT, 1)[0])

    def register_dump(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        groups = {}
        for spec in REGISTER_SPECS:
            groups.setdefault(spec.group, []).append(spec)
        group_sfs = {
            group: choose_group_scale_factor(group, [self.latest_values.get(spec.logical_key) for spec in specs if spec.encoding == Encoding.S16])
            for group, specs in groups.items()
        }
        for spec in REGISTER_SPECS:
            sf = group_sfs.get(spec.group, 0)
            if spec.encoding == Encoding.S16:
                words = [self._read_register(spec.register)]
            else:
                words = [self._read_register(spec.register), self._read_register(spec.register + 1)]
                sf = 0
            entries.append(
                {
                    "logical_key": spec.logical_key,
                    "address": spec.register,
                    "address_hex": f"0x{spec.register:04X}",
                    "encoding": spec.encoding.value,
                    "sf": sf,
                    "words": words,
                    "words_hex": [f"0x{w:04X}" for w in words],
                    "ha_raw_state": self.raw_states.get(spec.logical_key),
                    "ha_unit": self.entity_units.get(spec.logical_key),
                    "source_ha_unit": self.source_entity_units.get(spec.logical_key),
                    "converted_value": self.latest_values.get(spec.logical_key),
                    "friendly_name": self.entity_friendly_names.get(spec.logical_key),
                }
            )
        return {
            "meter_id": self.meter_id,
            "public_port": self.public_port,
            "range_start": 0x9C40,
            "range_end": 0x9CF1,
            "registers": entries,
            "recent_reads": list(self.recent_reads),
        }

    def status_payload(self) -> dict:
        last_modbus_age = None
        if self.stats.last_modbus_read_monotonic:
            last_modbus_age = round(time.monotonic() - self.stats.last_modbus_read_monotonic, 3)

        last_ha_age = None
        if self.stats.last_ha_update_monotonic:
            last_ha_age = round(time.monotonic() - self.stats.last_ha_update_monotonic, 3)

        last_reload_age = None
        if self.stats.last_reload_monotonic:
            last_reload_age = round(time.monotonic() - self.stats.last_reload_monotonic, 3)

        return {
            "meter_id": self.meter_id,
            "public_port": self.public_port,
            "device": dict(self.identity),
            "uptime_s": round(time.monotonic() - self.stats.started_monotonic, 1),
            "modbus_read_requests": self.stats.modbus_read_requests,
            "modbus_read_registers": self.stats.modbus_read_registers,
            "modbus_write_requests": self.stats.modbus_write_requests,
            "internal_register_updates": self.stats.internal_register_updates,
            "ha_updates": self.stats.ha_updates,
            "initial_syncs": self.stats.initial_syncs,
            "websocket_reconnects": self.stats.websocket_reconnects,
            "modbus_alias_reads": self.stats.modbus_alias_reads,
            "modbus_invalid_reads": self.stats.modbus_invalid_reads,
            "modbus_gate_enabled": self._modbus_gate_enable,
            "modbus_gate_blocked": self._modbus_gate_blocked,
            "modbus_gate_transitions": self.stats.modbus_gate_transitions,
            "config_reloads": self.stats.config_reloads,
            "last_invalid_read_address": self.stats.last_invalid_read_address,
            "last_invalid_read_count": self.stats.last_invalid_read_count,
            "last_alias_offset": self.stats.last_alias_offset,
            "last_modbus_read_age_s": last_modbus_age,
            "last_ha_update_age_s": last_ha_age,
            "last_reload_age_s": last_reload_age,
            "entities": {
                logical_key: {
                    "entity_id": binding.entity_id,
                    "raw_state": self.raw_states.get(logical_key),
                    "ha_unit": self.entity_units.get(logical_key),
                    "source_ha_unit": self.source_entity_units.get(logical_key),
                    "friendly_name": self.entity_friendly_names.get(logical_key),
                    "multiplier": binding.unit_multiplier,
                    "converted_value": self.latest_values.get(logical_key),
                    "register": f"0x{REGISTER_BY_KEY[logical_key].register:04X}" if logical_key in REGISTER_BY_KEY else None,
                }
                for logical_key, binding in self.entity_bindings.items()
            },
            "recent_reads": list(self.recent_reads)[:20],
        }

    def debug_dump(self) -> dict[str, Any]:
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": self.status_payload(),
            "register_dump": self.register_dump(),
            "stats": asdict(self.stats),
        }

    def debug_dump_json(self) -> str:
        return json.dumps(self.debug_dump(), indent=2, ensure_ascii=False)


def _parse_float_state(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "unavailable", "none", "nan"}:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _sum_if_any(*values: float | None) -> float | None:
    actual = [value for value in values if value is not None]
    if not actual:
        return None
    return float(sum(actual))


def _same_numeric(left: float | None, right: float | None) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(left - right) < 1e-9
