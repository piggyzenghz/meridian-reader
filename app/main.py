"""Meridian — self-hosted bilingual RSS reader. FastAPI application."""
import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (auth, cluster, config, db, digest, discover, extract, favicon,
               fetcher, market, tagger, translate)
from .sanitize import merge_lost_images, needs_translation, split_blocks, strip_tags

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("meridian")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    config.ensure_secret()
    task = asyncio.create_task(fetcher.refresh_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Meridian", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src https: data:; media-src https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'"
    )
    path = request.url.path
    if path.endswith((".js", ".css")) or path == "/":
        # revalidate via ETag every load so a deploy can't leave stale JS/CSS
        # paired with a newer backend (the cause of silent render breakage)
        response.headers["Cache-Control"] = "no-cache"
    elif "/fonts/" in path:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


# ---------------------------------------------------------------- auth

class AuthIn(BaseModel):
    pin: str = Field(min_length=1, max_length=128)


@app.post("/api/auth")
async def login(body: AuthIn, request: Request, response: Response) -> dict[str, Any]:
    auth.rate_limit(request)
    if not auth.check_pin(body.pin):
        raise HTTPException(401, "wrong PIN")
    response.set_cookie(
        config.SESSION_COOKIE, auth.create_token(),
        max_age=config.SESSION_TTL, httponly=True, samesite="lax", secure=True,
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(config.SESSION_COOKIE)
    return {"ok": True}


protected = Depends(auth.require_session)


def _public_feed(feed: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with the RSSHub ?key=... stripped from url/last_error — the
    key is a server-side credential, never client-facing. Copy (not in-place)
    so callers passing shared dicts aren't corrupted."""
    out = dict(feed)
    if "url" in out:
        out["url"] = fetcher._scrub_url(out["url"])
    if out.get("last_error"):
        out["last_error"] = fetcher._scrub_url(out["last_error"])
    return out


# ---------------------------------------------------------------- state

@app.get("/api/state", dependencies=[protected])
async def state() -> dict[str, Any]:
    with db.get_db() as conn:
        feeds = [_public_feed(dict(row)) for row in conn.execute(
            """SELECT f.id, f.url, f.title, f.category, f.site_url, f.enabled,
                      f.last_fetched, f.last_error, f.error_count,
                      COALESCE(u.unread,0) AS unread
               FROM feeds f LEFT JOIN (
                   SELECT feed_id, COUNT(*) AS unread FROM articles
                   WHERE is_read=0 GROUP BY feed_id) u ON u.feed_id=f.id
               ORDER BY f.category, f.title COLLATE NOCASE""").fetchall()]
        unread_by_cat = {row["category"]: row["n"] for row in conn.execute(
            """SELECT f.category AS category, COUNT(*) AS n
               FROM articles a JOIN feeds f ON f.id=a.feed_id
               WHERE a.is_read=0 GROUP BY f.category""").fetchall()}
        starred = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE is_starred=1").fetchone()["n"]
        later = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE read_later=1").fetchone()["n"]
        n_highlights = conn.execute(
            "SELECT COUNT(*) AS n FROM highlights").fetchone()["n"]
        # unread count per tag — drives the sidebar tag list, taxonomy order
        tag_counts = {row["tag"]: row["n"] for row in conn.execute(
            """SELECT at.tag AS tag, COUNT(*) AS n
               FROM article_tags at JOIN articles a ON a.id=at.article_id
               WHERE a.is_read=0 GROUP BY at.tag""").fetchall()}
        monitors = []
        for row in conn.execute(
                "SELECT id, query FROM monitors ORDER BY created_at").fetchall():
            clause, mparams = _monitor_clause(row["query"])
            unread = conn.execute(
                f"SELECT COUNT(*) AS n FROM articles a WHERE a.is_read=0 AND ({clause})",
                mparams).fetchone()["n"]
            monitors.append({"id": row["id"], "query": row["query"], "unread": unread})
        mutes = [dict(r) for r in conn.execute(
            "SELECT id, pattern FROM mutes ORDER BY created_at").fetchall()]
        last_refresh = int(db.get_meta(conn, "last_refresh", "0"))
        used = db.tokens_today(conn, time.strftime("%Y-%m-%d"))
        engines = {f: db.get_meta(conn, f"engine:{f}", config.AI_ENGINES_DEFAULT[f])
                   for f in config.AI_FEATURES}
    tags = [{"tag": t, "count": tag_counts.get(t, 0)}
            for t in config.TAXONOMY if tag_counts.get(t, 0) > 0]
    return {
        "later": later,
        "highlights": n_highlights,
        "tags": tags,
        "monitors": monitors,
        "mutes": mutes,
        "engines": engines,
        "engine_labels": config.AI_ENGINE_LABELS,
        "digest_ready": digest.get_cached(digest.today_key()) is not None,
        "categories": config.CATEGORIES,
        "category_labels": config.CATEGORY_LABELS,
        "feeds": feeds,
        "unread": unread_by_cat,
        "starred": starred,
        "last_refresh": last_refresh,
        "refreshing": fetcher.is_refreshing(),
        "last_new": fetcher.refresh_state["last_new"],
        "tokens_today": used,
        "token_budget": config.DAILY_TOKEN_BUDGET,
    }


# ---------------------------------------------------------------- articles

def _like_escape(term: str) -> str:
    """Escape LIKE metacharacters so user input matches literally (ESCAPE '\\')."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _monitor_clause(query: str) -> tuple[str, list[str]]:
    """AND of LIKE terms over title/summary/content — subscribe to a topic."""
    terms = [t for t in query.split() if t][:5] or [query[:100]]
    clauses, params = [], []
    for term in terms:
        like = f"%{_like_escape(term[:60])}%"
        clauses.append("(a.title LIKE ? ESCAPE '\\' OR a.summary LIKE ? ESCAPE '\\'"
                       " OR a.content LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    return " AND ".join(clauses), params


@app.get("/api/articles", dependencies=[protected])
async def list_articles(
    category: str = "", feed_id: int = 0, filter: str = "all",
    q: str = "", tag: str = "", monitor: str = "", before: int = 0, limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    where, params = ["1=1"], []
    if category and category in config.CATEGORIES:
        where.append("f.category=?")
        params.append(category)
    if feed_id:
        where.append("a.feed_id=?")
        params.append(feed_id)
    if tag in config.TAXONOMY:
        where.append("a.id IN (SELECT article_id FROM article_tags WHERE tag=?)")
        params.append(tag)
    if filter == "unread":
        where.append("a.is_read=0")
    elif filter == "starred":
        where.append("a.is_starred=1")
    elif filter == "later":
        where.append("a.read_later=1")
    if monitor:
        clause, mparams = _monitor_clause(monitor)
        where.append(f"({clause})")
        params += mparams
    if q:
        where.append("(a.title LIKE ? OR a.title_zh LIKE ? OR a.summary LIKE ?)")
        like = f"%{q[:100]}%"
        params += [like, like, like]
    if before:
        where.append("a.published < ?")
        params.append(before)
    apply_mutes = not q and not monitor  # explicit search/monitor overrides mutes
    with db.get_db() as conn:
        if apply_mutes:  # same connection — no race, no extra open
            for r in conn.execute("SELECT pattern FROM mutes LIMIT 100").fetchall():
                pat = f"%{_like_escape(r['pattern'])}%"
                where.append("a.title NOT LIKE ? ESCAPE '\\' AND a.summary NOT LIKE ? ESCAPE '\\'")
                params += [pat, pat]
        rows = conn.execute(
            f"""SELECT a.id, a.feed_id, a.link, a.title, a.title_zh, a.author,
                       a.published, a.summary, a.image, a.is_read, a.is_starred,
                       a.read_later, a.progress, a.word_count,
                       a.body_zh, f.title AS feed_title, f.category
                FROM articles a JOIN feeds f ON f.id=a.feed_id
                WHERE {' AND '.join(where)}
                ORDER BY a.published DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    items = [db.article_row_to_listing(row) for row in rows[:limit]]
    if items:  # attach tags for the whole page in one query
        ids = [it["id"] for it in items]
        ph = ",".join("?" * len(ids))
        with db.get_db() as conn:
            tag_rows = conn.execute(
                f"SELECT article_id, tag FROM article_tags WHERE article_id IN ({ph})",
                tuple(ids)).fetchall()
        by_id: dict[int, list[str]] = {}
        for r in tag_rows:
            if r["tag"] in config.TAXONOMY_SET:  # whitelist at read time
                by_id.setdefault(r["article_id"], []).append(r["tag"])
        for it in items:
            it["tags"] = by_id.get(it["id"], [])
    next_before = items[-1]["published"] if items and has_more else 0
    return {"items": items, "next_before": next_before}


def _get_article(article_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT a.*, f.title AS feed_title, f.category, f.site_url
               FROM articles a JOIN feeds f ON f.id=a.feed_id WHERE a.id=?""",
            (article_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "article not found")
    return dict(row)


def _content_of(article: dict[str, Any]) -> str:
    return article["content_full"] or article["content"]


def _is_paywalled(link: str) -> bool:
    host = (urlparse(link).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in config.PAYWALL_DOMAINS)


def _no_extract(link: str) -> bool:
    """Skip extraction when the RSS already carries the full text (华尔街见闻
    快讯), or the page is a video/live-blog whose extraction is all nav noise."""
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in config.NO_EXTRACT_DOMAINS):
        return True
    path = (parsed.path or "").lower()
    return any(frag in path for frag in config.NO_EXTRACT_PATHS)


def _engine_for(feature: str) -> str:
    """The AI engine chosen for a feature (digest/summary/translate), falling
    back to the configured default. Persisted in the meta table."""
    with db.get_db() as conn:
        return db.get_meta(conn, f"engine:{feature}",
                           config.AI_ENGINES_DEFAULT.get(feature, "deepseek"))


def _src_hash(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()


# Full-text extraction can take up to ~40s (Jina fallback). Doing it inline
# blocks the article response, and a China-route reset kills any connection
# idle >~11s. So extraction runs in the background and the client polls.
_extracting: set[int] = set()


async def _run_extraction(article_id: int, link: str) -> None:
    try:
        full = await extract.fetch_fulltext(link)
    except Exception:
        log.exception("extraction failed for %s", article_id)
        full = ""
    finally:
        with db.get_db() as conn:
            if full:
                row = conn.execute(
                    "SELECT content FROM articles WHERE id=?", (article_id,)
                ).fetchone()
                if row and row["content"]:
                    full = merge_lost_images(full, row["content"])
            conn.execute(
                "UPDATE articles SET extract_tried=1, content_full=? WHERE id=?",
                (full, article_id),
            )
        _extracting.discard(article_id)


def _start_extraction(article: dict[str, Any], force: bool = False) -> bool:
    """Kick off background extraction if it's worth trying. Returns True when an
    extraction is now pending (so the client should poll)."""
    aid = article["id"]
    if (not article["link"] or article["content_full"]
            or _is_paywalled(article["link"]) or _no_extract(article["link"])):
        return False
    if not force and article["extract_tried"]:
        return False
    if aid not in _extracting:
        if len(_extracting) >= 30:  # cap in-flight extractions (quota guard)
            return False
        _extracting.add(aid)
        asyncio.create_task(_run_extraction(aid, article["link"]))
    return True


def _summary_struct(raw: str) -> dict[str, Any] | None:
    """summary_zh column holds either v1 plain text or v2 JSON {tldr, points}."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "tldr" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass
    return {"tldr": raw, "points": []}


def _article_payload(article: dict[str, Any], extracting: bool) -> dict[str, Any]:
    content = _content_of(article)
    body_zh = []
    if article["body_zh"] and article["body_src_hash"] == _src_hash(content):
        body_zh = json.loads(article["body_zh"])
    plain_len = len(strip_tags(content))
    with db.get_db() as conn:
        highlights = [dict(row) for row in conn.execute(
            "SELECT id, text, note, created_at FROM highlights"
            " WHERE article_id=? ORDER BY id LIMIT 500", (article["id"],)).fetchall()]
        tags = [r["tag"] for r in conn.execute(
            "SELECT tag FROM article_tags WHERE article_id=?",
            (article["id"],)).fetchall() if r["tag"] in config.TAXONOMY_SET]
    return {
        "id": article["id"], "feed_id": article["feed_id"],
        "feed_title": article["feed_title"], "category": article["category"],
        "link": article["link"], "title": article["title"],
        "title_zh": article["title_zh"], "author": article["author"],
        "published": article["published"], "content": content,
        "has_fulltext": bool(article["content_full"]),
        "extract_tried": bool(article["extract_tried"]),
        "extracting": extracting,
        "paywalled": _is_paywalled(article["link"]),
        "no_extract": _no_extract(article["link"]),
        "image": article["image"], "body_zh": body_zh,
        "summary_zh": _summary_struct(article["summary_zh"]),
        "is_read": bool(article["is_read"]),
        "is_starred": bool(article["is_starred"]),
        "read_later": bool(article["read_later"]),
        "progress": article["progress"],
        "word_count": plain_len,
        "highlights": highlights,
        "tags": tags,
    }


@app.get("/api/articles/{article_id}", dependencies=[protected])
async def get_article(article_id: int) -> dict[str, Any]:
    """Always fast: returns stored content immediately, auto-starting a
    background extraction for short articles that haven't been tried yet."""
    article = _get_article(article_id)
    plain_len = len(strip_tags(_content_of(article)))
    auto = (not article["content_full"] and not article["extract_tried"]
            and plain_len < 600)
    extracting = _start_extraction(article) if auto else (article_id in _extracting)
    return _article_payload(article, extracting)


@app.post("/api/articles/{article_id}/extract", dependencies=[protected])
async def extract_article(article_id: int) -> dict[str, Any]:
    """Manually (re)trigger background full-text extraction, then poll GET."""
    article = _get_article(article_id)
    if article["content_full"]:
        return {"extracting": False, "has_fulltext": True}
    if _is_paywalled(article["link"]):
        return {"extracting": False, "paywalled": True}
    extracting = _start_extraction(article, force=True)
    return {"extracting": extracting, "has_fulltext": False}


class ReadIn(BaseModel):
    value: bool = True


@app.post("/api/articles/{article_id}/read", dependencies=[protected])
async def mark_read(article_id: int, body: ReadIn) -> dict[str, Any]:
    with db.get_db() as conn:
        conn.execute("UPDATE articles SET is_read=? WHERE id=?",
                     (int(body.value), article_id))
    return {"ok": True}


@app.post("/api/articles/{article_id}/star", dependencies=[protected])
async def toggle_star(article_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        row = conn.execute("SELECT is_starred FROM articles WHERE id=?",
                           (article_id,)).fetchone()
        if not row:
            raise HTTPException(404, "article not found")
        new_value = 0 if row["is_starred"] else 1
        conn.execute("UPDATE articles SET is_starred=? WHERE id=?",
                     (new_value, article_id))
    return {"starred": bool(new_value)}


@app.post("/api/articles/{article_id}/later", dependencies=[protected])
async def toggle_later(article_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        row = conn.execute("SELECT read_later FROM articles WHERE id=?",
                           (article_id,)).fetchone()
        if not row:
            raise HTTPException(404, "article not found")
        new_value = 0 if row["read_later"] else 1
        conn.execute("UPDATE articles SET read_later=? WHERE id=?",
                     (new_value, article_id))
    return {"read_later": bool(new_value)}


class ProgressIn(BaseModel):
    value: float = Field(ge=0, le=100)


@app.post("/api/articles/{article_id}/progress", dependencies=[protected])
async def report_progress(article_id: int, body: ProgressIn) -> dict[str, Any]:
    with db.get_db() as conn:
        conn.execute(
            "UPDATE articles SET progress=MAX(progress, ?) WHERE id=?",
            (round(body.value, 1), article_id))
    return {"ok": True}


# ---------------------------------------------------------------- highlights

class HighlightIn(BaseModel):
    article_id: int
    text: str = Field(min_length=2, max_length=1000)
    note: str = Field(default="", max_length=500)


@app.post("/api/highlights", dependencies=[protected])
async def add_highlight(body: HighlightIn) -> dict[str, Any]:
    with db.get_db() as conn:
        exists = conn.execute("SELECT id FROM articles WHERE id=?",
                              (body.article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "article not found")
        cur = conn.execute(
            "INSERT INTO highlights (article_id, text, note, created_at)"
            " VALUES (?,?,?,?)",
            (body.article_id, body.text.strip(), body.note.strip(),
             int(time.time())))
    return {"id": cur.lastrowid}


@app.get("/api/highlights", dependencies=[protected])
async def list_highlights(limit: int = 200) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT h.id, h.article_id, h.text, h.note, h.created_at,
                      a.title, a.title_zh, f.title AS feed_title, f.category
               FROM highlights h
               JOIN articles a ON a.id=h.article_id
               JOIN feeds f ON f.id=a.feed_id
               ORDER BY h.id DESC LIMIT ?""", (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.delete("/api/highlights/{highlight_id}", dependencies=[protected])
async def delete_highlight(highlight_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM highlights WHERE id=?", (highlight_id,))
        if not cur.rowcount:
            raise HTTPException(404, "highlight not found")
    return {"ok": True}


# ---------------------------------------------------------------- digest

@app.get("/api/digest", dependencies=[protected])
async def get_digest(day: str = "") -> Any:
    day = day or digest.today_key()
    cached = digest.get_cached(day)
    if not cached:
        raise HTTPException(404, "digest not generated yet")
    return cached


@app.post("/api/digest", dependencies=[protected])
async def make_digest_now(force: int = 0) -> Any:  # force is a query param (?force=1)
    try:
        return await digest.generate(digest.today_key(), force=bool(force))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        return _translate_error(exc)


# ---------------------------------------------------------------- discover

class DiscoverIn(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


@app.post("/api/feeds/discover", dependencies=[protected])
async def discover_endpoint(body: DiscoverIn) -> dict[str, Any]:
    try:
        candidates = await discover.discover_feeds(body.url.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"candidates": candidates}


class ReadAllIn(BaseModel):
    category: str = ""
    feed_id: int = 0
    before: int = 0


@app.post("/api/read-all", dependencies=[protected])
async def read_all(body: ReadAllIn) -> dict[str, Any]:
    where, params = ["is_read=0"], []
    if body.category and body.category in config.CATEGORIES:
        where.append("feed_id IN (SELECT id FROM feeds WHERE category=?)")
        params.append(body.category)
    if body.feed_id:
        where.append("feed_id=?")
        params.append(body.feed_id)
    if body.before:
        where.append("published <= ?")
        params.append(body.before)
    with db.get_db() as conn:
        cur = conn.execute(
            f"UPDATE articles SET is_read=1 WHERE {' AND '.join(where)}", params)
    return {"marked": cur.rowcount}


# ---------------------------------------------------------------- translation

def _translate_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, translate.BudgetExceeded):
        return JSONResponse({"error": str(exc)}, status_code=429)
    log.warning("translate failed: %s", exc)  # detail stays in server log only
    return JSONResponse({"error": "翻译服务暂不可用，请稍后再试"}, status_code=502)


@app.post("/api/articles/{article_id}/translate", dependencies=[protected])
async def translate_article(article_id: int) -> Any:
    article = _get_article(article_id)
    content = _content_of(article)
    if not content:
        raise HTTPException(400, "article has no content")
    src_hash = _src_hash(content)
    if article["body_zh"] and article["body_src_hash"] == src_hash:
        return {"blocks": json.loads(article["body_zh"]), "cached": True}
    blocks = split_blocks(content)
    todo = [i for i, blk in enumerate(blocks)
            if blk["t"] in ("p", "h") and needs_translation(blk["x"])]
    if todo:
        try:
            translated = await translate.translate_segments(
                [blocks[i]["x"] for i in todo], engine=_engine_for("translate"))
        except Exception as exc:
            return _translate_error(exc)
        for idx, zh in zip(todo, translated):
            blocks[idx]["z"] = zh
    payload = json.dumps(blocks, ensure_ascii=False)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE articles SET body_zh=?, body_src_hash=? WHERE id=?",
            (payload, src_hash, article_id),
        )
    return {"blocks": blocks, "cached": False}


@app.post("/api/articles/{article_id}/summarize", dependencies=[protected])
async def summarize_article(article_id: int) -> Any:
    article = _get_article(article_id)
    if article["summary_zh"]:
        return {"summary": _summary_struct(article["summary_zh"]), "cached": True}
    text = strip_tags(_content_of(article)) or article["summary"]
    if len(text) < 80:
        raise HTTPException(400, "article too short to summarize")
    try:
        summary = await translate.summarize(article["title"], text,
                                            engine=_engine_for("summary"))
    except Exception as exc:
        return _translate_error(exc)
    with db.get_db() as conn:
        conn.execute("UPDATE articles SET summary_zh=? WHERE id=?",
                     (json.dumps(summary, ensure_ascii=False), article_id))
    return {"summary": summary, "cached": False}


class TitlesIn(BaseModel):
    ids: list[int] = Field(max_length=60)


@app.post("/api/translate-titles", dependencies=[protected])
async def translate_titles(body: TitlesIn) -> Any:
    if not body.ids:
        return {"titles": {}}
    placeholders = ",".join("?" * len(body.ids))
    with db.get_db() as conn:
        rows = conn.execute(
            f"SELECT id, title, title_zh FROM articles WHERE id IN ({placeholders})",
            body.ids,
        ).fetchall()
    result: dict[int, str] = {}
    todo: list[tuple[int, str]] = []
    for row in rows:
        if row["title_zh"]:
            result[row["id"]] = row["title_zh"]
        elif needs_translation(row["title"]):
            todo.append((row["id"], row["title"]))
        else:
            result[row["id"]] = ""
    if todo:
        try:
            translated = await translate.translate_titles(
                [t for _, t in todo], engine=_engine_for("translate"))
        except Exception as exc:
            return _translate_error(exc)
        with db.get_db() as conn:
            for (article_id, _), zh in zip(todo, translated):
                conn.execute("UPDATE articles SET title_zh=? WHERE id=?",
                             (zh, article_id))
                result[article_id] = zh
    return {"titles": result}


class PhraseIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/api/translate-phrase", dependencies=[protected])
async def translate_phrase(body: PhraseIn) -> Any:
    """Translate a selected word / phrase / sentence (selection popover)."""
    try:
        result = await translate.translate_phrase(body.text.strip(),
                                                  engine=_engine_for("translate"))
    except Exception as exc:
        return _translate_error(exc)
    return result


class EngineIn(BaseModel):
    feature: str
    engine: str


@app.post("/api/settings/engine", dependencies=[protected])
async def set_engine(body: EngineIn) -> dict[str, Any]:
    """Switch the AI engine (deepseek/gpt55) used for digest / summary / translate."""
    if body.feature not in config.AI_FEATURES:
        raise HTTPException(400, "unknown feature")
    if body.engine not in config.AI_ENGINE_LABELS:
        raise HTTPException(400, "unknown engine")
    with db.get_db() as conn:
        db.set_meta(conn, f"engine:{body.feature}", body.engine)
    return {"feature": body.feature, "engine": body.engine}


# ---------------------------------------------------------------- markets

@app.get("/api/markets", dependencies=[protected])
async def markets(force: int = 0) -> dict[str, Any]:
    return await market.get_markets(force=bool(force))


# ---------------------------------------------------------------- monitors / mutes

class MonitorIn(BaseModel):
    query: str = Field(min_length=2, max_length=100)


@app.post("/api/monitors", dependencies=[protected])
async def add_monitor(body: MonitorIn) -> dict[str, Any]:
    with db.get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO monitors (query, created_at) VALUES (?,?)",
                (body.query.strip(), int(time.time())))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "monitor already exists")
    return {"id": cur.lastrowid}


@app.delete("/api/monitors/{monitor_id}", dependencies=[protected])
async def delete_monitor(monitor_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        if not cur.rowcount:
            raise HTTPException(404, "monitor not found")
    return {"ok": True}


class MuteIn(BaseModel):
    pattern: str = Field(min_length=2, max_length=80)


@app.post("/api/mutes", dependencies=[protected])
async def add_mute(body: MuteIn) -> dict[str, Any]:
    with db.get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO mutes (pattern, created_at) VALUES (?,?)",
                (body.pattern.strip(), int(time.time())))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "mute already exists")
    return {"id": cur.lastrowid}


@app.delete("/api/mutes/{mute_id}", dependencies=[protected])
async def delete_mute(mute_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM mutes WHERE id=?", (mute_id,))
        if not cur.rowcount:
            raise HTTPException(404, "mute not found")
    return {"ok": True}


# ---------------------------------------------------------------- related

@app.get("/api/articles/{article_id}/related", dependencies=[protected])
async def related_articles(article_id: int) -> dict[str, Any]:
    """Other articles sharing this one's tags, newest first."""
    with db.get_db() as conn:
        tags = [r["tag"] for r in conn.execute(
            "SELECT tag FROM article_tags WHERE article_id=?", (article_id,)).fetchall()]
        if not tags:
            return {"items": []}
        ph = ",".join("?" * len(tags))
        rows = conn.execute(
            f"""SELECT a.id, a.feed_id, a.link, a.title, a.title_zh, a.author,
                       a.published, a.summary, a.image, a.is_read, a.is_starred,
                       a.read_later, a.progress, a.word_count, a.body_zh,
                       f.title AS feed_title, f.category,
                       COUNT(*) AS shared
                FROM article_tags at
                JOIN articles a ON a.id=at.article_id
                JOIN feeds f ON f.id=a.feed_id
                WHERE at.tag IN ({ph}) AND a.id != ?
                GROUP BY a.id
                ORDER BY shared DESC, a.published DESC LIMIT 6""",
            (*tags, article_id)).fetchall()
    return {"items": [db.article_row_to_listing(r) for r in rows]}


# ---------------------------------------------------------------- feeds

class FeedIn(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    category: str = "tech"
    title: str = Field(default="", max_length=200)


@app.post("/api/feeds", dependencies=[protected])
async def add_feed(body: FeedIn) -> dict[str, Any]:
    if body.category not in config.CATEGORIES:
        raise HTTPException(400, "unknown category")
    try:
        await asyncio.to_thread(extract.assert_public_url, body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    now = int(time.time())
    with db.get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO feeds (url, title, category, created_at)"
                " VALUES (?,?,?,?)",
                (body.url.strip(), body.title.strip(), body.category, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "feed already exists")
        feed_id = cur.lastrowid
    with db.get_db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
    async with httpx.AsyncClient(
        timeout=config.FETCH_TIMEOUT, follow_redirects=True,
        headers={"User-Agent": config.USER_AGENT},
    ) as client:
        new_count = await fetcher.fetch_one(client, feed)
    with db.get_db() as conn:
        row = dict(conn.execute(
            "SELECT id, url, title, category, last_error, site_url FROM feeds"
            " WHERE id=?", (feed_id,)).fetchone())
    # fetch_one populated site_url from the feed — grab its favicon in the
    # background so the new source shows a real icon on the next render.
    asyncio.create_task(favicon.fetch_and_store(feed_id, row.get("site_url", "")))
    return {"feed": _public_feed(row), "new_articles": new_count}


class FeedPatch(BaseModel):
    enabled: bool | None = None
    category: str | None = None
    title: str | None = None


@app.patch("/api/feeds/{feed_id}", dependencies=[protected])
async def patch_feed(feed_id: int, body: FeedPatch) -> dict[str, Any]:
    sets, params = [], []
    if body.enabled is not None:
        sets.append("enabled=?")
        params.append(int(body.enabled))
    if body.category is not None:
        if body.category not in config.CATEGORIES:
            raise HTTPException(400, "unknown category")
        sets.append("category=?")
        params.append(body.category)
    if body.title is not None:
        sets.append("title=?")
        params.append(body.title.strip()[:200])
    if not sets:
        raise HTTPException(400, "nothing to update")
    with db.get_db() as conn:
        cur = conn.execute(
            f"UPDATE feeds SET {','.join(sets)} WHERE id=?", (*params, feed_id))
        if not cur.rowcount:
            raise HTTPException(404, "feed not found")
    return {"ok": True}


@app.delete("/api/feeds/{feed_id}", dependencies=[protected])
async def delete_feed(feed_id: int) -> dict[str, Any]:
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
        if not cur.rowcount:
            raise HTTPException(404, "feed not found")
    return {"ok": True}


# ---------------------------------------------------------------- refresh

@app.post("/api/refresh", dependencies=[protected])
async def manual_refresh() -> dict[str, Any]:
    if fetcher.is_refreshing():
        return {"started": False, "running": True}
    asyncio.create_task(fetcher.refresh_all())
    return {"started": True, "running": True}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


# ---------------------------------------------------------------- static

@app.get("/favicon/{feed_id}")
async def feed_favicon(feed_id: int) -> Any:
    """Serve a cached per-feed favicon (public; the UI falls back to a letter
    badge on 404)."""
    path = favicon.favicon_path(feed_id)
    if not path.exists():
        raise HTTPException(404, "no favicon")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/opml", dependencies=[protected])
async def export_opml() -> Response:
    """Export portable feeds as OPML, grouped by category. Internal feeds
    (localhost RSSHub with keys, xgo.ing user ids) are skipped — they depend on
    private infra, aren't portable, and shouldn't leak (open-source 脱敏铁律)."""
    from xml.sax.saxutils import quoteattr
    with db.get_db() as conn:
        feeds = conn.execute(
            "SELECT title, url, site_url, category FROM feeds WHERE enabled=1"
            " ORDER BY category, title COLLATE NOCASE").fetchall()
    by_cat: dict[str, list] = {}
    for f in feeds:
        host = (urlparse(f["url"]).hostname or "").lower()
        if (host in ("localhost", "127.0.0.1", "::1") or "xgo.ing" in host
                or "key=" in f["url"].lower()):
            continue  # non-portable / internal — never export
        by_cat.setdefault(f["category"], []).append(f)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">',
           '<head><title>Meridian feeds</title></head>', '<body>']
    for cat in config.CATEGORIES:
        rows = by_cat.get(cat)
        if not rows:
            continue
        label = config.CATEGORY_LABELS.get(cat, cat)
        out.append(f'  <outline text={quoteattr(label)} title={quoteattr(label)}>')
        for f in rows:
            out.append(
                f'    <outline type="rss" text={quoteattr(f["title"])} '
                f'title={quoteattr(f["title"])} xmlUrl={quoteattr(f["url"])} '
                f'htmlUrl={quoteattr(f["site_url"] or "")}/>')
        out.append('  </outline>')
    out += ['</body>', '</opml>']
    return Response("\n".join(out), media_type="text/x-opml",
                    headers={"Content-Disposition": 'attachment; filename="meridian-feeds.opml"'})


class OpmlIn(BaseModel):
    opml: str = Field(min_length=1, max_length=2_000_000)


@app.post("/api/opml/import", dependencies=[protected])
async def import_opml(body: OpmlIn) -> dict[str, Any]:
    """Import feeds from pasted OPML. Parses with the stdlib (defused against
    entity expansion by disabling DTD), caps the count, validates each URL via
    the existing SSRF guard, then reuses the add_feed fetch+favicon pipeline."""
    import xml.etree.ElementTree as ET
    valid_cats = set(config.CATEGORIES)
    try:
        # ET doesn't expand external entities by default; the size cap above
        # bounds billion-laughs-style blowups.
        root = ET.fromstring(body.opml)
    except ET.ParseError as exc:
        raise HTTPException(400, f"invalid OPML: {exc}")
    candidates: list[tuple[str, str, str]] = []
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = (outline.get("title") or outline.get("text") or url)[:200].strip()
        cat = (outline.get("category") or "tech").strip()
        candidates.append((url, title, cat if cat in valid_cats else "tech"))
        if len(candidates) >= 300:  # hard cap
            break
    # SSRF guard BEFORE insert: only public URLs ever reach the feeds table, so
    # a later refresh_all can't be tricked into probing internal/metadata hosts
    # (e.g. 169.254.169.254). Each check is bounded so a list of unresolvable
    # hostnames can't hold the request open.
    async def _is_public(url: str) -> bool:
        try:
            await asyncio.wait_for(asyncio.to_thread(extract.assert_public_url, url), 5.0)
            return True
        except Exception:
            return False
    checks = await asyncio.gather(*(_is_public(u) for u, _, _ in candidates))
    safe = [c for c, ok in zip(candidates, checks) if ok]
    now = int(time.time())
    added = []
    with db.get_db() as conn:
        for url, title, cat in safe:
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO feeds (url, title, category, created_at)"
                    " VALUES (?,?,?,?)", (url, title, cat, now))
                if cur.rowcount:
                    added.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                continue
    # URLs already SSRF-checked; hydrate just fetches + grabs favicons.
    async def _hydrate() -> None:
        async with httpx.AsyncClient(
            timeout=config.FETCH_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT}) as client:
            for fid in added:
                with db.get_db() as conn:
                    feed = conn.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone()
                try:
                    await fetcher.fetch_one(client, feed)
                    with db.get_db() as conn:
                        site = conn.execute("SELECT site_url FROM feeds WHERE id=?", (fid,)).fetchone()["site_url"]
                    await favicon.fetch_and_store(fid, site)
                except Exception:
                    pass
    if added:
        asyncio.create_task(_hydrate())
    return {"imported": len(added), "seen": len(candidates)}


@app.get("/api/clusters", dependencies=[protected])
async def list_clusters() -> dict[str, Any]:
    """Surfaced event clusters (same event across >=2 sources), newest first."""
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, top_title, title_zh, heat, member_count, source_count, "
            "first_seen, last_seen FROM clusters "
            "ORDER BY heat DESC, source_count DESC, last_seen DESC LIMIT 60")]
        refreshed = conn.execute("SELECT MAX(created_at) r FROM clusters").fetchone()["r"]
    return {"clusters": rows, "refreshed_at": refreshed or 0,
            "window_days": cluster.RECENT_DAYS}


@app.get("/api/cluster/{cluster_id}", dependencies=[protected])
async def cluster_members(cluster_id: int) -> dict[str, Any]:
    """A cluster's member articles — each source's OWN title (not translated),
    so the framing differences across outlets are visible."""
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT a.id, a.title, a.title_zh, a.link, a.published, a.image, "
            "a.summary, f.id AS feed_id, f.title AS feed_title, f.category, f.site_url "
            "FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE a.cluster_id=? ORDER BY a.published DESC", (cluster_id,))]
    return {"members": rows, "count": len(rows)}


@app.get("/api/cluster/{cluster_id}/summary", dependencies=[protected])
async def cluster_summary(cluster_id: int) -> Any:
    """gpt-5.5 event synthesis (overview / progress / takeaway), cached by event
    title + member count so it only regenerates when the event develops."""
    with db.get_db() as conn:
        cl = conn.execute("SELECT top_title, member_count FROM clusters WHERE id=?",
                          (cluster_id,)).fetchone()
        if not cl:
            raise HTTPException(404, "cluster not found")
        members = [dict(r) for r in conn.execute(
            "SELECT a.title, a.summary, f.title AS feed_title FROM articles a "
            "JOIN feeds f ON f.id=a.feed_id WHERE a.cluster_id=? ORDER BY a.published",
            (cluster_id,))]
        key = "csum:" + hashlib.sha1(cl["top_title"].encode()).hexdigest()[:16]
        cached = db.get_meta(conn, key)
    if cached:
        try:
            obj = json.loads(cached)
            if obj.get("n") == cl["member_count"]:
                return {"summary": obj["s"], "cached": True}
        except json.JSONDecodeError:
            pass
    try:
        s = await translate.summarize_cluster(cl["top_title"], members,
                                              engine=_engine_for("summary"))
    except Exception as exc:
        return _translate_error(exc)
    with db.get_db() as conn:
        db.set_meta(conn, key, json.dumps({"s": s, "n": cl["member_count"]},
                                          ensure_ascii=False))
    return {"summary": s, "cached": False}


@app.get("/api/cluster/{cluster_id}/analysis", dependencies=[protected])
async def cluster_analysis(cluster_id: int) -> Any:
    """gpt-5.5 deep event analysis. Cached under a STABLE event identity (the
    Chinese title, which survives reclustering). Only regenerated on a BIG change
    — a new source joins, or the report count grows by >=1/3 — so routine +1
    article churn reuses the cached analysis instead of re-billing gpt-5.5."""
    with db.get_db() as conn:
        cl = conn.execute(
            "SELECT top_title, title_zh, source_count, member_count, first_seen "
            "FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        if not cl:
            raise HTTPException(404, "cluster not found")
        members = [dict(r) for r in conn.execute(
            "SELECT a.title, a.summary, f.title AS feed_title FROM articles a "
            "JOIN feeds f ON f.id=a.feed_id WHERE a.cluster_id=? ORDER BY a.published",
            (cluster_id,))]
        # stable across reclustering; first_seen disambiguates same-title events
        # from different periods so they don't collide on one cache entry
        ident = f"{cl['title_zh'] or cl['top_title']}|{cl['first_seen']}"
        key = "canalysis:" + hashlib.sha1(ident.encode()).hexdigest()[:16]
        cached = db.get_meta(conn, key)
    if cached:
        try:
            c = json.loads(cached)
            sc, mc = c.get("sc", 0), c.get("mc", 0)
            src_grew = cl["source_count"] > sc                    # a new outlet joined
            mem_jump = cl["member_count"] - mc >= max(4, mc // 2)  # +50% (or +4) reports
            if isinstance(c.get("a"), dict) and not src_grew and not mem_jump:
                return {"analysis": c["a"], "cached": True}
        except (json.JSONDecodeError, AttributeError):
            pass
    try:
        a = await translate.analyze_cluster(cl["top_title"], members,
                                            engine=_engine_for("summary"))
    except Exception as exc:
        return _translate_error(exc)
    with db.get_db() as conn:
        db.set_meta(conn, key, json.dumps(
            {"a": a, "sc": cl["source_count"], "mc": cl["member_count"]},
            ensure_ascii=False))
    return {"analysis": a, "cached": False}


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


def _asset_version() -> str:
    """Content fingerprint of the JS+CSS bundle. Injected into the asset URLs
    so a Cloudflare edge cache can't serve stale JS against a newer backend
    (CF caches by full URL incl. query string)."""
    digest = hashlib.sha1()
    for name in ("app.js", "style.css"):
        try:
            digest.update((config.STATIC_DIR / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:10]


@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)  # uptime probes use HEAD
async def index() -> HTMLResponse:
    html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__ASSETV__", _asset_version()))
