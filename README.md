# Telegram → VLC Stream Bot

Send a video file (MP4, MKV, etc.) to the bot. It replies with a direct HTTP
streaming link you can open in **VLC → Media → Open Network Stream**.

The server downloads chunks from Telegram on demand (MTProto) and streams them
over HTTP with `Range` support, so seeking works.

## How it works

1. You send a file to the bot in a private chat.
2. The bot copies the message into a private "log channel" you own (so the file
   stays accessible permanently and the bot can re-download it later).
3. The bot replies with a signed URL like
   `https://<your-host>/stream/<msg_id>/<filename>?hash=<token>`.
4. When VLC opens the URL, the server pulls 1 MiB chunks from Telegram via
   Pyrogram and streams them back, honoring `Range` headers for seeking.

## Prerequisites

- Telegram **API_ID** and **API_HASH** from <https://my.telegram.org> →
  *API development tools*.
- A **bot token** from [@BotFather](https://t.me/BotFather).
- A **private Telegram channel** where files will be stored. Add your bot to
  this channel as an **admin** (it needs the "Post messages" permission).
  Get the channel ID:
  - Open the channel in Telegram Web, the URL contains the id, or
  - Forward any message from the channel to [@userinfobot](https://t.me/userinfobot)
    and read the "Forwarded from chat" id. It looks like `-1001234567890`.
- A public host. This guide covers **Koyeb**.

## Local run (for testing)

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env, set BASE_URL to your tunnel URL (e.g. ngrok / cloudflared)
python -m bot.main
```

For local testing you need a public tunnel because VLC on your phone can't
reach `localhost`. Easy options:

```bash
# Cloudflare tunnel (free, stable URL after login)
cloudflared tunnel --url http://localhost:8080

# or ngrok
ngrok http 8080
```

Use the resulting `https://...` URL as `BASE_URL`.

## Deploy to Koyeb

1. Push this repo to GitHub.
2. In Koyeb dashboard → **Create Service** → **GitHub** → pick the repo.
3. Builder: **Dockerfile** (auto-detected).
4. Instance: the smallest free tier is fine to start.
5. Region: pick one close to you.
6. **Ports**: expose port `8080` as HTTP, path `/healthz` for the health check.
7. **Environment variables** — set all of these:

   | Key            | Value                                              |
   |----------------|----------------------------------------------------|
   | `API_ID`       | from my.telegram.org                               |
   | `API_HASH`     | from my.telegram.org                               |
   | `BOT_TOKEN`    | from @BotFather                                    |
   | `LOG_CHANNEL`  | e.g. `-1001234567890`                              |
   | `BASE_URL`     | leave blank for first deploy, see step 9           |
   | `HASH_SECRET`  | a long random string (e.g. `openssl rand -hex 32`) |

8. Deploy. After it's healthy, Koyeb assigns a public URL like
   `https://my-app-myorg.koyeb.app`.
9. Set `BASE_URL=https://my-app-myorg.koyeb.app` in the service env vars and
   redeploy. (You need this so the URLs the bot generates point at itself.)

## Using it

1. Open the bot in Telegram, send `/start`.
2. Send any video file.
3. Bot replies with a `https://.../stream/...` URL.
4. In VLC: **Media → Open Network Stream** → paste the URL → Play.

Mobile VLC: tap the cone menu → **Network** → **Open Network Stream**.

## Notes & limits

- Files up to ~2 GB work with a regular bot. Larger needs a Premium user
  account, which is out of scope here.
- Each open VLC session uses one concurrent transmission. `WORKERS` controls
  how many parallel downloads from Telegram the bot will do.
- The link is HMAC-signed with `HASH_SECRET`, so people can't guess valid URLs.
  Anyone with the URL can stream it, though, so don't share publicly.
- The bot session is in-memory; restarts re-login from the bot token. No
  database or persistent storage required.

## Project layout

```
bot/
  main.py        # entrypoint, wires bot + aiohttp together
  config.py      # env var loading
  handlers.py    # Telegram message handlers
  server.py      # aiohttp routes including /stream
  streamer.py    # byte-range -> Pyrogram chunk translation
  utils.py       # HMAC tokens, helpers
Dockerfile
requirements.txt
.env.example
```
