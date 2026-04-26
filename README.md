# HA SunSpec Smart Meter Emulator

Home Assistant add-on and Docker app for exposing Home Assistant sensor data as one or more SunSpec-compatible Modbus TCP smart meters.

## What it does

- reads selected Home Assistant entity states directly from Home Assistant
- keeps an internal SunSpec-compatible register mirror
- exposes one or more Modbus TCP smart meters
- exposes a small status and diagnostics endpoint on port 8080

## Data source strategy

The application connects directly to Home Assistant and:

1. loads an initial snapshot from `/api/states`
2. subscribes to `state_changed` events over the Home Assistant WebSocket API
3. periodically re-syncs the full state snapshot as a safety net

## Quick start with Docker Compose

When testing outside Home Assistant Add-on mode, set `HA_BASE_URL` to the real reachable Home Assistant URL, for example `http://192.168.1.20:8123`. The placeholder `http://homeassistant:8123` only makes sense when that hostname is resolvable in your Docker network.

```bash
cp .env.example .env
# edit .env and set HA_BASE_URL + HA_TOKEN
cp config/entity_map_sm1.yaml config/entity_map_sm1.local.yaml
# optionally edit config/entity_map_sm1.local.yaml

docker compose up --build -d
```

After startup:

- Modbus TCP: `HOST_IP:502` and optional additional fixed meter ports
- Health: `http://HOST_IP:8080/healthz`
- Status: `http://HOST_IP:8080/status`

## Unit conversions

Incoming Home Assistant values are converted before encoding:

- A, V, Hz, PF: unchanged
- kW -> W
- kVA -> VA
- kvar -> VAR
- kWh -> Wh

Default scale factors:

- A: -1
- V: -1
- Hz: -2
- W: 0
- VA: 0
- VAR: 0
- PF: -3
- Wh: 0

## Modbus gate

When `MODBUS_GATE_ENABLE=true`, the front proxy holds Modbus traffic until valid import/export totals are available from Home Assistant.

## Multi-port support

This build supports up to 6 fixed smart-meter ports: 502, 1502, 2502, 3502, 4502, 5502.

In Home Assistant Add-on mode configure `/data/meters.yaml` and enable the meters you need. Each meter gets its own serial, version, gate state, and entity bindings.

Status API examples:
- `/status` for all meters
- `/status?meter=sm1` for one meter
- `/registers?meter=sm1` for one meter register dump

## Multi-meter configuration notes

- `unit_id` can be set per meter in `meters.yaml`.
- `device_modbus_address` can be set per meter in `meters.yaml`.
- `device_version` can stay in `defaults` and is transmitted in the SunSpec Common Model version field.
- `device_name` is transmitted in the SunSpec Common Model name field.
- `entity_map_path` can point to a separate file per meter, for example `entity_map_sm1.yaml` ... `entity_map_sm6.yaml`.

## Home Assistant Add-on packaging

The repository includes add-on metadata files such as `repository.yaml`, `addon/CHANGELOG.md`, branding assets, translations, and an MIT `LICENSE` so it can be used directly as a custom Home Assistant add-on repository on GitHub at `https://github.com/cray9000/HA-SunSpec-Smart-Meter-Emulator`.

## License

This project is licensed under the MIT License. See `LICENSE`.

> Do not commit a real `.env` file. Keep secrets local and outside Git. In Home Assistant Add-on mode no `HA_TOKEN` file is required because the add-on uses the Supervisor API.
