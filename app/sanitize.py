"""Allowlist HTML sanitizer built on html.parser — keeps article markup safe
to render with innerHTML without pulling in heavyweight dependencies."""
import re
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "p", "br", "hr", "a", "img", "blockquote", "pre", "code", "em", "strong",
    "b", "i", "u", "s", "ul", "ol", "li", "h2", "h3", "h4", "figure",
    "figcaption", "table", "thead", "tbody", "tr", "th", "td", "span", "sub",
    "sup", "mark", "cite",
}
VOID_TAGS = {"br", "hr", "img"}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_SAFE_URL = re.compile(r"^https?://", re.I)


def _safe_url(value: str) -> bool:
    return bool(_SAFE_URL.match(value.strip()))


# Decorative emoji sprites and 1×1 tracking pixels that some feeds inline as
# <img>. They inflate the image count, render as junk in the reader, and — for
# emoji — fool merge_lost_images into thinking the body already has a real
# picture (so the genuine RSS hero never gets prepended).
_IMG_SRC_BLOCKLIST = (
    "s.w.org/images/core/emoji",              # WordPress emoji sprites (e.g. 爱范儿)
    "media.npr.org/include/images/tracking",  # NPR RSS tracking pixel
)


def _blocked_img_src(src: str) -> bool:
    low = src.lower()
    return any(frag in low for frag in _IMG_SRC_BLOCKLIST)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0  # inside <script>/<style> etc.

    SKIP_TAGS = ("script", "style", "iframe", "object", "embed", "form", "noscript")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in ALLOWED_TAGS:
            return
        if tag == "img":
            src = next((v for n, v in attrs if n == "src" and v), "")
            if not _safe_url(src) or _blocked_img_src(src):
                return  # drop tracking pixel / emoji sprite / src-less <img>
        kept: list[str] = []
        for name, value in attrs:
            if value is None:
                continue
            if name not in ALLOWED_ATTRS.get(tag, set()):
                continue
            if name in ("href", "src") and not _safe_url(value):
                continue
            kept.append(f' {name}="{escape(value, quote=True)}"')
        if tag == "a":
            kept.append(' target="_blank" rel="noopener noreferrer"')
        if tag == "img":
            # NO loading="lazy": the reader panel uses container-type (CSS
            # containment) which breaks lazy-loading's viewport math, so lazy
            # images in the body never load. Eager-load body images instead.
            kept.append(' referrerpolicy="no-referrer"')
        close = "/" if tag in VOID_TAGS else ""
        self.out.append(f"<{tag}{''.join(kept)}{close}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.out.append(escape(data))


def sanitize_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return escape(strip_tags(raw))
    result = "".join(parser.out).strip()
    if not result:  # e.g. an unclosed <script> swallowed everything
        text = strip_tags(raw)
        return escape(text) if text else ""
    return result


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def strip_tags(raw: str) -> str:
    """HTML → collapsed plain text."""
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def first_image(raw: str) -> str:
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw or "", re.I)
    if match and _safe_url(match.group(1)):
        return match.group(1)
    return ""


_REAL_IMG = re.compile(r'<img\b[^>]*\bsrc=["\']https?://', re.I)
_RSS_FIGURE = re.compile(r"<figure\b[^>]*>.*?</figure>|<img\b[^>]*>", re.I | re.S)


def merge_lost_images(full_html: str, rss_html: str) -> str:
    """Full-text extractors (trafilatura, Jina Reader) sometimes drop every
    image. When the extracted body has no real image but the RSS body carried
    one, prepend the RSS figure(s) so the reader keeps its visuals instead of
    showing a wall of text. Both inputs are already sanitized, so the fragments
    we splice in are safe to render as-is."""
    if not full_html or _REAL_IMG.search(full_html):
        return full_html
    blocks: list[str] = []
    for frag in _RSS_FIGURE.findall(rss_html or ""):
        if re.search(r'\bsrc=["\']https?://', frag, re.I):
            blocks.append(frag if frag[:7].lower() == "<figure" else f"<figure>{frag}</figure>")
            if len(blocks) >= 3:  # a hero or two, not the whole gallery
                break
    return ("".join(blocks) + full_html) if blocks else full_html


_BLOCK_TAGS = {"p", "h2", "h3", "h4", "li", "blockquote", "figcaption", "td"}
_HEADING_TAGS = {"h2", "h3", "h4"}


class _BlockSplitter(HTMLParser):
    """Split sanitized article HTML into translation-ready blocks:
    {t: 'p'|'h'|'pre'|'img', x: text-or-src}."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str]] = []
        self.buffer: list[str] = []
        self.kind = "p"
        self.in_pre = 0

    def _flush(self, kind: str | None = None) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        self.buffer = []
        if text:
            self.blocks.append({"t": kind or self.kind, "x": text})
        self.kind = "p"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            self._flush()
            src = dict(attrs).get("src", "")
            if src:
                self.blocks.append({"t": "img", "x": src})
            return
        if tag == "pre":
            self._flush()
            self.in_pre += 1
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS:
                self.kind = "h"

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            text = "".join(self.buffer).strip()
            self.buffer = []
            if text:
                self.blocks.append({"t": "pre", "x": text})
            self.in_pre = max(0, self.in_pre - 1)
            return
        if tag in _BLOCK_TAGS:
            self._flush("h" if tag in _HEADING_TAGS else None)

    def handle_data(self, data: str) -> None:
        self.buffer.append(data)


_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’\-]{2,}")


def needs_translation(text: str) -> bool:
    """True when a block reads as foreign-language prose worth translating."""
    if len(text) < 8:
        return False
    latin_words = len(_LATIN_RUN.findall(text))
    if latin_words < 3:
        return False
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk < len(text) * 0.25


def split_blocks(html: str) -> list[dict[str, str]]:
    splitter = _BlockSplitter()
    try:
        splitter.feed(html or "")
        splitter.close()
        splitter._flush()
    except Exception:
        text = strip_tags(html)
        return [{"t": "p", "x": text}] if text else []
    return splitter.blocks
