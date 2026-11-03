"""Signal scorecard: did the opportunity signals (db `signals`) actually precede a move?

For every stored signal the price path of its *entry day* is read from Yahoo 5-minute bars (regular session only):
- a signal during the regular session enters at the open of the next 5-minute bar (no look-ahead);
- a signal before the open (premarket) enters at that day's opening bar;
- a signal after the close, on a weekend or a holiday enters at the next trading day's opening bar.
From the entry it records the return after 30 and 60 minutes and at the close, the best/worst excursion (MFE/MAE),
the same-window SPY return (market drift) and what the Sanal İşlem bot's exit rule (stop / target / close) would have
done. Returns are in the signal's direction: for a "short" signal a falling price counts as positive; "mixed" signals
are measured as long. Only completed sessions are evaluated (after the entry day's close), so nothing is guessed.

aggregate() turns the rows into the scorecard the UI shows (by score band, reason code, direction, entry kind).
"""
from __future__ import annotations

import importlib
import json
import logging
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import db
from .config import load_config
from .util import ET, close_minutes, is_trading_day, now_ts

log = logging.getLogger("marketdesk.scorecard")

BAR = 300                     # 5-minute bars
MAX_AGE_DAYS = 55             # Yahoo serves 5-minute bars for about 60 days
SETTLE_AFTER_CLOSE = 20 * 60  # wait this long after the close before reading a day's bars
NODATA_AFTER_DAYS = 2         # an entry day still without bars after this is marked "nodata"
MAX_TICKERS_PER_RUN = 150
SMALL_SAMPLE = 20             # groups below this are flagged "az veri"
CACHE_TTL = 120

CODE_LABELS = {
    "gap": "Sert hareket (≥ %3)", "rvol": "Yüksek hacim (≥ 2x)", "earnings_today": "Bugün bilanço",
    "earnings_next": "Yarın bilanço", "earnings_surprise": "EPS sürprizi (≥ %5)", "halt": "İşlem durdurma",
    "social": "Sosyal medyada trend", "watchlist": "İzleme listesinde",
}
ENTRY_TR = {"intraday": "Seans içi sinyal (sonraki 5 dk barında giriş)", "open": "Açılışta giriş (seans dışı sinyal)"}
DIR_TR = {"long": "Yükseliş", "short": "Düşüş", "mixed": "Karışık (yükseliş olarak ölçüldü)"}
BANDS = (("80+", 80, 101), ("60–79", 60, 80), ("40–59", 40, 60), ("< 40", -1, 40))

_run_lock = threading.Lock()
_state = {"last_run": None, "last_result": None, "running": False}
_cache: dict[tuple, tuple[float, dict]] = {}


# ---------------------------------------------------------------- time helpers

def _at_et(d: date, minutes: int) -> int:
    return int(datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET).timestamp())


def _open_ts(d: date) -> int:
    return _at_et(d, 9 * 60 + 30)


def _close_ts(d: date) -> int:
    return _at_et(d, close_minutes(d))


def _next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def entry_plan(ts: int) -> tuple[date, int | None]:
    """(entry day, signal ts for an intraday entry | None = enter at that day's open)."""
    d = datetime.fromtimestamp(ts, ET).date()
    if is_trading_day(d):
        if ts < _open_ts(d):
            return d, None
        if ts < _close_ts(d) - BAR:
            return d, ts
    return _next_trading_day(d), None


# ---------------------------------------------------------------- bars

def _market():
    return importlib.import_module(f"{__package__}.collectors.market")


def _bars(market, sym: str, range_: str) -> dict[date, list[tuple]]:
    """Regular-session 5-minute bars grouped by ET day: [(ts, open, high, low, close), ...]."""
    ysym = market._yahoo_symbol(sym) if hasattr(market, "_yahoo_symbol") else sym
    res = market._chart(ysym, range_=range_, interval="5m", prepost=False)
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    out: dict[date, list[tuple]] = defaultdict(list)
    for i, t in enumerate(res.get("timestamp") or []):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]  # noqa: E741
        except (KeyError, IndexError, TypeError):
            continue
        if None in (t, o, h, l, c) or c <= 0:
            continue
        d = datetime.fromtimestamp(t, ET).date()
        if _open_ts(d) <= t < _close_ts(d):
            out[d].append((int(t), float(o), float(h), float(l), float(c)))
    for d in out:
        out[d].sort()
    return out


