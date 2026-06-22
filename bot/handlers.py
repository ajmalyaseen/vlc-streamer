import asyncio
import datetime as dt
import html
import logging
import os
from urllib.parse import quote

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import plans as plansmod
from .config import Config
from .utils import human_size, make_token
from .backup import run_backup

log = logging.getLogger("handlers")

DEVELOPER = "Ajmal Yaseen"
CHANNEL_LINK = "https://t.me/alaska_in"
VERSION = "v2.0.0"

PLAN_RANK = {"free": 0, "plus": 1, "pro": 2}

# --- UI theme -------------------------------------------------------------
# Clean, card-style windows: a <blockquote> header (the vertical-bar look) plus
# "❄ Key : Value" lines. HTML parse mode is used for every styled window.
HTML = ParseMode.HTML
SNOW = "❄"


def _bq(text: str) -> str:
    """A blockquote header — gives the framed, card-like title look."""
    return f"<blockquote>{text}</blockquote>"


HELP_TEXT = (
    f"{_bq('💡 HELP')}\n\n"
    f"{SNOW} Send me a video file (MP4 / MKV)\n"
    f"{SNOW} I reply with a direct streaming link\n"
    f"{SNOW} Open VLC → Media → Open Network Stream\n"
    f"{SNOW} Paste the link and play\n\n"
    f"{_bq('<i>Premium unlocks larger files, more daily links and longer link validity.</i>')}"
)

ABOUT_TEXT = (
    f"{_bq('📕 BOT INFO')}\n\n"
    f"{SNOW} <b>Bot Name</b> : Alaska Stream\n"
    f"{SNOW} <b>Framework</b> : Pyrogram\n"
    f"{SNOW} <b>Language</b> : Python\n"
    f"{SNOW} <b>Version</b> : {VERSION}\n"
    f"{SNOW} <b>Source</b> : Private\n"
    f"{SNOW} <b>Developer</b> : {DEVELOPER}"
)


def welcome_text(name: str) -> str:
    safe = html.escape(name or "there")
    return (
        f"{_bq(f'👋 Hai {safe}')}\n\n"
        "I turn your Telegram files into direct <b>VLC streaming links</b>.\n"
        "Send me any video (MP4 / MKV) and get a link instantly.\n\n"
        f"{_bq('<i>For more info check 💡 HELP</i>')}\n\n"
        f"{SNOW} Maintained by <a href=\"{CHANNEL_LINK}\">Alaska</a>"
    )


def welcome_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💎 Premium Plans", callback_data="menu_plans")],
            [
                InlineKeyboardButton("📊 My Plan", callback_data="menu_myplan"),
                InlineKeyboardButton("💡 Help", callback_data="help"),
            ],
            [
                InlineKeyboardButton("📢 Updates", url=CHANNEL_LINK),
                InlineKeyboardButton("📕 About", callback_data="about"),
            ],
            [InlineKeyboardButton("🔐 Close", callback_data="close")],
        ]
    )


def dashboard_text(state) -> str:
    return (
        f"{_bq('⚙️ Manage Plan')}\n\n"
        f"{SNOW} <b>Plan</b> : {state.plan.name}\n"
        f"{SNOW} <b>Today</b> : {state.used_today} / {state.plan.daily_links} links used"
    )


def dashboard_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💎 Premium Plans", callback_data="menu_plans")],
            [InlineKeyboardButton("📊 My Plan", callback_data="menu_myplan")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_home")],
        ]
    )


def back_home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back", callback_data="menu_home")]]
    )


def about_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💎 Premium Plans", callback_data="menu_plans")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_home")],
        ]
    )


def _expiry_str(expires_at) -> str:
    if not expires_at:
        return "—"
    return expires_at.strftime("%d %b %Y")


