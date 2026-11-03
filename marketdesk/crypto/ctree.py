"""Kripto bot ağacı: two branches (kural = evolved rule bots, sinir = neural networks), each with five groups of 4
elite bots, chosen by a day-by-day walk over Binance's history (2018 -> today, every calendar day: crypto never
closes). In every group one bot trades a $100,000 account and three trade $100 accounts.

Groups (niş), holding periods in calendar days:
  gunluk    trades (almost) every day, holds 1-2 days
  korku     trades only on fear days (Fear & Greed <= 30 the day before), holds 1-7 days
  haftalik  holds 7 days (exits earlier at its stop / target)
  aylik     holds 30 days
  serbest   chooses its own holding period (1, 2, 3, 5, 7, 14, 30, 60 or 90 days); a stop is always set

The walk is the stock tree's (lab/tree.py): every bot buys up to 5 coins a day at 00:00 UTC (long only, at most 10
positions open), a trade counts only after it has closed (and never before its holding period's benchmark is known),
bots are judged on their closed trades of the last ~year, the weakest is replaced by a child of the strongest. The
trade engine, the fitness and the position cap are the stock tree's own code.

What the first bots carry: the stock system's rule bots, translated to crypto (RSI and streak thresholds kept;
return / distance / volatility thresholds scaled to crypto's ~2.2x larger moves; VIX conditions turned into Fear &
Greed conditions), plus a few classic crypto patterns (dip buy, momentum, breakout, fear buy, volume spike).

Honest limits: crypto history is ~8.7 years and the top-150 universe was much smaller before 2020; long holds have
few independent trades; prices are Binance spot daily candles (stops inside a day are assumed to fill at the stop).
"""
from __future__ import annotations

import copy
import gc
import json
import logging
import math
import random
import time

import numpy as np

from ..lab import evolve, learn, neuro, tree
from ..lab.tree import EMPTY, _agg, _cap, _passed, _picked, _plain, _rows, _trades, age_min, fitness, window  # noqa: F401
from ..util import now_ts
from . import cpool, cstore

log = logging.getLogger("marketdesk.crypto")

HOLD_FREE = (1, 2, 3, 5, 7, 14, 30, 60, 90)
FEAR_FNG = 0.30                  # Fear & Greed (0-1) at or below this the day before = fear day
NICHES = {
    "gunluk": {"label": "Günlük (1–2 gün)", "holds": (1, 2), "fear": False, "cov": 0.6},
    "korku": {"label": "Korku günü (Korku-Açgözlülük ≤ 30)", "holds": (1, 2, 3, 5, 7), "fear": True, "cov": 0.0},
    "haftalik": {"label": "Haftalık (7 gün)", "holds": (7,), "fear": False, "cov": 0.0},
    "aylik": {"label": "Aylık (30 gün)", "holds": (30,), "fear": False, "cov": 0.0},
    "serbest": {"label": "Serbest (1–90 gün, stoplu)", "holds": HOLD_FREE, "fear": False, "cov": 0.0},
}
ORDER = list(NICHES)
BRANCHES = {"kural": "Kural botları (evrim)", "sinir": "Sinir ağı botları"}
RULE_POP, NN_POP = 16, 16
FINALS = 4
BIG_RANKS = (1,)                 # in every group of 4 the best bot trades $100,000, the other three $100
FILL_TRIES = 4
SCHOOL_DAYS = 120
STOPS = tree.STOPS
HIDDEN = 12
INPUTS = list(cpool.NAMES)
RANKS = {"rsi2": (1, "en aşırı satılmış önce"), "ret1": (1, "dün en çok düşen önce"), "ret7": (-1, "7 günde en güçlü önce"),
         "rvol": (-1, "hacmi en çok artan önce"), "dvol": (-1, "en çok işlem gören önce")}
CTREE: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}

# stock feature -> (crypto feature, threshold transform). Moves are ~2.2x larger in crypto.
SCALE = 2.2
MAP = {"rsi2": ("rsi2", lambda t: t), "rsi14": ("rsi14", lambda t: t), "down_streak": ("down_streak", lambda t: t),
       "rvol": ("rvol", lambda t: t), "breadth50": ("breadth50", lambda t: t),
       "ret1": ("ret1", lambda t: t * SCALE), "ret5": ("ret7", lambda t: t * SCALE), "ret20": ("ret30", lambda t: t * SCALE),
       "dist20": ("dist20", lambda t: t * SCALE), "dist50": ("dist50", lambda t: t * SCALE),
       "dist200": ("dist200", lambda t: t * SCALE), "brk20": ("brk20", lambda t: t * SCALE),
       "brkl20": ("brkl20", lambda t: t * SCALE), "dist_52h": ("dist_365h", lambda t: t * SCALE),
       "dist_52l": ("dist_365l", lambda t: t * SCALE), "atr": ("atr", lambda t: t * SCALE),
       "rs_spy20": ("rs_btc30", lambda t: t * SCALE), "spy_ret1": ("btc_ret1", lambda t: t * SCALE)}

