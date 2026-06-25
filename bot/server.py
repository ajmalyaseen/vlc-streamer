import asyncio
import logging
import re
import time
from html import escape
from urllib.parse import quote, urlencode

from aiohttp import web
from pyrogram import Client
from pyrogram.errors import FileReferenceExpired
from pyrogram.file_id import FileId
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
    <a id="pcvlc" class="btn primary" href="{playlist}" style="display:none">🖥 Open in PC VLC</a>
    <a id="direct" class="btn secondary" href="{stream}">Copy / Direct link</a>
    <p class="hint">If VLC doesn't open automatically, tap "Open in VLC".
       On desktop, tap "Open in PC VLC" to launch the VLC app, or copy the direct
       link and use VLC &rarr; Open Network Stream.</p>
  </div>
<script>
  var stream = "{stream_js}";
  var playlist = "{playlist}";
  var ua = navigator.userAgent || "";
  var isiOS = /iPad|iPhone|iPod/.test(ua);
  var isAndroid = /Android/.test(ua);
  var isMobile = isiOS || isAndroid;
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

  // On desktop, show the .m3u "Open in PC VLC" button (VLC is the default app
  // for .m3u) and hide the mobile deep-link button which can't reach desktop VLC.
  if (!isMobile) {{
    document.getElementById("pcvlc").style.display = "block";
    document.getElementById("vlc").style.display = "none";
  }}

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


def _select_client_index(app: web.Application, chat_id: int, msg_id: int) -> int:
    """Pick the least-busy eligible client for this request. Workers can only read
    LOG_CHANNEL files, so they're only eligible when the file is in the log channel.

    Spreading a file's parallel/seek connections across all bots keeps any single
    media session from being flooded (continuous seeking was overloading one
    session and making Telegram drop it). Every bot is pre-warmed AND has the
    file reference pre-cached on first play, so a spread connection pays neither a
    cold handshake nor a get_messages round-trip. Tie-break by msg_id for a stable
    home bot under no load."""
    cfg: Config = app["config"]
    clients = app["clients"]
    if len(clients) > 1 and cfg.log_channel and chat_id == cfg.log_channel:
        active = app["active"]
        return min(range(len(clients)), key=lambda i: (active[i], (i - msg_id) % len(clients)))
    return 0  # only the main bot can read non-log-channel files


async def _prewarm_all(app: web.Application, chat_id: int, msg_id: int) -> None:
    """Warm every bot for this file once: cache its reference (get_messages) AND
    open its media session. After this, least-busy routing can send a seek to any
    bot with no get_messages latency and no cold handshake — fast and stable."""
    from .streamer import get_media_session

    async def _warm(ci: int):
        try:
            entry = await _get_entry(app, ci, chat_id, msg_id)
            if entry:
                await get_media_session(app["clients"][ci], entry["file_id"])
        except Exception:
            log.warning("prewarm failed for client %s (non-fatal)", ci, exc_info=True)

    await asyncio.gather(*(_warm(i) for i in range(len(app["clients"]))))


async def _get_entry(app: web.Application, ci: int, chat_id: int, msg_id: int):
    """Cached {message, size, mime, name} for a specific client (ci).

    Cached per client because Telegram file references are per-client: a message
    fetched by one bot can't be streamed by another. Distributing requests across
    bots therefore needs each bot to hold its own reference. The message + its warm
    media session are reused across that bot's many Range requests for the file."""
    cache = app["meta_cache"]
    key = (ci, chat_id, msg_id)
    now = time.monotonic()
    entry = cache.get(key)
    if entry and entry["expiry"] > now:
        return entry

    client = app["clients"][ci]
    message = await client.get_messages(chat_id, msg_id)
    media = _extract_media(message)
    if not media:
        return None
    entry = {
        "file_id": FileId.decode(media.file_id),
        "size": media.file_size,
        "mime": media.mime_type or "application/octet-stream",
        "name": getattr(media, "file_name", None) or f"file_{msg_id}",
        "expiry": now + CACHE_TTL,
    }
    cache[key] = entry
    return entry


