import asyncio
import logging
import re
from html import escape
from urllib.parse import quote

from aiohttp import web
from pyrogram import Client
from pyrogram.types import Message

from .config import Config
from .streamer import stream_range
from .utils import verify_token

log = logging.getLogger("server")

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


WATCH_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watch in VLC</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1115;
         color:#eaeaea; margin:0; display:flex; min-height:100vh; align-items:center;
         justify-content:center; }}
  .card {{ background:#171a21; padding:28px 24px; border-radius:16px; width:90%;
          max-width:420px; text-align:center; box-shadow:0 10px 40px rgba(0,0,0,.4); }}
  h2 {{ margin:0 0 6px; font-size:20px; }}
  .fn {{ color:#9aa0aa; font-size:13px; word-break:break-all; margin:0 0 20px; }}
  .btn {{ display:block; padding:14px; border-radius:10px; text-decoration:none;
         font-weight:600; margin:10px 0; }}
  .primary {{ background:#ff8800; color:#111; }}
  .secondary {{ background:#262b35; color:#eaeaea; }}
  .hint {{ color:#7d828c; font-size:12px; margin-top:18px; line-height:1.5; }}
</style>
</head>
<body>
  <div class="card">
    <h2>▶ Open in VLC</h2>
    <p class="fn">{name}</p>
    <a id="vlc" class="btn primary" href="#">Open in VLC</a>
    <a id="direct" class="btn secondary" href="{stream}">Copy / Direct link</a>
    <p class="hint">If VLC doesn't open automatically, tap "Open in VLC".
       On desktop, copy the direct link and use VLC &rarr; Open Network Stream.</p>
  </div>
<script>
  var stream = "{stream}";
  var ua = navigator.userAgent || "";
  var isiOS = /iPad|iPhone|iPod/.test(ua);
  var isAndroid = /Android/.test(ua);
  var PLAY = "https://play.google.com/store/apps/details?id=org.videolan.vlc";
  var APPSTORE = "https://apps.apple.com/app/vlc-media-player/id650377962";
  var vlcLink;
  if (isiOS) {{
    vlcLink = "vlc-x-callback://x-callback-url/stream?url=" + encodeURIComponent(stream);
  }} else if (isAndroid) {{
    var noScheme = stream.replace(/^https?:\\/\\//, "");
    vlcLink = "intent://" + noScheme +
              "#Intent;scheme=https;package=org.videolan.vlc;type=video/*;" +
              "S.browser_fallback_url=" + encodeURIComponent(PLAY) + ";end";
  }} else {{
    vlcLink = stream;
  }}
  document.getElementById("vlc").href = vlcLink;

  if (isAndroid) {{
    // The intent's browser_fallback_url sends users to the Play Store
    // automatically when VLC isn't installed.
    window.location.href = vlcLink;
  }} else if (isiOS) {{
    // If VLC opens, the page is backgrounded and the timer is cancelled.
    // Otherwise we assume it's not installed and go to the App Store.
    var start = Date.now();
    var timer = setTimeout(function() {{
      if (!document.hidden && Date.now() - start < 2500) {{
        window.location.href = APPSTORE;
      }}
    }}, 1500);
    window.addEventListener("pagehide", function() {{ clearTimeout(timer); }});
    document.addEventListener("visibilitychange", function() {{
      if (document.hidden) {{ clearTimeout(timer); }}
    }});
    window.location.href = vlcLink;
  }}
</script>
</body>
</html>"""




def _extract_media(message: Message):
    return message.document or message.video or message.audio or message.animation


def _pick_client(request: web.Request, chat_id: int):
    """Round-robin a streaming client. Workers can only read LOG_CHANNEL files,
    so only spread across the pool when the file lives in the log channel."""
    cfg: Config = request.app["config"]
    bot: Client = request.app["bot"]
    clients = request.app.get("clients") or [bot]
    if len(clients) > 1 and cfg.log_channel and chat_id == cfg.log_channel:
        cycle = request.app["client_index"]
        client = clients[cycle[0] % len(clients)]
        cycle[0] += 1
        return client
    return bot


async def _get_message_cached(request: web.Request, client: Client, chat_id: int, msg_id: int):
    """Cache message objects briefly so VLC's many Range requests during seeking
    don't each trigger a get_messages round-trip to Telegram."""
    import time

    cache = request.app["msg_cache"]
    key = (chat_id, msg_id)
    now = time.monotonic()
    cached = cache.get(key)
    if cached and now - cached[1] < 300:  # 5 min TTL
        return cached[0]
    message = await client.get_messages(chat_id, msg_id)
    cache[key] = (message, now)
    return message


async def stream_handler(request: web.Request) -> web.StreamResponse:
    cfg: Config = request.app["config"]

    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Bad chat or message id")

    token = request.query.get("hash", "")
    if not verify_token(chat_id, msg_id, token, cfg.hash_secret):
        return web.Response(status=403, text="Invalid or missing token")

    client = _pick_client(request, chat_id)

    try:
        message = await _get_message_cached(request, client, chat_id, msg_id)
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
        async for chunk in stream_range(client, message, start, end):
            await response.write(chunk)
    except (ConnectionError, asyncio.CancelledError):
        log.info("Client disconnected during stream of msg %s", msg_id)
    except Exception as e:
        log.exception("Streaming error for msg %s: %s", msg_id, e)
    finally:
        try:
            await response.write_eof()
        except Exception:
            pass

    return response


async def watch_handler(request: web.Request) -> web.Response:
    """Serve a small page that deep-links into VLC, with a direct-link fallback."""
    cfg: Config = request.app["config"]

    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Bad chat or message id")

    token = request.query.get("hash", "")
    if not verify_token(chat_id, msg_id, token, cfg.hash_secret):
        return web.Response(status=403, text="Invalid or missing token")

    name = request.match_info["name"]
    stream_url = f"{cfg.base_url}/stream/{chat_id}/{msg_id}/{quote(name)}?hash={token}"
    html = WATCH_PAGE.format(name=escape(name), stream=escape(stream_url, quote=True))
    return web.Response(text=html, content_type="text/html")


async def index(_request: web.Request) -> web.Response:
    return web.Response(text="Telegram → VLC stream bot is running.")


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app(bot: Client, cfg: Config, clients=None) -> web.Application:
    app = web.Application(client_max_size=1024 * 16)
    app["bot"] = bot
    app["config"] = cfg
    app["clients"] = clients or [bot]
    app["client_index"] = [0]   # mutable round-robin counter
    app["msg_cache"] = {}        # (chat_id, msg_id) -> (message, monotonic_ts)
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    # add_get also registers HEAD automatically (allow_head=True by default),
    # and our handler checks request.method == "HEAD".
    app.router.add_get("/stream/{chat_id}/{msg_id}/{name}", stream_handler)
    app.router.add_get("/watch/{chat_id}/{msg_id}/{name}", watch_handler)
    return app
