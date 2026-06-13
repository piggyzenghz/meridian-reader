"""Per-feed favicon fetch + cache.

Uses Google's favicon service, which follows through to a real PNG/JPEG for
virtually any domain (including Chinese sites), and stores one file per feed
under data/favicons/. x.com / twitter.com feeds are skipped — they'd all share
one X logo, so the UI keeps its distinguishing letter badge for those.
"""
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("meridian.favicon")

FAVICON_DIR = config.DB_PATH.parent / "favicons"
GOOGLE_FAVICON = "https://www.google.com/s2/favicons"
# Google returns a tiny blank-globe placeholder for domains with no icon; real
# icons measured 500-2000B, the placeholder is well under this.
MIN_FAVICON_BYTES = 120


def favicon_path(feed_id: int) -> Path:
    return FAVICON_DIR / f"{feed_id}.png"


def _domain(site_url: str) -> str:
    return (urlparse(site_url or "").hostname or "").lower()


def is_generic(site_url: str) -> bool:
    """x.com / twitter.com sources share one logo — not worth a favicon."""
    host = _domain(site_url)
    return host in ("x.com", "twitter.com") or host.endswith((".x.com", ".twitter.com"))


async def fetch_and_store(feed_id: int, site_url: str) -> bool:
    """Fetch this feed's favicon and cache it. Returns True on success."""
    host = _domain(site_url)
    if not host or is_generic(site_url):
        return False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(GOOGLE_FAVICON, params={"domain": host, "sz": "64"})
        if resp.status_code != 200 or len(resp.content) < MIN_FAVICON_BYTES:
            return False
        FAVICON_DIR.mkdir(parents=True, exist_ok=True)
        favicon_path(feed_id).write_bytes(resp.content)
        return True
    except Exception as exc:
        log.info("favicon fetch failed feed=%s host=%s: %s", feed_id, host, exc)
        return False
