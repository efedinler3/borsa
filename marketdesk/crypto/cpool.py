"""Crypto pool: every coin-day of the day's 150 most traded coins (by 20-day average USDT volume), with features known
at 00:00 UTC (the daily candle's open) and the flat daily bars needed to walk trades of 1-90 days.

The same object shape as the stock Pool (F, Q, dayidx, udates, group, slip, atr, bO/bH/bL/bC, bar_i ...), so the stock
tree's trade engine (lab/longpath.py) and its fitness / position-cap helpers run on it unchanged.

Features use only finished candles (up to the previous UTC day). The Fear & Greed value of a day is published at that
day's 00:00 UTC; to be safe the previous day's value is used. A coin that leaves Binance is sold at its last real
close (its history comes from the public archive), so the walk is not only made of coins that survived.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import date

import numpy as np

from . import cstore

log = logging.getLogger("marketdesk.crypto")

TOP_N = 150
MIN_HISTORY = 30                 # days of candles a coin needs before it can be traded
START = "2018-02-02"             # the first day with a Fear & Greed value for the previous day
GROUPS = 5                       # coins are split into 5 fixed groups; group E (4) is never used for selection
QGRID = 201
FEE = 0.001                      # Binance spot fee per side (0.1 %)

FEATURES = {
    "ret1": ("Dünkü getiri", "pct"), "ret3": ("Son 3 gün getirisi", "pct"), "ret7": ("Son 7 gün getirisi", "pct"),
    "ret30": ("Son 30 gün getirisi", "pct"), "ret90": ("Son 90 gün getirisi", "pct"),
    "rsi2": ("RSI(2)", "rsi"), "rsi14": ("RSI(14)", "rsi"), "atr": ("Oynaklık (ATR)", "pct"),
    "rvol": ("Dünkü göreli hacim", "x"), "dist20": ("20 günlük ortalamaya uzaklık", "pct"),
    "dist50": ("50 günlük ortalamaya uzaklık", "pct"), "dist200": ("200 günlük ortalamaya uzaklık", "pct"),
    "brk20": ("20 günlük zirveye uzaklık", "pct"), "brkl20": ("20 günlük dibe uzaklık", "pct"),
    "dist_365h": ("1 yıllık zirveye uzaklık", "pct"), "dist_365l": ("1 yıllık dibe uzaklık", "pct"),
    "down_streak": ("Art arda düşüş günü", "int"), "dvol": ("Günlük işlem hacmi (20 gün ort.)", "usd"),
    "taker": ("Dünkü alıcı baskısı (taker alım payı)", "share"), "age": ("Borsadaki yaşı (gün, log)", "logd"),
    "btc_ret1": ("BTC dünkü getiri", "pct"), "btc_ret7": ("BTC son 7 gün", "pct"),
    "btc_dist50": ("BTC'nin 50 günlük ortalamaya uzaklığı", "pct"), "rs_btc30": ("BTC'ye göre 30 günlük güç", "pct"),
    "breadth50": ("Piyasa genişliği (50 gün ort. üstü coin oranı)", "share"),
    "fng": ("Korku-Açgözlülük endeksi", "fng"), "fng_chg7": ("Korku-Açgözlülük 7 günlük değişim", "fngd"),
    "vol_rank": ("Hacim sırası (1 = en çok işlem gören)", "share"), "dow": ("Haftanın günü (0=Pzt … 6=Paz)", "int"),
}
NAMES = list(FEATURES)


# ---------------------------------------------------------------- per-coin indicators

def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(np.nan_to_num(x), 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _roll(x: np.ndarray, n: int, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(np.lib.stride_tricks.sliding_window_view(x, n), axis=1)
    return out


def _ago(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) > k:
        out[k:] = x[:-k]
    return out


def _rsi(c: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    up, dn = np.maximum(d, 0), np.maximum(-d, 0)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(c)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + dn[i - 1]) / n
        out[i] = 1.0 if ad == 0 else au / (au + ad)
    return out


def end_stats(o, h, l, c, qv, v, tb) -> dict[str, np.ndarray]:
    """Indicators at the CLOSE of every candle i (known from the next day's 00:00 UTC on)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        out = {}
        for k, n in (("ret1", 1), ("ret3", 3), ("ret7", 7), ("ret30", 30), ("ret90", 90)):
            out[k] = c / _ago(c, n) - 1
        out["rsi2"], out["rsi14"] = _rsi(c, 2), _rsi(c, 14)
        pc = _ago(c, 1)
        tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
        out["atr"] = _sma(tr, 14) / c
        avg20 = _sma(qv, 20)
        out["rvol"] = qv / _ago(avg20, 1)
        for k, n in (("dist20", 20), ("dist50", 50), ("dist200", 200)):
            out[k] = c / _sma(c, n) - 1
        out["brk20"] = c / _roll(h, 20, np.max) - 1
        out["brkl20"] = c / _roll(l, 20, np.min) - 1
        out["dist_365h"] = c / _roll(h, 365, np.max) - 1
        out["dist_365l"] = c / _roll(l, 365, np.min) - 1
        ds = np.zeros(len(c))
        for i in range(1, len(c)):
            ds[i] = ds[i - 1] + 1 if c[i] < c[i - 1] else 0
        out["down_streak"] = ds
        out["dvol"] = np.log10(np.maximum(avg20, 1.0))
        out["taker"] = np.where(v > 0, tb / v, np.nan)
        out["age"] = np.log10(np.arange(1, len(c) + 1, dtype=np.float64))
    return out


STOCK_TOKENS_FROM = "2026-06-01"


def is_stock_token(symbol: str, first_day: str) -> bool:
    """Binance's tokenized US stocks and ETFs (NVDAB, TSLAB, QQQB ...): listed from June 2026 on, the base ends in B.
    They follow the stock market, not crypto, so the crypto bots do not trade them."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return base.endswith("B") and len(base) >= 3 and (first_day or "") >= STOCK_TOKENS_FROM


MAX_GAP_DAYS = 2                 # a longer hole inside a coin's candles is a token swap / relisting: a new coin


def _segments(rows: list) -> list[tuple[int, int]]:
    """Pieces of one symbol's candles without holes longer than MAX_GAP_DAYS (LUNA -> Terra 2.0, redenominations,
    relistings after months): every piece is its own coin, so no price path or indicator spans the hole."""
    ords = np.array([date.fromisoformat(r[0]).toordinal() for r in rows])
    cut = np.nonzero(np.diff(ords) > MAX_GAP_DAYS)[0] + 1
    edges = np.r_[0, cut, len(rows)]
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:])]


def load_bars() -> tuple[list[str], dict]:
    """All coins' candles, trailing no-trade candles cut off. A symbol whose history has a hole is split: the earlier
    pieces become delisted coins of their own ("LUNAUSDT#1"), the last piece keeps the symbol."""
    cstore.init()
    con = cstore.conn()
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol")]
    out = {}
    for s in syms:
        rows = con.execute("SELECT d, o, h, l, c, v, qv, tb FROM bars WHERE symbol=? ORDER BY d", (s,)).fetchall()
        if not rows or is_stock_token(s, rows[0][0]):
            continue
        segs = _segments(rows)
        for j, (a0, a1) in enumerate(segs):
            part = rows[a0:a1]
            if len(part) < MIN_HISTORY + 2:
                continue
            a = np.array([r[1:] for r in part], dtype=np.float64)
            d = np.array([r[0] for r in part])
            live = np.nonzero(a[:, 6] > 0)[0]
            if not len(live):
                continue
            end = live[-1] + 1
            a, d = a[:end], d[:end]
            name = s if j == len(segs) - 1 else f"{s}#{j + 1}"
            out[name] = {"d": d, "o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3], "v": a[:, 4], "qv": a[:, 5], "tb": a[:, 6]}
    return syms, out


def fng_by_day() -> dict[str, float]:
    return {r[0]: float(r[1]) for r in cstore.conn().execute("SELECT d, v FROM fng")}


def slippage(dvol_log: np.ndarray) -> np.ndarray:
    """Per-side cost: Binance fee + a spread / impact that grows for thinner coins."""
    dv = 10 ** np.nan_to_num(dvol_log, nan=5.0)
    spread = np.where(dv >= 1e8, 0.0002, np.where(dv >= 2e7, 0.0005, np.where(dv >= 5e6, 0.0010, 0.0020)))
    return (FEE + spread).astype(np.float32)


def group_of(sym: str) -> int:
    return int(hashlib.sha1(sym.encode()).hexdigest(), 16) % GROUPS


def _market(btc_e: dict, btc_d: np.ndarray, fng: dict, days: list[str]) -> dict[str, np.ndarray]:
    """Market features per day (indexed like `days`): BTC moves up to the previous close, the previous day's fear."""
    pos = {d: i for i, d in enumerate(btc_d.tolist())}
    out = {k: np.full(len(days), np.nan) for k in ("btc_ret1", "btc_ret7", "btc_dist50", "btc_ret30", "fng", "fng_chg7", "dow")}
    for j, d in enumerate(days):
        dd = date.fromisoformat(d)
        prev = date.fromordinal(dd.toordinal() - 1).isoformat()
        i = pos.get(prev)
        if i is not None:
            for k, src in (("btc_ret1", "ret1"), ("btc_ret7", "ret7"), ("btc_dist50", "dist50"), ("btc_ret30", "ret30")):
                out[k][j] = btc_e[src][i]
        f1 = fng.get(prev)
        f8 = fng.get(date.fromordinal(dd.toordinal() - 8).isoformat())
        if f1 is not None:
            out["fng"][j] = f1 / 100
            if f8 is not None:
                out["fng_chg7"][j] = (f1 - f8) / 100
        out["dow"][j] = dd.weekday()
    return out