def _range_for(oldest: date, today: date) -> str:
    age = (today - oldest).days
    return "5d" if age <= 4 else "1mo" if age <= 27 else "60d"


# ---------------------------------------------------------------- evaluation

def _price_at(path: list[tuple], t: int) -> float | None:
    """Close of the last bar that ended at or before t."""
    px = None
    for b in path:
        if b[0] + BAR <= t:
            px = b[4]
        else:
            break
    return px


def _pct(a: float | None, b: float | None) -> float | None:
    return round((a / b - 1) * 100, 3) if a and b else None


def evaluate_one(ts: int, direction: str, day_bars: list[tuple], spy_bars: list[tuple] | None,
                 tp: float, sl: float) -> dict | None:
    """Outcome of one signal on its entry day's bars; None when the entry bar does not exist."""
    day, t0 = entry_plan(ts)
    if not day_bars:
        return None
    idx = 0 if t0 is None else next((i for i, b in enumerate(day_bars) if b[0] >= t0), None)
    if idx is None:
        return None
    path = day_bars[idx:]
    entry_ts, entry = path[0][0], path[0][1] or path[0][4]
    sg = -1.0 if direction == "short" else 1.0
    raw30 = _pct(_price_at(path, entry_ts + 1800), entry)
    raw60 = _pct(_price_at(path, entry_ts + 3600), entry)
    rawc = _pct(path[-1][4], entry)
    hi, lo = max(b[2] for b in path), min(b[3] for b in path)
    mfe_raw, mae_raw = _pct(hi, entry), _pct(lo, entry)
    mfe, mae = (mfe_raw, mae_raw) if sg > 0 else (-mae_raw if mae_raw is not None else None,
                                                  -mfe_raw if mfe_raw is not None else None)
    # The bot's exit rule on the same path: stop is checked before target inside one bar (conservative).
    result, bot_ret = "close", (rawc * sg if rawc is not None else None)
    for b in path:
        adverse = (b[3] / entry - 1) * 100 * sg if sg > 0 else (b[2] / entry - 1) * 100 * sg
        favorable = (b[2] / entry - 1) * 100 * sg if sg > 0 else (b[3] / entry - 1) * 100 * sg
        if adverse <= -sl:
            result, bot_ret = "stop", -sl
            break
        if favorable >= tp:
            result, bot_ret = "target", tp
            break
    spy = None
    if spy_bars:
        s0 = next((b for b in spy_bars if b[0] >= entry_ts), None)
        if s0:
            spy = _pct(spy_bars[-1][4], s0[1] or s0[4])
    d = lambda v: round(v * sg, 3) if v is not None else None  # noqa: E731
    return {"status": "done", "entry_day": day.isoformat(), "entry_kind": "open" if t0 is None else "intraday",
            "entry_ts": entry_ts, "entry_price": round(entry, 4), "ret_30m": d(raw30), "ret_60m": d(raw60),
            "ret_close": d(rawc), "mfe": mfe, "mae": mae, "spy_ret_close": spy, "bot_result": result,
            "bot_ret": round(bot_ret, 3) if bot_ret is not None else None, "tp_pct": tp, "sl_pct": sl}


def _pending(now: int, limit: int = 20000) -> list[dict]:
    rows = db.conn().execute(
        "SELECT s.ts, s.ticker, s.direction FROM signals s LEFT JOIN signal_outcomes o ON o.ts = s.ts AND o.ticker = s.ticker"
        " WHERE o.ts IS NULL AND s.ts >= ? ORDER BY s.ts LIMIT ?", (now - MAX_AGE_DAYS * 86400, limit)).fetchall()
    return [dict(r) for r in rows]


def evaluate(now: int | None = None) -> dict:
    """Evaluate every stored signal whose entry day has closed. Safe to call often (no-op when nothing is due)."""
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "zaten çalışıyor"}
    _state["running"] = True
    try:
        return _evaluate(now or now_ts())
    finally:
        _state["running"] = False
        _run_lock.release()


