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
from .sanitize import sanitize_html, strip_tags

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
    html = sanitize_html(markdown_to_html(md))
    if _is_consent_wall(html):  # the render proxy hit the same cookie wall
        return ""
    return html


# Post-extraction cleanup: trafilatura's balanced mode keeps article-adjacent
# affiliate ads, isolated section labels, and site chrome. Strip the worst of
# it before the body is stored / translated — noise here also wastes DeepSeek
# tokens, since split_blocks would otherwise hand it to the translator.
_LABEL_JUNK = re.compile(
    r"<(p|h[2-4])\b[^>]*>\s*(?:Summary|Quick Read|Key Points|Key Takeaways|"
    r"Advertisement|Read More)\s*</\1>", re.I)
_AD_CTA = re.compile(
    r"<(p|li)\b[^>]*>(?:(?!</\1>).)*?"
    r"(?:Grab the names FREE|named his top \d+|the analyst who called|"
    r"We just covered the)"
    r"(?:(?!</\1>).)*?</\1>", re.I | re.S)
# Affiliate stock-tip farms Yahoo aggregates inline. Drop a list item that is
# wholly such a link, and any anchor pointing at one (bare <a>…Click Here</a>
# that floats outside a paragraph), without nuking a real paragraph it sits in.
_AD_HOSTS = r"247wallst\.com|insidermonkey\.com"
_AD_LINK = re.compile(
    rf"<li\b[^>]*>(?:(?!</li>).)*?(?:{_AD_HOSTS})(?:(?!</li>).)*?</li>", re.I | re.S)
_AD_ANCHOR = re.compile(
    rf'<a\b[^>]*href="[^"]*(?:{_AD_HOSTS})[^"]*"[^>]*>.*?</a>', re.I | re.S)
_CNBC_CHART = re.compile(r"\b[A-Z]{1,6} YTD mountain [A-Z]{1,6} YTD chart\b")
_HF_CHROME = re.compile(
    r"<p\b[^>]*>(?:(?!</p>).)*?"
    r"(?:Enterprise Article|Upvote \d|Published [A-Z][a-z]+ \d)"
    r"(?:(?!</p>).)*?</p>", re.I | re.S)
_EMPTY_ANCHOR = re.compile(r"<a\b[^>]*>\s*</a>")
# 36氪 appends a fixed channel-blurb tail to extracted articles; drop those paras.
_36KR_TAIL = re.compile(
    r"<p>[^<]*(?:推送和解读前沿|一级市场金融信息|聚焦全球优秀创业者|项目融资率接近)[^<]*</p>")

# Cookie-consent walls (Didomi etc.) that trafilatura mistakes for the article
# body — they sail past the length gate, so detect them and fall through to Jina.
_CONSENT_MARKERS = (
    "we and our partners use cookies",
    "accept deny customize",
    "your personal data, your options",
)


def _clean_extracted(html: str, host: str) -> str:
    if not html:
        return html
    html = _LABEL_JUNK.sub("", html)
    html = _AD_CTA.sub("", html)
    html = _AD_LINK.sub("", html)
    html = _AD_ANCHOR.sub("", html)
    html = _CNBC_CHART.sub("", html)
    if "huggingface.co" in host:
        html = _HF_CHROME.sub("", html)
        html = _EMPTY_ANCHOR.sub("", html)
        html = html.replace("undfined", "")  # HF author-name glue artifact
    if "36kr.com" in host:
        html = _36KR_TAIL.sub("", html)
    return html.strip()


def _is_consent_wall(html: str) -> bool:
    text = strip_tags(html).lower()
    return any(marker in text for marker in _CONSENT_MARKERS)


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
            # redirects may have landed on an internal address — re-check the
            # final URL and discard the body if so (don't process internal data)
            await asyncio.to_thread(assert_public_url, str(resp.url))
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
            if extracted:
                host = (urlparse(url).hostname or "").lower()
                polished = _clean_extracted(sanitize_html(extracted), host)
                if len(polished) >= 200 and not _is_consent_wall(polished):
                    return polished

        # bot wall / consent wall / JS-only page — try the rendering proxy
        return await _jina_fallback(url, client)
