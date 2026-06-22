"""Weekly database backup to the Telegram LOG_CHANNEL.

Dumps the `users` and `payments` collections to a JSON file and uploads it as a
document to LOG_CHANNEL. A background loop runs the dump every Sunday (UTC), and
admins can trigger it on demand via /backup.
"""
import asyncio
import datetime as dt
import io
import json
import logging

log = logging.getLogger("backup")

# When the weekly backup fires (UTC). Sunday = weekday 6.
_BACKUP_WEEKDAY = 6
_BACKUP_HOUR = 0
_BACKUP_MINUTE = 0


def _json_default(obj):
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dt.date):
        return obj.isoformat()
    return str(obj)


async def build_backup_bytes(db) -> tuple[bytes, dict]:
    """Return (json_bytes, summary) for the current database contents."""
    users = await db.all_users_detailed()
    try:
        payments = await db.all_payments()
    except Exception:
        # Older DB instances may not implement all_payments.
        payments = []

    payload = {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "counts": {"users": len(users), "payments": len(payments)},
        "users": users,
        "payments": payments,
    }
    data = json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2)
    return data.encode("utf-8"), payload["counts"]


async def run_backup(bot, cfg, db) -> bool:
    """Create a backup file and send it to LOG_CHANNEL. Returns True on success."""
    if not cfg.log_channel:
        log.warning("Skipping backup: LOG_CHANNEL is not configured.")
        return False

    try:
        raw, counts = await build_backup_bytes(db)
    except Exception:
        log.exception("Failed to build database backup")
        return False

    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    buf = io.BytesIO(raw)
    buf.name = f"backup_{stamp}.json"

    caption = (
        "🗄 **Weekly Database Backup**\n"
        f"🗓 {dt.datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}\n"
        f"👥 Users: {counts['users']}\n"
        f"💳 Payments: {counts['payments']}"
    )
    try:
        await bot.send_document(cfg.log_channel, buf, caption=caption)
        log.info("Database backup sent to LOG_CHANNEL (%s users, %s payments)",
                 counts["users"], counts["payments"])
        return True
    except Exception:
        log.exception("Failed to upload database backup to LOG_CHANNEL")
        return False


def _seconds_until_next_backup(now: dt.datetime) -> float:
    """Seconds from `now` (UTC) until the next Sunday 00:00 UTC."""
    target_time = dt.time(hour=_BACKUP_HOUR, minute=_BACKUP_MINUTE)
    days_ahead = (_BACKUP_WEEKDAY - now.weekday()) % 7
    candidate = dt.datetime.combine(now.date(), target_time) + dt.timedelta(days=days_ahead)
    if candidate <= now:
        candidate += dt.timedelta(days=7)
    return (candidate - now).total_seconds()


async def weekly_backup_loop(bot, cfg, db) -> None:
    """Run forever: sleep until the next Sunday 00:00 UTC, then back up."""
    if not cfg.log_channel:
        log.warning("Weekly backup disabled: LOG_CHANNEL is not configured.")
        return
    while True:
        delay = _seconds_until_next_backup(dt.datetime.utcnow())
        log.info("Next database backup in %.1f hours", delay / 3600)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        await run_backup(bot, cfg, db)
        # Guard against firing twice within the same minute.
        await asyncio.sleep(60)
