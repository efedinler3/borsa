"""The four strategy bots on daily bars, and the outcome of the same-day trade each vote implies.

Decision time is the regular open of day t: a bot sees daily bars up to the close of day t-1, today's opening
price, and the events published between the close of t-1 and the open of t (earnings released after the previous
close or before today's open, overnight SEC filings, overnight headlines). Nothing later is visible.

Trade: enter at the open (plus slippage), exit at the stop, the target or the close of the same day. Daily bars do
not tell which of stop/target came first, so a day touching both counts as a stop (conservative). Returns are net of
slippage on both sides.

Bots (vote +1 long, -1 short, 0 nothing; confidence 0.1-1):
  momentum   yesterday's close broke the 20-day high/low on 1.5x volume, in the direction of the 50-day trend
  meanrev    2-day RSI below 10 above the 200-day average (buy the dip) / above 90 below it (sell the rip)
  gap        today's open gaps 2-10 % in the direction of the 20-day trend (gap and go)
  event      earnings surprise, overnight 8-K items / offerings / 13D, overnight headlines (sentiment x category)
"""
from __future__ import annotations

import bisect
import importlib
import json
import warnings
from collections import defaultdict
from datetime import date, datetime

import numpy as np

from ..util import ET, close_minutes
from . import store

BOTS = ("momentum", "meanrev", "gap", "event")
BOT_TR = {"momentum": "Momentum / kırılım", "meanrev": "Ortalamaya dönüş", "gap": "Açılış boşluğu", "event": "Olay / haber"}
WARMUP = 210
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5e6       # 20-day average; slippage grows for the thinner names (see evolve.slippage)

DEFAULT_PARAMS = {"sl_pct": 2.0, "tp_pct": 4.0, "slippage_bps": 5.0}

# 8-K item -> weight for the event bot (sign = usual price reaction; 2.02 results are covered by the earnings data)
ITEM_W = {"1.01": 0.3, "1.02": -0.3, "1.03": -1.0, "2.01": 0.25, "2.05": -0.3, "2.06": -0.5, "3.01": -0.6,
          "4.01": -0.4, "4.02": -0.8, "5.02": -0.15}
OFFERING_FORMS = {"S-1", "S-3", "424B4"}
ACTIVIST_FORMS = {"SC 13D", "SC 13D/A"}

FEATURES = ("gap", "ret1", "rvol", "rsi2", "rsi14", "atr", "dist20", "dist50", "dist200", "spy_ret1", "spy_gap",
            "spy_up", "vix", "vix_chg", "earn", "surprise", "filing_score", "offering", "form4", "news_n", "news_score",
            "news_era", "young", "ret5", "brk20", "brkl20", "ret20", "dist_52h", "dist_52l", "down_streak",
            "days_to_earn", "days_since_earn", "dvol")
PATH_DAYS = 5   # price path stored per row for the evolution pool (exits up to 5 days)


# ---------------------------------------------------------------- time helpers

_ts_cache: dict[str, tuple[int, int]] = {}


