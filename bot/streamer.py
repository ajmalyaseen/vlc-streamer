"""Custom byte-streamer that keeps one media session per (client, DC) warm.

Pyrogram's stream_media creates AND tears down a media session — a full
Diffie-Hellman auth-key exchange (~2s) — on every call. That handshake, redone
on every seek, was the buffering. Here we keep one media session per DC alive in
client.media_sessions and issue raw upload.GetFile calls on it, so seeks reuse
the warm session and skip the handshake.

Based on the well-known TG-FileStreamBot ByteStreamer pattern.
"""
import logging
import math

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

log = logging.getLogger("streamer")

CHUNK_SIZE = 1024 * 1024  # 1 MiB — Telegram's fixed max part size


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

    Lets FileReferenceExpired propagate so the caller can refresh + retry.
    """
    if end < start:
        return 0

    media_session = await get_media_session(client, file_id)
    location = get_location(file_id)

    offset = start - (start % CHUNK_SIZE)
    first_part_cut = start - offset
    last_part_cut = (end % CHUNK_SIZE) + 1
    part_count = math.ceil((end + 1) / CHUNK_SIZE) - math.floor(offset / CHUNK_SIZE)

    written = 0
    current_part = 1
    r = await media_session.send(
        raw.functions.upload.GetFile(location=location, offset=offset, limit=CHUNK_SIZE)
    )
    if isinstance(r, raw.types.upload.File):
        while current_part <= part_count:
            chunk = r.bytes
            if not chunk:
                break
            if part_count == 1:
                chunk = chunk[first_part_cut:last_part_cut]
            elif current_part == 1:
                chunk = chunk[first_part_cut:]
            elif current_part == part_count:
                chunk = chunk[:last_part_cut]

            await response.write(chunk)
            written += len(chunk)

            current_part += 1
            offset += CHUNK_SIZE
            if current_part > part_count:
                break
            r = await media_session.send(
                raw.functions.upload.GetFile(location=location, offset=offset, limit=CHUNK_SIZE)
            )
    return written
