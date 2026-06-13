"""DeepSeek client: paragraph translation, title batches, summaries.
All calls run with thinking disabled and are metered against a daily budget."""
import asyncio
import json
import logging
import time
from typing import Any

import httpx

from . import config, db

log = logging.getLogger("meridian.translate")

BATCH_CHAR_LIMIT = 3000
BATCH_CONCURRENCY = 3


class BudgetExceeded(Exception):
    pass


class TranslateError(Exception):
    pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _check_budget() -> None:
    with db.get_db() as conn:
        used = db.tokens_today(conn, _today())
    if used >= config.DAILY_TOKEN_BUDGET:
        raise BudgetExceeded(
            f"daily token budget reached ({used}/{config.DAILY_TOKEN_BUDGET})"
        )


def _record_usage(tokens: int) -> None:
    with db.get_db() as conn:
        db.add_usage(conn, _today(), tokens)


def _provider(engine: str) -> dict[str, Any]:
    """Resolve an engine id to endpoint/model/key. Read at call time so a key
    added to the env after import is still picked up. gpt55 = the secondary
    OpenAI-compatible provider (no DeepSeek-style thinking param)."""
    if engine == "gpt55":
        return {"name": "gpt55", "base_url": config.SUB2API_BASE_URL,
                "key": config.SUB2API_API_KEY, "model": config.SUB2API_MODEL,
                "thinking_off": False}
    return {"name": "deepseek", "base_url": config.DEEPSEEK_BASE_URL,
            "key": config.DEEPSEEK_API_KEY, "model": config.DEEPSEEK_MODEL,
            "thinking_off": True}


async def _chat(messages: list[dict[str, str]], max_tokens: int,
                json_mode: bool = False, engine: str = "deepseek") -> str:
    prov = _provider(engine)
    if not prov["key"] and engine != "deepseek":
        prov = _provider("deepseek")  # secondary engine unconfigured → fall back
    if not prov["key"]:
        raise TranslateError(f"{prov['name']} API key not configured")
    _check_budget()
    payload: dict[str, Any] = {
        "model": prov["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if prov["thinking_off"]:  # DeepSeek burns ~90% of tokens "thinking" otherwise
        payload["thinking"] = {"type": "disabled"}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{prov['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {prov['key']}",
                     "User-Agent": config.USER_AGENT},
            json=payload,
        )
    if resp.status_code != 200:
        raise TranslateError(f"{prov['name']} http {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    usage = data.get("usage", {}).get("total_tokens", 0)
    if usage:
        _record_usage(usage)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise TranslateError(f"unexpected deepseek response: {exc}") from exc
    if not content:
        raise TranslateError("empty deepseek response")
    return content


def _parse_json_list(content: str, expected: int) -> list[str]:
    parsed = json.loads(content)
    items = parsed.get("t") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise TranslateError("translation response is not a list")
    items = [str(item) for item in items]
    if len(items) < expected:
        items += [""] * (expected - len(items))
    return items[:expected]


async def _translate_batch(segments: list[str], engine: str = "deepseek") -> list[str]:
    numbered = json.dumps({"segments": segments}, ensure_ascii=False)
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是顶级财经/科技新闻译者。把 segments 数组中的每个英文段落翻译成"
                "简体中文，信达雅、术语准确（专有名词保留英文原文并在必要时加中文）。"
                '只输出 JSON：{"t": ["译文1", "译文2", ...]}，数组长度与输入一致，'
                "顺序一一对应，不要添加任何其他内容。若某段已是中文则原样返回。"
            )},
            {"role": "user", "content": numbered},
        ],
        max_tokens=6000,
        json_mode=True,
        engine=engine,
    )
    try:
        return _parse_json_list(content, len(segments))
    except (json.JSONDecodeError, TranslateError):
        log.warning("batch json parse failed, retrying once")
        content = await _chat(
            [
                {"role": "system", "content": (
                    '严格输出 JSON {"t": [...]}：把 segments 里每段英文翻成简体中文，'
                    "数组长度与输入完全一致。"
                )},
                {"role": "user", "content": numbered},
            ],
            max_tokens=6000,
            json_mode=True,
        )
        return _parse_json_list(content, len(segments))


async def translate_segments(segments: list[str], engine: str = "deepseek") -> list[str]:
    """Translate paragraphs preserving order; batches sized by char budget."""
    batches: list[list[str]] = []
    batch: list[str] = []
    chars = 0
    for segment in segments:
        # hard-cap pathological paragraphs so one batch stays bounded
        seg = segment[:BATCH_CHAR_LIMIT]
        if batch and chars + len(seg) > BATCH_CHAR_LIMIT:
            batches.append(batch)
            batch, chars = [], 0
        batch.append(seg)
        chars += len(seg)
    if batch:
        batches.append(batch)

    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def run(one: list[str]) -> list[str]:
        async with semaphore:
            return await _translate_batch(one, engine)

    results = await asyncio.gather(*(run(b) for b in batches))
    return [zh for chunk in results for zh in chunk]


async def translate_titles(titles: list[str], engine: str = "deepseek") -> list[str]:
    return await translate_segments(titles, engine)


