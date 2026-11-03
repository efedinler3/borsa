# Piyasa Masası — architecture contract

Desktop app (Windows) for a Turkish user who day-trades US stocks. It collects US market news from many
sources **and shows each source separately** ("kim ne diyor"), tags/scores everything locally, surfaces
opportunities, shows a calendar and market dashboard, and writes morning / midday / evening briefs.
Later a day-trade bot will read the same SQLite DB, so data must be clean and well-structured.

**No paid LLM API.** Analysis is local and rule-based by default. Optional add-ons:
FinBERT (local, `transformers`), Ollama (local LLM at `cfg["ollama_url"]`), and Claude Code
(the user's Claude subscription, via a scheduled Claude Code task that reads an exported Markdown file and
writes a brief back to a folder — see "Claude Code bridge").

UI language: **Turkish**. Code identifiers/comments: English. Python 3.14 on Windows, run with `py`.
Installed: flask, feedparser, requests, tzdata, pywebview (EdgeChromium). No numpy/pandas/torch by default —
do not require them. Pure stdlib + those packages only.

## Layout (file ownership)

```
trade/
  SPEC.md                         (this file — read only)
  requirements.txt, run.bat, README.md, CLAUDE_BRIEF.md
  marketdesk/
    __init__.py, util.py, config.py, db.py      CORE — already written, read-only for module authors
    collectors/__init__.py, collectors/base.py  CORE — already written
    collectors/rss.py           RSS/Atom feeds (media, press releases, aggregators, per-ticker feeds)
    collectors/sec.py           SEC EDGAR 8-K / Form 4 / S-1 etc. (needs cfg sec_user_agent)
    collectors/calendar.py      Economic calendar (ForexFactory JSON) + earnings calendar (Nasdaq API)
    collectors/alpaca.py        Alpaca news + movers/most-actives + watchlist snapshots (needs keys)
    collectors/finnhub.py       Finnhub general/company news + earnings calendar (needs key)
    collectors/market.py        Quotes (Yahoo chart API), movers (Yahoo screener), Fear & Greed (CNN)
    collectors/social.py        ApeWisdom (Reddit mentions), StockTwits trending, Reddit RSS posts
    analysis/__init__.py        enrich() entry point
    analysis/tickers.py         symbol universe + ticker extraction
    analysis/classify.py        CATEGORIES, category tagging, lexicon sentiment, importance
    analysis/cluster.py         cross-source story clustering ("kim ne diyor")
    analysis/opportunities.py   per-ticker opportunity scoring
    analysis/ml.py              optional FinBERT + Ollama helpers
    briefs.py                   rule-based Turkish briefs, Ollama briefs, Claude Code bridge
    scheduler.py                background loop: collectors, analysis, briefs, maintenance
    server.py                   Flask JSON API + static web/
    main.py                     launcher: server thread + desktop window
  web/index.html, web/app.css, web/app.js        single-page UI (vanilla JS, no build step, no CDN)
```

Never edit files you do not own. If a core change is needed, describe it in your final report instead.

## Core API (already implemented — read the files)

- `util`: `DATA_DIR` (outside OneDrive: `%LOCALAPPDATA%\PiyasaMasasi`, overridable with env
  `MARKETDESK_DATA`), `CLAUDE_IN_DIR`, `CLAUDE_OUT_DIR`, `WEB_DIR`, `ET`, `TR`, `http_get()`, `http_json()`
  (browser UA, 1 retry, raises on HTTP error), `now_ts()`, `make_id()`, `normalize_url()`, `to_ts()`,
  `fmt_et()`, `fmt_tr()`, `market_session()`, `last_close_ts()`, `is_trading_day()`, `close_minutes()`,
  `clean_html()`, `jdump()/jload()`, `setup_logging(level, file=True)` (rotating `marketdesk.log`, 5 MB × 3),
  `friendly_error(exc)` (short Turkish error text; `scheduler.friendly_error` re-exports it), `FOMC_DECISIONS`.
  NYSE holidays/early closes: explicit table for 2026–2027, rule-based for other years (warning logged once).
- `config`: `load_config()`, `save_config(updates, fn=None)` (one locked read-modify-write, atomic file write),
  `set_source_enabled(key, enabled)`, `public_config()`, `DEFAULTS`, `SECRET_KEYS`.
