"""Evrim havuzu: a genetic algorithm that breeds trading rules, keeps (clones) the best, crosses and mutates them,
and promotes the winners stage by stage to stocks they have never seen.

A genome is one bot: 1-3 entry conditions on features known at the open ("gap > 2.1 %", "RSI(14) < 35",
"sector-relative 20-day strength < -5 %", ...), a side (long/short) and an exit rule (stop: fixed % / x ATR / none;
target = stop x ratio; hold 1-5 days). Slippage grows for thinner stocks (3-20 bps per side).

Tournament (aşamalı turnuva). The ~2,600 stocks are split into 5 fixed groups (A-E) and the last 6 months of every
stock are locked away (kilitli kasa):
  stage 1  breed on A               -> exam on B (never seen)
  stage 2  breed on A+B (winners of stage 1 are cloned into the pool) -> exam on C
  stage 3  breed on A+B+C           -> exam on D
  stage 4  breed on A+B+C+D         -> exam on E
  final    the last survivors have now seen every stock in the training years; the locked last 6 months of all
           stocks are opened once. After that only live paper trading is unseen.
Inside each stage breeding stops early when the stage's own validation year stops improving (it would start to
memorise). A bot survives an exam when it beats the market drift there with enough trades and is positive in most
years. The higher bar ("çıtayı aşan") also requires beating a plain buy-the-dip rule (RSI(14) < 35, hold 5 days).

Fitness = t-statistic of the bot's real net return (after slippage), computed on DAILY averages so that hundreds of
trades on one crash day count as one event; scaled by the share of positive years, minus a small penalty per extra
condition. A bot must make money, not just lose less than random: an earlier version scored "excess over random
entry" and the pool learned that low-volatility stocks lose less under a tight 1 %/1 % bracket (still losing money).
To survive an exam a bot must ALSO beat the market drift (excess > 0). Reports split the edge into stock picking
(vs. all stocks on the same days) and market timing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from datetime import date

import numpy as np

from ..util import now_ts
from . import bots, download, hakem, store

log = logging.getLogger("marketdesk.lab")

COND_FEATURES = {
    "gap": ("Açılış boşluğu", "pct"), "ret1": ("Dünkü getiri", "pct"), "ret5": ("Son 5 gün getirisi", "pct"),
    "ret20": ("Son 20 gün getirisi", "pct"), "rvol": ("Dünkü göreli hacim", "x"), "rsi2": ("RSI(2)", "rsi"),
    "rsi14": ("RSI(14)", "rsi"), "atr": ("Oynaklık (ATR)", "pct"), "dist20": ("20 günlük ortalamaya uzaklık", "pct"),
    "dist50": ("50 günlük ortalamaya uzaklık", "pct"), "dist200": ("200 günlük ortalamaya uzaklık", "pct"),
    "brk20": ("20 günlük zirveye uzaklık", "pct"), "brkl20": ("20 günlük dibe uzaklık", "pct"),
    "dist_52h": ("52 haftalık zirveye uzaklık", "pct"), "dist_52l": ("52 haftalık dibe uzaklık", "pct"),
    "down_streak": ("Art arda düşüş günü", "int"), "rs_spy20": ("SPY'ye göre 20 günlük güç", "pct"),
    "rs_sector20": ("Sektörüne göre 20 günlük güç", "pct"), "breadth50": ("Piyasa genişliği (50 gün ort. üstü hisse oranı)", "share"),
    "surprise": ("EPS sürprizi (bilanço günü)", "pctraw"), "days_to_earn": ("Bilançoya kalan işlem günü", "int"),
    "days_since_earn": ("Bilançodan bu yana işlem günü", "int"), "news_score": ("Gece haber tonu", "num"),
    "news_n": ("Gece haber sayısı", "int"), "filing_score": ("8-K olay puanı", "num"),
    "spy_ret1": ("SPY dünkü getiri", "pct"), "spy_gap": ("SPY açılış boşluğu", "pct"), "vix": ("VIX", "vix"),
    "dvol": ("Günlük işlem hacmi (20 gün ort.)", "usd"), "dow": ("Haftanın günü (0=Pzt … 4=Cum)", "int"),
    "month_turn": ("Ay başı / sonu günü", "flag"),
}
SPARSE = {"surprise", "news_score", "news_n", "filing_score"}   # mostly 0: thresholds come from the non-zero values
STOP_SET = (("pct", 1.0), ("pct", 2.0), ("pct", 3.0), ("pct", 5.0), ("atr", 0.5), ("atr", 1.0), ("atr", 1.5), ("atr", 2.0),
            ("none", 0.0))
RATIO_SET = (1.0, 1.5, 2.0, 3.0)
HOLD_SET = (1, 2, 3, 5)
POP, MAX_GEN, ELITE, TOURNAMENT, MAX_CONDS = 80, 120, 6, 3, 3
PATIENCE, VAL_TOP = 15, 10
MIN_TRADES, MIN_EXAM_TRADES = 150, 40
MIN_DAYS, MIN_EXAM_DAYS = 40, 15     # distinct trading days: 500 trades on 3 crash days are 3 events, not 500
GROUPS = 5
LOCK_MONTHS, INNER_VAL_MONTHS = 6, 12
QGRID = 201
FEAT_DTYPE = np.float16    # ~2,600 stocks x 10 years = millions of stock-days: half the memory of float32
PATH_NAN = -32768          # int16 marker for "no bar" in the stored price paths
HALL = 20
POS_YEARS = 0.6

# Market regimes (known at the open: the previous close's VIX and today's market breadth). The live learning keeps a
# scorecard per regime for every bot and the referee weights a bot by its record in today's regime.
REGIMES = ("korku", "yukselis", "zayif")
REGIME_TR = {"korku": "Korku günü (VIX ≥ 20)", "yukselis": "Sakin, geniş yükseliş", "zayif": "Sakin, zayıf piyasa"}
REGIME_VIX = 0.20          # the pool stores VIX / 100
REGIME_BREADTH = 0.5       # share of stocks above their 50-day average


def regime_of(vix: float | None, breadth: float | None) -> str | None:
    """`vix` as VIX / 100 (0.20 = 20), `breadth` as a share. None when neither is known."""
    def ok(x):
        return x is not None and not (isinstance(x, float) and math.isnan(x))
    if ok(vix) and vix >= REGIME_VIX:
        return "korku"
    if not ok(breadth):
        return None if not ok(vix) else "yukselis"
    return "yukselis" if breadth >= REGIME_BREADTH else "zayif"


def day_regimes(pool: "Pool") -> np.ndarray:
    """Regime code per day index of the pool (index into REGIMES; -1 = unknown)."""
    nd = pool.n_days
    vix = np.full(nd, np.nan, dtype=np.float32)
    br = np.full(nd, np.nan, dtype=np.float32)
    v = pool.F["vix"].astype(np.float32)
    ok = ~np.isnan(v)
    vix[pool.dayidx[ok]] = v[ok]            # one value per day, repeated on every stock row
    br[pool.dayidx] = pool.F["breadth50"].astype(np.float32)
    out = np.full(nd, -1, dtype=np.int8)
    known_b = ~np.isnan(br)
    out[known_b] = np.where(br[known_b] >= REGIME_BREADTH, 1, 2)
    out[~known_b & ~np.isnan(vix)] = 1
    out[vix >= REGIME_VIX] = 0
    return out
MIN_COVERAGE = 0.0       # daily-bot tournaments: share of the split's trading days a bot must trade on (run(mode='daily'))


def slippage(dvol: np.ndarray) -> np.ndarray:
    """Per-side slippage from the 20-day average dollar volume."""
    return np.where(dvol >= 1e8, 0.0003, np.where(dvol >= 2e7, 0.0005, np.where(dvol >= 5e6, 0.0010, 0.0020))).astype(np.float32)


# ---------------------------------------------------------------- data

def _ret20_by_date(b: dict) -> dict[str, float]:
    c = b["c"]
    r = bots._shift(c) / bots._shift(c, 21) - 1
    return {d: float(x) for d, x in zip(b["d"], r)}


class Pool:
    """Every liquid stock-day (features known at the open + 5-day price path relative to the entry open)."""

    def __init__(self, progress=None, max_symbols: int | None = None):
        con = store.conn()
        uni = {r[0]: r[1] or "" for r in con.execute("SELECT symbol, sector FROM universe ORDER BY rank")}
        if max_symbols:          # quick test runs only
            uni = dict(list(uni.items())[:max_symbols])
        syms = list(uni)
        refs = list(download.REFERENCE)
        ev = bots.load_events(syms)
        news_from = {r[0]: r[1] for r in con.execute("SELECT symbol, MIN(ts) FROM news GROUP BY symbol")}
        ref_bars = bots.load_bars(refs)
        ctx = bots._context(ref_bars)
        spy20 = _ret20_by_date(ref_bars["SPY"]) if "SPY" in ref_bars else {}
        sect20 = {etf: _ret20_by_date(ref_bars[etf]) for etf in download.SECTOR_ETF.values() if etf in ref_bars}
        start = (ref_bars.get("SPY") or {}).get("d", [""])[0]
        keys = [k for k in COND_FEATURES if k not in ("rs_spy20", "rs_sector20", "breadth50", "dow", "month_turn")]
        feats = {k: [] for k in keys}
        rs_spy, rs_sec, sym_l, date_l, atr_l, dist50_l, slip_l, paths = [], [], [], [], [], [], [], []
        sym_codes = {s_: i for i, s_ in enumerate(syms)}
        params = dict(bots.DEFAULT_PARAMS)
        for i0 in range(0, len(syms), 100):          # bars are loaded 100 stocks at a time to keep memory low
            chunk = syms[i0:i0 + 100]
            bars = bots.load_bars(chunk)
            for sym in chunk:
                b = bars.get(sym)
                if not b:
                    continue
                young = bool(start) and (date.fromisoformat(b["d"][0]) - date.fromisoformat(start)).days > 60
                r = bots.compute_symbol(sym, b, ev, ctx, params, news_from.get(sym), young_listing=young, all_rows=True)
                if not r:
                    continue
                for k in keys:
                    x = r["feats"][k]
                    if k == "dvol":                     # dollars overflow float16: kept as log10
                        slip_l.append(slippage(np.nan_to_num(x)))
                        x = np.log10(np.maximum(x, 1.0))
                    feats[k].append(np.clip(x, -60000, 60000).astype(FEAT_DTYPE))   # float16 range
                dts = r["date"]
                s20 = np.array([spy20.get(d, np.nan) for d in dts])
                e20 = sect20.get(download.SECTOR_ETF.get(uni[sym], ""), {})
                x20 = np.array([e20.get(d, np.nan) for d in dts]) if e20 else np.full(len(dts), np.nan)
                own = r["feats"]["ret20"]
                rs_spy.append(((1 + own) / (1 + s20) - 1).astype(FEAT_DTYPE))
                rs_sec.append(((1 + own) / (1 + x20) - 1).astype(FEAT_DTYPE))
                sym_l.append(np.full(len(dts), sym_codes[sym], dtype=np.int16))
                date_l.append(np.array(dts))
                atr_l.append(r["feats"]["atr"].astype(np.float32))
                dist50_l.append(r["feats"]["dist50"].astype(np.float32))
                p = r["paths"]                           # (4, m, 5): open, high, low, close
                # basis points vs the entry open in int16 (1 bp precision, ±327 %): 4x smaller than float64, exact
                # enough for 1 % stops and 3 bps slippage (float16 would round to ~10 bp)
                with np.errstate(invalid="ignore"):
                    bp = np.clip(np.round((p / p[0][:, :1] - 1) * 10000), -32000, 32000)
                paths.append(np.where(np.isnan(bp), PATH_NAN, bp).astype(np.int16))
            if progress:
                progress(f"Havuz verisi hazırlanıyor: {min(len(syms), i0 + 100)}/{len(syms)} hisse", i0 + 100, len(syms))
        self.F = {}
        for k in list(feats):
            self.F[k] = np.concatenate(feats.pop(k))
        self.F["rs_spy20"] = np.concatenate(rs_spy)
        self.F["rs_sector20"] = np.concatenate(rs_sec)
        sym_arr = np.concatenate(sym_l)
        date_all = np.concatenate(date_l)
        self.atr = np.concatenate(atr_l)
        self.slip = np.concatenate(slip_l)
        P = np.concatenate(paths, axis=1)
        del paths
        self.O, self.H, self.L, self.C = P[0], P[1], P[2], P[3]
        self.n = len(date_all)
        # cross-sectional / calendar features
        udates, inv = np.unique(date_all, return_inverse=True)
        del date_all
        above = np.nan_to_num(np.concatenate(dist50_l)) > 0
        self.F["breadth50"] = (np.bincount(inv, weights=above) / np.maximum(1, np.bincount(inv))).astype(FEAT_DTYPE)[inv]
        del above, dist50_l
        dows = np.array([date.fromisoformat(d).weekday() for d in udates.tolist()], dtype=FEAT_DTYPE)
        self.F["dow"] = dows[inv]
        months = np.array([d[:7] for d in udates.tolist()])
        turn = np.zeros(len(udates), dtype=FEAT_DTYPE)
        for m in np.unique(months):
            ix = np.nonzero(months == m)[0]
            turn[ix[:3]] = 1
            turn[ix[-2:]] = 1
        self.F["month_turn"] = turn[inv]
        self.day_year = np.array([int(d[:4]) for d in udates.tolist()], dtype=np.int16)
        self.dayidx = inv.astype(np.int32)
        del inv
        self.n_days = len(udates)
        # splits: groups of stocks, locked last months, inner validation year
        days = udates.tolist()
        last = date.fromisoformat(days[-1])
        self.lock_start = hakem._months_back(last, LOCK_MONTHS)
        self.inner_val = hakem._months_back(date.fromisoformat(self.lock_start), INNER_VAL_MONTHS)

        def at(d: str) -> int:          # day index of a date
            return int(np.searchsorted(udates, d))

        def purge(d: str) -> int:       # trades entered in the last days before a boundary end after it
            return max(0, at(d) - max(HOLD_SET))

        di = self.dayidx
        self.region = di < purge(self.lock_start)
        self.inner_train = di < purge(self.inner_val)
        self.inner_valm = (di >= at(self.inner_val)) & self.region
        self.lock = di >= at(self.lock_start)
        present = np.unique(sym_arr)
        code_group = np.zeros(len(syms), dtype=np.int8)
        for c_ in present.tolist():
            code_group[c_] = int(hashlib.sha1(syms[c_].encode()).hexdigest(), 16) % GROUPS
        self.group = code_group[sym_arr]
        self.group_symbols = [int(np.sum(code_group[present] == g)) for g in range(GROUPS)]
        self.symbols = int(len(present))
        self.symcode = sym_arr
        self.sym_names = syms
        self.udates = days
        self.first_date = days[0]
        self.last_date = days[-1]
        tr = self.region
        self.Q = {}
        for k in COND_FEATURES:
            x = self.F[k][tr].astype(np.float32)
            x = x[~np.isnan(x)]
            if k in SPARSE:
                x = x[x != 0]
            self.Q[k] = np.quantile(x, np.linspace(0, 1, QGRID)) if len(x) else np.zeros(QGRID)
        self.masks: dict[str, np.ndarray] = {}
        self._base: dict = {}
        self._dbase: dict = {}
        self._sdays: dict = {}
        self._rng = np.random.default_rng(7)

    def set_mask(self, name: str, m: np.ndarray) -> None:
        self.masks[name] = m
        self._base = {k: v for k, v in self._base.items() if k[0] != name}
        self._dbase = {k: v for k, v in self._dbase.items() if k[0] != name}
        self._sdays.pop(name, None)

    def split_days(self, split: str) -> int:
        """Trading days in a split (coverage = share of them a bot trades on)."""
        if split not in self._sdays:
            self._sdays[split] = int(len(np.unique(self.dayidx[self.masks[split]])))
        return self._sdays[split]

    def thr(self, feat: str, q: float) -> float:
        return float(self.Q[feat][int(round(min(1, max(0, q)) * (QGRID - 1)))])

    def q_of(self, feat: str, value: float) -> float:
        if feat == "dvol":
            value = math.log10(max(value, 1.0))
        return float(np.searchsorted(self.Q[feat], value) / (QGRID - 1))

    def mask(self, g: dict) -> np.ndarray:
        m = np.ones(self.n, dtype=bool)
        for feat, op, q in g["conds"]:
            t = self.thr(feat, q)
            x = self.F[feat]
            m &= (x > t) if op == ">" else (x < t)
            if feat in SPARSE:
                m &= x != 0
        return m

    def outcome(self, idx: np.ndarray, side: int, stop: int, ratio: int, hold: int, detail: bool = False):
        """Net return of a trade from each row's open; with `detail` also the day offset of the exit (0 = entry day)
        and the close-to-close mark (price / entry) of every day for marking open positions."""
        kind, xk = STOP_SET[stop]
        hold_d = HOLD_SET[hold]
        def ratio_(a):     # int16 basis points -> price / entry open (NaN where the path ended)
            a = a[idx, :hold_d]
            return np.where(a == PATH_NAN, np.nan, 1 + a.astype(np.float32) / 10000)

        O, H, L, C = ratio_(self.O), ratio_(self.H), ratio_(self.L), ratio_(self.C)
        slip = self.slip[idx]
        if kind == "pct":
            sl = np.full(len(idx), xk / 100, dtype=np.float32)
        elif kind == "atr":
            sl = np.clip(np.nan_to_num(self.atr[idx], nan=0.02) * xk, 0.003, 0.25)
        else:
            sl = np.full(len(idx), np.inf, dtype=np.float32)
        tp = sl * RATIO_SET[ratio]
        entry = 1 + slip * side                      # prices are relative to the entry day's open
        stop_p, tgt_p = entry * (1 - sl * side), entry * (1 + tp * side)
        res = np.full(len(idx), np.nan, dtype=np.float32)
        ex_j = np.full(len(idx), -1, dtype=np.int8)
        live = ~np.isnan(C[:, hold_d - 1])
        with np.errstate(invalid="ignore"):
            for j in range(hold_d):
                oj, hj, lj, cj = O[:, j], H[:, j], L[:, j], C[:, j]
                if j > 0:
                    g = live & ((oj <= stop_p) if side > 0 else (oj >= stop_p))
                    res[g] = side * (oj[g] * (1 - slip[g] * side) / entry[g] - 1)
                    ex_j[g] = j
                    live &= ~g
                    g = live & ((oj >= tgt_p) if side > 0 else (oj <= tgt_p))
                    res[g] = side * (oj[g] * (1 - slip[g] * side) / entry[g] - 1)
                    ex_j[g] = j
                    live &= ~g
                g = live & ((lj <= stop_p) if side > 0 else (hj >= stop_p))
                res[g] = -sl[g] - slip[g]
                ex_j[g] = j
                live &= ~g
                g = live & ((hj >= tgt_p) if side > 0 else (lj <= tgt_p))
                res[g] = tp[g] - slip[g]
                ex_j[g] = j
                live &= ~g
                if j == hold_d - 1:
                    res[live] = side * (cj[live] * (1 - slip[live] * side) / entry[live] - 1)
                    ex_j[live] = j
        if detail:
            with np.errstate(invalid="ignore"):
                marks = side * (C / entry[:, None] - 1)
            return res, ex_j, marks
        return res

    def daily_base(self, split: str, side: int, stop: int, ratio: int, hold: int) -> np.ndarray:
        """Same-day average return of all stocks in the split (sampled) with this side and exit, per day index."""
        key = (split, side, stop, ratio, hold)
        if key not in self._dbase:
            idx = np.nonzero(self.masks[split])[0]
            if len(idx) > 400000:
                idx = np.sort(self._rng.choice(idx, 400000, replace=False))
            r = self.outcome(idx, side, stop, ratio, hold)
            ok = ~np.isnan(r)
            d = self.dayidx[idx][ok]
            s_ = np.bincount(d, weights=r[ok], minlength=self.n_days)
            c_ = np.bincount(d, minlength=self.n_days)
            with np.errstate(invalid="ignore", divide="ignore"):
                self._dbase[key] = np.where(c_ > 0, s_ / np.maximum(c_, 1), np.nan)
        return self._dbase[key]

    def baseline(self, split: str, side: int, stop: int, ratio: int, hold: int) -> float:
        """Average return of entering every stock-day of the split (sampled) with this side and exit: market drift."""
        key = (split, side, stop, ratio, hold)
        if key not in self._base:
            idx = np.nonzero(self.masks[split])[0]
            if len(idx) > 80000:
                idx = np.sort(self._rng.choice(idx, 80000, replace=False))
            r = self.outcome(idx, side, stop, ratio, hold)
            self._base[key] = float(np.nanmean(r)) if len(r) else 0.0
        return self._base[key]


# ---------------------------------------------------------------- genomes

def _sig(g: dict) -> str:
    conds = sorted((f, op, round(q / 0.02) * 0.02) for f, op, q in g["conds"])
    return json.dumps([conds, g["side"], g["stop"], g["ratio"], g["hold"]])


def _random_cond(rng: random.Random, exclude: set) -> list:
    feat = rng.choice([f for f in COND_FEATURES if f not in exclude])
    op = rng.choice((">", "<"))
    q = rng.uniform(0.55, 0.98) if op == ">" else rng.uniform(0.02, 0.45)
    return [feat, op, q]


def random_genome(rng: random.Random) -> dict:
    conds: list = []
    for _ in range(rng.choice((1, 1, 2, 2, 3))):
        conds.append(_random_cond(rng, {c[0] for c in conds}))
    return {"conds": conds, "side": rng.choice((1, -1)), "stop": rng.randrange(len(STOP_SET)),
            "ratio": rng.randrange(len(RATIO_SET)), "hold": rng.randrange(len(HOLD_SET))}


def dip_rule(pool: Pool) -> dict:
    """The bar to beat: plain buy-the-dip, RSI(14) < 35, no stop, hold 5 days."""
    return {"conds": [["rsi14", "<", pool.q_of("rsi14", 0.35)]], "side": 1, "stop": len(STOP_SET) - 1, "ratio": 0,
            "hold": HOLD_SET.index(5)}


def seed_genomes(pool: Pool) -> list[dict]:
    """The four original bots and the plain dip rule as genomes (cloning starts from known rules)."""
    q = pool.q_of
    base = [
        ([["brk20", ">", q("brk20", 0.0)], ["rvol", ">", q("rvol", 1.5)], ["dist50", ">", q("dist50", 0.0)]], 1),
        ([["rsi2", "<", q("rsi2", 0.10)], ["dist200", ">", q("dist200", 0.0)]], 1),
        ([["gap", ">", q("gap", 0.02)], ["dist20", ">", q("dist20", 0.0)]], 1),
        ([["gap", ">", q("gap", 0.02)], ["dist20", ">", q("dist20", 0.0)]], -1),
        ([["surprise", ">", q("surprise", 5.0)]], 1),
        ([["surprise", "<", q("surprise", -5.0)]], -1),
    ]
    out = [dip_rule(pool)]
    for conds, side in base:
        for hold in (0, 2):
            out.append({"conds": [list(c) for c in conds], "side": side, "stop": 1, "ratio": 2, "hold": hold})
    return out


def mutate(g: dict, rng: random.Random) -> dict:
    g = json.loads(json.dumps(g))
    for c in g["conds"]:
        if rng.random() < 0.35:
            c[2] = min(0.99, max(0.01, c[2] + rng.gauss(0, 0.07)))
        if rng.random() < 0.05:
            c[1] = ">" if c[1] == "<" else "<"
        if rng.random() < 0.05:
            c[0] = rng.choice([f for f in COND_FEATURES if f not in {x[0] for x in g["conds"]}] or [c[0]])
    if rng.random() < 0.15 and len(g["conds"]) < MAX_CONDS:
        g["conds"].append(_random_cond(rng, {c[0] for c in g["conds"]}))
    if rng.random() < 0.15 and len(g["conds"]) > 1:
        g["conds"].pop(rng.randrange(len(g["conds"])))
    if rng.random() < 0.05:
        g["side"] = -g["side"]
    for k, n in (("stop", len(STOP_SET)), ("ratio", len(RATIO_SET)), ("hold", len(HOLD_SET))):
        if rng.random() < 0.2:
            g[k] = min(n - 1, max(0, g[k] + rng.choice((-1, 1))))
    return g


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    pool_c, seen = [], set()
    for c in rng.sample(a["conds"] + b["conds"], len(a["conds"]) + len(b["conds"])):
        if c[0] not in seen:
            pool_c.append(list(c))
            seen.add(c[0])
    k = rng.randint(1, min(MAX_CONDS, len(pool_c)))
    return {"conds": pool_c[:k], "side": a["side"], "stop": rng.choice((a["stop"], b["stop"])),
            "ratio": rng.choice((a["ratio"], b["ratio"])), "hold": rng.choice((a["hold"], b["hold"]))}


# ---------------------------------------------------------------- scoring

def stats(pool: Pool, g: dict, split: str, m: np.ndarray | None = None) -> dict:
    m = pool.mask(g) if m is None else m
    idx = np.nonzero(m & pool.masks[split])[0]
    if not len(idx):
        return {"n": 0}
    key = (g["side"], g["stop"], g["ratio"], g["hold"])
    r = pool.outcome(idx, *key)
    ok = ~np.isnan(r)
    r, days = r[ok].astype(np.float64), pool.dayidx[idx][ok]
    if not len(r):
        return {"n": 0}
    base = pool.baseline(split, *key)
    ex = r - base
    # daily clusters: one observation per trading day (trades on the same day move together)
    ud, inv = np.unique(days, return_inverse=True)
    cnt = np.bincount(inv)
    dm = np.bincount(inv, weights=ex) / cnt          # daily excess over the drift
    dr = np.bincount(inv, weights=r) / cnt           # daily real net return
    n_days = len(ud)
    t = float(dr.mean() / (dr.std() + 1e-9) * math.sqrt(n_days)) if n_days > 1 else 0.0
    t_ex = float(dm.mean() / (dm.std() + 1e-9) * math.sqrt(n_days)) if n_days > 1 else 0.0
    dyears = pool.day_year[ud]
    years = {}
    for y in np.unique(dyears):
        sel = dyears == y
        if sel.sum() >= 5 and cnt[sel].sum() >= 20:
            years[int(y)] = round(float(dr[sel].mean() * 100), 3)
    same_day = pool.daily_base(split, *key)[days]
    pick = r - np.nan_to_num(same_day, nan=base)
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": int(len(r)), "days": int(n_days), "coverage": round(n_days / max(1, pool.split_days(split)), 3),
            "pos_days": round(float((dr > 0).mean() * 100), 1), "avg_pct": round(float(r.mean() * 100), 3),
            "daily_pct": round(float(dr.mean() * 100), 3), "t_excess": round(t_ex, 2),
            "excess_pct": round(float(ex.mean() * 100), 3), "daily_excess_pct": round(float(dm.mean() * 100), 3),
            "pick_pct": round(float(pick.mean() * 100), 3), "timing_pct": round(float((ex.mean() - pick.mean()) * 100), 3),
            "win_rate": round(float((r > 0).mean() * 100), 1), "t": round(t, 2), "base_pct": round(base * 100, 3),
            "profit_factor": round(float(pos / neg), 2) if neg > 0 else None, "years": years,
            "pos_years": round(sum(1 for v in years.values() if v > 0) / len(years), 2) if years else 0,
            "trades_per_day": round(len(r) / max(1, n_days), 2)}


def fitness(pool: Pool, g: dict, split: str) -> tuple[float, dict]:
    s = stats(pool, g, split)
    n, nd = s.get("n", 0), s.get("days", 0)
    if n < MIN_TRADES or nd < MIN_DAYS:
        return -5 + 2.5 * min(1, n / MIN_TRADES) + 2.5 * min(1, nd / MIN_DAYS), s
    f = s["t"] * (0.4 + 0.6 * s["pos_years"]) - 0.25 * (len(g["conds"]) - 1)
    if MIN_COVERAGE:      # daily bots: trading on too few days costs up to 6 points
        f -= 6 * max(0.0, MIN_COVERAGE - s.get("coverage", 0)) / MIN_COVERAGE
    return f, s


# ---------------------------------------------------------------- description (Turkish)

def _fmt(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def describe(pool: Pool, g: dict) -> dict:
    parts = []
    for feat, op, q in g["conds"]:
        label, unit = COND_FEATURES[feat]
        t = pool.thr(feat, q)
        sign = "-" if t < 0 else ""
        if feat == "dvol":
            t = 10 ** t                                  # stored as log10 dollars
        if unit == "flag":
            parts.append(f"{label}: {'evet' if op == '>' else 'hayır'}")
            continue
        val = {"pct": f"{sign}%{_fmt(abs(t) * 100)}", "rsi": _fmt(t * 100, 0), "x": f"{_fmt(t, 1)}x", "vix": _fmt(t * 100, 1),
               "pctraw": f"{sign}%{_fmt(abs(t), 1)}", "int": f"{t:.0f}", "num": _fmt(t), "share": f"%{_fmt(t * 100, 0)}",
               "usd": f"{_fmt(t / 1e6, 0)} M$"}.get(unit, _fmt(t))
        parts.append(f"{label} {'>' if op == '>' else '<'} {val}")
    kind, x = STOP_SET[g["stop"]]
    ratio, hold = RATIO_SET[g["ratio"]], HOLD_SET[g["hold"]]
    if kind == "pct":
        ex = f"zarar durdur %{_fmt(x, 1)}, kâr al %{_fmt(x * ratio, 1)}"
    elif kind == "atr":
        ex = f"zarar durdur {_fmt(x, 1)}×ATR, kâr al {_fmt(x * ratio, 1)}×ATR"
    else:
        ex = "zarar durdur yok"
    side = "Yükseliş" if g["side"] > 0 else "Düşüş"
    return {"entry": " VE ".join(parts), "side": side, "exit": ex, "hold": hold,
            "text": f"{' VE '.join(parts)} → {side} · {ex} · {hold} gün"}


# ---------------------------------------------------------------- one breeding stage

def _breed(pool: Pool, rng: random.Random, start_pop: list[dict], pop_size: int, max_gen: int, stage_label: str,
           progress=None) -> tuple[list, list, str]:
    """Evolve on mask 'breed'; stop early when the best of the top bots stops improving on 'bval'."""
    cache: dict[str, tuple[float, dict, dict]] = {}
    val_cache: dict[str, float] = {}

    def evaluate(g: dict) -> float:
        s_ = _sig(g)
        if s_ not in cache:
            f, st = fitness(pool, g, "breed")
            cache[s_] = (f, st, g)
        return cache[s_][0]

    def val_score(g: dict) -> float:
        s_ = _sig(g)
        if s_ not in val_cache:
            v = stats(pool, g, "bval")
            val_cache[s_] = v["t"] if v.get("n", 0) >= MIN_EXAM_TRADES and v.get("days", 0) >= MIN_EXAM_DAYS else -9.0
        return val_cache[s_]

    population = [json.loads(json.dumps(g)) for g in start_pop]
    while len(population) < pop_size:
        population.append(random_genome(rng))
    history, best_val, since, reason = [], -1e9, 0, f"Azami nesil ({max_gen})"
    for gen in range(max_gen):
        scored = sorted(((evaluate(g), g) for g in population), key=lambda x: -x[0])
        fits = [f for f, _ in scored]
        cands = [g for _, g in scored[:VAL_TOP] if cache[_sig(g)][1].get("days", 0) >= MIN_DAYS]
        vbest = max((val_score(g) for g in cands), default=-9.0)
        if vbest > best_val + 0.05:
            best_val, since = vbest, 0
        else:
            since += 1
        history.append({"gen": gen + 1, "best": round(fits[0], 2), "median": round(float(np.median(fits)), 2),
                        "val_best": round(vbest, 2), "evaluated": len(cache)})
        if progress:
            progress(f"{stage_label} · nesil {gen + 1}: eğitim {fits[0]:.1f} · doğrulama {vbest:.1f} "
                     f"({since}/{PATIENCE} nesildir gelişme yok) · {len(cache)} farklı bot", gen + 1, max_gen)
        if since >= PATIENCE:
            reason = f"Doğrulama {PATIENCE} nesildir gelişmiyor (ezber başlamadan durdu)"
            break
        nxt, seen = [json.loads(json.dumps(g)) for _, g in scored[:ELITE]], {_sig(g) for _, g in scored[:ELITE]}

        def pick() -> dict:
            return max(rng.sample(scored, TOURNAMENT), key=lambda x: x[0])[1]

        tries = 0
        while len(nxt) < pop_size and tries < pop_size * 20:
            tries += 1
            child = crossover(pick(), pick(), rng) if rng.random() < 0.5 else json.loads(json.dumps(pick()))
            child = mutate(child, rng)
            if rng.random() < 0.05:
                child = random_genome(rng)
            s_ = _sig(child)
            if s_ not in seen:
                seen.add(s_)
                nxt.append(child)
        population = nxt
    # hall of fame: best distinct bots
    ranked = sorted(cache.values(), key=lambda x: -x[0])
    hall, taken = [], []
    for f, st, g in ranked:
        if st.get("n", 0) < MIN_TRADES or st.get("days", 0) < MIN_DAYS:
            continue
        m = pool.mask(g) & pool.masks["breed"]
        if any((m & t).sum() / max(1, min(m.sum(), t.sum())) > 0.7 for t in taken):
            continue
        taken.append(m)
        hall.append((f, st, g))
        if len(hall) >= HALL:
            break
    return hall, history, reason


def _passes(s: dict, bar: float) -> bool:
    """Enough trades on enough days, real money made (daily average above `bar`), beats the drift, positive in most years."""
    return bool(s.get("n", 0) >= MIN_EXAM_TRADES and s.get("days", 0) >= MIN_EXAM_DAYS and s.get("daily_pct", -1) > bar
                and s.get("avg_pct", -1) > 0 and s.get("daily_excess_pct", -1) > 0 and s.get("pos_years", 0) >= POS_YEARS
                and s.get("coverage", 1) >= MIN_COVERAGE * 0.75)


# ---------------------------------------------------------------- portfolio simulation (realistic limits)

PORT_MAX_NEW, PORT_POS_FRAC, PORT_MAX_POS = 5, 0.10, 10


def bot_weight(e: dict) -> float:
    """Referee weight of an evolved bot: its daily t-statistic on the stocks it had never seen (0 when negative)."""
    return max(0.0, float((e.get("exam") or {}).get("t") or 0.0))


def _candidates(pool: Pool, members: list[dict], split_mask: np.ndarray) -> dict:
    """Per row where at least one member fires: referee score (sum of weights of members agreeing on the side) and the
    trade of the strongest member (its exit rule)."""
    best: dict[tuple, tuple] = {}
    score: dict[tuple, float] = {}
    marks_of: list = []
    for b_i, mb in enumerate(members):
        g, w = mb["genome"], mb["weight"]
        idx = np.nonzero(pool.mask(g) & split_mask)[0]
        if not len(idx) or w <= 0:
            marks_of.append(None)
            continue
        res, ex_j, marks = pool.outcome(idx, g["side"], g["stop"], g["ratio"], g["hold"], detail=True)
        marks_of.append(marks)
        ok = np.nonzero(~np.isnan(res) & (ex_j >= 0))[0]
        for k, i, r, ej in zip(ok.tolist(), idx[ok].tolist(), res[ok].tolist(), ex_j[ok].tolist()):
            key = (i, g["side"])
            score[key] = score.get(key, 0.0) + w
            if key not in best or w > best[key][0]:
                best[key] = (w, r, ej, b_i, k)
    by_day: dict[int, list] = {}
    for (i, side), (w, r, ej, b_i, k) in best.items():
        by_day.setdefault(int(pool.dayidx[i]), []).append((score[(i, side)], i, side, r, ej, (b_i, k)))
    return by_day, marks_of


def _equity_stats(days: list[str], eq: list[float]) -> dict:
    if len(eq) < 2:
        return {}
    a = np.array(eq, dtype=np.float64)
    rets = a[1:] / a[:-1] - 1
    peak = np.maximum.accumulate(a)
    years = max(1e-9, len(a) / 252)
    return {"total_pct": round(float(a[-1] / a[0] - 1) * 100, 2), "cagr_pct": round(float((a[-1] / a[0]) ** (1 / years) - 1) * 100, 2),
            "max_dd_pct": round(float((a / peak - 1).min() * 100), 2),
            "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2) if rets.std() > 0 else None,
            "from": days[0], "to": days[-1]}


def _curve(days: list[str], eq: list[float], points: int = 260) -> list:
    out = [[d, round(float(v), 4)] for d, v in zip(days, eq)]
    if len(out) > points:
        step = len(out) / points
        out = [out[int(i * step)] for i in range(points)] + [out[-1]]
    return out


def portfolio(pool: Pool, members: list[dict], split_mask: np.ndarray, max_new: int = PORT_MAX_NEW,
              pos_frac: float = PORT_POS_FRAC, max_pos: int = PORT_MAX_POS) -> dict:
    """Daily simulation: each day the referee's best `max_new` signals open (strongest score first, one position per
    stock), each `pos_frac` of the account, at most `max_pos` open; positions are marked to market at every close and
    closed by their own exit rule. Starts with 1.0."""
    by_day, marks_of = _candidates(pool, members, split_mask)
    rsi2 = np.nan_to_num(pool.F["rsi2"].astype(np.float32), nan=9.0)     # tie-break: the more oversold stock first
    di = np.nonzero(split_mask)[0]
    if not len(di):
        return {"stats": {}}
    d0, d1 = int(pool.dayidx[di].min()), int(pool.dayidx[di].max())
    cash, eq_curve, days, open_pos, trades, wins, invested = 1.0, [], [], [], 0, 0, []
    for d in range(d0, d1 + 1):
        # new positions at the open (sized on yesterday's equity)
        equity_prev = eq_curve[-1] if eq_curve else 1.0
        held = {pool.symcode[p["row"]] for p in open_pos}
        slots = max_pos - len(open_pos)
        for sc, i, side, r, ej, (b_i, k) in sorted(by_day.get(d, ()), key=lambda x: (-x[0], float(rsi2[x[1]]))):
            if slots <= 0 or max_new <= 0:
                break
            if pool.symcode[i] in held or len([p for p in open_pos if p["day"] == d]) >= max_new:
                continue
            alloc = min(cash, equity_prev * pos_frac)
            if alloc <= 1e-6:
                break
            cash -= alloc
            open_pos.append({"row": i, "day": d, "alloc": alloc, "r": r, "exit": d + ej, "marks": marks_of[b_i][k, : ej + 1]})
            held.add(pool.symcode[i])
            slots -= 1
        # mark at the close, realise exits
        value, still = 0.0, []
        for p in open_pos:
            j = d - p["day"]
            if d >= p["exit"]:
                cash += p["alloc"] * (1 + p["r"])
                trades += 1
                wins += 1 if p["r"] > 0 else 0
            else:
                m = float(p["marks"][j]) if j < len(p["marks"]) and not np.isnan(p["marks"][j]) else 0.0
                value += p["alloc"] * (1 + m)
                still.append(p)
        open_pos = still
        eq = cash + value
        eq_curve.append(eq)
        invested.append(value / eq if eq > 0 else 0)
        days.append(pool.udates[d])
    st = _equity_stats(days, eq_curve)
    st.update(trades=trades, win_rate=round(wins / trades * 100, 1) if trades else None,
              exposure_pct=round(float(np.mean(invested)) * 100, 1), days=len(days))
    return {"stats": st, "curve": _curve(days, eq_curve)}


def spy_curve(pool: Pool, split_mask: np.ndarray) -> dict:
    di = np.nonzero(split_mask)[0]
    if not len(di):
        return {"stats": {}}
    d0, d1 = int(pool.dayidx[di].min()), int(pool.dayidx[di].max())
    closes = {r[0]: r[1] for r in store.conn().execute("SELECT d, c FROM bars WHERE symbol='SPY'")}
    days = [pool.udates[d] for d in range(d0, d1 + 1) if pool.udates[d] in closes]
    if len(days) < 2:
        return {"stats": {}}
    base = closes[days[0]]
    eq = [float(closes[d] / base) for d in days]
    return {"stats": _equity_stats(days, eq), "curve": _curve(days, eq)}


def news_check(pool: Pool, g: dict) -> dict:
    """News era only (the months with Finnhub headlines): the bot's trades split by the overnight news tone. Shows
    whether a news filter would have helped; the training years have no headlines, so the pool could not learn it."""
    era = pool.F["news_n"].astype(np.float32) >= 0
    first = None
    nz = np.nonzero(pool.F["news_n"].astype(np.float32) > 0)[0]
    if len(nz):
        first = int(pool.dayidx[nz].min())
        era = pool.dayidx >= first
    else:
        return {}
    m = pool.mask(g)
    ns = pool.F["news_score"].astype(np.float32)
    nn = pool.F["news_n"].astype(np.float32)
    out = {"from": pool.udates[first]}
    for name, sel in (("pos", ns > 0.05), ("neg", ns < -0.05), ("none", nn == 0)):
        pool.set_mask("_news", era & sel)
        out[name] = stats(pool, g, "_news", m)
    return out


def conds_abs(pool: Pool, g: dict) -> list:
    """Conditions with absolute thresholds (live trading evaluates them on today's features)."""
    return [[f, op, pool.thr(f, q), f in SPARSE] for f, op, q in g["conds"]]


# ---------------------------------------------------------------- the tournament

def run(max_generations: int = MAX_GEN, pop_size: int = POP, seed: int = 1, progress=None, max_symbols: int | None = None,
        mode: str = "fear") -> dict:
    """mode 'daily': bots must trade on at least 80 % of the days (fitness penalty, 60 % in the exams); their finals go
    to kv.evo_daily_finals instead of replacing the trading set."""
    global MIN_COVERAGE
    MIN_COVERAGE = 0.8 if mode == "daily" else 0.0
    try:
        return _run(max_generations, pop_size, seed, progress, max_symbols, mode)
    finally:
        MIN_COVERAGE = 0.0


def _run(max_generations: int, pop_size: int, seed: int, progress, max_symbols, mode: str) -> dict:
    t0 = time.time()
    rng = random.Random(seed)
    pool = Pool(progress, max_symbols=max_symbols)
    dip = dip_rule(pool)
    letters = "ABCDE"
    stages, carry = [], []
    for k in range(GROUPS - 1):
        breed_groups = list(range(k + 1))
        exam_group = k + 1
        in_breed = np.isin(pool.group, breed_groups)
        pool.set_mask("breed", in_breed & pool.inner_train & pool.region)
        pool.set_mask("bval", in_breed & pool.inner_valm)
        pool.set_mask("exam", (pool.group == exam_group) & pool.region)
        label = f"Aşama {k + 1}/{GROUPS - 1} ({'+'.join(letters[g] for g in breed_groups)} → {letters[exam_group]})"
        start = []
        for g in carry:                       # winners are cloned into the next pool, plus mutated copies
            start.append(g)
            start.append(mutate(g, rng))
            start.append(mutate(g, rng))
        start += seed_genomes(pool)
        hall, history, reason = _breed(pool, rng, start[:pop_size], pop_size, max_generations, label, progress)
        dip_exam = stats(pool, dip, "exam")
        dip_bar = max(0.0, dip_exam.get("daily_pct", 0) or 0)
        cands = []
        for f, st, g in hall:
            full = pool.mask(g)
            e = {"genome": g, "fitness": round(f, 2), "rule": describe(pool, g), "breed": st,
                 "val": stats(pool, g, "bval", full), "exam": stats(pool, g, "exam", full)}
            e["survived"] = bool(e["val"].get("days", 0) >= MIN_EXAM_DAYS and e["val"].get("daily_pct", -1) > 0
                                 and e["val"].get("daily_excess_pct", -1) > 0 and _passes(e["exam"], 0.0))
            e["beats_dip"] = bool(e["survived"] and _passes(e["exam"], dip_bar))
            cands.append(e)
        survivors = [e["genome"] for e in cands if e["survived"]]
        stages.append({"stage": k + 1, "breed_groups": [letters[g] for g in breed_groups], "exam_group": letters[exam_group],
                       "generations": len(history), "stop_reason": reason, "evaluated": history[-1]["evaluated"] if history else 0,
                       "history": history, "candidates": cands, "survivors": len(survivors),
                       "beats_dip": sum(1 for e in cands if e["beats_dip"]), "dip_exam": dip_exam, "dip_bar": round(dip_bar, 3)})
        # survivors carry on; if none survived, the stage's best bots carry on (they will meet new stocks again)
        carry = survivors or [e["genome"] for e in cands[:5]]

    # final: the last stage's survivors have seen every group in the training years -> open the locked months once
    pool.set_mask("lock", pool.lock)
    for g in range(GROUPS):
        pool.set_mask(f"lock_{g}", pool.lock & (pool.group == g))
    final_c = [e for e in stages[-1]["candidates"] if e["survived"]]
    dip_lock = stats(pool, dip, "lock")
    finals = []
    for e in final_c:
        full = pool.mask(e["genome"])
        lk = stats(pool, e["genome"], "lock", full)
        finals.append({"genome": e["genome"], "rule": e["rule"], "lock": lk,
                       "lock_groups": {letters[g]: stats(pool, e["genome"], f"lock_{g}", full) for g in range(GROUPS)},
                       "beats_dip_lock": bool(_passes(lk, max(0.0, dip_lock.get("daily_pct", 0) or 0)) if lk.get("n") else False)})
    # referee of the evolved bots (weights = exam t on unseen stocks) and realistic portfolios
    weight_of = {_sig(e["genome"]): bot_weight(e) for e in final_c}
    members = [{"genome": e["genome"], "weight": weight_of[_sig(e["genome"])]} for e in final_c]
    for f in finals:
        f["weight"] = round(weight_of[_sig(f["genome"])], 2)
        f["conds_abs"] = conds_abs(pool, f["genome"])
        f["news"] = news_check(pool, f["genome"])
    if progress:
        progress("Portföy simülasyonu (günde en fazla 5 işlem, her biri hesabın %10'u, en fazla 10 pozisyon)…", 0, 1)
    e_mask = (pool.group == GROUPS - 1) & pool.region
    dip_m = [{"genome": dip, "weight": 1.0}]
    portfolios = {
        "unseen_stocks": {"label": f"Görülmemiş hisseler (grup {letters[GROUPS - 1]}), {pool.first_date} – {pool.lock_start}",
                          "bots": portfolio(pool, members, e_mask), "dip": portfolio(pool, dip_m, e_mask), "spy": spy_curve(pool, e_mask)},
        "lock": {"label": f"Kilitli kasa: tüm hisseler, {pool.lock_start} – {pool.last_date}",
                 "bots": portfolio(pool, members, pool.lock), "dip": portfolio(pool, dip_m, pool.lock), "spy": spy_curve(pool, pool.lock)},
        "all_seen": {"label": f"Tüm hisseler, eğitim dönemi (botlar bu veriyi gördü: iyimser) {pool.first_date} – {pool.lock_start}",
                     "bots": portfolio(pool, members, pool.region), "dip": portfolio(pool, dip_m, pool.region), "spy": spy_curve(pool, pool.region)},
    }
    n_all = sum(f["lock"].get("n", 0) for f in finals)
    pooled = ({"n": n_all, "avg_pct": round(sum(f["lock"].get("avg_pct", 0) * f["lock"].get("n", 0) for f in finals) / n_all, 3),
               "daily_pct": round(sum(f["lock"].get("daily_pct", 0) for f in finals) / len(finals), 3),
               "bots_profitable": sum(1 for f in finals if f["lock"].get("avg_pct", -1) > 0),
               "excess_pct": round(sum(f["lock"].get("excess_pct", 0) * f["lock"].get("n", 0) for f in finals) / n_all, 3),
               "bots_positive": sum(1 for f in finals if f["lock"].get("excess_pct", -1) > 0), "bots": len(finals)}
              if n_all else {"n": 0, "bots": len(finals)})
    report = {
        "kind": "tournament", "created_at": now_ts(), "seconds": round(time.time() - t0, 1), "rows": pool.n,
        "symbols": pool.symbols, "group_symbols": pool.group_symbols,
        "ranges": {"data": [pool.first_date, pool.last_date], "region": [pool.first_date, pool.lock_start],
                   "inner_val": [pool.inner_val, pool.lock_start], "lock": [pool.lock_start, pool.last_date]},
        "stages": stages, "finals": finals, "lock_pooled": pooled, "dip_lock": dip_lock, "portfolios": portfolios,
        "seed": seed, "mode": mode, "portfolio_settings": {"max_new": PORT_MAX_NEW, "pos_frac": PORT_POS_FRAC, "max_pos": PORT_MAX_POS},
        "evaluated": sum(s["evaluated"] for s in stages),
        "settings": {"population": pop_size, "max_generations": max_generations, "patience": PATIENCE, "groups": GROUPS,
                     "lock_months": LOCK_MONTHS, "min_trades": MIN_TRADES, "min_exam_trades": MIN_EXAM_TRADES,
                     "min_days": MIN_DAYS, "min_exam_days": MIN_EXAM_DAYS, "pos_years": POS_YEARS},
    }
    store.kv_set("evo_daily_finals" if mode == "daily" else "evo_survivors", [{"genome": f["genome"], "rule": f["rule"], "lock": f["lock"], "weight": f["weight"],
                                    "conds_abs": f["conds_abs"], "stop": STOP_SET[f["genome"]["stop"]],
                                    "ratio": RATIO_SET[f["genome"]["ratio"]], "hold": HOLD_SET[f["genome"]["hold"]],
                                    "side": f["genome"]["side"], "run_at": report["created_at"]} for f in finals])
    with store.tx() as c:
        c.execute("INSERT INTO runs(created_at, kind, params, report) VALUES (?,?,?,?)",
                  (report["created_at"], "evolve", json.dumps({"max_generations": max_generations, "population": pop_size, "seed": seed}),
                   json.dumps(report, ensure_ascii=False)))
    return report


OVERSOLD = {("rsi2", "<"), ("rsi14", "<"), ("gap", "<"), ("ret1", "<"), ("ret5", "<"), ("ret20", "<"), ("rs_spy20", "<"),
            ("rs_sector20", "<"), ("brk20", "<"), ("dist_52h", "<"), ("dist20", "<"), ("dist50", "<"), ("down_streak", ">")}


def family(g: dict) -> str:
    """Coarse rule family: what kind of situation the bot buys or sells."""
    feats = {(f, op) for f, op, _ in g["conds"]}
    fear = ("vix", ">") in feats or ("breadth50", "<") in feats or ("spy_ret1", "<") in feats
    oversold = bool(feats & OVERSOLD)
    side = "al" if g["side"] > 0 else "sat"
    if fear and oversold:
        return f"korku + aşırı satış ({side})"
    if fear:
        return f"korku günü ({side})"
    if oversold:
        return f"aşırı satış ({side})"
    return f"diğer ({side})"


def stability(n: int = 3) -> dict:
    """Do independent tournaments (different random starts) find the same kind of rules?"""
    rows = [json.loads(r[0]) for r in store.conn().execute(
        "SELECT report FROM runs WHERE kind='evolve' ORDER BY id DESC LIMIT ?", (n,))]
    runs = []
    for rep_ in rows:
        if rep_.get("kind") != "tournament":
            continue
        fins = rep_.get("finals") or []
        fam: dict[str, int] = {}
        feats: dict[str, int] = {}
        for f in fins:
            fam[family(f["genome"])] = fam.get(family(f["genome"]), 0) + 1
            for c in f["genome"]["conds"]:
                feats[c[0]] = feats.get(c[0], 0) + 1
        ex = [c["exam"] for c in (rep_.get("stages") or [{}])[-1].get("candidates", []) if c.get("survived")]
        runs.append({"created_at": rep_.get("created_at"), "seed": rep_.get("seed"), "finals": len(fins), "families": fam,
                     "features": dict(sorted(feats.items(), key=lambda kv: -kv[1])[:8]),
                     "exam_daily_pct": round(float(np.mean([x.get("daily_pct", 0) for x in ex])), 3) if ex else None,
                     "lock": rep_.get("lock_pooled"),
                     "portfolio": ((rep_.get("portfolios") or {}).get("unseen_stocks") or {}).get("bots", {}).get("stats")})
    top = {}
    for r in runs:
        if r["finals"]:
            k = max(r["families"].items(), key=lambda kv: kv[1])[0]
            top[k] = top.get(k, 0) + 1
    return {"runs": runs, "dominant": top}


def last_run() -> dict | None:
    r = store.conn().execute("SELECT report FROM runs WHERE kind='evolve' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r else None
