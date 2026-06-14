"""Market quotes for the briefing sidebar — Yahoo Finance, cached in-process."""
import asyncio
import logging
import time
from typing import Any

import httpx

from . import config

log = logging.getLogger("meridian.market")

_cache: dict[str, Any] = {"ts": 0, "data": []}
_lock = asyncio.Lock()
YF = "https://query{}.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1mo"
# Plain UA — matches the console's proven-working Yahoo fetch on this VPS.
_HEADERS = {"User-Agent": "Mozilla/5.0"}


async def _quote(client: httpx.AsyncClient, name: str, symbol: str) -> dict[str, Any] | None:
    try:
        for host in (1, 2, 1):  # query1 → query2 → query1, backing off on 429
            resp = await client.get(YF.format(host, symbol))
            if resp.status_code != 429:
                resp.raise_for_status()
                break
            await asyncio.sleep(1.2)
        else:
            resp.raise_for_status()  # all three were 429 — raise the last one
        result = resp.json()["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        quote_list = result.get("indicators", {}).get("quote") or [{}]
        closes = [c for c in (quote_list[0].get("close") or []) if c is not None]
        if not prev and len(closes) >= 2:  # fall back to the penultimate daily close
            prev = closes[-2]
            price = price or closes[-1]
        if price is None or not prev:
            return None
        change = price - prev
        # monthly trend sparkline — downsample the daily closes to ~24 points
        spark = closes
        if len(spark) > 24:
            step = len(spark) / 24
            spark = [spark[int(i * step)] for i in range(24)]
        return {
            "name": name, "symbol": symbol,
            "price": round(price, 2),
            "change": round(change, 2),
            "change_pct": round(change / prev * 100, 2),
            "spark": [round(v, 4) for v in spark],
        }
    except Exception as exc:
        log.info("quote failed %s: %s", symbol, exc)
        return None


async def get_markets(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < config.MARKET_CACHE_TTL:
        return {"items": _cache["data"], "ts": int(_cache["ts"]), "cached": True}
    async with _lock:
        if not force and _cache["data"] and time.time() - _cache["ts"] < config.MARKET_CACHE_TTL:
            return {"items": _cache["data"], "ts": int(_cache["ts"]), "cached": True}
        async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as client:
            # sequential — Yahoo rate-limits bursts; 9 symbols ~3s, cached 5min
            quotes = []
            for n, s in config.MARKET_SYMBOLS:
                quotes.append(await _quote(client, n, s))
                await asyncio.sleep(0.15)
        items = [q for q in quotes if q]
        if items:  # keep last good data on a total failure
            _cache["data"] = items
            _cache["ts"] = time.time()
        return {"items": _cache["data"], "ts": int(_cache["ts"]), "cached": False}
