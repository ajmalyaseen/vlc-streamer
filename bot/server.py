import asyncio
import logging
import re
import time
from html import escape
from urllib.parse import quote

from aiohttp import web
from pyrogram import Client
from pyrogram.errors import FileReferenceExpired
from pyrogram.types import Message

from .config import Config
from .streamer import stream_to_response
from .utils import verify_token

log = logging.getLogger("server")

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CACHE_TTL = 1800  # seconds to keep a file's message/metadata + client assignment


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
    window.location.href = vlcLink;
  }} else if (isiOS) {{
    var startedAt = Date.now();
    var timer = setTimeout(function() {{
      if (!document.hidden && Date.now() - startedAt < 2500) {{
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


def _select_client_index(app: web.Application, chat_id: int) -> int:
    """Pick the least-busy eligible client. Workers can only read LOG_CHANNEL
    files, so they're only eligible when the file lives in the log channel."""
    cfg: Config = app["config"]
    clients = app["clients"]
    active = app["active"]
    if len(clients) > 1 and cfg.log_channel and chat_id == cfg.log_channel:
        # least active streams; ties broken by lowest index
        return min(range(len(clients)), key=lambda i: active[i])
    return 0  # only the main bot can read non-log-channel files


async def _get_entry(app: web.Application, chat_id: int, msg_id: int):
    """Return a cached {message, size, mime, name, ci} entry, fetching fresh on
    miss/expiry. The file is pinned to one client (ci) so its file reference and
    warm media session stay valid across the many Range requests of a seek."""
    cache = app["meta_cache"]
    key = (chat_id, msg_id)
    now = time.monotonic()
    entry = cache.get(key)
    if entry and entry["expiry"] > now:
        return entry

    ci = _select_client_index(app, chat_id)
    client = app["clients"][ci]
    message = await client.get_messages(chat_id, msg_id)
    media = _extract_media(message)
    if not media:
        return None
    entry = {
        "message": message,
        "size": media.file_size,
        "mime": media.mime_type or "application/octet-stream",
        "name": getattr(media, "file_name", None) or f"file_{msg_id}",
        "ci": ci,
        "expiry": now + CACHE_TTL,
    }
    cache[key] = entry
    return entry


async def _refresh_entry(app: web.Application, chat_id: int, msg_id: int, ci: int):
    """Re-fetch a fresh message (new file reference) using the pinned client."""
    client = app["clients"][ci]
    message = await client.get_messages(chat_id, msg_id)
    entry = app["meta_cache"].get((chat_id, msg_id))
    if entry:
        entry["message"] = message
        entry["expiry"] = time.monotonic() + CACHE_TTL
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

    try:
        entry = await _get_entry(request.app, chat_id, msg_id)
    except Exception as e:
        log.warning("get_messages failed for %s/%s: %s", chat_id, msg_id, e)
        return web.Response(status=404, text="File not found")
    if entry is None:
        return web.Response(status=404, text="File not found")

    file_size = entry["size"]
    mime = entry["mime"]
    file_name = entry["name"]
    ci = entry["ci"]
    client = request.app["clients"][ci]
    message = entry["message"]

    # ---- Range handling ----
    start, end, status = 0, file_size - 1, 200
    range_header = request.headers.get("Range")
    if range_header:
        m = RANGE_RE.match(range_header)
        if m:
            s, e = m.group(1), m.group(2)
            if s == "" and e == "":
                return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
            if s == "":  # suffix: last N bytes
                start = max(0, file_size - int(e))
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

    active = request.app["active"]
    active[ci] += 1
    cname = getattr(client, "name", f"client{ci}")
    log.info("stream start msg=%s client=%s range=%s-%s active=%s",
             msg_id, cname, start, end, active)
    try:
        try:
            await stream_to_response(client, message, start, end, response)
        except FileReferenceExpired:
            # Reference expired (occurs before any bytes are sent): refresh + retry once.
            message = await _refresh_entry(request.app, chat_id, msg_id, ci)
            await stream_to_response(client, message, start, end, response)
    except (ConnectionError, asyncio.CancelledError):
        pass  # client disconnected mid-stream (normal during seeking)
    except Exception as e:
        log.exception("Streaming error for msg %s: %s", msg_id, e)
    finally:
        active[ci] -= 1
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
    clients = clients or [bot]
    app = web.Application(client_max_size=1024 * 16)
    app["bot"] = bot
    app["config"] = cfg
    app["clients"] = clients
    app["active"] = [0] * len(clients)        # active streams per client (load monitor)
    app["meta_cache"] = {}                      # (chat,msg) -> entry with TTL
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    # add_get also registers HEAD automatically; the handler checks request.method.
    app.router.add_get("/stream/{chat_id}/{msg_id}/{name}", stream_handler)
    app.router.add_get("/watch/{chat_id}/{msg_id}/{name}", watch_handler)
    return app