def _evaluate(now: int) -> dict:
    t_start = time.time()
    try:
        ps = importlib.import_module(f"{__package__}.paper").settings(load_config())
        tp, sl = float(ps["bot_take_profit_pct"]), float(ps["bot_stop_loss_pct"])
    except Exception:  # noqa: BLE001
        tp, sl = 4.0, 2.0
    today = datetime.fromtimestamp(now, ET).date()
    due: dict[str, list[tuple[int, str, date]]] = defaultdict(list)
    waiting = 0
    for r in _pending(now):
        day, _ = entry_plan(int(r["ts"]))
        if now < _close_ts(day) + SETTLE_AFTER_CLOSE:
            waiting += 1
            continue
        due[r["ticker"]].append((int(r["ts"]), r["direction"] or "mixed", day))
    if not due:
        res = {"evaluated": 0, "nodata": 0, "waiting": waiting, "tickers": 0, "seconds": round(time.time() - t_start, 2)}
        _state["last_run"], _state["last_result"] = now, res
        return res
    market = _market()
    tickers = sorted(due, key=lambda s: min(x[0] for x in due[s]))[:MAX_TICKERS_PER_RUN]
    oldest = min(x[2] for s in tickers for x in due[s])
    rng = _range_for(oldest, today)
    try:
        spy = _bars(market, "SPY", rng)
    except Exception as e:  # noqa: BLE001 - the scorecard works without the SPY comparison
        log.info("scorecard SPY bars failed: %s", e)
        spy = {}
    fetched = market._parallel(lambda s: _bars(market, s, rng), [(s,) for s in tickers], workers=4, timeout=120)
    rows, nodata, failed = [], 0, 0
    for i, sym in enumerate(tickers):
        bars = fetched.get(i)
        if isinstance(bars, Exception) or bars is None:
            failed += 1
            log.info("scorecard bars %s failed: %s", sym, bars)
            bars = None
        for ts, direction, day in due[sym]:
            out = evaluate_one(ts, direction, (bars or {}).get(day) or [], spy.get(day), tp, sl) if bars is not None else None
            if out is None:
                if bars is None or (today - day).days < NODATA_AFTER_DAYS:
                    continue    # network error or bars not published yet: retry on the next run
                out = {"status": "nodata", "entry_day": day.isoformat()}
                nodata += 1
            rows.append((ts, sym, out))
    cols = ("status", "entry_day", "entry_kind", "entry_ts", "entry_price", "ret_30m", "ret_60m", "ret_close", "mfe",
            "mae", "spy_ret_close", "bot_result", "bot_ret", "tp_pct", "sl_pct")
    with db._tx() as c:
        c.executemany(f"INSERT OR REPLACE INTO signal_outcomes(ts, ticker, {', '.join(cols)}, evaluated_at) VALUES"
                      f" (?, ?, {', '.join('?' * len(cols))}, ?)",
                      [(ts, sym, *[o.get(k) for k in cols], now) for ts, sym, o in rows])
        c.execute("DELETE FROM signal_outcomes WHERE ts < ?", (now - 180 * 86400,))
    _cache.clear()
    res = {"evaluated": len(rows) - nodata, "nodata": nodata, "failed_tickers": failed, "waiting": waiting,
           "tickers": len(tickers), "range": rng, "seconds": round(time.time() - t_start, 2)}
    _state["last_run"], _state["last_result"] = now, res
    log.info("scorecard: %s", res)
    return res


# ---------------------------------------------------------------- aggregation (read side)

def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    rc = [r["ret_close"] for r in rows if r["ret_close"] is not None]
    bot = [r for r in rows if r["bot_result"]]
    excess = [r["ret_close"] - (r["spy_ret_close"] * (-1 if r["direction"] == "short" else 1))
              for r in rows if r["ret_close"] is not None and r["spy_ret_close"] is not None]
    return {
        "n": n, "small": n < SMALL_SAMPLE,
        "win_rate": round(sum(1 for x in rc if x > 0) / len(rc) * 100, 1) if rc else None,
        "avg_ret_close": _avg(rc), "median_ret_close": round(statistics.median(rc), 3) if rc else None,
        "avg_ret_30m": _avg([r["ret_30m"] for r in rows]), "avg_ret_60m": _avg([r["ret_60m"] for r in rows]),
        "avg_mfe": _avg([r["mfe"] for r in rows]), "avg_mae": _avg([r["mae"] for r in rows]),
        "avg_excess_spy": _avg(excess),
        "bot_target_pct": round(sum(1 for r in bot if r["bot_result"] == "target") / len(bot) * 100, 1) if bot else None,
        "bot_stop_pct": round(sum(1 for r in bot if r["bot_result"] == "stop") / len(bot) * 100, 1) if bot else None,
        "bot_avg_ret": _avg([r["bot_ret"] for r in bot]),
    }