ARCHETYPES = [
    ("Dipten alım", [["rsi2", "<", 0.1, False], ["ret3", "<", -0.08, False]], ["atr", 2.0], 1.5, "rsi2"),
    ("Momentum", [["ret30", ">", 0.2, False], ["dist50", ">", 0.0, False], ["rvol", ">", 1.5, False]], ["atr", 2.0], 2.0, "ret7"),
    ("Kırılım", [["brk20", ">", -0.01, False], ["rvol", ">", 2.0, False]], ["atr", 1.5], 2.0, "rvol"),
    ("Korkuda alım", [["fng", "<", 0.25, False], ["rsi14", "<", 0.35, False]], ["atr", 2.0], 2.0, "rsi2"),
    ("Hacim patlaması", [["rvol", ">", 3.0, False], ["taker", ">", 0.55, False]], ["pct", 5.0], 1.5, "rvol"),
    ("BTC güçlüyken geride kalan", [["btc_ret7", ">", 0.0, False], ["rs_btc30", "<", -0.1, False]], ["atr", 2.0], 1.5, "rsi2"),
    ("Büyük coinlerde düşüş", [["vol_rank", ">", 0.8, False], ["ret3", "<", -0.06, False]], ["atr", 1.5], 1.5, "ret1"),
    ("Yıllık zirveye yakın", [["dist_365h", ">", -0.1, False], ["dist20", ">", 0.0, False]], ["atr", 2.0], 2.0, "ret7"),
]


# ---------------------------------------------------------------- shared

def _step_hold(h: int, niche: str, rng) -> int:
    holds = NICHES[niche]["holds"]
    if h not in holds:
        return holds[min(range(len(holds)), key=lambda i: abs(holds[i] - h))]
    if len(holds) > 1 and rng.random() < 0.2:
        i = holds.index(h)
        return holds[min(len(holds) - 1, max(0, i + (1 if rng.random() < 0.5 else -1)))]
    return h


def _context(pool) -> dict:
    drifts: dict[int, np.ndarray] = {}

    def drift(h: int) -> np.ndarray:
        if h not in drifts:
            drifts[h] = tree.longpath.drift(pool, h)
        return drifts[h]

    fng = np.full(pool.n_days, np.nan, np.float32)
    fng[pool.dayidx] = pool.F["fng"]
    fear_day = np.round(np.nan_to_num(fng, nan=1.0) * 100) <= round(FEAR_FNG * 100)     # whole index points, as live
    return {"pool": pool, "nd": pool.n_days, "years": pool.day_year, "drift": drift, "fear_day": fear_day,
            "fear_row": fear_day[pool.dayidx], "train": pool.group <= 3, "exam": pool.group == 4}


def _fmt(x: float, d: int = 2) -> str:
    return evolve._fmt(x, d)


def describe(b: dict) -> str:
    parts = []
    for feat, op, t, _sp in b["conds"]:
        label, unit = cpool.FEATURES[feat]
        sign = "-" if t < 0 else ""
        val = {"pct": f"{sign}%{_fmt(abs(t) * 100)}", "rsi": _fmt(t * 100, 0), "x": f"{_fmt(t, 1)}x",
               "int": f"{t:.0f}", "share": f"%{_fmt(t * 100, 0)}", "usd": f"{_fmt(10 ** t / 1e6, 1)} M$",
               "fng": _fmt(t * 100, 0), "fngd": f"{t * 100:+.0f}", "logd": f"{10 ** t:.0f} gün"}.get(unit, _fmt(t))
        parts.append(f"{label} {op} {val}")
    kind, x = b["stop"]
    ex = (f"zarar durdur %{_fmt(x, 1)}, kâr al %{_fmt(x * b['ratio'], 1)}" if kind == "pct"
          else f"zarar durdur {_fmt(x, 1)}×ATR, kâr al {_fmt(x * b['ratio'], 1)}×ATR")
    rk = RANKS.get(b.get("order") or "rsi2", RANKS["rsi2"])[1]
    return f"{' VE '.join(parts) or 'Her gün'} → Al ({rk}) · {ex} · {b['hold']} gün"


# ---------------------------------------------------------------- rule branch

def _clean(pool, b: dict, niche: str, rng) -> dict:
    """Long only, always a stop, 1-3 conditions on known features, the group's holding period."""
    stop = tuple(b.get("stop") or ("atr", 2.0))
    if stop not in STOPS:
        stop = ("atr", 2.0)
    ratio = b.get("ratio") if b.get("ratio") in evolve.RATIO_SET else 1.5
    conds, seen = [], set()
    for c in b.get("conds") or []:
        if c[0] in cpool.FEATURES and c[0] not in seen and c[1] in ("<", ">"):
            seen.add(c[0])
            conds.append([c[0], c[1], float(c[2]), False])
    if not conds:
        conds = [["rsi2", "<", 0.1, False]]
    order = b.get("order") if b.get("order") in RANKS else "rsi2"
    return {"conds": conds[:3], "side": 1, "stop": list(stop), "ratio": ratio, "order": order,
            "hold": _step_hold(int(b.get("hold") or 7), niche, rng)}


def _shift(pool, cond: list, sd: float, rng) -> None:
    q = pool.q_of(cond[0], cond[2])
    cond[2] = pool.thr(cond[0], min(0.99, max(0.01, q + rng.gauss(0, sd))))


