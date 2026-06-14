/* Meridian client — vanilla ES module, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const S = {
  state: null,            // /api/state payload
  articles: [],
  nextBefore: 0,
  nextBeforeId: 0,        // compound pagination cursor (published, id)
  loadingMore: false,
  view: { mode: "list", category: "", feedId: 0, filter: "all", tag: "", monitor: "", q: "" },
  current: null,          // open article detail
  currentBlocks: null,
  selectedIdx: -1,
  titleTrans: localStorage.getItem("m.titles") === "1",
  theme: localStorage.getItem("m.theme") || "dark",
  pollTimer: 0,
};

let progressTimer = 0, pendingProgress = -1;  // reader scroll-progress reporting

const CAT_LABEL = {
  markets: ["Markets", "财经"], world: ["World", "时政"],
  tech: ["Tech", "科技"], ai: ["AI", "前沿"],
  x: ["X", "AI圈"], cn: ["中文", "CN"],
};
const CAT_VAR = {
  markets: "var(--c-markets)", world: "var(--c-world)",
  tech: "var(--c-tech)", ai: "var(--c-ai)",
  x: "var(--c-x)", cn: "var(--c-cn)",
};

/* deterministic colored letter badge per source */
function letterBadge(title) {
  let hash = 0;
  for (const ch of String(title)) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  const hue = hash % 360;
  const ch = [...String(title).replace(/^(the|a)\s+/i, "")][0]?.toUpperCase() || "?";
  return `<span class="src-badge" style="background:hsl(${hue} 42% 46%)">${esc(ch)}</span>`;
}

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch { return ""; }
}

/* Real favicon for normal sources; x.com / twitter feeds (all one X logo) keep
   the distinguishing letter badge. A favicon load failure falls back to the
   letter badge — see the central image-error listener. */
function srcBadge(title, feedId) {
  const feed = feedId ? (S.state?.feeds || []).find((f) => f.id === feedId) : null;
  const host = feed ? hostOf(feed.site_url) : "";
  const isX = /(^|\.)(x|twitter)\.com$/.test(host);
  if (feed && host && !isX) {
    return `<img class="src-favicon" src="/favicon/${feedId}" alt="" width="17" height="17" data-badge="${esc(title)}" referrerpolicy="no-referrer">`;
  }
  return letterBadge(title);
}

const fmtMins = (chars) => Math.max(1, Math.round(chars / 1100));

function tweetText(a) {
  const text = (a.summary || a.title || "").slice(0, 220);
  if (text === "Tweet" || /^(x\.com|pic\.x\.com|https?:\/\/)/.test(text))
    return "🖼 图片 / 视频推文 — 点开查看";
  return text;
}

/* ── helpers ─────────────────────────────────────── */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { showAuth(); throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  return data;
}

const prefersReduce = matchMedia("(prefers-reduced-motion: reduce)");
const scrollBehavior = () => (prefersReduce.matches ? "auto" : "smooth");

