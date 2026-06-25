"""Custom byte-streamer that keeps one media session per (client, DC) warm.

Pyrogram's stream_media creates AND tears down a media session — a full
Diffie-Hellman auth-key exchange (~2s) — on every call. That handshake, redone
on every seek, was the buffering. Here we keep one media session per DC alive in
client.media_sessions and issue raw upload.GetFile calls on it, so seeks reuse
the warm session and skip the handshake.

Based on the well-known TG-FileStreamBot ByteStreamer pattern.
"""
import asyncio
import logging
import math
import os
import time
from collections import deque

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

log = logging.getLogger("streamer")

CHUNK_SIZE = 1024 * 1024  # 1 MiB — Telegram's fixed max part size
MAX_FLOOD_WAIT = 20       # seconds; obey short Telegram GetFile rate-limits, give up beyond this
SEND_TIMEOUT = 10         # seconds; abandon a stuck GetFile sooner than Pyrogram's 15s and retry
MAX_CHUNK_RETRIES = 5     # retry a chunk this many times across session disconnect/timeout
# How many 1 MiB GetFile requests to keep in flight per connection. A high-latency
# link (e.g. server far from the file's DC) is throughput-starved with only one
# chunk in flight; issuing several in parallel multiplies throughput (~N×), which
# fixes seek stalls and the "audio plays but video freezes" symptom. Tunable via
# the STREAM_PIPELINE env var; 6 is a safe default that rarely triggers FloodWait.
PIPELINE_DEPTH = max(1, int(os.environ.get("STREAM_PIPELINE", "6") or "6"))


async def get_media_session(client: Client, file_id: FileId) -> Session:
    """Return a cached, warm media session for the file's DC, creating it once."""
    media_session = client.media_sessions.get(file_id.dc_id, None)
    if media_session is not None:
        return media_session

    if file_id.dc_id != await client.storage.dc_id():
        media_session = Session(
            client,
            file_id.dc_id,
            await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(),
            is_media=True,
        )
        await media_session.start()
        for _ in range(6):
            exported_auth = await client.invoke(
                raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
            )
            try:
                await media_session.send(
                    raw.functions.auth.ImportAuthorization(
                        id=exported_auth.id, bytes=exported_auth.bytes
                    )
                )
                break
            except AuthBytesInvalid:
                continue
        else:
            await media_session.stop()
            raise AuthBytesInvalid
    else:
        media_session = Session(
            client,
            file_id.dc_id,
            await client.storage.auth_key(),
            await client.storage.test_mode(),
            is_media=True,
        )
        await media_session.start()

    client.media_sessions[file_id.dc_id] = media_session
    return media_session


def get_location(file_id: FileId):
    if file_id.file_type == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    return raw.types.InputDocumentFileLocation(
        id=file_id.media_id,
        access_hash=file_id.access_hash,
        file_reference=file_id.file_reference,
        thumb_size=file_id.thumbnail_size,
    )


async def stream_to_response(client: Client, file_id: FileId, start: int, end: int, response) -> int:
    """Stream bytes [start, end] to an aiohttp response using a warm media session.

    Parallel-pipelined: up to PIPELINE_DEPTH GetFile requests are kept in flight
    at once and written to the client in order. On a high-latency path to the
    file's DC this multiplies throughput vs one-chunk-at-a-time, which is what
    fixes seek stalls / "audio plays but video frozen". Lets FileReferenceExpired
    propagate so the caller can refresh + retry.
    """
    if end < start:
        return 0

    media_session = await get_media_session(client, file_id)
    location = get_location(file_id)

    offset = start - (start % CHUNK_SIZE)
    first_part_cut = start - offset
    last_part_cut = (end % CHUNK_SIZE) + 1
    part_count = math.ceil((end + 1) / CHUNK_SIZE) - math.floor(offset / CHUNK_SIZE)

    # Telegram fetch stats for this request (measures the GetFile speed only,
    # i.e. how fast the server pulls bytes FROM Telegram, excluding the time
    # spent writing to the viewer's socket).
    stats = {"fetch_time": 0.0, "fetch_bytes": 0, "first_latency": None, "calls": 0}

    async def _fetch_chunk(off: int):
        # Obey short Telegram GetFile rate-limits (FloodWait) and survive session
        # disconnects/timeouts (common under heavy seeking) by retrying the chunk
        # instead of letting the error kill the whole stream.
        attempts = 0
        while True:
            try:
                t0 = time.monotonic()
                r = await asyncio.wait_for(
                    media_session.send(
                        raw.functions.upload.GetFile(location=location, offset=off, limit=CHUNK_SIZE)
                    ),
                    timeout=SEND_TIMEOUT,
                )
                dt = time.monotonic() - t0
                stats["fetch_time"] += dt
                stats["calls"] += 1
                if stats["first_latency"] is None:
                    stats["first_latency"] = dt
                if isinstance(r, raw.types.upload.File) and r.bytes:
                    stats["fetch_bytes"] += len(r.bytes)
                return r
            except FloodWait as e:
                wait = int(getattr(e, "value", 0) or 0)
                if wait > MAX_FLOOD_WAIT:
                    raise
                await asyncio.sleep(wait + 1)
            except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as e:
                # Session likely dropped/reconnecting (heavy seeking). Back off
                # briefly and retry on the same (reconnected) session.
                attempts += 1
                if attempts > MAX_CHUNK_RETRIES:
                    raise
                log.warning("chunk fetch retry %d/%d at offset %d (%s)",
                            attempts, MAX_CHUNK_RETRIES, off, type(e).__name__)
                await asyncio.sleep(min(0.5 * attempts, 2.0))

    def _fetch(off: int):
        return asyncio.ensure_future(_fetch_chunk(off))

    written = 0
    # Offsets of every 1 MiB part we need, in order.
    offsets = [offset + i * CHUNK_SIZE for i in range(part_count)]
    inflight = deque()
    next_idx = 0
    # Prime the pipeline with up to PIPELINE_DEPTH concurrent requests.
    while next_idx < part_count and len(inflight) < PIPELINE_DEPTH:
        inflight.append(_fetch(offsets[next_idx]))
        next_idx += 1

    current_part = 0
    try:
        while inflight:
            r = await inflight.popleft()
            current_part += 1
            # Refill the pipeline so it stays full (keeps N requests in flight).
            if next_idx < part_count:
                inflight.append(_fetch(offsets[next_idx]))
                next_idx += 1

            if not isinstance(r, raw.types.upload.File) or not r.bytes:
                break
            chunk = r.bytes
            if part_count == 1:
                chunk = chunk[first_part_cut:last_part_cut]
            elif current_part == 1:
                chunk = chunk[first_part_cut:]
            elif current_part == part_count:
                chunk = chunk[:last_part_cut]

            await response.write(chunk)
            written += len(chunk)
    finally:
        # Cancel any still-in-flight fetches (client disconnected / seeked away),
        # freeing those requests promptly instead of letting them linger.
        for fut in inflight:
            fut.cancel()
        ft = stats["fetch_time"]
        if stats["calls"] and ft > 0 and stats["fetch_bytes"]:
            mib = stats["fetch_bytes"] / (1024 * 1024)
            log.info(
                "tg-fetch: %.1f MiB in %.2fs = %.1f MiB/s (%.0f Mbit/s) | "
                "first-chunk %.0f ms | %d calls | dc=%s",
                mib, ft, mib / ft, (mib * 8) / ft,
                (stats["first_latency"] or 0) * 1000, stats["calls"], file_id.dc_id,
            )
    return written
