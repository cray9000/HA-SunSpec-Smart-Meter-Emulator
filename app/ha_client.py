from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from typing import Any

import aiohttp

from .config import AppConfig
from .register_store import RegisterStore

_LOGGER = logging.getLogger(__name__)


class HomeAssistantBridge:
    def __init__(self, config: AppConfig, stores: list[RegisterStore]) -> None:
        self.config = config
        self.stores = stores
        self._headers = {
            "Authorization": f"Bearer {config.ha_token}",
            "Content-Type": "application/json",
        }
        self._entity_targets: dict[str, list[RegisterStore]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._targets_lock = asyncio.Lock()
        self.rebuild_entity_targets()

    def rebuild_entity_targets(self) -> None:
        mapping: dict[str, list[RegisterStore]] = defaultdict(list)
        for store in self.stores:
            for entity_id in store.entity_lookup:
                mapping[entity_id].append(store)
        self._entity_targets = dict(mapping)

    async def force_resync(self) -> None:
        session = self._session
        if session is None:
            return
        await self._sync_once(session)

    async def run_forever(self) -> None:
        if not self.config.ha_token:
            raise RuntimeError("No HA token available. Set HA_TOKEN or SUPERVISOR_TOKEN.")

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._headers) as session:
            self._session = session
            try:
                while True:
                    try:
                        await self._sync_once(session)
                        await self._websocket_loop(session)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        for store in self.stores:
                            store.stats.websocket_reconnects += 1
                        _LOGGER.warning("HA bridge reconnect after error: %s", exc)
                        await asyncio.sleep(3)
            finally:
                self._session = None

    async def _sync_once(self, session: aiohttp.ClientSession) -> None:
        url = f"{self.config.ha_base_url}/states"
        _LOGGER.info("Fetching initial HA state snapshot from %s", url)
        async with session.get(url) as response:
            response.raise_for_status()
            payload = await response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected HA /states response")
        await asyncio.gather(*(store.apply_initial_states(payload) for store in self.stores))

    async def _websocket_loop(self, session: aiohttp.ClientSession) -> None:
        _LOGGER.info("Opening HA websocket %s", self.config.ws_url)
        async with session.ws_connect(self.config.ws_url, heartbeat=30) as ws:
            first = await ws.receive_json()
            if first.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected websocket greeting: {first}")

            await ws.send_json({"type": "auth", "access_token": self.config.ha_token})
            auth_reply = await ws.receive_json()
            if auth_reply.get("type") != "auth_ok":
                raise RuntimeError(f"HA websocket auth failed: {auth_reply}")

            await ws.send_json({"id": 1, "type": "supported_features", "features": {"coalesce_messages": 1}})
            await ws.send_json({"id": 2, "type": "subscribe_events", "event_type": "state_changed"})

            resync_task = asyncio.create_task(self._periodic_resync(session))
            try:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_ws_text(message.data)
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(f"websocket error: {ws.exception()}")
                    elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING}:
                        raise RuntimeError("websocket closed")
            finally:
                resync_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await resync_task

    async def _handle_ws_text(self, raw_message: str) -> None:
        decoded = json.loads(raw_message)
        messages: list[dict[str, Any]]
        if isinstance(decoded, list):
            messages = [msg for msg in decoded if isinstance(msg, dict)]
        elif isinstance(decoded, dict):
            messages = [decoded]
        else:
            return

        tasks = []
        async with self._targets_lock:
            targets = self._entity_targets
            for data in messages:
                if data.get("type") != "event":
                    continue
                event = data.get("event", {})
                if not isinstance(event, dict) or event.get("event_type") != "state_changed":
                    continue
                payload = event.get("data", {})
                if not isinstance(payload, dict):
                    continue
                entity_id = payload.get("entity_id")
                new_state = payload.get("new_state") or {}
                if entity_id is None or not isinstance(new_state, dict):
                    continue
                for store in targets.get(entity_id, []):
                    tasks.append(store.apply_state_change(entity_id, new_state))
        if tasks:
            await asyncio.gather(*tasks)

    async def _periodic_resync(self, session: aiohttp.ClientSession) -> None:
        while True:
            await asyncio.sleep(self.config.resync_interval_s)
            try:
                await self._sync_once(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Periodic HA resync failed: %s", exc)
