"""Byte-range streaming over Pyrogram's stream_media.

Pyrogram yields fixed 1 MiB chunks. We align the requested [start, end] byte
range to chunk boundaries, then trim the first/last chunks so callers get
exactly the bytes they asked for.

Kept deliberately simple (sequential) for stability: VLC opens and abandons
many short Range requests, and a background prefetch task made early-cancel
cleanup unreliable on constrained hosts.
"""
from typing import AsyncGenerator

from pyrogram import Client
from pyrogram.types import Message

CHUNK_SIZE = 1024 * 1024  # 1 MiB — Pyrogram's native chunk size


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

    pos = first_chunk_index * CHUNK_SIZE
    async for chunk in client.stream_media(
        message,
        offset=first_chunk_index,
        limit=n_chunks,
    ):
        chunk_start = pos
        slice_lo = max(0, start - chunk_start)
        slice_hi = min(len(chunk), last_byte_exclusive - chunk_start)
        if slice_lo < slice_hi:
            yield chunk[slice_lo:slice_hi]
        pos += len(chunk)
        if pos >= last_byte_exclusive:
            break
