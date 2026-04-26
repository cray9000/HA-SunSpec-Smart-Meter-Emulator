# HA SunSpec Smart Meter Emulator

This add-on exposes up to 6 Home Assistant sensor sets as SunSpec-compatible Modbus TCP smart meters.

## Public Modbus ports

Each enabled meter uses one fixed TCP port:

- meter1 -> 502
- meter2 -> 1502
- meter3 -> 2502
- meter4 -> 3502
- meter5 -> 4502
- meter6 -> 5502

## Persistent configuration files

On first start the add-on copies default files into `/data`:

- `/data/meters.yaml`
- `/data/entity_map_sm1.yaml`
- `/data/entity_map_sm2.yaml`
- `/data/entity_map_sm3.yaml`
- `/data/entity_map_sm4.yaml`
- `/data/entity_map_sm5.yaml`
- `/data/entity_map_sm6.yaml`

Edit those files to assign Home Assistant entities to each emulated smart meter.

## Example `meters.yaml`

```yaml
defaults:
  modbus_host: 0.0.0.0
  modbus_enable_proxy: true
  modbus_gate_enable: true
  modbus_log_reads: false
  alias_unit_ids: [1]
  device_manufacturer: Fronius
  device_model: Smart Meter 63A
  device_version: "3.0.1-0002"

meters:
  - id: sm1
    enabled: true
    port: 502
    unit_id: 3
    device_modbus_address: 241
    device_name: "meter1"
    device_serial: "00000002"
    entity_map_path: entity_map_sm1.yaml

  - id: sm2
    enabled: true
    port: 1502
    unit_id: 3
    device_modbus_address: 242
    device_name: "meter2"
    device_serial: "00000003"
    entity_map_path: entity_map_sm2.yaml
```

## Reload behavior

Phase 2 supports automatic reload when `/data/meters.yaml` or one of the referenced entity-map files changes.

You can also reload manually with:

- `POST /reload`

A restart is still required when you change:

- meter IDs
- public ports
- the number of active listeners

Changes to meter identity, unit ID, device address, gate settings, logging flags, and entity maps reload automatically.

## Web UI / diagnostics

The ingress UI and status server run on port `8080`.

Useful endpoints:

- `/` -> overview of all meters
- `/meter/sm1` -> live page for one meter
- `/status` -> JSON status for all meters
- `/status?meter=sm1` -> JSON status for one meter
- `/registers?meter=sm1` -> register dump for one meter
- `/dump?meter=sm1` -> debug dump for one meter
- `/dump/all.zip` -> ZIP containing dumps for all meters

## Home Assistant connection

In add-on mode the application uses the Home Assistant Supervisor API.
No manual base URL or token is required in the add-on configuration.

## Notes

- The add-on emulates SunSpec-compatible smart meters. It does not write values back into Home Assistant.
- Per-meter ports are fixed by design for predictable Fronius configuration.
- Entity values should use the following units on the Home Assistant side: currents in A, voltages in V, frequency in Hz, power in W, apparent power in VA, reactive power in var, energy in kWh.

## Repository

GitHub: `https://github.com/cray9000/HA-SunSpec-Smart-Meter-Emulator`

## License

This project is licensed under the MIT License.

> Do not commit a real `.env` file. Keep secrets local and outside Git. In Home Assistant Add-on mode no `HA_TOKEN` file is required because the add-on uses the Supervisor API.
