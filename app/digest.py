"""Daily digest: assemble the last-24h article pool, have DeepSeek distill it,
cache the result in meta. Generated automatically after DIGEST_HOUR or on demand."""
import asyncio
import datetime
import json
import logging
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db, translate

log = logging.getLogger("meridian.digest")

_lock = asyncio.Lock()
PER_CATEGORY = 30
# Pin to the boss's timezone (CLAUDE.md 东八区铁律) so the digest day key and
# the DIGEST_HOUR trigger are correct even if the VPS is rebuilt under UTC.
_TZ = ZoneInfo("Asia/Shanghai")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_local() -> datetime.datetime:
    return datetime.datetime.now(_TZ)


def today_key() -> str:
    return now_local().strftime("%Y-%m-%d")


def get_cached(day: str) -> dict[str, Any] | None:
    if not _DAY_RE.match(day):
        return None
    with db.get_db() as conn:
        raw = db.get_meta(conn, f"digest:{day}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _collect_sections() -> list[dict[str, Any]]:
    cutoff = int(time.time()) - 24 * 3600
    sections: list[dict[str, Any]] = []
    with db.get_db() as conn:
        for cat in config.CATEGORIES:
            rows = conn.execute(
                """SELECT a.id, a.title, a.title_zh, a.summary
                   FROM articles a JOIN feeds f ON f.id=a.feed_id
                   WHERE f.category=? AND a.published > ?
                   ORDER BY a.published DESC LIMIT ?""",
                (cat, cutoff, PER_CATEGORY),
            ).fetchall()
            if rows:
                sections.append({
                    "cat": cat,
                    "label": config.CATEGORY_LABELS[cat],
                    "articles": [dict(r) for r in rows],
                })
    return sections


async def generate(day: str, force: bool = False) -> dict[str, Any]:
    """Generate (or return cached) digest for the given day."""
    if not force:
        cached = get_cached(day)
        if cached:
            return cached
    async with _lock:
        if not force:  # double-check after waiting on the lock
            cached = get_cached(day)
            if cached:
                return cached
        sections = await asyncio.to_thread(_collect_sections)
        if not sections:
            raise ValueError("no articles in the last 24h")
        data = await translate.make_digest(sections)
        data["generated_at"] = int(time.time())
        data["day"] = day
        with db.get_db() as conn:
            db.set_meta(conn, f"digest:{day}", json.dumps(data, ensure_ascii=False))
        log.info("digest generated for %s (%d sections)", day, len(sections))
        return data


async def ensure_today() -> None:
    """Called from the refresh loop: auto-generate once per day after DIGEST_HOUR."""
    if now_local().hour < config.DIGEST_HOUR:
        return
    day = today_key()
    if get_cached(day):
        return
    try:
        await generate(day)
    except Exception as exc:
        log.warning("auto digest failed: %s", exc)  # retried next loop iteration
