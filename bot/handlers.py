import logging
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import Config
from .utils import human_size, make_token

log = logging.getLogger("handlers")

DEVELOPER = "Ajmal Yaseen"
CHANNEL_LINK = "https://t.me/alaska_in"
VERSION = "v1.0.0"


def start_text(name: str) -> str:
    return (
        f"👋 Hai {name},\n\n"
        "I am a **File to VLC Stream Link** bot.\n"
        "Send me any video file (MP4 / MKV) and I'll give you a direct "
        "streaming link you can open in VLC.\n\n"
        f"✨ Maintained by [Alaska bots]({CHANNEL_LINK})"
    )


HELP_TEXT = (
    "💡 **How to use**\n\n"
    "1. Send me a video file (MP4 / MKV / etc.).\n"
    "2. I'll reply with a direct streaming link.\n"
    "3. Open **VLC → Media → Open Network Stream**, paste the link and play.\n\n"
    "The link supports seeking, so you can jump around in the video."
)

ABOUT_TEXT = (
    "📂 **About Me**\n\n"
    "❄ **Bot Name :** VLC Streamer\n"
    "❄ **Framework :** Pyrogram\n"
    "❄ **Language :** Python\n"
    f"❄ **Version :** {VERSION}\n"
    "❄ **Source Code :** Private\n"
    f"❄ **Developer :** {DEVELOPER}"
)


def start_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Updates", url=CHANNEL_LINK)],
            [
                InlineKeyboardButton("💡 Help", callback_data="help"),
                InlineKeyboardButton("📂 About", callback_data="about"),
            ],
            [InlineKeyboardButton("🔐 Close", callback_data="close")],
        ]
    )


def back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("◀ Back", callback_data="back"),
                InlineKeyboardButton("🔐 Close", callback_data="close"),
            ]
        ]
    )


def register_handlers(app: Client, cfg: Config) -> None:
    @app.on_message(filters.command("start") & filters.private)
    async def on_start(_c: Client, m: Message):
        name = m.from_user.mention if m.from_user else "there"
        await m.reply_text(
            start_text(name),
            reply_markup=start_markup(),
            disable_web_page_preview=True,
            quote=True,
        )

    @app.on_message(filters.command("help") & filters.private)
    async def on_help(_c: Client, m: Message):
        await m.reply_text(
            HELP_TEXT, reply_markup=back_markup(), disable_web_page_preview=True, quote=True
        )

    @app.on_message(filters.command("about") & filters.private)
    async def on_about(_c: Client, m: Message):
        await m.reply_text(
            ABOUT_TEXT, reply_markup=back_markup(), disable_web_page_preview=True, quote=True
        )

    @app.on_callback_query()
    async def on_callback(_c: Client, cq: CallbackQuery):
        data = cq.data
        if data == "help":
            await cq.message.edit_text(
                HELP_TEXT, reply_markup=back_markup(), disable_web_page_preview=True
            )
        elif data == "about":
            await cq.message.edit_text(
                ABOUT_TEXT, reply_markup=back_markup(), disable_web_page_preview=True
            )
        elif data == "back":
            name = cq.from_user.mention if cq.from_user else "there"
            await cq.message.edit_text(
                start_text(name),
                reply_markup=start_markup(),
                disable_web_page_preview=True,
            )
        elif data == "close":
            try:
                await cq.message.delete()
            except Exception:
                pass
        await cq.answer()

    @app.on_message(filters.command("id"))
    async def on_id(_c: Client, m: Message):
        await m.reply_text(f"chat id: `{m.chat.id}`", quote=True)

    @app.on_message(
        filters.private
        & (filters.document | filters.video | filters.audio | filters.animation)
    )
    async def on_file(client: Client, m: Message):
        media = m.document or m.video or m.audio or m.animation
        if media is None:
            return

        if cfg.log_channel:
            # Channel mode: copy into the log channel for permanent storage.
            try:
                stored = await m.copy(cfg.log_channel)
            except Exception as e:
                log.exception("copy to log channel failed: %s", e)
                await m.reply_text(
                    "Couldn't store the file. Make sure the bot is an admin of "
                    "the LOG_CHANNEL with permission to post messages."
                )
                return
            chat_id = cfg.log_channel
            msg_id = stored.id
        else:
            # No-channel mode: stream straight from the message the user sent.
            chat_id = m.chat.id
            msg_id = m.id

        token = make_token(chat_id, msg_id, cfg.hash_secret)
        file_name = getattr(media, "file_name", None) or f"file_{msg_id}.mp4"
        url = f"{cfg.base_url}/stream/{chat_id}/{msg_id}/{quote(file_name)}?hash={token}"
        watch_url = f"{cfg.base_url}/watch/{chat_id}/{msg_id}/{quote(file_name)}?hash={token}"

        await m.reply_text(
            f"**File:** `{file_name}`\n"
            f"**Size:** {human_size(media.file_size)}\n\n"
            f"**Stream link:**\n`{url}`\n\n"
            f"Tap **▶️ Watch Now** to open directly in VLC, or paste the link "
            f"in VLC →  Media → Open Network Stream",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶️ Watch Now", url=watch_url)]]
            ),
            disable_web_page_preview=True,
            quote=True,
        )
