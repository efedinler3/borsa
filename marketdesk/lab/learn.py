"""Canlı öğrenme: every trading day all bot variants race on the live market in "shadow"; after the close the winners
are cloned with small changes, the losers drop out, and the next morning the paper account trades with what was
learned. No AI: rules and statistics only.

Shadow (gölge): every variant's every signal is followed virtually with the same entry / exit rules and costs as a
paper trade, so 48 settings can be compared on the same day without opening 48 positions.

Four arms (kol):
  ana     the 10-year tournament bots, frozen: the yardstick the evolving arm has to beat
  evrim   the same lineage, evolving nightly: the best are cloned with small threshold / exit changes; a clone enters
          as a candidate (aday) only after passing the 10-year exam and trades only after enough live results
  firsat  variants of the opportunity bot's settings (score, move, volume / liquidity, stop, target, 10-minute cut,
          30-minute cull, profit lock, intraday stop), replayed on every recorded candidate with 1-minute bars
  mikro   the same for the $100 small-cap bot

Scoring is combined (bileşik): expected daily return = (n0 x prior + sum of live daily returns) / (n0 + live days).
The prior is half the 10-year daily return for the evolution bots (0 for setting variants); n0 and the evidence
needed before anything is promoted, dropped or applied come from the speed setting (Dengeli: n0 = 20 days, at least
30 results on 3 days). One lucky day therefore moves a score by about 1/21 of its size and cannot take over alone.
The referee (hakem) is that score: evolution bots vote with weights that follow their live results, the setting
variant with the best score drives the paper bot, and the evolving arm trades only while it is not behind the frozen
arm.
"""
from __future__ import annotations

import copy
import gc
import hashlib
import importlib
import json
import logging
import math
import random
import threading
import time
from datetime import date, datetime, timedelta

import numpy as np

from ..util import ET, close_minutes, is_trading_day, market_session, now_ts
from . import evo_live, evolve, store

log = logging.getLogger("marketdesk.lab")

SPEEDS = {
    "dengeli": {"label": "Dengeli", "n0": 20, "min_results": 30, "min_days": 3},
    "hizli": {"label": "Hızlı", "n0": 5, "min_results": 10, "min_days": 1},
    "temkinli": {"label": "Temkinli", "n0": 60, "min_results": 80, "min_days": 10},
}
DEFAULT_SETTINGS = {"speed": "dengeli", "auto_apply": True}
WINDOW_DAYS = 40            # setting variants are scored on the last 40 recorded trading days (markets change)
KEEP_DAYS = 60              # recorded candidates / 1-minute bars kept this many days
RECORD_EVERY = 60           # one candidate snapshot per symbol per minute
INTRADAY_EVERY = 30 * 60    # live scoreboard
EVO_ACTIVE_MAX, EVO_CAND_MAX, EVO_ACTIVE_MIN = 70, 40, 20
CLONE_SHARE, CLONES_PER_PARENT = 0.2, 2
PRIOR_HAIRCUT = 0.5         # 10-year results were seen by the lineage: only half of them counts as expectation
CLONE_FIT_FLOOR = 0.95      # a clone keeps at least 95 % of its parent's 10-year fitness (güvenlik çıtası)
STOPCLONE_FIT_FLOOR = 0.8   # a no-stop bot given a stop keeps at least 80 % of its fitness
CAND_EXPIRE_DAYS = 30       # a candidate without enough live evidence after 30 trading days is retired
VAR_POP = 48
ATR_FILTER = ["atr", ">", 0.0573, False]   # 2026-10-27 entry filter (90th percentile of ATR); forward test only
ATR_TWINS = 20                              # how many of the best trading bots get an ATR-filtered twin each night
ELIM_SHARE = 0.2
MIKRO_MAX_POS, MIKRO_FRAC = 3, 0.35
SWITCH_MARGIN = 0.01        # % per day a challenger must lead the applied setting by

FIRSAT_GENES = {
    "min_score": (40.0, 50.0, 60.0, 70.0, 80.0),
    "mixed": (0, 1),
    "min_chg": (1.0, 2.0, 3.0, 4.0),
    "max_chg": (5.0, 8.0, 12.0, 20.0),
    "min_rvol": (1.0, 1.5, 2.0, 3.0),
    "min_price": (5.0, 10.0, 20.0),
    "min_dvol": (5e6, 2e7, 5e7, 2e8),
    "sl": (0.75, 1.0, 1.5, 2.0, 3.0),
    "tp": (1.5, 2.0, 3.0, 4.0, 6.0),
    "cut10": (0, 1),
    "cull30": (0, 1),
    "lock": (0.0, 1.0, 1.5),
    "start": ("09:45", "10:00", "10:30"),
    "last": ("11:30", "14:00", "15:30"),
    "max_trades": (3, 5),
    "day_stop": (0.0, 0.5, 1.0),
}
MIKRO_GENES = {
    "min_price": (1.0, 2.0, 3.0),
    "max_price": (5.0, 10.0),
    "min_chg": (2.0, 4.0, 6.0),
    "max_chg": (12.0, 20.0, 40.0),
    "min_rvol": (0.0, 1.5, 3.0),
    "min_dvol": (1e6, 5e6, 2e7),
    "sl": (1.5, 3.0, 5.0),
    "tp": (3.0, 4.0, 6.0, 10.0),
    "cut10": (0, 1),
    "cull30": (0, 1),
    "lock": (0.0, 2.0),
    "start": ("09:35", "09:45", "10:00", "10:30"),
    "last": ("12:00", "15:00", "15:30"),
    "max_trades": (3, 6),
    "day_stop": (0.0, 5.0),
}
GENES = {"firsat": FIRSAT_GENES, "mikro": MIKRO_GENES}
KIND_TR = {"firsat": "Fırsat botu", "mikro": "Mikro bot"}
FIRSAT_TO_PAPER = {"min_score": "bot_min_score", "mixed": "bot_direction_mixed", "min_chg": "bot_min_change_pct",
                   "max_chg": "bot_max_change_pct", "min_rvol": "bot_min_rel_volume", "min_price": "bot_min_price",
                   "min_dvol": "bot_min_dollar_volume", "sl": "bot_stop_loss_pct", "tp": "bot_take_profit_pct",
                   "cut10": "bot_cut10", "cull30": "bot_cull30", "lock": "bot_lock_pct", "start": "bot_start_et",
                   "last": "bot_last_entry_et", "max_trades": "bot_max_trades_per_day", "day_stop": "bot_day_stop_pct"}

NIGHT: dict = {"state": "idle", "message": "", "started_at": None, "finished_at": None}
LIVE_STATE: dict = {"last": 0}
_ready = False


def _pkg(name: str):
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{name}")


def _init() -> None:
    global _ready
    if not _ready:
        store.init()
        _ready = True


# ---------------------------------------------------------------- settings

def settings() -> dict:
    _init()
    s = dict(DEFAULT_SETTINGS)
    raw = store.kv_get("learn_settings") or {}
    s.update({k: v for k, v in raw.items() if k in s})
    if s["speed"] not in SPEEDS:
        s["speed"] = DEFAULT_SETTINGS["speed"]
    s["auto_apply"] = bool(s["auto_apply"])
    return s


def update_settings(upd: dict) -> dict:
    s = settings()
    if (upd or {}).get("speed") in SPEEDS:
        s["speed"] = upd["speed"]
    if "auto_apply" in (upd or {}):
        v = upd["auto_apply"]
        s["auto_apply"] = v.strip().lower() in ("1", "true", "evet", "on") if isinstance(v, str) else bool(v)
    store.kv_set("learn_settings", s)
    return s


# ---------------------------------------------------------------- time helpers

def _et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def _day_ts(d: date, minutes: int) -> int:
    return int(datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET).timestamp())


def _hhmm(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _prev_trading_days(d: date, n: int) -> list[date]:
    out, x = [], d
    while len(out) < n:
        x -= timedelta(days=1)
        if is_trading_day(x):
            out.append(x)
    return out


def _last_bar_day() -> date | None:
    r = store.conn().execute("SELECT MAX(d) FROM bars WHERE symbol='SPY'").fetchone()
    return date.fromisoformat(r[0]) if r and r[0] else None


# ---------------------------------------------------------------- recording (paper engine threads)

_rec_last: dict[tuple, int] = {}
_rec_lock = threading.Lock()   # record() is called from both paper engines (firsat, mikro)


RETIRED = [False]          # set by runner.tick: the bot tree (fleet.py) replaced this live learning


def record(kind: str, now: int, rows: list[dict]) -> None:
    """One snapshot per symbol per minute of what the bot could see: price, today's move, time-adjusted relative
    volume, average daily dollar volume, opportunity score and direction."""
    if not rows or RETIRED[0]:
        return
    _init()
    day = _et(now).date().isoformat()
    out = []
    with _rec_lock:
        for r in rows:
            sym = r.get("symbol")
            if not sym or not r.get("price"):
                continue
            k = (kind, sym)
            if now - _rec_last.get(k, 0) < RECORD_EVERY - 5:
                continue
            _rec_last[k] = now
            out.append((kind, day, now, sym, float(r["price"]), _f(r.get("chg")), _f(r.get("rvol")), _f(r.get("dvol")),
                        _f(r.get("score")), r.get("dir")))
        if len(_rec_last) > 5000:
            for key in [k for k, t in _rec_last.items() if now - t > 3600]:
                _rec_last.pop(key, None)
    if out:
        with store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO cands(kind, day, ts, symbol, price, chg, rvol, dvol, score, dir)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)", out)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _cand_days(kind: str) -> list[str]:
    return [r[0] for r in store.conn().execute("SELECT DISTINCT day FROM cands WHERE kind=? ORDER BY day", (kind,))]


def _cand_rows(kind: str, day: str) -> list[tuple]:
    return [tuple(r) for r in store.conn().execute(
        "SELECT ts, symbol, price, chg, rvol, dvol, score, dir FROM cands WHERE kind=? AND day=? ORDER BY ts, symbol",
        (kind, day))]


# ---------------------------------------------------------------- 1-minute bars

_mb_cache: dict[tuple, tuple[float, list]] = {}
_mb_lock = threading.Lock()   # shared by the intraday and nightly threads


def _parse_bars(res: dict, d: date) -> list[list]:
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    o_, h_, l_, c_ = (q.get(k) or [] for k in ("open", "high", "low", "close"))
    t0, t1 = _day_ts(d, 9 * 60 + 30), _day_ts(d, close_minutes(d))
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        if t is None or not t0 <= t < t1:
            continue
        try:
            o, h, l, c = o_[i], h_[i], l_[i], c_[i]  # noqa: E741
        except IndexError:
            continue
        if None in (o, h, l, c) or c <= 0:
            continue
        out.append([int(t), round(float(o), 4), round(float(h), 4), round(float(l), 4), round(float(c), 4)])
    out.sort()
    return out


