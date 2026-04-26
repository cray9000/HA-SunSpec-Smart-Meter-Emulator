from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from collections.abc import Awaitable, Callable

from aiohttp import web

from .register_store import RegisterStore

_LOGGER = logging.getLogger(__name__)

ReloadCallback = Callable[[], Awaitable[dict[str, object]]]
ReloadStateProvider = Callable[[], dict[str, object]]


def _store_or_404(stores: dict[str, RegisterStore], meter_id: str | None) -> RegisterStore:
    if not meter_id:
        raise web.HTTPBadRequest(text="Missing meter query parameter")
    store = stores.get(meter_id)
    if store is None:
        raise web.HTTPNotFound(text=f"Unknown meter '{meter_id}'")
    return store


def _dashboard_html(stores: dict[str, RegisterStore], reload_state: dict[str, object]) -> str:
    cards = []
    for meter_id, store in stores.items():
        status = store.status_payload()
        cards.append(
            f"""
            <div class='card'>
              <h3>{meter_id}</h3>
              <p><b>Port:</b> {status['public_port']} &nbsp; <b>Name:</b> {status['device']['device_name']}</p>
              <p><b>Serial:</b> {status['device']['serial']} &nbsp; <b>Version:</b> {status['device']['version']}</p>
              <p><b>Gate:</b> {'blocked' if status['modbus_gate_blocked'] else 'open'} &nbsp; <b>Reads:</b> {status['modbus_read_requests']}</p>
              <p>
                <a href='/meter/{meter_id}'>Live view</a> |
                <a href='/status?meter={meter_id}'>Status JSON</a> |
                <a href='/registers?meter={meter_id}'>Registers JSON</a> |
                <a href='/dump?meter={meter_id}'>Debug dump</a>
              </p>
            </div>
            """
        )
    reload_msg = reload_state.get("last_reload_message", "")
    return f"""
    <html>
    <head>
      <title>HA SunSpec Smart Meter Emulator</title>
      <style>
        body {{ font-family: Arial, sans-serif; margin: 24px; }}
        .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin-bottom: 12px; }}
        button {{ padding: 8px 12px; }}
      </style>
    </head>
    <body>
      <h1>HA SunSpec Smart Meter Emulator</h1>
      <p><b>Reload state:</b> {reload_msg}</p>
      <p>
        <button onclick="fetch('/reload', {{method:'POST'}}).then(r => r.json()).then(() => location.reload())">Reload config now</button>
        <a href='/dump/all.zip'>Download all dumps</a>
      </p>
      {''.join(cards)}
    </body>
    </html>
    """


def _meter_html(meter_id: str) -> str:
    return f"""
    <html>
    <head>
      <title>{meter_id} live view</title>
      <style>
        body {{ font-family: Arial, sans-serif; margin: 24px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 6px; text-align: left; }}
        th {{ background: #f4f4f4; position: sticky; top: 0; }}
        .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
      </style>
    </head>
    <body>
      <h1>Meter {meter_id}</h1>
      <p><a href='/'>Back</a> | <a href='/dump?meter={meter_id}'>Download debug dump</a></p>
      <h2>Status</h2>
      <pre id='status'>loading…</pre>
      <h2>Registers</h2>
      <table id='regs'>
        <thead><tr><th>Register</th><th>Key</th><th>Value</th><th>Words</th><th>SF</th><th>HA raw</th><th>Source unit</th></tr></thead>
        <tbody></tbody>
      </table>
      <script>
      async function refresh() {{
        const status = await fetch('/status?meter={meter_id}').then(r => r.json());
        document.getElementById('status').textContent = JSON.stringify(status, null, 2);
        const regs = await fetch('/registers?meter={meter_id}').then(r => r.json());
        const tbody = document.querySelector('#regs tbody');
        tbody.innerHTML = '';
        for (const row of regs.registers) {{
          const tr = document.createElement('tr');
          tr.innerHTML = `<td class="mono">${{row.address_hex}}</td><td>${{row.logical_key}}</td><td>${{row.converted_value ?? ''}}</td><td class="mono">${{row.words_hex.join(' ')}}</td><td>${{row.sf}}</td><td>${{row.ha_raw_state ?? ''}}</td><td>${{row.source_ha_unit ?? ''}}</td>`;
          tbody.appendChild(tr);
        }}
      }}
      refresh();
      setInterval(refresh, 2000);
      </script>
    </body>
    </html>
    """


async def run_status_server(
    host: str,
    port: int,
    stores: dict[str, RegisterStore],
    *,
    reload_callback: ReloadCallback | None = None,
    reload_state_provider: ReloadStateProvider | None = None,
) -> None:
    app = web.Application()

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "meter_count": len(stores)})

    async def root(_: web.Request) -> web.Response:
        reload_state = reload_state_provider() if reload_state_provider is not None else {}
        return web.Response(text=_dashboard_html(stores, reload_state), content_type="text/html")

    async def meter_page(request: web.Request) -> web.Response:
        meter_id = request.match_info["meter_id"]
        _store_or_404(stores, meter_id)
        return web.Response(text=_meter_html(meter_id), content_type="text/html")

    async def status(request: web.Request) -> web.Response:
        meter_id = request.query.get("meter")
        if meter_id:
            store = _store_or_404(stores, meter_id)
            payload = store.status_payload()
            if reload_state_provider is not None:
                payload["reload"] = reload_state_provider()
            return web.json_response(payload)
        payload = {"meters": {meter_id: store.status_payload() for meter_id, store in stores.items()}}
        if reload_state_provider is not None:
            payload["reload"] = reload_state_provider()
        return web.json_response(payload)

    async def registers(request: web.Request) -> web.Response:
        meter_id = request.query.get("meter")
        if meter_id:
            store = _store_or_404(stores, meter_id)
            return web.json_response(store.register_dump())
        return web.json_response({"meters": {meter_id: store.register_dump() for meter_id, store in stores.items()}})

    async def reload(_: web.Request) -> web.Response:
        if reload_callback is None:
            raise web.HTTPNotImplemented(text="Reload callback not configured")
        result = await reload_callback()
        status_code = 200 if result.get("ok") else 409
        return web.json_response(result, status=status_code)

    async def dump_meter(request: web.Request) -> web.Response:
        meter_id = request.query.get("meter")
        store = _store_or_404(stores, meter_id)
        body = store.debug_dump_json().encode("utf-8")
        headers = {"Content-Disposition": f'attachment; filename="{meter_id}_debug_dump.json"'}
        return web.Response(body=body, headers=headers, content_type="application/json")

    async def dump_all(_: web.Request) -> web.Response:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for meter_id, store in stores.items():
                zf.writestr(f"{meter_id}_debug_dump.json", store.debug_dump_json())
        headers = {"Content-Disposition": 'attachment; filename="fronius_debug_dumps.zip"'}
        return web.Response(body=buf.getvalue(), headers=headers, content_type="application/zip")

    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/meter/{meter_id}", meter_page)
    app.router.add_get("/status", status)
    app.router.add_get("/registers", registers)
    app.router.add_post("/reload", reload)
    app.router.add_get("/dump", dump_meter)
    app.router.add_get("/dump/all.zip", dump_all)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _LOGGER.info("Status server listening on %s:%s", host, port)

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