# ---------------------------------------------------------------- the pool

class CryptoPool:
    def __init__(self, progress=None, top_n: int = TOP_N, max_symbols: int | None = None):
        prog = progress or (lambda m, i, n: None)
        prog("Kripto mumları okunuyor…", 0, 1)
        _all, bars = load_bars()
        if "BTCUSDT" not in bars:
            raise RuntimeError("BTC verisi yok: önce kripto geçmişini indirin")
        if max_symbols:
            keep = ["BTCUSDT", "ETHUSDT"] + sorted(bars, key=lambda s: -float(np.nansum(bars[s]["qv"][-365:])))[:max_symbols]
            bars = {s: bars[s] for s in dict.fromkeys(keep) if s in bars}
        fng = fng_by_day()
        names = sorted(bars)
        self.sym_names = names
        # flat bars of every coin (the trade engine walks these)
        lens = [len(bars[s]["d"]) for s in names]
        starts = np.r_[0, np.cumsum(lens)[:-1]].astype(np.int64)
        self.bO = np.concatenate([bars[s]["o"] for s in names]).astype(np.float32)
        self.bH = np.concatenate([bars[s]["h"] for s in names]).astype(np.float32)
        self.bL = np.concatenate([bars[s]["l"] for s in names]).astype(np.float32)
        self.bC = np.concatenate([bars[s]["c"] for s in names]).astype(np.float32)
        last_all = max(bars[s]["d"][-1] for s in names)
        last_ord = date.fromisoformat(last_all).toordinal()
        # the calendar: every UTC day from START to the last finished candle (crypto trades every day)
        d0 = date.fromisoformat(START).toordinal()
        days = [date.fromordinal(x).isoformat() for x in range(d0, last_ord + 1)]
        day_pos = {d: i for i, d in enumerate(days)}
        btc = bars["BTCUSDT"]
        btc_e = end_stats(btc["o"], btc["h"], btc["l"], btc["c"], btc["qv"], btc["v"], btc["tb"])
        mk = _market(btc_e, btc["d"], fng, days)
        cols = {k: [] for k in NAMES if k not in ("btc_ret1", "btc_ret7", "btc_dist50", "rs_btc30", "breadth50", "fng",
                                                  "fng_chg7", "vol_rank", "dow")}
        ret30_l, day_l, code_l, bi_l, end_l, gone_l = [], [], [], [], [], []
        bar_day = np.zeros(len(self.bO), np.int32)
        for k, s in enumerate(names):
            b = bars[s]
            n = len(b["d"])
            bd = np.array([day_pos.get(x, -1) for x in b["d"].tolist()])
            # bars before START map to day 0 (only exits use this, and they are always after an entry)
            bar_day[starts[k]:starts[k] + n] = np.where(bd >= 0, bd, 0)
            e = end_stats(b["o"], b["h"], b["l"], b["c"], b["qv"], b["v"], b["tb"])
            t = np.arange(MIN_HISTORY, n)                     # entry candle t, features of candle t - 1
            t = t[(bd[t] >= 0) & (b["o"][t] > 0) & (b["qv"][t] > 0) & np.isfinite(e["dvol"][t - 1])]
            if not len(t):
                continue
            for c_ in cols:
                cols[c_].append(e[c_][t - 1].astype(np.float32))
            ret30_l.append(e["ret30"][t - 1])
            day_l.append(bd[t].astype(np.int32))
            code_l.append(np.full(len(t), k, np.int32))
            bi_l.append((starts[k] + t).astype(np.int64))
            end_l.append(np.full(len(t), starts[k] + n, np.int64))
            gone = date.fromisoformat(b["d"][-1]).toordinal() < last_ord - 7
            gone_l.append(np.full(len(t), gone, bool))
            if progress and k % 50 == 0:
                prog(f"Kripto göstergeleri hesaplanıyor: {k}/{len(names)} coin", k, len(names))
        F = {c_: np.concatenate(v) for c_, v in cols.items()}
        di = np.concatenate(day_l)
        code = np.concatenate(code_l)
        bi = np.concatenate(bi_l)
        bend = np.concatenate(end_l)
        bgone = np.concatenate(gone_l)
        r30 = np.concatenate(ret30_l)
        # the day's universe: its TOP_N coins by 20-day average volume
        order = np.lexsort((-F["dvol"], di))
        di_o = di[order]
        first = np.r_[True, di_o[1:] != di_o[:-1]]
        start = np.maximum.accumulate(np.where(first, np.arange(len(di_o)), 0))
        rank = np.arange(len(di_o)) - start
        keep = np.sort(order[rank < top_n])
        F = {c_: v[keep] for c_, v in F.items()}
        di, code, bi, bend, bgone, r30 = di[keep], code[keep], bi[keep], bend[keep], bgone[keep], r30[keep]
        # cross-sectional features
        cnt = np.bincount(di, minlength=len(days))
        above = np.nan_to_num(F["dist50"]) > 0
        F["breadth50"] = (np.bincount(di, weights=above, minlength=len(days)) / np.maximum(cnt, 1)).astype(np.float32)[di]
        o2 = np.lexsort((-F["dvol"], di))
        d2 = di[o2]
        f2 = np.r_[True, d2[1:] != d2[:-1]]
        s2 = np.maximum.accumulate(np.where(f2, np.arange(len(d2)), 0))
        vr = np.empty(len(di), np.float32)
        vr[o2] = 1 - (np.arange(len(d2)) - s2) / np.maximum(cnt[d2] - 1, 1)
        F["vol_rank"] = vr
        for k in ("btc_ret1", "btc_ret7", "btc_dist50", "fng", "fng_chg7", "dow"):
            F[k] = mk[k][di].astype(np.float32)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["rs_btc30"] = ((1 + r30) / (1 + mk["btc_ret30"][di]) - 1).astype(np.float32)
        self.F = {k: F[k] for k in NAMES}
        # drop days with no coin, re-index the calendar to the days that have rows
        used = np.unique(di)
        remap = np.full(len(days), -1, np.int64)
        remap[used] = np.arange(len(used))
        self.dayidx = remap[di].astype(np.int32)
        self.udates = [days[i] for i in used.tolist()]
        bd_full = np.searchsorted(used, bar_day)            # exits on a day without rows map to the next day with rows
        self.bar_day = np.minimum(bd_full, len(used) - 1).astype(np.int32)
        self.n = len(di)
        self.n_days = len(used)
        self.day_year = np.array([int(d[:4]) for d in self.udates], dtype=np.int16)
        self.first_date, self.last_date = self.udates[0], self.udates[-1]
        self.symcode = code
        self.group = np.array([group_of(s) for s in names], dtype=np.int8)[code]
        self.symbols = int(len(np.unique(code)))
        self.bar_i = bi.astype(np.int32)
        self.bar_end = bend.astype(np.int32)
        self.bar_gone = bgone
        self.atr = np.nan_to_num(self.F["atr"], nan=0.05).astype(np.float32)
        self.slip = slippage(self.F["dvol"])
        tr = self.group <= 3
        self.Q = {}
        for k in NAMES:
            x = self.F[k][tr]
            x = x[np.isfinite(x)]
            self.Q[k] = np.quantile(x, np.linspace(0, 1, QGRID)) if len(x) else np.zeros(QGRID)
        prog(f"Kripto havuzu hazır: {self.n:,} coin-gün, {self.symbols} coin, {self.first_date} → {self.last_date}", 1, 1)

    def thr(self, feat: str, q: float) -> float:
        return float(self.Q[feat][int(round(min(1, max(0, q)) * (QGRID - 1)))])

    def q_of(self, feat: str, value: float) -> float:
        return float(np.searchsorted(self.Q[feat], value) / (QGRID - 1))


