"""Meridian configuration: environment, constants, default feeds."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("MERIDIAN_DB", BASE_DIR / "data" / "meridian.db"))
STATIC_DIR = BASE_DIR / "static"

PIN = os.environ.get("MERIDIAN_PIN", "")
SECRET = os.environ.get("MERIDIAN_SECRET", "")
PORT = int(os.environ.get("MERIDIAN_PORT", "3023"))

# Optional Jina Reader key — without it the free tier (~20 RPM) is used, which
# is enough for one reader but gets rate-limited on bursts.
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# Daily DeepSeek token budget (sum of total_tokens). Hard gate against runaway cost.
DAILY_TOKEN_BUDGET = int(os.environ.get("MERIDIAN_DAILY_TOKEN_BUDGET", "3000000"))

# Secondary AI provider — any OpenAI-compatible chat endpoint. Used for the
# digest / summary while DeepSeek handles translation. Point these at your
# gateway via the env; the default base is a local gateway and the key is unset,
# so an unconfigured deployment cleanly falls back to DeepSeek.
SUB2API_BASE_URL = os.environ.get("SUB2API_BASE_URL", "http://localhost:8080/v1")
SUB2API_API_KEY = os.environ.get("SUB2API_API_KEY", "")
SUB2API_MODEL = os.environ.get("SUB2API_MODEL", "gpt-5.5")

# Per-feature AI engine. Persisted in the meta table via the settings UI; these
# are the defaults. Engine ids: "deepseek" | "gpt55".
AI_FEATURES = ("digest", "summary", "translate")
AI_ENGINES_DEFAULT = {
    "digest": os.environ.get("MERIDIAN_ENGINE_DIGEST", "gpt55"),
    "summary": os.environ.get("MERIDIAN_ENGINE_SUMMARY", "gpt55"),
    "translate": os.environ.get("MERIDIAN_ENGINE_TRANSLATE", "deepseek"),
}
AI_ENGINE_LABELS = {"deepseek": "DeepSeek", "gpt55": "GPT-5.5"}

FETCH_INTERVAL_MIN = int(os.environ.get("MERIDIAN_FETCH_INTERVAL_MIN", "30"))
FETCH_CONCURRENCY = 8
FETCH_TIMEOUT = 25.0
KEEP_DAYS = 90               # prune articles older than this (starred kept)
MAX_PER_FEED = 400           # cap stored articles per feed (starred kept)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MeridianReader/1.0"
)

SESSION_COOKIE = "meridian_session"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days

CATEGORIES = ["markets", "world", "tech", "ai", "x", "cn"]
CATEGORY_LABELS = {
    "markets": "Markets · 财经",
    "world": "World · 时政",
    "tech": "Tech · 科技",
    "ai": "AI · 前沿",
    "x": "X · AI圈",
    "cn": "中文 · CN",
}

# Daily digest is generated after this local hour (Asia/Shanghai on the VPS).
DIGEST_HOUR = 8

# Auto-tagging: a fixed, high-signal taxonomy. DeepSeek assigns 1-3 of these per
# article so the boss can slice the firehose by interest, not just by source.
TAXONOMY = [
    "大模型", "AI应用", "芯片半导体", "机器人", "融资并购", "财报业绩",
    "宏观经济", "地缘政治", "政策监管", "加密货币", "创业公司", "大厂动态",
    "研究论文", "观点评论", "深度长文", "产品发布", "突发事件",
]
TAXONOMY_SET = frozenset(TAXONOMY)   # read-path whitelist (defense in depth)
TAG_BATCH_PER_CYCLE = 240   # cap articles tagged per refresh cycle (cost guard)

# Market ticker shown beside the daily briefing (Yahoo Finance symbols).
MARKET_SYMBOLS = [
    ("纳斯达克", "^IXIC"), ("标普500", "^GSPC"), ("道琼斯", "^DJI"),
    ("上证指数", "000001.SS"), ("恒生指数", "^HSI"),
    ("黄金", "GC=F"), ("WTI原油", "CL=F"), ("布伦特", "BZ=F"),
    ("比特币", "BTC-USD"),
]
MARKET_CACHE_TTL = 600      # seconds (10 min — light load on Yahoo)

# Sites where full-text extraction reliably fails (hard paywall or bot wall
# that beats even the Jina rendering proxy). We skip extraction entirely and
# the UI shows a friendly "summary only, open original" note instead of an
# alarming failure message. RSS summaries from these are still substantial.
PAYWALL_DOMAINS = (
    "wsj.com", "ft.com", "economist.com", "bloomberg.com", "barrons.com",
    "theinformation.com", "caixin.com", "nytimes.com",
    "marketwatch.com", "dowjones.io",  # hard bot wall (401 + captcha, Jina 451)
)

# Sources whose RSS feed already carries the COMPLETE article body — skip
# full-text extraction entirely. Two reasons land a domain here:
#   • 华尔街见闻 快讯: extracting the web page only scrapes in nav/markets/
#     footer/友情链接 noise (the RSS 快讯 IS the whole story).
#   • x.com / twitter.com: the RSS (via RSSHub) already holds the full tweet
#     text AND its pbs.twimg images; fetching x.com hits a login wall that
#     extracts nothing and wipes the tweet's own images.
# france24.com is also here for a different reason: its page extraction returns
# a Didomi cookie-consent wall (server-rendered, so the Jina proxy hits it too).
# Skipping extraction falls back to the RSS body, which carries a real summary.
NO_EXTRACT_DOMAINS = ("wallstreetcn.com", "awtmt.com", "x.com", "twitter.com",
                      "france24.com")

# Path fragments (any host) whose pages are video players or live-blogs:
# extraction only scrapes the surrounding nav + related-video rail (BBC
# /news/videos/, Al Jazeera /video/, NYT /live/), so skip it and keep the RSS
# summary instead of a wall of unrelated chrome.
NO_EXTRACT_PATHS = ("/news/videos/", "/av/", "/video/", "/live/")

# (url, title, category) — curated launch set, every URL verified reachable.
# v1.1 (6-12): dropped hard-paywall feeds (WSJ/FT/Economist), added open
# full-text sources + cn category.
DEFAULT_FEEDS: list[tuple[str, str, str]] = [
    # Markets 财经
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
     "CNBC Top News", "markets"),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
     "CNBC Finance", "markets"),
    ("https://feeds.content.dowjones.io/public/rss/mw_topstories", "MarketWatch", "markets"),
    ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance", "markets"),
    ("https://feeds.businessinsider.com/custom/all", "Business Insider", "markets"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk", "markets"),
    # World 时政
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World", "world"),
    ("https://www.theguardian.com/world/rss", "The Guardian World", "world"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "NYT World", "world"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera", "world"),
    ("https://feeds.npr.org/1001/rss.xml", "NPR News", "world"),
    ("https://rss.politico.com/politics-news.xml", "Politico", "world"),
    ("https://rss.dw.com/rdf/rss-en-world", "DW World", "world"),
    ("https://www.france24.com/en/rss", "France 24", "world"),
    # Tech 科技
    ("https://www.theverge.com/rss/index.xml", "The Verge", "tech"),
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "tech"),
    ("https://techcrunch.com/feed/", "TechCrunch", "tech"),
    ("https://hnrss.org/best", "Hacker News Best", "tech"),
    ("https://www.technologyreview.com/feed/", "MIT Tech Review", "tech"),
    ("https://www.engadget.com/rss.xml", "Engadget", "tech"),
    ("https://www.theregister.com/headlines.atom", "The Register", "tech"),
    # AI 前沿
    ("https://openai.com/news/rss.xml", "OpenAI News", "ai"),
    ("https://blog.google/technology/ai/rss/", "Google AI Blog", "ai"),
    ("https://deepmind.google/blog/rss.xml", "DeepMind Blog", "ai"),
    ("https://huggingface.co/blog/feed.xml", "Hugging Face", "ai"),
    ("https://venturebeat.com/category/ai/feed/", "VentureBeat AI", "ai"),
    ("https://simonwillison.net/atom/everything/", "Simon Willison", "ai"),
    ("https://the-decoder.com/feed/", "The Decoder", "ai"),
    ("https://thegradient.pub/rss/", "The Gradient", "ai"),
    # X · AI 圈（api.xgo.ing Twitter→RSS · 源自 SuYxh/ai-news-aggregator 精选）
    ("https://api.xgo.ing/rss/user/97f1484ae48c430fbbf3438099743674", "宝玉 @dotey", "x"),
    ("https://api.xgo.ing/rss/user/831fac36aa0a49a9af79f35dc1c9b5d9", "歸藏 @op7418", "x"),
    ("https://api.xgo.ing/rss/user/74e542992cf7441390c708f5601071d4", "小互 @imxiaohu", "x"),
    ("https://api.xgo.ing/rss/user/ca2fa444b6ea4b8b974fe148056e497a", "李继刚 @lijigang_com", "x"),
    ("https://api.xgo.ing/rss/user/9de19c78f7454ad08c956c1a00d237fe", "向阳乔木 @vista8", "x"),
    ("https://api.xgo.ing/rss/user/3d72acd51d21414ea39871fc01982a65", "idoubi @idoubicc", "x"),
    ("https://api.xgo.ing/rss/user/665fc88440fd4436acbc2e630d824926", "Tw93 @HiTw93", "x"),
    ("https://api.xgo.ing/rss/user/5b632b7fba274f62928cdcc9d3db4c5e", "AI产品黄叔", "x"),
    ("https://api.xgo.ing/rss/user/0c0856a69f9f49cf961018c32a0b0049", "OpenAI @OpenAI", "x"),
    ("https://api.xgo.ing/rss/user/fc28a211471b496682feff329ec616e5", "Anthropic", "x"),
    ("https://api.xgo.ing/rss/user/01f60d63a61b44d692cc35c7feb0b4a4", "Claude @claudeai", "x"),
    ("https://api.xgo.ing/rss/user/68b610deb24b47ae9a236811563cda86", "DeepSeek", "x"),
    ("https://api.xgo.ing/rss/user/80032d016d654eb4afe741ff34b7643d", "Qwen", "x"),
    ("https://api.xgo.ing/rss/user/3953aa71e87a422eb9d7bf6ff1c7c43e", "xAI @xai", "x"),
    ("https://api.xgo.ing/rss/user/c6cfe7c0d6b74849997073233fdea840", "Jim Fan @DrJimFan", "x"),
    ("https://api.xgo.ing/rss/user/a8f7e2238039461cbc8bf55f5f194498", "Lilian Weng", "x"),
    ("https://api.xgo.ing/rss/user/08b5488b20bc437c8bfc317a52e5c26d", "Andrew Ng", "x"),
    ("https://api.xgo.ing/rss/user/a4bfe44bfc0d4c949da21ebd3f5f42a5", "Fei-Fei Li", "x"),
    # 中文 CN（中文内容自动跳过翻译）
    ("https://36kr.com/feed", "36氪", "cn"),
    ("https://sspai.com/feed", "少数派", "cn"),
    ("https://www.ifanr.com/feed", "爱范儿", "cn"),
    ("https://baoyu.io/feed.xml", "宝玉的分享", "cn"),
    ("https://www.qbitai.com/feed", "量子位", "cn"),
    ("http://feeds.feedburner.com/ruanyifeng", "阮一峰的网络日志", "cn"),
    ("https://tech.meituan.com/feed/", "美团技术团队", "cn"),
    # 华尔街见闻快讯走自家 RSSHub（带 key），由迁移脚本/手动添加，不进默认清单
]

# v1.2 newsletter additions live in the ai category:
DEFAULT_FEEDS += [
    ("https://www.bensbites.com/feed", "Ben's Bites", "ai"),
    ("https://tldr.tech/api/rss/ai", "TLDR AI", "ai"),
    ("https://www.interconnects.ai/feed", "Interconnects", "ai"),
    ("https://www.oneusefulthing.org/feed", "One Useful Thing", "ai"),
]


def ensure_secret() -> str:
    """Session-signing secret; generated once and persisted next to the DB."""
    global SECRET
    if SECRET:
        return SECRET
    secret_file = DB_PATH.parent / ".secret"
    if secret_file.exists():
        SECRET = secret_file.read_text().strip()
    else:
        SECRET = secrets.token_hex(32)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(SECRET)
        secret_file.chmod(0o600)
    return SECRET
