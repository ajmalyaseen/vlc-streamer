"""Byte-range streaming over Pyrogram's stream_media.

Pyrogram yields fixed 1 MiB chunks. We align the requested [start, end] byte
range to chunk boundaries, then trim the first/last chunks so callers get
exactly the bytes they asked for.

Chunks are prefetched a few ahead via a bounded queue so downloading from
Telegram continues while the current chunk is being written to the client.
"""
import asyncio
from typing import AsyncGenerator

from pyrogram import Client
from pyrogram.types import Message

CHUNK_SIZE = 1024 * 1024  # 1 MiB — Pyrogram's native chunk size
PREFETCH = 4              # how many chunks to download ahead of the writer


async def stream_range(
    client: Client,
    message: Message,
    start: int,
    end: int,
) -> AsyncGenerator[bytes, None]:
    """Yield bytes from `start` to `end` (inclusive) of the message's media."""
    if end < start:
        return

    first_chunk_index = start // CHUNK_SIZE
    last_chunk_index = end // CHUNK_SIZE
    n_chunks = last_chunk_index - first_chunk_index + 1
    last_byte_exclusive = end + 1

    queue: asyncio.Queue = asyncio.Queue(maxsize=PREFETCH)

    async def producer() -> None:
        try:
            async for chunk in client.stream_media(
                message, offset=first_chunk_index, limit=n_chunks
            ):
                await queue.put(chunk)
        except Exception as e:  # surface errors to the consumer
            await queue.put(e)
        finally:
            await queue.put(None)  # sentinel: done

    task = asyncio.create_task(producer())
    pos = first_chunk_index * CHUNK_SIZE
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            chunk = item
            chunk_start = pos
            slice_lo = max(0, start - chunk_start)
            slice_hi = min(len(chunk), last_byte_exclusive - chunk_start)
            if slice_lo < slice_hi:
                yield chunk[slice_lo:slice_hi]
            pos += len(chunk)
            if pos >= last_byte_exclusive:
                break
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
