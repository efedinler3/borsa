"""SQLite storage. One connection per thread, WAL mode, a global lock around writes."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .util import DATA_DIR, jdump, jload, make_id, normalize_url, now_ts

DB_PATH = DATA_DIR / "marketdesk.db"

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    url TEXT DEFAULT '',
    author TEXT DEFAULT '',
    published_at INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    tickers TEXT DEFAULT '[]',
    categories TEXT DEFAULT '[]',
    sentiment REAL DEFAULT 0,
    sentiment_label TEXT DEFAULT 'neu',
    importance REAL DEFAULT 0,
    cluster_id TEXT DEFAULT NULL,
    extra TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_news_pub ON news(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_fetched ON news(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_source ON news(source);
CREATE INDEX IF NOT EXISTS idx_news_cluster ON news(cluster_id);
-- upsert_news title dedup (source + title, case-insensitive) must not scan a whole source.
CREATE INDEX IF NOT EXISTS idx_news_source_title ON news(source, title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    country TEXT DEFAULT '',
    ts INTEGER NOT NULL,
    all_day INTEGER DEFAULT 0,
    impact TEXT DEFAULT '',
    forecast TEXT DEFAULT '',
    previous TEXT DEFAULT '',
    actual TEXT DEFAULT '',
    ticker TEXT DEFAULT '',
    source TEXT DEFAULT '',
    extra TEXT DEFAULT '{}',
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON calendar_events(ts);

CREATE TABLE IF NOT EXISTS snapshots (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_briefs_created ON briefs(created_at DESC);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    source_name TEXT,
    last_run INTEGER,
    last_ok INTEGER,
    last_error TEXT,
    items_last INTEGER DEFAULT 0,
    items_total INTEGER DEFAULT 0
);

-- History of opportunity scores for the day-trade bot (the `opportunities` snapshot is overwritten).
CREATE TABLE IF NOT EXISTS signals (
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    score REAL NOT NULL,
    direction TEXT NOT NULL,
    session TEXT DEFAULT '',
    price REAL,
    change_pct REAL,
    rel_volume REAL,
    volume INTEGER,
    categories TEXT DEFAULT '[]',
    reason_codes TEXT DEFAULT '[]',
    news_ids TEXT DEFAULT '[]',
    news_count INTEGER DEFAULT 0,
    n_sources INTEGER DEFAULT 0,
    first_news_ts INTEGER,
    earnings INTEGER DEFAULT 0,
    in_watchlist INTEGER DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ts, ticker)
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker, ts);

-- Compact catalyst history kept 365 days (full news text is pruned after 21): what headline preceded a move.
CREATE TABLE IF NOT EXISTS news_catalyst (
    id TEXT PRIMARY KEY,
    published_at INTEGER NOT NULL,
    publisher TEXT DEFAULT '',
    tickers TEXT DEFAULT '[]',
    categories TEXT DEFAULT '[]',
    sentiment REAL DEFAULT 0,
    sentiment_label TEXT DEFAULT 'neu',
    importance REAL DEFAULT 0,
    title TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_catalyst_pub ON news_catalyst(published_at);

-- Signal scorecard (marketdesk/scorecard.py): what the price did after each `signals` row, from Yahoo 5-minute bars.
-- Returns are % in the signal's direction (short = price fall is positive); spy_* is the same window for SPY.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    ts INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_day TEXT,
    entry_kind TEXT,
    entry_ts INTEGER,
    entry_price REAL,
    ret_30m REAL,
    ret_60m REAL,
    ret_close REAL,
    mfe REAL,
    mae REAL,
    spy_ret_close REAL,
    bot_result TEXT,
    bot_ret REAL,
    tp_pct REAL,
    sl_pct REAL,
    evaluated_at INTEGER NOT NULL,
    PRIMARY KEY (ts, ticker)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_day ON signal_outcomes(entry_day);

-- Paper trading (marketdesk/paper.py): simulated account, never sent to a broker.
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    start_cash REAL NOT NULL,
    created_at INTEGER NOT NULL,
    day_date TEXT DEFAULT '',
    day_start_equity REAL,
    halted_date TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    type TEXT NOT NULL,
    limit_price REAL,
    stop_price REAL,
    tif TEXT DEFAULT 'day',
    ext_hours INTEGER DEFAULT 0,
    expires_at INTEGER,
    status TEXT NOT NULL,
    filled_at INTEGER,
    fill_price REAL,
    fee REAL DEFAULT 0,
    realized_pnl REAL,
    origin TEXT DEFAULT 'manual',
    note TEXT DEFAULT '',
    stop_loss_pct REAL,
    take_profit_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_orders_filled ON paper_orders(filled_at);
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,
    opened_at INTEGER NOT NULL,
    stop_price REAL,
    target_price REAL,
    origin TEXT DEFAULT 'manual',
    last_price REAL,
    last_ts INTEGER
);
CREATE TABLE IF NOT EXISTS paper_equity (
    ts INTEGER PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT DEFAULT 'info',
    symbol TEXT DEFAULT '',
    msg TEXT NOT NULL
);
"""

