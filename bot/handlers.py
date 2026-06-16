import logging
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.types import Message

from .config import Config
from .utils import human_size, make_token

log = logging.getLogger("handlers")

WELCOME = (
    "Hi! Send me a video file (MP4 / MKV / etc.) and I'll reply with a direct "
    "streaming link you can open in VLC.\n\n"
    "In VLC: Media → Open Network Stream → paste the link."
)


def register_handlers(app: Client, cfg: Config) -> None:
    @app.on_message(filters.command("start") & filters.private)
    async def on_start(_c: Client, m: Message):
        await m.reply_text(WELCOME)

    @app.on_message(filters.command("id"))
    async def on_id(_c: Client, m: Message):
        # Useful when setting up: forward a message from the log channel to
        # the bot, or run /id inside the channel via the bot to find chat_id.
        await m.reply_text(f"chat id: `{m.chat.id}`", quote=True)

    @app.on_message(
        filters.private
        & (filters.document | filters.video | filters.audio | filters.animation)
    )
    async def on_file(client: Client, m: Message):
        media = m.document or m.video or m.audio or m.animation
        if media is None:
            return

        # Copy into the log channel so we get a stable, permanent message_id
        # we can fetch later when serving the stream.
        try:
            stored = await m.copy(cfg.log_channel)
        except Exception as e:
            log.exception("copy to log channel failed: %s", e)
            await m.reply_text(
                "Couldn't store the file. Make sure the bot is an admin of the "
                "LOG_CHANNEL with permission to post messages."
            )
            return

        msg_id = stored.id
        token = make_token(msg_id, cfg.hash_secret)
        file_name = getattr(media, "file_name", None) or f"file_{msg_id}.mp4"
        url = f"{cfg.base_url}/stream/{msg_id}/{quote(file_name)}?hash={token}"

        await m.reply_text(
            f"**File:** `{file_name}`\n"
            f"**Size:** {human_size(media.file_size)}\n\n"
            f"**Stream link:**\n`{url}`\n\n"
            f"Open in VLC → _Media_ → _Open Network Stream_.",
            disable_web_page_preview=True,
            quote=True,
        )