def _mutate(pool, b: dict, rng: random.Random) -> dict:
    c = copy.deepcopy(b)
    r = rng.random()
    if r < 0.35:
        for cond in c["conds"]:
            if rng.random() < 0.7:
                _shift(pool, cond, 0.08, rng)
    elif r < 0.55:                                  # a new feature in place of one condition
        i = rng.randrange(len(c["conds"]))
        used = {x[0] for x in c["conds"]}
        feat = rng.choice([f for f in INPUTS if f not in used])
        c["conds"][i] = [feat, rng.choice("<>"), pool.thr(feat, rng.uniform(0.1, 0.9)), False]
    elif r < 0.7 and len(c["conds"]) < 3:
        used = {x[0] for x in c["conds"]}
        feat = rng.choice([f for f in INPUTS if f not in used])
        op = rng.choice("<>")
        c["conds"].append([feat, op, pool.thr(feat, rng.uniform(0.55, 0.9) if op == ">" else rng.uniform(0.1, 0.45)), False])
    elif r < 0.8 and len(c["conds"]) > 1:
        c["conds"].pop(rng.randrange(len(c["conds"])))
    elif r < 0.88:
        cond = rng.choice(c["conds"])
        cond[1] = ">" if cond[1] == "<" else "<"
        _shift(pool, cond, 0.15, rng)
    _step_exit(c, rng, 0.3)
    if rng.random() < 0.15:
        c["order"] = rng.choice(list(RANKS))
    return c


def _step_exit(c: dict, rng, p: float) -> None:
    if rng.random() < p and tuple(c["stop"]) in STOPS:
        i = STOPS.index(tuple(c["stop"]))
        c["stop"] = list(STOPS[min(len(STOPS) - 1, max(0, i + rng.choice((-1, 1))))])
    if rng.random() < p:
        i = evolve.RATIO_SET.index(c["ratio"]) if c["ratio"] in evolve.RATIO_SET else 1
        c["ratio"] = evolve.RATIO_SET[min(len(evolve.RATIO_SET) - 1, max(0, i + rng.choice((-1, 1))))]


def _micro_clone(pool, b: dict, rng: random.Random) -> dict:
    """The rule's backbone stays; thresholds move a little (±3 % of the coin-day distribution); the exit may step."""
    c = copy.deepcopy(b)
    for cond in c["conds"]:
        if rng.random() < 0.6:
            _shift(pool, cond, 0.03, rng)
    _step_exit(c, rng, 0.25)
    return c


def _cross(a: dict, b: dict, rng: random.Random) -> dict:
    pool_c = [list(x) for x in a["conds"]] + [list(x) for x in b["conds"]]
    rng.shuffle(pool_c)
    conds, seen = [], set()
    for x in pool_c:
        if x[0] not in seen and len(conds) < rng.choice((1, 2, 2, 3)):
            seen.add(x[0])
            conds.append(x)
    src = a if rng.random() < 0.5 else b
    return {"conds": conds or [list(a["conds"][0])], "stop": list(src["stop"]), "ratio": src["ratio"],
            "order": (a if rng.random() < 0.5 else b).get("order", "rsi2"), "hold": a["hold"]}


def _child(pool, pa: dict, other: dict | None, niche: str, rng: random.Random) -> tuple[dict, str]:
    r = rng.random()
    if other is not None and r < 0.25:
        c, origin = _cross(pa, other, rng), "çaprazlama"
        if rng.random() < 0.5:
            c = _mutate(pool, c, rng)
    elif r < 0.45:
        c, origin = _mutate(pool, pa, rng), "mutasyon"
    else:
        c, origin = _micro_clone(pool, pa, rng), "klon"
    return _clean(pool, dict(c, hold=pa["hold"]), niche, rng), origin


def transfer_stock_bot(b: dict) -> dict | None:
    """A stock rule bot's conditions in crypto terms (None when nothing maps)."""
    conds = []
    for c in b.get("conds") or []:
        feat, op, t = c[0], c[1], float(c[2])
        if feat == "vix":                           # VIX 20 ~ Fear & Greed 30, VIX 25 ~ 20; the direction flips
            conds.append(["fng", "<" if op == ">" else ">", min(0.95, max(0.05, 0.30 - 2.0 * (t - 0.20))), False])
        elif feat in MAP:
            nf, fn = MAP[feat]
            conds.append([nf, op, float(fn(t)), False])
    if not conds:
        return None
    return {"conds": conds, "stop": b.get("stop") or ["atr", 2.0], "ratio": float(b.get("ratio") or 1.5),
            "hold": int(b.get("hold") or 5), "order": "rsi2", "src": b.get("src") or "hisse botu"}


def seeds() -> dict[str, list[dict]]:
    """Crypto patterns + every stock rule bot that translates (the old system's learning)."""
    out = [{"conds": [list(c) for c in conds], "stop": list(stop), "ratio": ratio, "hold": 7, "order": rk, "src": f"kripto kalıbı: {name}"}
           for name, conds, stop, ratio, rk in ARCHETYPES]
    try:
        st = tree._rule_seeds()
    except Exception as e:  # noqa: BLE001 - the stock lab may be empty or busy
        log.warning("crypto seeds: stock bots unavailable: %s", e)
        st = {"all": [], "fear": [], "daily": []}
    seen = set()
    stock = []
    for b in st["all"]:
        t = transfer_stock_bot(b)
        if t is None:
            continue
        key = repr(sorted((c[0], c[1], round(c[2], 3)) for c in t["conds"]))
        if key in seen:
            continue
        seen.add(key)
        t["src"] = f"hisse botundan aktarıldı ({b.get('src') or 'evrim'})"
        stock.append(t)
    fear = [b for b in out + stock if any(c[0] == "fng" and c[1] == "<" for c in b["conds"])]
    return {"all": out + stock, "fear": fear, "stock": len(stock)}


