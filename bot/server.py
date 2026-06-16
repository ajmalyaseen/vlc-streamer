import asyncio
import logging
import re

from aiohttp import web
from pyrogram import Client
from pyrogram.types import Message

from .config import Config
from .streamer import stream_range
from .utils import verify_token

log = logging.getLogger("server")

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _extract_media(message: Message):
    return message.document or message.video or message.audio or message.animation


async def stream_handler(request: web.Request) -> web.StreamResponse:
    cfg: Config = request.app["config"]
    bot: Client = request.app["bot"]

    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Bad chat or message id")

    token = request.query.get("hash", "")
    if not verify_token(chat_id, msg_id, token, cfg.hash_secret):
        return web.Response(status=403, text="Invalid or missing token")

    try:
        message = await bot.get_messages(chat_id, msg_id)
    except Exception as e:
        log.warning("get_messages failed for %s/%s: %s", chat_id, msg_id, e)
        return web.Response(status=404, text="File not found")

    media = _extract_media(message)
    if not media:
        return web.Response(status=404, text="No streamable media on this message")

    file_size: int = media.file_size
    mime: str = media.mime_type or "application/octet-stream"
    file_name: str = getattr(media, "file_name", None) or f"file_{msg_id}"

    # Default: full content
    start = 0
    end = file_size - 1
    status = 200

    range_header = request.headers.get("Range")
    if range_header:
        m = RANGE_RE.match(range_header)
        if m:
            s, e = m.group(1), m.group(2)
            if s == "" and e == "":
                return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
            if s == "":
                # suffix range: last N bytes
                suffix = int(e)
                start = max(0, file_size - suffix)
                end = file_size - 1
            else:
                start = int(s)
                end = int(e) if e else file_size - 1
            if start >= file_size or end >= file_size or start > end:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
            status = 206

    length = end - start + 1
    headers = {
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f'inline; filename="{file_name}"',
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    if request.method == "HEAD":
        await response.write_eof()
        return response

    try:
        async for chunk in stream_range(bot, message, start, end):
            await response.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        log.info("Client disconnected during stream of msg %s", msg_id)
    except Exception as e:
        log.exception("Streaming error for msg %s: %s", msg_id, e)
    finally:
        try:
            await response.write_eof()
        except Exception:
            pass

    return response


async def index(_request: web.Request) -> web.Response:
    return web.Response(text="Telegram → VLC stream bot is running.")


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app(bot: Client, cfg: Config) -> web.Application:
    app = web.Application(client_max_size=1024 * 16)
    app["bot"] = bot
    app["config"] = cfg
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    # add_get also registers HEAD automatically (allow_head=True by default),
    # and our handler checks request.method == "HEAD".
    app.router.add_get("/stream/{chat_id}/{msg_id}/{name}", stream_handler)
    return app