# ---------------------------------------------------------------- live: today's rows

def today_rows(top_n: int = TOP_N) -> dict:
    """Features of today's universe from the finished candles (the same formulas as the pool). Today = the UTC day
    after the last finished BTC candle."""
    _all, bars = load_bars()
    btc = bars.get("BTCUSDT")
    if btc is None:
        return {"rows": [], "day": None}
    last = btc["d"][-1]
    today = date.fromordinal(date.fromisoformat(last).toordinal() + 1).isoformat()
    fng = fng_by_day()
    btc_e = end_stats(btc["o"], btc["h"], btc["l"], btc["c"], btc["qv"], btc["v"], btc["tb"])
    mk = _market(btc_e, btc["d"], fng, [today])
    cand = []
    for s, b in bars.items():
        if "#" in s or b["d"][-1] != last or len(b["d"]) < MIN_HISTORY:
            continue                                   # no candle yesterday: delisted / halted
        e = end_stats(b["o"], b["h"], b["l"], b["c"], b["qv"], b["v"], b["tb"])
        f = {k: float(e[k][-1]) for k in e if k in FEATURES or k == "ret30"}
        if not math.isfinite(f.get("dvol", float("nan"))):
            continue
        cand.append({"symbol": s, "close": float(b["c"][-1]), "f": f})
    cand.sort(key=lambda r: -r["f"]["dvol"])
    rows = cand[:top_n]
    n = len(rows)
    br = sum(1 for r in rows if (r["f"].get("dist50") or 0) > 0) / max(1, n)
    for i, r in enumerate(rows):
        f = r["f"]
        f["breadth50"] = br
        f["vol_rank"] = 1 - i / max(n - 1, 1)
        for k in ("btc_ret1", "btc_ret7", "btc_dist50", "fng", "fng_chg7", "dow"):
            f[k] = float(mk[k][0])
        f["rs_btc30"] = (1 + f["ret30"]) / (1 + float(mk["btc_ret30"][0])) - 1 if math.isfinite(mk["btc_ret30"][0]) else float("nan")
    fv = float(mk["fng"][0]) if math.isfinite(mk["fng"][0]) else None
    return {"rows": rows, "day": today, "last_candle": last, "fng": fv}