def bars_for(symbols, d: date, now: int) -> dict[str, list]:
    """Regular-session bars of day `d` per symbol ([ts, o, h, l, c], 1 minute; 5 minutes for days older than a week
    that were never stored). Complete days are stored in lab.db; today's partial bars are cached for 4 minutes."""
    _init()
    diso = d.isoformat()
    complete = now >= _day_ts(d, close_minutes(d)) + 300
    out, need = {}, []
    con = store.conn()
    for s in sorted(set(symbols)):
        ck = _mb_cache.get((s, diso))
        if ck and (complete or time.time() - ck[0] < 240):
            out[s] = ck[1]
            continue
        r = con.execute("SELECT bars FROM mbars WHERE symbol=? AND day=?", (s, diso)).fetchone()
        if r:
            out[s] = json.loads(r[0])
            _mb_cache[(s, diso)] = (time.time(), out[s])
            continue
        need.append(s)
    if need:
        market = _pkg("collectors.market")
        age = (_et(now).date() - d).days

        def fetch(sym: str) -> list:
            ysym = market._yahoo_symbol(sym) if hasattr(market, "_yahoo_symbol") else sym
            if age <= 6:
                res = market._chart(ysym, range_="1d" if age == 0 else "7d", interval="1m", prepost=False)
            else:
                res = market._chart(ysym, range_="60d", interval="5m", prepost=False)
            return _parse_bars(res, d)

        res = market._parallel(fetch, [(s,) for s in need], workers=4, timeout=180)
        store_rows = []
        for i, s in enumerate(need):
            b = res.get(i)
            if isinstance(b, Exception) or b is None:
                continue
            out[s] = b
            _mb_cache[(s, diso)] = (time.time(), b)
            if complete and b:
                store_rows.append((s, diso, json.dumps(b, separators=(",", ":"))))
        if store_rows:
            with store.tx() as c:
                c.executemany("INSERT OR REPLACE INTO mbars(symbol, day, bars) VALUES (?,?,?)", store_rows)
    with _mb_lock:
        if len(_mb_cache) > 3000:
            for k in sorted(list(_mb_cache.items()), key=lambda kv: kv[1][0])[:1000]:
                _mb_cache.pop(k[0], None)
    return out


# ---------------------------------------------------------------- setting variants: replay

def _slip(kind: str, price: float, dvol: float | None) -> float:
    """Per-side cost: liquidity-based like the lab (3-20 bps) for the opportunity bot; for $1-10 stocks at least half a
    cent of spread (and more for thin ones)."""
    d = dvol or 0.0
    if kind == "firsat":
        return 0.0003 if d >= 1e8 else 0.0005 if d >= 2e7 else 0.0010 if d >= 5e6 else 0.0020
    s = max(0.005 / max(price, 0.01), 0.002)
    return s + (0.003 if d < 2e6 else 0.0)


def _passes(kind: str, g: dict, row: tuple, minute: int) -> bool:
    _ts, _sym, price, chg, rvol, dvol, score, dir_ = row
    if minute < _hhmm(g["start"]) or minute > _hhmm(g["last"]) or price is None or chg is None:
        return False
    if kind == "firsat":
        if dir_ != "long" and not (dir_ == "mixed" and g["mixed"]):
            return False
        if (score or 0) < g["min_score"] or price < g["min_price"]:
            return False
    elif not g["min_price"] <= price <= g["max_price"]:
        return False
    if not g["min_chg"] <= chg <= g["max_chg"]:
        return False
    if g["min_rvol"] > 0 and (rvol is None or rvol < g["min_rvol"]):
        return False
    return (dvol or 0.0) >= g["min_dvol"]


def _trade(g: dict, t0: int, px0: float, bars: list, slip: float, t_end: int, complete: bool) -> dict | None:
    """One shadow trade bought at `px0` (+ cost) at `t0`, followed on the bars that start after it: stop first when a
    bar touches both levels, the 10-minute cut once, the 30-minute cull at every half hour of the position's age,
    the profit lock moving the stop to entry + 0.2 %, the end-of-day exit 5 minutes before the close."""
    if not bars:
        return None
    entry = px0 * (1 + slip)
    stop, tgt = entry * (1 - g["sl"] / 100), entry * (1 + g["tp"] / 100)
    lock = float(g.get("lock") or 0) / 100
    cut_done, next_cull, marks = False, t0 + 1800, []

    def done(t: int, px: float, why: str) -> dict:
        return {"t": t, "ret": px * (1 - slip) / entry - 1, "why": why, "entry": entry, "marks": marks, "open": False}

    for ts, o, h, l, c in bars:  # noqa: E741
        if ts < t0:
            continue
        if ts >= t_end:
            break
        if l <= stop:
            return done(ts + 60, min(o, stop), "lock" if stop > entry else "stop")
        if h >= tgt:
            return done(ts + 60, max(o, tgt), "target")
        if lock and stop < entry * 1.002 and h >= entry * (1 + lock):
            stop = entry * 1.002
        age, gain = ts + 60 - t0, c / entry - 1
        if g.get("cut10") and not cut_done and age >= 600:
            cut_done = True
            if gain <= -0.008:
                return done(ts + 60, c, "cut10")
        if g.get("cull30") and ts + 60 >= next_cull:
            while next_cull <= ts + 60:
                next_cull += 1800
            if gain < 0.002:
                return done(ts + 60, c, "cull30")
        marks.append((ts + 60, c))
    if complete:
        return done(marks[-1][0] if marks else t_end, marks[-1][1] if marks else px0, "eod")
    last = marks[-1][1] if marks else px0
    return {"t": None, "ret": last * (1 - slip) / entry - 1, "why": "open", "entry": entry, "marks": marks, "open": True}


def _mark_at(tr: dict, t: int) -> float:
    if tr["t"] is not None and tr["t"] <= t:
        return tr["ret"]
    px = None
    for ts, c in tr["marks"]:
        if ts > t:
            break
        px = c
    return 0.0 if px is None else px / tr["entry"] - 1


def _frac(kind: str, g: dict, ps: dict) -> float:
    """Share of equity one trade uses (the paper bot sizes by risk and caps by position size)."""
    if kind == "mikro":
        return MIKRO_FRAC
    # risk % / stop % is already a share of equity (0.3 / 1 = 30 %); the position cap is in percent
    return min(ps.get("bot_risk_per_trade_pct", 0.3) / max(g["sl"], 0.1), ps.get("max_position_pct", 10.0) / 100)


def _sim_day(kind: str, g: dict, rows: list, bars_of, d: date, now: int, frac: float, memo: dict) -> dict:
    close_ts = _day_ts(d, close_minutes(d))
    t_end, complete = close_ts - 300, now >= close_ts
    cps = [_day_ts(d, m) for m in range(600, close_minutes(d) - 29, 30)]     # 10:00 ... 15:30 checks
    trades, traded, elim, ci = [], set(), None, 0
    for row in rows:
        ts = row[0]
        if ts > now or ts >= t_end:
            break
        while elim is None and ci < len(cps) and cps[ci] <= ts:
            if g.get("day_stop") and sum(_mark_at(t, cps[ci]) for t in trades) * frac < -g["day_stop"] / 100:
                elim = cps[ci]
            ci += 1
        if elim is not None:
            break
        if len(trades) >= g["max_trades"] or row[1] in traded:
            continue
        et = _et(ts)
        if not _passes(kind, g, row, et.hour * 60 + et.minute):
            continue
        if kind == "mikro" and sum(1 for t in trades if t["t"] is None or t["t"] > ts) >= MIKRO_MAX_POS:
            continue
        key = (row[1], ts, g["sl"], g["tp"], g.get("lock"), g.get("cut10"), g.get("cull30"), complete, now if not complete else 0)
        if key not in memo:
            memo[key] = _trade(g, ts, row[2], bars_of(row[1]), _slip(kind, row[2], row[5]), t_end, complete)
        tr = memo[key]
        if tr is None:
            continue
        traded.add(row[1])
        trades.append(dict(tr, symbol=row[1], t0=ts, px=row[2]))
    return {"ret": sum(t["ret"] for t in trades) * frac, "trades": trades, "elim": elim}


# ---------------------------------------------------------------- setting variants: population

def _vid(kind: str, g: dict) -> str:
    return hashlib.sha1(json.dumps([kind, sorted(g.items())], default=str).encode()).hexdigest()[:10]


def _nearest(vals: tuple, x) -> int:
    if x in vals:
        return vals.index(x)
    if isinstance(vals[0], str):
        return 0
    return min(range(len(vals)), key=lambda i: abs(float(vals[i]) - float(x)))


def _mutate_genes(kind: str, g: dict, rng: random.Random, n: int) -> dict:
    out = dict(g)
    for gene in rng.sample(list(GENES[kind]), n):
        vals = GENES[kind][gene]
        i = _nearest(vals, out[gene])
        j = min(len(vals) - 1, max(0, i + rng.choice((-1, 1)))) if len(vals) > 2 else 1 - i
        out[gene] = vals[j]
    if kind == "firsat" and out["max_chg"] <= out["min_chg"]:
        out["max_chg"] = next((v for v in FIRSAT_GENES["max_chg"] if v > out["min_chg"]), out["min_chg"] + 4)
    if kind == "mikro" and out["max_price"] <= out["min_price"]:
        out["max_price"] = MIKRO_GENES["max_price"][-1]
    return out


def genes_paper(s: dict) -> dict:
    """Opportunity-bot genes of the paper settings."""
    g = {gene: s.get(key) for gene, key in FIRSAT_TO_PAPER.items()}
    for k in ("mixed", "cut10", "cull30"):
        g[k] = 1 if g[k] else 0
    for k in ("min_score", "min_chg", "max_chg", "min_rvol", "min_price", "min_dvol", "sl", "tp", "lock", "day_stop"):
        g[k] = float(g[k] or 0)
    g["max_trades"] = int(g["max_trades"] or 0)
    return g


def _incumbent(kind: str) -> dict:
    if kind == "firsat":
        return genes_paper(_pkg("paper").settings())
    return dict(_pkg("micro_paper").settings())


def genes_text(kind: str, g: dict) -> str:
    def n(x, d=1):
        return f"{float(x):.{d}f}".rstrip("0").rstrip(",.").replace(".", ",") if d else f"{float(x):.0f}"
    p = []
    if kind == "firsat":
        p.append(f"skor ≥ {n(g['min_score'], 0)}")
        p.append("yükseliş + karışık" if g["mixed"] else "yükseliş")
        p.append(f"fiyat ≥ {n(g['min_price'], 0)} $")
    else:
        p.append(f"fiyat {n(g['min_price'])}–{n(g['max_price'])} $")
    p.append(f"değişim %{n(g['min_chg'])}–{n(g['max_chg'])}")
    if g["min_rvol"]:
        p.append(f"göreli hacim ≥ {n(g['min_rvol'])}x")
    p.append(f"hacim ≥ {n(g['min_dvol'] / 1e6, 0)} M$/gün")
    p.append(f"zarar durdur %{n(g['sl'], 2)}, kâr al %{n(g['tp'])}")
    if g.get("cut10"):
        p.append("10 dk kesme")
    if g.get("cull30"):
        p.append("30 dk eleme")
    if g.get("lock"):
        p.append(f"kâr kilidi %{n(g['lock'])}")
    if g.get("day_stop"):
        p.append(f"gün içi eleme −%{n(g['day_stop'])}")
    p.append(f"{g['start']}–{g['last']} ET, günde ≤ {g['max_trades']}")
    return " · ".join(p)


