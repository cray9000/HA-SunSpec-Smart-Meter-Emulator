from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartAsyncTcpServer

from .config import AppConfig, MeterConfig, load_config, load_meters_from_file
from .entity_map import load_entity_map, merge_entity_map_data
from .ha_client import HomeAssistantBridge
from .modbus_proxy import run_modbus_proxy
from .register_store import RegisterStore
from .status_server import run_status_server

_LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_modbus_context(store: RegisterStore, primary_unit_id: int) -> ModbusServerContext:
    slave = ModbusSlaveContext(hr=store.block, ir=store.block)
    _LOGGER.info(
        "Configured single-device Modbus context for %s (accepting any TCP Unit-ID; primary hint=%s)",
        store.meter_id,
        primary_unit_id,
    )
    return ModbusServerContext(slaves=slave, single=True)


async def run_modbus_server(store: RegisterStore, host: str, port: int, unit_id: int, meter_id: str) -> None:
    context = build_modbus_context(store, unit_id)
    _LOGGER.info("Starting Modbus backend server for %s on %s:%s (unit hint %s)", meter_id, host, port, unit_id)
    await StartAsyncTcpServer(context=context, address=(host, port))


async def start_meter_runtime(meter: MeterConfig, store: RegisterStore) -> None:
    tasks: list[asyncio.Future] = []
    if meter.modbus_run_backend:
        tasks.append(run_modbus_server(store, meter.modbus_host, meter.backend_bind_port, meter.unit_id, meter.meter_id))

    enable_proxy = meter.modbus_enable_proxy or meter.modbus_raw_relay or bool(meter.alias_unit_ids) or not meter.modbus_run_backend
    if enable_proxy:
        backend_port = meter.modbus_backend_port
        if meter.modbus_run_backend and meter.modbus_backend_host in {"127.0.0.1", "localhost", "0.0.0.0"} and meter.modbus_backend_port == meter.backend_bind_port:
            backend_port = meter.backend_bind_port
        tasks.append(
            run_modbus_proxy(
                meter.modbus_host,
                meter.public_port,
                meter.modbus_backend_host,
                backend_port,
                meter.unit_id,
                meter.alias_unit_ids,
                raw_relay=meter.modbus_raw_relay,
                is_gate_blocked=store.is_modbus_blocked if meter.modbus_gate_enable else None,
                wait_until_gate_open=store.wait_until_modbus_ready if meter.modbus_gate_enable else None,
            )
        )
    elif not meter.modbus_run_backend:
        raise RuntimeError(f"{meter.meter_id}: MODBUS_RUN_BACKEND=false requires proxy mode")

    await asyncio.gather(*tasks)


def _build_store(meter: MeterConfig) -> RegisterStore:
    entity_bindings = load_entity_map(meter.entity_map_path)
    if meter.inline_entity_map:
        entity_bindings = merge_entity_map_data(meter.inline_entity_map, base_map=entity_bindings)
    return RegisterStore(
        meter_id=meter.meter_id,
        public_port=meter.public_port,
        unit_id=meter.device_modbus_address,
        manufacturer=meter.device_manufacturer,
        model=meter.device_model,
        device_name=meter.device_name,
        version=meter.device_version,
        serial=meter.device_serial,
        entity_bindings=entity_bindings,
        modbus_log_reads=meter.modbus_log_reads,
        modbus_gate_enable=meter.modbus_gate_enable,
    )


class RuntimeManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stores_by_meter: dict[str, RegisterStore] = {meter.meter_id: _build_store(meter) for meter in config.meters}
        self.meters_by_id: dict[str, MeterConfig] = {meter.meter_id: meter for meter in config.meters}
        self.bridge = HomeAssistantBridge(config, list(self.stores_by_meter.values()))
        self._reload_lock = asyncio.Lock()
        self._last_reload_message = "not reloaded yet"
        self._last_reload_ok = True
        self._last_reload_ts: float | None = None

    def reload_state(self) -> dict[str, object]:
        return {
            "last_reload_ok": self._last_reload_ok,
            "last_reload_message": self._last_reload_message,
            "last_reload_ts": self._last_reload_ts,
            "meters_file": self.config.meters_file,
            "config_reload_interval_s": self.config.config_reload_interval_s,
        }

    async def reload_from_files(self) -> dict[str, object]:
        async with self._reload_lock:
            try:
                if not self.config.meters_file:
                    raise RuntimeError("Reload is only available when METERS_FILE is configured")
                new_meters = load_meters_from_file(self.config.meters_file)
                new_ids = {m.meter_id for m in new_meters}
                old_ids = set(self.stores_by_meter)
                if new_ids != old_ids:
                    raise RuntimeError(f"Reload would change meter ids. Existing={sorted(old_ids)} New={sorted(new_ids)}. Restart required.")
                old_ports = {m.meter_id: m.public_port for m in self.meters_by_id.values()}
                new_ports = {m.meter_id: m.public_port for m in new_meters}
                if old_ports != new_ports:
                    raise RuntimeError(f"Reload would change public ports from {old_ports} to {new_ports}. Restart required.")
                for meter in new_meters:
                    bindings = load_entity_map(meter.entity_map_path)
                    if meter.inline_entity_map:
                        bindings = merge_entity_map_data(meter.inline_entity_map, base_map=bindings)
                    await self.stores_by_meter[meter.meter_id].reload_meter_config(meter, bindings)
                    self.meters_by_id[meter.meter_id] = meter
                self.bridge.rebuild_entity_targets()
                await self.bridge.force_resync()
                self._last_reload_ok = True
                self._last_reload_message = f"reloaded {len(new_meters)} meter(s) successfully"
                self._last_reload_ts = asyncio.get_running_loop().time()
                _LOGGER.info("Configuration reload complete")
            except Exception as exc:  # noqa: BLE001
                self._last_reload_ok = False
                self._last_reload_message = str(exc)
                self._last_reload_ts = asyncio.get_running_loop().time()
                _LOGGER.warning("Configuration reload failed: %s", exc)
                return {"ok": False, "message": str(exc)}
            return {"ok": True, "message": self._last_reload_message}

    def _watched_files(self) -> list[Path]:
        files: list[Path] = []
        if self.config.meters_file:
            files.append(Path(self.config.meters_file))
        for meter in self.meters_by_id.values():
            if meter.entity_map_path:
                files.append(Path(meter.entity_map_path))
        # preserve order while removing duplicates
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in files:
            if path in seen:
                continue
            seen.add(path)
            unique.append(path)
        return unique

    async def watch_config_file(self) -> None:
        if not self.config.meters_file:
            return
        mtimes = {path: (path.stat().st_mtime if path.exists() else None) for path in self._watched_files()}
        while True:
            await asyncio.sleep(max(1, self.config.config_reload_interval_s))
            changed = False
            current_paths = self._watched_files()
            current_mtimes: dict[Path, float | None] = {}
            for path in current_paths:
                try:
                    current_mtimes[path] = path.stat().st_mtime if path.exists() else None
                except FileNotFoundError:
                    current_mtimes[path] = None
                if current_mtimes[path] != mtimes.get(path):
                    changed = True
            if not changed and set(current_paths) == set(mtimes):
                continue
            mtimes = current_mtimes
            await self.reload_from_files()


async def main() -> None:
    config = load_config()
    configure_logging(config.log_level)
    runtime = RuntimeManager(config)

    tasks = [
        run_status_server(config.status_host, config.status_port, runtime.stores_by_meter, reload_callback=runtime.reload_from_files, reload_state_provider=runtime.reload_state),
        runtime.bridge.run_forever(),
        runtime.watch_config_file(),
    ]
    for meter in config.meters:
        tasks.append(start_meter_runtime(meter, runtime.stores_by_meter[meter.meter_id]))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
