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


def _balance_by_source(members: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Round-robin across feeds before truncating, so one prolific outlet (e.g.
    Yahoo/Teortaxes) can't fill the whole LLM sample and bias the synthesis —
    the 'neutral across outlets' promise needs a balanced sample, not the first N."""
    by_src: dict[Any, list] = {}   # dict is insertion-ordered (3.7+)
    for m in members:
        by_src.setdefault(m.get("feed_title") or "", []).append(m)
    out: list[dict[str, Any]] = []
    queues = list(by_src.values())
    while len(out) < cap and any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
                if len(out) >= cap:
                    break
    return out


async def summarize_cluster(top_title: str, members: list[dict[str, Any]],
                            engine: str = "gpt55") -> dict[str, Any]:
    """Event synthesis across a cluster's multi-source reports — overview /
    progress / takeaway, in Chinese, neutral across outlets."""
    if not members:
        raise TranslateError("cluster has no members")
    lines = [f"[{m['feed_title']}] {m['title']} — {(m.get('summary') or '')[:120]}"
             for m in _balance_by_source(members, 30)]
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是新闻主编。下面是同一事件被多家媒体报道的标题与摘要。综合写一份"
                "中文事件简报,只输出 JSON："
                '{"overview":"事件概述(2-3句,讲清是什么事、关键事实)",'
                '"progress":["最新进展要点1","要点2","要点3"],'
                '"takeaway":"一句话总结这件事的意义或影响"}。'
                "客观中立,综合各家信息,不偏向任何一家媒体立场,不编造未提及的事实。"
            )},
            {"role": "user", "content": f"事件：{top_title}\n\n各家报道：\n" + "\n".join(lines)},
        ],
        max_tokens=900, json_mode=True, engine=engine)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise TranslateError("unexpected cluster summary shape")
    return {"overview": str(parsed.get("overview", "")),
            "progress": [str(p) for p in parsed.get("progress", [])][:5],
            "takeaway": str(parsed.get("takeaway", ""))}


async def analyze_cluster(top_title: str, members: list[dict[str, Any]],
                          engine: str = "gpt55") -> dict[str, Any]:
    """Deep multi-source event analysis (much richer than summarize_cluster):
    panorama, timeline, perspectives, impact, controversy, outlook, key facts."""
    if not members:
        raise TranslateError("cluster has no members")
    lines = [f"[{m['feed_title']}] {m['title']} — {(m.get('summary') or '')[:160]}"
             for m in _balance_by_source(members, 40)]
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是资深新闻分析师。下面是同一事件被多家媒体报道的标题与摘要。"
                "写一份深度事件分析,帮读者快速了解来龙去脉与后续走势。只输出 JSON："
                '{"panorama":"事件全景(180-220字,讲清是什么事、起因、经过、现状)",'
                '"timeline":[{"t":"时间(如6月12日)","e":"该节点发生了什么"}],'
                '"perspectives":[{"who":"某一方或某媒体","view":"该方观点或立场(一句话)"}],'
                '"impact":[{"area":"影响维度(经济/政治/科技/社会/市场 之一)","detail":"具体影响"}],'
                '"controversy":"核心争议点或各方主要分歧(一段)",'
                '"outlook":[{"path":"一种可能走向","odds":"高|中|低","detail":"分析依据"}],'
                '"facts":[{"k":"指标名(≤6字,如 战争持续/受损基地)","v":"量化数值(≤8字醒目,如 105天/50+处/2个月)"}]}。'
                "timeline 按时间先后 3-6 个关键节点;perspectives 3-5 方;impact 3-4 维;"
                "outlook 2-3 种;facts 2-4 个**可量化的关键数字**(v 要简短醒目,只放能量化的,"
                "不放长描述句)。客观中立,综合各家,基于报道不编造。"
            )},
            {"role": "user", "content": f"事件：{top_title}\n\n各家报道：\n" + "\n".join(lines)},
        ],
        max_tokens=2600, json_mode=True, engine=engine)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise TranslateError("unexpected analysis shape")

    def _objs(key: str, fields: tuple) -> list[dict[str, str]]:
        return [{f: str(x.get(f, "")) for f in fields}
                for x in parsed.get(key, []) if isinstance(x, dict)]
    return {
        "panorama": str(parsed.get("panorama", "")),
        "timeline": _objs("timeline", ("t", "e"))[:8],
        "perspectives": _objs("perspectives", ("who", "view"))[:6],
        "impact": _objs("impact", ("area", "detail"))[:5],
        "controversy": str(parsed.get("controversy", "")),
        "outlook": _objs("outlook", ("path", "odds", "detail"))[:4],
        "facts": _objs("facts", ("k", "v"))[:6],
    }


async def score_clusters_ai(events: list[dict[str, Any]],
                            engine: str = "gpt55") -> list[dict[str, Any]]:
    """Batch-score event clusters: a concise Chinese title + a heat score
    (0-100, blending source breadth, report volume, importance). Chunked so a
    long event list never overflows the output token budget. Returns
    [{i, title_zh, heat}, ...] indexed by the input order."""
    if not events:
        return []
    sys_prompt = (
        "你是新闻主编。下面是今日各个新闻事件(每个已由多家媒体报道聚合而成)。"
        "为每个事件做三件事：① 起一个简洁中文标题(≤20字,概括事件核心,客观中立、"
        "不带媒体立场) ② 打一个热度分(0-100 整数,综合报道媒体数量、报道篇数、"
        "事件重要性与影响范围) ③ 按 7 个维度各打 0-10 整数分(衡量客观重要性,"
        "与热度无关)：scale 影响范围/波及人数、impact 后果严重程度、novelty 新颖"
        "程度、potential 潜在连锁影响、legacy 长期历史意义、positivity 正面程度"
        "(越正面越高)、credibility 信源可信度。"
        '只输出 JSON：{"events":[{"i":序号整数,"title_zh":"中文标题","heat":热度分整数,'
        '"f":{"scale":整数,"impact":整数,"novelty":整数,"potential":整数,'
        '"legacy":整数,"positivity":整数,"credibility":整数}}]}。不编造未提及的信息。'
    )
    _FK = ("scale", "impact", "novelty", "potential", "legacy", "positivity", "credibility")
    out: list[dict[str, Any]] = []
    batch = 25   # 7 factors per event ~doubles output tokens — smaller batch avoids truncation
    for start in range(0, len(events), batch):
        chunk = events[start:start + batch]
        lines = [f"{j}. [{e['source_count']}源/{e['member_count']}篇] {e['top_title']}"
                 for j, e in enumerate(chunk)]
        content = await _chat(   # budget/network errors propagate to the caller
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": "事件列表：\n" + "\n".join(lines)}],
            max_tokens=3600, json_mode=True, engine=engine)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue   # truncated/malformed: skip only this chunk, keep the rest
        if not isinstance(parsed, dict):
            continue
        for it in (parsed.get("events") or []):
            if not isinstance(it, dict):
                continue
            try:
                local_i = int(it.get("i"))
                heat = max(0, min(100, int(it.get("heat", 0))))
            except (TypeError, ValueError):
                continue
            if not 0 <= local_i < len(chunk):   # reject hallucinated index
                continue
            raw_f = it.get("f") if isinstance(it.get("f"), dict) else {}
            factors = {}
            if raw_f:   # leave empty when the model omitted factors → neutral significance, not all-zero
                for k in _FK:
                    try:
                        factors[k] = max(0, min(10, int(raw_f.get(k, 0))))
                    except (TypeError, ValueError):
                        factors[k] = 0
            out.append({"i": local_i + start,   # chunk-local -> global
                        "title_zh": str(it.get("title_zh", ""))[:60], "heat": heat,
                        "factors": factors})
    return out


async def ask_article(title: str, content: str, question: str,
                      engine: str = "gpt55") -> str:
    """Answer a user's question grounded in one article's text (interactive深读)."""
    out = await _chat(
        [
            {"role": "system", "content": (
                "你是阅读助手。严格依据用户提供的【文章正文】回答问题，简体中文，准确"
                "简洁、有条理。正文未提及的内容明说“文章未提及”，绝不编造或外推。"
            )},
            {"role": "user", "content":
                f"【标题】{title}\n\n【正文】\n{content[:9000]}\n\n【问题】{question}"},
        ],
        max_tokens=900, engine=engine)
    return out.strip()


async def rewrite_title(title: str, text: str, engine: str = "gpt55") -> dict[str, Any]:
    """Judge whether a title is clickbait and rewrite it into a neutral, accurate
    Chinese headline grounded in the body. Returns {clickbait, rewritten}."""
    content = await _chat(
        [
            {"role": "system", "content": (
                "你是新闻编辑，专治标题党。判断【原标题】是否夸张/悬念/情绪化/标题党；"
                "依据【正文】把它重写成一个中性、准确、信息完整的简体中文标题（≤30字，"
                "陈述事实、不卖关子，不用“震惊/突发/速看/惊呆”等情绪词，专有名词保留）。"
                "涉及台湾一律写“中国台湾”。不编造正文未提及的事实。"
                '只输出 JSON：{"clickbait": true或false, "rewritten": "重写后的中性中文标题"}。'
            )},
            {"role": "user", "content": f"【原标题】{title}\n\n【正文】\n{text[:6000]}"},
        ],
        max_tokens=300, json_mode=True, engine=engine)
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or "rewritten" not in parsed:
        raise TranslateError("unexpected rewrite shape")
    return {"clickbait": bool(parsed.get("clickbait")),
            "rewritten": str(parsed.get("rewritten", ""))[:120]}


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