GENE_TR = {"min_score": "min. skor", "mixed": "karışık yönler", "min_chg": "min. değişim %", "max_chg": "maks. değişim %",
           "min_rvol": "min. göreli hacim", "min_price": "min. fiyat", "max_price": "maks. fiyat", "min_dvol": "min. hacim (M$)",
           "sl": "zarar durdur %", "tp": "kâr al %", "cut10": "10 dk kesme", "cull30": "30 dk eleme", "lock": "kâr kilidi %",
           "start": "ilk giriş", "last": "son giriş", "max_trades": "günlük işlem", "day_stop": "gün içi eleme %"}


def genes_diff(a: dict, b: dict) -> list[str]:
    def v(k, x):
        if k in ("mixed", "cut10", "cull30"):
            return "açık" if x else "kapalı"
        if k == "min_dvol":
            return f"{float(x) / 1e6:g}"
        return str(x).replace(".", ",") if not isinstance(x, float) else f"{x:g}".replace(".", ",")
    return [f"{GENE_TR.get(k, k)}: {v(k, a.get(k))} → {v(k, b.get(k))}" for k in GENES_ORDER if a.get(k) != b.get(k) and k in b]


GENES_ORDER = list(dict.fromkeys(list(FIRSAT_GENES) + list(MIKRO_GENES)))


def _seed_variants(kind: str, base: dict, rng: random.Random) -> list[tuple[str, dict]]:
    out = [("mevcut ayar", dict(base))]
    if kind == "firsat":   # the idea from the Gemini session: 30-minute cull, 10-minute cut, profit lock
        out.append(("30 dk / 10 dk / kâr kilidi", dict(base, cut10=1, cull30=1, lock=1.0)))
    for gene, vals in GENES[kind].items():
        for v in vals:
            if v != base.get(gene):
                g = dict(base, **{gene: v})
                if kind == "firsat" and g["max_chg"] <= g["min_chg"]:
                    continue
                if kind == "mikro" and g["max_price"] <= g["min_price"]:
                    continue
                out.append(("tek değişiklik", g))
    seen, uniq = set(), []
    for origin, g in out:
        i = _vid(kind, g)
        if i not in seen:
            seen.add(i)
            uniq.append((origin, g))
    tries = 0
    while len(uniq) < VAR_POP and tries < 500:
        tries += 1
        g = _mutate_genes(kind, base, rng, 2)
        i = _vid(kind, g)
        if i not in seen:
            seen.add(i)
            uniq.append(("iki değişiklik", g))
    return uniq[:VAR_POP]


def _load_variants(kind: str, alive_only: bool = True) -> list[dict]:
    q = "SELECT * FROM variants WHERE kind=?" + (" AND status!='elendi'" if alive_only else "")
    out = []
    for r in store.conn().execute(q, (kind,)):
        v = dict(r)
        v["genes"] = json.loads(v["genes"])
        v["stats"] = json.loads(v["stats"]) if v["stats"] else {}
        out.append(v)
    return out


def _save_variants(kind: str, vs: list[dict]) -> None:
    now = now_ts()
    with store.tx() as c:
        for v in vs:
            c.execute("INSERT OR REPLACE INTO variants(kind, id, status, parent, origin, born, genes, stats, score, updated)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (kind, v["id"], v["status"], v.get("parent"), v.get("origin"), v.get("born"),
                       json.dumps(v["genes"], ensure_ascii=False), json.dumps(v.get("stats") or {}, ensure_ascii=False),
                       v.get("score"), now))


def _ensure_variants(kind: str, today: str, rng: random.Random) -> list[dict]:
    vs = _load_variants(kind)
    inc = _incumbent(kind)
    if not vs:
        vs = [{"id": _vid(kind, g), "status": "yarışıyor", "origin": o, "born": today, "genes": g, "stats": {}}
              for o, g in _seed_variants(kind, inc, rng)]
        _save_variants(kind, vs)
    iid = _vid(kind, inc)
    if not any(v["id"] == iid for v in vs):     # the settings were changed by hand: they join the race too
        v = {"id": iid, "status": "yarışıyor", "origin": "elle ayarlanan", "born": today, "genes": inc, "stats": {}}
        _save_variants(kind, [v])
        vs.append(v)
    return vs


def _score_variants(kind: str, vs: list[dict], days: list[str], now: int, n0: int) -> dict[str, list]:
    """Replay every variant on every window day; fills v["stats"] / v["score"]; returns day -> per-variant results."""
    ps = _pkg("paper").settings()
    memo: dict = {}
    per_day: dict[str, list] = {}
    for v in vs:
        v["_days"] = []
    for diso in days:
        d = date.fromisoformat(diso)
        rows = _cand_rows(kind, diso)
        if not rows:
            continue
        loose = _loosest(kind, vs)
        syms = sorted({r[1] for r in rows if _passes(kind, loose, r, 600)})
        bars = bars_for(syms, d, now) if syms else {}
        res_day = []
        for v in vs:
            r = _sim_day(kind, v["genes"], rows, lambda s: bars.get(s) or [], d, now, _frac(kind, v["genes"], ps), memo)
            v["_days"].append((diso, r))
            res_day.append((v["id"], r))
        per_day[diso] = res_day
    for v in vs:
        rs = [r["ret"] for _, r in v["_days"]]
        tr = [t for _, r in v["_days"] for t in r["trades"]]
        closed = [t for t in tr if not t["open"]]
        nd = len(rs)
        v["stats"] = {"days": nd, "n": len(closed), "open": len(tr) - len(closed), "sum_pct": round(sum(rs) * 100, 3),
                      "daily_pct": round(sum(rs) / nd * 100, 3) if nd else None,
                      "avg_trade_pct": round(sum(t["ret"] for t in closed) / len(closed) * 100, 3) if closed else None,
                      "win_rate": round(sum(1 for t in closed if t["ret"] > 0) / len(closed) * 100, 1) if closed else None,
                      "elim_days": sum(1 for _, r in v["_days"] if r["elim"]),
                      "exits": _count([t["why"] for t in closed]),
                      "last": _day_summary(v["_days"][-1]) if v["_days"] else None}
        v["stats"]["autopsy"] = autopsy(tr)
        v["score"] = round(sum(rs) / (n0 + nd) * 100, 4) if nd else 0.0
        v.pop("_days", None)
    return per_day


def autopsy(trades: list[dict]) -> dict:
    """Why the shadow trades lost. Every trade's own closing-price marks give how far it went in our favour before it
    ended (MFE). `best_avg_pct` is the average best price reached: what a perfect exit would have earned (an upper
    bound, nobody sells at the very top)."""
    closed = [t for t in trades if not t.get("open")]
    out = {"trades": len(closed), "losses": 0, "by_why": {}, "gave_back": 0, "never_up": 0,
           "avg_pct": None, "best_avg_pct": None}
    if not closed:
        return out
    best = []
    for t in closed:
        mfe = max([c for _, c in t["marks"]] + [t["entry"]]) / t["entry"] - 1
        best.append(max(mfe, t["ret"]))
        if t["ret"] < 0:
            out["losses"] += 1
            out["by_why"][t["why"]] = out["by_why"].get(t["why"], 0) + 1
            if mfe >= 0.01:
                out["gave_back"] += 1          # was at least +1 % in profit first, then lost
            elif mfe < 0.005:
                out["never_up"] += 1           # never got above +0.5 %: wrong from the start
    out["avg_pct"] = round(sum(t["ret"] for t in closed) / len(closed) * 100, 3)
    out["best_avg_pct"] = round(sum(best) / len(best) * 100, 3)
    return out


def _count(xs: list) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def _day_summary(item) -> dict:
    diso, r = item
    return {"day": diso, "ret_pct": round(r["ret"] * 100, 3), "n": len(r["trades"]),
            "elim": _et(r["elim"]).strftime("%H:%M") if r["elim"] else None,
            "trades": [{"symbol": t["symbol"], "t0": t["t0"], "ret_pct": round(t["ret"] * 100, 2), "why": t["why"]}
                       for t in r["trades"][:8]]}


def _loosest(kind: str, vs: list[dict]) -> dict:
    """A filter every variant's entries pass (used to fetch bars only for symbols some variant could buy)."""
    gs = [v["genes"] for v in vs]
    g = {"start": "00:00", "last": "23:59", "min_chg": min(x["min_chg"] for x in gs), "max_chg": max(x["max_chg"] for x in gs),
         "min_rvol": 0.0, "min_dvol": min(x["min_dvol"] for x in gs), "min_price": min(x["min_price"] for x in gs)}
    if kind == "firsat":
        g.update(mixed=1, min_score=min(x["min_score"] for x in gs))
    else:
        g.update(max_price=max(x["max_price"] for x in gs))
    return g