- `db`: `init_db()` (also adds columns introduced later, e.g. `source_status.last_warning`),
  `upsert_news(items) -> new_ids` (keeps the `tickers` order enrich produced: `tickers[0]` is the subject),
  `update_news_fields([(id, {...})])`, `query_news(...)`,
  `recent_news(since_ts)`, `count_news()`, `news_source_counts()`, `upsert_events()`,
  `delete_events(source, start_ts, end_ts, keep_ids, kind=None, status=None)`, `query_events()`,
  `set_snapshot()/get_snapshot()/get_snapshot_data()`, `save_brief()/list_briefs()/brief_exists()`,
  `update_source_status(source, name, ok, items=0, error=None, warning=None)/list_source_status()`,
  `append_signals(items, ts, session)`,
  `query_signals(since, until, ticker, min_score, limit)`, `SIGNALS_SCHEMA_VERSION`, `prune()`.
  `news_catalyst` (id, published_at, publisher, tickers, categories, sentiment, sentiment_label, importance,
  title ≤120 chars) is written with every new news row and kept 365 days, while `news` is pruned after 21 days.
  `stale_news(analysis_version, limit)` lists rows whose `extra.analysis_version` differs; the scheduler's
  reanalyze pass re-enriches them once `analysis.ANALYSIS_VERSION` exists (stamped as a string).
  Every write helper runs in one transaction (`db._tx()`): commit on success, rollback on any error.
- `collectors.base.Collector`: attributes `key, name, group, kind, source_type, interval, requires,
  description`; `fetch(cfg) -> {"news": [...], "events": [...], "snapshots": {key: data}}`; helper
  `self.item(title, url, published_at, summary, tickers, author, extra)` builds a news dict.
  `collectors.all_collectors()` gathers `COLLECTORS` lists from each module.

### Collector rules
- `fetch` must be self-contained, finish in < 60 s, raise on total failure (scheduler logs it),
  and skip individual malformed entries silently. Return at most ~100 news items per call.
- Partial failure (some feeds / pages / days failed, the rest worked): return the data plus
  `"warnings": ["Kısmi hata — ..."]` (short Turkish strings). The scheduler keeps the source OK, stores the joined
  text in its job state and `source_status.last_warning` (never in `last_error`), and `/api/sources` reports
  `state: "partial"` ("Kısmi hata") with `warning`.
- `extra.publisher` is always a normalized outlet name (`collectors.rss.normalize_publisher`: 'The Motley Fool' ->
  'Motley Fool', 'Yahoo! Finance Canada' -> 'Yahoo Finance'); `collectors.rss.canonical_publisher(item)` gives the
  outlet for counting independent sources (extra.publisher / provider -> known-outlet author -> known URL domain ->
  source_name). Quote / option-chain pages (`analysis.classify.is_junk_title`) are dropped at ingest.
- `published_at` is unix seconds UTC (use `util.to_ts`). Drop items older than 3 days.
- `summary` is plain text (use `util.clean_html`, ≤ 600 chars).
- If the source itself tags symbols (Alpaca, Finnhub, SEC filer ticker, `(NASDAQ: XYZ)` in a press
  release), put them in `tickers`. Analysis adds more later.
- Requirements gating: put config keys in `requires`; the scheduler will not call `fetch` until set.
- Keep request rates polite (SEC: ≤ 10 req/s and must send `cfg["sec_user_agent"]` as User-Agent).
- Watchlist-driven collectors read `cfg["watchlist"]` at fetch time.
- Calendar collectors may return `"event_windows": [{"start": ts, "end": ts, "kind"?: str, "status"?: str,
  "keep_ids"?: [...]}]` next to `"events"`: one window per day/range that was fetched **successfully and
  returned rows**. After the upsert the scheduler deletes this source's rows with `start <= ts < end` (and that
  `kind` / `extra.status`) whose id is not in `keep_ids` (default: the ids of all returned events), so a moved
  earnings date or a withdrawn IPO disappears. A window containing none of the returned events is ignored.
  Never send a window for a failed, empty or cached-fallback response.

## Analysis contract

`analysis.enrich(items: list[dict], cfg: dict) -> list[dict]` — called by the scheduler on new items
before `db.upsert_news`. Fills/merges: `tickers` (list of US symbols), `categories` (list of keys below),
`sentiment` (-1..1), `sentiment_label` ("pos" | "neg" | "neu"), `importance` (0..100). Must be fast
(> 500 items/s), pure Python, deterministic. Optional FinBERT only when `cfg["use_finbert"]` and installed.