CATALYST_DAYS = 365
CATALYST_COLUMNS = ("id", "published_at", "publisher", "tickers", "categories", "sentiment", "sentiment_label",
                    "importance", "title")

SIGNALS_SCHEMA_VERSION = 1

NEWS_JSON_FIELDS = ("tickers", "categories", "extra")
NEWS_COLUMNS = ("id", "source", "source_name", "source_type", "title", "summary", "url", "author",
                "published_at", "fetched_at", "tickers", "categories", "sentiment", "sentiment_label",
                "importance", "cluster_id", "extra")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


@contextmanager
def _tx():
    """Write transaction under the global write lock: commit on success, roll back on any error so a failed
    write never leaves this thread's connection holding SQLite's write lock (blocking every other writer)."""
    with _write_lock:
        c = conn()
        try:
            yield c
            c.commit()
        except BaseException:
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            raise


def init_db() -> None:
    with _tx() as c:
        c.executescript(SCHEMA)
        # Columns added after the first release (CREATE TABLE IF NOT EXISTS does not add them to old files).
        cols = {r[1] for r in c.execute("PRAGMA table_info(source_status)")}
        if "last_warning" not in cols:
            c.execute("ALTER TABLE source_status ADD COLUMN last_warning TEXT")
        # multi-day paper positions (evolution bot): exit at the close of the n-th trading day
        if "exit_at" not in {r[1] for r in c.execute("PRAGMA table_info(paper_positions)")}:
            c.execute("ALTER TABLE paper_positions ADD COLUMN exit_at INTEGER")
        if "hold_days" not in {r[1] for r in c.execute("PRAGMA table_info(paper_orders)")}:
            c.execute("ALTER TABLE paper_orders ADD COLUMN hold_days INTEGER")


# ---------------------------------------------------------------- news

def news_id(item: dict) -> str:
    url = normalize_url(item.get("url") or "")
    return make_id(url) if url else make_id(item.get("source"), item.get("title"))


def _news_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for f in NEWS_JSON_FIELDS:
        d[f] = jload(d.get(f), {} if f == "extra" else [])
    return d