def _nightly_variants(kind: str, day: date, sp: dict, now: int, auto: bool, rng: random.Random, progress=None) -> dict:
    today = day.isoformat()
    vs = _ensure_variants(kind, today, rng)
    days = [d for d in _cand_days(kind) if d <= today][-WINDOW_DAYS:]
    inc = _incumbent(kind)
    iid = _vid(kind, inc)
    out = {"days": len(days), "window": [days[0], days[-1]] if days else None, "variants": len(vs), "changes": []}
    if not days:
        out["note"] = "Henüz kayıtlı aday yok: seans açıkken her dakika kaydedilir, ilk sonuçlar ilk seansın akşamı gelir."
        for v in vs:
            v["status"] = "şampiyon" if v["id"] == iid else ("yarışıyor" if v["status"] != "elendi" else v["status"])
        _save_variants(kind, vs)
        return out
    if progress:
        progress(f"{KIND_TR[kind]}: {len(vs)} ayar {len(days)} günde yeniden oynatılıyor…")
    per_day = _score_variants(kind, vs, days, now, sp["n0"])
    _record_forward(kind, today, iid, per_day.get(today))
    enough = [v for v in vs if v["stats"]["n"] >= sp["min_results"] and v["stats"]["days"] >= sp["min_days"]]
    inc_v = next((v for v in vs if v["id"] == iid), None)
    champ = inc_v
    best = max(enough, key=lambda v: v["score"]) if enough else None
    if best is not None and best is not inc_v and best["score"] > 0 and best["score"] > (inc_v["score"] if inc_v else -1e9) + SWITCH_MARGIN:
        champ = best
        out["changes"].append({"kind": "ayar", "from": inc, "to": best["genes"], "diff": genes_diff(inc, best["genes"]),
                               "evidence": dict(best["stats"], score=best["score"]),
                               "incumbent": dict(inc_v["stats"], score=inc_v["score"]) if inc_v else None,
                               "applied": bool(auto)})
        if auto:
            _apply_genes(kind, best["genes"], genes_diff(inc, best["genes"]))
    # elimination: the weakest fifth that already has enough evidence and loses money (never the champion)
    ranked = sorted(vs, key=lambda v: -v["score"])
    k = int(len(ranked) * ELIM_SHARE)
    enough_ids = {v["id"] for v in enough}
    gone = [v for v in reversed(ranked) if v is not champ and v is not inc_v and v["id"] in enough_ids and v["score"] < 0][:k]
    for v in gone:
        v["status"] = "elendi"
        out["changes"].append({"kind": "elendi", "id": v["id"], "text": genes_text(kind, v["genes"]), "score": v["score"]})
    alive = [v for v in vs if v["status"] != "elendi"]
    # cloning: the best fifth (with at least one replayed day) gets mutated children until the population is full
    parents = [v for v in sorted(alive, key=lambda v: -v["score"]) if v["stats"]["days"] >= 1][:max(1, int(len(alive) * ELIM_SHARE))]
    ids = {v["id"] for v in _load_variants(kind, alive_only=False)}
    born, tries = [], 0
    while len(alive) + len(born) < VAR_POP and parents and tries < 400:
        tries += 1
        p = parents[tries % len(parents)]
        if rng.random() < 0.3 and len(parents) > 1:
            q = rng.choice(parents)
            g = {gene: (p["genes"] if rng.random() < 0.5 else q["genes"])[gene] for gene in GENES[kind]}
            g = _mutate_genes(kind, g, rng, 1)
        else:
            g = _mutate_genes(kind, p["genes"], rng, rng.choice((1, 2)))
        i = _vid(kind, g)
        if i in ids:
            continue
        ids.add(i)
        born.append({"id": i, "status": "yarışıyor", "origin": "klon", "parent": p["id"], "born": today, "genes": g, "stats": {}})
    if born:
        _score_variants(kind, born, days, now, sp["n0"])      # replayed on the same window right away
        out["changes"] += [{"kind": "klon", "id": v["id"], "parent": v["parent"], "text": genes_text(kind, v["genes"]),
                            "score": v["score"]} for v in born]
    for v in alive + born:
        v["status"] = "şampiyon" if v is champ else "yarışıyor"
    _save_variants(kind, vs + born)
    top = sorted(alive + born, key=lambda v: -v["score"])
    out.update(champion=champ["id"] if champ else None, champion_text=genes_text(kind, champ["genes"]) if champ else "",
               champion_stats=dict(champ["stats"], score=champ["score"]) if champ else None,
               top=[_variant_row(kind, v) for v in top[:5]], eliminated=len(gone), born=len(born))
    return out


FORWARD_KEEP = 250


def _record_forward(kind: str, day: str, iid: str, res_day: list | None) -> None:
    """Honest forward test of the selection itself. The setting in effect today (`iid`) was chosen last night from
    earlier days only, so today's replayed result is out of sample. It is compared with the average and the median
    variant of the same day (= picking a setting at random) and with the hindsight best (unreachable, reference).
    The first result of a day is kept: a re-run after an applied change would already have seen today."""
    if not res_day:
        return
    log_ = store.kv_get(f"learn_forward_{kind}") or []
    if any(x.get("day") == day for x in log_):
        return
    rets = {vid: float(r["ret"]) for vid, r in res_day}
    if iid not in rets:
        return
    vals = sorted(rets.values())
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    inc_r = next(r for vid, r in res_day if vid == iid)
    log_.append({"day": day, "champion": iid, "ret_pct": round(rets[iid] * 100, 4),
                 "mean_pct": round(sum(vals) / n * 100, 4), "median_pct": round(median * 100, 4),
                 "best_pct": round(vals[-1] * 100, 4), "variants": n, "trades": len(inc_r["trades"])})
    log_.sort(key=lambda x: x["day"])
    store.kv_set(f"learn_forward_{kind}", log_[-FORWARD_KEEP:])


def forward_summary(kind: str) -> dict:
    """Cumulative out-of-sample result of the setting selection vs a randomly picked setting."""
    log_ = store.kv_get(f"learn_forward_{kind}") or []
    n = len(log_)
    if not n:
        return {"days": 0, "rows": []}
    sel = sum(x["ret_pct"] for x in log_)
    avg = sum(x["mean_pct"] for x in log_)
    beat = sum(1 for x in log_ if x["ret_pct"] > x["mean_pct"])
    return {"days": n, "sum_pct": round(sel, 3), "mean_sum_pct": round(avg, 3), "edge_pct": round(sel - avg, 3),
            "edge_daily_pct": round((sel - avg) / n, 4), "beat_days": beat, "beat_share": round(beat / n * 100, 1),
            "reliable": n >= 20, "rows": log_[-30:][::-1]}


def _variant_row(kind: str, v: dict) -> dict:
    return {"id": v["id"], "status": v["status"], "origin": v.get("origin"), "parent": v.get("parent"), "born": v.get("born"),
            "text": genes_text(kind, v["genes"]), "genes": v["genes"], "stats": v.get("stats") or {}, "score": v.get("score")}


def _apply_genes(kind: str, g: dict, diff: list[str]) -> None:
    paper = _pkg("paper")
    if kind == "firsat":
        upd = {key: g[gene] for gene, key in FIRSAT_TO_PAPER.items()}
        for k in ("bot_direction_mixed", "bot_cut10", "bot_cull30"):
            upd[k] = bool(upd[k])
        paper.update_settings(upd)
        paper._log("info", "", "Öğrenme: fırsat botu ayarları güncellendi — " + "; ".join(diff)[:420])
    else:
        _pkg("micro_paper").update_settings(g, note="Öğrenme: " + "; ".join(diff))


# ---------------------------------------------------------------- evolution lineage

def _bot_sig(b: dict) -> str:
    return json.dumps([sorted([c[0], c[1], round(float(c[2]), 4)] for c in b["conds"]), b["side"], list(b["stop"]),
                       b["ratio"], b["hold"]])


def _bot_id(b: dict) -> str:
    return hashlib.sha1(_bot_sig(b).encode()).hexdigest()[:10]


def _idx(b: dict) -> tuple[int, int, int]:
    return evolve.STOP_SET.index(tuple(b["stop"])), evolve.RATIO_SET.index(b["ratio"]), evolve.HOLD_SET.index(b["hold"])


def _g(b: dict) -> dict:
    s, r, h = _idx(b)
    return {"conds": [], "side": b["side"], "stop": s, "ratio": r, "hold": h}


def _mask(pool, b: dict) -> np.ndarray:
    m = np.ones(pool.n, dtype=bool)
    for feat, op, thr, sparse in b["conds"]:
        x = pool.F[feat]
        m &= (x > thr) if op == ">" else (x < thr)
        if sparse:
            m &= x != 0
    return m


def _fit(s: dict, b: dict) -> float:
    n, nd = s.get("n", 0), s.get("days", 0)
    if n < evolve.MIN_TRADES or nd < evolve.MIN_DAYS:
        return -5 + 2.5 * min(1, n / evolve.MIN_TRADES) + 2.5 * min(1, nd / evolve.MIN_DAYS)
    return s["t"] * (0.4 + 0.6 * s["pos_years"]) - 0.25 * (len(b["conds"]) - 1)


def _exam(pool, b: dict, m: np.ndarray | None = None) -> dict:
    m = _mask(pool, b) if m is None else m
    hist = evolve.stats(pool, _g(b), "all", m)
    lock = evolve.stats(pool, _g(b), "lock", m)
    return {"hist": hist, "lock": lock, "fit": round(_fit(hist, b), 3)}


def _describe(b: dict) -> str:
    fmt = evolve._fmt
    parts = []
    for feat, op, t, _sp in b["conds"]:
        label, unit = evolve.COND_FEATURES[feat]
        sign = "-" if t < 0 else ""
        if feat == "dvol":
            t = 10 ** t
        if unit == "flag":
            parts.append(f"{label}: {'evet' if op == '>' else 'hayır'}")
            continue
        val = {"pct": f"{sign}%{fmt(abs(t) * 100)}", "rsi": fmt(t * 100, 0), "x": f"{fmt(t, 1)}x", "vix": fmt(t * 100, 1),
               "pctraw": f"{sign}%{fmt(abs(t), 1)}", "int": f"{t:.0f}", "num": fmt(t), "share": f"%{fmt(t * 100, 0)}",
               "usd": f"{fmt(t / 1e6, 0)} M$"}.get(unit, fmt(t))
        parts.append(f"{label} {op} {val}")
    kind, x = b["stop"]
    if kind == "pct":
        ex = f"zarar durdur %{fmt(x, 1)}, kâr al %{fmt(x * b['ratio'], 1)}"
    elif kind == "atr":
        ex = f"zarar durdur {fmt(x, 1)}×ATR, kâr al {fmt(x * b['ratio'], 1)}×ATR"
    else:
        ex = "zarar durdur yok"
    return f"{' VE '.join(parts)} → {'Yükseliş' if b['side'] > 0 else 'Düşüş'} · {ex} · {b['hold']} gün"


