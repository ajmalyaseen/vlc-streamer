import asyncio
import logging
import signal

from aiohttp import web
from pyrogram import Client

from .config import load_config
from .handlers import register_handlers
from .server import make_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def run() -> None:
    cfg = load_config()

    bot = Client(
        name="vlc_stream_bot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        in_memory=True,
        sleep_threshold=60,
        max_concurrent_transmissions=cfg.workers,
    )
    register_handlers(bot, cfg)

    await bot.start()
    me = await bot.get_me()
    log.info("Bot logged in as @%s (id=%s)", me.username, me.id)

    app = make_app(bot, cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bind_host, cfg.port)
    await site.start()
    log.info("HTTP server listening on %s:%d  base_url=%s", cfg.bind_host, cfg.port, cfg.base_url)

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
        await bot.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
