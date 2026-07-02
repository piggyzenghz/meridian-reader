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

from . import config, db, extract
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


# --- Post-sanitize body polish -------------------------------------------
# Run on the SANITIZED RSS body (well-formed markup) to strip chrome the
# upstream feed bakes in. Each regex is a no-op on sources that don't carry
# that pattern, so _polish_rss_body is safe to run on every body.
#   • Twitter-RSS (xgo.ing) engagement clusters: <span><span>❤️</span><span>7
#     </span></span>… and the trailing empty <a href="https://xgo.ing"> </a>.
#   • Newsletter/WordPress boilerplate paragraphs ("… appeared first on …",
#     "This is today's edition of …").
_ENGAGEMENT_SPANS = re.compile(
    r"(?:<span><span>[💬🔁🔄❤👀📊🖼♥️]{1,3}</span>"
    r"<span>[\d,.KkMm]+</span></span>)+")
_XGO_LINK = re.compile(
    r'<a\b[^>]*href="https?://xgo\.ing[^"]*"[^>]*>.*?</a>', re.I | re.S)
_EMPTY_ANCHOR = re.compile(r"<a\b[^>]*>\s*</a>")
_BOILERPLATE_P = re.compile(
    r"<p\b[^>]*>(?:(?!</p>).)*?"
    r"(?:appeared first on|this is today[’']s edition of)"
    r"(?:(?!</p>).)*?</p>", re.I | re.S)
# feedx.net full-text mirror (界面新闻 / 联合早报 / WSJ中文) appends a promo footer:
#   …<hr/>获取更多RSS：<a href="https://feedx.net">…</a> <a href="https://feedx.site">…</a>
# always at the very end → strip from the marker (and any leading <hr>/<br>) onward.
_FEEDX_FOOTER = re.compile(r"(?:\s|<br\s*/?>|<hr\s*/?>)*获取更多\s*RSS[：:].*$", re.S)
_EMPTY_LIST = re.compile(r"<(ul|ol)\b[^>]*>\s*</\1>")          # leftover empty list
_LEAD_DATE = re.compile(r"^\s*\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s*(?=<)")  # feedx 快讯 lead date stamp


def _polish_rss_body(html: str) -> str:
    if not html:
        return html
    html = _FEEDX_FOOTER.sub("", html)   # before others: removes the whole tail incl. its <hr>/links
    html = _ENGAGEMENT_SPANS.sub("", html)
    html = _XGO_LINK.sub("", html)
    html = _BOILERPLATE_P.sub("", html)
    html = _EMPTY_LIST.sub("", html)
    html = _LEAD_DATE.sub("", html)
    html = _EMPTY_ANCHOR.sub("", html)  # leftover image/video placeholder anchors
    return html.strip()


def _recompute_gap(conn: sqlite3.Connection, feed_id: int) -> int:
    """Estimate a feed's publish cadence = median gap (secs) between its recent
    articles' publish times. Median (not mean) resists a burst skewing it. 0 =
    too few samples to know."""
    rows = conn.execute(
        "SELECT published FROM articles WHERE feed_id=? AND published>0"
        " ORDER BY published DESC LIMIT ?", (feed_id, config.GAP_SAMPLE_N + 1),
    ).fetchall()
    pubs = [r["published"] for r in rows]
    if len(pubs) < 2:
        return 0
    diffs = sorted(pubs[i] - pubs[i + 1] for i in range(len(pubs) - 1))
    mid = len(diffs) // 2
    median = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) // 2
    return max(0, int(median))


def _next_fetch(gap_sec: int, always_keep: bool, now: int, inserted: int,
                errored: bool, error_count: int) -> tuple[int, str]:
    """Pure scheduler: when to fetch this feed next + a coarse tier label.
    Errored feeds back off exponentially; otherwise the interval tracks the
    feed's publish cadence (check ~twice per publish gap), nudged by whether the
    last fetch brought anything new, then clamped to [MIN, cap]."""
    if errored:
        backoff = min(config.ERROR_BACKOFF_CAP_MIN,
                      config.ERROR_BACKOFF_BASE_MIN * (2 ** min(error_count, 6)))
        return now + backoff * 60, "slow"
    if not config.ADAPTIVE_FETCH:
        return now + config.FETCH_INTERVAL_MIN * 60, "normal"
    interval = gap_sec / 120 if gap_sec > 0 else config.FETCH_INTERVAL_MIN  # min = gap/60/2
    interval *= 0.7 if inserted > 0 else 1.4   # seen something → tighten, quiet → loosen
    cap = config.ADAPTIVE_SLOW_MAX_MIN if always_keep else config.ADAPTIVE_MAX_MIN
    interval = max(config.ADAPTIVE_MIN_MIN, min(cap, interval))
    tier = "fast" if interval <= 15 else "normal" if interval <= 90 else "slow"
    return now + int(interval * 60), tier


