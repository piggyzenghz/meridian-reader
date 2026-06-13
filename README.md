# Meridian · 子午线

> A self-hosted **bilingual RSS reader** for people who read a lot of finance,
> world, tech & AI news — inline DeepSeek translation, an editorial reading
> experience, and zero build step.

Meridian opens an article's RSS summary instantly, pulls the full text in the
background, and lets you flip any foreign-language piece into a paragraph-level
side-by-side translation. Dark-first, keyboard-driven, native HTML/CSS/JS — no
framework, no bundler.

## Features

- **Bilingual reading** — paragraph-level original↔Chinese, powered by DeepSeek.
  Translations are cached per article, so you never pay for the same text twice.
- **Background full-text extraction** — the RSS summary shows immediately, then
  the full article is fetched in the background (trafilatura → [Jina Reader]
  fallback for bot-walled pages) and the reader hydrates when it's ready.
- **Daily digest** — an auto-generated morning briefing with a live market
  ticker (indices, gold, oil, crypto).
- **Auto-tagging** — a fixed, high-signal taxonomy (大模型 / 芯片 / 融资 / 宏观 …)
  assigned by the model, so you can slice the firehose by topic, not just source.
- **Keyword monitors** — follow a topic across every source as its own feed.
- **Reading tools** — TL;DR summaries, highlight-and-translate selection,
  read-later queue, reading-progress resume, related articles, TTS read-aloud.
- **Editorial UI** — dual theme with a View-Transition reveal, self-hosted
  Fraunces + Inter variable fonts, `Cmd+K` command palette, `j`/`k` flow,
  infinite scroll.

## Stack

- **Backend** — FastAPI + SQLite (WAL), single uvicorn worker.
- **Frontend** — vanilla HTML/CSS/JS, served static. No build step.
- **Translation / summaries** — DeepSeek (`deepseek-v4-flash`) behind a daily
  token-budget gate.
- **Extraction** — trafilatura, falling back to `r.jina.ai` for JS-heavy or
  bot-walled pages.

`app/` modules: `config` (feeds + constants), `fetcher` (30-min poll, ETag,
noise scrubbing), `translate`, `extract`, `digest`, `discover` (add any URL →
auto-find its feed, with an SSRF guard), `tagger`, `market`, `sanitize`,
`auth`, `db`, `main`.

## Quick start (local)

```bash
git clone https://github.com/piggyzenghz/meridian-reader.git
cd meridian-reader
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env          # set MERIDIAN_PIN + DEEPSEEK_API_KEY
venv/bin/uvicorn app.main:app --port 3023
```

Open <http://127.0.0.1:3023> and enter your PIN.

## Deploy (systemd + reverse proxy)

```bash
# on the server, as a non-root user
git clone https://github.com/piggyzenghz/meridian-reader.git ~/meridian
cd ~/meridian
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env       # fill in your secrets

sudo cp deploy/meridian.service /etc/systemd/system/
# edit User= and the paths in the unit to match your box
sudo systemctl daemon-reload
sudo systemctl enable --now meridian
```

The app binds to `127.0.0.1:3023` — front it with nginx / Caddy / a Cloudflare
Tunnel for TLS and public access.

```bash
sudo systemctl restart meridian   # restart
journalctl -u meridian -f         # tail logs
```

## Configuration

Everything is set via the environment (`.env`):

| Variable | Default | Notes |
|---|---|---|
| `MERIDIAN_PIN` | — | gate password (**required**) |
| `DEEPSEEK_API_KEY` | — | **required** for translation & summaries |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | any DeepSeek-compatible model |
| `MERIDIAN_PORT` | `3023` | bind port |
| `MERIDIAN_FETCH_INTERVAL_MIN` | `30` | feed poll interval |
| `MERIDIAN_DAILY_TOKEN_BUDGET` | `3000000` | hard cap on DeepSeek tokens/day |
| `JINA_API_KEY` | — | optional; raises the Jina Reader rate limit |

Feeds are managed in-app (⚙ settings: add / remove / enable / categorize) or
seeded from `app/config.py`. All reading data — stars, highlights, read state,
cached translations — lives in `data/meridian.db` (gitignored). Back that file
up and the service is otherwise stateless.

## Keyboard

`j`/`k` next·prev · `o`/`Enter` open · `b` bilingual · `s` star ·
`l` read-later · `t` translate title · `r` refresh · `d` theme ·
`Cmd+K` command palette.

## Credits

- Default X/Twitter source picks adapted from
  [SuYxh/ai-news-aggregator](https://github.com/SuYxh/ai-news-aggregator).
- Fonts: [Fraunces](https://github.com/undercasetype/Fraunces) ·
  [Inter](https://github.com/rsms/inter).
- Full-text fallback: [Jina Reader](https://jina.ai/reader/).

## License

[MIT](LICENSE)

[Jina Reader]: https://jina.ai/reader/
