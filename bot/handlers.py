import asyncio
import logging
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, UserNotParticipant
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


def _invite_link(cfg: Config) -> str:
    if cfg.force_sub_invite:
        return cfg.force_sub_invite
    return f"https://t.me/{cfg.force_sub.lstrip('@')}"


def fsub_markup(cfg: Config, file_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=_invite_link(cfg))],
            [InlineKeyboardButton("🔄 I've Joined", callback_data=f"checksub_{file_msg_id}")],
        ]
    )


async def is_subscribed(client: Client, cfg: Config, user_id: int) -> bool:
    """True if force-sub is off, or the user is a member of the channel."""
    if not cfg.force_sub:
        return True
    try:
        member = await client.get_chat_member(cfg.force_sub, user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except UserNotParticipant:
        return False
    except Exception:
        # Misconfig (e.g. bot not admin in channel) shouldn't lock everyone out.
        log.exception("force-sub check failed; allowing access")
        return True


async def send_stream_link(client: Client, cfg: Config, file_message: Message, reply_to: Message) -> None:
    """Generate and send the stream link + Watch Now button for a media message."""
    media = (
        file_message.document
        or file_message.video
        or file_message.audio
        or file_message.animation
    )
    if media is None:
        await reply_to.reply_text("That message has no streamable file.", quote=True)
        return

    if cfg.log_channel:
        try:
            stored = await file_message.copy(cfg.log_channel)
        except Exception as e:
            log.exception("copy to log channel failed: %s", e)
            await reply_to.reply_text(
                "Couldn't store the file. Make sure the bot is an admin of "
                "the LOG_CHANNEL with permission to post messages.",
                quote=True,
            )
            return
        chat_id = cfg.log_channel
        msg_id = stored.id
    else:
        chat_id = file_message.chat.id
        msg_id = file_message.id

    token = make_token(chat_id, msg_id, cfg.hash_secret)
    file_name = getattr(media, "file_name", None) or f"file_{msg_id}.mp4"
    url = f"{cfg.base_url}/stream/{chat_id}/{msg_id}/{quote(file_name)}?hash={token}"
    watch_url = f"{cfg.base_url}/watch/{chat_id}/{msg_id}/{quote(file_name)}?hash={token}"

    await reply_to.reply_text(
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


def register_handlers(app: Client, cfg: Config, db) -> None:
    @app.on_message(filters.command("start") & filters.private)
    async def on_start(_c: Client, m: Message):
        if m.from_user:
            await db.add_user(
                m.from_user.id, m.from_user.username, m.from_user.first_name
            )
        name = m.from_user.mention if m.from_user else "there"
        await m.reply_text(
            start_text(name),
            reply_markup=start_markup(),
            disable_web_page_preview=True,
            quote=True,
        )

    @app.on_message(filters.command("stats") & filters.private)
    async def on_stats(_c: Client, m: Message):
        if not m.from_user or m.from_user.id not in cfg.admins:
            return
        total = await db.count()
        await m.reply_text(f"📊 **Total users:** {total}", quote=True)

    @app.on_message(filters.command("users") & filters.private)
    async def on_users(_c: Client, m: Message):
        if not m.from_user or m.from_user.id not in cfg.admins:
            uid = m.from_user.id if m.from_user else "unknown"
            await m.reply_text(
                f"🚫 You are not an admin.\nYour ID: `{uid}`", quote=True
            )
            return
        users = await db.all_users_detailed()
        if not users:
            await m.reply_text("No users yet.", quote=True)
            return
        lines = []
        for u in users:
            uid = u.get("_id")
            uname = u.get("username")
            fname = u.get("first_name") or ""
            handle = f"@{uname}" if uname else "(no username)"
            lines.append(f"• `{uid}` — {handle} {fname}".strip())
        text = "👥 **Users**\n\n" + "\n".join(lines)
        # Telegram messages cap at 4096 chars; send as a file if too long.
        if len(text) > 4000:
            import io
            buf = io.BytesIO("\n".join(lines).encode())
            buf.name = "users.txt"
            await m.reply_document(buf, caption=f"👥 {len(users)} users", quote=True)
        else:
            await m.reply_text(text, quote=True)

    @app.on_message(filters.command("broadcast") & filters.private)
    async def on_broadcast(_c: Client, m: Message):
        if not m.from_user or m.from_user.id not in cfg.admins:
            uid = m.from_user.id if m.from_user else "unknown"
            await m.reply_text(
                f"🚫 You are not an admin.\nYour ID: `{uid}`", quote=True
            )
            return
        if not m.reply_to_message:
            await m.reply_text(
                "Reply to a message with /broadcast to send it to all users.",
                quote=True,
            )
            return

        users = await db.all_users()
        status = await m.reply_text(f"📢 Broadcasting to {len(users)} users...", quote=True)
        sent = failed = 0
        for uid in users:
            try:
                await m.reply_to_message.copy(uid)
                sent += 1
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
                try:
                    await m.reply_to_message.copy(uid)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await status.edit_text(
            f"📢 **Broadcast done.**\n✅ Sent: {sent}\n❌ Failed: {failed}"
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
        elif data.startswith("checksub_"):
            file_msg_id = int(data.split("_", 1)[1])
            if not await is_subscribed(_c, cfg, cq.from_user.id):
                await cq.answer(
                    "❌ You haven't joined yet. Please join the channel first.",
                    show_alert=True,
                )
                return
            await cq.answer("✅ Verified!")
            try:
                file_message = await _c.get_messages(cq.from_user.id, file_msg_id)
            except Exception:
                file_message = None
            try:
                await cq.message.delete()
            except Exception:
                pass
            if file_message and not file_message.empty:
                await send_stream_link(_c, cfg, file_message, file_message)
            else:
                await _c.send_message(cq.from_user.id, "Please send the file again.")
            return
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
        if m.from_user:
            await db.add_user(
                m.from_user.id, m.from_user.username, m.from_user.first_name
            )

        # Force-subscribe gate.
        user_id = m.from_user.id if m.from_user else 0
        if not await is_subscribed(client, cfg, user_id):
            await m.reply_text(
                "🚫 **Access Denied**\n\n"
                "Please join our channel to use this bot.\n"
                "After joining, tap **🔄 I've Joined** and I'll send your link.",
                reply_markup=fsub_markup(cfg, m.id),
                quote=True,
            )
            return

        await send_stream_link(client, cfg, m, m)
