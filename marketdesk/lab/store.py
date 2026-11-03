"""lab.db: historical data for the test lab. Kept apart from marketdesk.db so the live app's database stays small
and a re-download never touches it. One connection per thread, WAL, a global lock around writes."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from ..util import DATA_DIR, now_ts

LAB_PATH = DATA_DIR / "lab.db"

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    cik INTEGER,
    sector TEXT,
    industry TEXT,
    market_cap REAL,
    ipo_year INTEGER,
    rank INTEGER,
    news INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    d TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (symbol, d)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS earnings (
    symbol TEXT NOT NULL,
    d TEXT NOT NULL,
    time TEXT,
    eps REAL,
    eps_est REAL,
    surprise REAL,
    PRIMARY KEY (symbol, d)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS filings (
    symbol TEXT NOT NULL,
    accession TEXT NOT NULL,
    ts INTEGER NOT NULL,
    form TEXT,
    items TEXT,
    PRIMARY KEY (symbol, accession)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_filings_sym_ts ON filings(symbol, ts);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    source TEXT,
    headline TEXT,
    categories TEXT DEFAULT '[]',
    sentiment REAL DEFAULT 0,
    importance REAL DEFAULT 0,
    subject INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lab_news_sym_ts ON news(symbol, ts);
CREATE TABLE IF NOT EXISTS progress (
    task TEXT NOT NULL,
    key TEXT NOT NULL,
    done_at INTEGER,
    info TEXT,
    PRIMARY KEY (task, key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    kind TEXT,
    params TEXT,
    report TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS lineage (
    arm TEXT NOT NULL,
    id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent TEXT,
    origin TEXT,
    born TEXT,
    bot TEXT NOT NULL,
    live TEXT,
    score REAL,
    weight REAL,
    updated INTEGER,
    PRIMARY KEY (arm, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS variants (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent TEXT,
    origin TEXT,
    born TEXT,
    genes TEXT NOT NULL,
    stats TEXT,
    score REAL,
    updated INTEGER,
    PRIMARY KEY (kind, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS cands (
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    chg REAL,
    rvol REAL,
    dvol REAL,
    score REAL,
    dir TEXT,
    PRIMARY KEY (kind, day, symbol, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS mbars (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    bars TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
) WITHOUT ROWID;
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(LAB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:   # WAL is stored in the file; a concurrent first open may find it locked for a moment
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


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


def kv_get(k: str, default=None):
    r = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def kv_set(k: str, v) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))


def done(task: str, key: str) -> bool:
    return conn().execute("SELECT 1 FROM progress WHERE task=? AND key=?", (task, key)).fetchone() is not None


def done_keys(task: str) -> set[str]:
    return {r[0] for r in conn().execute("SELECT key FROM progress WHERE task=?", (task,))}


def mark(task: str, key: str, info=None) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO progress(task, key, done_at, info) VALUES (?,?,?,?)",
                  (task, key, now_ts(), json.dumps(info, ensure_ascii=False) if info is not None else None))


def counts() -> dict:
    c = conn()
    out = {}
    for t in ("universe", "bars", "earnings", "filings", "news"):
        out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    r = c.execute("SELECT MIN(d), MAX(d), COUNT(DISTINCT symbol) FROM bars").fetchone()
    out["bars_from"], out["bars_to"], out["bars_symbols"] = r[0], r[1], r[2]
    r = c.execute("SELECT MIN(ts), MAX(ts), COUNT(DISTINCT symbol) FROM news").fetchone()
    out["news_from"], out["news_to"], out["news_symbols"] = r[0], r[1], r[2]
    r = c.execute("SELECT MIN(d), MAX(d) FROM earnings").fetchone()
    out["earnings_from"], out["earnings_to"] = r[0], r[1]
    r = c.execute("SELECT MIN(ts), MAX(ts) FROM filings").fetchone()
    out["filings_from"], out["filings_to"] = r[0], r[1]
    try:
        out["size_mb"] = round(LAB_PATH.stat().st_size / 1e6, 1)
    except OSError:
        out["size_mb"] = None
    return out
