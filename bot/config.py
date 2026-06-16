import os
from dataclasses import dataclass


@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    log_channel: int       # private channel ID where files are stored (negative, e.g. -1001234567890)
    base_url: str          # public URL of this service, e.g. https://my-app.koyeb.app
    hash_secret: str       # used to sign stream URLs
    session_string: str = ""  # optional: reuse an existing login across restarts
    port: int = 8080
    bind_host: str = "0.0.0.0"
    workers: int = 4


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _normalize_base_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def load_config() -> Config:
    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        base_url=_normalize_base_url(_require("BASE_URL")),
        hash_secret=_require("HASH_SECRET"),
        log_channel=int(os.environ.get("LOG_CHANNEL", "0") or "0"),
        session_string=os.environ.get("SESSION_STRING", ""),
        port=int(os.environ.get("PORT", "8080")),
        bind_host=os.environ.get("BIND_HOST", "0.0.0.0"),
        workers=int(os.environ.get("WORKERS", "4")),
    )