def myplan_text(state) -> str:
    p = state.plan
    lines = [
        f"{_bq('📊 MY PLAN')}\n",
        f"{SNOW} <b>Plan</b> : {p.emoji} {p.name}",
        f"{SNOW} <b>Today</b> : {state.used_today} / {p.daily_links} links",
    ]
    if p.key != "free":
        lines.append(f"{SNOW} <b>Valid Until</b> : {_expiry_str(state.expires_at)}")
    lines.append(f"{SNOW} <b>Max File Size</b> : {human_size(p.max_file_size)}")
    lines.append(f"{SNOW} <b>Link Validity</b> : {plansmod._expiry_text(p.expiry_seconds)}")
    return "\n".join(lines)


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
    if not cfg.force_sub:
        return True
    try:
        member = await client.get_chat_member(cfg.force_sub, user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except UserNotParticipant:
        return False
    except Exception:
        log.exception("force-sub check failed; allowing access")
        return True


async def send_stream_link(client, cfg, subs, file_message, reply_to, plan) -> bool:
    """Store/locate the media and reply with a stream link. Returns True on success."""
    media = (
        file_message.document
        or file_message.video
        or file_message.audio
        or file_message.animation
    )
    if media is None:
        await reply_to.reply_text("That message has no streamable file.", quote=True)
        return False

    if cfg.log_channel:
        try:
            u = getattr(file_message, "from_user", None)
            uname = f"@{u.username}" if (u and u.username) else "—"
            ucap = (
                "📥 **New file streamed**\n"
                f"👤 Name: {u.first_name if u else '—'}\n"
                f"🔗 Username: {uname}\n"
                f"🆔 User ID: `{u.id if u else '—'}`"
            )
            stored = await file_message.copy(cfg.log_channel, caption=ucap)
        except Exception as e:
            log.exception("copy to log channel failed: %s", e)
            await reply_to.reply_text(
                "Couldn't store the file. Make sure the bot is an admin of "
                "the LOG_CHANNEL with permission to post messages.",
                quote=True,
            )
            return False
        chat_id = cfg.log_channel
        msg_id = stored.id
    else:
        chat_id = file_message.chat.id
        msg_id = file_message.id

    exp = int(dt.datetime.utcnow().timestamp()) + plan.expiry_seconds
    token = make_token(chat_id, msg_id, cfg.hash_secret, exp)
    file_name = getattr(media, "file_name", None) or f"file_{msg_id}.mp4"
    q = quote(file_name)
    uid = getattr(getattr(file_message, "from_user", None), "id", 0) or 0
    url = f"{cfg.base_url}/stream/{chat_id}/{msg_id}/{q}?hash={token}&exp={exp}&uid={uid}"
    watch_url = f"{cfg.base_url}/watch/{chat_id}/{msg_id}/{q}?hash={token}&exp={exp}&uid={uid}"

    await reply_to.reply_text(
        f"{_bq('✅ STREAM LINK READY')}\n\n"
        f"{SNOW} <b>File</b> : <code>{html.escape(file_name)}</code>\n"
        f"{SNOW} <b>Size</b> : {human_size(media.file_size)}\n"
        f"{SNOW} <b>Plan</b> : {plan.emoji} {plan.name}\n"
        f"{SNOW} <b>Valid</b> : {plansmod._expiry_text(plan.expiry_seconds)}\n\n"
        f"<code>{html.escape(url)}</code>\n\n"
        "Tap <b>▶ Watch Now</b> to open in VLC, or paste the link in "
        "VLC → Media → Open Network Stream.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶ Watch Now", url=watch_url)]]
        ),
        parse_mode=HTML,
        disable_web_page_preview=True,
        quote=True,
    )
    return True


# Local fallback locations for the /start banner when START_IMAGE isn't set.
_START_IMAGE_CANDIDATES = (
    os.path.join(os.path.dirname(__file__), "assets", "start.jpg"),
    os.path.join(os.path.dirname(__file__), "assets", "start.png"),
)