def parse_and_store(feed_id: int, body: bytes, conn: sqlite3.Connection) -> int:
    """Parse a feed payload and insert new articles. Returns inserted count."""
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception!r}")
    now = int(time.time())
    prune_cutoff = now - config.KEEP_DAYS * 86400  # same line prune_articles deletes below
    inserted = 0
    stale = 0  # entries whose publish date is already past the age-prune cutoff
    for entry in parsed.entries[:80]:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        if not guid:
            continue
        link = entry.get("link", "")[:2048]
        if not link.startswith(("http://", "https://")):
            link = ""  # never store a javascript:/data: link for the UI to open
        raw_content = _entry_content(entry)
        # HTML body: strip emoji/noise, sanitize, then polish feed chrome.
        content = _polish_rss_body(sanitize_html(_clean_noise(raw_content)))
        plain = _clean_text_field(strip_tags(raw_content))
        summary = (_clean_text_field(strip_tags(entry.get("summary", ""))) or plain)[:360]
        title = _clean_text_field(strip_tags(entry.get("title", "")))[:500]
        published = min(_entry_timestamp(entry), now + 3600)  # clamp future spam
        if published < prune_cutoff:
            stale += 1
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
    # Surface silent zero-yield feeds: a 200 + valid XML that nonetheless lands
    # nothing (all entries duplicates, or — the "live-looking dead feed" case —
    # every item already older than KEEP_DAYS, so the prune at the end of
    # refresh_all deletes them the same cycle they're inserted). error_count
    # stays 0, so without this line these feeds look healthy while never growing.
    n_entries = len(parsed.entries)
    if n_entries and inserted == 0:
        if stale == n_entries:
            log.info("feed %d: parsed %d entries, ALL older than KEEP_DAYS "
                     "(%dd) - nothing will survive prune", feed_id, n_entries,
                     config.KEEP_DAYS)
        else:
            log.info("feed %d: parsed %d entries but stored 0 (all duplicates)",
                     feed_id, n_entries)
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
        # Manual redirect-follow, SSRF-guarding EACH hop's target — a public
        # feed URL that 302s to an internal address (e.g. 169.254.169.254) is
        # rejected before that hop is requested. (client default follow is off
        # here via follow_redirects=False.)
        url = feed["url"]
        body = None
        for _hop in range(6):
            # the stored feed URL is trusted (operator-added — may be an internal
            # RSSHub at 127.0.0.1). Only SSRF-guard REDIRECT targets, which the
            # feed server controls and could point at an internal address.
            if _hop:
                await asyncio.to_thread(extract.assert_public_url, url)
            async with client.stream("GET", url, headers=headers,
                                     follow_redirects=False) as resp:
                loc = resp.headers.get("location", "")
                if resp.status_code in (301, 302, 303, 307, 308) and loc:
                    url = str(resp.url.join(loc))
                    continue
                if resp.status_code == 304:   # confirmed no new content (not a cold feed)
                    with db.get_db() as conn:
                        gap = _recompute_gap(conn, feed["id"])
                        nf, tier = _next_fetch(gap, bool(feed["always_keep"]), now,
                                               inserted=0, errored=False, error_count=0)
                        conn.execute(
                            "UPDATE feeds SET last_fetched=?, last_error='', error_count=0,"
                            " avg_gap=?, next_fetch=?, fetch_tier=? WHERE id=?",
                            (now, gap, nf, tier, feed["id"]),
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
                break
        if body is None:
            raise ValueError("too many redirects")
    except Exception as exc:
        message = _scrub_url(f"{type(exc).__name__}: {exc}")[:300]
        log.warning("fetch failed %s: %s", _scrub_url(feed["url"]), message)
        nf, tier = _next_fetch(0, bool(feed["always_keep"]), now, inserted=0,
                               errored=True, error_count=(feed["error_count"] or 0) + 1)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE feeds SET last_fetched=?, last_error=?,"
                " error_count=error_count+1, next_fetch=?, fetch_tier=? WHERE id=?",
                (now, message, nf, tier, feed["id"]),
            )
        return 0

    def _store() -> int:
        with db.get_db() as conn:
            count = parse_and_store(feed["id"], body, conn)
            gap = _recompute_gap(conn, feed["id"])
            nf, tier = _next_fetch(gap, bool(feed["always_keep"]), now,
                                   inserted=count, errored=False, error_count=0)
            conn.execute(
                "UPDATE feeds SET last_fetched=?, etag=?, last_modified=?,"
                " last_error='', error_count=0, avg_gap=?, next_fetch=?, fetch_tier=?"
                " WHERE id=?",
                (now, resp.headers.get("etag", ""),
                 resp.headers.get("last-modified", ""), gap, nf, tier, feed["id"]),
            )
            return count

    try:
        return await asyncio.to_thread(_store)
    except Exception as exc:
        message = _scrub_url(f"{type(exc).__name__}: {exc}")[:300]
        log.warning("parse failed %s: %s", _scrub_url(feed["url"]), message)
        nf, tier = _next_fetch(0, bool(feed["always_keep"]), now, inserted=0,
                               errored=True, error_count=(feed["error_count"] or 0) + 1)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE feeds SET last_fetched=?, last_error=?,"
                " error_count=error_count+1, next_fetch=?, fetch_tier=? WHERE id=?",
                (now, message, nf, tier, feed["id"]),
            )
        return 0