class RuleBot:
    def __init__(self, ctx: dict, b: dict, niche: str, born: int, uid: int, origin: str, parent=None):
        self.b, self.niche, self.born, self.uid, self.origin, self.parent = b, niche, born, uid, origin, parent
        self.fit = 0.0
        pool = ctx["pool"]
        m = learn._mask(pool, b) & (pool.dayidx >= born)
        if NICHES[niche]["fear"]:
            m &= ctx["fear_row"]
        s_i, r_i = evolve.STOP_SET.index(tuple(b["stop"])), evolve.RATIO_SET.index(b["ratio"])
        self.led, self.ex = (self._run(ctx, m & part, s_i, r_i) for part in (ctx["train"], ctx["exam"]))
        self.by_day = {int(d): i for i, d in enumerate(self.led[1])}

    def _run(self, ctx, m, s_i, r_i):
        idx = np.nonzero(m)[0]
        sign, _t = RANKS[self.b.get("order") or "rsi2"]
        x = ctx["pool"].F[self.b.get("order") or "rsi2"][idx]
        score = np.nan_to_num(-sign * x, nan=-1e9)
        idx = _picked(ctx, idx, score)
        return _trades(ctx, idx, s_i, r_i, self.b["hold"])

    def fitness(self, d: int) -> float:
        return fitness(self.led, d, self.born, self.b["hold"], NICHES[self.niche]["cov"])