def _live_outcome(pool, idx: np.ndarray, b: dict):
    """Like Pool.outcome, but a trade whose price path ends before its exit (the most recent days) stays open with its
    last close as mark instead of being dropped. Returns (net return | NaN while open, open flag, mark)."""
    stop_i, ratio_i, hold_i = _idx(b)
    side = b["side"]
    kind, xk = evolve.STOP_SET[stop_i]
    hold_d = evolve.HOLD_SET[hold_i]

    def r_(a):
        a = a[idx, :hold_d]
        return np.where(a == evolve.PATH_NAN, np.nan, 1 + a.astype(np.float32) / 10000)

    O, H, L, C = r_(pool.O), r_(pool.H), r_(pool.L), r_(pool.C)
    slip = pool.slip[idx]
    n = len(idx)
    if kind == "pct":
        sl = np.full(n, xk / 100, dtype=np.float32)
    elif kind == "atr":
        sl = np.clip(np.nan_to_num(pool.atr[idx], nan=0.02) * xk, 0.003, 0.25)
    else:
        sl = np.full(n, np.inf, dtype=np.float32)
    tp = sl * evolve.RATIO_SET[ratio_i]
    entry = 1 + slip * side
    stop_p, tgt_p = entry * (1 - sl * side), entry * (1 + tp * side)
    res = np.full(n, np.nan, dtype=np.float32)
    mark = np.full(n, np.nan, dtype=np.float32)
    live = np.ones(n, dtype=bool)
    open_ = np.zeros(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        for j in range(hold_d):
            avail = ~np.isnan(C[:, j])
            open_ |= live & ~avail
            live &= avail
            oj, hj, lj, cj = O[:, j], H[:, j], L[:, j], C[:, j]
            if j > 0:
                for cond in ((oj <= stop_p) if side > 0 else (oj >= stop_p), (oj >= tgt_p) if side > 0 else (oj <= tgt_p)):
                    g = live & cond
                    res[g] = side * (oj[g] * (1 - slip[g] * side) / entry[g] - 1)
                    live &= ~g
            g = live & ((lj <= stop_p) if side > 0 else (hj >= stop_p))
            res[g] = -sl[g] - slip[g]
            live &= ~g
            g = live & ((hj >= tgt_p) if side > 0 else (lj <= tgt_p))
            res[g] = tp[g] - slip[g]
            live &= ~g
            mark[live] = side * (cj[live] / entry[live] - 1)
            if j == hold_d - 1:
                res[live] = side * (cj[live] * (1 - slip[live] * side) / entry[live] - 1)
                live[:] = False
    return res, open_, mark


def _live_stats(pool, b: dict, m: np.ndarray, live_mask: np.ndarray) -> dict:
    idx = np.nonzero(m & live_mask)[0]
    if not len(idx):
        return {"n": 0, "days": 0, "sum": 0.0, "open": 0}
    res, open_, mark = _live_outcome(pool, idx, b)
    ok = ~np.isnan(res)
    out = {"open": int(open_.sum()), "open_mark_pct": round(float(np.nanmean(mark[open_])) * 100, 2) if open_.any() else None}
    if not ok.any():
        return dict(out, n=0, days=0, sum=0.0)
    r = res[ok].astype(np.float64)
    days = pool.dayidx[idx][ok]
    ud, inv = np.unique(days, return_inverse=True)
    dr = np.bincount(inv, weights=r) / np.bincount(inv)
    recent = sorted(zip(idx[ok].tolist(), r.tolist()), key=lambda x: -int(pool.dayidx[x[0]]))[:6]
    out.update(n=int(len(r)), days=int(len(ud)), sum=float(dr.sum()), daily_pct=round(float(dr.mean()) * 100, 3),
               avg_pct=round(float(r.mean()) * 100, 3), win_rate=round(float((r > 0).mean()) * 100, 1),
               recent=[{"symbol": pool.sym_names[int(pool.symcode[i])], "day": pool.udates[int(pool.dayidx[i])],
                        "ret_pct": round(x * 100, 2)} for i, x in recent])
    return out


def _posterior(b: dict, live: dict, n0: int) -> tuple[float, float]:
    """(score: expected daily net return in %, referee weight)."""
    mu0 = PRIOR_HAIRCUT * float((b.get("exam") or {}).get("hist", {}).get("daily_pct") or 0) / 100
    nd = live.get("days", 0)
    mu = (n0 * mu0 + float(live.get("sum") or 0)) / (n0 + nd)
    wb = float(b.get("weight_base") or 0)
    if mu0 > 0:
        w = wb * min(2.0, max(0.0, mu / mu0))
    else:
        w = wb if mu > 0 else 0.0
    return round(mu * 100, 4), round(w, 3)


REGIME_K = 60   # days: a bot's per-regime history is shrunk toward its overall history (few fear days = little trust)


def _regime_stats(pool, b: dict, m: np.ndarray, hist_mask: np.ndarray, live_mask: np.ndarray, day_reg: np.ndarray,
                  split: str) -> dict:
    """Per regime: days traded and summed daily net return, in history (cached in the bot for this split) and live."""
    cached = b.get("regime_hist") if isinstance(b.get("regime_hist"), dict) else None
    need_hist = not cached or cached.get("split") != split
    sel = (hist_mask | live_mask) if need_hist else live_mask
    idx = np.nonzero(m & sel)[0]
    out = {r: {"h_days": 0, "h_sum": 0.0, "l_days": 0, "l_sum": 0.0} for r in evolve.REGIMES}
    if len(idx):
        res, _open, _mark = _live_outcome(pool, idx, b)
        ok = ~np.isnan(res)
        idx, r = idx[ok], res[ok].astype(np.float64)
        if len(idx):
            ud, inv = np.unique(pool.dayidx[idx], return_inverse=True)
            dr = np.bincount(inv, weights=r) / np.bincount(inv)
            day_live = np.zeros(len(ud), dtype=bool)
            day_live[inv] = live_mask[idx]
            reg = day_reg[ud]
            for i, name in enumerate(evolve.REGIMES):
                s = reg == i
                if need_hist:
                    h = s & ~day_live
                    out[name]["h_days"], out[name]["h_sum"] = int(h.sum()), float(dr[h].sum())
                lv = s & day_live
                out[name]["l_days"], out[name]["l_sum"] = int(lv.sum()), float(dr[lv].sum())
    if need_hist:
        b["regime_hist"] = {"split": split, **{k: {"h_days": v["h_days"], "h_sum": round(v["h_sum"], 6)} for k, v in out.items()}}
    else:
        for k in evolve.REGIMES:
            out[k].update(cached.get(k) or {})
    return out


def _regime_weights(b: dict, rs: dict, n0: int) -> dict:
    """Referee weight per regime, the same posterior as `_posterior` but on the bot's record in that regime: its
    regime history (shrunk toward its overall history) as prior, live days of that regime as evidence."""
    mu_all = float((b.get("exam") or {}).get("hist", {}).get("daily_pct") or 0) / 100
    mu0 = PRIOR_HAIRCUT * mu_all
    wb = float(b.get("weight_base") or 0)
    out = {}
    for name in evolve.REGIMES:
        s = rs.get(name) or {}
        hd, ld = int(s.get("h_days") or 0), int(s.get("l_days") or 0)
        hist_mu = (REGIME_K * mu_all + float(s.get("h_sum") or 0)) / (REGIME_K + hd)
        mu = (n0 * PRIOR_HAIRCUT * hist_mu + float(s.get("l_sum") or 0)) / (n0 + ld)
        if mu0 > 0:
            w = wb * min(2.0, max(0.0, mu / mu0))
        else:
            w = wb if mu > 0 else 0.0
        out[name] = {"w": round(w, 3), "score": round(mu * 100, 4), "h_days": hd, "l_days": ld,
                     "h_daily_pct": round(float(s.get("h_sum") or 0) / hd * 100, 3) if hd else None,
                     "l_daily_pct": round(float(s.get("l_sum") or 0) / ld * 100, 3) if ld else None}
    return out


def _load_lineage() -> list[dict]:
    out = []
    for r in store.conn().execute("SELECT * FROM lineage"):
        x = dict(r)
        x["bot"] = json.loads(x["bot"])
        x["live"] = json.loads(x["live"]) if x["live"] else {}
        out.append(x)
    return out


def _save_lineage(rows: list[dict]) -> None:
    now = now_ts()
    with store.tx() as c:
        for x in rows:
            c.execute("INSERT OR REPLACE INTO lineage(arm, id, status, parent, origin, born, bot, live, score, weight, updated)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (x["arm"], x["id"], x["status"], x.get("parent"), x.get("origin"), x.get("born"),
                       json.dumps(x["bot"], ensure_ascii=False), json.dumps(x.get("live") or {}, ensure_ascii=False),
                       x.get("score"), x.get("weight"), now))


def _from_survivor(sv: dict) -> dict:
    return {"conds": [list(c) for c in sv["conds_abs"]], "side": int(sv["side"]), "stop": list(sv["stop"]),
            "ratio": float(sv["ratio"]), "hold": int(sv["hold"]), "rule": (sv.get("rule") or {}).get("text") or "",
            "weight_base": float(sv.get("weight") or 0), "lock_tournament": sv.get("lock")}


def _init_lineage(pool, today: str, changes: list) -> list[dict]:
    base = store.kv_get("evo_survivors_base40") or store.kv_get("evo_survivors") or []
    rows = []
    for sv in base:
        b = _from_survivor(sv)
        m = _mask(pool, b)
        b["exam"] = _exam(pool, b, m)
        if b["stop"][0] != "none":
            for arm in ("ana", "evrim"):
                rows.append({"arm": arm, "id": _bot_id(b), "status": "aktif", "origin": "turnuva", "born": today, "bot": copy.deepcopy(b)})
            continue
        # no stop: the user asked for these to come back only with a stop that passes the 10-year exam
        best = None
        for si, st in enumerate(evolve.STOP_SET):
            if st[0] == "none":
                continue
            for ratio in evolve.RATIO_SET:
                c = dict(copy.deepcopy(b), stop=list(st), ratio=ratio)
                ex = _exam(pool, c, m)
                if evolve._passes(ex["hist"], 0.0) and ex["fit"] >= STOPCLONE_FIT_FLOOR * b["exam"]["fit"] and (
                        best is None or ex["fit"] > best["exam"]["fit"]):
                    c["exam"] = ex
                    best = c
        item = {"kind": "stoplu klon", "rule": b["rule"], "fit_before": b["exam"]["fit"]}
        if best is None:
            item["result"] = "geçemedi"
            changes.append(item)
            continue
        best["rule"] = _describe(best)
        best["weight_base"] = round(b["weight_base"] * min(1.25, max(0.8, best["exam"]["fit"] / max(b["exam"]["fit"], 1e-6))), 2)
        item.update(result="geçti", new_rule=best["rule"], fit_after=best["exam"]["fit"])
        changes.append(item)
        for arm in ("ana", "evrim"):
            rows.append({"arm": arm, "id": _bot_id(best), "status": "aktif", "origin": "stoplu klon", "parent": _bot_id(b),
                         "born": today, "bot": copy.deepcopy(best)})
    return rows


def _clone(pool, b: dict, rng: random.Random) -> dict:
    """Micro-mutation: the rule's backbone (features, directions) stays; thresholds move a little (±3 % of the
    stock-day distribution) and the exit may step to a neighbouring stop / target / hold."""
    c = copy.deepcopy({k: v for k, v in b.items() if k in ("conds", "side", "stop", "ratio", "hold", "weight_base")})
    for _ in range(10):
        changed = False
        for cond in c["conds"]:
            if rng.random() < 0.6:
                feat = cond[0]
                q = float(np.searchsorted(pool.Q[feat], cond[2]) / (evolve.QGRID - 1))
                cond[2] = pool.thr(feat, min(0.99, max(0.01, q + rng.gauss(0, 0.03))))
                changed = True
        stops = [s for s in evolve.STOP_SET if s[0] != "none"]
        if rng.random() < 0.3 and tuple(c["stop"]) in stops:
            i = stops.index(tuple(c["stop"]))
            c["stop"] = list(stops[min(len(stops) - 1, max(0, i + rng.choice((-1, 1))))])
            changed = True
        if rng.random() < 0.3:
            i = evolve.RATIO_SET.index(c["ratio"])
            c["ratio"] = evolve.RATIO_SET[min(len(evolve.RATIO_SET) - 1, max(0, i + rng.choice((-1, 1))))]
            changed = True
        if rng.random() < 0.2:
            i = evolve.HOLD_SET.index(c["hold"])
            c["hold"] = evolve.HOLD_SET[min(len(evolve.HOLD_SET) - 1, max(0, i + rng.choice((-1, 1))))]
            changed = True
        if changed and _bot_sig(c) != _bot_sig(b):
            break
    return c