async def _refresh_entry(app: web.Application, ci: int, chat_id: int, msg_id: int):
    """Re-decode a fresh file reference for client ci (on FileReferenceExpired)."""
    client = app["clients"][ci]
    message = await client.get_messages(chat_id, msg_id)
    media = _extract_media(message)
    file_id = FileId.decode(media.file_id)
    entry = app["meta_cache"].get((ci, chat_id, msg_id))
    if entry:
        entry["file_id"] = file_id
        entry["expiry"] = time.monotonic() + CACHE_TTL
    return file_id


def _build_pool(app: web.Application, chat_id: int, msg_id: int, fallback):
    """List of (client, file_id) across every bot that already has this file's
    reference cached — the sessions to stripe a single stream's chunks across.
    Falls back to [fallback] (the primary client) if nothing else is warm yet."""
    pool = []
    cache = app["meta_cache"]
    now = time.monotonic()
    for cj in range(len(app["clients"])):
        ej = cache.get((cj, chat_id, msg_id))
        if ej and ej["expiry"] > now:
            pool.append((app["clients"][cj], ej["file_id"]))
    return pool or [fallback]


async def _refresh_pool(app: web.Application, chat_id: int, msg_id: int):
    """Refresh every cached reference for this file (on FileReferenceExpired)."""
    for cj in range(len(app["clients"])):
        if app["meta_cache"].get((cj, chat_id, msg_id)):
            try:
                await _refresh_entry(app, cj, chat_id, msg_id)
            except Exception:
                pass


async def stream_handler(request: web.Request) -> web.StreamResponse:
    cfg: Config = request.app["config"]

    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Bad chat or message id")

    token = request.query.get("hash", "")
    exp = int(request.query.get("exp", "0") or "0")
    if not verify_token(chat_id, msg_id, token, cfg.hash_secret, exp):
        return web.Response(status=403, text="Invalid or missing token")
    if exp and exp < int(time.time()):
        return web.Response(status=410, text="This link has expired")

    # Distribute each request to the least-busy eligible client so VLC's many
    # parallel connections for one file spread across bots instead of piling
    # onto a single one. Increment the counter NOW (at selection) so concurrent
    # requests see updated load and don't all pick the same client.
    ci = _select_client_index(request.app, chat_id, msg_id)
    active = request.app["active"]
    active[ci] += 1
    try:
        try:
            entry = await _get_entry(request.app, ci, chat_id, msg_id)
        except Exception as e:
            log.warning("get_messages failed for %s/%s on client %s: %s", chat_id, msg_id, ci, e)
            entry = None
        # If the chosen worker can't read the file (e.g. not resolved / not an
        # admin of the log channel), fall back to the main bot, which always can.
        if entry is None and ci != 0:
            active[ci] -= 1
            ci = 0
            active[ci] += 1
            try:
                entry = await _get_entry(request.app, ci, chat_id, msg_id)
            except Exception as e:
                log.warning("get_messages failed for %s/%s on main bot: %s", chat_id, msg_id, e)
                entry = None
        if entry is None:
            return web.Response(status=404, text="File not found")

        # On the first request for this file, warm all bots in the background
        # (reference + session) so least-busy routing can spread seek connections
        # across them with no per-bot get_messages latency or cold handshake.
        if len(request.app["clients"]) > 1:
            warmed = request.app.setdefault("warmed_files", set())
            if msg_id not in warmed:
                warmed.add(msg_id)
                asyncio.ensure_future(_prewarm_all(request.app, chat_id, msg_id))

        file_size = entry["size"]
        mime = entry["mime"]
        file_name = entry["name"]
        client = request.app["clients"][ci]
        file_id = entry["file_id"]

        # Record who's watching (admin /streamusers view). Side-effect-free: a
        # dict write only, no influence on the streaming path below.
        monitor = request.app.get("monitor")
        if monitor is not None:
            uid = request.query.get("uid")
            try:
                uid = int(uid) if uid else None
            except ValueError:
                uid = None
            monitor.touch(uid, chat_id, msg_id, file_name)

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

        cname = getattr(client, "name", f"client{ci}")
        log.info("stream start msg=%s client=%s range=%s-%s active=%s",
                 msg_id, cname, start, end, active)
        # Stripe this stream's chunks across every warm bot session for this file
        # (multi-session = aggregate throughput on a far DC). Falls back to the
        # primary client until the prewarm has cached the others.
        pool = _build_pool(request.app, chat_id, msg_id, (client, file_id))
        try:
            try:
                await stream_to_response(pool, start, end, response)
            except FileReferenceExpired:
                # A reference expired: refresh all + rebuild pool, retry once.
                await _refresh_pool(request.app, chat_id, msg_id)
                pool = _build_pool(request.app, chat_id, msg_id, (client, file_id))
                await stream_to_response(pool, start, end, response)
        except (ConnectionError, asyncio.CancelledError):
            pass  # client disconnected mid-stream (normal during seeking)
        except Exception as e:
            log.exception("Streaming error for msg %s: %s", msg_id, e)
        finally:
            try:
                await response.write_eof()
            except Exception:
                pass
        return response
    finally:
        active[ci] -= 1


