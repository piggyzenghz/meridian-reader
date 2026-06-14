"""Story clustering — group same-event multi-source reports into clusters.

bge-m3 dense embedding + title char-trigram (sparse) + temporal Gaussian decay,
stream-clustered (EACL 2021 style). Embeddings are cached per article; clustering
runs over a recent window only (CPU embedding is slow). The cross-source filter
is the key quality lever: a cluster must span >=2 distinct feeds to surface as an
"event" — same-source template runs (e.g. Yahoo "Is X A Good Stock") never do.
"""
import asyncio
import hashlib
import json
import logging
import math
import re
import struct
import time
from collections import Counter

import httpx

from . import config, db

log = logging.getLogger(__name__)

OLLAMA = "http://localhost:11434/api/embeddings"
EMB_MODEL = "bge-m3"
THRESHOLD = 0.60                       # merge if weighted score >= this
W_DENSE, W_SPARSE, W_TIME = 0.60, 0.25, 0.15
WINDOW_DAYS = 4                        # don't merge into clusters older than this
SIGMA_DAYS = 1.5                       # temporal decay width
RECENT_DAYS = 5                        # only cluster the last N days
EMBED_BATCH = 400                      # cap embeddings per incremental run
MAX_MEMBERS = 80                       # backstop ceiling — the anchor gate is the real arbiter;
                                       # this only stops pathological runaway, not normal big events
MAX_SPAN_DAYS = WINDOW_DAYS            # a cluster can't span longer than this from its first article
ANCHOR_SIM = 0.50                      # a new article must also resemble the cluster's FIRST article

_lock = asyncio.Lock()


def _pack(vec: list) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list:
    return list(struct.unpack(f"{len(blob) // 4}f", blob)) if blob else []


def _trigrams(text: str) -> Counter:
    t = re.sub(r"\s+", " ", re.sub(r"[^\w ]", "", text.lower())).strip()
    return Counter(t[i:i + 3] for i in range(len(t) - 2)) if len(t) >= 3 else Counter([t])


def _cos_vec(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cos_cnt(a: Counter, b: Counter) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


async def _embed(text: str, client: httpx.AsyncClient) -> list:
    try:
        r = await client.post(OLLAMA, json={"model": EMB_MODEL, "prompt": text[:512]},
                              timeout=30)
        return r.json().get("embedding", [])
    except Exception:
        return []


async def embed_recent() -> int:
    """Incrementally embed recent articles that don't have an embedding yet."""
    cutoff = int(time.time()) - RECENT_DAYS * 86400
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, summary FROM articles WHERE published>? AND title!='' "
            "AND (embedding IS NULL OR embedding='') ORDER BY published DESC LIMIT ?",
            (cutoff, EMBED_BATCH))]
    if not rows:
        return 0
    done = 0
    async with httpx.AsyncClient() as client:
        for a in rows:
            text = a["title"] + ". " + (a["summary"] or "")[:240]
            vec = await _embed(text, client)
            if vec:
                with db.get_db() as conn:
                    conn.execute("UPDATE articles SET embedding=? WHERE id=?",
                                 (_pack(vec), a["id"]))
                done += 1
    return done


