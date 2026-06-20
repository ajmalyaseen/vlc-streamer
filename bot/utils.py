import base64
import hashlib
import hmac
import secrets
from urllib.parse import quote, urlencode

TOKEN_LEN = 8


def make_token(chat_id: int, message_id: int, secret: str, expires_at: int = 0) -> str:
    """Short HMAC token to prevent random people from guessing stream URLs.

    When `expires_at` (unix seconds) is non-zero it is signed into the token so
    the link can be made to expire. `expires_at=0` keeps the original permanent
    behaviour (backward compatible with existing links)."""
    payload = f"{chat_id}:{message_id}:{expires_at}"
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:TOKEN_LEN]


def verify_token(chat_id: int, message_id: int, token: str, secret: str, expires_at: int = 0) -> bool:
    expected = make_token(chat_id, message_id, secret, expires_at)
    return hmac.compare_digest(expected, token)


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


def make_payment_reference() -> str:
    """Short, hard-to-guess payment reference like 'P3F9K2A1'."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars
    return "P" + "".join(secrets.choice(alphabet) for _ in range(7))


def make_payment_token(reference: str, secret: str) -> str:
    """Signed token binding a checkout URL to a payment reference."""
    mac = hmac.new(secret.encode(), reference.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:16]


def verify_payment_token(reference: str, token: str, secret: str) -> bool:
    return hmac.compare_digest(make_payment_token(reference, secret), token)


def build_upi_link(upi_id: str, name: str, amount: int, note: str) -> str:
    """Build a upi://pay deep link openable by GPay/PhonePe/Paytm/BHIM."""
    params = {
        "pa": upi_id,
        "pn": name,
        "am": str(amount),
        "cu": "INR",
        "tn": note,
    }
    return "upi://pay?" + urlencode(params, quote_via=quote)