async def playlist_handler(request: web.Request) -> web.Response:
    """Serve a tiny .m3u playlist pointing at the stream URL.

    On desktop, browsers have no vlc:// scheme, but VLC registers itself as the
    default handler for .m3u files. So downloading/opening this playlist launches
    VLC and starts streaming automatically — the reliable "Open in PC VLC" path."""
    cfg: Config = request.app["config"]

    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Bad chat or message id")

    token = request.query.get("hash", "")
    exp = int(request.query.get("exp", "0") or "0")
    if not verify_token(chat_id, msg_id, token, cfg.hash_secret, exp):
        return web.Response(status=403, text="Invalid or missing token")
    if exp and exp < int(time.time()):
        return web.Response(status=410, text="This link has expired")

    name = request.match_info["name"]
    uid = request.query.get("uid", "")
    suffix = f"&exp={exp}" if exp else ""
    if uid:
        suffix += f"&uid={quote(uid)}"
    stream_url = f"{cfg.base_url}/stream/{chat_id}/{msg_id}/{quote(name)}?hash={token}{suffix}"

    body = f"#EXTM3U\n#EXTINF:-1,{name}\n{stream_url}\n"
    # Strip characters that would break the Content-Disposition filename.
    safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name) or f"file_{msg_id}"
    if not safe_name.lower().endswith(".m3u"):
        safe_name += ".m3u"
    return web.Response(
        text=body,
        content_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


async def watch_handler(request: web.Request) -> web.Response:
    """Serve a small page that deep-links into VLC, with a direct-link fallback."""
    cfg: Config = request.app["config"]

    try:
        chat_id = int(request.match_info["chat_id"])
        msg_id = int(request.match_info["msg_id"])
    except ValueError:
        return web.Response(status=400, text="Bad chat or message id")

    token = request.query.get("hash", "")
    exp = int(request.query.get("exp", "0") or "0")
    if not verify_token(chat_id, msg_id, token, cfg.hash_secret, exp):
        return web.Response(status=403, text="Invalid or missing token")

    name = request.match_info["name"]
    uid = request.query.get("uid", "")
    suffix = f"&exp={exp}" if exp else ""
    if uid:
        suffix += f"&uid={quote(uid)}"
    stream_url = f"{cfg.base_url}/stream/{chat_id}/{msg_id}/{quote(name)}?hash={token}{suffix}"
    playlist_url = f"{cfg.base_url}/play/{chat_id}/{msg_id}/{quote(name)}?hash={token}{suffix}"
    # Two contexts: the <a href> needs HTML-escaped "&amp;" (browser decodes it back),
    # but the JS string variable needs the RAW url — JS does not decode HTML entities,
    # so "&amp;" there would literally reach VLC and break the query string (exp lost).
    html = WATCH_PAGE.format(
        name=escape(name),
        stream=escape(stream_url, quote=True),
        stream_js=stream_url,
        playlist=escape(playlist_url, quote=True),
    )
    return web.Response(text=html, content_type="text/html")


async def pay_handler(request: web.Request) -> web.Response:
    """Show a UPI-app chooser. Each button opens that app's deep link with the
    payee/amount/note pre-filled. (Telegram won't allow upi:// in a button, so
    the bot button points here.)"""
    pa = request.query.get("pa", "")
    pn = request.query.get("pn", "Payment")
    am = request.query.get("am", "")
    tn = request.query.get("tn", "")
    q = urlencode({"pa": pa, "pn": pn, "am": am, "cu": "INR", "tn": tn}, quote_via=quote)

    # App-specific UPI deep-link schemes (same query params).
    links = {
        "gpay": "tez://upi/pay?" + q,
        "phonepe": "phonepe://pay?" + q,
        "paytm": "paytmmp://pay?" + q,
        "upi": "upi://pay?" + q,
    }

    def btn(href, bg, fg, label):
        return (
            f"<a class='btn' style='background:{bg};color:{fg}' "
            f"href='{escape(href, quote=True)}'>{label}</a>"
        )

    buttons = (
        btn(links["gpay"], "#ffffff", "#3c4043", "🟢🔵🔴🟡 &nbsp; Google Pay")
        + btn(links["phonepe"], "#5f259f", "#ffffff", "🟣 &nbsp; PhonePe")
        + btn(links["paytm"], "#00baf2", "#ffffff", "🔵 &nbsp; Paytm")
        + btn(links["upi"], "#ff8800", "#111111", "💳 &nbsp; Any UPI App")
    )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Choose UPI App</title>"
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;"
        "color:#eaeaea;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}"
        ".card{background:#171a21;padding:26px 22px;border-radius:16px;width:90%;max-width:380px;"
        "text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.4)}"
        ".btn{display:block;margin:10px 0;padding:14px;border-radius:10px;text-decoration:none;"
        "font-weight:700}h2{margin:0 0 4px}p{color:#9aa0aa;margin:0 0 16px;font-size:14px}"
        ".hint{color:#7d828c;font-size:12px;margin-top:16px}</style></head><body>"
        "<div class='card'>"
        f"<h2>Pay ₹{escape(am)}</h2><p>To {escape(pa)} · Ref {escape(tn)}</p>"
        f"{buttons}"
        "<p class='hint'>Pick your app — amount &amp; note are pre-filled. "
        "After paying, send the last 4 digits of your UTR to the bot.</p>"
        "</div></body></html>"
    )
    return web.Response(text=html, content_type="text/html")