def _rule_walk(ctx: dict, seed: int, prog) -> dict:
    pool, nd = ctx["pool"], ctx["nd"]
    rng = random.Random(seed)
    sd = seeds()
    uid = [0]
    pops: dict[str, list[RuleBot]] = {}
    templates: dict[str, list[dict]] = {}
    for niche in ORDER:
        first = sd["fear"] if niche == "korku" else []
        rest = [b for b in sd["all"] if b not in first]
        rng.shuffle(rest)
        templates[niche] = [_clean(pool, b, niche, rng) for b in first + rest]
        start = templates[niche][:RULE_POP]
        while len(start) < RULE_POP:
            start.append(_child(pool, rng.choice(templates[niche]), None, niche, rng)[0])
        pops[niche] = []
        for b in start:
            uid[0] += 1
            pops[niche].append(RuleBot(ctx, b, niche, 0, uid[0], "eski sistemden"))
        prog(f"Kripto kural dalı · {NICHES[niche]['label']}: {len(pops[niche])} başlangıç botu", 0, nd)
    champs = {k: pops[k][0] for k in ORDER}
    champ_rows: dict[str, list] = {k: [] for k in ORDER}
    births, deaths = sum(len(v) for v in pops.values()), 0
    for d in range(nd):
        for k in ORDER:
            i = champs[k].by_day.get(d)
            if i is not None:
                champ_rows[k].append(tuple(a[i].item() for a in champs[k].led))
        k = ORDER[d % len(ORDER)]
        judged = [n for n in pops[k] if d - n.born >= age_min(n.b["hold"])]
        if len(judged) >= 3:
            for n in judged:
                n.fit = n.fitness(d)
            judged.sort(key=lambda n: -n.fit)
            champs[k] = judged[0]
            parents = [n for n in judged[:max(2, len(judged) // 5)] if n.fit > -1.0] or judged[:3]
            dead = judged[-1]
            if dead is not champs[k]:
                deaths += 1
                if rng.random() < 0.04:
                    child, origin, par = copy.deepcopy(rng.choice(templates[k])), "eski sistemden", None
                else:
                    pa = rng.choice(parents)
                    other = rng.choice(judged[:max(3, len(judged) // 2)]).b
                    child, origin = _child(pool, pa.b, other if other is not pa.b else None, k, rng)
                    par = pa.uid
                uid[0] += 1
                pops[k][pops[k].index(dead)] = RuleBot(ctx, child, k, d, uid[0], origin, par)
                births += 1
        if d % 50 == 0 or d == nd - 1:
            prog(f"Kripto kural dalı gün gün · {pool.udates[d]} ({d + 1}/{nd} gün) · {deaths} elendi, {births} doğdu", d + 1, nd)
    finals = {}
    for k in ORDER:
        finals[k] = _rule_finals(ctx, pops[k], k, rng, uid)
        finals[k]["record"] = neuro._short(neuro._agg_stats([x for x in champ_rows[k] if x[1] >= SCHOOL_DAYS]))
    return {"births": births, "deaths": deaths, "groups": finals, "stock_seeds": sd["stock"]}


def _rule_entry(ctx, n: RuleBot, k: str, f: float) -> dict:
    nd = ctx["nd"]
    lo = nd - window(n.b["hold"])
    life, last, ex = (neuro._agg_stats(r) for r in (_rows(n.led), _rows(n.led, lo) or _rows(n.led)[-60:], _rows(n.ex)))
    return dict(n.b, rule=describe(n.b), origin=n.origin, fit=round(f, 3), niche=k, uid=n.uid,
                alive_since=ctx["pool"].udates[n.born], life=neuro._short(life), last=neuro._short(last),
                exam=neuro._short(ex), passed=_passed(nd - n.born, life, last, ex))


def _rule_finals(ctx: dict, pop: list[RuleBot], k: str, rng: random.Random, uid: list) -> dict:
    nd, pool = ctx["nd"], ctx["pool"]
    scored = sorted(((n.fitness(nd), n) for n in pop), key=lambda x: -x[0])
    entries = [_rule_entry(ctx, n, k, f) for f, n in scored]
    passed = [e for e in entries if e["passed"]][:FINALS]
    top3 = (passed or entries)[:3]
    filled = 0
    while len(passed) < FINALS:
        best = None
        for _ in range(FILL_TRIES):
            pa = rng.choice(top3)
            other = rng.choice(entries)
            child, _o = _child(pool, pa, other if other is not pa else None, k, rng)
            uid[0] += 1
            n = RuleBot(ctx, child, k, 0, uid[0], "en iyi 3'ün klonu", pa["uid"])
            f = n.fitness(nd)
            if best is None or f > best[0]:
                best = (f, n)
        e = _rule_entry(ctx, best[1], k, best[0])
        e["filled"] = True
        passed.append(e)
        filled += 1
    return {"bots": passed, "filled": filled, "passed": FINALS - filled}


# ---------------------------------------------------------------- neural branch (29 crypto inputs -> 12 -> 1)

def norm_params(pool, idx: np.ndarray, rng: np.random.Generator) -> dict:
    samp = idx if len(idx) <= 300_000 else np.sort(rng.choice(idx, 300_000, replace=False))
    out = {}
    for k in INPUTS:
        x = pool.F[k][samp].astype(np.float32)
        x = x[np.isfinite(x)]
        if len(x):
            q1, med, q3 = np.quantile(x, [0.25, 0.5, 0.75])
            out[k] = [float(med), float(q3 - q1) if q3 > q1 else float(x.std() or 1.0)]
        else:
            out[k] = [0.0, 1.0]
    return out


def matrix(pool, idx: np.ndarray, norm: dict) -> np.ndarray:
    X = np.zeros((len(idx), len(INPUTS)), dtype=np.float16)
    for j, k in enumerate(INPUTS):
        med, sc = norm[k]
        X[:, j] = np.nan_to_num(np.clip((pool.F[k][idx].astype(np.float32) - med) / sc, -4, 4), nan=0.0)
    return X


def scale_row(f: dict, norm: dict) -> np.ndarray:
    v = np.zeros(len(INPUTS), dtype=np.float32)
    for j, k in enumerate(INPUTS):
        x = f.get(k)
        if x is None or not math.isfinite(float(x)):
            continue
        med, sc = norm[k]
        v[j] = min(4.0, max(-4.0, (float(x) - med) / sc))
    return v


def unpack(w: np.ndarray):
    d = len(INPUTS)
    a = d * HIDDEN
    return w[:a].reshape(d, HIDDEN), w[a:a + HIDDEN], w[a + HIDDEN:a + 2 * HIDDEN], w[-1]


def forward(w, X: np.ndarray) -> np.ndarray:
    W1, b1, W2, b2 = unpack(np.asarray(w, dtype=np.float32))
    return np.tanh(X.astype(np.float32) @ W1 + b1) @ W2 + b2


def _random_net(rng: np.random.Generator) -> dict:
    d = len(INPUTS)
    w = np.concatenate([rng.normal(0, 1 / math.sqrt(d), d * HIDDEN), np.zeros(HIDDEN),
                        rng.normal(0, 1 / math.sqrt(HIDDEN), HIDDEN), [float(rng.uniform(-2.2, -1.2))]]).astype(np.float32)
    return {"w": w, "sigma": 0.12, "stop": int(rng.choice(neuro.EXIT_STOPS)), "ratio": int(rng.integers(len(evolve.RATIO_SET))),
            "label": "Rastgele keşif ağı", "origin": "rastgele"}


def rule_to_net(b: dict, norm: dict, rng: np.random.Generator, label: str) -> dict:
    """A rule bot as a network: the first hidden units are smooth gates for its conditions, the rest start small."""
    d = len(INPUTS)
    W1 = rng.normal(0, 0.05, (d, HIDDEN)).astype(np.float32)
    b1 = np.zeros(HIDDEN, dtype=np.float32)
    W2 = rng.normal(0, 0.06, HIDDEN).astype(np.float32)
    conds = [c for c in b["conds"] if c[0] in INPUTS][:HIDDEN - 2]
    for u, c in enumerate(conds):
        j = INPUTS.index(c[0])
        med, sc = norm.get(c[0], [0.0, 1.0])
        z = max(-3.5, min(3.5, (float(c[2]) - med) / (sc or 1.0)))
        sg = 1.0 if c[1] == ">" else -1.0
        W1[:, u] = rng.normal(0, 0.03, d).astype(np.float32)
        W1[j, u] = 2.6 * sg
        b1[u] = -2.6 * sg * z
        W2[u] = 1.0
    b2 = -(max(1, len(conds)) - 0.32)
    return {"w": np.concatenate([W1.ravel(), b1, W2, [b2]]).astype(np.float32), "sigma": 0.07,
            "stop": neuro._stop_idx(b["stop"]), "ratio": neuro._nearest_idx(evolve.RATIO_SET, float(b["ratio"])),
            "label": label, "origin": "eski sistemden"}


def _cross_net(a: dict, b: dict, rng: np.random.Generator) -> dict:
    W1a, b1a, W2a, b2a = unpack(a["w"])
    W1b, b1b, W2b, b2b = unpack(b["w"])
    pick = rng.random(HIDDEN) < 0.5
    w = np.concatenate([np.where(pick[None, :], W1a, W1b).ravel(), np.where(pick, b1a, b1b), np.where(pick, W2a, W2b),
                        [min(-0.35, float(b2a if rng.random() < 0.5 else b2b))]]).astype(np.float32)
    src = a if rng.random() < 0.5 else b
    return {"w": w, "sigma": (a["sigma"] + b["sigma"]) / 2, "stop": src["stop"], "ratio": src["ratio"],
            "label": src.get("label") or ""}


def _nn_clean(b: dict, niche: str, hold: int, rng) -> dict:
    c = {k: b[k] for k in ("w", "sigma", "stop", "ratio") if k in b}
    c["label"] = b.get("label") or ""
    c["origin"] = b.get("origin") or ""
    c["hold_d"] = _step_hold(int(hold), niche, rng)
    return c


def _nn_child(pa: dict, other: dict | None, niche: str, rng: np.random.Generator) -> tuple[dict, str]:
    if other is not None and rng.random() < 0.5:
        c, origin = _cross_net(pa, other, rng), "çaprazlama"
    else:
        c, origin = dict(pa), "klon"
    c = neuro._mutate(dict(c, hold=0), rng)        # weight noise + exit steps (its hold index is not used here)
    return _nn_clean(dict(c, label=pa.get("label")), niche, pa["hold_d"], rng), origin


def _nn_seeds(pool, norm: dict, rng: np.random.Generator, niche: str, sd: dict) -> list[dict]:
    src = (sd["fear"] + [b for b in sd["all"] if b not in sd["fear"]]) if niche == "korku" else sd["all"]
    out = [rule_to_net(b, norm, rng, f"İkiz: {describe(dict(b, hold=7))[:70]}") for b in src[:NN_POP - 4]]
    while len(out) < NN_POP:
        if out and rng.random() < 0.6:
            p = out[int(rng.integers(len(out)))]
            c = neuro._mutate(dict(p, hold=0), rng)
            out.append(dict(c, label=f"Hibrit ({p.get('label', '')[:40]})", origin="hibrit"))
        else:
            out.append(_random_net(rng))
    return out


def _nn_full(ctx: dict, X: np.ndarray, order: np.ndarray, net: dict, niche: str, born: int = 0) -> tuple:
    pool = ctx["pool"]
    sc = forward(net["w"], X)
    rows = order
    m = (sc > 0) & (pool.dayidx[rows] >= born)
    if NICHES[niche]["fear"]:
        m &= ctx["fear_row"][rows]
    out = []
    for part in (ctx["train"], ctx["exam"]):
        mm = m & part[rows]
        idx = _picked(ctx, rows[mm], sc[mm])
        out.append(_trades(ctx, idx, net["stop"], net["ratio"], net["hold_d"]))
    return out[0], out[1]


def _nn_walk(ctx: dict, seed: int, prog) -> dict:
    pool, nd = ctx["pool"], ctx["nd"]
    rng = np.random.default_rng(seed)
    pyrng = random.Random(seed)
    di, grp = pool.dayidx, pool.group
    norm = norm_params(pool, np.nonzero((di < max(250, SCHOOL_DAYS)) & (grp <= 3))[0], rng)
    prog("Kripto sinir ağı dalı: girdiler hazırlanıyor…", 0, nd)
    order = np.argsort(di, kind="stable")
    X = matrix(pool, order, norm)
    starts = np.searchsorted(di[order], np.arange(nd + 1))
    g_sorted = grp[order]
    H, d_in = HIDDEN, len(INPUTS)
    sd = seeds()
    nets: list[dict] = []
    uid = [0]

    def born(b: dict, niche: str, day: int, origin: str, parent=None) -> dict:
        uid[0] += 1
        return dict(b, niche=niche, uid=uid[0], born=day, origin=origin or b.get("origin") or "eski sistemden", parent=parent,
                    led=[], life=[], ex_led=[], open=[], open_ex=[], fit=0.0)

    for niche in ORDER:
        for b in _nn_seeds(pool, norm, rng, niche, sd):
            hold = pyrng.choice(NICHES[niche]["holds"]) if niche == "serbest" else NICHES[niche]["holds"][-1]
            nets.append(born(_nn_clean(b, niche, int(hold), pyrng), niche, 0, b.get("origin") or "eski sistemden"))
    W1 = np.zeros((d_in, len(nets) * H), np.float32)
    B1 = np.zeros(len(nets) * H, np.float32)
    W2 = np.zeros((len(nets), H), np.float32)
    B2 = np.zeros(len(nets), np.float32)

    def sync(p: int) -> None:
        w1, b1, w2, b2 = unpack(np.asarray(nets[p]["w"], dtype=np.float32))
        W1[:, p * H:(p + 1) * H], B1[p * H:(p + 1) * H], W2[p], B2[p] = w1, b1, w2, b2

    for p in range(len(nets)):
        sync(p)
    champs = {k: next(n for n in nets if n["niche"] == k) for k in ORDER}
    champ_rows: dict[str, list] = {k: [] for k in ORDER}
    births, deaths = len(nets), 0
    templates = sd["all"]
    for d in range(nd):
        a, b_ = starts[d], starts[d + 1]
        if b_ > a:
            Xd = X[a:b_].astype(np.float32)
            rows_d = order[a:b_]
            gd = g_sorted[a:b_]
            S = np.einsum("nph,ph->np", np.tanh(Xd @ W1 + B1).reshape(len(Xd), len(nets), H), W2) + B2
            fear = bool(ctx["fear_day"][d])
            want: dict[tuple, set] = {}
            picks = []
            for p, n in enumerate(nets):
                if NICHES[n["niche"]]["fear"] and not fear:
                    continue
                for exam in (False, True):
                    op = n["open_ex"] if exam else n["open"]
                    op[:] = [e for e in op if e >= d]
                    room = min(tree.TOP_K, tree.MAX_OPEN - len(op))
                    if room <= 0:
                        continue
                    sel = np.nonzero((gd == 4) if exam else (gd <= 3))[0]
                    pos = sel[S[sel, p] > 0]
                    if not len(pos):
                        continue
                    top = rows_d[pos[np.argsort(-S[pos, p])[:room]]]
                    key = (n["stop"], n["ratio"], n["hold_d"])
                    want.setdefault(key, set()).update(top.tolist())
                    picks.append((p, exam, key, top))
            res_map: dict = {}
            for key, rs in want.items():
                idx = np.array(sorted(rs))
                r, e = tree.longpath.outcome(pool, idx, 1, *key)
                ed = tree.longpath.exit_day(pool, idx, e)
                for i, rr, ee in zip(idx.tolist(), r.tolist(), ed.tolist()):
                    res_map[(key, i)] = (rr, ee)
            y = int(ctx["years"][d])
            for p, exam, key, top in picks:
                n = nets[p]
                dh = float(ctx["drift"](n["hold_d"])[d])
                op = n["open_ex"] if exam else n["open"]
                valid = []
                for i in top.tolist():
                    rr, ee = res_map[(key, i)]
                    op.append(ee if ee >= 0 else nd + 10_000)
                    if rr == rr:
                        valid.append((float(rr), int(ee)))
                if not valid:
                    continue
                cnt = len(valid)
                dr = sum(r for r, _ in valid) / cnt
                agg = (max(max(e for _, e in valid), d + n["hold_d"] - 1), d, dr, dr - dh, cnt, sum(1 for r, _ in valid if r > 0), y)
                if exam:
                    n["ex_led"].append(agg)
                else:
                    n["led"].append(agg)
                    n["life"].append(agg)
                if not exam and n is champs[n["niche"]]:
                    champ_rows[n["niche"]].append(agg)
        for k in ORDER:
            members = [p for p, n in enumerate(nets) if n["niche"] == k]
            judged = [p for p in members if d - nets[p]["born"] >= age_min(nets[p]["hold_d"])]
            if len(judged) < 3:
                continue
            for p in judged:
                n = nets[p]
                arr = tuple(np.array(c) for c in zip(*n["led"])) if n["led"] else EMPTY
                n["fit"] = fitness(arr, d, n["born"], n["hold_d"], NICHES[k]["cov"])
            judged.sort(key=lambda p: -nets[p]["fit"])
            champs[k] = nets[judged[0]]
            dead = judged[-1]
            if dead == judged[0]:
                continue
            parents = [p for p in judged[:max(2, len(judged) // 5)] if nets[p]["fit"] > -1.0] or judged[:3]
            pa = nets[parents[int(rng.integers(len(parents)))]]
            if rng.random() < 0.04 and templates:
                t = templates[int(rng.integers(len(templates)))]
                c = rule_to_net(t, norm, rng, f"İkiz: {describe(dict(t, hold=7))[:70]}")
                child, origin = _nn_clean(neuro._mutate(dict(c, hold=0), rng), k, pa["hold_d"], pyrng), "eski sistemden"
            else:
                other = nets[judged[int(rng.integers(max(3, len(judged) // 2)))]]
                child, origin = _nn_child(pa, other if other is not pa else None, k, rng)
            nets[dead] = born(child, k, d, origin, pa["uid"])
            sync(dead)
            deaths += 1
            births += 1
        if d % 20 == 0:
            for n in nets:
                lo = d - window(n["hold_d"]) - 100
                if len(n["led"]) > 400:
                    n["led"] = [x for x in n["led"] if x[1] >= lo]
        if d % 50 == 0 or d == nd - 1:
            prog(f"Kripto sinir ağı dalı gün gün · {pool.udates[d]} ({d + 1}/{nd} gün) · {deaths} elendi, {births} doğdu", d + 1, nd)
    finals = {}
    for k in ORDER:
        finals[k] = _nn_finals(ctx, X, order, [n for n in nets if n["niche"] == k], k, norm, rng, pyrng, uid)
        finals[k]["record"] = neuro._short(neuro._agg_stats([x for x in champ_rows[k] if x[1] >= SCHOOL_DAYS]))
    del X
    return {"births": births, "deaths": deaths, "groups": finals}


def _nn_entry(ctx: dict, n: dict, k: str, norm: dict, led: tuple, ex: tuple, f: float) -> dict:
    nd = ctx["nd"]
    lo = nd - window(n["hold_d"])
    life, last, exs = (neuro._agg_stats(r) for r in (_rows(led), _rows(led, lo) or _rows(led)[-60:], _rows(ex)))
    stop = list(evolve.STOP_SET[n["stop"]])
    ratio = evolve.RATIO_SET[n["ratio"]]
    ex_txt = describe({"conds": [], "stop": stop, "ratio": ratio, "hold": int(n["hold_d"])}).split(" · ", 1)[-1]
    return {"w": [round(float(x), 5) for x in n["w"]], "sigma": float(n["sigma"]), "stop_i": int(n["stop"]),
            "ratio_i": int(n["ratio"]), "stop": stop, "ratio": ratio, "hold": int(n["hold_d"]), "norm": norm,
            "inputs": list(INPUTS), "hidden": HIDDEN, "label": n.get("label") or "", "origin": n.get("origin") or "",
            "rule": f"Sinir ağı ({HIDDEN} nöron, {len(INPUTS)} gösterge) · {ex_txt}",
            "niche": k, "uid": n["uid"], "fit": round(f, 3), "alive_since": ctx["pool"].udates[n["born"]],
            "life": neuro._short(life), "last": neuro._short(last), "exam": neuro._short(exs),
            "passed": _passed(nd - n["born"], life, last, exs)}


def _nn_finals(ctx: dict, X, order, pop: list[dict], k: str, norm: dict, rng, pyrng, uid: list) -> dict:
    nd = ctx["nd"]
    entries = []
    for n in pop:
        led = tuple(np.array(c) for c in zip(*n["life"])) if n["life"] else EMPTY
        ex = tuple(np.array(c) for c in zip(*n["ex_led"])) if n["ex_led"] else EMPTY
        entries.append((fitness(led, nd, n["born"], n["hold_d"], NICHES[k]["cov"]), n, led, ex))
    entries.sort(key=lambda x: -x[0])
    rows = [_nn_entry(ctx, n, k, norm, led, ex, f) for f, n, led, ex in entries]
    passed = [r for r in rows if r["passed"]][:FINALS]
    top3 = [n for (f, n, _l, _e), r in zip(entries, rows) if r["passed"]][:3] or [x[1] for x in entries[:3]]
    filled = 0
    while len(passed) < FINALS:
        best = None
        for _ in range(FILL_TRIES):
            pa = top3[int(rng.integers(len(top3)))]
            other = entries[int(rng.integers(len(entries)))][1]
            child, _o = _nn_child(pa, other if other is not pa else None, k, rng)
            uid[0] += 1
            child.update(uid=uid[0], born=0, origin="en iyi 3'ün klonu", parent=pa["uid"])
            led, ex = _nn_full(ctx, X, order, child, k)
            f = fitness(led, nd, 0, child["hold_d"], NICHES[k]["cov"])
            if best is None or f > best[0]:
                best = (f, child, led, ex)
        e = _nn_entry(ctx, best[1], k, norm, best[2], best[3], best[0])
        e["filled"] = True
        passed.append(e)
        filled += 1
    return {"bots": passed, "filled": filled, "passed": FINALS - filled}


# ---------------------------------------------------------------- run

def run(seed: int | None = None, progress=None, max_symbols: int | None = None, branches=("kural", "sinir"),
        save: bool = True) -> dict:
    t0 = time.time()
    seed = int(seed if seed is not None else now_ts() % 100000)
    prog = progress or (lambda m, i, n: None)
    pool = cpool.CryptoPool(progress=lambda m, i, n: prog(m, 0, 1), max_symbols=max_symbols)
    try:
        ctx = _context(pool)
        out = {}
        if "kural" in branches:
            out["kural"] = _rule_walk(ctx, seed, prog)
            gc.collect()
        if "sinir" in branches:
            out["sinir"] = _nn_walk(ctx, seed + 1, prog)
        for br, rep in out.items():
            for k, g in rep["groups"].items():
                for rank, b in enumerate(g["bots"], 1):
                    b["rank"] = rank
                    b["branch"] = br
                    b["account"] = 100_000.0 if rank in BIG_RANKS else 100.0
                    b["id"] = f"c-{br}-{k}-{seed}-{b['uid']}"
        report = {"created_at": now_ts(), "seed": seed, "days": int(ctx["nd"]), "range": [pool.first_date, pool.last_date],
                  "symbols": int(pool.symbols), "rows": int(pool.n), "branches": out, "max_symbols": max_symbols,
                  "rules": {"top_k": tree.TOP_K, "max_open": tree.MAX_OPEN, "rule_pop": RULE_POP, "nn_pop": NN_POP,
                            "finals": FINALS, "big_ranks": list(BIG_RANKS), "fear_fng": FEAR_FNG, "top_n": cpool.TOP_N,
                            "niches": {k: {"label": v["label"], "holds": list(v["holds"]), "fear": v["fear"]} for k, v in NICHES.items()}},
                  "seconds": round(time.time() - t0, 1)}
        report = _plain(report)
        if save and max_symbols is None:
            cstore.kv_set("ctree_finals", report)
        return report
    finally:
        del pool
        gc.collect()


def last_report() -> dict | None:
    return cstore.kv_get("ctree_finals")


if __name__ == "__main__":       # py -m marketdesk.crypto.ctree [max_symbols]
    import sys
    logging.basicConfig(level=logging.INFO)
    ms = int(sys.argv[1]) if len(sys.argv) > 1 else None
    t0 = time.time()
    rep = run(seed=7, max_symbols=ms, progress=lambda m, i, n: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True), save=False)
    for br, r in rep["branches"].items():
        print(br, "births", r["births"], "deaths", r["deaths"])
        for k, g in r["groups"].items():
            b = g["bots"][0]
            print(f"  {k:9} passed {g['passed']} filled {g['filled']} | record {g['record'].get('daily_pct')} | #1 hold {b['hold']} "
                  f"fit {b['fit']} last {b['last'].get('daily_pct')} exam {b['exam'].get('daily_pct')} | {b['rule'][:110]}")
    print("seconds", rep["seconds"])
    print(json.dumps(rep)[:300])
