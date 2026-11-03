"""crypto.db: crypto daily bars, the Fear & Greed index and the crypto bot fleet. Kept apart from the stock databases so
neither side can slow or break the other. One connection per thread, WAL, a global lock around writes."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from ..util import DATA_DIR, now_ts

PATH = DATA_DIR / "crypto.db"

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    base TEXT,
    status TEXT,              -- live = trading on Binance now, gone = delisted (history from the public archive)
    first_d TEXT,
    last_d TEXT,
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    d TEXT NOT NULL,          -- UTC day of the candle's open (00:00 UTC)
    o REAL, h REAL, l REAL, c REAL,
    v REAL,                   -- base volume
    qv REAL,                  -- quote (USDT) volume
    tb REAL,                  -- taker-buy base volume
    n INTEGER,                -- trades
    PRIMARY KEY (symbol, d)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fng (
    d TEXT PRIMARY KEY,       -- UTC day
    v INTEGER                 -- 0 = extreme fear ... 100 = extreme greed
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS progress (
    task TEXT NOT NULL,
    key TEXT NOT NULL,
    done_at INTEGER,
    info TEXT,
    PRIMARY KEY (task, key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS cfleet_bots (
    id TEXT PRIMARY KEY, branch TEXT NOT NULL, niche TEXT NOT NULL, rank INTEGER, status TEXT NOT NULL DEFAULT 'live',
    born_at INTEGER, retired_at INTEGER, parent TEXT, origin TEXT, start_cash REAL NOT NULL, cash REAL NOT NULL,
    genome TEXT NOT NULL, walk TEXT, day_date TEXT, day_start_equity REAL, score REAL, why TEXT, updated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cfleet_bots_status ON cfleet_bots(status, branch, niche);
CREATE TABLE IF NOT EXISTS cfleet_positions (
    bot_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL NOT NULL, cost REAL NOT NULL,
    opened_at INTEGER, entry_day TEXT, stop_price REAL, target_price REAL, exit_at INTEGER, hold_days INTEGER,
    last_price REAL, last_ts INTEGER, ref_price REAL, rate REAL,
    PRIMARY KEY (bot_id, symbol)
);
CREATE TABLE IF NOT EXISTS cfleet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, symbol TEXT, opened_at INTEGER, closed_at INTEGER,
    qty REAL, entry REAL, exit REAL, pnl REAL, ret_pct REAL, why TEXT, hold_days INTEGER, fees REAL
);
CREATE INDEX IF NOT EXISTS idx_cfleet_trades_bot ON cfleet_trades(bot_id, closed_at);
CREATE TABLE IF NOT EXISTS cfleet_equity (
    bot_id TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL,
    PRIMARY KEY (bot_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS cfleet_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, bot_id TEXT, msg TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


@contextmanager
def tx():
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


_inited = [False]


def init() -> None:
    if _inited[0]:
        return
    with tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(cfleet_bots)")}
        pcols = {r[1] for r in c.execute("PRAGMA table_info(cfleet_positions)")}
        if (cols and "day_date" not in cols) or (pcols and "rate" not in pcols):   # first (empty) drafts of the tables
            for t in ("cfleet_bots", "cfleet_positions", "cfleet_trades", "cfleet_equity", "cfleet_log"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript(SCHEMA)
    _inited[0] = True


def kv_get(k: str, default=None):
    init()
    r = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def kv_set(k: str, v) -> None:
    init()
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))


def done_keys(task: str) -> set[str]:
    init()
    return {r[0] for r in conn().execute("SELECT key FROM progress WHERE task=?", (task,))}


def mark(task: str, key: str, info=None) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO progress(task, key, done_at, info) VALUES (?,?,?,?)",
                  (task, key, now_ts(), json.dumps(info, ensure_ascii=False) if info is not None else None))


def counts() -> dict:
    init()
    c = conn()
    r = c.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(d), MAX(d) FROM bars").fetchone()
    s = c.execute("SELECT SUM(status='live'), SUM(status='gone') FROM symbols").fetchone()
    f = c.execute("SELECT COUNT(*), MIN(d), MAX(d) FROM fng").fetchone()
    return {"bars": r[0], "symbols": r[1], "first": r[2], "last": r[3], "live": s[0] or 0, "gone": s[1] or 0,
            "fng_days": f[0], "fng_first": f[1], "fng_last": f[2]}
