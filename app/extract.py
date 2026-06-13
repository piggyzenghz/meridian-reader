"""On-demand full-text extraction: trafilatura first, Jina Reader fallback
for bot-walled sites. Also hosts the SSRF guard shared with feed management."""
import asyncio
import ipaddress
import logging
import re
import socket
from html import escape
from urllib.parse import urlparse

import httpx
import trafilatura

from . import config
from .sanitize import sanitize_html

log = logging.getLogger("meridian.extract")

MIN_USEFUL_CHARS = 600
JINA_TIMEOUT = 40.0          # Jina renders JS-heavy pages, give it room
_jina_gate = asyncio.Semaphore(2)  # free tier is 20 RPM — stay polite


def assert_public_url(url: str) -> None:
    """Raise ValueError unless url resolves only to global unicast addresses.
    Blocking (DNS) — call via asyncio.to_thread from async code."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid url")
    host = parsed.hostname.strip("[]")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith(".local"):
            raise ValueError("private address not allowed")
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise ValueError("cannot resolve host")
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addrs or any(not addr.is_global for addr in addrs):
        raise ValueError("private address not allowed")


_MD_IMG = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)[^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)[^)]*\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _md_inline(text: str) -> str:
    """Escape then re-introduce the few inline marks we support."""
    out = escape(text)
    out = _MD_LINK.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        out)
    out = _MD_BOLD.sub(r"<strong>\1</strong>", out)
    return out


def markdown_to_html(md: str) -> str:
    """Tiny markdown→HTML for Jina Reader output (paragraphs, headings,
    images, quotes, lists, fenced code). Not a general-purpose converter."""
    lines = md.splitlines()
    blocks: list[str] = []
    para: list[str] = []
    in_code = False
    code: list[str] = []
    in_list = False

    def flush_para() -> None:
        nonlocal para
        text = " ".join(part.strip() for part in para).strip()
        para = []
        if text:
            blocks.append(f"<p>{_md_inline(text)}</p>")

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                blocks.append(f"<pre>{escape(chr(10).join(code))}</pre>")
                code, in_code = [], False
            else:
                flush_para(); close_list()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        stripped = line.strip()
        img = _MD_IMG.match(stripped)
        if img:
            flush_para(); close_list()
            blocks.append(f'<img src="{escape(img.group(1), quote=True)}" alt="">')
            continue
        if stripped.startswith("#"):
            flush_para(); close_list()
            level = min(len(stripped) - len(stripped.lstrip("#")), 4)
            text = stripped.lstrip("#").strip()
            if text:
                blocks.append(f"<h{max(level, 2)}>{_md_inline(text)}</h{max(level, 2)}>")
            continue
        if stripped.startswith(">"):
            flush_para(); close_list()
            blocks.append(f"<blockquote><p>{_md_inline(stripped.lstrip('> '))}</p></blockquote>")
            continue
        if re.match(r"^[-*+]\s+", stripped):
            flush_para()
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            item_text = re.sub(r"^[-*+]\s+", "", stripped)
            blocks.append(f"<li>{_md_inline(item_text)}</li>")
            continue
        if not stripped:
            flush_para(); close_list()
            continue
        para.append(line)
    flush_para(); close_list()
    if in_code and code:
        blocks.append(f"<pre>{escape(chr(10).join(code))}</pre>")
    return "\n".join(blocks)


async def _jina_fallback(url: str, client: httpx.AsyncClient) -> str:
    """Fetch via r.jina.ai (rendering proxy) and convert markdown to HTML."""
    headers = {"X-Return-Format": "markdown"}
    if config.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"
    async with _jina_gate:
        try:
            resp = await client.get(f"https://r.jina.ai/{url}",
                                    headers=headers, timeout=JINA_TIMEOUT)
        except Exception as exc:
            log.info("jina fetch failed %s: %s", url, exc)
            return ""
    if resp.status_code != 200:
        log.info("jina http %s for %s", resp.status_code, url)
        return ""
    md = resp.text
    # default plain format carries a metadata preamble before the body
    if "Markdown Content:" in md[:2000]:
        md = md.split("Markdown Content:", 1)[1]
    if len(md.strip()) < 300:
        return ""
    return sanitize_html(markdown_to_html(md))


async def fetch_fulltext(url: str) -> str:
    """Return sanitized article HTML, or '' when all extraction paths fail."""
    if not url.startswith("http"):
        return ""
    try:
        await asyncio.to_thread(assert_public_url, url)
    except ValueError as exc:
        log.info("fulltext blocked %s: %s", url, exc)
        return ""
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": config.USER_AGENT,
                 "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        html = ""
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            log.info("fulltext fetch failed %s: %s", url, exc)

        if html:
            def _extract() -> str:
                try:
                    # Balanced mode (no favor_precision): precision mode is
                    # aggressive and strips inline article images, which is the
                    # whole bug we're fixing. Balanced still detects the article
                    # boundary, so noise stays low while images survive.
                    result = trafilatura.extract(
                        html, url=url, output_format="html",
                        include_images=True, include_links=True,
                        include_comments=False,
                    )
                    return result or ""
                except Exception as exc:  # trafilatura can raise on odd markup
                    log.info("trafilatura failed %s: %s", url, exc)
                    return ""

            extracted = await asyncio.to_thread(_extract)
            if len(extracted) >= 200:
                return sanitize_html(extracted)

        # bot wall / JS-only page — try the rendering proxy before giving up
        return await _jina_fallback(url, client)
