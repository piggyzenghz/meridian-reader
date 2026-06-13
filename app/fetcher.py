"""Feed fetching: conditional HTTP, feedparser parsing, dedup upsert,
background refresh loop."""
import asyncio
import calendar
import logging
import re
import sqlite3
import time
from typing import Any

import feedparser
import httpx

from . import config, db
from .sanitize import first_image, sanitize_html, strip_tags

log = logging.getLogger("meridian.fetcher")

_refresh_lock = asyncio.Lock()
refresh_state: dict[str, Any] = {"running": False, "last_run": 0, "last_new": 0}


def is_refreshing() -> bool:
    return _refresh_lock.locked()


def _entry_timestamp(entry: Any) -> int:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None) or entry.get(key)
        if parsed:
            try:
                return int(calendar.timegm(parsed))
            except Exception:
                continue
    return int(time.time())


def _entry_content(entry: Any) -> str:
    if entry.get("content"):
        try:
            return entry["content"][0].get("value", "") or ""
        except (IndexError, KeyError, TypeError):
            pass
    return entry.get("summary", "") or ""


def _entry_image(entry: Any, content_html: str) -> str:
    for media_key in ("media_content", "media_thumbnail"):
        for media in entry.get(media_key, []) or []:
            url = media.get("url", "")
            if url.startswith("http"):
                return url
    for enclosure in entry.get("enclosures", []) or []:
        href = enclosure.get("href", "")
        if "image" in (enclosure.get("type") or "") and href.startswith("http"):
            return href
    return first_image(content_html)


MAX_FEED_BYTES = 10 * 1024 * 1024  # refuse feeds larger than 10 MB

# Twitter-RSS services append engagement counters + watermarks; strip them.
_TWEET_NOISE = re.compile(
    # engagement counter cluster — require a digit so decorative ❤ in CN prose
    # (and the numeral 万) is never stripped
    r"(?:[💬🔁🔄❤👀📊🖼♥]️?\s*[\d,.KkMm]+\s*){2,}"
    r"|⚡?\s*Powered by xgo\.ing"
    r"|🔗?\s*View on Twitter"
    r"|Your browser does not support the video tag\.?",
    re.U)


def _clean_noise(text: str) -> str:
    cleaned = _TWEET_NOISE.sub(" ", (text or "")[:8000])  # cap guards regex cost
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _strip_trailing_urls(text: str) -> str:
    """Drop trailing bare URL token(s) tweets append (e.g. '...loopholes
    https://t.co/x https://t.co/x'). O(n) split — no regex backtracking."""
    words = text.split()
    while words and words[-1].startswith(("http://", "https://")):
        words.pop()
    result = " ".join(words)
    return result if result else text  # a title that's only a URL keeps the URL


def _clean_text_field(text: str) -> str:
    """For PLAIN-TEXT fields only (title/summary) — never the HTML body, where
    stripping bare URLs would corrupt href attributes."""
    return _strip_trailing_urls(_clean_noise(text)).strip()


def _scrub_url(url: str) -> str:
    """Mask a ?key= / ?access_key= query param so the RSSHub access key never
    lands in logs or the last_error column (which is surfaced by /api/state)."""
    return re.sub(r"([?&](?:access_)?key=)[^&\s]+", r"\1***", url, flags=re.IGNORECASE)


def parse_and_store(feed_id: int, body: bytes, conn: sqlite3.Connection) -> int:
    """Parse a feed payload and insert new articles. Returns inserted count."""
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception!r}")
    now = int(time.time())
    inserted = 0
    for entry in parsed.entries[:80]:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        if not guid:
            continue
        link = entry.get("link", "")[:2048]
        if not link.startswith(("http://", "https://")):
            link = ""  # never store a javascript:/data: link for the UI to open
        raw_content = _entry_content(entry)
        content = sanitize_html(_clean_noise(raw_content))  # HTML: only emoji/noise
        plain = _clean_text_field(strip_tags(raw_content))
        summary = (_clean_text_field(strip_tags(entry.get("summary", ""))) or plain)[:360]
        title = _clean_text_field(strip_tags(entry.get("title", "")))[:500]
        published = min(_entry_timestamp(entry), now + 3600)  # clamp future spam
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (feed_id, guid, link, title, author, published, summary,
                content, image, word_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                feed_id, guid[:1024], link, title,
                strip_tags(entry.get("author", ""))[:200], published, summary,
                content, _entry_image(entry, raw_content)[:2048], len(plain), now,
            ),
        )
        inserted += cur.rowcount
    site_url = (parsed.feed.get("link") or "")[:2048]
    feed_title = strip_tags(parsed.feed.get("title", ""))[:200]
    conn.execute(
        """UPDATE feeds SET site_url=CASE WHEN site_url='' THEN ? ELSE site_url END,
           title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?""",
        (site_url, feed_title, feed_id),
    )
    return inserted