def _member_weight(x: dict, regime: str | None) -> float:
    """Referee weight of a lineage member in a regime (its overall weight when the regime is unknown)."""
    rw = ((x.get("live") or {}).get("regime") or {}).get(regime) if regime else None
    return float(rw["w"]) if isinstance(rw, dict) and rw.get("w") is not None else float(x.get("weight") or 0)


def _arm_live(pool, members: list[dict], live_mask: np.ndarray, day_reg: np.ndarray | None = None) -> dict:
    """What the referee of one arm would have traded live: each day the 5 stocks with the most summed weight (the
    bots' weights in that day's regime), each traded by its strongest bot's exit (resolved trades only)."""
    picks: dict[tuple, list] = {}
    for x in members:
        b = x["bot"]
        idx = np.nonzero(x["_mask"] & live_mask)[0]
        if not len(idx):
            continue
        res, _open, _mark = _live_outcome(pool, idx, b)
        for i, r in zip(idx.tolist(), res.tolist()):
            d = int(pool.dayidx[i])
            code = int(day_reg[d]) if day_reg is not None else -1
            w = _member_weight(x, evolve.REGIMES[code] if code >= 0 else None)
            if w <= 0:
                continue
            key = (d, int(pool.symcode[i]))
            p = picks.setdefault(key, [0.0, -1.0, None])
            p[0] += w
            if w > p[1]:
                p[1], p[2] = w, r
    by_day: dict[int, list] = {}
    for (d, _s), (sc, _w, r) in picks.items():
        by_day.setdefault(d, []).append((sc, r))
    daily, n = [], 0
    for d, lst in sorted(by_day.items()):
        top = [r for sc, r in sorted(lst, key=lambda x: -x[0])[:5] if r is not None and not math.isnan(r)]
        if top:
            daily.append(sum(top) / len(top))
            n += len(top)
    return {"days": len(daily), "trades": n, "sum_pct": round(sum(daily) * 100, 3),
            "avg_pct": round(sum(daily) / len(daily) * 100, 3) if daily else None}


def _trading_set(rows: list[dict]) -> list[dict]:
    out = []
    for x in sorted(rows, key=lambda x: -float(x.get("weight") or 0)):
        b = x["bot"]
        out.append({"id": x["id"], "arm": x["arm"], "conds_abs": b["conds"], "side": b["side"], "stop": b["stop"],
                    "ratio": b["ratio"], "hold": b["hold"], "rule": {"text": b["rule"]}, "weight": x.get("weight"),
                    "weights_by_regime": {r: _member_weight(x, r) for r in evolve.REGIMES},
                    "score": x.get("score"), "lock": (b.get("exam") or {}).get("lock"), "run_at": now_ts()})
    return out


def _nightly_evo(day: date, sp: dict, auto: bool, rng: random.Random, progress=None) -> dict:
    today = day.isoformat()
    if progress:
        progress("Evrim kolları: 10 yıllık havuz hazırlanıyor (birkaç dakika)…")
    pool = evolve.Pool(progress=(lambda m, i, n: progress(m)) if progress else None)
    try:
        live_from = store.kv_get("learn_live_from")
        if not live_from:      # the first trading day the tournament never saw
            end = _tournament_end() or today
            live_from = next((d for d in pool.udates if d > end), today)
            store.kv_set("learn_live_from", live_from)
        # Fixed splits: the Pool's own lock is "the last 6 months" and slides forward every night, so it would
        # drift into the live window and into days already used for selection. The exams use the tournament's lock
        # start instead (kept in kv): history before it, the lock between it and the first live day, live after.
        lock_from = store.kv_get("learn_lock_start")
        if not lock_from:
            lock_from = min(_tournament_lock_start() or pool.lock_start, live_from)
            store.kv_set("learn_lock_start", lock_from)
        udates = np.array(pool.udates)
        purge = max(evolve.HOLD_SET)     # multi-day trades entered just before a boundary must not end after it
        lock_i, live_i = int(np.searchsorted(udates, lock_from)), int(np.searchsorted(udates, live_from))
        pool.set_mask("all", pool.dayidx < max(0, lock_i - purge))
        pool.set_mask("lock", (pool.dayidx >= lock_i) & (pool.dayidx < max(lock_i, live_i - purge)))
        live_mask = pool.dayidx >= live_i
        day_reg = evolve.day_regimes(pool)
        hist_mask = pool.masks["all"]

        def score_member(x: dict) -> None:
            """Live record, posterior score / weight and the per-regime scorecard of one lineage member."""
            x["live"] = _live_stats(pool, x["bot"], x["_mask"], live_mask)
            x["score"], x["weight"] = _posterior(x["bot"], x["live"], sp["n0"])
            rs = _regime_stats(pool, x["bot"], x["_mask"], hist_mask, live_mask, day_reg, lock_from)
            x["live"]["regime"] = _regime_weights(x["bot"], rs, sp["n0"])
        live_days = int(len(np.unique(pool.dayidx[live_mask]))) if live_mask.any() else 0
        changes: list = []
        rows = _load_lineage()
        if not rows:
            if progress:
                progress("Evrim kolları: ilk kurulum (turnuva botları + stoplu klonlar)…")
            rows = _init_lineage(pool, today, changes)
        # daily bots from a daily-bot tournament join both arms as trading bots next to the fear-day bots
        pending = store.kv_get("learn_pending_bots") or []
        if pending:
            known_ids = {(x["arm"], x["id"]) for x in rows}
            added = 0
            for sv in pending:
                b = _from_survivor(sv)
                b["kind"] = "günlük"
                b["exam"] = _exam(pool, b)
                bid = _bot_id(b)
                for arm in ("ana", "evrim"):
                    if (arm, bid) not in known_ids:
                        rows.append({"arm": arm, "id": bid, "status": "aktif", "origin": "günlük turnuva", "born": today,
                                     "bot": copy.deepcopy(b)})
                        added += arm == "evrim"
                changes.append({"kind": "terfi", "id": bid, "rule": b["rule"], "score": None, "why": "günlük bot turnuvasından",
                                "live": {}})
            store.kv_set("learn_pending_bots", [])
            if progress:
                progress(f"Evrim kolları: {added} günlük bot eklendi")
        # finals of the day-by-day rule walk (walk.py) join the evolving arm as candidates: live results decide
        pend_c = store.kv_get("learn_pending_cands") or []
        if pend_c:
            known_ids = {x["id"] for x in rows if x["arm"] == "evrim"}
            for pb in pend_c:
                b = {k: pb[k] for k in ("conds", "side", "stop", "ratio", "hold")}
                b["rule"] = pb.get("rule") or _describe(b)
                bid = _bot_id(b)
                if bid in known_ids:
                    continue
                b["exam"] = _exam(pool, b)
                b["weight_base"] = round(max(0.0, float(b["exam"]["hist"].get("t") or 0)), 2)
                rows.append({"arm": "evrim", "id": bid, "status": "aday", "origin": "gün gün evrim", "born": today, "bot": b})
                changes.append({"kind": "klon", "id": bid, "parent": None, "origin": "gün gün evrim", "rule": b["rule"],
                                "fit": b["exam"]["fit"], "parent_fit": None, "hist": _short_hist(b["exam"]["hist"])})
            store.kv_set("learn_pending_cands", [])
        prev_set = {x["id"] for x in rows if x["arm"] == "evrim" and x["status"] == "aktif"}
        if progress:
            progress(f"Evrim kolları: {len(rows)} botun canlı sonuçları hesaplanıyor…")
        for x in rows:
            if x["status"] in ("emekli", "red"):
                continue
            x["_mask"] = _mask(pool, x["bot"])
            score_member(x)

        def enough(x):
            return x["live"].get("n", 0) >= sp["min_results"] and x["live"].get("days", 0) >= sp["min_days"]

        evo = [x for x in rows if x["arm"] == "evrim"]
        active = [x for x in evo if x["status"] == "aktif"]
        cands = [x for x in evo if x["status"] == "aday"]
        # drop: live evidence says it loses money
        for x in sorted(active, key=lambda x: x["score"]):
            if len(active) <= EVO_ACTIVE_MIN:
                break
            if enough(x) and x["score"] < 0:
                x["status"] = "emekli"
                active.remove(x)
                changes.append({"kind": "emekli", "id": x["id"], "rule": x["bot"]["rule"], "score": x["score"],
                                "live": _short_live(x["live"])})
        # promote: candidates with enough live evidence that beat the weakest trading bot
        for x in sorted(cands, key=lambda x: -x["score"]):
            if not enough(x) or x["score"] <= 0:
                continue
            weakest = min(active, key=lambda a: a["score"]) if active else None
            if len(active) < EVO_ACTIVE_MAX:
                pass
            elif weakest is not None and x["score"] > weakest["score"]:
                weakest["status"] = "emekli"
                active.remove(weakest)
                changes.append({"kind": "emekli", "id": weakest["id"], "rule": weakest["bot"]["rule"], "score": weakest["score"],
                                "why": "daha iyi bir aday geldi", "live": _short_live(weakest["live"])})
            else:
                continue
            x["status"] = "aktif"
            active.append(x)
            cands.remove(x)
            changes.append({"kind": "terfi", "id": x["id"], "rule": x["bot"]["rule"], "score": x["score"], "live": _short_live(x["live"])})
        # candidates that never collect evidence make room
        for x in list(cands):
            age = sum(1 for d in pool.udates if d > (x.get("born") or today))
            if age >= CAND_EXPIRE_DAYS and not enough(x):
                x["status"] = "emekli"
                cands.remove(x)
                changes.append({"kind": "emekli", "id": x["id"], "rule": x["bot"]["rule"], "why": "kanıt toplayamadı"})
        # cloning: the best fifth of the trading bots, two micro-mutated children each, 10-year exam first
        if progress:
            progress("Evrim kolları: en iyi botlar klonlanıyor ve 10 yıllık sınava giriyor…")
        parents = sorted(active, key=lambda x: -x["score"])[:max(1, int(len(active) * CLONE_SHARE))]
        known = {x["id"] for x in rows}
        exams, passed = 0, 0
        for p in parents:
            p_exam = _exam(pool, p["bot"], p["_mask"])
            for _ in range(CLONES_PER_PARENT):
                c = _clone(pool, p["bot"], rng)
                cid = _bot_id(c)
                if cid in known:
                    continue
                known.add(cid)
                cm = _mask(pool, c)
                ex = _exam(pool, c, cm)
                exams += 1
                lock_ok = ex["lock"].get("n", 0) < evolve.MIN_EXAM_TRADES or (ex["lock"].get("daily_pct") or 0) >= min(0.0, p_exam["lock"].get("daily_pct") or 0)
                if not (evolve._passes(ex["hist"], 0.0) and ex["fit"] >= CLONE_FIT_FLOOR * p_exam["fit"] and lock_ok):
                    continue
                passed += 1
                c["exam"] = ex
                c["rule"] = _describe(c)
                c["weight_base"] = round(float(p["bot"].get("weight_base") or 0) * min(1.25, max(0.8, ex["fit"] / max(p_exam["fit"], 1e-6))), 2)
                x = {"arm": "evrim", "id": cid, "status": "aday", "origin": "klon", "parent": p["id"], "born": today, "bot": c, "_mask": cm}
                score_member(x)
                rows.append(x)
                cands.append(x)
                changes.append({"kind": "klon", "id": cid, "parent": p["id"], "rule": c["rule"], "fit": ex["fit"],
                                "parent_fit": p_exam["fit"], "hist": _short_hist(ex["hist"])})
        # entry filter found on 2026-10-27 (ATR > 5.7 %, forward test): the best trading bots also get a twin with it
        for p in sorted(active, key=lambda x: -x["score"])[:ATR_TWINS]:
            if any(c[0] == "atr" for c in p["bot"]["conds"]):
                continue
            t = copy.deepcopy(p["bot"])
            t["conds"] = [list(c) for c in t["conds"]] + [list(ATR_FILTER)]
            tid = _bot_id(t)
            if tid in known:
                continue
            known.add(tid)
            tm = _mask(pool, t)
            t["exam"] = _exam(pool, t, tm)
            if not evolve._passes(t["exam"]["hist"], 0.0):
                continue
            t["rule"] = _describe(t)
            t["weight_base"] = float(p["bot"].get("weight_base") or 0)
            x = {"arm": "evrim", "id": tid, "status": "aday", "origin": "atr-filtre", "parent": p["id"], "born": today,
                 "bot": t, "_mask": tm}
            score_member(x)
            rows.append(x)
            cands.append(x)
            changes.append({"kind": "klon", "id": tid, "parent": p["id"], "origin": "atr-filtre", "rule": t["rule"],
                            "fit": t["exam"]["fit"], "parent_fit": p["bot"].get("exam", {}).get("fit"),
                            "hist": _short_hist(t["exam"]["hist"])})
        if len(cands) > EVO_CAND_MAX:     # keep the best candidates (by score, then the 10-year fitness)
            for x in sorted(cands, key=lambda x: (x["score"], (x["bot"].get("exam") or {}).get("fit", 0)))[:len(cands) - EVO_CAND_MAX]:
                x["status"] = "emekli"
        # referee between the arms: the evolving arm trades unless the frozen one is clearly ahead live
        ana = [x for x in rows if x["arm"] == "ana" and x["status"] == "aktif"]
        live_ana, live_evo = _arm_live(pool, ana, live_mask, day_reg), _arm_live(pool, active, live_mask, day_reg)
        trading = "evrim"
        if (live_ana["days"] >= 2 * sp["min_days"] and live_ana["trades"] >= sp["min_results"]
                and (live_ana["avg_pct"] or 0) > (live_evo["avg_pct"] or 0) + 0.05):
            trading = "ana"
        tset = _trading_set(ana if trading == "ana" else active)
        set_changed = {x["id"] for x in active} != prev_set or store.kv_get("learn_trading_arm") != trading
        if auto:
            store.kv_set("evo_survivors", tset)
            store.kv_set("learn_trading_arm", trading)
        for x in rows:
            x.pop("_mask", None)
        _save_lineage(rows)
        try:    # the neural-network population: its forward record on the same live days (shadow, places no orders)
            if progress:
                progress("Sinir ağı botları: canlı günlerdeki sonuçlar hesaplanıyor…")
            importlib.import_module(f"{__package__}.neuro").nightly(pool, live_mask, _live_stats, sp["min_days"],
                                                                     sp["min_results"], seed=int(rng.random() * 1e6))
        except Exception:  # noqa: BLE001
            log.exception("neural network live evaluation failed")
        return {"live_from": live_from, "live_days": live_days, "changes": changes, "exams": exams, "passed": passed,
                "trading_arm": trading, "applied": bool(auto), "set_changed": set_changed,
                "arms": {"ana": dict(live_ana, bots=len(ana)), "evrim": dict(live_evo, bots=len(active), candidates=len(cands))}}
    finally:
        del pool
        gc.collect()