def recluster() -> int:
    """Rebuild clusters over the recent window from cached embeddings. Pure
    vector math (no network), safe to run via asyncio.to_thread. Returns the
    number of surfaced (multi-source) clusters."""
    cutoff = int(time.time()) - RECENT_DAYS * 86400
    with db.get_db() as conn:
        arts = [dict(r) for r in conn.execute(
            "SELECT id, title, published, feed_id, embedding FROM articles "
            "WHERE published>? AND embedding IS NOT NULL AND embedding!='' "
            "ORDER BY published ASC", (cutoff,))]
    clusters: list[dict] = []
    for a in arts:
        vec = _unpack(a["embedding"])
        if not vec:
            continue
        tri = _trigrams(a["title"])
        pub = a["published"]
        best, best_score = None, 0.0
        for c in clusters:
            if len(c["members"]) >= MAX_MEMBERS:               # ceiling reached — no more merges
                continue
            if pub - c["last_pub"] > WINDOW_DAYS * 86400:       # latest member too old
                continue
            if pub - c["first_pub"] > MAX_SPAN_DAYS * 86400:    # event has run too long → likely drift
                continue
            dense = _cos_vec(vec, c["vec"])
            sparse = _cos_cnt(tri, c["tri"])
            dt = abs(pub - c["last_pub"]) / 86400
            temporal = math.exp(-(dt * dt) / (2 * SIGMA_DAYS ** 2))
            score = W_DENSE * dense + W_SPARSE * sparse + W_TIME * temporal
            if score > best_score:
                best, best_score = c, score
        # anchor gate: the centroid drifts as members are averaged in, eventually
        # matching almost anything. Also require similarity to the cluster's FIRST
        # (anchor) article — this is what stops the snowball that produced the
        # 255-article junk clusters (unrelated stories sharing one drifted centroid).
        if best and best_score >= THRESHOLD and _cos_vec(vec, best["anchor"]) >= ANCHOR_SIM:
            n = len(best["members"])
            best["vec"] = [(best["vec"][i] * n + vec[i]) / (n + 1) for i in range(len(vec))]
            best["tri"] += tri
            best["members"].append(a["id"])
            best["feeds"].add(a["feed_id"])
            best["last_pub"] = max(best["last_pub"], pub)
            best["top_title"] = a["title"]   # newest member's title
        else:
            clusters.append({"vec": vec, "anchor": vec[:], "tri": tri, "members": [a["id"]],
                             "feeds": {a["feed_id"]}, "last_pub": pub,
                             "first_pub": pub, "top_title": a["title"]})
    now = int(time.time())
    with db.get_db() as conn:
        conn.execute("UPDATE articles SET cluster_id=0 WHERE cluster_id!=0")
        seen: set[str] = set()
        surfaced = 0
        for c in clusters:
            if len(c["feeds"]) < 2:          # cross-source filter
                continue
            ckey = str(c["members"][0])      # anchor article id = stable identity
            seen.add(ckey)
            row = conn.execute("SELECT id FROM clusters WHERE ckey=?", (ckey,)).fetchone()
            if row:                          # same event re-formed → keep id (and its caches)
                cid = row["id"]
                conn.execute(
                    "UPDATE clusters SET top_title=?, member_count=?, source_count=?, "
                    "first_seen=?, last_seen=? WHERE id=?",
                    (c["top_title"][:300], len(c["members"]), len(c["feeds"]),
                     c["first_pub"], c["last_pub"], cid))
            else:
                cur = conn.execute(
                    "INSERT INTO clusters (ckey, top_title, member_count, source_count, "
                    "first_seen, last_seen, created_at) VALUES (?,?,?,?,?,?,?)",
                    (ckey, c["top_title"][:300], len(c["members"]), len(c["feeds"]),
                     c["first_pub"], c["last_pub"], now))
                cid = cur.lastrowid
            conn.executemany("UPDATE articles SET cluster_id=? WHERE id=?",
                             [(cid, mid) for mid in c["members"]])
            surfaced += 1
        if seen:                             # drop clusters that no longer surface
            ph = ",".join("?" * len(seen))
            conn.execute(f"DELETE FROM clusters WHERE ckey NOT IN ({ph})", tuple(seen))
        else:
            conn.execute("DELETE FROM clusters")
        db.set_meta(conn, "clusters_refreshed", str(now))  # recluster timestamp for "更新于"
    return surfaced


def _score_key(cluster_id: int) -> str:
    # keyed on the STABLE cluster id (preserved across reclusters via the ckey
    # UPSERT). The old top_title key drifted every cycle — top_title is the newest
    # member's headline, so each new report changed the key and re-billed gpt-5.5
    # on exactly the hottest events.
    return f"cheat:{cluster_id}"


async def score_clusters() -> int:
    """gpt-5.5: give each surfaced cluster a Chinese title + heat score (for the
    left column's 中英对照 and heat-based sort). Cached by stable cluster id so
    unchanged events aren't re-scored. Returns newly-scored count. Best-effort — clusters
    still work (heat=0 → falls back to source_count sort) if gpt-5.5 is down."""
    from . import translate
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, top_title, source_count, member_count FROM clusters "
            "ORDER BY source_count DESC, member_count DESC LIMIT 80")]
    if not rows:
        return 0
    cached, need = {}, []
    with db.get_db() as conn:
        for r in rows:
            hit = db.get_meta(conn, _score_key(r["id"]))
            try:
                cached[r["id"]] = json.loads(hit) if hit else None
            except json.JSONDecodeError:
                cached[r["id"]] = None
            if cached[r["id"]] is None:
                need.append(r)
    fresh = {}
    if need:
        scored = await translate.score_clusters_ai(need)
        by_i = {s["i"]: s for s in scored}
        for idx, r in enumerate(need):
            s = by_i.get(idx)
            if s:
                fresh[r["id"]] = {"title_zh": s["title_zh"], "heat": s["heat"]}
    with db.get_db() as conn:   # single write block: cache fresh + apply all scores
        for r in need:
            if r["id"] in fresh:
                db.set_meta(conn, _score_key(r["id"]),
                            json.dumps(fresh[r["id"]], ensure_ascii=False))
        for r in rows:
            s = cached.get(r["id"]) or fresh.get(r["id"])
            if s:
                conn.execute("UPDATE clusters SET title_zh=?, heat=? WHERE id=?",
                             (s["title_zh"], s["heat"], r["id"]))
    return len(fresh)


async def run_once() -> int:
    """Embed new articles, rebuild clusters, then gpt-5.5 score them. Called from
    the refresh loop."""
    if _lock.locked():
        return 0
    async with _lock:
        await embed_recent()
        n = await asyncio.to_thread(recluster)
        try:
            await score_clusters()
        except Exception:
            log.warning("score_clusters best-effort failed", exc_info=True)
        return n
