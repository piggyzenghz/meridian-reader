"""Story clustering — group same-event multi-source reports into clusters.

bge-m3 dense embedding + title char-trigram (sparse) + temporal Gaussian decay,
stream-clustered (EACL 2021 style). Embeddings are cached per article; clustering
runs over a recent window only (CPU embedding is slow). The cross-source filter
is the key quality lever: a cluster must span >=2 distinct feeds to surface as an
"event" — same-source template runs (e.g. Yahoo "Is X A Good Stock") never do.
"""
import asyncio
import math
import re
import struct
import time
from collections import Counter

import httpx

from . import config, db

OLLAMA = "http://localhost:11434/api/embeddings"
EMB_MODEL = "bge-m3"
THRESHOLD = 0.60                       # merge if weighted score >= this
W_DENSE, W_SPARSE, W_TIME = 0.60, 0.25, 0.15
WINDOW_DAYS = 4                        # don't merge into clusters older than this
SIGMA_DAYS = 1.5                       # temporal decay width
RECENT_DAYS = 5                        # only cluster the last N days
EMBED_BATCH = 400                      # cap embeddings per incremental run

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
            if pub - c["last_pub"] > WINDOW_DAYS * 86400:
                continue
            dense = _cos_vec(vec, c["vec"])
            sparse = _cos_cnt(tri, c["tri"])
            dt = abs(pub - c["last_pub"]) / 86400
            temporal = math.exp(-(dt * dt) / (2 * SIGMA_DAYS ** 2))
            score = W_DENSE * dense + W_SPARSE * sparse + W_TIME * temporal
            if score > best_score:
                best, best_score = c, score
        if best and best_score >= THRESHOLD:
            n = len(best["members"])
            best["vec"] = [(best["vec"][i] * n + vec[i]) / (n + 1) for i in range(len(vec))]
            best["tri"] += tri
            best["members"].append(a["id"])
            best["feeds"].add(a["feed_id"])
            best["last_pub"] = max(best["last_pub"], pub)
            best["top_title"] = a["title"]   # newest member's title
        else:
            clusters.append({"vec": vec, "tri": tri, "members": [a["id"]],
                             "feeds": {a["feed_id"]}, "last_pub": pub,
                             "first_pub": pub, "top_title": a["title"]})
    now = int(time.time())
    with db.get_db() as conn:
        conn.execute("DELETE FROM clusters")
        conn.execute("UPDATE articles SET cluster_id=0 WHERE cluster_id!=0")
        surfaced = 0
        for c in clusters:
            if len(c["feeds"]) < 2:          # cross-source filter
                continue
            cur = conn.execute(
                "INSERT INTO clusters (top_title, member_count, source_count, "
                "first_seen, last_seen, created_at) VALUES (?,?,?,?,?,?)",
                (c["top_title"][:300], len(c["members"]), len(c["feeds"]),
                 c["first_pub"], c["last_pub"], now))
            cid = cur.lastrowid
            conn.executemany("UPDATE articles SET cluster_id=? WHERE id=?",
                             [(cid, mid) for mid in c["members"]])
            surfaced += 1
    return surfaced


async def run_once() -> int:
    """Embed new articles then rebuild clusters. Called from the refresh loop."""
    if _lock.locked():
        return 0
    async with _lock:
        await embed_recent()
        return await asyncio.to_thread(recluster)