def _resolve_start_image(cfg: Config):
    """Return a URL or local path for the /start image, or None to use text."""
    if cfg.start_image:
        return cfg.start_image  # explicit URL or file path from .env
    for path in _START_IMAGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def register_handlers(app: Client, cfg: Config, db, subs, payments, plans, monitor=None) -> None:

    def _is_admin(user) -> bool:
        return bool(user) and user.id in cfg.admins

    async def _nav(cq: CallbackQuery, text: str, markup=None) -> None:
        """Edit a menu in place, handling both text and photo (banner) messages.

        /start sends a photo banner with the menu attached, so navigation must
        edit the caption on media messages; plain text messages use edit_text.
        All styled windows use HTML parse mode."""
        msg = cq.message
        is_media = bool(msg.photo or msg.video or msg.document or msg.animation)
        try:
            if is_media:
                await msg.edit_caption(caption=text, reply_markup=markup, parse_mode=HTML)
            else:
                await msg.edit_text(text, reply_markup=markup, parse_mode=HTML,
                                    disable_web_page_preview=True)
        except Exception:
            # Fallback: send a fresh message if the edit can't be applied.
            await msg.reply_text(text, reply_markup=markup, parse_mode=HTML,
                                 disable_web_page_preview=True)

    # ---------------- user menus ----------------

    @app.on_message(filters.command("start") & filters.private)
    async def on_start(_c: Client, m: Message):
        await subs.get_state(m.from_user)  # ensure user + lazy refresh
        name = m.from_user.first_name if m.from_user else "there"
        caption = welcome_text(name)
        markup = welcome_markup()
        image = _resolve_start_image(cfg)
        if image:
            try:
                await m.reply_photo(image, caption=caption, reply_markup=markup,
                                    parse_mode=HTML, quote=True)
                return
            except Exception:
                log.exception("start image send failed; falling back to text")
        await m.reply_text(
            caption,
            reply_markup=markup,
            parse_mode=HTML,
            disable_web_page_preview=True,
            quote=True,
        )

    @app.on_message(filters.command("plans") & filters.private)
    async def on_plans(_c: Client, m: Message):
        await subs.get_state(m.from_user)  # lazy refresh
        await m.reply_text(
            plansmod.format_plans_text(plans),
            reply_markup=plansmod.buy_markup(),
            parse_mode=HTML,
            disable_web_page_preview=True,
            quote=True,
        )

    @app.on_message(filters.command("myplan") & filters.private)
    async def on_myplan(_c: Client, m: Message):
        state = await subs.get_state(m.from_user)
        await m.reply_text(myplan_text(state), reply_markup=back_home_markup(),
                           parse_mode=HTML, quote=True)

    @app.on_message(filters.command("help") & filters.private)
    async def on_help(_c: Client, m: Message):
        await m.reply_text(HELP_TEXT, reply_markup=about_markup(), parse_mode=HTML,
                           disable_web_page_preview=True, quote=True)

    @app.on_message(filters.command("about") & filters.private)
    async def on_about(_c: Client, m: Message):
        await m.reply_text(ABOUT_TEXT, reply_markup=about_markup(), parse_mode=HTML,
                           disable_web_page_preview=True, quote=True)

    @app.on_message(filters.command("id"))
    async def on_id(_c: Client, m: Message):
        await m.reply_text(f"chat id: `{m.chat.id}`", quote=True)

    @app.on_message(filters.command("fileid") & filters.private)
    async def on_fileid(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        # Accept a photo either replied-to or sent directly with /fileid as caption.
        src = m.reply_to_message or m
        media = src.photo or src.document or src.video or src.animation
        if media is None:
            await m.reply_text(
                "Reply to a photo with `/fileid` (or send a photo with `/fileid` "
                "as the caption) and I'll return its file_id.",
                quote=True,
            )
            return
        await m.reply_text(
            f"**file_id**\n`{media.file_id}`\n\n"
            "Set this as `START_IMAGE` in your .env to use it as the /start banner.",
            quote=True,
        )

    # ---------------- admin commands ----------------

    @app.on_message(filters.command("stats") & filters.private)
    async def on_stats(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        c = await subs.analytics()
        await m.reply_text(
            f"📊 **Stats**\n\nTotal: {c['total']}\n🆓 Free: {c['free']}\n"
            f"⭐ Plus: {c['plus']}\n🚀 Pro: {c['pro']}",
            quote=True,
        )

    @app.on_message(filters.command("plans_stats") & filters.private)
    async def on_plans_stats(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        c = await subs.analytics()
        paid = c["plus"] + c["pro"]
        revenue = c["plus"] * cfg.plus_price + c["pro"] * cfg.pro_price
        await m.reply_text(
            f"📈 **Subscription Analytics**\n\n"
            f"Total users: {c['total']}\nPaid users: {paid}\n"
            f"⭐ Plus: {c['plus']} (₹{c['plus'] * cfg.plus_price})\n"
            f"🚀 Pro: {c['pro']} (₹{c['pro'] * cfg.pro_price})\n"
            f"Est. monthly revenue: ₹{revenue}",
            quote=True,
        )

    @app.on_message(filters.command("backup") & filters.private)
    async def on_backup(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        if not cfg.log_channel:
            await m.reply_text("⚠️ LOG_CHANNEL is not configured; cannot back up.", quote=True)
            return
        status = await m.reply_text("🗄 Creating database backup...", quote=True)
        ok = await run_backup(_c, cfg, db)
        if ok:
            await status.edit_text("✅ Backup sent to the log channel.")
        else:
            await status.edit_text("❌ Backup failed. Check the logs.")

    @app.on_message(filters.command("streamusers") & filters.private)
    async def on_streamusers(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        if monitor is None:
            await m.reply_text("Stream monitor not available.", quote=True)
            return
        watching = monitor.current()
        if not watching:
            await m.reply_text("📺 No one is streaming right now.", quote=True)
            return
        lines = []
        for w in watching:
            uid = w.get("uid")
            name, uname = "—", ""
            if uid:
                rec = await db.get_user(uid) or {}
                name = rec.get("first_name") or "—"
                uname = f"@{rec['username']}" if rec.get("username") else ""
            uid_str = f"`{uid}`" if uid else "—"
            lines.append(f"• {name} {uname} ({uid_str})\n   🎬 {w.get('file_name')}")
        await m.reply_text(
            f"📺 **Now Streaming: {len(watching)}**\n\n" + "\n".join(lines),
            quote=True, disable_web_page_preview=True,
        )

    @app.on_message(filters.command("users") & filters.private)
    async def on_users(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        users = await db.all_users_detailed()
        total = len(users)
        # newest first (created_at may be missing for older records)
        users_sorted = sorted(
            users, key=lambda u: u.get("created_at") or dt.datetime.min, reverse=True
        )
        lines = []
        for u in users_sorted[:20]:
            name = u.get("first_name") or "—"
            uname = f"@{u['username']}" if u.get("username") else ""
            plan = (u.get("plan") or "free").title()
            lines.append(f"• `{u['_id']}` {name} {uname} — {plan}")
        body = "\n".join(lines) if lines else "No users yet."
        await m.reply_text(
            f"👥 **Users: {total}**\n\nLatest 20:\n{body}",
            quote=True, disable_web_page_preview=True,
        )

    @app.on_message(filters.command("user") & filters.private)
    async def on_user(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        if len(m.command) < 2:
            await m.reply_text("Usage: `/user USER_ID`", quote=True)
            return
        try:
            uid = int(m.command[1])
        except ValueError:
            await m.reply_text("Invalid user id.", quote=True)
            return
        state = await subs.get_state(uid)
        await m.reply_text(
            f"👤 **User {uid}**\nPlan: {state.plan.name}\n"
            f"Expiry: {_expiry_str(state.expires_at)}\n"
            f"Today: {state.used_today}/{state.plan.daily_links}",
            quote=True,
        )

    @app.on_message(filters.command("addplan") & filters.private)
    async def on_addplan(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        args = m.command[1:]
        # Accept user id + (plan and days in ANY order):
        #   /addplan USER_ID plus 30   OR   /addplan USER_ID 30 plus
        if len(args) < 3:
            await m.reply_text(
                "Usage: `/addplan USER_ID PLAN DAYS`\n"
                "Example: `/addplan 1853251761 plus 30`\n"
                "(plan and days can be in any order)",
                quote=True,
            )
            return
        try:
            uid = int(args[0])
        except ValueError:
            await m.reply_text("Invalid user id. It must be a number.", quote=True)
            return
        rest = [a.lower() for a in args[1:]]
        plan_key = next((a for a in rest if a in ("plus", "pro")), None)
        days = next((int(a) for a in rest if a.isdigit()), None)
        if plan_key is None:
            await m.reply_text("Plan must be `plus` or `pro`.", quote=True)
            return
        if days is None or days <= 0:
            await m.reply_text("Days must be a positive number, e.g. `30`.", quote=True)
            return
        expires = await subs.set_plan(uid, plan_key, days)
        await m.reply_text(
            f"✅ Set user `{uid}` to **{plan_key}** for **{days} days** "
            f"(until {_expiry_str(expires)}).",
            quote=True,
        )
        try:
            await app.send_message(
                uid, f"🎉 You've been granted **{plan_key.title()}** for {days} days!"
            )
        except Exception:
            log.info("Could not DM user %s about granted plan (they may not have started the bot)", uid)

    @app.on_message(filters.command("removeplan") & filters.private)
    async def on_removeplan(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        if len(m.command) < 2:
            await m.reply_text("Usage: `/removeplan USER_ID`", quote=True)
            return
        try:
            uid = int(m.command[1])
        except ValueError:
            await m.reply_text("Invalid user id.", quote=True)
            return
        await subs.remove_plan(uid)
        await m.reply_text(f"✅ User {uid} reverted to Free.", quote=True)

    @app.on_message(filters.command("extend") & filters.private)
    async def on_extend(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        if len(m.command) < 3:
            await m.reply_text("Usage: `/extend USER_ID DAYS`", quote=True)
            return
        try:
            uid, days = int(m.command[1]), int(m.command[2])
        except ValueError:
            await m.reply_text("Invalid arguments.", quote=True)
            return
        expires = await subs.extend_plan(uid, days)
        await m.reply_text(f"✅ Extended user {uid} until {_expiry_str(expires)}.", quote=True)

    @app.on_message(filters.command("broadcast") & filters.private)
    async def on_broadcast(_c: Client, m: Message):
        if not _is_admin(m.from_user):
            return
        if not m.reply_to_message:
            await m.reply_text("Reply to a message with /broadcast to send it to all users.", quote=True)
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
        await status.edit_text(f"📢 **Broadcast done.**\n✅ Sent: {sent}\n❌ Failed: {failed}")

    # ---------------- callbacks ----------------

    async def _start_payment(cq: CallbackQuery, plan_key: str):
        plan = plans[plan_key]
        # Safety guard: block if user already has this plan or a higher one
        # (e.g. an old button left in chat history).
        state = await subs.get_state(cq.from_user)
        if PLAN_RANK.get(state.plan.key, 0) >= PLAN_RANK.get(plan_key, 0):
            msg = (
                "You're already on Pro — our highest plan."
                if state.plan.key == "pro"
                else f"You're already on the {plan.name} plan."
            )
            await cq.message.reply_text(
                f"{_bq('ℹ️ NOTICE')}\n\n{msg}\n\n<i>If you face any issue, contact support.</i>",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("💬 Contact Support", url=cfg.support_link)]]
                ),
                parse_mode=HTML,
            )
            return
        if not payments.enabled:
            await cq.message.reply_text("Payments aren't enabled yet. Please contact support.")
            return
        res = await payments.start_purchase(cq.from_user, plan_key)
        if res.blocked:
            await cq.message.reply_text(
                f"{_bq('⏳ REQUEST PENDING')}\n\n"
                "You have a payment request awaiting verification. "
                "Please wait for an admin to review it before creating a new one.",
                parse_mode=HTML,
            )
            return
        if res.error:
            await cq.message.reply_text("Couldn't start payment right now. Please try again later.")
            return

        if res.provider == "razorpay":
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(f"💳 Pay ₹{res.amount}", url=res.pay_url)],
                    [InlineKeyboardButton("💬 Contact Support", url=cfg.support_link)],
                ]
            )
            await cq.message.reply_text(
                f"{_bq(f'{plan.emoji} {plan.name.upper()} · ₹{res.amount}')}\n\n"
                f"{SNOW} Secure checkout (UPI / Card / Wallet / Netbanking)\n"
                f"{SNOW} Plan activates automatically on success\n\n"
                f"{_bq('<i>If you face any issue, contact support.</i>')}",
                reply_markup=markup, parse_mode=HTML, disable_web_page_preview=True,
            )
            return

        # manual UPI fallback
        pay_page = (
            f"{cfg.base_url}/pay?pa={quote(cfg.upi_id)}&pn=Alaska%20Stream"
            f"&am={res.amount}&tn={quote(res.reference)}"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Open UPI App", url=pay_page)],
            [InlineKeyboardButton("💬 Contact Support", url=cfg.support_link)],
        ])
        await cq.message.reply_text(
            f"{_bq(f'{plan.emoji} PAY ₹{res.amount} · {plan.name.upper()}')}\n\n"
            f"{SNOW} <b>Reference</b> : <code>{res.reference}</code>\n"
            f"{SNOW} <b>UPI ID</b> : <code>{html.escape(cfg.upi_id)}</code>\n\n"
            "Tap <b>Open UPI App</b> to pay with the amount pre-filled, "
            "or pay manually to the UPI ID above.\n\n"
            "After paying, send the <b>last 4 digits of your UTR</b> here to verify.\n\n"
            f"{_bq('<i>If you face any issue, contact support.</i>')}",
            reply_markup=markup, parse_mode=HTML, disable_web_page_preview=True,
        )

    @app.on_callback_query()
    async def on_callback(_c: Client, cq: CallbackQuery):
        data = cq.data or ""

        if data == "menu_home":
            name = cq.from_user.first_name if cq.from_user else "there"
            await _nav(cq, welcome_text(name), welcome_markup())
        elif data == "menu_manage":
            state = await subs.get_state(cq.from_user)
            await _nav(cq, dashboard_text(state), dashboard_markup())
        elif data in ("menu_plans", "plans"):
            await _nav(cq, plansmod.format_plans_text(plans), plansmod.buy_markup())
        elif data == "menu_myplan":
            state = await subs.get_state(cq.from_user)
            await _nav(cq, myplan_text(state), back_home_markup())
        elif data == "help":
            await _nav(cq, HELP_TEXT, about_markup())
        elif data == "about":
            await _nav(cq, ABOUT_TEXT, about_markup())
        elif data in ("buy_plus", "buy_pro"):
            plan_key = "plus" if data == "buy_plus" else "pro"
            state = await subs.get_state(cq.from_user)
            current = state.plan.key
            # Already on the highest plan: nothing to buy.
            if current == "pro":
                await _nav(
                    cq,
                    f"{_bq('🚀 ALREADY ON PRO')}\n\n"
                    "You're on our highest plan — there's nothing higher to upgrade to.\n"
                    "If you face any issue, reach out to support.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Contact Support", url=cfg.support_link)],
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_plans")],
                    ]),
                )
                return
            # Already on the plan they're trying to buy again.
            if current == plan_key:
                await _nav(
                    cq,
                    f"{_bq(f'⭐ ALREADY ON {plans[plan_key].name.upper()}')}\n\n"
                    "Upgrade to Pro for higher limits, or contact support "
                    "if you face any issue.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Upgrade to Pro", callback_data="buy_pro")],
                        [InlineKeyboardButton("💬 Contact Support", url=cfg.support_link)],
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_plans")],
                    ]),
                )
                return
            # Allowed: free→plus, free→pro, plus→pro.
            plan = plans[plan_key]
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Pay ₹{plan.price}", callback_data=f"pay_{plan_key}")],
                [InlineKeyboardButton("💬 Contact Support", url=cfg.support_link)],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_plans")],
            ])
            text = (
                plansmod.purchase_text(plan)
                + f"\n\n{_bq('<i>If you face any issue, contact support.</i>')}"
            )
            await _nav(cq, text, markup)
        elif data in ("pay_plus", "pay_pro"):
            await cq.answer()
            await _start_payment(cq, "plus" if data == "pay_plus" else "pro")
        elif data.startswith("approve_") or data.startswith("reject_"):
            if not _is_admin(cq.from_user):
                await cq.answer("Not authorized.", show_alert=True)
                return
            action, ref = data.split("_", 1)
            if action == "approve":
                result = await payments.approve(ref, cq.from_user.id)
                if not result:
                    await cq.answer("Already processed.", show_alert=True)
                    return
                plan = plans[result["plan"]]
                await cq.message.edit_text(
                    cq.message.text + f"\n\n✅ Approved by {cq.from_user.first_name}"
                )
                try:
                    await app.send_message(
                        result["user_id"],
                        f"{_bq('✅ PAYMENT VERIFIED')}\n\n"
                        f"{plan.emoji} <b>{plan.name} Plan activated</b>\n"
                        f"{SNOW} <b>Valid Until</b> : {_expiry_str(result['expires'])}\n\n"
                        f"{plansmod.benefits_text(plan)}\n\n"
                        f"{_bq('<i>Thank you for supporting Alaska.</i>')}",
                        parse_mode=HTML,
                    )
                except Exception:
                    log.exception("notify approve failed")
                await cq.answer("Approved")
            else:
                result = await payments.reject(ref, cq.from_user.id)
                if not result:
                    await cq.answer("Already processed.", show_alert=True)
                    return
                await cq.message.edit_text(
                    cq.message.text + f"\n\n❌ Rejected by {cq.from_user.first_name}"
                )
                try:
                    await app.send_message(
                        result["user_id"],
                        f"{_bq('❌ VERIFICATION FAILED')}\n\n"
                        "Please check your payment details and try again. "
                        "If you believe this is a mistake, contact support.",
                        parse_mode=HTML,
                    )
                except Exception:
                    log.exception("notify reject failed")
                await cq.answer("Rejected")
        elif data == "close":
            try:
                await cq.message.delete()
            except Exception:
                pass
        elif data.startswith("checksub_"):
            file_msg_id = int(data.split("_", 1)[1])
            if not await is_subscribed(_c, cfg, cq.from_user.id):
                await cq.answer("❌ You haven't joined yet. Please join the channel first.",
                                show_alert=True)
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
                await _process_file(_c, file_message, cq.from_user, file_message)
            else:
                await _c.send_message(cq.from_user.id, "Please send the file again.")
            return
        await cq.answer()

    # ---------------- file handling with enforcement ----------------

    async def _process_file(client, file_message, user, reply_to):
        media = (file_message.document or file_message.video
                 or file_message.audio or file_message.animation)
        if media is None:
            return
        decision = await subs.can_generate(user, media.file_size)
        plan = decision.plan
        if not decision.ok and decision.reason == "file_too_big":
            await reply_to.reply_text(
                f"{_bq('⚠️ FILE TOO LARGE')}\n\n"
                f"{SNOW} <b>Your Plan</b> : {plan.name}\n"
                f"{SNOW} <b>Max Allowed</b> : {human_size(plan.max_file_size)}\n\n"
                f"{_bq('<i>Upgrade to stream larger files.</i>')}",
                reply_markup=plansmod.upgrade_markup(), parse_mode=HTML, quote=True,
            )
            return
        if not decision.ok and decision.reason == "daily_limit":
            await reply_to.reply_text(
                f"{_bq('⚠️ DAILY LIMIT REACHED')}\n\n"
                f"{SNOW} <b>Plan</b> : {plan.name}\n"
                f"{SNOW} <b>Used</b> : {plan.daily_links}/{plan.daily_links} links today\n\n"
                f"{_bq('<i>Upgrade to keep generating links.</i>')}",
                reply_markup=plansmod.upgrade_markup(), parse_mode=HTML, quote=True,
            )
            return
        ok = await send_stream_link(client, cfg, subs, file_message, reply_to, plan)
        if ok:
            await subs.record_link(user.id)

    @app.on_message(
        filters.private
        & (filters.document | filters.video | filters.audio | filters.animation)
    )
    async def on_file(client: Client, m: Message):
        media = m.document or m.video or m.audio or m.animation
        if media is None:
            return
        await subs.get_state(m.from_user)  # ensure user + lazy refresh
        user_id = m.from_user.id if m.from_user else 0
        if not await is_subscribed(client, cfg, user_id):
            await m.reply_text(
                f"{_bq('🔒 ACCESS REQUIRED')}\n\n"
                "Please join our channel to use this bot.\n"
                "After joining, tap <b>I've Joined</b> and I'll send your link.",
                reply_markup=fsub_markup(cfg, m.id), parse_mode=HTML, quote=True,
            )
            return
        await _process_file(client, m, m.from_user, m)

    # ---------------- UTR capture (must be registered last) ----------------

    @app.on_message(filters.private & filters.text & ~filters.via_bot)
    async def on_text(client: Client, m: Message):
        if not m.text or m.text.startswith("/"):
            return
        pending = await db.get_pending_payment(m.from_user.id)
        if not pending or pending["status"] != "awaiting_utr":
            return
        utr = m.text.strip()
        payment = await payments.submit_utr(m.from_user, utr)
        if payment is None:
            await m.reply_text("Please send exactly the **last 4 digits** of your UTR (e.g. `6451`).",
                               quote=True)
            return
        await payments.post_to_admins(payment)
        await m.reply_text(
            f"{_bq('⏳ PAYMENT SUBMITTED')}\n\n"
            f"{SNOW} <b>Plan</b> : {payment['plan'].title()}\n"
            f"{SNOW} <b>Reference</b> : <code>{payment['_id']}</code>\n"
            f"{SNOW} <b>UTR</b> : ****{payment['utr_last4']}\n\n"
            f"{_bq('<i>An admin will verify your payment shortly.</i>')}",
            parse_mode=HTML,
            quote=True,
        )