`enrich` also stores attribution data in `extra` (recomputed for older rows by the scorer via `derive_meta`):
`title_tickers` (title symbols in order), `subject_tickers` (the story's subject: first non-ETF title ticker plus
companies listed with it), `summary_tickers`, `title_categories`, `summary_categories` ({category: tickers of the
sentence that triggered it}), `roundup` (market wrap / digest / movers list), `<category>_outcome`
(earnings `beat|miss|mixed|preview`, fda `approve|reject`, dividend `raise|cut`, insider `buy|sell|mixed`,
clinical `positive|negative`, delisting `compliance`, index_change `removed`) and `junk` (quote / option-chain page:
no categories, importance <= 3, skipped by clustering and the scorer). Public helpers:
`tickers.extract_spans`, `tickers.mention_span`, `tickers.extract(text, watch, social=)`, `classify.is_junk_title`,
`classify.is_roundup`, `classify.outcomes`, `classify.features`, `cluster.publisher_key`, `cluster.publisher_name`,
`cluster.voices`, `cluster.cluster_sizes`, `analysis.clean_summary`, `analysis.title_subjects`, `analysis.derive_meta`.

`analysis.classify.CATEGORIES: dict[key, {"label": TR label, "bias": +1|-1|0, "weight": float, "color": css}]`.
Keys (at minimum): `earnings, guidance_up, guidance_down, analyst_up, analyst_down, mna, fda, clinical,
offering, buyback, dividend, insider, contract, lawsuit, bankruptcy, layoffs, macro, fed, ipo, halt,
short_report, split, filing, product, crypto, geopolitics`. The UI renders labels/colors from
`GET /api/meta`, so new keys are fine.

`analysis.cluster.assign_clusters(since_hours: int = 48) -> int` — groups the same story across
sources (title similarity + shared tickers + time window), writes `cluster_id` via
`db.update_news_fields`. `cluster_id` = id of the earliest item in the cluster. Singletons get their own id.

