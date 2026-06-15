"""One-off: populate source_bias for every feed.

International outlets get a hand-seeded profile (AllSides/MBFC-style reasoning,
China-reader frame); the rest are drafted by gpt-5.5. Idempotent (UPSERT) and
re-runnable. All rows land with draft=1 — the boss confirms by editing sqlite
and setting draft=0.

    venv/bin/python -m scripts.backfill_bias            # seed + AI-draft the gaps
    venv/bin/python -m scripts.backfill_bias --dry-run  # print, write nothing
    venv/bin/python -m scripts.backfill_bias --only-missing  # skip feeds already profiled
"""
import argparse
import asyncio
import time

from app import config, db, translate

# title-substring → (lean, factuality). Hand-seeded from public reputation.
SEED: list[tuple[str, str, str]] = [
    # overseas mainstream
    ("BBC", "overseas", "high"), ("Guardian", "overseas", "high"),
    ("NYT", "overseas", "high"), ("New York Times", "overseas", "high"),
    ("Al Jazeera", "overseas", "mixed"), ("NPR", "overseas", "high"),
    ("Politico", "overseas", "high"), ("DW", "overseas", "high"),
    ("France 24", "overseas", "high"), ("CNBC", "overseas", "high"),
    ("MarketWatch", "overseas", "high"), ("Business Insider", "overseas", "mixed"),
    ("Yahoo", "overseas", "mixed"), ("CoinDesk", "overseas", "mixed"),
    ("Reuters", "overseas", "high"), ("Bloomberg", "overseas", "high"),
    ("The Verge", "overseas", "high"), ("Ars Technica", "overseas", "high"),
    ("TechCrunch", "overseas", "high"), ("MIT Tech Review", "overseas", "high"),
    ("Engadget", "overseas", "mixed"), ("Register", "overseas", "mixed"),
    ("Hacker News", "overseas", "mixed"),
    # AI labs / vendor blogs → overseas (official-ish but foreign)
    ("OpenAI", "overseas", "mixed"), ("Google AI", "overseas", "mixed"),
    ("DeepMind", "overseas", "mixed"), ("Hugging Face", "overseas", "mixed"),
    ("VentureBeat", "overseas", "mixed"), ("The Decoder", "overseas", "mixed"),
    ("Anthropic", "overseas", "mixed"), ("Claude", "overseas", "mixed"),
    # independent (personal blogs / newsletters / X accounts)
    ("Simon Willison", "independent", "high"), ("Gradient", "independent", "mixed"),
    ("Ben's Bites", "independent", "mixed"), ("TLDR", "independent", "mixed"),
    ("Interconnects", "independent", "high"), ("One Useful Thing", "independent", "high"),
    ("阮一峰", "independent", "high"), ("宝玉", "independent", "mixed"),
    # market (Chinese commercial tech media)
    ("36氪", "market", "mixed"), ("少数派", "market", "mixed"),
    ("爱范儿", "market", "mixed"), ("量子位", "market", "mixed"),
    ("美团技术", "market", "high"), ("华尔街见闻", "market", "mixed"),
]


def _seed_lean(title: str) -> tuple[str, str] | None:
    for needle, lean, fact in SEED:
        if needle.lower() in (title or "").lower():
            return lean, fact
    return None


def _upsert(conn, feed_id: int, lean: str, fact: str, note: str) -> None:
    conn.execute(
        "INSERT INTO source_bias (feed_id, lean, factuality, note, draft, updated_at)"
        " VALUES (?,?,?,?,1,?) ON CONFLICT(feed_id) DO UPDATE SET"
        " lean=excluded.lean, factuality=excluded.factuality, note=excluded.note,"
        " updated_at=excluded.updated_at",
        (feed_id, lean, fact, note, int(time.time())))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-missing", action="store_true")
    args = ap.parse_args()

    with db.get_db() as conn:
        feeds = [dict(r) for r in conn.execute(
            "SELECT id, title, site_url, category FROM feeds")]
        have = {r["feed_id"] for r in conn.execute("SELECT feed_id FROM source_bias")}

    seeded, need_ai = 0, []
    with db.get_db() as conn:
        for f in feeds:
            if args.only_missing and f["id"] in have:
                continue
            hit = _seed_lean(f["title"])
            if hit:
                lean, fact = hit
                print(f"  seed  {f['title'][:28]:28} → {lean}/{fact}")
                if not args.dry_run:
                    _upsert(conn, f["id"], lean, fact, "seed")
                seeded += 1
            else:
                need_ai.append(f)

    drafted = 0
    if need_ai:
        print(f"\ngpt-5.5 drafting {len(need_ai)} unmatched sources…")
        results = await translate.draft_source_bias(
            [{"i": i, "title": f["title"], "site_url": f["site_url"],
              "category": f["category"]} for i, f in enumerate(need_ai)])
        by_i = {r["i"]: r for r in results}
        with db.get_db() as conn:
            for i, f in enumerate(need_ai):
                r = by_i.get(i)
                if not r:
                    continue
                print(f"  ai    {f['title'][:28]:28} → {r['lean']}/{r['factuality']} ({r['note']})")
                if not args.dry_run:
                    _upsert(conn, f["id"], r["lean"], r["factuality"], r["note"] or "ai-draft")
                drafted += 1

    print(f"\n{'(dry-run) ' if args.dry_run else ''}seeded {seeded}, ai-drafted {drafted}, "
          f"total feeds {len(feeds)}")


if __name__ == "__main__":
    asyncio.run(main())
