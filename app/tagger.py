"""Background auto-tagger: batches untagged articles through DeepSeek and
writes their taxonomy tags into article_tags. Runs from the refresh loop."""
import asyncio
import logging

from . import config, db, translate

log = logging.getLogger("meridian.tagger")

_lock = asyncio.Lock()
BATCH = 40  # articles per DeepSeek call


def _pending(limit: int) -> list[dict]:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, summary FROM articles "
            "WHERE tagged=0 ORDER BY published DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _store(tag_map: dict[int, list[str]], all_ids: list[int]) -> None:
    with db.get_db() as conn:
        for aid, tags in tag_map.items():
            for tag in tags:
                conn.execute(
                    "INSERT OR IGNORE INTO article_tags (article_id, tag)"
                    " VALUES (?,?)", (aid, tag))
        # mark every article we attempted as tagged (even if it got 0 tags) so
        # we don't keep re-paying for the same untaggable items
        conn.executemany("UPDATE articles SET tagged=1 WHERE id=?",
                         [(i,) for i in all_ids])


async def run_once() -> int:
    """Tag up to TAG_BATCH_PER_CYCLE newest untagged articles. Returns count."""
    if _lock.locked():
        return 0
    async with _lock:
        pending = await asyncio.to_thread(_pending, config.TAG_BATCH_PER_CYCLE)
        if not pending:
            return 0
        total = 0
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            try:
                tag_map = await translate.assign_tags(chunk)
            except translate.BudgetExceeded:
                log.info("tagger stopped: daily token budget reached")
                break
            except Exception as exc:
                # transient (network/API) failure — leave tagged=0 to retry
                # next cycle. Only a successful call marks the chunk tagged.
                log.warning("tag batch failed (will retry): %s", exc)
                continue
            await asyncio.to_thread(_store, tag_map, [c["id"] for c in chunk])
            total += sum(len(v) for v in tag_map.values())
        log.info("tagged %d articles, %d tags assigned", len(pending), total)
        return total
