"""PIN auth: HMAC-signed expiring session tokens + login rate limiting."""
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

from . import config

_attempts: OrderedDict[str, list[float]] = OrderedDict()
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
MAX_TRACKED_IPS = 10000


def _sign(payload: str) -> str:
    return hmac.new(
        config.ensure_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def create_token() -> str:
    expiry = str(int(time.time()) + config.SESSION_TTL)
    nonce = secrets.token_hex(8)
    payload = f"{expiry}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> bool:
    try:
        expiry, nonce, signature = token.split(".")
    except ValueError:
        return False
    payload = f"{expiry}.{nonce}"
    if not hmac.compare_digest(_sign(payload), signature):
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


def check_pin(pin: str) -> bool:
    if not config.PIN:
        return False
    return hmac.compare_digest(pin.encode(), config.PIN.encode())


def rate_limit(request: Request) -> None:
    ip = (request.headers.get("cf-connecting-ip")
          or (request.client.host if request.client else "?"))
    now = time.time()
    window = [t for t in _attempts.pop(ip, []) if now - t < WINDOW_SECONDS]
    if len(window) >= MAX_ATTEMPTS:
        _attempts[ip] = window
        raise HTTPException(429, "too many attempts, try again later")
    window.append(now)
    _attempts[ip] = window
    while len(_attempts) > MAX_TRACKED_IPS:  # evict oldest, never reset all
        _attempts.popitem(last=False)


def require_session(request: Request) -> None:
    token = request.cookies.get(config.SESSION_COOKIE, "")
    if not token or not verify_token(token):
        raise HTTPException(401, "authentication required")