def reset_lineage() -> None:
    """After a new tournament: the arms are rebuilt from its finals by the next nightly run."""
    _init()
    with store.tx() as c:
        c.execute("DELETE FROM lineage")
    for k in ("learn_live_from", "learn_trading_arm", "learn_lock_start"):
        store.kv_set(k, None)


def _tournament_range(name: str, i: int) -> str | None:
    r = store.conn().execute("SELECT report FROM runs WHERE kind='evolve' ORDER BY created_at DESC LIMIT 1").fetchone()
    try:
        return json.loads(r[0])["ranges"][name][i]
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _tournament_end() -> str | None:
    return _tournament_range("data", 1)


def _tournament_lock_start() -> str | None:
    return _tournament_range("lock", 0)


def _short_live(lv: dict) -> dict:
    return {k: lv.get(k) for k in ("n", "days", "daily_pct", "avg_pct", "win_rate", "open")}


def _short_hist(h: dict) -> dict:
    return {k: h.get(k) for k in ("n", "days", "daily_pct", "avg_pct", "t", "pos_years", "win_rate")}


# ---------------------------------------------------------------- nightly job

def nightly(day: date | None = None) -> dict:
    """After the close (once the day's bars are in lab.db): live results of every bot, promotion / retirement,
    cloning with the 10-year exam, setting variants replayed and the best one applied; a version is kept for undo."""
    _init()
    t0 = time.time()
    now = now_ts()
    day = day or _last_bar_day() or _et(now).date()
    st = settings()
    sp = SPEEDS[st["speed"]]
    rng = random.Random(f"{day.isoformat()}-learn")
    NIGHT.update(state="running", message="Başlıyor…", started_at=now, finished_at=None)

    def progress(msg: str) -> None:
        NIGHT["message"] = msg

    paper = _pkg("paper")
    micro = _pkg("micro_paper")
    version = {"date": day.isoformat(), "ts": now, "evo_survivors": store.kv_get("evo_survivors"),
               "paper": {key: paper.settings()[key] for key in FIRSAT_TO_PAPER.values()}, "micro": micro.settings()}
    report: dict = {"date": day.isoformat(), "ts": now, "speed": st["speed"], "auto_apply": st["auto_apply"]}
    for name, fn in (("evo", lambda: _nightly_evo(day, sp, st["auto_apply"], rng, progress)),
                     ("firsat", lambda: _nightly_variants("firsat", day, sp, now, st["auto_apply"], rng, progress)),
                     ("mikro", lambda: _nightly_variants("mikro", day, sp, now, st["auto_apply"], rng, progress))):
        try:
            report[name] = fn()
        except Exception as e:  # noqa: BLE001 - one arm failing must not stop the others
            log.exception("learn nightly %s failed", name)
            report[name] = {"error": str(e)}
    changed = bool(obj(report.get("evo")).get("set_changed")) or any(
        c.get("kind") == "ayar" and c.get("applied") for k in ("firsat", "mikro") for c in obj(report.get(k)).get("changes", []))
    if st["auto_apply"] and changed:
        vers = store.kv_get("learn_versions") or []
        vers.insert(0, version)
        store.kv_set("learn_versions", vers[:20])
    report["seconds"] = round(time.time() - t0, 1)
    store.kv_set("learn_report", report)
    hist = store.kv_get("learn_history") or []
    hist = [h for h in hist if h.get("date") != report["date"]]
    hist.insert(0, _history_item(report))
    store.kv_set("learn_history", hist[:60])
    store.kv_set("learn_night", day.isoformat())
    _prune(day)
    NIGHT.update(state="done", message=f"{day.strftime('%d.%m.%Y')} gece öğrenmesi tamamlandı ({report['seconds'] / 60:.1f} dk)",
                 finished_at=now_ts())
    return report


def obj(x) -> dict:
    return x if isinstance(x, dict) else {}


def _history_item(rep: dict) -> dict:
    evo = obj(rep.get("evo"))
    ch = evo.get("changes") or []
    item = {"date": rep["date"], "ts": rep["ts"], "evo": {k: sum(1 for c in ch if c.get("kind") == k) for k in ("klon", "terfi", "emekli")},
            "trading_arm": evo.get("trading_arm"), "arms": evo.get("arms")}
    for k in ("firsat", "mikro"):
        r = obj(rep.get(k))
        item[k] = {"champion": r.get("champion"), "text": r.get("champion_text"), "stats": r.get("champion_stats"),
                   "switched": any(c.get("kind") == "ayar" for c in r.get("changes") or []), "days": r.get("days")}
    return item


