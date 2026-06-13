"""Feed auto-discovery (RSSHub-Radar idea): given any site URL, find its
feeds via <link rel="alternate"> plus common-path probing."""
import asyncio
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from . import config
from .extract import assert_public_url

log = logging.getLogger("meridian.discover")

COMMON_PATHS = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
                "/index.xml", "/feed/", "/blog/feed"]
_FEED_TYPES = ("application/rss+xml", "application/atom+xml",
               "application/feed+json")


class _LinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []  # (href, title)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k: (v or "") for k, v in attrs}
        if (a.get("rel", "").lower() == "alternate"
                and a.get("type", "").lower() in _FEED_TYPES and a.get("href")):
            self.found.append((a["href"], a.get("title", "")))


def _looks_like_feed(body: bytes) -> tuple[bool, str]:
    """Cheap parse check; returns (is_feed, feed_title)."""
    parsed = feedparser.parse(body[:512_000])
    if parsed.entries:
        return True, (parsed.feed.get("title") or "")[:200]
    return False, ""


async def discover_feeds(url: str, limit: int = 5) -> list[dict[str, str]]:
    """Return verified candidate feeds for a URL: [{url, title}]."""
    await asyncio.to_thread(assert_public_url, url)  # SSRF guard (raises)
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True,
        headers={"User-Agent": config.USER_AGENT},
    ) as client:

        async def verify(candidate: str) -> None:
            if candidate in seen:
                return
            seen.add(candidate)
            try:
                await asyncio.to_thread(assert_public_url, candidate)
                resp = await client.get(candidate)
                if resp.status_code != 200:
                    return
                # follow_redirects may have left the public IP — re-check final URL
                await asyncio.to_thread(assert_public_url, str(resp.url))
                ok, title = await asyncio.to_thread(_looks_like_feed, resp.content)
                if ok and len(results) < limit:  # limit check after awaits, pre-append
                    results.append({"url": str(resp.url), "title": title})
            except Exception as exc:
                log.debug("candidate failed %s: %s", candidate, exc)

        # 1. the URL itself might already be a feed
        await verify(url)
        if results:
            return results

        # 2. parse the page for <link rel=alternate>
        html = ""
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                await asyncio.to_thread(assert_public_url, str(resp.url))  # post-redirect
                html = resp.text[:600_000]
        except Exception as exc:
            log.info("discover fetch failed %s: %s", url, exc)
        if html:
            finder = _LinkFinder()
            try:
                finder.feed(html)
            except Exception:
                pass
            for href, _title in finder.found[:8]:
                await verify(urljoin(url, href))

        # 3. probe common feed paths off the site root
        if not results:
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            await asyncio.gather(*(verify(base + p) for p in COMMON_PATHS))

    # de-dup by normalized URL, keep order
    unique: list[dict[str, str]] = []
    norm_seen: set[str] = set()
    for r in results:
        key = re.sub(r"/$", "", r["url"].lower())
        if key not in norm_seen:
            norm_seen.add(key)
            unique.append(r)
    return unique[:limit]
