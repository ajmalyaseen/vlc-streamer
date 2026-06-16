import base64
import hashlib
import hmac

TOKEN_LEN = 8


def make_token(chat_id: int, message_id: int, secret: str) -> str:
    """Short HMAC token to prevent random people from guessing stream URLs."""
    payload = f"{chat_id}:{message_id}"
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:TOKEN_LEN]


def verify_token(chat_id: int, message_id: int, token: str, secret: str) -> bool:
    expected = make_token(chat_id, message_id, secret)
    return hmac.compare_digest(expected, token)


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"