async def summarize(title: str, text: str, engine: str = "deepseek") -> dict[str, Any]:
    """Structured TL;DR: one-line takeaway + 3-4 key bullets (Chinese)."""
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是私人新闻主编。给文章写结构化中文摘要，只输出 JSON："
                '{"tldr": "一句话核心结论（≤40字）", "points": ["要点1", "要点2", "要点3"]}。'
                "要点 3-4 条，每条 ≤50 字，讲关键事实/数据/影响，信息密度高，不要套话。"
            )},
            {"role": "user", "content": f"标题：{title}\n\n正文：{text[:6000]}"},
        ],
        max_tokens=700,
        json_mode=True,
        engine=engine,
    )
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or "tldr" not in parsed:
        raise TranslateError("unexpected summary shape")
    return {"tldr": str(parsed.get("tldr", "")),
            "points": [str(p) for p in parsed.get("points", [])][:5]}


async def translate_phrase(text: str, engine: str = "deepseek") -> dict[str, Any]:
    """Translate a selected word / phrase / sentence. For short selections also
    return a one-line gloss (part of speech / nuance); for long ones just the
    translation. Returns {zh, note}."""
    short = len(text) <= 40
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是即时翻译助手。把用户划选的英文（或其他外文）翻译成简体中文。"
                "只输出 JSON：" + (
                    '{"zh": "翻译", "note": "≤20字补充：词性/搭配/言外之意，没有就留空"}'
                    if short else '{"zh": "翻译", "note": ""}'
                ) + "。准确自然，专有名词保留英文。"
            )},
            {"role": "user", "content": text[:2000]},
        ],
        max_tokens=600,
        json_mode=True,
        engine=engine,
    )
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or "zh" not in parsed:
        raise TranslateError("unexpected phrase translation shape")
    return {"zh": str(parsed.get("zh", "")), "note": str(parsed.get("note", ""))}


async def assign_tags(items: list[dict[str, Any]], engine: str = "deepseek") -> dict[int, list[str]]:
    """Classify articles into config.TAXONOMY tags. items: [{id, title, summary}].
    Returns {article_id: [tags]} — only tags from the fixed taxonomy."""
    if not items:
        return {}
    allowed = set(config.TAXONOMY)
    # guillemets fence off untrusted titles from prompt-injection attempts
    lines = [f"[{it['id']}] «{(it['title'] or '')[:90]}» — «{(it['summary'] or '')[:90]}»"
             for it in items]
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是新闻分类器。从这个固定标签库里给每篇文章选 1-3 个最贴切的标签："
                f"{'、'.join(config.TAXONOMY)}。只能用库里的标签，不要造新词。"
                '只输出 JSON：{"文章id(数字)": ["标签1","标签2"], ...}。'
                "每篇至少 1 个、最多 3 个。宁缺毋滥，只打真正相关的。"
            )},
            {"role": "user", "content": "\n".join(lines)},
        ],
        max_tokens=3000,
        json_mode=True,
        engine=engine,
    )
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise TranslateError("tags response is not an object")
    result: dict[int, list[str]] = {}
    for key, tags in parsed.items():
        try:
            aid = int(key)
        except (ValueError, TypeError):
            continue
        if isinstance(tags, list):
            clean = [t for t in tags if t in allowed][:3]
            if clean:
                result[aid] = clean
    return result


async def make_digest(sections: list[dict[str, Any]], engine: str = "deepseek") -> dict[str, Any]:
    """Daily digest from the last 24h of articles.
    sections: [{cat, label, articles: [{id, title, title_zh, summary}]}]"""
    # Titles/summaries are untrusted feed content — wrap in guillemets so an
    # injected "ignore previous instructions" can't pose as a real instruction.
    corpus_lines: list[str] = []
    for section in sections:
        corpus_lines.append(f"## {section['label']} ({section['cat']})")
        for art in section["articles"]:
            t = (art["title_zh"] or art["title"]).replace("\n", " ")[:120]
            s = (art["summary"] or "").replace("\n", " ")[:100]
            corpus_lines.append(f"- [{art['id']}] «{t}» — {s}")
    corpus = "\n".join(corpus_lines)[:24000]
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是私人情报主编，把过去 24 小时的新闻池提炼成中文每日简报。只输出 JSON："
                '{"headline": "今日一句话大势判断（≤50字）",'
                ' "top": [{"t": "要闻标题", "s": "两句话讲清事实+影响", "ids": [文章id]}],'
                ' "sections": [{"cat": "分类key", "items": [{"t": "标题", "s": "一句话", "ids": [id]}]}]}'
                "。top 选 4-6 条全局最重要的（跨分类），sections 按输入分类各 3-4 条"
                "（不与 top 重复），ids 填来源文章的数字 id（可多个）。"
                "判断力优先：合并同题材报道，宁缺毋滥，不要营销稿。"
            )},
            {"role": "user", "content": corpus},
        ],
        max_tokens=3200,
        json_mode=True,
        engine=engine,
    )
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or "top" not in parsed:
        raise TranslateError("unexpected digest shape")
    return parsed