def _prune(day: date) -> None:
    cut = (day - timedelta(days=KEEP_DAYS * 7 // 5)).isoformat()
    with store.tx() as c:
        c.execute("DELETE FROM cands WHERE day < ?", (cut,))
        c.execute("DELETE FROM mbars WHERE day < ?", (cut,))
        c.execute("DELETE FROM variants WHERE status='elendi' AND born < ?", (cut,))


def rollback() -> dict:
    """Undo the last applied change (trading set and bot settings) and stop automatic applying until it is turned on
    again; learning itself goes on in shadow."""
    vers = store.kv_get("learn_versions") or []
    if not vers:
        raise RuntimeError("Geri alınacak bir değişiklik yok")
    v = vers.pop(0)
    if v.get("evo_survivors") is not None:
        store.kv_set("evo_survivors", v["evo_survivors"])
    paper = _pkg("paper")
    if v.get("paper"):
        paper.update_settings(v["paper"])
    if v.get("micro"):
        _pkg("micro_paper").update_settings(v["micro"], note="Öğrenme değişikliği geri alındı")
    store.kv_set("learn_versions", vers)
    update_settings({"auto_apply": False})
    paper._log("info", "", f"Öğrenme: {v.get('date')} değişikliği geri alındı; otomatik uygulama durduruldu (gölgede öğrenme sürüyor)")
    return {"ok": True, "restored": v.get("date")}


# ---------------------------------------------------------------- intraday (every 30 minutes in the session)

def evo_open() -> dict:
    """Shortly after the open: which bot fires on which stock today (for the live scoreboard; the nightly job scores
    them on the official daily bars)."""
    _init()
    data = evo_live.today_rows()
    members = [x for x in _load_lineage() if x["status"] in ("aktif", "aday")]
    fires, opens = {}, {}
    for row in data["rows"]:
        for x in members:
            if evo_live._fires(row["f"], x["bot"]["conds"]):
                fires.setdefault(f"{x['arm']}:{x['id']}", []).append(row["symbol"])
                opens[row["symbol"]] = row["open"]
    sides = {f"{x['arm']}:{x['id']}": x["bot"]["side"] for x in members}
    try:
        nn_picks = importlib.import_module(f"{__package__}.neuro").score_today(data["rows"])
        for m in nn_picks:
            if m.get("picks"):
                k = f"sinir:{m['id']}"
                fires[k] = [p["symbol"] for p in m["picks"]]
                sides[k] = 1
                for p in m["picks"]:
                    opens[p["symbol"]] = p["open"]
    except Exception:  # noqa: BLE001
        log.exception("learn evo_open neural picks failed")
    out = {"date": data["date"], "ts": data["ts"], "vix": data.get("vix"), "opens": opens, "fires": fires,
           "sides": sides}
    store.kv_set("learn_evo_today", out)
    store.kv_set("learn_evo_open", data["date"])
    return out


def intraday(now: int | None = None) -> dict:
    """Live scoreboard: every setting variant replayed on today's candidates so far (1-minute bars), the intraday
    stop applied at each half hour; the evolution bots' signals of the morning marked at the latest prices."""
    _init()
    now = now or now_ts()
    d = _et(now).date()
    ps = _pkg("paper").settings()
    out: dict = {"ts": now, "day": d.isoformat()}
    for kind in ("firsat", "mikro"):
        try:
            vs = _load_variants(kind)
            rows = _cand_rows(kind, d.isoformat())
            if not vs or not rows:
                out[kind] = {"rows": [], "candidates": len({r[1] for r in rows})}
                continue
            loose = _loosest(kind, vs)
            syms = sorted({r[1] for r in rows if _passes(kind, loose, r, 600)})
            bars = bars_for(syms, d, now) if syms else {}
            memo: dict = {}
            res = []
            for v in vs:
                r = _sim_day(kind, v["genes"], rows, lambda s: bars.get(s) or [], d, now, _frac(kind, v["genes"], ps), memo)
                res.append({"id": v["id"], "status": v["status"], "text": genes_text(kind, v["genes"]),
                            "ret_pct": round(r["ret"] * 100, 3), "n": len(r["trades"]), "open": sum(1 for t in r["trades"] if t["open"]),
                            "elim": _et(r["elim"]).strftime("%H:%M") if r["elim"] else None,
                            "trades": [{"symbol": t["symbol"], "ret_pct": round(t["ret"] * 100, 2), "why": t["why"]} for t in r["trades"][:6]]})
            res.sort(key=lambda x: -x["ret_pct"])
            out[kind] = {"rows": res, "candidates": len({r[1] for r in rows}), "eliminated": sum(1 for x in res if x["elim"]),
                         "champion": next((x for x in res if x["status"] == "şampiyon"), None)}
        except Exception as e:  # noqa: BLE001
            log.exception("learn intraday %s failed", kind)
            out[kind] = {"error": str(e)}
    out["evo"] = _evo_marks(now, d)
    store.kv_set("learn_live", out)
    LIVE_STATE["last"] = now
    return out


def _evo_marks(now: int, d: date) -> dict:
    today = store.kv_get("learn_evo_today") or {}
    if today.get("date") != d.isoformat() or not today.get("fires"):
        return {"signals": 0, "vix": today.get("vix") if today.get("date") == d.isoformat() else None}
    syms = sorted(today["opens"])[:400]
    q = _pkg("paper").quotes(syms)
    arms: dict = {}
    best = []
    for key, lst in today["fires"].items():
        arm = key.split(":")[0]
        side = today["sides"].get(key, 1)
        gains = [side * (q[s]["price"] / today["opens"][s] - 1) for s in lst if s in q and q[s].get("price") and today["opens"].get(s)]
        a = arms.setdefault(arm, {"bots": 0, "signals": 0, "sum": 0.0})
        a["bots"] += 1
        a["signals"] += len(gains)
        a["sum"] += sum(gains)
    for s in syms:
        if s in q and q[s].get("price"):
            best.append({"symbol": s, "ret_pct": round((q[s]["price"] / today["opens"][s] - 1) * 100, 2)})
    for a in arms.values():
        a["avg_pct"] = round(a.pop("sum") / a["signals"] * 100, 2) if a["signals"] else None
    best.sort(key=lambda x: -x["ret_pct"])
    return {"signals": len(syms), "vix": today.get("vix"), "arms": arms, "top": best[:5], "bottom": best[-5:][::-1]}


# ---------------------------------------------------------------- scheduling (lab runner, every 2 minutes)

def _run_open() -> None:
    try:
        evo_open()
    except Exception:  # noqa: BLE001
        log.exception("learn evo_open failed")
        store.kv_set("learn_evo_open", _et(now_ts()).date().isoformat())


def _run_intraday() -> None:
    try:
        intraday()
    except Exception:  # noqa: BLE001
        log.exception("learn intraday failed")


def _run_nightly(day: date) -> None:
    try:
        nightly(day)
    except Exception as e:  # noqa: BLE001
        log.exception("learn nightly failed")
        NIGHT.update(state="error", message=f"Hata: {e}", finished_at=now_ts())
        store.kv_set("learn_night_error", {"date": day.isoformat(), "ts": now_ts(), "error": str(e)})


def tick(now: int, start, running) -> dict:
    _init()
    et = _et(now)
    d, minute = et.date(), et.hour * 60 + et.minute
    session = market_session(now)
    out: dict = {}
    if is_trading_day(d) and session == "open":
        if minute >= 9 * 60 + 40 and store.kv_get("learn_evo_open") != d.isoformat() and not running("learn_open"):
            if start("learn_open", _run_open):
                out["open"] = "started"
        if minute >= 10 * 60 and now - LIVE_STATE["last"] >= INTRADAY_EVERY - 60 and not running("learn_intraday"):
            LIVE_STATE["last"] = now
            if start("learn_intraday", _run_intraday):
                out["intraday"] = "started"
    last = _last_bar_day()
    err = store.kv_get("learn_night_error") or {}
    retry_ok = err.get("date") != (last.isoformat() if last else None) or now - int(err.get("ts") or 0) > 3 * 3600
    if (last and store.kv_get("learn_night") != last.isoformat() and session != "open" and retry_ok
            and not (last == d and minute < 16 * 60 + 50) and not running("download") and not running("learn_nightly")
            and not running("evolve") and not running("nn_evolve") and not running("rule_walk") and not running("deepnet")):
        if start("learn_nightly", _run_nightly, last):
            out["nightly"] = "started"
    return out


def start_now(start, running) -> dict:
    if running("learn_nightly"):
        return {"ok": False, "message": "Gece öğrenmesi zaten çalışıyor"}
    if running("download") or running("evolve") or running("nn_evolve"):
        return {"ok": False, "message": "Önce çalışan veri indirme / turnuva / sinir ağı evriminin bitmesini bekleyin"}
    last = _last_bar_day()
    if not last:
        return {"ok": False, "message": "Laboratuvar verisi yok"}
    start("learn_nightly", _run_nightly, last)
    return {"ok": True, "day": last.isoformat()}


# ---------------------------------------------------------------- read side (API)

def status(running=None) -> dict:
    _init()
    st = settings()
    lin = _load_lineage()

    def member(x):
        b = x["bot"]
        ex = b.get("exam") or {}
        return {"arm": x["arm"], "id": x["id"], "status": x["status"], "origin": x.get("origin"), "parent": x.get("parent"),
                "born": x.get("born"), "rule": b.get("rule"), "score": x.get("score"), "weight": x.get("weight"),
                "weight_base": b.get("weight_base"), "fit": ex.get("fit"), "hist": _short_hist(ex.get("hist") or {}),
                "lock": _short_hist(ex.get("lock") or {}), "live": x.get("live") or {}}

    order = {"aktif": 0, "aday": 1, "emekli": 2, "red": 3}
    members = sorted((member(x) for x in lin), key=lambda m: (m["arm"] != "evrim", order.get(m["status"], 9), -(m["score"] or 0)))
    out = {"settings": st, "speeds": SPEEDS, "night": dict(NIGHT), "report": store.kv_get("learn_report"),
           "history": store.kv_get("learn_history") or [], "live": store.kv_get("learn_live"),
           "evo_today": {k: v for k, v in (store.kv_get("learn_evo_today") or {}).items() if k in ("date", "ts", "vix")},
           "live_from": store.kv_get("learn_live_from"), "trading_arm": store.kv_get("learn_trading_arm") or "evrim",
           "members": [m for m in members if m["status"] in ("aktif", "aday") or m["arm"] == "evrim"][:160],
           "counts": {arm: {s: sum(1 for m in members if m["arm"] == arm and m["status"] == s) for s in ("aktif", "aday", "emekli")}
                      for arm in ("ana", "evrim")},
           "versions": len(store.kv_get("learn_versions") or []), "night_error": store.kv_get("learn_night_error"),
           "last_night": store.kv_get("learn_night")}
    for kind in ("firsat", "mikro"):
        vs = _load_variants(kind)
        vs.sort(key=lambda v: (v["status"] != "şampiyon", -(v.get("score") or 0)))
        out[kind] = {"variants": [_variant_row(kind, v) for v in vs[:16]], "count": len(vs),
                     "days": len(_cand_days(kind)), "applied": genes_text(kind, _incumbent(kind)),
                     "forward": forward_summary(kind)}
    try:
        neuro = importlib.import_module(f"{__package__}.neuro")
        nn_pop = neuro.population_summary()
        nn_act = [m for m in nn_pop if m.get("status") == "aktif"]
        nn_lvs = [m.get("live") or {} for m in nn_act if (m.get("live") or {}).get("days")]
        out["nn"] = {
            "active": len(nn_act),
            "retired": sum(1 for m in nn_pop if m.get("status") == "emekli"),
            "models": nn_pop[:24],
            "night": store.kv_get("nn_night"),
            "live_days": max((int(lv.get("days") or 0) for lv in nn_lvs), default=0),
            "live_trades": sum(int(lv.get("n") or 0) for lv in nn_lvs),
            "avg_pct": round(sum(float(lv.get("daily_pct") or 0) for lv in nn_lvs) / len(nn_lvs), 2) if nn_lvs else None,
        }
    except Exception:  # noqa: BLE001
        out["nn"] = {"active": 0, "models": []}
    if running:
        out["running"] = {k: running(f"learn_{k}") for k in ("nightly", "intraday", "open")}
    return out
