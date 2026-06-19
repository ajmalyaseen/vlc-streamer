"""Byte-range streaming over Pyrogram's stream_media.

Telegram caps file parts at 1 MiB (upload.GetFile), so Pyrogram yields fixed
1 MiB chunks — larger "chunk sizes" are not possible. Instead we prefetch a
few chunks ahead via a bounded queue so downloading from Telegram continues
while the current chunk is written to the client. This is the real lever for
reducing mid-playback waiting.

The prefetch producer runs as a normal task owned by this plain coroutine
(not an async generator), so cancelling/awaiting it during cleanup is safe —
that avoids the "coroutine ignored GeneratorExit" problem of generator-based
prefetch.
"""
import asyncio
import logging

from pyrogram import Client
from pyrogram.types import Message

log = logging.getLogger("streamer")

CHUNK_SIZE = 1024 * 1024  # 1 MiB — Telegram's max part size (fixed)
PREFETCH = 5              # chunks downloaded ahead (~5 MiB/stream); load now spread over 5 bots

_SENTINEL = object()


async def stream_to_response(
    client: Client,
    message: Message,
    start: int,
    end: int,
    response,
) -> int:
    """Stream bytes [start, end] of the media straight to an aiohttp response.

    Returns the number of bytes written. Lets FileReferenceExpired propagate so
    the caller can refresh the message and retry (it occurs before any write).
    """
    if end < start:
        return 0

    first_chunk = start // CHUNK_SIZE
    last_chunk = end // CHUNK_SIZE
    n_chunks = last_chunk - first_chunk + 1
    last_byte_exclusive = end + 1

    queue: asyncio.Queue = asyncio.Queue(maxsize=PREFETCH)

    async def producer() -> None:
        try:
            async for chunk in client.stream_media(
                message, offset=first_chunk, limit=n_chunks
            ):
                await queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # surface to the consumer
            await queue.put(e)
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(producer())
    pos = first_chunk * CHUNK_SIZE
    written = 0
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                raise item
            chunk = item
            lo = max(0, start - pos)
            hi = min(len(chunk), last_byte_exclusive - pos)
            if lo < hi:
                await response.write(chunk[lo:hi])
                written += hi - lo
            pos += len(chunk)
            if pos >= last_byte_exclusive:
                break
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return written