def aggregate(days: int = 30, min_score: float = 0.0, direction: str = "", unit: str = "first") -> dict:
    """Scorecard. unit "first": one row per ticker per entry day (its first qualifying signal) — what a bot that
    enters a name once a day would see; "all": every stored signal row (overlapping, overstates the sample)."""
    key = (days, min_score, direction, unit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    now = now_ts()
    since = now - days * 86400
    c = db.conn()
    q = ("SELECT s.ts, s.ticker, s.score, s.direction, s.session, s.reason_codes, o.* FROM signals s"
         " JOIN signal_outcomes o ON o.ts = s.ts AND o.ticker = s.ticker"
         " WHERE o.status = 'done' AND s.ts >= ? AND s.score >= ?")
    args: list = [since, min_score]
    if direction in ("long", "short", "mixed"):
        q += " AND s.direction = ?"
        args.append(direction)
    rows = []
    for r in c.execute(q + " ORDER BY s.ts", args):
        d = dict(r)
        try:
            d["codes"] = [str(x) for x in json.loads(d.get("reason_codes") or "[]")]
        except ValueError:
            d["codes"] = []
        rows.append(d)
    if unit != "all":
        first: dict[tuple, dict] = {}
        for r in rows:
            first.setdefault((r["ticker"], r["entry_day"]), r)
        rows = list(first.values())
    by_band = []
    for label, lo, hi in BANDS:
        sub = [r for r in rows if lo <= float(r["score"] or 0) < hi]
        if sub:
            by_band.append(dict(_stats(sub), key=label, label=f"Skor {label}"))
    codes: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        for cd in dict.fromkeys(r["codes"]):
            codes[cd].append(r)
    by_code = sorted((dict(_stats(v), key=k, label=CODE_LABELS.get(k, "")) for k, v in codes.items()),
                     key=lambda x: (-x["n"], x["key"]))
    by_dir = [dict(_stats(v), key=k, label=DIR_TR.get(k, k))
              for k in ("long", "short", "mixed") if (v := [r for r in rows if r["direction"] == k])]
    by_entry = [dict(_stats(v), key=k, label=ENTRY_TR.get(k, k))
                for k in ("intraday", "open") if (v := [r for r in rows if r["entry_kind"] == k])]
    recent = sorted(rows, key=lambda r: -r["ts"])[:150]
    pending_total = c.execute("SELECT COUNT(*) FROM signals s LEFT JOIN signal_outcomes o ON o.ts = s.ts AND o.ticker = s.ticker"
                              " WHERE o.ts IS NULL AND s.ts >= ?", (since,)).fetchone()[0]
    first_ts = c.execute("SELECT MIN(ts) FROM signals").fetchone()[0]
    out = {
        "now": now, "days": days, "min_score": min_score, "direction": direction, "unit": unit,
        "overall": _stats(rows), "by_band": by_band, "by_code": by_code, "by_direction": by_dir, "by_entry": by_entry,
        "recent": [{k: r.get(k) for k in ("ts", "ticker", "score", "direction", "session", "codes", "entry_day", "entry_kind",
                                          "entry_ts", "entry_price", "ret_30m", "ret_60m", "ret_close", "mfe", "mae",
                                          "spy_ret_close", "bot_result", "bot_ret")} for r in recent],
        "pending": pending_total, "signals_since": first_ts,
        "last_run": _state["last_run"], "last_result": _state["last_result"], "running": _state["running"],
        "small_sample": SMALL_SAMPLE,
        "bot_rule": {"tp": (recent[0]["tp_pct"] if recent else None), "sl": (recent[0]["sl_pct"] if recent else None)},
    }
    _cache[key] = (time.time(), out)
    return out
