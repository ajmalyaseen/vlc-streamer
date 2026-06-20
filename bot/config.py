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
    database_url: str = ""    # optional MongoDB URL for persistent user storage
    admins: tuple = ()        # Telegram user IDs allowed to run /stats, /broadcast
    force_sub: str = ""       # channel username/id users must join (bot must be admin)
    force_sub_invite: str = ""  # join link for the button (defaults to t.me/<username>)
    worker_tokens: tuple = ()   # extra bot tokens for parallel streaming (need LOG_CHANNEL)
    # --- subscription / payments ---
    upi_id: str = ""            # UPI ID for payments; empty disables purchases
    admin_group_id: int = 0     # group where payment requests are posted (0 = DM admins)
    support_link: str = "https://t.me/alaska_in"  # "Contact Support" button target
    razorpay_key_id: str = ""        # Razorpay Standard Checkout key id
    razorpay_key_secret: str = ""    # Razorpay key secret (server-side only)
    razorpay_webhook_secret: str = ""  # optional webhook signing secret
    plus_price: int = 27        # rupees / 30 days
    pro_price: int = 67
    free_daily: int = 2         # links per day
    plus_daily: int = 20
    pro_daily: int = 100
    free_max_gb: float = 2.0    # max file size (GB)
    plus_max_gb: float = 4.0
    pro_max_gb: float = 10.0
    free_expiry_h: int = 6      # stream-link validity (hours)
    plus_expiry_h: int = 24
    pro_expiry_h: int = 168
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


def _parse_admins(raw: str) -> tuple:
    ids = []
    for part in raw.replace(",", " ").split():
        try:
            ids.append(int(part))
        except ValueError:
            pass
    return tuple(ids)


def _parse_worker_tokens() -> tuple:
    """Collect MULTI_TOKEN1, MULTI_TOKEN2, ... and/or space-separated MULTI_TOKENS."""
    tokens = []
    combined = os.environ.get("MULTI_TOKENS", "")
    for part in combined.replace(",", " ").split():
        tokens.append(part)
    i = 1
    while True:
        val = os.environ.get(f"MULTI_TOKEN{i}")
        if not val:
            break
        tokens.append(val.strip())
        i += 1
    return tuple(t for t in tokens if t)


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_config() -> Config:
    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        base_url=_normalize_base_url(_require("BASE_URL")),
        hash_secret=_require("HASH_SECRET"),
        log_channel=int(os.environ.get("LOG_CHANNEL", "0") or "0"),
        session_string=os.environ.get("SESSION_STRING", ""),
        database_url=os.environ.get("DATABASE_URL", ""),
        admins=_parse_admins(os.environ.get("ADMINS", "")),
        force_sub=os.environ.get("FORCE_SUB_CHANNEL", "").strip(),
        force_sub_invite=os.environ.get("FORCE_SUB_INVITE", "").strip(),
        worker_tokens=_parse_worker_tokens(),
        upi_id=os.environ.get("UPI_ID", "").strip(),
        admin_group_id=int(os.environ.get("ADMIN_GROUP_ID", "0") or "0"),
        support_link=os.environ.get("SUPPORT_LINK", "https://t.me/alaska_in").strip(),
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", "").strip(),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", "").strip(),
        razorpay_webhook_secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip(),
        plus_price=_i("PLUS_PRICE", 27),
        pro_price=_i("PRO_PRICE", 67),
        free_daily=_i("FREE_DAILY", 2),
        plus_daily=_i("PLUS_DAILY", 20),
        pro_daily=_i("PRO_DAILY", 100),
        free_max_gb=_f("FREE_MAX_GB", 2.0),
        plus_max_gb=_f("PLUS_MAX_GB", 4.0),
        pro_max_gb=_f("PRO_MAX_GB", 10.0),
        free_expiry_h=_i("FREE_EXPIRY_H", 6),
        plus_expiry_h=_i("PLUS_EXPIRY_H", 24),
        pro_expiry_h=_i("PRO_EXPIRY_H", 168),
        port=int(os.environ.get("PORT", "8080")),
        bind_host=os.environ.get("BIND_HOST", "0.0.0.0"),
        workers=int(os.environ.get("WORKERS", "4")),
    )