def upsert_news(items: list[dict]) -> list[str]:
    """Insert new items (existing ids are left untouched). Returns ids that were new."""
    if not items:
        return []
    new_ids: list[str] = []
    with _tx() as c:
        # fetched_at is taken after the lock wait so it stays close to the commit (alert/new-news cursors).
        now = now_ts()
        for it in items:
            if not it.get("title"):
                continue
            nid = it.get("id") or news_id(it)
            row = {
                "id": nid,
                "source": it["source"],
                "source_name": it.get("source_name") or it["source"],
                "source_type": it.get("source_type") or "news",
                "title": it["title"].strip()[:500],
                "summary": (it.get("summary") or "")[:2000],
                "url": it.get("url") or "",
                "author": it.get("author") or "",
                "published_at": int(it.get("published_at") or now),
                "fetched_at": now,
                # enrich's order is kept: tickers[0] is the story's subject (the bot relies on it)
                "tickers": jdump(list(dict.fromkeys(it.get("tickers") or []))),
                "categories": jdump(list(dict.fromkeys(it.get("categories") or []))),
                "sentiment": float(it.get("sentiment") or 0.0),
                "sentiment_label": it.get("sentiment_label") or "neu",
                "importance": float(it.get("importance") or 0.0),
                "cluster_id": it.get("cluster_id"),
                "extra": jdump(it.get("extra") or {}),
            }
            # Future-dated items (bad feed clocks) are clamped to now.
            if row["published_at"] > now + 300:
                row["published_at"] = now
            # Same headline re-published by the same source under a different URL.
            if c.execute("SELECT 1 FROM news WHERE source=? AND title=? COLLATE NOCASE AND published_at>=? LIMIT 1",
                         (row["source"], row["title"], row["published_at"] - 3 * 86400)).fetchone():
                continue
            cur = c.execute(
                f"INSERT OR IGNORE INTO news ({','.join(NEWS_COLUMNS)}) VALUES ({','.join('?' * len(NEWS_COLUMNS))})",
                [row[k] for k in NEWS_COLUMNS],
            )
            if cur.rowcount:
                new_ids.append(nid)
                c.execute(
                    f"INSERT OR IGNORE INTO news_catalyst ({','.join(CATALYST_COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(CATALYST_COLUMNS))})",
                    [nid, row["published_at"], str((it.get("extra") or {}).get("publisher") or "")[:120],
                     row["tickers"], row["categories"], row["sentiment"], row["sentiment_label"],
                     row["importance"], row["title"][:120]],
                )
    return new_ids


def stale_news(analysis_version, limit: int = 500) -> list[dict]:
    """Stored news whose extra.analysis_version differs from `analysis_version` (oldest-first is not needed:
    the caller loops until none are left)."""
    rows = conn().execute(
        "SELECT * FROM news WHERE COALESCE(json_extract(extra, '$.analysis_version'), '') != ? "
        "ORDER BY published_at DESC LIMIT ?", (str(analysis_version), int(limit))).fetchall()
    return [_news_row(r) for r in rows]


