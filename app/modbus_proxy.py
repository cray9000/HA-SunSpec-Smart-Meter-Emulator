from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


def _frame_length(buffer: bytes) -> int | None:
    if len(buffer) < 7:
        return None
    _tid, _pid, length = struct.unpack(">HHH", buffer[:6])
    return 6 + length


def _extract_frames(buffer: bytearray) -> list[bytes]:
    frames: list[bytes] = []
    while True:
        total = _frame_length(buffer)
        if total is None or len(buffer) < total:
            break
        frames.append(bytes(buffer[:total]))
        del buffer[:total]
    return frames


def _rewrite_unit_id(frame: bytes, primary_unit_id: int, alias_unit_ids: tuple[int, ...]) -> tuple[bytes, bool]:
    if len(frame) < 8:
        return frame, False
    unit = frame[6]
    if unit not in alias_unit_ids:
        return frame, False
    rewritten = bytearray(frame)
    rewritten[6] = primary_unit_id & 0xFF
    return bytes(rewritten), True


def _log_frame(prefix: str, stream_name: str, frame: bytes, peer: object, *, rewritten: bool = False, original_unit: int | None = None) -> None:
    tid, _pid, _length = struct.unpack(">HHH", frame[:6])
    unit = frame[6]
    fc = frame[7] if len(frame) > 7 else 0
    suffix = ""
    if rewritten and original_unit is not None:
        suffix = f" rewritten_unit={original_unit}->{unit}"
    _LOGGER.info(
        "%s %s frame: peer=%s tid=%s unit=%s fc=0x%02X len=%s%s raw=%s",
        prefix,
        stream_name,
        peer,
        tid,
        unit,
        fc,
        len(frame),
        suffix,
        frame.hex(" "),
    )


async def run_modbus_proxy(
    listen_host: str,
    listen_port: int,
    backend_host: str,
    backend_port: int,
    primary_unit_id: int,
    alias_unit_ids: tuple[int, ...],
    raw_relay: bool = False,
    is_gate_blocked: Callable[[], bool] | None = None,
    wait_until_gate_open: Callable[[], Awaitable[None]] | None = None,
) -> None:
    async def wait_for_gate(peer: object, phase: str) -> None:
        if is_gate_blocked is None or wait_until_gate_open is None:
            return
        waiting_logged = False
        while is_gate_blocked():
            if not waiting_logged:
                _LOGGER.info(
                    "Modbus gate blocking proxy traffic during %s for peer=%s; waiting for valid import/export",
                    phase,
                    peer,
                )
                waiting_logged = True
            await wait_until_gate_open()
        if waiting_logged:
            _LOGGER.info("Modbus gate reopened; resuming proxy traffic during %s for peer=%s", phase, peer)

    async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        peer = client_writer.get_extra_info("peername")
        _LOGGER.info(
            "Modbus proxy accepted %s -> %s:%s (primary_unit=%s aliases=%s raw_relay=%s)",
            peer,
            backend_host,
            backend_port,
            primary_unit_id,
            list(alias_unit_ids),
            raw_relay,
        )

        await wait_for_gate(peer, "accept")

        try:
            backend_reader, backend_writer = await asyncio.open_connection(backend_host, backend_port)
        except Exception:
            _LOGGER.exception("Failed to connect proxy backend %s:%s", backend_host, backend_port)
            client_writer.close()
            await client_writer.wait_closed()
            return

        async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str, peer_obj: object) -> None:
            buffer = bytearray()
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break

                if direction == "c2b":
                    await wait_for_gate(peer_obj, direction)

                buffer.extend(chunk)
                frames = _extract_frames(buffer)
                if not frames:
                    continue
                for frame in frames:
                    if direction == "c2b":
                        await wait_for_gate(peer_obj, direction)
                    original_unit = frame[6] if len(frame) > 6 else None
                    rewritten = False
                    if direction == "c2b":
                        frame, rewritten = _rewrite_unit_id(frame, primary_unit_id, alias_unit_ids)
                    _log_frame("Modbus proxy", direction, frame, peer_obj, rewritten=rewritten, original_unit=original_unit)
                    writer.write(frame)
                    if raw_relay:
                        await writer.drain()
                if not raw_relay:
                    await writer.drain()

            if buffer:
                _LOGGER.warning(
                    "Modbus proxy %s closed with %s unparsed bytes from peer=%s: %s",
                    direction,
                    len(buffer),
                    peer_obj,
                    bytes(buffer).hex(" "),
                )
                writer.write(buffer)
                await writer.drain()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        await asyncio.gather(
            relay(client_reader, backend_writer, "c2b", peer),
            relay(backend_reader, client_writer, "b2c", peer),
            return_exceptions=True,
        )

    server = await asyncio.start_server(handle_client, listen_host, listen_port)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    _LOGGER.info(
        "Modbus proxy listening on %s -> backend %s:%s (primary_unit=%s aliases=%s raw_relay=%s)",
        addrs,
        backend_host,
        backend_port,
        primary_unit_id,
        list(alias_unit_ids),
        raw_relay,
    )
    async with server:
        await server.serve_forever()
