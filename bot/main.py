import asyncio
import logging
import signal

from aiohttp import web
from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import BotCommand

# uvloop is a faster asyncio event loop (Linux/macOS). Optional: ignored on
# platforms where it isn't installed (e.g. Windows dev machines).
try:
    import uvloop

    uvloop.install()
except Exception:
    pass

# Telegram now issues channel IDs larger than Pyrogram 2.0.106's built-in
# valid range, which makes it reject them with "Peer id invalid". Widen the
# lower bound so modern -100... channel IDs resolve correctly.
import pyrogram.utils as _pyro_utils

_pyro_utils.MIN_CHANNEL_ID = -100_999_999_999_999

from .config import load_config
from .db import make_user_db
from .handlers import register_handlers
from .server import make_app


BOT_COMMANDS = [
    BotCommand("start", "Check if the bot is alive"),
    BotCommand("help", "How to use the bot"),
    BotCommand("about", "Know about the bot"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

# Cap how long we'll obey a single FloodWait so we don't hang forever.
MAX_FLOOD_WAIT = 3600


async def _start_bot_with_retry(bot: Client) -> None:
    """Start the bot, respecting Telegram FloodWait instead of crash-looping.

    The HTTP server is already up by the time this runs, so health checks keep
    passing while we wait out any rate limit. This prevents the crash/restart
    loop that itself causes repeated re-authentication and more FloodWaits.
    """
    while True:
        try:
            await bot.start()
            me = await bot.get_me()
            log.info("Bot logged in as @%s (id=%s)", me.username, me.id)
            return
        except FloodWait as e:
            wait = min(int(e.value), MAX_FLOOD_WAIT)
            log.warning(
                "FloodWait from Telegram: waiting %ss before retrying bot login. "
                "This is a temporary rate limit; the server stays healthy meanwhile.",
                wait,
            )
            await asyncio.sleep(wait + 1)
        except Exception:
            log.exception("Bot login failed, retrying in 15s")
            await asyncio.sleep(15)


async def run() -> None:
    cfg = load_config()

    client_kwargs = dict(
        name="vlc_stream_bot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        in_memory=True,
        sleep_threshold=60,
        max_concurrent_transmissions=cfg.workers,
    )
    # A session string lets restarts reuse an existing login instead of
    # re-authenticating every time (which can trigger FloodWait).
    if cfg.session_string:
        client_kwargs["session_string"] = cfg.session_string
    else:
        client_kwargs["bot_token"] = cfg.bot_token

    bot = Client(**client_kwargs)
    db = make_user_db(cfg.database_url)
    register_handlers(bot, cfg, db)

    # Optional extra bot clients used to parallelize streaming across many
    # users. They can only read files in a shared LOG_CHANNEL, so they require
    # LOG_CHANNEL to be set and every worker bot added to that channel.
    workers = []
    for idx, token in enumerate(cfg.worker_tokens, start=1):
        workers.append(
            Client(
                name=f"vlc_worker_{idx}",
                api_id=cfg.api_id,
                api_hash=cfg.api_hash,
                bot_token=token,
                in_memory=True,
                sleep_threshold=60,
                max_concurrent_transmissions=cfg.workers,
            )
        )

    # 1) Start the HTTP server FIRST so Koyeb health checks pass immediately,
    #    even if the bot login is briefly delayed by a FloodWait.
    clients = [bot] + workers
    app = make_app(bot, cfg, clients)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bind_host, cfg.port)
    await site.start()
    log.info("HTTP server listening on %s:%d  base_url=%s", cfg.bind_host, cfg.port, cfg.base_url)

    # 2) Log the bot in, retrying on FloodWait without taking the server down.
    await _start_bot_with_retry(bot)

    # Start worker clients (best-effort; a failed worker just isn't used).
    for w in workers:
        try:
            await _start_bot_with_retry(w)
        except Exception:
            log.exception("A worker client failed to start; continuing")
    if workers:
        log.info("Started %d worker client(s) for parallel streaming", len(workers))

    # Register the slash-command menu shown when users type "/".
    try:
        await bot.set_bot_commands(BOT_COMMANDS)
    except Exception:
        log.exception("Failed to set bot commands (non-fatal)")

    stop_event = asyncio.Event()

    def _on_signal():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGTERM
            pass

    try:
        await stop_event.wait()
    finally:
        log.info("Stopping HTTP server and bot")
        await runner.cleanup()
        for client in clients:
            try:
                await client.stop()
            except Exception:
                pass


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
