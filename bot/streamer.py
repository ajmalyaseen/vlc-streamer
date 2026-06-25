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
import os
import time
from collections import deque

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

log = logging.getLogger("streamer")

ALIGN = 4096              # Telegram GetFile requires offset & limit to be 4 KiB-aligned
CHUNK_SIZE = 1024 * 1024  # 1 MiB — Telegram's fixed max part size (and 1 MiB block boundary)
MAX_FLOOD_WAIT = 20       # seconds; obey short Telegram GetFile rate-limits, give up beyond this
SEND_TIMEOUT = 10         # seconds; abandon a stuck GetFile sooner than Pyrogram's 15s and retry
MAX_CHUNK_RETRIES = 5     # retry a chunk this many times across session disconnect/timeout
# How many 1 MiB GetFile requests to keep in flight per connection. A high-latency
# link (e.g. server far from the file's DC) is throughput-starved with only one
# chunk in flight; issuing several in parallel multiplies throughput (~N×), which
# fixes seek stalls and the "audio plays but video freezes" symptom. Tunable via
# the STREAM_PIPELINE env var; 6 is a safe default that rarely triggers FloodWait.
PIPELINE_DEPTH = max(1, int(os.environ.get("STREAM_PIPELINE", "6") or "6"))

# Valid GetFile `limit` values: divisors of 1 MiB (4 KiB … 1 MiB), descending.
# Telegram requires `limit` to be one of these AND `offset % limit == 0`.
_DIVISORS = (1048576, 524288, 262144, 131072, 65536, 32768, 16384, 8192, 4096)


def _snap_divisor(n: int) -> int:
    """Largest 1 MiB-divisor <= n (min 4 KiB)."""
    for d in _DIVISORS:
        if d <= n:
            return d
    return 4096


def _largest_div(pos: int, cap: int) -> int:
    """Largest 1 MiB-divisor d <= cap that divides pos (so offset % d == 0)."""
    for d in _DIVISORS:
        if d <= cap and pos % d == 0:
            return d
    return 4096


# Size of the FIRST chunk fetched right after a seek (a 1 MiB-divisor). Smaller =
# the first frame arrives sooner (lower seek latency) with less wasted
# pre-download; steady chunks ramp up to 1 MiB for throughput. Tunable via
# STREAM_FIRST_CHUNK (bytes). Default 64 KiB.
FIRST_CHUNK = _snap_divisor(int(os.environ.get("STREAM_FIRST_CHUNK", str(64 * 1024)) or (64 * 1024)))


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


def _plan_parts(start: int, end: int):
    """Build the GetFile plan for byte range [start, end] (inclusive).

    Returns a list of (offset, limit, lo, hi): fetch `limit` bytes at `offset`,
    emit returned[lo:hi]. Every part satisfies Telegram's GetFile rules: `limit`
    is a divisor of 1 MiB and `offset % limit == 0` (which also keeps the range
    inside one 1 MiB block and offset 4 KiB-aligned). The first part uses the
    small FIRST_CHUNK size for a fast seek start; later parts ramp up to 1 MiB as
    the position aligns to bigger divisors."""
    parts = []
    pos = start
    first = True
    while pos <= end:
        if first:
            limit = FIRST_CHUNK
            offset = (pos // limit) * limit       # aligned down to the first-chunk size
            first = False
        else:
            limit = _largest_div(pos, CHUNK_SIZE)  # pos is aligned; pick biggest valid limit
            offset = pos
        lo = pos - offset                          # front bytes to skip (only first part > 0)
        hi = min(end, offset + limit - 1) - offset + 1  # exclusive end of useful bytes
        parts.append((offset, limit, lo, hi))
        pos = offset + limit                       # stays aligned to `limit`
    return parts


async def stream_to_response(client: Client, file_id: FileId, start: int, end: int, response) -> int:
    """Stream bytes [start, end] to an aiohttp response using a warm media session.

    Two optimizations stack here:
    - Adaptive fast-start: the FIRST chunk after a seek is small and 4 KiB-aligned
      to the seek point (not the 1 MiB boundary), so the first frame arrives fast
      with almost no wasted pre-download; steady chunks then ramp to 1 MiB.
    - Parallel pipeline: up to PIPELINE_DEPTH GetFile requests in flight at once,
      written in order, which multiplies throughput on a high-latency DC path.

    Lets FileReferenceExpired propagate so the caller can refresh + retry.
    """
    if end < start:
        return 0

    media_session = await get_media_session(client, file_id)
    location = get_location(file_id)

    parts = _plan_parts(start, end)

    # Telegram fetch stats for this request (GetFile speed only).
    stats = {"fetch_time": 0.0, "fetch_bytes": 0, "first_latency": None, "calls": 0}

    async def _fetch_chunk(off: int, lim: int):
        # Obey short Telegram GetFile rate-limits (FloodWait) and survive session
        # disconnects/timeouts (common under heavy seeking) by retrying the chunk
        # instead of letting the error kill the whole stream.
        attempts = 0
        while True:
            try:
                t0 = time.monotonic()
                r = await asyncio.wait_for(
                    media_session.send(
                        raw.functions.upload.GetFile(location=location, offset=off, limit=lim)
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

    def _fetch(part):
        return asyncio.ensure_future(_fetch_chunk(part[0], part[1]))

    written = 0
    n = len(parts)
    inflight = deque()
    next_idx = 0
    # Prime the pipeline with up to PIPELINE_DEPTH concurrent requests.
    while next_idx < n and len(inflight) < PIPELINE_DEPTH:
        inflight.append((_fetch(parts[next_idx]), parts[next_idx]))
        next_idx += 1

    try:
        while inflight:
            fut, part = inflight.popleft()
            r = await fut
            # Refill the pipeline so it stays full (keeps N requests in flight).
            if next_idx < n:
                inflight.append((_fetch(parts[next_idx]), parts[next_idx]))
                next_idx += 1

            if not isinstance(r, raw.types.upload.File) or not r.bytes:
                break
            _aoff, _lim, lo, hi = part
            chunk = r.bytes[lo:hi]
            await response.write(chunk)
            written += len(chunk)
    finally:
        # Cancel any still-in-flight fetches (client disconnected / seeked away),
        # freeing those requests promptly instead of letting them linger.
        for fut, _ in inflight:
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
    return written
