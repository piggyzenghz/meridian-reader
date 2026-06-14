"""PIN auth: HMAC-signed expiring session tokens + login rate limiting."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

from . import config

_attempts: OrderedDict[str, list[float]] = OrderedDict()
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
MAX_TRACKED_IPS = 10000

_ai_calls: OrderedDict[str, list[float]] = OrderedDict()
AI_MAX_PER_MIN = 60                     # per-IP cap on token-burning AI endpoints
_limit_lock = threading.Lock()          # sync Depends run in FastAPI's threadpool


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


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "?"))


def _sliding_limit(store: "OrderedDict[str, list[float]]", ip: str,
                   max_n: int, window: int, msg: str) -> None:
    now = time.time()
    with _limit_lock:  # read-modify-write must be atomic across threadpool workers
        hits = [t for t in store.pop(ip, []) if now - t < window]
        if len(hits) >= max_n:
            store[ip] = hits
            raise HTTPException(429, msg)
        hits.append(now)
        store[ip] = hits
        while len(store) > MAX_TRACKED_IPS:  # evict oldest, never reset all
            store.popitem(last=False)


def rate_limit(request: Request) -> None:
    """Login attempts — strict brute-force defence."""
    _sliding_limit(_attempts, _client_ip(request), MAX_ATTEMPTS, WINDOW_SECONDS,
                   "too many attempts, try again later")


def rate_limit_ai(request: Request) -> None:
    """AI endpoints (FastAPI dependency): cap per-IP burst so a leaked or shared
    session can't drain the daily token budget — the budget guard only refuses
    AFTER tokens are spent."""
    _sliding_limit(_ai_calls, _client_ip(request), AI_MAX_PER_MIN, 60,
                   "请求过于频繁，请稍后再试")


def require_session(request: Request) -> None:
    token = request.cookies.get(config.SESSION_COOKIE, "")
    if not token or not verify_token(token):
        raise HTTPException(401, "authentication required")