`analysis.cluster.list_clusters(since_hours=24, min_sources=2, limit=100) -> list[dict]` →
`{cluster_id, title, tickers, categories, first_seen, last_seen, sentiment_avg, importance_max,
source_count, collector_count, item_count, labels: {pos, neg, neu}, agreement, items: [news...] }` sorted by
importance/recency. `source_count` = independent voices (`cluster.voices`: one per publisher, syndicated identical
titles once; `min_sources` applies to it), `collector_count` = app feeds that carried the story, `items` = one row
per voice (the direct collector's copy, else the earliest) with `publisher` (display name) and `collectors`
(source names of every feed that carried it). `cluster.cluster_sizes(ids) -> {cluster_id: (rows, voices)}`.

`analysis.opportunities.compute(cfg) -> dict` → snapshot stored at key `opportunities`:
```
{"ts": int, "window_start": int,
 "items": [{"ticker": "NVDA", "score": 0..100, "direction": "long"|"short"|"mixed",
            "reasons": ["FDA onayı (3 kaynak)", ...Turkish strings],
            "categories": [...], "sources": ["CNBC", ...], "news_ids": [...], "news_count": int,
            "sentiment_avg": float, "social_rank": int|null, "social_change": int|null,
            "price": float|null, "change_pct": float|null, "volume": int|null,
            "in_watchlist": bool, "earnings_today": bool,
            # bot fields; rel_volume / first_news_ts / reason_codes are copied into `signals`:
            "rel_volume": float|null, "first_news_ts": int|null, "last_news_ts": int|null,
            "move_label": "premarket"|"open"|"afterhours"|"last_session"|null  (what change_pct describes),
            "reason_codes": [category keys + "gap" (|move| >= 3), "rvol" (>= 2x volume), "earnings_today",
                             "earnings_next", "earnings_surprise" (|EPS surprise| >= 5 %), "halt", "social",
                             "watchlist"]}]}
```
`sources` are publisher names (one per independent voice, so 'N kaynak' / 'N farklı kaynakta haber' count outlets,
not collectors). Reasons include official halts ('İşlem durduruldu (haber bekleniyor)' for nasdaq_halts codes
T1/T12/H4/H9/H10/H11) and reported EPS surprises of the previous trading day / today ('EPS beklentiyi %12 aştı';
a score bump, never a direction). Prices also come from `movers.quotes_extra`.
After each compute the scheduler appends rows to the `signals` table (every ticker every 10 min while a session
is on, hourly while closed; in between only tickers that are new, moved ≥ 8 points or flipped direction).
Inputs: news since `util.last_close_ts()` minus some hours (and last 24 h fallback), categories,
multi-source breadth, sentiment, social snapshot, movers/quotes snapshots, today's earnings events.

`analysis.tickers.refresh_symbols(cfg) -> int` downloads the symbol universe (nasdaqtrader.com
`nasdaqlisted.txt` + `otherlisted.txt`) into `DATA_DIR/symbols.json` (cached, refresh if > 7 days).
`analysis.tickers.extract(text) -> list[str]`. `analysis.tickers.company_name(sym) -> str|None`.

`analysis.ml`: `finbert_available()`, `finbert_scores(texts)`, `ollama_available(cfg) -> bool`,
`ollama_models(cfg) -> list[str]`, `ollama_generate(prompt, cfg, system=None, timeout=120) -> str|None`.

## Snapshot keys (written by collectors via `"snapshots"`)

- `quotes`: `{"ts", "items": [{"symbol", "name" (TR label), "group": "vadeli"|"endeks"|"emtia"|"faiz"|"doviz"|"kripto"|"izleme", "price", "prev_close", "change", "change_pct", "spark": [floats, ≤ 80 pts], "market_state"}]}`
  (futures ES=F NQ=F YM=F RTY=F, ^GSPC ^IXIC ^DJI ^RUT, ^VIX, DX-Y.NYB, ^TNX, CL=F, GC=F, BTC-USD, + watchlist)
- `movers`: `{"ts", "source": "yahoo"|"alpaca", "gainers": [...], "losers": [...], "actives": [...],
  "small_caps": [...], "premarket": [...], "postmarket": [...], "ext_ts", "quotes_extra": {SYM: row}}`;
  each row `{"symbol", "name", "price", "change_pct", "volume", "avg_volume"?, "rel_volume"?, "market_cap"?,
  "pre_change_pct"?, "post_change_pct"?}`. `premarket` / `postmarket` (extended-hours gappers, time `ext_ts`) carry
  the extended-hours move in `price` / `change_pct` and the regular session in `regular_change_pct` /
  `regular_price`. `quotes_extra` = one v7 quote per followed symbol that no screener lists (app spelling, e.g.
  `BRK.B`; not a mover list). Field names are stable (briefs and the scorer read them).
- `movers_alpaca`: same shape from Alpaca (when keys set).
- `fear_greed`: `{"ts", "score", "rating", "previous_close", "week_ago", "month_ago"}`
- `social`: `{"ts", "apewisdom": [{"symbol", "name", "rank", "rank_24h_ago", "mentions", "mentions_24h_ago", "upvotes"}],
  "stocktwits": [{"symbol", "title", "watchlist_count"?}]}`
- `halts` (collector `nasdaq_halts`): `{"ts", "source": "nasdaqtrader"|"nyse", "items": [{"symbol", "name", "market",
  "reason_code", "reason_tr", "halt_ts", "resume_ts", "resume_quote_ts"}] (halts not resumed yet, newest first),
  "luld_today": {SYM: LULD pauses today}, "total"}`.
- `movers_why` (written by the scheduler every 2 min, not a collector): `{"ts", "since", "items": {SYM: {"title",
  "url", "source_name", "publisher", "published_at", "categories" (≤ 2), "sentiment_label", "source_count",
  "news_id"}}}` — the catalyst story per symbol of the movers lists (`briefs.group_stories` + `briefs.why_for`,
  public aliases of `_group_stories` / `_why(p, sym, name, chg)`).
- Collectors may add more keys; document them in the final report.

News sources with special items: `movers_news` (Yahoo headlines of the day's movers / large earnings reporters,
`extra.mover_symbol`) and `nasdaq_halts` (official halts: explicit ids, Turkish titles, `categories: ["halt"]`,
`extra.reason_code`, `reason_tr`, `halt_ts`, `resume_ts`, `resume_quote_ts`, `market`, `issue_name`; resumption
items have `extra.resumed`, LULD pause items `pause_index`, `pause_count`, `pause_times`).

## Calendar events (`"events"`)
`{"kind": "macro"|"earnings"|"ipo", "title", "country", "ts", "all_day", "impact": "high"|"medium"|"low"|"",
"forecast", "previous", "actual", "ticker", "source", "extra": {...}}`. For earnings put
`extra.time` = `"bmo"` (before open) | `"amc"` (after close) | `"unknown"`, `extra.eps_forecast`,
`extra.company`, `extra.market_cap`, and once reported `extra.eps_actual`, `extra.surprise_pct` (Nasdaq keeps the
previous trading day for that). Source `nasdaq_econ` = provisional USD macro events for days ForexFactory has not
published yet (`extra.provisional = true`, `extra.note_tr`); `ff_calendar` deletes them once FF covers the day.
IPO rows: `extra.status` `upcoming|priced`, `status_tr`, `price`, `shares`, `offer_amount`, `exchange`. Titles for macro events stay as provided (English) plus
`extra.title_tr` with a Turkish translation for common ones (CPI → "TÜFE (Enflasyon)", Non-Farm
Payrolls → "Tarım Dışı İstihdam", FOMC → "FOMC Faiz Kararı", ...).

## Briefs

`briefs.generate(kind, cfg) -> dict` (kind = `sabah` | `ogle` | `aksam` | `manual`): rule-based Turkish
Markdown brief from DB data, saved with author `kural`. `briefs.generate_ollama(kind, cfg)` (author
`ollama`) when Ollama is available. Brief times come from `cfg["brief_times_et"]` (ET; skip non-trading days
except a weekend `manual`).

Content: market tone (futures/VIX/Fear&Greed), top stories by importance with source names, "kim ne
diyor" for the biggest multi-source stories, opportunities top 10 with reasons, today's high-impact
macro events and notable earnings (bmo/amc), watchlist news, social buzz, and a fixed disclaimer
("Yatırım tavsiyesi değildir"). The morning layout adds "Dünkü bilanço sonuçları" (previous session's biggest
|EPS surprise| x market cap). Earnings-call transcripts are never stories or clusters.

### Claude Code bridge
`briefs.export_for_claude(kind) -> Path` writes `CLAUDE_IN_DIR/<YYYY-MM-DD>_<kind>.md` and
`CLAUDE_IN_DIR/latest.md` (compact data pack: top ~60 stories with source/time/tickers/tags/url,
clusters, opportunities, calendar, quotes). `briefs.import_claude_outputs() -> int` scans
`CLAUDE_OUT_DIR/*.md` and saves new files as briefs with author `claude` (id = make_id(filename)).
`CLAUDE_BRIEF.md` (repo root) is the instruction prompt a scheduled Claude Code task follows: run
`py -m marketdesk.export <kind>`, read `latest.md`, write a Turkish analysis to `CLAUDE_OUT_DIR`.
`marketdesk/export.py` (owned by the briefs author) is the CLI for that.

## HTTP API (Flask, `127.0.0.1:<cfg.port>`), all JSON

| Method | Path | Response |
|---|---|---|
| GET | `/api/status` | `{now, et_time, tr_time, session, session_label, next_open_ts, next_close_ts, session_end_ts (end of the current premarket/open/afterhours part; 17:00 ET after-hours end on early-close days; null when closed), counts: {news_24h, news_total, sources_ok, sources_err, sources_idle, sources_total, sources_disabled, source_groups: {group: {err, total}}}, ml: {finbert, ollama}, version, next_briefs: [{kind, ts}], instance: {pid, headless, can_shutdown}}` |
| GET | `/api/meta` | `{categories: CATEGORIES, groups: GROUPS, source_types: {key: TR label}, sentiments: {pos, neg, neu}}` |
| GET | `/api/news` | params `q, source (comma list), source_type, category, ticker, sentiment, since_hours (default 24), after_fetched, min_importance, order, limit (≤500), offset` → `{items, total}`; rows carry `cluster_sources` = `cluster_size` (independent voices of the story, "+N kaynak") and `cluster_items` |
| GET | `/api/news/by_source` | params `since_hours, per_source (default 15), group` → `{sources: [{source, source_name, group, source_type, items: [...]}]}` (TweetDeck-style columns) |
| GET | `/api/clusters` | params `since_hours, min_sources` → `{items: list_clusters(...)}` |
| GET | `/api/ticker/<sym>` | `{ticker, company, news, by_source: {source_name: [news]}, events, quote, quote_source, social, opportunity, sentiment_avg, news_count, source_count (independent voices), in_watchlist}` |
| GET | `/api/opportunities` | snapshot `opportunities` (empty `{items: []}` if none) |
| GET | `/api/market` | `{quotes, movers, movers_alpaca, fear_greed, social, alpaca_snapshots, halts, updated: {key: ts}}`; every movers row gets `catalyst` (a `movers_why` item or `null`) once that snapshot exists |
| GET | `/api/signals` | params `since` (exclusive; oldest first), `until`, `ticker`, `min_score`, `limit (≤5000)` → `{schema_version, items: [signals rows], now}` |
| GET | `/api/calendar` | params `days (default 7), kind, impact, ticker, prev (0/1)` → `{items, start, end}` (from start of today ET; `prev=1` starts at the previous trading day, whose earnings carry `eps_actual` / `surprise_pct`) |
| GET | `/api/briefs` | params `limit, kind, author` → `{items}` |
| POST | `/api/briefs/generate` | body `{kind, engine: "kural"|"ollama"}` → brief |
| POST | `/api/briefs/claude_export` | body `{kind}` → `{path}` |
| GET | `/api/sources` | `{items: [collector.info(cfg) + status fields last_run,last_ok,last_error,items_last,items_total, news_24h, warning, state (ok, partial, error, idle, disabled, needs_config, running), state_label]}` |
| POST | `/api/sources/toggle` | body `{source, enabled}` |
| POST | `/api/refresh` | body `{source?}` → `{queued: [...]}` (runs in background) |
| GET | `/api/alerts` | params `since` (fetched_at ts) → `{items: news with importance ≥ cfg.alert_min_importance, or watchlist tickers with importance ≥ max(40, threshold − 30) or a strong tag (earnings, guidance_*, analyst_*, mna, fda, clinical, offering, halt, bankruptcy), now}` |
| GET/POST | `/api/config` | `public_config()`; POST merges with `save_config` |
| POST | `/api/shutdown` | JSON body `{}` → `{ok, message}`; stops the whole app (collection included), e.g. the background instance. 503 when the server was created without a shutdown callback |

Errors: `{"error": "..."}` with 4xx/5xx. The UI polls (no websockets): status 15 s, news 30 s, alerts 20 s,
market 60 s.

## UI

Dark "trading terminal" look, dense but readable, Turkish everywhere, times shown in **TR time** with ET in
tooltips/secondary text. Left sidebar (inline SVG icons, labels):
1. **Haberler** — live feed: filters (search, source chips by group, category, sentiment, ticker,
   importance slider, time window); each row: time, source badge, title (opens link in external browser),
   ticker chips (click → ticker view), category tags (TR labels/colors), sentiment dot, importance bar,
   "+N kaynak" when clustered. Toggle "Sütun görünümü" = one column per source (by_source endpoint).
2. **Kim Ne Diyor** — multi-source story clusters: headline + each source's own title/time/sentiment side
   by side; sentiment agreement meter.
3. **Fırsatlar** — opportunity cards sorted by score: ticker, direction badge (Yükseliş/Düşüş/Karışık),
   score bar, price/change, reasons (Turkish), sources, linked news; filters (yön, min skor, sadece izleme listesi).
4. **Piyasa Panosu** ("işimize yarayanlar") — session clock (ET+TR, countdown to open/close), futures &
   indices tiles with sparkline, VIX, DXY, 10Y, oil, gold, BTC, Fear & Greed gauge, top gainers / losers /
   actives tables (+ pre/after-market gappers, a "Neden?" catalyst column from `movers_why`, hidden below 900 px),
   "İşlem durdurmaları" (snapshot `halts`), Reddit/StockTwits buzz table.
5. **Takvim** — today + next days: macro events (impact color, TR title, forecast/previous/actual) and
   earnings (BMO/AMC, EPS forecast); filter by impact; watchlist earnings highlighted.
6. **Brifingler** — list of briefs (sabah/öğle/akşam, author badge: Kural / Ollama / Claude), Markdown
   rendered (small built-in renderer), buttons: "Şimdi oluştur", "Claude için dışa aktar".
7. **Hisse** — ticker detail (search box): all news grouped by source ("kim ne diyor" for one ticker),
   quote, events, social, opportunity.
8. **Kaynaklar & Ayarlar** — source table (group, status OK/Kısmi hata/hata/bekliyor, last run, items, error text,
   enable toggle, "Şimdi çek"), API keys (masked), SEC e-posta, watchlist editor, brief times, alert
   threshold, Ollama/FinBERT status, data folder path.

Global: top bar with session badge (Piyasa öncesi / Açık / Piyasa sonrası / Kapalı), clock, last update,
manual refresh; toast notifications for `/api/alerts` (+ optional browser Notification); keyboard
shortcuts 1–8 for tabs, `/` focuses search. No external network calls from the frontend (no CDN fonts or
libs). External links open via `target="_blank"` (pywebview opens them in the system browser).