async def fetch_one(client: httpx.AsyncClient, feed: sqlite3.Row) -> int:
    """Fetch a single feed; returns number of new articles."""
    headers: dict[str, str] = {}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]
    now = int(time.time())
    try:
        async with client.stream("GET", feed["url"], headers=headers) as resp:
            if resp.status_code == 304:
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE feeds SET last_fetched=?, last_error='',"
                        " error_count=0 WHERE id=?", (now, feed["id"]),
                    )
                return 0
            resp.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes(65536):
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise ValueError("feed response exceeds 10MB")
                chunks.append(chunk)
            body = b"".join(chunks)
    except Exception as exc:
        message = _scrub_url(f"{type(exc).__name__}: {exc}")[:300]
        log.warning("fetch failed %s: %s", _scrub_url(feed["url"]), message)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE feeds SET last_fetched=?, last_error=?,"
                " error_count=error_count+1 WHERE id=?",
                (now, message, feed["id"]),
            )
        return 0

    def _store() -> int:
        with db.get_db() as conn:
            count = parse_and_store(feed["id"], body, conn)
            conn.execute(
                "UPDATE feeds SET last_fetched=?, etag=?, last_modified=?,"
                " last_error='', error_count=0 WHERE id=?",
                (now, resp.headers.get("etag", ""),
                 resp.headers.get("last-modified", ""), feed["id"]),
            )
            return count

    try:
        return await asyncio.to_thread(_store)
    except Exception as exc:
        message = _scrub_url(f"{type(exc).__name__}: {exc}")[:300]
        log.warning("parse failed %s: %s", _scrub_url(feed["url"]), message)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE feeds SET last_fetched=?, last_error=?,"
                " error_count=error_count+1 WHERE id=?",
                (now, message, feed["id"]),
            )
        return 0


async def refresh_all() -> int:
    """Fetch every enabled feed concurrently. Returns total new articles."""
    if _refresh_lock.locked():
        return 0
    async with _refresh_lock:
        refresh_state["running"] = True
        try:
            with db.get_db() as conn:
                feeds = conn.execute(
                    "SELECT * FROM feeds WHERE enabled=1"
                ).fetchall()
            semaphore = asyncio.Semaphore(config.FETCH_CONCURRENCY)
            async with httpx.AsyncClient(
                timeout=config.FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": config.USER_AGENT},
            ) as client:

                async def guarded(feed: sqlite3.Row) -> int:
                    async with semaphore:
                        return await fetch_one(client, feed)

                results = await asyncio.gather(
                    *(guarded(feed) for feed in feeds), return_exceptions=True
                )
            total = sum(r for r in results if isinstance(r, int))
            with db.get_db() as conn:
                db.prune_articles(conn)
                db.set_meta(conn, "last_refresh", str(int(time.time())))
            refresh_state["last_run"] = int(time.time())
            refresh_state["last_new"] = total
            log.info("refresh done: %d new articles across %d feeds",
                     total, len(feeds))
            return total
        finally:
            refresh_state["running"] = False


async def refresh_loop() -> None:
    """Background task started from app lifespan."""
    from . import digest, tagger  # late import to avoid a cycle
    await asyncio.sleep(2)
    while True:
        try:
            await refresh_all()
        except Exception:
            log.exception("refresh loop iteration failed")
        try:
            await tagger.run_once()  # auto-tag newly fetched articles
        except Exception:
            log.exception("tagger failed")
        try:
            await digest.ensure_today()
        except Exception:
            log.exception("digest check failed")
        await asyncio.sleep(config.FETCH_INTERVAL_MIN * 60)
