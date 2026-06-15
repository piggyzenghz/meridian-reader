"""Source-bias aggregation for the Events bias bar (Ground News style).

Pure functions over a cluster's per-source lean labels → a distribution + a
Blindspot flag. China-reader frame (see config.LEAN_ORDER): the question is
whether an event is reported only by 海外, only by 国内市场化, etc.
"""
from typing import Any

from . import config

# An event is "blindspotted" on an axis when, with enough known sources, one of
# the two opposing perspectives is missing/under-represented.
BLINDSPOT_MIN_SOURCES = 3   # too few known sources → distribution is noise, no flag
BLINDSPOT_PCT = 15          # a side below this share counts as a blindspot
# the two opposing axes a Chinese reader cares about: 海外视角 vs 国内视角
_OPPOSING = (("overseas", "海外视角"), ("market", "国内视角"))


def compute_distribution(leans: list[str]) -> dict[str, Any]:
    """leans = one lean string per DISTINCT source in the cluster.
    Returns {dist:[{lean,count,pct}], sources, total, unknown, blindspot:[label]}."""
    total = len(leans)
    counts: dict[str, int] = {}
    for lean in leans:
        key = lean if lean in config.LEAN_LABELS else "unknown"
        counts[key] = counts.get(key, 0) + 1
    known = total - counts.get("unknown", 0)
    dist = []
    for lean in config.LEAN_ORDER:   # fixed order; only non-zero segments
        c = counts.get(lean, 0)
        if c:
            dist.append({"lean": lean, "count": c,
                         "pct": round(c / total * 100) if total else 0})
    if counts.get("unknown"):   # unknown rendered last as a grey segment
        c = counts["unknown"]
        dist.append({"lean": "unknown", "count": c,
                     "pct": round(c / total * 100) if total else 0})
    blindspot = []
    if known >= BLINDSPOT_MIN_SOURCES:
        for lean, label in _OPPOSING:
            share = counts.get(lean, 0) / known * 100 if known else 0
            if share < BLINDSPOT_PCT:
                blindspot.append(label)
    return {"dist": dist, "sources": known, "total": total,
            "unknown": counts.get("unknown", 0), "blindspot": blindspot}


def summarize_for_cluster(conn, cluster_id: int) -> dict[str, Any]:
    """Bias distribution for one cluster — one lean per distinct member source."""
    rows = conn.execute(
        "SELECT COALESCE(sb.lean,'unknown') AS lean FROM articles a "
        "LEFT JOIN source_bias sb ON sb.feed_id=a.feed_id "
        "WHERE a.cluster_id=? GROUP BY a.feed_id", (cluster_id,)).fetchall()
    return compute_distribution([r["lean"] for r in rows])