async def refresh_all(force: bool = False) -> int:
    """Fetch due feeds concurrently. Returns total new articles. With adaptive
    scheduling on (and not force), only feeds whose next_fetch is due are
    fetched; force=True (manual refresh / adaptive off) fetches every feed."""
    if _refresh_lock.locked():
        return 0
    async with _refresh_lock:
        refresh_state["running"] = True
        t0 = time.perf_counter()
        try:
            now = int(time.time())
            with db.get_db() as conn:
                if config.ADAPTIVE_FETCH and not force:
                    feeds = conn.execute(
                        "SELECT * FROM feeds WHERE enabled=1 AND next_fetch<=?", (now,)
                    ).fetchall()
                else:
                    feeds = conn.execute("SELECT * FROM feeds WHERE enabled=1").fetchall()
            if not feeds:   # nothing due this tick — skip prune/refresh bookkeeping
                return 0
            semaphore = asyncio.Semaphore(config.FETCH_CONCURRENCY)
            async with httpx.AsyncClient(
                timeout=config.FETCH_TIMEOUT,
                follow_redirects=False,  # fetch_one follows hops manually w/ per-hop SSRF guard
                headers={"User-Agent": config.USER_AGENT},
            ) as client:

                async def guarded(feed: sqlite3.Row) -> int:
                    async with semaphore:
                        return await fetch_one(client, feed)  # fetch_one SSRF-guards each hop

                results = await asyncio.gather(
                    *(guarded(feed) for feed in feeds), return_exceptions=True
                )
            total = sum(r for r in results if isinstance(r, int))
            with db.get_db() as conn:
                db.prune_articles(conn)
                db.set_meta(conn, "last_refresh", str(int(time.time())))
            refresh_state["last_run"] = int(time.time())
            refresh_state["last_new"] = total
            log.info("refresh done: %d new articles across %d feeds in %.1fs",
                     total, len(feeds), time.perf_counter() - t0)
            return total
        finally:
            refresh_state["running"] = False


async def refresh_loop() -> None:
    """Background task started from app lifespan. With adaptive scheduling the
    loop wakes every SCHEDULER_TICK_MIN to fetch only due feeds, but the heavy
    post-processing (tag / digest / cluster) is batched to once per
    FETCH_INTERVAL_MIN — clustering isn't real-time-critical, and recluster is
    O(n²) at scale, so triggering it on every article trickle would peg the CPU
    continuously. Articles fetched between heavy passes simply wait for the next."""
    from . import cluster, digest, tagger  # late import to avoid a cycle
    await asyncio.sleep(2)
    last_heavy = 0.0
    while True:
        try:
            await refresh_all()
        except Exception:
            log.exception("refresh loop iteration failed")
        now = time.time()
        if (now - last_heavy) >= config.FETCH_INTERVAL_MIN * 60:
            last_heavy = now
            try:
                await tagger.run_once()  # auto-tag newly fetched articles
            except Exception:
                log.exception("tagger failed")
            try:
                await digest.ensure_today()
            except Exception:
                log.exception("digest check failed")
            try:
                await cluster.run_once()  # embed new articles + rebuild story clusters
            except Exception:
                log.exception("clustering failed")
        tick = config.SCHEDULER_TICK_MIN if config.ADAPTIVE_FETCH else config.FETCH_INTERVAL_MIN
        await asyncio.sleep(tick * 60)