def session_ts(d: str) -> tuple[int, int]:
    """(open ts, close ts) of an ISO trading date."""
    hit = _ts_cache.get(d)
    if hit is None:
        x = date.fromisoformat(d)
        o = int(datetime(x.year, x.month, x.day, 9, 30, tzinfo=ET).timestamp())
        cm = close_minutes(x)
        c = int(datetime(x.year, x.month, x.day, cm // 60, cm % 60, tzinfo=ET).timestamp())
        hit = _ts_cache[d] = (o, c)
    return hit


# ---------------------------------------------------------------- numpy helpers

def _shift(a: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k]
    return out


def _mean_prior(a: np.ndarray, n: int) -> np.ndarray:
    """mean(a[t-n : t]) — the n values before t."""
    x = np.nan_to_num(a, nan=0.0)
    cs = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(a), np.nan)
    if len(a) > n:
        out[n:] = (cs[n:len(a)] - cs[0:len(a) - n]) / n
    return out


def _window_prior(a: np.ndarray, n: int, fn, lag: int = 1) -> np.ndarray:
    """fn over a[t-lag-n : t-lag] (n values ending lag days before t)."""
    out = np.full(len(a), np.nan)
    if len(a) >= n + lag + 1:
        w = fn(np.lib.stride_tricks.sliding_window_view(np.nan_to_num(a, nan=0.0), n), axis=1)
        out[n + lag:] = w[:len(a) - n - lag]
    return out


def _rsi(c: np.ndarray, n: int) -> np.ndarray:
    """Wilder RSI of the closes (value at index i uses closes up to i)."""
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(c)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:  # noqa: E741
    pc = _shift(c)
    with warnings.catch_warnings():   # the live row (today) has no high/low yet: an all-NaN column is expected
        warnings.simplefilter("ignore", RuntimeWarning)
        tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    a = np.nanmean(tr[1:n + 1])
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


# ---------------------------------------------------------------- loading

def load_bars(symbols: list[str] | None = None) -> dict[str, dict]:
    q = "SELECT symbol, d, o, h, l, c, v FROM bars"
    args: list = []
    if symbols:
        q += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
        args = list(symbols)
    grouped: dict[str, list] = defaultdict(list)
    for r in store.conn().execute(q + " ORDER BY symbol, d", args):
        grouped[r[0]].append(r[1:])
    out = {}
    for sym, rows in grouped.items():
        arr = np.array([x[1:] for x in rows], dtype=float)
        out[sym] = {"d": [x[0] for x in rows], "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2], "c": arr[:, 3], "v": arr[:, 4]}
    return out


def _category_bias() -> dict[str, float]:
    try:
        classify = importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.analysis.classify")
        return {k: float((v or {}).get("bias") or 0) for k, v in classify.CATEGORIES.items()}
    except Exception:  # noqa: BLE001
        return {}


def load_events(symbols: list[str]) -> dict:
    """Per symbol: earnings rows, filing rows (ts, form, items), news rows (ts, score, subject), first news ts."""
    c = store.conn()
    wanted = set(symbols)
    earn: dict[str, list] = defaultdict(list)
    for r in c.execute("SELECT symbol, d, time, surprise FROM earnings"):
        if r[0] in wanted:
            earn[r[0]].append((r[1], r[2], r[3]))
    fil: dict[str, list] = defaultdict(list)
    for r in c.execute("SELECT symbol, ts, form, items FROM filings ORDER BY symbol, ts"):
        if r[0] in wanted:
            fil[r[0]].append((r[1], r[2], r[3] or ""))
    bias = _category_bias()
    news: dict[str, list] = defaultdict(list)
    for r in c.execute("SELECT symbol, ts, categories, sentiment, importance, subject FROM news ORDER BY symbol, ts"):
        if r[0] not in wanted:
            continue
        try:
            cats = json.loads(r[2] or "[]")
        except ValueError:
            cats = []
        b = max(-1.0, min(1.0, sum(bias.get(x, 0.0) for x in cats[:3])))
        w = max(0.0, float(r[4] or 0)) / 100.0 * (1.0 if r[5] else 0.4)
        news[r[0]].append((r[1], w * (0.6 * float(r[3] or 0) + 0.4 * b), w))
    return {"earnings": earn, "filings": fil, "news": news}


# ---------------------------------------------------------------- per-symbol computation

def _context(bars: dict[str, dict]) -> dict[str, dict]:
    """date -> market context known at day t's open (SPY, VIX)."""
    ctx: dict[str, dict] = defaultdict(dict)
    spy, vix = bars.get("SPY"), bars.get("^VIX")
    if spy:
        c, o = spy["c"], spy["o"]
        r1 = _shift(c) / _shift(c, 2) - 1
        g = o / _shift(c) - 1
        up = (_shift(c) > _mean_prior(c, 200)).astype(float)
        for i, d in enumerate(spy["d"]):
            ctx[d].update(spy_ret1=r1[i], spy_gap=g[i], spy_up=up[i])
    if vix:
        c = vix["c"]
        v1, chg = _shift(c), _shift(c) / _shift(c, 2) - 1
        for i, d in enumerate(vix["d"]):
            ctx[d].update(vix=v1[i] / 100.0, vix_chg=chg[i])
    return ctx


def _overnight_index(dates: list[str]) -> tuple[list[int], list[int]]:
    opens, closes = zip(*(session_ts(d) for d in dates)) if dates else ((), ())
    return list(opens), list(closes)


def compute_symbol(sym: str, b: dict, ev: dict, ctx: dict, params: dict, news_from: int | None,
                   young_listing: bool = False, live: bool = False, all_rows: bool = False) -> dict | None:
    """Features, votes and outcomes for every day of one symbol. `live`: the last row is today (only its open is
    known); its outcome is left NaN."""
    n = len(b["d"])
    if n < WARMUP + (0 if live else 1):
        return None
    o, h, l, c, v = b["o"], b["h"], b["l"], b["c"], b["v"]  # noqa: E741
    c1, c2 = _shift(c), _shift(c, 2)
    gap = o / c1 - 1
    ret1 = c1 / c2 - 1
    # _mean_prior(c, k)[t] uses c[t-k:t]: the closes up to t-1
    sma20, sma50, sma200 = _mean_prior(c, 20), _mean_prior(c, 50), _mean_prior(c, 200)
    hi20 = _window_prior(h, 20, np.max, lag=1)
    lo20 = _window_prior(l, 20, np.min, lag=1)
    vavg = _window_prior(v, 20, np.mean, lag=1)
    rvol = _shift(v) / np.where(vavg > 0, vavg, np.nan)
    dvol = _mean_prior(c * v, 20)
    rsi2, rsi14 = _shift(_rsi(c, 2)), _shift(_rsi(c, 14))
    atr = _shift(_atr(h, l, c)) / c1

    # ---- votes
    liquid = (c1 >= MIN_PRICE) & (dvol >= MIN_DOLLAR_VOLUME)
    votes = np.zeros((n, 4), dtype=np.int8)
    conf = np.zeros((n, 4))
    with np.errstate(invalid="ignore", divide="ignore"):
        up_b = (c1 > hi20) & (rvol >= 1.5) & (c1 > sma50)
        dn_b = (c1 < lo20) & (rvol >= 1.5) & (c1 < sma50)
        votes[up_b, 0], votes[dn_b, 0] = 1, -1
        conf[:, 0] = np.clip(np.abs(c1 / np.where(up_b, hi20, lo20) - 1) * 10 + (rvol - 1.5) / 4 + 0.3, 0.1, 1)
        mr_l = (rsi2 < 10) & (c1 > sma200)
        mr_s = (rsi2 > 90) & (c1 < sma200)
        votes[mr_l, 1], votes[mr_s, 1] = 1, -1
        conf[:, 1] = np.clip(np.where(mr_l, (10 - rsi2) / 10, (rsi2 - 90) / 10), 0.1, 1)
        g_l = (gap >= 0.02) & (gap <= 0.10) & (c1 > sma20)
        g_s = (gap <= -0.02) & (gap >= -0.10) & (c1 < sma20)
        votes[g_l, 2], votes[g_s, 2] = 1, -1
        conf[:, 2] = np.clip(np.abs(gap) / 0.06, 0.1, 1)

    # ---- events -> day index
    dates = b["d"]
    opens, closes = _overnight_index(dates)
    surprise = np.full(n, np.nan)
    earn = np.zeros(n)
    for d, tm, s in ev["earnings"].get(sym, ()):
        # before the open: that day reacts; after the close or time unknown: the next day (never earlier)
        t = bisect.bisect_left(dates, d) if tm == "bmo" else bisect.bisect_right(dates, d)
        if 0 <= t < n:
            earn[t] = 1
            if s is not None and (np.isnan(surprise[t]) or abs(s) > abs(surprise[t])):
                surprise[t] = s
    filing = np.zeros(n)
    offering = np.zeros(n)
    form4_day = np.zeros(n)
    for ts, form, items in ev["filings"].get(sym, ()):
        t = bisect.bisect_right(opens, ts)      # first day whose open comes after the filing
        if t == 0 or t >= n:
            continue
        if form == "4":
            form4_day[t] += 1                   # insider filings are known by the next open either way
            continue
        if ts < closes[t - 1]:
            continue                            # filed during the previous session: that day already reacted
        if form == "8-K":
            filing[t] += sum(ITEM_W.get(x.strip(), 0.0) for x in items.split(","))
        elif form in OFFERING_FORMS:
            offering[t] = 1
            filing[t] -= 0.4
        elif form in ACTIVIST_FORMS:
            filing[t] += 0.3
    form4 = np.convolve(form4_day, np.ones(5), mode="full")[:n]   # filings in the 5 days up to t
    news_n = np.zeros(n)
    news_raw = np.zeros(n)
    for ts, sc, w in ev["news"].get(sym, ()):
        t = bisect.bisect_right(opens, ts)
        if 0 < t < n and ts >= closes[t - 1]:
            news_n[t] += 1
            news_raw[t] += sc
    news_score = np.tanh(news_raw * 1.5)
    news_era = np.zeros(n)
    if news_from:
        news_era[np.array(opens) >= news_from + 86400] = 1
    with np.errstate(invalid="ignore"):
        e_s = np.where(np.abs(surprise) >= 5, np.sign(surprise) * np.minimum(1, 0.5 + np.abs(surprise) / 40), 0.0)
    e_s = np.nan_to_num(e_s)
    ev_total = e_s + np.clip(filing, -1, 1) + news_score
    votes[ev_total >= 0.3, 3] = 1
    votes[ev_total <= -0.3, 3] = -1
    conf[:, 3] = np.clip(np.abs(ev_total), 0.1, 1)

    votes[: WARMUP] = 0
    votes[~liquid] = 0
    if live:
        rows_mask = np.zeros(n, dtype=bool)
        rows_mask[-1] = True
    elif all_rows:      # every liquid day after the warm-up (evolution pool: genomes pick their own days)
        rows_mask = liquid.copy()
        rows_mask[: WARMUP] = False
    else:
        rows_mask = np.any(votes != 0, axis=1)
    idx = np.nonzero(rows_mask)[0]
    if not len(idx):
        return None

    # ---- extra context for the evolution pool (all known at the open of day t)
    ret20 = c1 / _shift(c, 21) - 1
    hi252 = _window_prior(h, min(252, max(1, n - 2)), np.max, lag=1)
    lo252 = _window_prior(l, min(252, max(1, n - 2)), np.min, lag=1)
    down = np.zeros(n)
    run_ = 0
    for i in range(1, n):          # consecutive lower closes ending yesterday
        down[i] = run_
        run_ = run_ + 1 if c[i] < c[i - 1] else 0
    e_days = sorted({t for t in range(n) if earn[t]})
    to_earn = np.full(n, 99.0)
    since_earn = np.full(n, 99.0)
    for t in range(n):
        k = bisect.bisect_left(e_days, t)
        if k < len(e_days) and e_days[k] - t <= 15:   # report dates are announced ~2-4 weeks ahead: farther = unknown
            to_earn[t] = e_days[k] - t                    # 0 = the report is today (before the open or last night)
        if k > 0:
            since_earn[t] = min(99, t - e_days[k - 1])
    # (99 = no report within the known window)

    # ---- outcomes (same-day bracket from the open)
    sl, tp, slip = params["sl_pct"] / 100, params["tp_pct"] / 100, params["slippage_bps"] / 10000
    with np.errstate(invalid="ignore", divide="ignore"):
        e_l = o * (1 + slip)
        e_s_ = o * (1 - slip)
        r_long = np.where(l <= e_l * (1 - sl), -sl, np.where(h >= e_l * (1 + tp), tp, c * (1 - slip) / e_l - 1))
        r_short = np.where(h >= e_s_ * (1 + sl), -sl, np.where(l <= e_s_ * (1 - tp), tp, 1 - c * (1 + slip) / e_s_))
        # slippage on the exit leg of stop/target fills
        r_long = np.where(np.abs(r_long - tp) < 1e-12, tp - slip, np.where(np.abs(r_long + sl) < 1e-12, -sl - slip, r_long))
        r_short = np.where(np.abs(r_short - tp) < 1e-12, tp - slip, np.where(np.abs(r_short + sl) < 1e-12, -sl - slip, r_short))
        oc = c / o - 1
    young = np.zeros(n)
    if young_listing:       # listed during the data window: its first year of trading
        young[: min(n, 250)] = 1

    def col(a):
        return a[idx]

    cx = [ctx.get(dates[i], {}) for i in idx]
    feats = {
        "gap": col(gap), "ret1": col(ret1), "rvol": col(rvol), "rsi2": col(rsi2) / 100, "rsi14": col(rsi14) / 100,
        "atr": col(atr), "dist20": col(c1 / sma20 - 1), "dist50": col(c1 / sma50 - 1), "dist200": col(c1 / sma200 - 1),
        "spy_ret1": np.array([x.get("spy_ret1", np.nan) for x in cx]), "spy_gap": np.array([x.get("spy_gap", np.nan) for x in cx]),
        "spy_up": np.array([x.get("spy_up", np.nan) for x in cx]), "vix": np.array([x.get("vix", np.nan) for x in cx]),
        "vix_chg": np.array([x.get("vix_chg", np.nan) for x in cx]),
        "earn": col(earn), "surprise": np.nan_to_num(col(surprise)), "filing_score": col(filing), "offering": col(offering),
        "form4": col(form4), "news_n": col(news_n), "news_score": col(news_score), "news_era": col(news_era), "young": col(young),
        "ret5": col(c1 / _shift(c, 6) - 1), "brk20": col(c1 / hi20 - 1), "brkl20": col(c1 / lo20 - 1),
        "ret20": col(ret20), "dist_52h": col(c1 / hi252 - 1), "dist_52l": col(c1 / lo252 - 1), "down_streak": col(down),
        "days_to_earn": col(to_earn), "days_since_earn": col(since_earn), "dvol": col(dvol),
    }
    out = {"sym": sym, "date": [dates[i] for i in idx], "votes": votes[idx], "conf": conf[idx], "feats": feats,
           "r_long": col(r_long), "r_short": col(r_short), "oc": col(oc), "open": col(o), "prev_close": col(c1)}
    if all_rows:
        paths = np.full((4, len(idx), PATH_DAYS), np.nan, dtype=np.float32)
        for j in range(PATH_DAYS):
            k = idx + j
            ok = k < n
            for a_i, arr_ in enumerate((o, h, l, c)):
                paths[a_i, ok, j] = arr_[k[ok]]
        out["paths"] = paths
    return out


def build_dataset(params: dict | None = None, symbols: list[str] | None = None, progress=None) -> dict:
    """Run the four bots over every symbol in lab.db. Returns column arrays over all rows with at least one vote."""
    params = dict(DEFAULT_PARAMS, **(params or {}))
    syms = symbols or [r[0] for r in store.conn().execute("SELECT symbol FROM universe ORDER BY rank")]
    bars = load_bars(syms + ["SPY", "^VIX"])
    ctx = _context(bars)
    ev = load_events(syms)
    news_from = {r[0]: r[1] for r in store.conn().execute("SELECT symbol, MIN(ts) FROM news GROUP BY symbol")}
    start = (bars.get("SPY") or {}).get("d", [""])[0]
    parts = []
    for i, sym in enumerate(syms):
        b = bars.get(sym)
        if b:
            young = bool(start) and (date.fromisoformat(b["d"][0]) - date.fromisoformat(start)).days > 60
            r = compute_symbol(sym, b, ev, ctx, params, news_from.get(sym), young_listing=young)
            if r:
                parts.append(r)
        if progress and i % 25 == 0:
            progress(i + 1, len(syms))
    return _concat(parts)


def _concat(parts: list[dict]) -> dict:
    if not parts:
        return {"n": 0}
    out = {"n": sum(len(p["date"]) for p in parts),
           "sym": np.array([p["sym"] for p in parts for _ in p["date"]]),
           "date": np.array([d for p in parts for d in p["date"]]),
           "votes": np.vstack([p["votes"] for p in parts]), "conf": np.vstack([p["conf"] for p in parts]),
           "r_long": np.concatenate([p["r_long"] for p in parts]), "r_short": np.concatenate([p["r_short"] for p in parts]),
           "oc": np.concatenate([p["oc"] for p in parts]), "open": np.concatenate([p["open"] for p in parts]),
           "prev_close": np.concatenate([p["prev_close"] for p in parts])}
    out["feats"] = {k: np.concatenate([p["feats"][k] for p in parts]) for k in FEATURES}
    order = np.lexsort((out["sym"], out["date"]))
    for k in ("sym", "date", "votes", "conf", "r_long", "r_short", "oc", "open", "prev_close"):
        out[k] = out[k][order]
    out["feats"] = {k: v[order] for k, v in out["feats"].items()}
    return out


def directional(ds: dict, direction: np.ndarray) -> np.ndarray:
    """Net return of a trade in `direction` (+1/-1) for each row; NaN where direction is 0."""
    return np.where(direction > 0, ds["r_long"], np.where(direction < 0, ds["r_short"], np.nan))