function toast(html) {
  // contract: callers MUST esc() any non-numeric dynamic value
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = html;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

const fmtTime = (ts) => {
  const d = new Date(ts * 1000), now = Date.now(), diff = (now - d) / 60000;
  if (diff < 1) return "刚刚";
  if (diff < 60) return `${Math.floor(diff)} 分钟前`;
  const TZ = "Asia/Shanghai";   // 东八区铁律：时间一律 CST，不跟浏览器/服务器时区漂移
  const clock = d.toLocaleTimeString("zh-CN", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  const dayKey = (x) => x.toLocaleDateString("en-CA", { timeZone: TZ });
  if (dayKey(d) === dayKey(new Date())) return clock;
  return d.toLocaleDateString("zh-CN", { timeZone: TZ, month: "short", day: "numeric" }) + " " + clock;
};

function groupKey(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.floor((startOf(today) - startOf(d)) / 86400000);
  if (days <= 0) return "今天 · Today";
  if (days === 1) return "昨天 · Yesterday";
  const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 · ${week}`;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ── auth ────────────────────────────────────────── */

function showAuth() {
  $("#app").classList.add("hidden");
  $("#auth").classList.remove("hidden");
  setTimeout(() => $("#auth-pin").focus(), 60);
}

async function boot() {
  applyTheme(S.theme, false);
  $("#masthead-date").textContent = new Date().toLocaleDateString("zh-CN",
    { year: "numeric", month: "long", day: "numeric", weekday: "long" });
  try {
    S.state = await api("/api/state");
    enterApp();
  } catch (err) {
    if (err.message !== "unauthorized") { console.error("boot failed:", err); toast(`加载失败：${esc(err.message)}`); }
  }
}

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("#auth-pin").value.trim();
  if (!pin) return;
  try {
    await api("/api/auth", { method: "POST", body: { pin } });
    $("#auth-error").textContent = "";
    S.state = await api("/api/state");
    enterApp();
  } catch (err) {
    $("#auth-error").textContent = err.message.includes("429")
      ? "尝试过于频繁，请稍后再试" : "密码不对，再想想？";
    const card = $(".auth-card");
    card.classList.remove("shake"); void card.offsetWidth; card.classList.add("shake");
    $("#auth-pin").select();
  }
});

function enterApp() {
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  renderNav(); renderTags(); renderMonitors(); renderFeeds(); renderUsage();
  loadArticles(true);
  loadTicker();
}

/* ── tags (auto-assigned topical filters) ─────────── */

function renderTags() {
  const tags = S.state.tags || [];
  $("#tags-head").classList.toggle("hidden", !tags.length);
  $("#nav-tags").innerHTML = tags.map((t) => `
    <button class="tag-chip ${S.view.tag === t.tag && S.view.mode === "list" ? "active" : ""}"
      data-tag="${esc(t.tag)}">${esc(t.tag)} <b>${t.count}</b></button>`).join("");
}

function jumpTag(tag) {
  if (S.view.tag === tag && S.view.mode === "list") {  // toggle off
    Object.assign(S.view, { tag: "", q: "" });
  } else {
    Object.assign(S.view, { mode: "list", tag, monitor: "", q: "" });
    if (["starred", "later"].includes(S.view.filter)) S.view.filter = "all";
  }
  syncFilterSeg(); switchView(); closeSidebar();
}

$("#nav-tags").addEventListener("click", (e) => {
  const chip = e.target.closest(".tag-chip");
  if (chip) jumpTag(chip.dataset.tag);
});

/* ── keyword monitors (subscribe to a topic across all feeds) ── */

function renderMonitors() {
  const mons = S.state.monitors || [];
  $("#nav-monitors").innerHTML = mons.map((m) => `
    <button class="mon-item ${S.view.monitor === m.query ? "active" : ""}" data-q="${esc(m.query)}">
      <svg><use href="#i-radar"/></svg>
      <span class="mon-name">${esc(m.query)}</span>
      ${m.unread ? `<span class="mon-count">${m.unread > 999 ? "999+" : Number(m.unread)}</span>` : ""}
      <span class="mon-del" data-id="${m.id}" title="删除监控">✕</span>
    </button>`).join("");
}

function jumpMonitor(query) {
  Object.assign(S.view, { mode: "list", category: "", feedId: 0, tag: "", monitor: query, q: "" });
  if (["starred", "later"].includes(S.view.filter)) S.view.filter = "all";
  syncFilterSeg(); switchView(); closeSidebar();
}

$("#nav-monitors").addEventListener("click", async (e) => {
  const del = e.target.closest(".mon-del");
  if (del) {
    e.stopPropagation();
    const gone = (S.state?.monitors || []).find((m) => String(m.id) === del.dataset.id);
    await api(`/api/monitors/${del.dataset.id}`, { method: "DELETE" }).catch(() => {});
    await refreshState(false);
    if (gone && S.view.monitor === gone.query) {  // only leave the view if it's the one deleted
      S.view.monitor = ""; switchView();
    }
    return;
  }
  const item = e.target.closest(".mon-item");
  if (item) jumpMonitor(item.dataset.q);
});

$("#btn-add-monitor").addEventListener("click", async () => {
  const q = prompt("监控一个关键词/话题（任何源里出现都会汇进来）：\n例如：英伟达、OpenAI、降息、某公司名");
  if (!q || q.trim().length < 2) return;
  try {
    await api("/api/monitors", { method: "POST", body: { query: q.trim() } });
    await refreshState(false);
    jumpMonitor(q.trim());
    toast(`已监控 <b>${esc(q.trim())}</b>`);
  } catch (err) { toast(`创建失败：${esc(err.message)}`); }
});

/* ── nav / sidebar ───────────────────────────────── */

function navItem({ key, en, zh, count, color, feedId }) {
  const active = feedId
    ? (S.view.mode === "list" && S.view.feedId === feedId)
    : (S.view.mode === "list" && !S.view.feedId && S.view.category === key &&
       !["starred", "later"].includes(S.view.filter));
  return `<button class="nav-item ${active ? "active" : ""}" ${active ? 'aria-current="true"' : ""}
    data-cat="${key}" ${feedId ? `data-feed="${feedId}"` : ""}
    style="--cat:${color || "var(--accent)"}">
    <span class="nav-dot"></span>
    <span class="nav-label"><span class="en">${esc(en)}</span><span class="zh">${esc(zh)}</span></span>
    ${count ? `<span class="nav-count">${count > 999 ? "999+" : count}</span>` : ""}
  </button>`;
}

function iconNavItem({ mode, filter, icon, en, zh, count, color, fill }) {
  const active = mode
    ? S.view.mode === mode
    : (S.view.mode === "list" && S.view.filter === filter && !S.view.category);
  return `<button class="nav-item ${active ? "active" : ""}" ${active ? 'aria-current="true"' : ""}
    ${mode ? `data-mode="${mode}"` : `data-filter-view="${filter}"`} style="--cat:${color}">
    <span class="nav-dot" style="background:transparent;box-shadow:none">
      <svg style="width:11px;height:11px;stroke:${color};fill:${fill && active ? color : "none"};stroke-width:2"><use href="#${icon}"/></svg>
    </span>
    <span class="nav-label"><span class="en">${en}</span><span class="zh">${zh}</span></span>
    ${count ? `<span class="nav-count">${count}</span>` : ""}</button>`;
}

function renderNav() {
  const u = S.state.unread || {};
  const total = Object.values(u).reduce((a, b) => a + b, 0);
  let html = iconNavItem({ mode: "digest", icon: "i-news", en: "Briefing", zh: "今日简报", color: "var(--accent)" });
  html += iconNavItem({ mode: "clusters", icon: "i-link2", en: "Events", zh: "事件聚合", color: "var(--c-x)" });
  html += navItem({ key: "", en: "Today", zh: "全部", count: total, color: "var(--accent)" });
  for (const cat of S.state.categories)
    html += navItem({ key: cat, en: CAT_LABEL[cat][0], zh: CAT_LABEL[cat][1], count: u[cat] || 0, color: CAT_VAR[cat] });
  html += iconNavItem({ filter: "later", icon: "i-clock", en: "Later", zh: "稍后读", count: S.state.later, color: "var(--c-world)" });
  html += iconNavItem({ filter: "starred", icon: "i-star", en: "Starred", zh: "收藏", count: S.state.starred, color: "var(--accent)", fill: true });
  html += iconNavItem({ mode: "highlights", icon: "i-marker", en: "Highlights", zh: "高亮", count: S.state.highlights, color: "var(--c-tech)" });
  $("#nav-cats").innerHTML = html;
}

function renderFeeds() {
  const feeds = S.state.feeds.filter((f) =>
    f.enabled && (!S.view.category || f.category === S.view.category));
  $("#nav-feeds").innerHTML = feeds.map((f) => `
    <button class="feed-item ${S.view.feedId === f.id ? "active" : ""} ${f.error_count > 3 ? "errored" : ""}"
      data-feed="${f.id}" style="--cat:${CAT_VAR[f.category]}" title="${esc(f.title)}">
      <span class="feed-fav"></span>
      <span class="feed-name">${esc(f.title || f.url)}</span>
      ${f.unread ? `<span class="feed-unread">${f.unread}</span>` : ""}
    </button>`).join("");
}

function renderUsage() {
  const { tokens_today: used = 0, token_budget: budget = 1 } = S.state;
  const pct = Math.min(100, (used / budget) * 100);
  $("#usage-fill").style.width = `${pct}%`;
  $("#usage-label").textContent = used >= 1000
    ? `${(used / 1000).toFixed(used >= 100000 ? 0 : 1)}k tokens` : `${used} tokens`;
}

$("#sidebar").addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-item, .feed-item");
  if (!btn) return;
  if (btn.dataset.mode) {
    Object.assign(S.view, { mode: btn.dataset.mode, tag: "", monitor: "", q: "" });
  } else if (btn.dataset.filterView) {
    Object.assign(S.view, { mode: "list", category: "", feedId: 0, tag: "", monitor: "",
                            filter: btn.dataset.filterView, q: "" });
  } else if (btn.dataset.feed) {
    Object.assign(S.view, { mode: "list", feedId: +btn.dataset.feed, tag: "", monitor: "", q: "" });
    const feed = S.state.feeds.find((f) => f.id === +btn.dataset.feed);
    if (feed) S.view.category = feed.category;
  } else {
    Object.assign(S.view, { mode: "list", category: btn.dataset.cat, feedId: 0, tag: "", monitor: "", q: "" });
    if (["starred", "later"].includes(S.view.filter)) S.view.filter = "all";
  }
  syncFilterSeg();
  switchView();
  closeSidebar();
});

/* ── article list ────────────────────────────────── */

function viewTitle() {
  if (S.view.mode === "digest") return "Briefing";
  if (S.view.mode === "clusters") return "Events";
  if (S.view.mode === "highlights") return "Highlights";
  if (S.view.monitor) return `◎ ${S.view.monitor}`;
  if (S.view.tag) return `# ${S.view.tag}`;
  if (S.view.q) return `“${S.view.q}”`;
  if (S.view.feedId) {
    const feed = S.state.feeds.find((f) => f.id === S.view.feedId);
    if (feed) return feed.title;
  }
  if (S.view.filter === "starred") return "Starred";
  if (S.view.filter === "later") return "Read Later";
  if (S.view.category) return CAT_LABEL[S.view.category]?.[0] || S.view.category;
  return "Today";
}

function switchView() {
  ++loadSeq;   // any in-flight list load belongs to the previous view — supersede it so a late /api/articles can't clobber #list (articles bleeding into Events/Briefing/Highlights)
  renderNav(); renderTags(); renderMonitors(); renderFeeds();
  const isList = S.view.mode === "list";
  $(".seg").style.display = isList ? "" : "none";
  $("#btn-readall").style.display = isList ? "" : "none";
  $("#btn-titles").style.display = isList ? "" : "none";
  $("#ticker").style.display = isList ? "" : "none";
  // note: no View Transition here — rapid category switches abort each other
  // and leave stale snapshots; the per-item fade-up is animation enough
  if (S.view.mode !== "list") { S.articles = []; S.selectedIdx = -1; S.nextBefore = 0; S.nextBeforeId = 0; }
  if (S.view.mode === "digest") renderDigestView();
  else if (S.view.mode === "clusters") renderClustersView();
  else if (S.view.mode === "highlights") renderHighlightsView();
  else {
    // serve the first page from cache instantly (no skeleton flash), then
    // silently revalidate in the background — switching tabs feels instant
    const cached = viewCacheGet();
    if (cached) {
      S.articles = cached.items; S.nextBefore = cached.nextBefore;
      S.nextBeforeId = cached.nextBeforeId || 0; S.selectedIdx = -1;
      renderList();
      loadArticles(true, /*silent=*/true);
    } else {
      renderListSkeleton(); loadArticles(true);
    }
  }
}

// ── per-view list cache (instant tab switches) ──────
const _viewCache = new Map();       // viewKey -> {items, nextBefore, nextBeforeId, ts}
const VIEW_CACHE_TTL = 120000;      // 2 min freshness
function viewKey() {
  const v = S.view;
  return `${v.category}|${v.feedId}|${v.filter}|${v.tag}|${v.monitor}|${v.q}`;
}
function viewCacheGet() {
  const e = _viewCache.get(viewKey());
  return e && Date.now() - e.ts < VIEW_CACHE_TTL ? e : null;
}
function viewCachePut(items, nextBefore, nextBeforeId) {
  _viewCache.set(viewKey(), { items: items.slice(0, 50), nextBefore, nextBeforeId, ts: Date.now() });
  if (_viewCache.size > 30) _viewCache.delete(_viewCache.keys().next().value);
}
function viewCacheClear() { _viewCache.clear(); }

/* ── digest view ─────────────────────────────────── */

function digestItemHtml(item, big = false) {
  const links = (item.ids || []).map((id) => parseInt(id, 10))
    .filter((n) => Number.isInteger(n)).slice(0, 4).map((id) =>
    `<span class="digest-link" data-id="${id}">↗</span>`).join("");
  return `<div class="digest-item ${big ? "digest-top" : ""}">
    <div class="digest-item-t">${esc(item.t)}<span class="digest-links">${links}</span></div>
    <div class="digest-item-s">${esc(item.s)}</div></div>`;
}

async function renderDigestView(forceGen = false) {
  $("#view-title").textContent = "Briefing";
  $("#view-count").textContent = "";
  const wrap = $("#list");
  $("#list-end").classList.add("hidden");
  const dateLabel = new Date().toLocaleDateString("zh-CN",
    { month: "long", day: "numeric", weekday: "long" });
  wrap.innerHTML = `<div class="digest-wrap"><div class="digest-date">每日简报 · ${dateLabel}</div>
    <div class="digest-empty"><div class="sk-line" style="width:60%;height:22px;margin:30px auto"></div>
    <div class="empty-sub">正在${forceGen ? "重新生成" : "加载"}简报…（首次生成约 20 秒）</div></div></div>`;
  let data;
  try {
    data = forceGen
      ? await api("/api/digest?force=1", { method: "POST" })
      : await api("/api/digest").catch(async (err) => {
          if (String(err.message).includes("404") || String(err.message).includes("not generated"))
            return api("/api/digest", { method: "POST" });
          throw err;
        });
  } catch (err) {
    if (S.view.mode !== "digest") return;   // switched away mid-fetch — don't clobber the new view
    wrap.innerHTML = `<div class="digest-wrap"><div class="digest-date">每日简报 · ${dateLabel}</div>
      <div class="digest-empty"><div class="empty-line">简报生成失败</div>
      <div class="empty-sub">${esc(err.message)}</div>
      <button class="pill" style="margin-top:18px" onclick="window.__retryDigest()">重试生成</button></div></div>`;
    return;
  }
  if (S.view.mode !== "digest") return;     // switched away mid-fetch — don't clobber the new view
  let main = `<div class="digest-date">每日简报 · ${dateLabel}</div>
    <h1 class="digest-headline">${esc(data.headline || "")}</h1>`;
  if (data.top?.length) {
    main += `<div class="digest-sec"><div class="digest-sec-title" style="--cat:var(--accent)">今日要闻 · Top Stories</div>`;
    main += data.top.map((it) => digestItemHtml(it, true)).join("") + "</div>";
  }
  for (const sec of data.sections || []) {
    const cat = sec.cat in CAT_LABEL ? sec.cat : "tech";
    main += `<div class="digest-sec"><div class="digest-sec-title" style="--cat:${CAT_VAR[cat]}">
      ${esc(CAT_LABEL[cat][0])} · ${esc(CAT_LABEL[cat][1])}</div>`;
    main += (sec.items || []).map((it) => digestItemHtml(it)).join("") + "</div>";
  }
  const genTime = data.generated_at
    ? new Date(data.generated_at * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";
  main += `<div class="digest-meta"><span>DeepSeek 提炼自过去 24h 全部源</span>
    <span>生成于 ${genTime}</span>
    <button class="pill ghost" style="margin-left:auto" onclick="window.__retryDigest()">重新生成</button></div>`;
  wrap.innerHTML = `<div class="digest-layout">
    <div class="digest-main">${main}</div>
    <aside class="digest-market" id="digest-market">
      <div class="market-head">市场行情 · Markets</div>
      <div class="market-skel">${Array.from({length:6}).map(()=>'<div class="sk-line" style="height:34px;margin:8px 0;border-radius:9px"></div>').join("")}</div>
    </aside></div>`;
  loadMarkets();
  refreshState(false);
}
window.__retryDigest = () => renderDigestView(true);

/* ── story clusters (events): 3-column event desk ── */
const EV = { clusterId: null, articleId: null, blocks: null, article: null };

// reader-style body render (reused for the right column; mirrors renderReader)
function clusterBodyHtml(a, blocks) {
  if (blocks?.length) {
    let h = `<div class="translate-note">中英对照 · DeepSeek 译</div><div class="prose-blocks">`;
    let i = 0;
    for (const blk of blocks) {
      h += `<div class="blk" style="--i:${i++}">`;
      if (blk.t === "img") h += `<div class="blk-img"><img src="${esc(blk.x)}" alt="" referrerpolicy="no-referrer"></div>`;
      else if (blk.t === "pre") h += `<div class="blk-pre">${esc(blk.x)}</div>`;
      else { h += `<div class="blk-en ${blk.t === "h" ? "is-h" : ""}">${esc(blk.x)}</div>`; if (blk.z) h += `<div class="blk-zh ${blk.t === "h" ? "is-h" : ""}">${esc(blk.z)}</div>`; }
      h += "</div>";
    }
    return h + "</div>";
  }
  if (a.content) return `<div class="prose">${a.content}</div>`;
  return `<div class="fulltext-hint"><span>该源只提供摘要：${esc(a.summary || "无内容")}</span></div>`;
}

async function renderClustersView() {
  $("#view-title").textContent = "Events";
  $("#view-count").textContent = "";
  EV.clusterId = null; EV.articleId = null; EV.blocks = null; EV.article = null;
  $("#list").innerHTML = `<div class="ev3">
    <div class="ev-col ev-list" id="ev-list"><div class="sk-line" style="margin:16px;width:72%"></div></div>
    <div class="ev-col ev-mid" id="ev-mid"><div class="ev-ph">← 选一个事件，看多家媒体怎么报道同一件事</div></div>
    <div class="ev-col ev-read" id="ev-read">
      <button class="ev-back" data-ev-back="reports">← 各家报道</button>
      <div class="ev-read-body" id="ev-read-body"><div class="ev-ph">选一篇报道，正文显示在这里</div></div>
    </div>
  </div>`;
  try {
    const data = await api("/api/clusters");
    if (S.view.mode !== "clusters") return;   // switched away mid-fetch — don't clobber #list
    const list = $("#ev-list");
    if (!list) return;
    $("#view-count").textContent = data.refreshed_at
      ? `近 ${data.window_days || 5} 天热点 · 更新于 ${fmtTime(data.refreshed_at)}`
      : "";
    if (!data.clusters?.length) { list.innerHTML = `<div class="ev-ph">还没有聚合的事件<br><span style="font-size:11px">同一事件被 ≥2 家媒体报道时自动归并</span></div>`; return; }
    list.innerHTML = data.clusters.map((c) => `
      <button class="ev-item" data-cluster="${c.id}" data-title="${esc(c.title_zh || c.top_title)}">
        <span class="ev-item-badge">${c.heat ? `<span class="ev-item-heat">🔥 ${c.heat}</span>` : ""}<span>◈ ${c.source_count} 家 · ${c.member_count} 篇</span></span>
        <span class="ev-item-title">${esc(c.title_zh || c.top_title)}</span>
        ${c.title_zh ? `<span class="ev-item-title-en">${esc(c.top_title)}</span>` : ""}</button>`).join("");
  } catch {
    if (S.view.mode !== "clusters") return;   // switched away mid-fetch — don't clobber the new view
    const li = $("#ev-list"); if (li) li.innerHTML = `<div class="ev-ph">加载失败</div>`;
  }
}

function evSelectCluster(cid, title) {
  EV.clusterId = cid; EV.articleId = null;
  const ev3 = $(".ev3");
  ev3?.classList.add("has-cluster");           // narrow screens: drill into the event (list → reports)
  ev3?.classList.remove("has-article");
  $$(".ev-item").forEach((b) => b.classList.toggle("active", b.dataset.cluster === cid));
  $("#ev-mid").innerHTML = `<button class="ev-back" data-ev-back="list">← 事件列表</button>
    <div class="ev-event-title">${esc(title)}</div>
    <div class="ev-summary" id="ev-summary"><div class="ev-sum-loading"><i class="spin-dot"></i> gpt-5.5 事件综述生成中…</div></div>
    <button class="ev-analyze-btn" data-cluster="${cid}" data-title="${esc(title)}">
      <span class="ev-az-ic">🧠</span><span class="ev-az-tx"><b>AI 聚合分析</b><i>右侧展开完整事件报告 · 全景/时间线/各方观点/走势</i></span><span class="ev-az-arrow">→</span></button>
    <div class="ev-reports-head">各家报道</div>
    <div class="ev-reports" id="ev-reports"><div class="sk-line" style="width:60%;margin:8px 0"></div></div>`;
  $("#ev-read-body").innerHTML = `<div class="ev-ph">选一篇报道，正文显示在这里</div>`;
  api(`/api/cluster/${cid}`).then((d) => {
    if (EV.clusterId !== cid) return;
    $("#ev-reports").innerHTML = d.members.map((m) => `
      <button class="ev-report" data-id="${m.id}" style="--cat:${CAT_VAR[m.category] || "var(--accent)"}">
        ${srcBadge(m.feed_title, m.feed_id)}<span class="ev-rep-src">${esc(m.feed_title)}</span>
        <span class="ev-rep-title">${esc(m.title_zh || m.title)}</span></button>`).join("");
  }).catch(() => { if (EV.clusterId === cid) $("#ev-reports").innerHTML = `<div class="ev-ph">加载失败</div>`; });
  api(`/api/cluster/${cid}/summary`).then((d) => {
    if (EV.clusterId !== cid) return;
    const s = d.summary || {};
    $("#ev-summary").innerHTML =
      `${s.overview ? `<div class="ev-sum-over">${esc(s.overview)}</div>` : ""}
       ${s.progress?.length ? `<div class="ev-sum-prog"><span class="ev-sum-tag">进展</span><ul>${s.progress.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}
       ${s.takeaway ? `<div class="ev-sum-take">◆ ${esc(s.takeaway)}</div>` : ""}`;
  }).catch(() => { if (EV.clusterId === cid) $("#ev-summary").innerHTML = `<div class="ev-sum-err">综述暂不可用</div>`; });
}

async function evSelectArticle(id) {
  EV.articleId = id; EV.blocks = null;
  $(".ev3")?.classList.add("has-article");  // narrow screens: right column takes over
  $$(".ev-report").forEach((b) => b.classList.toggle("active", b.dataset.id === id));
  $("#ev-read-body").innerHTML = `<div class="ev-reading"><div class="sk-line" style="width:50%;margin:24px auto"></div></div>`;
  try {
    const a = await api(`/api/articles/${id}`);
    if (EV.articleId !== id) return;
    EV.article = a; EV.blocks = a.body_zh?.length ? a.body_zh : null;
    evRenderArticle();
    if (a.extracting) evPollArticle(id);
  } catch { if (EV.articleId === id) $("#ev-read-body").innerHTML = `<div class="ev-ph">加载失败</div>`; }
}

function evRenderArticle() {
  const a = EV.article; if (!a) return;
  const titleBlock = a.title_zh
    ? `<h1 class="reader-title">${esc(a.title_zh)}</h1><div class="reader-title-en">${esc(a.title)}</div>`
    : `<h1 class="reader-title">${esc(a.title)}</h1>`;
  $("#ev-read-body").innerHTML = `
    <div class="ev-read-bar">
      <span class="ev-read-meta"><b style="--cat:${CAT_VAR[a.category]}">${esc(a.feed_title)}</b> · ${fmtTime(a.published)}</span>
      <span class="ev-read-acts">
        <button class="pill ghost ev-btn-translate" data-id="${a.id}">${EV.blocks ? "原文" : "中英对照"}</button>
        <a class="pill ghost" href="${/^https?:\/\//i.test(a.link) ? esc(a.link) : "#"}" target="_blank" rel="noopener">原文 ↗</a></span>
    </div>
    <div class="reader-inner ev-read-inner ${EV.blocks ? "bilingual" : ""}">${titleBlock}${clusterBodyHtml(a, EV.blocks)}</div>`;
  $$("#ev-read img").forEach((im) => { im.loading = "eager"; });
}

async function evPollArticle(id) {
  for (let i = 0; i < 20 && EV.articleId === id; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    if (EV.articleId !== id) return;
    const a = await api(`/api/articles/${id}`).catch(() => null);
    if (a && (a.has_fulltext || !a.extracting)) {
      EV.article = a; if (a.body_zh?.length) EV.blocks = a.body_zh; evRenderArticle(); return;
    }
  }
}

async function evToggleTranslate(id) {
  if (EV.blocks) { EV.blocks = null; evRenderArticle(); return; }
  const btn = $(".ev-btn-translate"); if (btn) { btn.classList.add("busy"); btn.textContent = "翻译中…"; }
  try {
    const data = await api(`/api/articles/${id}/translate`, { method: "POST" });
    if (EV.articleId === id) { EV.blocks = data.blocks; evRenderArticle(); }
  } catch (e) { toast(`翻译失败：${esc(e.message)}`); btn?.classList.remove("busy"); if (btn) btn.textContent = "中英对照"; }
}

async function evShowAnalysis(cid, title) {
  const read = $("#ev-read-body");
  if (!read) return;   // view switched away before the click resolved
  EV.articleId = null;
  $(".ev3")?.classList.add("has-article");   // narrow screens: right column takes over
  $$(".ev-report").forEach((b) => b.classList.remove("active"));
  $(".ev-analyze-btn")?.classList.add("busy");
  read.innerHTML = `<div class="ev-analysis-loading"><i class="spin-dot"></i><div>gpt-5.5 正在生成深度事件分析…</div><div class="ev-al-sub">综合多家媒体报道，梳理来龙去脉与后续走势</div></div>`;
  try {
    const d = await api(`/api/cluster/${cid}/analysis`);
    if (EV.clusterId !== cid || !$("#ev-read-body")) return;
    renderAnalysis(d.analysis || {}, title);
  } catch (e) {
    if ($("#ev-read-body")) $("#ev-read-body").innerHTML = `<div class="ev-ph">分析生成失败：${esc(e.message)}</div>`;
  } finally { $(".ev-analyze-btn")?.classList.remove("busy"); }
}

function evOddsClass(o) { return /高|high/i.test(o) ? "hi" : /低|low/i.test(o) ? "lo" : "mid"; }

function renderAnalysis(a, title) {
  // icon/name 必须是硬编码常量 — 两者都未经 esc()，只有 body 是已转义的 HTML
  const sec = (icon, name, body) => body ? `<section class="an-sec"><h3 class="an-h"><span>${icon}</span>${name}</h3>${body}</section>` : "";
  const facts = a.facts?.length
    ? `<div class="an-facts">${a.facts.map((f) => {
        const long = (f.v || "").length > 9;   // long values render as quiet body text, not a big number
        return `<div class="an-fact${long ? " long" : ""}"><div class="an-fact-k">${esc(f.k)}</div><div class="an-fact-v">${esc(f.v)}</div></div>`;
      }).join("")}</div>` : "";
  const timeline = a.timeline?.length
    ? sec("⏱", "事件时间线", `<div class="an-timeline">${a.timeline.map((t) => `<div class="an-tl"><div class="an-tl-dot"></div><div class="an-tl-t">${esc(t.t)}</div><div class="an-tl-e">${esc(t.e)}</div></div>`).join("")}</div>`) : "";
  const PC = ["var(--c-markets)", "var(--c-tech)", "var(--c-ai)", "var(--c-cn)", "var(--c-world)", "var(--c-x)"];
  const persp = a.perspectives?.length
    ? sec("👥", "各方观点", `<div class="an-persp">${a.perspectives.map((p, i) => `<div class="an-p" style="--pc:${PC[i % PC.length]}"><div class="an-p-who">${esc(p.who)}</div><div class="an-p-view">${esc(p.view)}</div></div>`).join("")}</div>`) : "";
  const impact = a.impact?.length
    ? sec("💥", "影响分析", `<div class="an-impact">${a.impact.map((i) => `<div class="an-i"><span class="an-i-area">${esc(i.area)}</span><span class="an-i-detail">${esc(i.detail)}</span></div>`).join("")}</div>`) : "";
  const contro = a.controversy ? sec("⚡", "争议焦点", `<div class="an-contro">${esc(a.controversy)}</div>`) : "";
  const outlook = a.outlook?.length
    ? sec("🔮", "后续展望", `<div class="an-outlook">${a.outlook.map((o) => `<div class="an-o an-o-${evOddsClass(o.odds)}"><div class="an-o-head"><span class="an-o-path">${esc(o.path)}</span><span class="an-o-odds">可能性 ${esc(o.odds)}</span></div><div class="an-o-detail">${esc(o.detail)}</div></div>`).join("")}</div>`) : "";
  $("#ev-read-body").innerHTML = `
    <div class="ev-read-bar"><span class="ev-read-meta">🧠 AI 深度事件分析 · gpt-5.5</span></div>
    <div class="ev-analysis">
      <h1 class="an-title">${esc(title)}</h1>
      ${facts}
      ${sec("📋", "事件全景", a.panorama ? `<p class="an-panorama">${esc(a.panorama)}</p>` : "")}
      ${timeline}${persp}${impact}${contro}${outlook}
    </div>`;
}

$("#list").addEventListener("click", (e) => {
  if (S.view.mode !== "clusters") return;
  const back = e.target.closest(".ev-back");
  if (back) return evBack(back.dataset.evBack);
  const item = e.target.closest(".ev-item");
  if (item) return evSelectCluster(item.dataset.cluster, item.dataset.title);
  const az = e.target.closest(".ev-analyze-btn");
  if (az) return evShowAnalysis(az.dataset.cluster, az.dataset.title);
  const rep = e.target.closest(".ev-report");
  if (rep) return evSelectArticle(rep.dataset.id);
  const tb = e.target.closest(".ev-btn-translate");
  if (tb) return evToggleTranslate(tb.dataset.id);
});

// narrow-screen drill-out: article/analysis → reports → event list (no-op on desktop, back buttons are hidden)
function evBack(to) {
  const ev3 = $(".ev3"); if (!ev3) return;
  if (to === "reports") ev3.classList.remove("has-article");
  else ev3.classList.remove("has-cluster", "has-article");
}

// mini monthly trend sparkline (SVG); colored up/down via the row's class (currentColor)
function sparkline(vals) {
  if (!Array.isArray(vals) || vals.length < 2) return "";
  const w = 50, h = 22, pad = 2.5, n = vals.length;
  const min = Math.min(...vals), max = Math.max(...vals), rng = (max - min) || 1;
  const pts = vals.map((v, i) => {
    const x = pad + (i / (n - 1)) * (w - 2 * pad);
    const y = pad + (1 - (v - min) / rng) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`
    + `<path class="area" d="M${pad},${h - pad} L${pts.join(" L")} L${(w - pad).toFixed(1)},${h - pad} Z"/>`
    + `<path class="line" d="M${pts.join(" L")}"/></svg>`;
}

async function loadMarkets() {
  const el = $("#digest-market");
  if (!el) return;
  try {
    const data = await api("/api/markets");
    if (!$("#digest-market")) return;  // view changed while loading
    const time = data.ts ? new Date(data.ts * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";
    el.innerHTML = `<div class="market-head">市场行情 · Markets</div>` +
      data.items.map((m) => {
        const up = m.change >= 0;
        const price = esc(Number(m.price).toLocaleString("en-US", { maximumFractionDigits: 2 }));
        const pct = esc(Math.abs(Number(m.change_pct)).toFixed(2));
        return `<div class="market-row ${up ? "up" : "down"}">
          <span class="market-name">${esc(m.name)}</span>
          <span class="market-val"><span class="market-price">${price}</span><span class="market-chg">${up ? "▲" : "▼"} ${pct}%</span></span>
          ${sparkline(m.spark)}
        </div>`;
      }).join("") +
      `<div class="market-foot">Yahoo Finance · ${time} 更新</div>`;
  } catch {
    if ($("#digest-market"))
      $("#digest-market").innerHTML = `<div class="market-head">市场行情</div><div class="market-foot">行情暂不可用</div>`;
  }
}

/* ── highlights view ─────────────────────────────── */

async function renderHighlightsView() {
  $("#view-title").textContent = "Highlights";
  $("#view-count").textContent = "";
  $("#list-end").classList.add("hidden");
  const wrap = $("#list");
  wrap.innerHTML = `<div class="digest-wrap"><div class="sk-line" style="width:50%;height:18px"></div></div>`;
  try {
    const data = await api("/api/highlights");
    if (S.view.mode !== "highlights") return;   // switched away mid-fetch — don't clobber the new view
    $("#view-count").textContent = data.items.length ? `${data.items.length} 条` : "";
    if (!data.items.length) {
      wrap.innerHTML = `<div class="empty"><svg><use href="#i-marker"/></svg>
        <div class="empty-line">还没有高亮 — 阅读时选中文字即可标记。</div>
        <div class="empty-sub">高亮的句子会在这里聚合，点击可跳回原文</div></div>`;
      return;
    }
    wrap.innerHTML = `<div class="digest-wrap">` + data.items.map((h) => `
      <div class="hl-card" data-id="${h.id}" data-article="${h.article_id}">
        <div class="hl-card-text">${esc(h.text)}</div>
        <div class="hl-card-meta">
          ${srcBadge(h.feed_title, h.feed_id)}<span>${esc(h.feed_title)}</span><span>·</span>
          <span>${esc((h.title_zh || h.title).slice(0, 50))}</span>
          <span>·</span><span>${fmtTime(h.created_at)}</span>
          <span class="del" title="删除高亮">✕</span>
        </div>
      </div>`).join("") + "</div>";
  } catch (err) {
    if (S.view.mode !== "highlights") return;   // switched away mid-fetch — don't clobber the new view
    wrap.innerHTML = `<div class="empty"><div class="empty-line">加载失败</div><div class="empty-sub">${esc(err.message)}</div></div>`;
  }
}

function renderListSkeleton() {
  $("#view-title").textContent = viewTitle();
  $("#view-count").textContent = "";
  $("#list").innerHTML = Array.from({ length: 6 }).map(() => `
    <div class="sk">
      <span></span>
      <div><div class="sk-line" style="width:30%;height:11px;margin-bottom:10px"></div>
        <div class="sk-line" style="width:92%;height:16px;margin-bottom:8px"></div>
        <div class="sk-line" style="width:64%;height:13px"></div></div>
      <div class="sk-line sk-thumb"></div>
    </div>`).join("");
  $("#list-end").classList.add("hidden");
}

let loadSeq = 0;
async function loadArticles(reset = false, silent = false) {
  if (reset && !silent) { S.articles = []; S.nextBefore = 0; S.nextBeforeId = 0; S.selectedIdx = -1; }
  // a reset (view switch) always supersedes an in-flight load; pagination waits
  if (S.loadingMore && !reset) return;
  const seq = ++loadSeq;
  S.loadingMore = true;
  try {
    const p = new URLSearchParams();
    if (S.view.category) p.set("category", S.view.category);
    if (S.view.feedId) p.set("feed_id", S.view.feedId);
    if (S.view.filter !== "all") p.set("filter", S.view.filter);
    if (S.view.tag) p.set("tag", S.view.tag);
    if (S.view.monitor) p.set("monitor", S.view.monitor);
    if (S.view.q) p.set("q", S.view.q);
    if (!reset && S.nextBefore) {  // compound cursor — send both halves
      p.set("before", S.nextBefore);
      p.set("before_id", S.nextBeforeId);
    }
    const data = await api(`/api/articles?${p}`);
    if (seq !== loadSeq) return;  // a newer view switch already started
    S.articles = reset ? data.items : S.articles.concat(data.items);
    S.nextBefore = data.next_before;
    S.nextBeforeId = data.next_before_id || 0;
    if (reset) viewCachePut(data.items, data.next_before, data.next_before_id);
    renderList();
    if (S.titleTrans) translateVisibleTitles(data.items);
  } catch (err) {
    if (!silent && err.message !== "unauthorized") toast(`加载失败：${esc(err.message)}`);
  } finally { if (seq === loadSeq) S.loadingMore = false; }
}

function itemHtml(a, i) {
  const zh = S.titleTrans && a.title_zh
    ? `<div class="item-zh">${esc(a.title_zh)}</div>` : "";
  // tweets (x category) repeat the title inside summary — show one, not both
  const isTweet = a.category === "x";
  const showSummary = a.summary && !isTweet &&
    !a.summary.startsWith(a.title.slice(0, 40));
  const mins = a.word_count > 350 ? `<span>·</span><span class="item-min">${fmtMins(a.word_count)} min</span>` : "";
  const progress = a.progress > 4 && a.progress < 96 && !a.is_read
    ? `<span class="item-progressbar" style="width:${Math.round(a.progress)}%"></span>` : "";
  return `<article class="item ${a.is_read ? "read" : ""}" data-id="${a.id}" tabindex="0" role="button" aria-label="${esc(a.title_zh || a.title)}" style="--i:${i};--cat:${CAT_VAR[a.category]}">
    <span class="item-dot"></span>
    <div class="item-body">
      <div class="item-meta">
        ${srcBadge(a.feed_title, a.feed_id)}
        <span class="item-src">${esc(a.feed_title)}</span><span>·</span><span>${fmtTime(a.published)}</span>${mins}
        ${a.read_later ? '<svg class="item-later-mini"><use href="#i-clock"/></svg>' : ""}
        ${a.is_starred ? '<svg class="item-star-mini"><use href="#i-star"/></svg>' : ""}
      </div>
      <h3 class="item-title">${esc(isTweet ? tweetText(a) : a.title)}</h3>
      ${zh}
      ${showSummary ? `<p class="item-summary">${esc(a.summary)}</p>` : ""}
      ${(a.tags || []).length ? `<div class="item-tags">${a.tags.slice(0, 3).map((t) =>
        `<span class="item-tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("")}</div>` : ""}
    </div>
    ${a.image ? `<div class="item-thumb"><img src="${esc(a.image)}" alt="" loading="lazy"></div>` : ""}
    ${progress}
  </article>`;
}

function renderList() {
  $("#view-title").textContent = viewTitle();
  $("#view-count").textContent = S.articles.length
    ? `${S.articles.length}${S.nextBefore ? "+" : ""} 篇` : "";
  if (!S.articles.length) {
    $("#list").innerHTML = `<div class="empty">
      <svg><use href="#i-empty"/></svg>
      <div class="empty-line">All caught up — 世界此刻安静。</div>
      <div class="empty-sub">没有${S.view.filter === "unread" ? "未读" : ""}文章，去别的分类逛逛或点右上角刷新</div></div>`;
    $("#list-end").classList.add("hidden");
    return;
  }
  let html = "", lastGroup = "", idx = 0;
  for (const a of S.articles) {
    const g = groupKey(a.published);
    if (g !== lastGroup) {
      const n = S.articles.filter((x) => groupKey(x.published) === g).length;
      html += `<div class="group-head">${g}<span class="num">№ ${n}</span></div>`;
      lastGroup = g;
    }
    html += itemHtml(a, idx++);
  }
  $("#list").innerHTML = html;
  $("#list-end").classList.toggle("hidden", !!S.nextBefore);
}

$("#list").addEventListener("click", async (e) => {
  const link = e.target.closest(".digest-link");
  if (link) { openArticle(+link.dataset.id); return; }
  const del = e.target.closest(".hl-card .del");
  if (del) {
    const card = del.closest(".hl-card");
    await api(`/api/highlights/${card.dataset.id}`, { method: "DELETE" }).catch(() => {});
    card.remove(); refreshState(false);
    return;
  }
  const hlText = e.target.closest(".hl-card-text");
  if (hlText) { openArticle(+hlText.closest(".hl-card").dataset.article); return; }
  const itemTag = e.target.closest(".item-tag");
  if (itemTag) { jumpTag(itemTag.dataset.tag); return; }  // filter, don't open
  const item = e.target.closest(".item");
  if (item) openArticle(+item.dataset.id);
});
// keyboard: Enter/Space on a focused list item opens it (items are tabindex=0).
// stopPropagation so the global Enter handler (j/k selection) doesn't also fire.
$("#list").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const item = e.target.closest?.(".item");
  if (item && e.target === item) {
    e.preventDefault(); e.stopPropagation();
    openArticle(+item.dataset.id);
  }
});

/* infinite scroll */
new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting && S.nextBefore && !S.loadingMore) loadArticles();
}, { root: $("#list-wrap"), rootMargin: "600px" }).observe($("#list-sentinel"));

/* ── ticker ──────────────────────────────────────── */

async function loadTicker() {
  try {
    const data = await api("/api/articles?limit=14");
    const items = data.items;
    if (!items.length) return;
    const cell = (a) => `<span class="tick" data-id="${a.id}" style="--cat:${CAT_VAR[a.category]}">
      <span class="tick-src">${esc(a.feed_title)}</span>${esc(a.title)}<span class="tick-sep">◆</span></span>`;
    const half = items.map(cell).join("");
    const track = $("#ticker-track");
    track.innerHTML = half + half;
    track.style.setProperty("--ticker-dur", `${Math.max(40, items.length * 6)}s`);
  } catch { /* ticker is decorative */ }
}

$("#ticker").addEventListener("click", (e) => {
  const tick = e.target.closest(".tick");
  if (tick) openArticle(+tick.dataset.id);
});

/* ── title translation ───────────────────────────── */

async function translateVisibleTitles(items) {
  const ids = (items || S.articles).filter((a) => !a.title_zh).map((a) => a.id).slice(0, 60);
  if (!ids.length) return;
  try {
    const data = await api("/api/translate-titles", { method: "POST", body: { ids } });
    for (const a of S.articles)
      if (data.titles[a.id] !== undefined) a.title_zh = data.titles[a.id];
    renderList();
    renderUsage(); refreshState(false);
  } catch (err) { toast(`标题翻译失败：${esc(err.message)}`); }
}

$("#btn-titles").addEventListener("click", () => {
  S.titleTrans = !S.titleTrans;
  localStorage.setItem("m.titles", S.titleTrans ? "1" : "0");
  $("#btn-titles").classList.toggle("on", S.titleTrans);
  renderList();
  if (S.titleTrans) translateVisibleTitles();
});

/* ── reader ──────────────────────────────────────── */

const _articleCache = new Map();   // id -> detail payload (prefetch)
function nextArticleId(id) {
  const idx = S.articles.findIndex((a) => a.id === id);
  return idx >= 0 && S.articles[idx + 1] ? S.articles[idx + 1].id : 0;
}
function prefetchArticle(id) {
  if (!id || _articleCache.has(id)) return;
  api(`/api/articles/${id}?prefetch=1`).then((a) => {   // don't trigger extraction on speculative warmup
    _articleCache.set(id, a);
    if (_articleCache.size > 12) _articleCache.delete(_articleCache.keys().next().value);
  }).catch(() => {});
}

async function openArticle(id) {
  const listItem = S.articles.find((a) => a.id === id);
  if (!$("#reader").classList.contains("open"))  // remember where to return
    S.listScrollTop = $("#list-wrap").scrollTop;
  ttsStop();
  S.related = null;  // clear previous article's related before re-fetching
  openReaderShell(listItem);
  S.extractPoll = (S.extractPoll || 0) + 1;
  const token = S.extractPoll;  // invalidate polling if user opens another article
  try {
    const cached = _articleCache.get(id);
    _articleCache.delete(id);  // consume & evict so is_starred/read_later can't go stale
    const a = cached || await api(`/api/articles/${id}`);
    if (token !== S.extractPoll) return;
    S.current = a; S.currentBlocks = a.body_zh?.length ? a.body_zh : null;
    renderReader();
    loadRelated(id);
    prefetchArticle(nextArticleId(id));  // next article opens instantly
    if (a.extracting) pollExtraction(id, token);
    if (!a.is_read) {
      api(`/api/articles/${id}/read`, { method: "POST", body: { value: true } }).catch(() => {});
      a.is_read = true;
      if (listItem && !listItem.is_read) { listItem.is_read = true; markItemRead(id); decUnread(a); }
    }
  } catch (err) {
    if (err.message !== "unauthorized")
      $("#reader-inner").innerHTML = `<div class="empty"><div class="empty-line">加载失败</div><div class="empty-sub">${esc(err.message)}</div></div>`;
  }
}

async function loadRelated(id) {
  try {
    const data = await api(`/api/articles/${id}/related`);
    if (S.current && S.current.id === id) {
      S.related = { id, items: data.items };  // cache so re-renders keep it
      renderRelated();
    }
  } catch { /* related is a bonus */ }
}

// (re)render related at the foot of the reader. Called from renderReader too,
// so a full-text/translate re-render of #reader-inner doesn't drop it.
function renderRelated() {
  $("#reader-inner .related")?.remove();
  const r = S.related;
  if (!S.current || !r || r.id !== S.current.id || !r.items.length) return;
  const html = `<div class="related"><div class="related-head"><svg><use href="#i-link2"/></svg>相关阅读</div>${
    r.items.map((a) => `<div class="related-item" data-id="${a.id}">
      <span class="related-src" style="--cat:${CAT_VAR[a.category]}">${esc(a.feed_title)}</span>
      <span class="related-title">${esc(a.title_zh || a.title)}</span></div>`).join("")}</div>`;
  $("#reader-inner").insertAdjacentHTML("beforeend", html);
}

// Poll a backgrounded full-text extraction (~3s × up to 15 ≈ 45s) and swap
// the body in when it lands. Each request is fast, so no long-connection reset.
async function pollExtraction(id, token, tries = 0) {
  if (token !== S.extractPoll || tries > 15) {
    if (S.current && S.current.id === id && S.current.extracting) {
      S.current.extracting = false; renderReader();
    }
    return;
  }
  await new Promise((r) => setTimeout(r, 3000));
  if (token !== S.extractPoll) return;
  try {
    const a = await api(`/api/articles/${id}`);
    if (token !== S.extractPoll) return;
    if (a.has_fulltext || a.extract_tried || !a.extracting) {
      const wasTranslated = !!S.currentBlocks;
      S.current = a; S.currentBlocks = a.body_zh?.length ? a.body_zh : null;
      renderReader();
      if (a.has_fulltext && !wasTranslated) toast("全文已加载");
      return;
    }
    pollExtraction(id, token, tries + 1);
  } catch {
    pollExtraction(id, token, tries + 1);
  }
}

function markItemRead(id) {
  const el = $(`.item[data-id="${id}"]`);
  if (el) el.classList.add("read");
}

function decUnread(a) {
  const u = S.state.unread;
  if (u[a.category] > 0) u[a.category]--;
  const feed = S.state.feeds.find((f) => f.id === a.feed_id);
  if (feed && feed.unread > 0) feed.unread--;
  renderNav(); renderFeeds();
}

function openReaderShell(listItem) {
  $("#reader").classList.add("open");
  $("#scrim").classList.add("show");
  $("#reader-scroll").scrollTop = 0;
  S.current = null; S.currentBlocks = null; S.restoredScroll = false;
  hlPop.classList.add("hidden");
  $("#btn-translate").classList.remove("busy", "on");
  $("#reader-bar-meta").innerHTML = listItem
    ? `<b style="--cat:${CAT_VAR[listItem.category]}">${esc(listItem.feed_title)}</b> · ${fmtTime(listItem.published)}` : "";
  $("#reader-inner").innerHTML = `
    <div class="sk-line" style="width:38%;height:12px;margin-bottom:22px"></div>
    <div class="sk-line" style="width:96%;height:30px;margin-bottom:12px"></div>
    <div class="sk-line" style="width:70%;height:30px;margin-bottom:34px"></div>
    ${Array.from({ length: 5 }).map((_, i) =>
      `<div class="sk-line" style="width:${[98, 94, 97, 88, 60][i]}%;height:15px;margin-bottom:13px"></div>`).join("")}`;
}

function readerMetaHtml(a) {
  const words = a.word_count || 0;
  const mins = Math.max(1, Math.round(words / 1100));
  return `<div class="reader-meta">
    <span><b style="color:${CAT_VAR[a.category]}">${esc(a.feed_title)}</b></span>
    ${a.author ? `<span>${esc(a.author)}</span>` : ""}
    <span>${new Date(a.published * 1000).toLocaleString("zh-CN", { dateStyle: "long", timeStyle: "short" })}</span>
    <span>≈ ${mins} 分钟读完</span>
    ${a.has_fulltext ? "<span>全文模式</span>" : ""}
  </div>`;
}

function renderReader() {
  const a = S.current;
  if (!a) return;
  $("#reader-bar-meta").innerHTML =
    `<b style="--cat:${CAT_VAR[a.category]}">${esc(a.feed_title)}</b> · ${fmtTime(a.published)}`;
  $("#btn-star").classList.toggle("active", a.is_starred);
  $("#btn-original").href = /^https?:\/\//i.test(a.link) ? a.link : "#";
  $("#btn-fulltext").style.display = (a.has_fulltext || a.no_extract) ? "none" : "";
  $("#btn-translate").classList.toggle("on", !!S.currentBlocks);

  const catColor = CAT_VAR[a.category];
  // Chinese-first headline when a translation exists (Margin/QMReader pattern)
  const titleBlock = a.title_zh
    ? `<h1 class="reader-title">${esc(a.title_zh)}</h1>
       <div class="reader-title-en">${esc(a.title)}</div>`
    : `<h1 class="reader-title">${esc(a.title)}</h1>`;
  const tagChips = (a.tags || []).length
    ? `<div class="reader-tags">${a.tags.map((t) =>
        `<span class="reader-tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("")}</div>` : "";
  let html = `<div class="reader-cat" style="--cat:${catColor}">${esc(CAT_LABEL[a.category]?.[0] || "")} · ${esc(CAT_LABEL[a.category]?.[1] || "")}</div>
    ${titleBlock}
    ${readerMetaHtml(a)}
    ${tagChips}
    <div id="summary-slot">${a.summary_zh ? summaryCardHtml(a.summary_zh) : ""}</div>`;

  if (S.currentBlocks) {
    html += `<div class="translate-note">中英对照 · DeepSeek 译</div><div class="prose-blocks">`;
    let i = 0;
    for (const blk of S.currentBlocks) {
      html += `<div class="blk" style="--i:${i++}">`;
      if (blk.t === "img") html += `<div class="blk-img"><img src="${esc(blk.x)}" alt="" referrerpolicy="no-referrer"></div>`;
      else if (blk.t === "pre") html += `<div class="blk-pre">${esc(blk.x)}</div>`;
      else {
        html += `<div class="blk-en ${blk.t === "h" ? "is-h" : ""}">${esc(blk.x)}</div>`;
        if (blk.z) html += `<div class="blk-zh ${blk.t === "h" ? "is-h" : ""}">${esc(blk.z)}</div>`;
      }
      html += "</div>";
    }
    html += "</div>";
  } else if (a.content) {
    html += `<div class="prose">${a.content}</div>`;  // sanitized server-side
  } else {
    html += `<div class="fulltext-hint"><span>该源只提供摘要：${esc(a.summary || "无内容")}</span></div>`;
  }

  if (!a.has_fulltext && a.extracting)
    html += `<div class="fulltext-hint extracting"><span><i class="spin-dot"></i>正在提取全文，请稍候…</span></div>`;
  else if (!a.has_fulltext && a.paywalled)
    html += `<div class="fulltext-hint"><span>📰 该站为付费墙，仅提供 RSS 摘要 — 完整内容请打开右上角原文</span></div>`;
  else if (!a.has_fulltext && a.extract_tried)
    html += `<div class="fulltext-hint"><span>全文提取失败，可打开原文阅读</span>
      <button class="pill ghost" id="btn-retry-fulltext">重试提取</button></div>`;

  $("#reader-inner").innerHTML = html;
  $("#reader-inner").classList.toggle("bilingual", !!S.currentBlocks);
  // force eager on body images — container-type breaks lazy; also fixes the
  // already-stored articles whose HTML still carries loading="lazy"
  $$("#reader-inner img").forEach((im) => { im.loading = "eager"; });
  applyType();
  $("#btn-retry-fulltext")?.addEventListener("click", loadFulltext);
  $("#btn-later").classList.toggle("active", !!a.read_later);
  applyHighlights();
  renderRelated();  // re-append related (survives full-text/translate re-renders)
  if (a.progress > 8 && a.progress < 96 && !S.restoredScroll) {
    S.restoredScroll = true;
    const el = $("#reader-scroll");
    requestAnimationFrame(() =>
      el.scrollTo({ top: (el.scrollHeight - el.clientHeight) * a.progress / 100 }));
  }
}

/* wrap stored highlight texts in <mark> via text-node walking */
function applyHighlights() {
  const texts = (S.current?.highlights || []).map((h) => h.text).filter(Boolean);
  if (!texts.length) return;
  const root = $("#reader-inner");
  for (const text of texts) markText(root, text);
}

function markText(root, text) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => n.parentElement.closest("mark, script, style")
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  let node;
  while ((node = walker.nextNode())) {
    const idx = node.data.indexOf(text);
    if (idx === -1) continue;
    const range = document.createRange();
    range.setStart(node, idx);
    range.setEnd(node, idx + text.length);
    const mark = document.createElement("mark");
    mark.className = "hl";
    try { range.surroundContents(mark); } catch { /* crosses elements — skip */ }
    return;  // first occurrence only
  }
}

function summaryCardHtml(summary) {
  // summary: {tldr, points[]} (v2) or plain string (loading text / legacy)
  const struct = typeof summary === "string" ? { tldr: summary, points: [] } : summary;
  const points = (struct.points || []).length
    ? `<ul class="summary-points">${struct.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
  return `<div class="summary-card">
    <div class="summary-label"><svg><use href="#i-sparkle"/></svg>AI 摘要 · TL;DR</div>
    <div class="summary-tldr">${esc(struct.tldr || "")}</div>${points}</div>`;
}

function closeReader() {
  clearTimeout(progressTimer);  // don't let a pending tick write to the next article
  progressTimer = 0; pendingProgress = -1;
  ttsStop();
  $("#reader").classList.remove("open");
  $("#scrim").classList.remove("show");
  $("#type-pop").classList.add("hidden");
  S.current = null;
  hlPop.classList.add("hidden");
  if (S.listScrollTop != null)  // return to where the list was
    requestAnimationFrame(() => { $("#list-wrap").scrollTop = S.listScrollTop; });
}
$("#reader-inner").addEventListener("click", (e) => {
  const tag = e.target.closest(".reader-tag");
  if (tag) { closeReader(); jumpTag(tag.dataset.tag); return; }
  const rel = e.target.closest(".related-item");
  if (rel) openArticle(+rel.dataset.id);
});
$("#btn-close-reader").addEventListener("click", closeReader);
$("#scrim").addEventListener("click", () => { closeReader(); closeSidebar(); });

/* reader actions */
$("#btn-star").addEventListener("click", async () => {
  if (!S.current) return;
  const data = await api(`/api/articles/${S.current.id}/star`, { method: "POST" });
  S.current.is_starred = data.starred;
  $("#btn-star").classList.toggle("active", data.starred);
  const listItem = S.articles.find((x) => x.id === S.current.id);
  if (listItem) listItem.is_starred = data.starred;
  S.state.starred = Math.max(0, S.state.starred + (data.starred ? 1 : -1));
  renderNav();
  toast(data.starred ? "已收藏 ★" : "已取消收藏");
});

$("#btn-translate").addEventListener("click", async () => {
  if (!S.current) return;
  if (S.currentBlocks) { S.currentBlocks = null; renderReader(); return; }
  const btn = $("#btn-translate");
  btn.classList.add("busy"); btn.disabled = true;
  try {
    const data = await api(`/api/articles/${S.current.id}/translate`, { method: "POST" });
    S.currentBlocks = data.blocks;
    renderReader();
    renderUsage(); refreshState(false);
    if (!S.current.title_zh) {
      const t = await api("/api/translate-titles", { method: "POST", body: { ids: [S.current.id] } });
      if (t.titles[S.current.id]) { S.current.title_zh = t.titles[S.current.id]; renderReader(); }
    }
  } catch (err) { toast(`翻译失败：${esc(err.message)}`); }
  finally { btn.classList.remove("busy"); btn.disabled = false; }
});

$("#btn-summary").addEventListener("click", async () => {
  if (!S.current) return;
  const slot = $("#summary-slot");
  if (S.current.summary_zh) { slot.scrollIntoView({ behavior: scrollBehavior() }); return; }
  slot.innerHTML = summaryCardHtml("正在阅读全文并撰写摘要…").replace("summary-card", "summary-card loading");
  try {
    const data = await api(`/api/articles/${S.current.id}/summarize`, { method: "POST" });
    S.current.summary_zh = data.summary;
    slot.innerHTML = summaryCardHtml(data.summary);
    renderUsage(); refreshState(false);
  } catch (err) {
    slot.innerHTML = "";
    toast(`摘要失败：${esc(err.message)}`);
  }
});

async function loadFulltext() {
  if (!S.current) return;
  const id = S.current.id;
  try {
    const r = await api(`/api/articles/${id}/extract`, { method: "POST" });
    if (r.has_fulltext) { return reloadCurrent(id); }
    if (r.paywalled) { toast("付费墙站点，仅提供摘要"); return; }
    S.current.extracting = true; renderReader();
    pollExtraction(id, S.extractPoll);
  } catch (err) { toast(`提取失败：${esc(err.message)}`); }
}
async function reloadCurrent(id) {
  const a = await api(`/api/articles/${id}`);
  S.current = a; S.currentBlocks = a.body_zh?.length ? a.body_zh : null;
  renderReader();
}
$("#btn-fulltext").addEventListener("click", loadFulltext);

/* reading progress: bar + throttled server report */
$("#reader-scroll").addEventListener("scroll", () => {
  const el = $("#reader-scroll");
  const max = el.scrollHeight - el.clientHeight;
  const pct = max > 0 ? (el.scrollTop / max) * 100 : 0;
  $("#reader-progress-fill").style.width = `${pct}%`;
  if (!S.current) return;
  pendingProgress = Math.max(pendingProgress, pct);
  if (!progressTimer) {
    progressTimer = setTimeout(() => {
      progressTimer = 0;
      if (!S.current || pendingProgress < 0) return;
      const value = Math.min(100, Math.round(pendingProgress * 10) / 10);
      const listItem = S.articles.find((x) => x.id === S.current.id);
      if (listItem) listItem.progress = Math.max(listItem.progress || 0, value);
      api(`/api/articles/${S.current.id}/progress`,
          { method: "POST", body: { value } }).catch(() => {});
      pendingProgress = -1;
    }, 2500);
  }
}, { passive: true });

/* read later */
$("#btn-later").addEventListener("click", async () => {
  if (!S.current) return;
  const data = await api(`/api/articles/${S.current.id}/later`, { method: "POST" });
  S.current.read_later = data.read_later;
  $("#btn-later").classList.toggle("active", data.read_later);
  const listItem = S.articles.find((x) => x.id === S.current.id);
  if (listItem) listItem.read_later = data.read_later;
  S.state.later = Math.max(0, S.state.later + (data.read_later ? 1 : -1));
  renderNav();
  toast(data.read_later ? "已加入稍后读 ⏰" : "已移出稍后读");
});

/* prev / next article */
function navigateArticle(delta) {
  if (!S.current) return;
  const idx = S.articles.findIndex((x) => x.id === S.current.id);
  if (idx === -1) return;
  const target = S.articles[idx + delta];
  if (!target) { toast(delta > 0 ? "已是最后一篇" : "已是第一篇"); return; }
  S.restoredScroll = false;
  openArticle(target.id);
}
$("#btn-prev").addEventListener("click", () => navigateArticle(-1));
$("#btn-next").addEventListener("click", () => navigateArticle(1));

/* ── TTS: read aloud with word-level highlight (SpeechSynthesis) ── */
const tts = { utter: null, rate: 1, spans: [], lastMark: null, gen: 0 };
function ttsSupported() { return "speechSynthesis" in window; }

function ttsBuildSpans() {
  // wrap each word of the prose/blocks in a span so we can highlight on boundary
  const root = $("#reader-inner");
  const blocks = root.querySelectorAll(
    ".prose p, .prose li, .prose h2, .prose h3, .blk-en, .blk-zh, .reader-title, .reader-title-en, .summary-tldr, .summary-points li");
  const segs = [];
  blocks.forEach((b) => {
    if (b.closest(".related")) return;
    const text = b.textContent.trim();
    if (text) segs.push({ el: b, text });
  });
  return segs;
}

function ttsStop() {
  if (!ttsSupported()) return;
  tts.gen++;  // invalidate any in-flight onend callback from a prior article
  try {
    // Chrome bug: cancel() is ignored while paused — resume first so it takes
    window.speechSynthesis.resume();
    window.speechSynthesis.cancel();
  } catch { /* ignore */ }
  if (tts.lastMark) tts.lastMark.classList?.remove("tts-word");
  tts.lastMark = null; tts.utter = null;
  $("#tts-bar").classList.add("hidden");
  $("#btn-listen").classList.remove("active");
}
// safety net: never let audio keep playing when the tab is hidden/unloaded
window.addEventListener("pagehide", ttsStop);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") ttsStop();
});

function ttsStart() {
  if (!ttsSupported()) { toast("浏览器不支持朗读"); return; }
  ttsStop();
  const myGen = tts.gen;  // this run's generation; stale callbacks bail out
  const segs = ttsBuildSpans();
  if (!segs.length) { toast("没有可朗读的内容"); return; }
  $("#tts-bar").classList.remove("hidden");
  $("#btn-listen").classList.add("active");
  $("#tts-status").textContent = "朗读中";
  let i = 0;
  const speakNext = () => {
    if (tts.gen !== myGen) return;  // a newer stop/start superseded this run
    if (i >= segs.length) { ttsStop(); return; }
    const seg = segs[i];
    const u = new SpeechSynthesisUtterance(seg.text);
    u.rate = tts.rate;
    u.lang = /[一-鿿]/.test(seg.text) ? "zh-CN" : "en-US";
    seg.el.scrollIntoView({ block: "center", behavior: scrollBehavior() });
    u.onstart = () => { seg.el.classList.add("tts-word"); tts.lastMark = seg.el; };
    u.onend = () => { seg.el.classList.remove("tts-word"); i++; speakNext(); };
    u.onerror = () => { seg.el.classList.remove("tts-word"); i++; speakNext(); };
    tts.utter = u;
    window.speechSynthesis.speak(u);
  };
  speakNext();
}

$("#btn-listen").addEventListener("click", () => {
  if ($("#tts-bar").classList.contains("hidden")) ttsStart(); else ttsStop();
});
$("#tts-stop").addEventListener("click", ttsStop);
$("#tts-toggle").addEventListener("click", () => {
  const s = window.speechSynthesis;
  if (s.paused) { s.resume(); $("#tts-status").textContent = "朗读中"; }
  else { s.pause(); $("#tts-status").textContent = "已暂停"; }
});
$$(".tts-rate").forEach((b) => b.addEventListener("click", () => {
  tts.rate = parseFloat(b.dataset.rate);
  $$(".tts-rate").forEach((x) => x.classList.toggle("active", x === b));
  if (tts.utter) ttsStart();  // restart at new rate from current article
}));

/* ── type controls: font size / line-height / width ── */
const typeCfg = (() => {
  try {
    const raw = JSON.parse(localStorage.getItem("m.type") || "{}");
    return {
      font: Math.max(13, Math.min(24, Number(raw.font) || 17)),
      lh: Math.max(1.4, Math.min(2.4, Number(raw.lh) || 1.8)),
      width: Math.max(-1, Math.min(1, Math.trunc(Number(raw.width)) || 0)),
    };
  } catch { return { font: 17, lh: 1.8, width: 0 }; }
})();
const WIDTHS = [["紧凑", 880], ["标准", 1000], ["宽松", 1120]];
function applyType() {
  const r = $("#reader-inner");
  r.style.setProperty("--prose-font", `${typeCfg.font}px`);
  r.style.setProperty("--prose-lh", typeCfg.lh);
  // bilingual width is governed by .bilingual CSS — clear any inline override
  r.style.maxWidth = r.classList.contains("bilingual")
    ? "" : `${WIDTHS[typeCfg.width + 1][1]}px`;
  $("#type-font").textContent = typeCfg.font;
  $("#type-lh").textContent = typeCfg.lh.toFixed(1);
  $("#type-width").textContent = WIDTHS[typeCfg.width + 1][0];
  localStorage.setItem("m.type", JSON.stringify(typeCfg));
}
$("#btn-type").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#type-pop").classList.toggle("hidden");
});
$("#type-pop").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-type]");
  if (!b) return;
  const d = +b.dataset.d;
  if (b.dataset.type === "font") typeCfg.font = Math.max(13, Math.min(24, typeCfg.font + d));
  else if (b.dataset.type === "lh") typeCfg.lh = Math.max(1.4, Math.min(2.4, +(typeCfg.lh + d * 0.1).toFixed(1)));
  else typeCfg.width = Math.max(-1, Math.min(1, typeCfg.width + d));
  applyType();
});
document.addEventListener("click", (e) => {
  if (!$("#type-pop").contains(e.target) && e.target.id !== "btn-type")
    $("#type-pop").classList.add("hidden");
});

/* selection → highlight popover */
const hlPop = $("#hl-pop");
let hlSelection = "";
function showSelPopover() {
  const sel = window.getSelection();
  const text = sel?.toString().trim() || "";
  if (!text || text.length < 2 || text.length > 1000 || !S.current) {
    hlPop.classList.add("hidden");
    return;
  }
  if (!$("#reader-inner").contains(sel.anchorNode)) return;
  hlSelection = text;
  hlSelRect = sel.getRangeAt(0).getBoundingClientRect();
  hlPop.classList.remove("hidden");
  hlTrans.classList.add("hidden");
  const top = Math.max(8, hlSelRect.top - 48);
  const left = Math.min(window.innerWidth - 240,
    Math.max(8, hlSelRect.left + hlSelRect.width / 2 - 115));
  hlPop.style.top = `${top}px`;
  hlPop.style.left = `${left}px`;
}
let _selTimer;
$("#reader-scroll").addEventListener("mouseup", () => {
  clearTimeout(_selTimer);   // cancel a pending touch-path call (hybrid mouse+touch)
  setTimeout(showSelPopover, 10);
});
// touch devices don't fire mouseup on long-press selection — use selectionchange (debounced)
document.addEventListener("selectionchange", () => {
  if (!("ontouchstart" in window)) return;   // desktop already covered by mouseup
  clearTimeout(_selTimer);
  _selTimer = setTimeout(showSelPopover, 350);
});
document.addEventListener("mousedown", (e) => {
  if (!hlPop.contains(e.target)) hlPop.classList.add("hidden");
  if (!hlTrans.contains(e.target) && !hlPop.contains(e.target))
    hlTrans.classList.add("hidden");
});

const hlTrans = $("#hl-trans");
let hlSelRect = null;
$("#hl-translate").addEventListener("click", async () => {
  if (!hlSelection) return;
  const text = hlSelection;
  hlPop.classList.add("hidden");
  $("#hl-trans-src").textContent = text.length > 160 ? text.slice(0, 160) + "…" : text;
  $("#hl-trans-zh").textContent = "翻译中…";
  $("#hl-trans-zh").className = "hl-trans-zh loading";
  $("#hl-trans-note").textContent = "";
  // anchor below the selection, clamped to the viewport
  const top = Math.min(window.innerHeight - 160, (hlSelRect?.bottom || 100) + 10);
  const left = Math.min(window.innerWidth - 372,
    Math.max(8, (hlSelRect?.left || 20) - 10));
  hlTrans.style.top = `${top}px`;
  hlTrans.style.left = `${left}px`;
  hlTrans.classList.remove("hidden");
  window.getSelection()?.removeAllRanges();
  try {
    const data = await api("/api/translate-phrase", { method: "POST", body: { text } });
    $("#hl-trans-zh").textContent = data.zh || "(无翻译)";
    $("#hl-trans-zh").className = "hl-trans-zh";
    $("#hl-trans-note").textContent = data.note || "";
  } catch (err) {
    console.warn("phrase translate failed:", err);
    $("#hl-trans-zh").textContent = "翻译失败，请稍后再试";
    $("#hl-trans-zh").className = "hl-trans-zh";
  }
});
$("#hl-save").addEventListener("click", async () => {
  if (!hlSelection || !S.current) return;
  hlPop.classList.add("hidden");
  try {
    const data = await api("/api/highlights",
      { method: "POST", body: { article_id: S.current.id, text: hlSelection } });
    S.current.highlights.push({ id: data.id, text: hlSelection, note: "" });
    markText($("#reader-inner"), hlSelection);
    S.state.highlights++;
    renderNav();
    window.getSelection()?.removeAllRanges();
    toast("已高亮 ✎");
  } catch (err) { toast(`高亮失败：${esc(err.message)}`); }
});
$("#hl-copy").addEventListener("click", () => {
  navigator.clipboard?.writeText(hlSelection).then(() => toast("已复制"));
  hlPop.classList.add("hidden");
});

/* ── topbar actions ──────────────────────────────── */

function syncFilterSeg() {
  $$(".seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.filter === S.view.filter));
}
$$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  S.view.filter = b.dataset.filter;
  syncFilterSeg(); switchView();
}));

$("#btn-readall").addEventListener("click", async () => {
  const v = S.view, body = {};
  // send the FULL current filter so "mark all read" marks exactly what's shown,
  // not the whole library (monitor/tag/starred/later views used to over-mark)
  if (v.category) body.category = v.category;
  if (v.feedId) body.feed_id = v.feedId;
  if (v.filter && v.filter !== "all") body.filter = v.filter;
  if (v.tag) body.tag = v.tag;
  if (v.monitor) body.monitor = v.monitor;
  if (v.q) body.q = v.q;
  // only mark what the user has had a chance to see (newest loaded item) — compound cursor
  body.before = S.articles[0]?.published || Math.floor(Date.now() / 1000);
  body.before_id = S.articles[0]?.id || 0;
  const data = await api("/api/read-all", { method: "POST", body });
  toast(`已读 <b>${data.marked}</b> 篇`);
  viewCacheClear();
  await refreshState(false);
  loadArticles(true);
});

let refreshPoll = 0;
$("#btn-refresh").addEventListener("click", manualRefresh);
async function manualRefresh() {
  const btn = $("#btn-refresh");
  btn.classList.add("spinning");
  try { await api("/api/refresh", { method: "POST" }); } catch {}
  clearInterval(refreshPoll);
  let polls = 0;
  refreshPoll = setInterval(async () => {
    if (++polls > 48) {  // ~2 min cap so a stuck refresh can't poll forever
      clearInterval(refreshPoll);
      btn.classList.remove("spinning");
      toast("抓取仍在后台进行，稍后手动刷新查看");
      return;
    }
    const st = await api("/api/state").catch(() => null);
    if (st && !st.refreshing) {
      clearInterval(refreshPoll);
      S.state = st;
      btn.classList.remove("spinning");
      viewCacheClear();
      renderNav(); renderTags(); renderMonitors(); renderFeeds(); renderUsage();
      loadArticles(true); loadTicker();
      toast(`抓取完成，<b>+${Number(st.last_new)}</b> 篇新文章`);
    }
  }, 2500);
}

async function refreshState(reload = true) {
  try {
    S.state = await api("/api/state");
    renderNav(); renderTags(); renderMonitors(); renderFeeds(); renderUsage();
    if (reload) loadArticles(true);
  } catch {}
}

/* ── theme ───────────────────────────────────────── */

function applyTheme(theme, animate = true, origin = null) {
  S.theme = theme;
  localStorage.setItem("m.theme", theme);
  const apply = () => document.documentElement.dataset.theme = theme;
  if (animate && document.startViewTransition) {
    if (origin) {
      const r = origin.getBoundingClientRect();
      document.documentElement.style.setProperty("--rx", `${r.left + r.width / 2}px`);
      document.documentElement.style.setProperty("--ry", `${r.top + r.height / 2}px`);
    }
    document.startViewTransition(apply);
  } else apply();
}
$("#btn-theme").addEventListener("click", (e) =>
  applyTheme(S.theme === "dark" ? "light" : "dark", true, e.currentTarget));

/* broken images: CSP (script-src 'self') blocks inline onerror, so handle load
   failures centrally. The error event doesn't bubble → listen in capture phase. */
document.addEventListener("error", (e) => {
  const el = e.target;
  if (!el || el.tagName !== "IMG") return;
  if (el.classList.contains("src-favicon")) {  // no cached favicon → letter badge
    el.outerHTML = letterBadge(el.dataset.badge || "?");
    return;
  }
  const thumb = el.closest(".item-thumb");
  if (thumb) thumb.remove();        // list: drop the broken thumbnail box
  else el.style.display = "none";   // reader body: hide the broken image
}, true);

/* ── sidebar (mobile) ────────────────────────────── */

function closeSidebar() { $("#sidebar").classList.remove("open"); if (!$("#reader").classList.contains("open")) $("#scrim").classList.remove("show"); }
$("#btn-menu").addEventListener("click", () => {
  $("#sidebar").classList.add("open"); $("#scrim").classList.add("show");
});

/* ── command palette ─────────────────────────────── */

const ACTIONS = [
  { id: "refresh", label: "刷新所有订阅源", icon: "i-refresh", run: manualRefresh, kbd: "r" },
  { id: "theme", label: "切换深色 / 浅色主题", icon: "i-sun", run: () => applyTheme(S.theme === "dark" ? "light" : "dark"), kbd: "d" },
  { id: "titles", label: "开关标题中文对照", icon: "i-lang", run: () => $("#btn-titles").click(), kbd: "t" },
  { id: "readall", label: "当前视图全部标记已读", icon: "i-checkall", run: () => $("#btn-readall").click() },
  { id: "settings", label: "订阅源管理", icon: "i-settings", run: openSettings, kbd: "," },
  { id: "cat-", label: "跳转：Today 全部", icon: "i-globe", run: () => jumpCat("") },
  ...Object.keys(CAT_LABEL).map((c) => (
    { id: `cat-${c}`, label: `跳转：${CAT_LABEL[c][0]} ${CAT_LABEL[c][1]}`, icon: "i-globe", run: () => jumpCat(c) })),
];
function jumpCat(c) {
  Object.assign(S.view, { mode: "list", category: c, feedId: 0, tag: "", monitor: "", q: "" });
  if (["starred", "later"].includes(S.view.filter)) S.view.filter = "all";
  syncFilterSeg(); switchView();
}

let paletteIdx = 0, paletteItems = [], searchTimer = 0, searchSeq = 0;

function openPalette() {
  $("#palette").classList.remove("hidden");
  $("#palette-input").value = ""; $("#palette-input").focus();
  renderPalette("");
}
function closePalette() { $("#palette").classList.add("hidden"); }
$("#btn-search").addEventListener("click", openPalette);

async function renderPalette(q) {
  const seq = ++searchSeq;  // drop out-of-order search responses
  paletteIdx = 0;
  const box = $("#palette-results");
  const acts = ACTIONS.filter((a) => !q || a.label.toLowerCase().includes(q.toLowerCase()));
  let html = "";
  if (acts.length) {
    html += `<div class="p-head">命令</div>` + acts.map((a, i) => `
      <button class="p-item" data-kind="action" data-id="${a.id}">
        <svg><use href="#${a.icon}"/></svg><span class="grow">${a.label}</span>
        ${a.kbd ? `<kbd>${a.kbd}</kbd>` : ""}</button>`).join("");
  }
  if (q && q.length >= 2) {
    try {
      const data = await api(`/api/articles?q=${encodeURIComponent(q)}&limit=9`);
      if (data.items.length) {
        html += `<div class="p-head">文章</div>` + data.items.map((a) => `
          <button class="p-item" data-kind="article" data-id="${a.id}">
            <svg style="stroke:${CAT_VAR[a.category]}"><use href="#i-globe"/></svg>
            <span class="grow">${esc(a.title)}</span>
            <span class="dim">${esc(a.feed_title)}</span></button>`).join("");
      }
    } catch {}
  }
  if (seq !== searchSeq) return;
  box.innerHTML = html || `<div class="p-head">没有匹配结果</div>`;
  paletteItems = $$(".p-item", box);
  highlightPalette(0);
}

function highlightPalette(i) {
  paletteIdx = Math.max(0, Math.min(i, paletteItems.length - 1));
  paletteItems.forEach((el, j) => el.classList.toggle("active", j === paletteIdx));
  paletteItems[paletteIdx]?.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
}

function runPaletteItem(el) {
  if (!el) return;
  closePalette();
  if (el.dataset.kind === "article") openArticle(+el.dataset.id);
  else ACTIONS.find((a) => a.id === el.dataset.id)?.run();
}

$("#palette-input").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderPalette(e.target.value.trim()), 220);
});
$("#palette-results").addEventListener("click", (e) => runPaletteItem(e.target.closest(".p-item")));
$("#palette").addEventListener("click", (e) => { if (e.target.id === "palette") closePalette(); });

/* ── settings ────────────────────────────────────── */

function openSettings() {
  $("#settings").classList.remove("hidden");
  $("#feed-add-cat").innerHTML = S.state.categories.map((c) =>
    `<option value="${c}">${CAT_LABEL[c][0]} ${CAT_LABEL[c][1]}</option>`).join("");
  renderFeedList();
  renderMuteList();
  renderEngines();
}
function closeSettings() { $("#settings").classList.add("hidden"); }

const ENGINE_FEATURES = [
  { key: "digest", name: "今日简报" },
  { key: "summary", name: "AI 摘要" },
  { key: "translate", name: "翻译" },
];
function renderEngines() {
  const eng = S.state.engines || {};
  const labels = S.state.engine_labels || { deepseek: "DeepSeek", gpt55: "GPT-5.5" };
  const opts = Object.keys(labels);
  $("#engine-list").innerHTML = ENGINE_FEATURES.map((f) => `
    <div class="engine-row">
      <span class="engine-name">${f.name}</span>
      <div class="engine-seg">
        ${opts.map((o) => `<button class="eng-btn${eng[f.key] === o ? " active" : ""}" data-feat="${f.key}" data-engine="${o}">${esc(labels[o])}</button>`).join("")}
      </div>
    </div>`).join("");
}
$("#engine-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-feat]");
  if (!btn || btn.classList.contains("active")) return;
  try {
    await api("/api/settings/engine", { method: "POST",
      body: { feature: btn.dataset.feat, engine: btn.dataset.engine } });
    (S.state.engines ||= {})[btn.dataset.feat] = btn.dataset.engine;
    renderEngines();
    toast(`${esc(btn.textContent)} · 已切换`);
  } catch (err) { toast(`切换失败：${esc(err.message)}`); }
});

function renderMuteList() {
  const mutes = S.state.mutes || [];
  $("#mute-list").innerHTML = mutes.map((m) =>
    `<span class="mute-chip">${esc(m.pattern)}<button data-id="${m.id}" title="移除">✕</button></span>`).join("")
    || `<span style="font-size:12px;color:var(--text-3)">还没有屏蔽词</span>`;
}
$("#mute-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-id]");
  if (!b) return;
  await api(`/api/mutes/${b.dataset.id}`, { method: "DELETE" }).catch(() => {});
  await refreshState(false); renderMuteList(); viewCacheClear();
  if (S.view.mode === "list") loadArticles(true);
});
$("#mute-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#mute-add-input").value.trim();
  if (v.length < 2) return;
  try {
    await api("/api/mutes", { method: "POST", body: { pattern: v } });
    $("#mute-add-input").value = "";
    await refreshState(false); renderMuteList(); viewCacheClear();
    if (S.view.mode === "list") loadArticles(true);
    toast(`已屏蔽 <b>${esc(v)}</b>`);
  } catch (err) { toast(`屏蔽失败：${esc(err.message)}`); }
});
$("#btn-opml-import")?.addEventListener("click", async () => {
  const ta = $("#opml-input");
  if (ta.classList.contains("hidden")) { ta.classList.remove("hidden"); ta.focus(); return; }
  const opml = ta.value.trim();
  if (!opml) { ta.classList.add("hidden"); return; }
  try {
    const r = await api("/api/opml/import", { method: "POST", body: { opml } });
    toast(`已导入 <b>${r.imported}</b> 个源（共 ${r.seen} 个，抓取中…）`);
    ta.value = ""; ta.classList.add("hidden");
    setTimeout(async () => { await refreshState(false); renderFeedList(); renderFeeds(); }, 1500);
  } catch (e) { toast(`导入失败：${esc(e.message)}`); }
});
$("#btn-settings").addEventListener("click", openSettings);
$("#btn-add-feed").addEventListener("click", openSettings);
$("#btn-close-settings").addEventListener("click", closeSettings);
$("#settings").addEventListener("click", (e) => { if (e.target.id === "settings") closeSettings(); });

function renderFeedList() {
  let html = "";
  for (const cat of S.state.categories) {
    const feeds = S.state.feeds.filter((f) => f.category === cat);
    if (!feeds.length) continue;
    html += `<div class="feed-group-label" style="--cat:${CAT_VAR[cat]}">${CAT_LABEL[cat][0]} · ${CAT_LABEL[cat][1]}</div>`;
    html += feeds.map((f) => `
      <div class="feed-row" data-id="${f.id}">
        <button class="switch ${f.enabled ? "on" : ""}" data-act="toggle" title="${f.enabled ? "停用" : "启用"}"></button>
        <div class="feed-row-main">
          <div class="feed-row-title">${esc(f.title || "(未命名)")}</div>
          <div class="feed-row-url">${esc(f.url)}</div>
          ${f.error_count > 0 ? `<div class="feed-row-err">⚠ 连续失败 ${f.error_count} 次${f.last_error ? `：${esc(f.last_error.slice(0, 80))}` : ""}</div>` : ""}
        </div>
        <button class="icon-btn sm" data-act="del" title="删除"><svg><use href="#i-trash"/></svg></button>
      </div>`).join("");
  }
  $("#feed-list").innerHTML = html;
}

$("#feed-list").addEventListener("click", async (e) => {
  const row = e.target.closest(".feed-row");
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!row || !act) return;
  const id = +row.dataset.id;
  const feed = S.state.feeds.find((f) => f.id === id);
  if (act === "toggle") {
    await api(`/api/feeds/${id}`, { method: "PATCH", body: { enabled: !feed.enabled } });
    feed.enabled = !feed.enabled;
    renderFeedList(); renderFeeds();
  } else if (act === "del") {
    if (!confirm(`删除订阅源「${feed.title}」？文章也会一并删除。`)) return;
    await api(`/api/feeds/${id}`, { method: "DELETE" });
    S.state.feeds = S.state.feeds.filter((f) => f.id !== id);
    renderFeedList(); renderFeeds();
    toast("已删除订阅源");
  }
});

$("#feed-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#feed-add-url").value.trim();
  const category = $("#feed-add-cat").value;
  if (!url) return;
  const btn = $("#feed-add-form button");
  btn.disabled = true; btn.textContent = "发现源…";
  try {
    // any URL works: feed URLs verify instantly, site URLs get auto-discovery
    const found = await api("/api/feeds/discover", { method: "POST", body: { url } });
    if (!found.candidates.length) {
      toast("该网址下没有发现可用的 RSS 源");
      return;
    }
    const target = found.candidates[0];
    if (target.url !== url)
      toast(`自动发现 feed：${esc(target.title || target.url.slice(0, 50))}`);
    btn.textContent = "添加中…";
    const data = await api("/api/feeds", { method: "POST",
      body: { url: target.url, category, title: target.title || "" } });
    $("#feed-add-url").value = "";
    await refreshState(false);
    renderFeedList();
    if (data.feed.last_error) toast(`已添加，但抓取报错：${esc(data.feed.last_error.slice(0, 60))}`);
    else toast(`已添加 <b>${esc(data.feed.title || url)}</b>，抓到 ${data.new_articles} 篇`);
  } catch (err) { toast(`添加失败：${esc(err.message)}`); }
  finally { btn.disabled = false; btn.textContent = "添加"; }
});

$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
});

/* ── keyboard ────────────────────────────────────── */

function moveSelection(delta) {
  const items = $$("#list .item");
  if (!items.length) return;
  S.selectedIdx = Math.max(0, Math.min(items.length - 1, S.selectedIdx + delta));
  items.forEach((el, i) => el.style.background =
    i === S.selectedIdx ? "var(--active-bg)" : "");
  items[S.selectedIdx].scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
}

document.addEventListener("keydown", (e) => {
  const inInput = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    $("#palette").classList.contains("hidden") ? openPalette() : closePalette();
    return;
  }
  if (!$("#palette").classList.contains("hidden")) {
    if (e.key === "Escape") closePalette();
    else if (e.key === "ArrowDown") { e.preventDefault(); highlightPalette(paletteIdx + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlightPalette(paletteIdx - 1); }
    else if (e.key === "Enter") { e.preventDefault(); runPaletteItem(paletteItems[paletteIdx]); }
    return;
  }
  if (e.key === "Escape") {
    if (!$("#settings").classList.contains("hidden")) closeSettings();
    else if ($("#reader").classList.contains("open")) closeReader();
    else closeSidebar();
    return;
  }
  if (inInput) return;
  const readerOpen = $("#reader").classList.contains("open");
  switch (e.key) {
    case "j": readerOpen ? navigateArticle(1) : moveSelection(1); break;
    case "k": readerOpen ? navigateArticle(-1) : moveSelection(-1); break;
    case "o": case "Enter": {
      const el = $$("#list .item")[S.selectedIdx];
      if (el) openArticle(+el.dataset.id);
      break;
    }
    case "s": if (S.current) $("#btn-star").click(); break;
    case "l": if (S.current) $("#btn-later").click(); break;
    case "b": if (S.current) $("#btn-translate").click(); break;
    case "p": if (S.current) $("#btn-listen").click(); break;
    case "r": manualRefresh(); break;
    case "t": $("#btn-titles").click(); break;
    case "d": applyTheme(S.theme === "dark" ? "light" : "dark"); break;
    case ",": openSettings(); break;
  }
});

/* ── periodic state sync (unread badges stay fresh) ─ */
setInterval(() => {
  if (!$("#app").classList.contains("hidden") && document.visibilityState === "visible")
    refreshState(false);
}, 120000);

/* ── go ──────────────────────────────────────────── */
$("#btn-titles").classList.toggle("on", S.titleTrans);
boot();