CHECKOUT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alaska Stream — Checkout</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eaeaea;
text-align:center;padding-top:20vh}#msg{margin-top:18px;color:#9aa0aa;font-size:15px}
.btn{display:inline-block;margin-top:14px;padding:13px 22px;background:#ff8800;color:#111;
border-radius:10px;text-decoration:none;font-weight:700;border:0;font-size:15px}</style>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script></head>
<body>
<h2>Alaska Stream</h2>
<p id="msg">Starting secure checkout…</p>
<button class="btn" onclick="start()">Pay now</button>
<script>
var REFERENCE = "__REF__";
var TOKEN = "__TOKEN__";
async function start(){
  document.getElementById('msg').innerText = 'Starting secure checkout…';
  var o;
  try {
    o = await fetch('/api/create-order',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({reference:REFERENCE, token:TOKEN})}).then(function(r){return r.json();});
  } catch(e){ document.getElementById('msg').innerText='Network error. Try again.'; return; }
  if(!o || !o.order_id){ document.getElementById('msg').innerText='Could not start payment. Please try again.'; return; }
  var rzp = new Razorpay({
    key:o.key_id, order_id:o.order_id, amount:o.amount, currency:o.currency,
    name:o.name||'Alaska Stream', description:o.description||'Subscription',
    handler: async function(resp){
      document.getElementById('msg').innerText='Verifying payment…';
      var v = await fetch('/api/verify-payment',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({reference:REFERENCE, razorpay_order_id:resp.razorpay_order_id,
          razorpay_payment_id:resp.razorpay_payment_id, razorpay_signature:resp.razorpay_signature})
        }).then(function(r){return r.json();});
      document.getElementById('msg').innerText = v.success
        ? '✅ Payment successful! Your plan is now active. You can return to Telegram.'
        : '⚠️ Verification failed. If money was deducted, contact support.';
    },
    modal:{ondismiss:function(){document.getElementById('msg').innerText='Payment cancelled.';}}
  });
  rzp.on('payment.failed', function(r){
    document.getElementById('msg').innerText='Payment failed: '+((r.error&&r.error.description)||'try again');
  });
  rzp.open();
}
window.onload = start;
</script></body></html>"""


async def checkout_handler(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    payments = request.app.get("payments")
    reference = request.match_info["reference"]
    token = request.query.get("token", "")
    from .utils import verify_payment_token
    if not (payments and payments.razorpay_enabled) or not verify_payment_token(
        reference, token, cfg.hash_secret
    ):
        return web.Response(status=403, text="Invalid or expired checkout link")
    html = CHECKOUT_PAGE.replace("__REF__", reference).replace("__TOKEN__", token)
    return web.Response(text=html, content_type="text/html")


async def create_order_handler(request: web.Request) -> web.Response:
    cfg: Config = request.app["config"]
    payments = request.app.get("payments")
    if not (payments and payments.razorpay_enabled):
        return web.json_response({"error": "payments disabled"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    reference = data.get("reference", "")
    token = data.get("token", "")
    from .utils import verify_payment_token
    if not verify_payment_token(reference, token, cfg.hash_secret):
        return web.json_response({"error": "invalid token"}, status=403)
    order = await payments.create_order(reference)
    if not order:
        return web.json_response({"error": "could not create order"}, status=500)
    return web.json_response({
        **order,
        "key_id": cfg.razorpay_key_id,
        "name": "Alaska Stream",
        "description": f"Subscription {reference}",
    })


async def verify_payment_handler(request: web.Request) -> web.Response:
    payments = request.app.get("payments")
    if not payments:
        return web.json_response({"error": "payments disabled"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    ref = data.get("reference")
    oid = data.get("razorpay_order_id")
    pid = data.get("razorpay_payment_id")
    sig = data.get("razorpay_signature")
    if not all([ref, oid, pid, sig]):
        return web.json_response({"error": "missing fields"}, status=400)
    ok = await payments.verify_and_fulfill(ref, oid, pid, sig)
    return web.json_response({"success": bool(ok)}, status=200 if ok else 400)


async def webhook_handler(request: web.Request) -> web.Response:
    """Razorpay server-to-server webhook (reliable fulfillment).

    Razorpay calls this even if the user closed the browser before the
    client-side verify ran, so the plan still activates. Always returns 200 on
    a valid signature (even for events we ignore) so Razorpay stops retrying."""
    payments = request.app.get("payments")
    if not payments:
        return web.Response(status=200, text="ignored")
    body = await request.read()
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        await payments.fulfill_from_webhook(body, signature)
    except Exception:
        log.exception("webhook processing failed")
    return web.Response(status=200, text="ok")


async def index(_request: web.Request) -> web.Response:
    return web.Response(text="Telegram → VLC stream bot is running.")


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app(bot: Client, cfg: Config, clients=None, payments=None, monitor=None) -> web.Application:
    clients = clients or [bot]
    app = web.Application(client_max_size=1024 * 16)
    app["bot"] = bot
    app["config"] = cfg
    app["clients"] = clients
    app["payments"] = payments
    app["monitor"] = monitor
    app["active"] = [0] * len(clients)        # active streams per client (load monitor)
    app["meta_cache"] = {}                      # (chat,msg) -> entry with TTL
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    # add_get also registers HEAD automatically; the handler checks request.method.
    app.router.add_get("/stream/{chat_id}/{msg_id}/{name}", stream_handler)
    app.router.add_get("/watch/{chat_id}/{msg_id}/{name}", watch_handler)
    app.router.add_get("/play/{chat_id}/{msg_id}/{name}", playlist_handler)
    app.router.add_get("/pay", pay_handler)
    app.router.add_get("/checkout/{reference}", checkout_handler)
    app.router.add_post("/api/create-order", create_order_handler)
    app.router.add_post("/api/verify-payment", verify_payment_handler)
    app.router.add_post("/webhook", webhook_handler)
    return app
