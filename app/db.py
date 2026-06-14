"""SQLite layer: schema, connection helpers, queries."""
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'tech',
    site_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    always_keep INTEGER NOT NULL DEFAULT 0,
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    last_fetched INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    error_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    title_zh TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    content_full TEXT NOT NULL DEFAULT '',
    extract_tried INTEGER NOT NULL DEFAULT 0,
    image TEXT NOT NULL DEFAULT '',
    body_zh TEXT NOT NULL DEFAULT '',
    body_src_hash TEXT NOT NULL DEFAULT '',
    summary_zh TEXT NOT NULL DEFAULT '',
    is_read INTEGER NOT NULL DEFAULT 0,
    is_starred INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published DESC);
CREATE INDEX IF NOT EXISTS idx_articles_feed ON articles(feed_id, published DESC);
CREATE INDEX IF NOT EXISTS idx_articles_unread ON articles(is_read, published DESC);

CREATE TABLE IF NOT EXISTS highlights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_highlights_article ON highlights(article_id);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (article_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag);

-- keyword monitors: subscribe to a topic across every feed
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

-- mute rules: hide articles whose title/summary matches a pattern
CREATE TABLE IF NOT EXISTS mutes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- story clusters: same-event multi-source reports grouped together
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    top_title TEXT NOT NULL DEFAULT '',
    centroid BLOB,
    member_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL DEFAULT 0,
    last_seen INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT 0,
    title_zh TEXT NOT NULL DEFAULT '',
    heat INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clusters_lastseen ON clusters(last_seen DESC);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Applied with try/except at startup — sqlite has no IF NOT EXISTS for columns.
COLUMN_MIGRATIONS = [
    "ALTER TABLE articles ADD COLUMN read_later INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE articles ADD COLUMN progress REAL NOT NULL DEFAULT 0",
    "ALTER TABLE articles ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE articles ADD COLUMN tagged INTEGER NOT NULL DEFAULT 0",
    # always_keep feeds (low-frequency quality sources) are exempt from the
    # KEEP_DAYS age-prune so their slow updates aren't deleted before being seen.
    "ALTER TABLE feeds ADD COLUMN always_keep INTEGER NOT NULL DEFAULT 0",
    # story clustering: per-article bge-m3 embedding cache + assigned cluster id
    "ALTER TABLE articles ADD COLUMN cluster_id INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE articles ADD COLUMN embedding BLOB",
    "CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id)",
    # gpt-5.5 event scoring: Chinese event title (左列中英对照) + heat (排序)
    "ALTER TABLE clusters ADD COLUMN title_zh TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE clusters ADD COLUMN heat INTEGER NOT NULL DEFAULT 0",
]


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
        for migration in COLUMN_MIGRATIONS:
            try:
                db.execute(migration)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise  # only swallow the already-applied case, surface real errors
        seeded = db.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not seeded:
            now = int(time.time())
            for url, title, category in config.DEFAULT_FEEDS:
                db.execute(
                    "INSERT OR IGNORE INTO feeds (url, title, category, created_at)"
                    " VALUES (?,?,?,?)",
                    (url, title, category, now),
                )
            db.execute("INSERT INTO meta (key, value) VALUES ('seeded','1')")
    for suffix in ("", "-wal", "-shm"):  # keep article DB private to service user
        path = config.DB_PATH.parent / (config.DB_PATH.name + suffix)
        if path.exists():
            path.chmod(0o600)


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta (key,value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def add_usage(db: sqlite3.Connection, day: str, tokens: int) -> None:
    db.execute(
        "INSERT INTO usage (day, tokens, calls) VALUES (?,?,1)"
        " ON CONFLICT(day) DO UPDATE SET tokens=tokens+excluded.tokens, calls=calls+1",
        (day, tokens),
    )


def tokens_today(db: sqlite3.Connection, day: str) -> int:
    row = db.execute("SELECT tokens FROM usage WHERE day=?", (day,)).fetchone()
    return row["tokens"] if row else 0


def prune_articles(db: sqlite3.Connection) -> int:
    """Drop old / overflow articles (starred always kept). Returns rows deleted."""
    cutoff = int(time.time()) - config.KEEP_DAYS * 86400
    # age-prune everything EXCEPT always_keep feeds (slow-update quality sources
    # whose latest item may itself be >KEEP_DAYS old). They're still bounded by
    # the per-feed MAX_PER_FEED cap below, so the DB can't grow unbounded.
    cur = db.execute(
        "DELETE FROM articles WHERE is_starred=0 AND published < ? AND feed_id"
        " NOT IN (SELECT id FROM feeds WHERE always_keep=1)", (cutoff,)
    )
    deleted = cur.rowcount
    rows = db.execute("SELECT id FROM feeds").fetchall()
    for row in rows:
        # keep newest MAX_PER_FEED per feed (bounds always_keep feeds too so the
        # DB can't grow unbounded); is_starred=0 protects starred items here
        # regardless of their rank in the window.
        cur = db.execute(
            """DELETE FROM articles WHERE feed_id=? AND is_starred=0 AND id NOT IN (
                   SELECT id FROM articles WHERE feed_id=?
                   ORDER BY published DESC LIMIT ?)""",
            (row["id"], row["id"], config.MAX_PER_FEED),
        )
        deleted += cur.rowcount
    return deleted


def article_row_to_listing(row: sqlite3.Row) -> dict[str, Any]:
    """Light projection for list views (no body payloads)."""
    return {
        "id": row["id"],
        "feed_id": row["feed_id"],
        "feed_title": row["feed_title"],
        "category": row["category"],
        "link": row["link"],
        "title": row["title"],
        "title_zh": row["title_zh"],
        "author": row["author"],
        "published": row["published"],
        "summary": row["summary"],
        "image": row["image"],
        "is_read": bool(row["is_read"]),
        "is_starred": bool(row["is_starred"]),
        "read_later": bool(row["read_later"]),
        "progress": row["progress"],
        "word_count": row["word_count"],
        "has_body_zh": bool(row["body_zh"]),
    }