def update_news_fields(updates: list[tuple[str, dict]]) -> None:
    """Bulk update of analysis fields, e.g. [(id, {"cluster_id": "...", "importance": 80})]."""
    allowed = {"tickers", "categories", "sentiment", "sentiment_label", "importance", "cluster_id", "extra", "summary"}
    # Serialize everything first: a bad value must fail before the first UPDATE, not halfway through.
    stmts = []
    for nid, fields in updates:
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            continue
        vals = [jdump(v) if k in NEWS_JSON_FIELDS else v for k, v in fields.items()]
        stmts.append((f"UPDATE news SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*vals, nid]))
    if not stmts:
        return
    with _tx() as c:
        for sql, args in stmts:
            c.execute(sql, args)


def query_news(*, q: str | None = None, sources: list[str] | None = None, source_types: list[str] | None = None,
               category: str | None = None, ticker: str | None = None, sentiment: str | None = None,
               since_ts: int | None = None, until_ts: int | None = None, after_fetched: int | None = None,
               min_importance: float | None = None, cluster_id: str | None = None,
               ids: list[str] | None = None, order: str = "published",
               limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
    where, args = [], []
    if q:
        where.append("(title LIKE ? OR summary LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if sources:
        where.append(f"source IN ({','.join('?' * len(sources))})")
        args += sources
    if source_types:
        where.append(f"source_type IN ({','.join('?' * len(source_types))})")
        args += source_types
    if category:
        where.append("categories LIKE ?")
        args.append(f'%"{category}"%')
    if ticker:
        where.append("tickers LIKE ?")
        args.append(f'%"{ticker.upper()}"%')
    if sentiment in ("pos", "neg", "neu"):
        where.append("sentiment_label = ?")
        args.append(sentiment)
    if since_ts:
        where.append("published_at >= ?")
        args.append(since_ts)
    if until_ts:
        where.append("published_at < ?")
        args.append(until_ts)
    if after_fetched:
        where.append("fetched_at > ?")
        args.append(after_fetched)
    if min_importance is not None:
        where.append("importance >= ?")
        args.append(min_importance)
    if cluster_id:
        where.append("cluster_id = ?")
        args.append(cluster_id)
    if ids is not None:
        if not ids:
            return [], 0
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args += ids
    w = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = {"published": "published_at DESC", "fetched": "fetched_at DESC",
                 "importance": "importance DESC, published_at DESC"}.get(order, "published_at DESC")
    c = conn()
    total = c.execute(f"SELECT COUNT(*) FROM news {w}", args).fetchone()[0]
    rows = c.execute(f"SELECT * FROM news {w} ORDER BY {order_sql} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return [_news_row(r) for r in rows], total


def recent_news(since_ts: int, limit: int = 5000) -> list[dict]:
    rows = conn().execute("SELECT * FROM news WHERE published_at >= ? ORDER BY published_at DESC LIMIT ?",
                          (since_ts, limit)).fetchall()
    return [_news_row(r) for r in rows]


def count_news(since_ts: int | None = None) -> int:
    if since_ts:
        return conn().execute("SELECT COUNT(*) FROM news WHERE published_at >= ?", (since_ts,)).fetchone()[0]
    return conn().execute("SELECT COUNT(*) FROM news").fetchone()[0]


def news_source_counts(since_ts: int) -> dict[str, int]:
    rows = conn().execute("SELECT source, COUNT(*) n FROM news WHERE published_at >= ? GROUP BY source",
                          (since_ts,)).fetchall()
    return {r["source"]: r["n"] for r in rows}


# ---------------------------------------------------------------- calendar

EVENT_COLUMNS = ("id", "kind", "title", "country", "ts", "all_day", "impact", "forecast", "previous", "actual",
                 "ticker", "source", "extra", "updated_at")


def event_id(e: dict) -> str:
    return e.get("id") or make_id(e.get("kind"), e.get("title"), e.get("country"), e.get("ts"), e.get("ticker"))


def upsert_events(events: list[dict]) -> int:
    if not events:
        return 0
    now = now_ts()
    with _tx() as c:
        for e in events:
            eid = event_id(e)
            row = {
                "id": eid, "kind": e.get("kind") or "macro", "title": e.get("title") or "", "country": e.get("country") or "",
                "ts": int(e["ts"]), "all_day": 1 if e.get("all_day") else 0, "impact": e.get("impact") or "",
                "forecast": str(e.get("forecast") or ""), "previous": str(e.get("previous") or ""),
                "actual": str(e.get("actual") or ""), "ticker": (e.get("ticker") or "").upper(),
                "source": e.get("source") or "", "extra": jdump(e.get("extra") or {}), "updated_at": now,
            }
            c.execute(f"INSERT OR REPLACE INTO calendar_events ({','.join(EVENT_COLUMNS)}) "
                      f"VALUES ({','.join('?' * len(EVENT_COLUMNS))})", [row[k] for k in EVENT_COLUMNS])
    return len(events)


def delete_events(source: str, start_ts: int, end_ts: int, keep_ids, kind: str | None = None,
                  status: str | None = None) -> int:
    """Remove events of `source` with start_ts <= ts < end_ts whose id is not in `keep_ids` (a moved earnings
    date, a withdrawn IPO). Only call it for a window the source has just fetched successfully.
    `status` limits the delete to rows whose extra.status matches (e.g. IPO "upcoming")."""
    keep = list(dict.fromkeys(str(i) for i in keep_ids or ()))
    where, args = ["source = ?", "ts >= ?", "ts < ?"], [source, int(start_ts), int(end_ts)]
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if status:
        where.append("json_extract(extra, '$.status') = ?")
        args.append(status)
    with _tx() as c:
        stale = [r[0] for r in c.execute(f"SELECT id FROM calendar_events WHERE {' AND '.join(where)}", args)]
        keep_set = set(keep)
        stale = [i for i in stale if i not in keep_set]
        for i in range(0, len(stale), 500):
            chunk = stale[i:i + 500]
            c.execute(f"DELETE FROM calendar_events WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return len(stale)


def query_events(start_ts: int, end_ts: int, kind: str | None = None, ticker: str | None = None) -> list[dict]:
    where, args = ["ts >= ?", "ts < ?"], [start_ts, end_ts]
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if ticker:
        where.append("ticker = ?")
        args.append(ticker.upper())
    rows = conn().execute(f"SELECT * FROM calendar_events WHERE {' AND '.join(where)} ORDER BY ts", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["extra"] = jload(d.get("extra"), {})
        d["all_day"] = bool(d["all_day"])
        out.append(d)
    return out


# ---------------------------------------------------------------- snapshots

def set_snapshot(key: str, data) -> None:
    text = jdump(data)
    with _tx() as c:
        c.execute("INSERT OR REPLACE INTO snapshots (key, data, updated_at) VALUES (?,?,?)", (key, text, now_ts()))


def get_snapshot(key: str) -> tuple[object, int] | None:
    r = conn().execute("SELECT data, updated_at FROM snapshots WHERE key=?", (key,)).fetchone()
    return (jload(r["data"]), r["updated_at"]) if r else None


def get_snapshot_data(key: str, default=None):
    s = get_snapshot(key)
    return s[0] if s else default


# ---------------------------------------------------------------- briefs

def save_brief(kind: str, author: str, title: str, body_md: str, created_at: int | None = None,
               brief_id: str | None = None) -> dict:
    created_at = created_at or now_ts()
    bid = brief_id or make_id(kind, author, created_at, title)
    with _tx() as c:
        c.execute("INSERT OR REPLACE INTO briefs (id, kind, author, created_at, title, body_md) VALUES (?,?,?,?,?,?)",
                  (bid, kind, author, created_at, title, body_md))
    return {"id": bid, "kind": kind, "author": author, "created_at": created_at, "title": title, "body_md": body_md}


def list_briefs(limit: int = 30, kind: str | None = None, author: str | None = None) -> list[dict]:
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if author:
        where.append("author = ?")
        args.append(author)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn().execute(f"SELECT * FROM briefs {w} ORDER BY created_at DESC LIMIT ?", [*args, limit]).fetchall()
    return [dict(r) for r in rows]


def brief_exists(brief_id: str) -> bool:
    return conn().execute("SELECT 1 FROM briefs WHERE id=?", (brief_id,)).fetchone() is not None


# ---------------------------------------------------------------- source status

def update_source_status(source: str, source_name: str, ok: bool, items: int = 0, error: str | None = None,
                         warning: str | None = None) -> None:
    """`warning`: partial failure of a successful run (shown as "Kısmi hata", never as last_error)."""
    now = now_ts()
    with _tx() as c:
        c.execute("INSERT OR IGNORE INTO source_status (source, source_name, items_total) VALUES (?,?,0)",
                  (source, source_name))
        if ok:
            c.execute("UPDATE source_status SET source_name=?, last_run=?, last_ok=?, last_error=NULL, last_warning=?, "
                      "items_last=?, items_total=items_total+? WHERE source=?",
                      (source_name, now, now, (warning or None) and str(warning)[:500], items, items, source))
        else:
            c.execute("UPDATE source_status SET source_name=?, last_run=?, last_error=? WHERE source=?",
                      (source_name, now, (error or "hata")[:500], source))


def list_source_status() -> dict[str, dict]:
    return {r["source"]: dict(r) for r in conn().execute("SELECT * FROM source_status").fetchall()}


# ---------------------------------------------------------------- signals (opportunity history)

SIGNAL_COLUMNS = ("ts", "ticker", "score", "direction", "session", "price", "change_pct", "rel_volume", "volume",
                  "categories", "reason_codes", "news_ids", "news_count", "n_sources", "first_news_ts", "earnings",
                  "in_watchlist", "schema_version")
SIGNAL_JSON_FIELDS = ("categories", "reason_codes", "news_ids")


def _num(v, cast=float):
    try:
        return None if v is None or v == "" else cast(v)
    except (TypeError, ValueError):
        return None


def _reason_codes(it: dict, rel_volume) -> list[str]:
    """Machine-readable reasons; uses the scorer's own `reason_codes` when it provides them."""
    codes = it.get("reason_codes")
    if isinstance(codes, list) and codes:
        return [str(c) for c in codes]
    out = [str(c) for c in it.get("categories") or []]
    if it.get("earnings_today"):
        out.append("earnings_today")
    chg = _num(it.get("change_pct"))
    if chg is not None and abs(chg) >= 3:
        out.append("gap")
    if rel_volume is not None and rel_volume >= 2:
        out.append("rvol")
    if it.get("social_rank") is not None:
        out.append("social")
    if it.get("in_watchlist"):
        out.append("watchlist")
    return list(dict.fromkeys(out))


def append_signals(items: list[dict], ts: int, session: str = "", rel_volumes: dict | None = None) -> int:
    """Store opportunity items (analysis.opportunities.compute()["items"]) as signal rows at `ts`."""
    items = [it for it in items or [] if isinstance(it, dict) and it.get("ticker")]
    if not items:
        return 0
    rel_volumes = rel_volumes or {}
    c = conn()
    ids = list({str(i) for it in items for i in (it.get("news_ids") or [])[:12]})
    pub: dict[str, int] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in c.execute(f"SELECT id, published_at FROM news WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            pub[r[0]] = r[1]
    rows = []
    for it in items:
        sym = str(it["ticker"]).upper()
        news_ids = [str(i) for i in (it.get("news_ids") or [])][:12]
        rv = _num(it.get("rel_volume"))
        if rv is None:
            rv = _num(rel_volumes.get(sym))
        first = _num(it.get("first_news_ts"), int)
        if first is None:
            times = [pub[i] for i in news_ids if i in pub]
            first = min(times) if times else None
        rows.append({
            "ts": int(ts), "ticker": sym, "score": _num(it.get("score")) or 0.0,
            "direction": str(it.get("direction") or "mixed"), "session": session or "",
            "price": _num(it.get("price")), "change_pct": _num(it.get("change_pct")), "rel_volume": rv,
            "volume": _num(it.get("volume"), int), "categories": jdump(list(it.get("categories") or [])),
            "reason_codes": jdump(_reason_codes(it, rv)), "news_ids": jdump(news_ids),
            "news_count": _num(it.get("news_count"), int) or 0, "n_sources": len(it.get("sources") or []),
            "first_news_ts": first, "earnings": 1 if it.get("earnings_today") else 0,
            "in_watchlist": 1 if it.get("in_watchlist") else 0, "schema_version": SIGNALS_SCHEMA_VERSION,
        })
    with _tx() as c:
        c.executemany(f"INSERT OR REPLACE INTO signals ({','.join(SIGNAL_COLUMNS)}) "
                      f"VALUES ({','.join('?' * len(SIGNAL_COLUMNS))})", [[r[k] for k in SIGNAL_COLUMNS] for r in rows])
    return len(rows)


def query_signals(since: int | None = None, until: int | None = None, ticker: str | None = None,
                  min_score: float | None = None, limit: int = 1000) -> list[dict]:
    """Signal rows. With `since` (exclusive) the oldest come first, so a poller can continue from the largest
    ts it received; otherwise the newest come first."""
    where, args = [], []
    if since:
        where.append("ts > ?")
        args.append(int(since))
    if until:
        where.append("ts < ?")
        args.append(int(until))
    if ticker:
        where.append("ticker = ?")
        args.append(ticker.upper())
    if min_score is not None:
        where.append("score >= ?")
        args.append(float(min_score))
    w = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ts ASC, score DESC" if since else "ts DESC, score DESC"
    rows = conn().execute(f"SELECT * FROM signals {w} ORDER BY {order} LIMIT ?", [*args, int(limit)]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in SIGNAL_JSON_FIELDS:
            d[f] = jload(d.get(f), [])
        d["earnings"], d["in_watchlist"] = bool(d["earnings"]), bool(d["in_watchlist"])
        out.append(d)
    return out


# ---------------------------------------------------------------- maintenance

def prune(days: int = 21, signal_days: int = 180) -> None:
    cutoff = now_ts() - days * 86400
    with _tx() as c:
        c.execute("DELETE FROM news WHERE published_at < ?", (cutoff,))
        c.execute("DELETE FROM calendar_events WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM briefs WHERE created_at < ?", (now_ts() - 90 * 86400,))
        c.execute("DELETE FROM signals WHERE ts < ?", (now_ts() - signal_days * 86400,))
        c.execute("DELETE FROM news_catalyst WHERE published_at < ?", (now_ts() - CATALYST_DAYS * 86400,))
