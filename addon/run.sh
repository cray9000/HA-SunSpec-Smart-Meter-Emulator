#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data

for file in entity_map.yaml meters.yaml entity_map_sm1.yaml entity_map_sm2.yaml entity_map_sm3.yaml entity_map_sm4.yaml entity_map_sm5.yaml entity_map_sm6.yaml; do
  if [ ! -f "/data/${file}" ]; then
    cp "/defaults/${file}" "/data/${file}"
  fi
done

export HA_MODE=supervisor
export STATUS_PORT="$(bashio::config 'status_port')"
export RESYNC_INTERVAL_S="300"
export CONFIG_RELOAD_INTERVAL_S="$(bashio::config 'config_reload_interval_s')"
export LOG_LEVEL="$(bashio::config 'log_level')"
export METERS_FILE="/data/$(bashio::config 'meters_file')"

exec python3 -m app.main
