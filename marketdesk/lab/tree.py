"""Bot ağacı (bot tree): two branches (kural = evolved rule bots, sinir = neural-network bots), each with five groups
of 10 elite bots, chosen by a day-by-day walk over the whole 10-year history.

Groups (niş):
  gunluk    trades (almost) every day, holds 1-2 days
  korku     trades only on fear days (VIX >= 20 at the previous close), holds 1-5 days
  haftalik  holds 5 trading days (exits earlier at its stop / target)
  aylik     holds 20 trading days
  serbest   chooses its own holding period (1, 2, 3, 5, 10, 20, 45 or 90 trading days); a stop is always set

Walk: every bot buys up to 5 stocks a day at the open (long only; at most 10 positions open at once, like the paper
accounts), a trade counts for the bot only after it has closed, and bots are judged on their closed trades of the
last ~year (longer for long holds). Neural branch: every trading day each group eliminates its weakest network and a
child of the group's best fifth is born. Rule branch: a rule bot's whole history is computed at birth (~2 s), so the
daily elimination rotates over the five groups (each group every 5th day) to keep the walk to a few hours. The first
bots carry what the old system learned: our tournament / walk / daily bots (and their neural twins), put into each
group with that group's holding period.

Finals: the 10 strongest of each group by their last-year record. When fewer than 10 pass the exam (positive last
year, positive on the never-judged stock group E), the empty places are filled with clones of the group's top 3,
crossed with the other finalists (their learnings carried over), the best of several tries each.

Honest limits: the old bots that seed the walk were chosen on these same 10 years, so the record is optimistic;
long holds have few independent trades, so their statistics are weaker than they look.
"""
from __future__ import annotations

import copy
import gc
import heapq
import json
import logging
import math
import random
import time

import numpy as np

from ..util import now_ts
from . import evolve, learn, longpath, neuro, store

log = logging.getLogger("marketdesk.lab")

HOLD_FREE = (1, 2, 3, 5, 10, 20, 45, 90)
NICHES = {
    "gunluk": {"label": "Günlük", "holds": (1, 2), "fear": False, "cov": 0.6},
    "korku": {"label": "Korku günü (VIX ≥ 20)", "holds": (1, 2, 3, 5), "fear": True, "cov": 0.0},
    "haftalik": {"label": "Haftalık (~5 gün)", "holds": (5,), "fear": False, "cov": 0.0},
    "aylik": {"label": "Aylık (~20 gün)", "holds": (20,), "fear": False, "cov": 0.0},
    "serbest": {"label": "Serbest (1–90 gün, stoplu)", "holds": HOLD_FREE, "fear": False, "cov": 0.0},
}
ORDER = list(NICHES)
BRANCHES = {"kural": "Kural botları (evrim)", "sinir": "Sinir ağı botları"}
RULE_POP, NN_POP = 16, 24          # per group
FINALS = 10
TOP_K = 5
MAX_OPEN = 10
MICRO_RANKS = (2, 5)               # in every group of 10, the bots ranked 2 and 5 trade a $100 account
FILL_TRIES = 4
SCHOOL_DAYS = 120
STOPS = [s for s in evolve.STOP_SET if s[0] != "none"]
TREE: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}
EMPTY = tuple(np.zeros(0, t) for t in (np.int32, np.int32, np.float64, np.float64, np.int32, np.int32, np.int32))


# ---------------------------------------------------------------- shared helpers

def age_min(hold: int) -> int:
    return max(20, hold + 10)


def window(hold: int) -> int:
    return max(250, 3 * hold)


def fitness(arr: tuple, d: int, born: int, hold: int, cov_min: float = 0.0) -> float:
    """Closed entry days (exit before d) of the bot's window: 0.45 t(return) + 0.55 t(return over random entry),
    minus penalties for losing, for too few closed days (scaled with the holding period) and, in the daily group,
    for trading on too few days."""
    E, D, DR, DE = arr[0], arr[1], arr[2], arr[3]
    lo = d - window(hold)
    j1 = int(np.searchsorted(D, d))
    j0 = int(np.searchsorted(D, lo))
    sel = np.nonzero(E[j0:j1] < d)[0] + j0
    if len(sel) < 5:
        sel = np.nonzero(E[:j1] < d)[0][-60:]
    nd = len(sel)
    age = d - born
    if nd < 5:
        if age >= hold + 90 and nd == 0:
            return -6.0
        if age >= hold + 60 and nd < 3:
            return -4.0 + 0.3 * nd
        return -0.4 + 0.05 * nd
    dr, de = DR[sel], DE[sel]
    mr, me = float(dr.mean()), float(de.mean())
    f = 0.45 * mr / (float(dr.std()) + 1e-9) * math.sqrt(nd) + 0.55 * me / (float(de.std()) + 1e-9) * math.sqrt(nd)
    f -= (1.5 if mr < 0 else 0.0) + (1.0 if me < 0 else 0.0)
    if cov_min > 0:
        span = max(1, min(d - max(lo, born), window(hold)))
        cov = float(np.count_nonzero(D[j0:j1] >= max(lo, born))) / span
        f -= 4.0 * max(0.0, cov_min - cov) / cov_min
    return f


def _rows(arr: tuple, lo: int = -1) -> list[tuple]:
    rows = [tuple(x) for x in zip(*(a.tolist() for a in arr))]
    return rows if lo < 0 else [r for r in rows if r[1] >= lo]


def _cap(days: np.ndarray, exits: np.ndarray) -> np.ndarray:
    """Keep-mask for rows sorted by (day, rank): at most MAX_OPEN positions open at once (a position frees its place
    the day after it exits)."""
    keep = np.zeros(len(days), bool)
    heap: list[int] = []
    for i, (d, e) in enumerate(zip(days.tolist(), exits.tolist())):
        while heap and heap[0] < d:
            heapq.heappop(heap)
        if len(heap) < MAX_OPEN:
            keep[i] = True
            heapq.heappush(heap, e)
    return keep


def _agg(ctx: dict, days: np.ndarray, res: np.ndarray, exits: np.ndarray, hold: int) -> tuple:
    """Per entry day: (known day, entry day, day return, return over random entry, trades, wins, year). The known day
    is the last exit, but never before entry + hold - 1: the random-entry benchmark of a `hold`-day trade is only
    known then, so an early stop-out does not let the walk peek at the market's next weeks."""
    ok = ~np.isnan(res)
    if not ok.any():
        return EMPTY
    days, res, exd = days[ok], res[ok].astype(np.float64), (exits[ok] - days[ok]).astype(np.int32)
    ud, inv = np.unique(days, return_inverse=True)
    cnt = np.bincount(inv)
    dr = np.bincount(inv, weights=res) / cnt
    wins = np.bincount(inv, weights=(res > 0).astype(np.float64)).astype(np.int32)
    emax = np.zeros(len(ud), np.int32)
    np.maximum.at(emax, inv, exd)
    de = dr - ctx["drift"](hold)[ud]
    return (np.maximum(ud + emax, ud + hold - 1).astype(np.int32), ud.astype(np.int32), dr, de, cnt.astype(np.int32), wins,
            ctx["years"][ud].astype(np.int32))


def _picked(ctx: dict, idx: np.ndarray, score: np.ndarray) -> np.ndarray:
    """Rows of each day's TOP_K best scores (score sorted descending), rows sorted by (day, rank)."""
    if not len(idx):
        return idx
    days = ctx["pool"].dayidx[idx]
    order = np.lexsort((-score, days))
    idx, days = idx[order], days[order]
    first = np.r_[True, days[1:] != days[:-1]]
    start = np.maximum.accumulate(np.where(first, np.arange(len(days)), 0))
    return idx[(np.arange(len(days)) - start) < TOP_K]


def _trades(ctx: dict, idx: np.ndarray, stop: int, ratio: int, hold: int) -> tuple:
    """Outcomes of rows already sorted by (day, rank), the position cap applied, aggregated per entry day."""
    if not len(idx):
        return EMPTY
    pool = ctx["pool"]
    res, exd = longpath.outcome(pool, idx, 1, stop, ratio, hold)
    days = pool.dayidx[idx]
    ed = longpath.exit_day(pool, idx, exd)
    exits = np.where(ed >= 0, ed, ctx["nd"] + 10_000)
    keep = _cap(days, exits)
    return _agg(ctx, days[keep], res[keep], exits[keep], hold)


def _context(pool) -> dict:
    longpath.attach(pool)
    drifts: dict[int, np.ndarray] = {}

    def drift(h: int) -> np.ndarray:
        if h not in drifts:
            drifts[h] = longpath.drift(pool, h)
        return drifts[h]

    fear_day = evolve.day_regimes(pool) == 0
    return {"pool": pool, "nd": pool.n_days, "years": pool.day_year, "drift": drift, "fear_day": fear_day,
            "fear_row": fear_day[pool.dayidx], "train": pool.group <= 3, "exam": pool.group == 4,
            "rsi2": np.nan_to_num(pool.F["rsi2"].astype(np.float32), nan=9.0)}


def _passed(age: int, life: dict, last: dict, exam: dict) -> bool:
    return bool(age >= 20 and life.get("days", 0) >= 8 and last.get("days", 0) >= 5 and last.get("daily_pct", -1) > 0
                and last.get("daily_excess_pct", -1) > 0 and exam.get("days", 0) >= 5 and exam.get("daily_pct", -1) > 0)


def _step_hold(h: int, niche: str, rng) -> int:
    holds = NICHES[niche]["holds"]
    if h not in holds:
        return holds[min(range(len(holds)), key=lambda i: abs(holds[i] - h))]
    if len(holds) > 1 and rng.random() < 0.2:
        i = holds.index(h)
        return holds[min(len(holds) - 1, max(0, i + (1 if rng.random() < 0.5 else -1)))]
    return h


# ---------------------------------------------------------------- rule branch

def _rule_clean(b: dict, niche: str, rng) -> dict:
    """Long only, always a stop, the group's holding period."""
    stop = tuple(b["stop"])
    if stop not in STOPS:
        stop = ("atr", 2.0)
    ratio = b["ratio"] if b["ratio"] in evolve.RATIO_SET else 1.5
    return {"conds": [list(c) for c in b["conds"]], "side": 1, "stop": list(stop), "ratio": ratio,
            "hold": _step_hold(int(b.get("hold") or 5), niche, rng)}


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
        idx = _picked(ctx, idx, -ctx["rsi2"][idx])        # the most oversold first
        return _trades(ctx, idx, s_i, r_i, self.b["hold"])

    def fitness(self, d: int) -> float:
        return fitness(self.led, d, self.born, self.b["hold"], NICHES[self.niche]["cov"])


def _q_genome(pool, b: dict) -> dict:
    conds = [[f, op, float(np.searchsorted(pool.Q[f], thr) / (evolve.QGRID - 1))] for f, op, thr, _sp in b["conds"]]
    return {"conds": conds, "side": 1, "stop": evolve.STOP_SET.index(tuple(b["stop"])),
            "ratio": evolve.RATIO_SET.index(b["ratio"]), "hold": evolve.HOLD_SET.index(5)}


def _rule_child(pool, pa: dict, other: dict | None, niche: str, rng: random.Random) -> tuple[dict, str]:
    """Child of `pa`: mostly a micro-clone; sometimes a bigger mutation; with `other`, a crossover that carries
    another bot's conditions over (its learning)."""
    r = rng.random()
    if other is not None and r < 0.25:
        g = evolve.crossover(_q_genome(pool, pa), _q_genome(pool, other), rng)
        g = evolve.mutate(g, rng) if rng.random() < 0.5 else g
        child, origin = {"conds": evolve.conds_abs(pool, g), "stop": list(evolve.STOP_SET[g["stop"]]),
                         "ratio": evolve.RATIO_SET[g["ratio"]], "hold": pa["hold"]}, "çaprazlama"
    elif r < 0.4:
        g = evolve.mutate(_q_genome(pool, pa), rng)
        child, origin = {"conds": evolve.conds_abs(pool, g), "stop": list(evolve.STOP_SET[g["stop"]]),
                         "ratio": evolve.RATIO_SET[g["ratio"]], "hold": pa["hold"]}, "mutasyon"
    else:
        c = learn._clone(pool, dict(pa, hold=5), rng)
        child, origin = {"conds": c["conds"], "stop": c["stop"], "ratio": c["ratio"], "hold": pa["hold"]}, "klon"
    return _rule_clean(child, niche, rng), origin


def _rule_seeds() -> dict[str, list[dict]]:
    """What the old system learned: every rule bot we have (lineage, tournament, walk and daily finals), long side."""
    out: dict[str, dict] = {}

    def add(b: dict, src: str) -> None:
        try:
            if int(b.get("side", 1)) < 0 or not b.get("conds"):
                return
            key = repr(sorted((c[0], c[1], round(float(c[2]), 4)) for c in b["conds"])) + repr(b.get("stop"))
            out.setdefault(key, {"conds": [list(c) for c in b["conds"]], "side": 1, "stop": list(b.get("stop") or ["atr", 2.0]),
                                 "ratio": float(b.get("ratio") or 1.5), "hold": int(b.get("hold") or 5), "src": src})
        except Exception:  # noqa: BLE001 - one malformed old bot must not stop the walk
            pass
    for x in learn._load_lineage():
        if x["status"] in ("aktif", "aday"):
            add(x["bot"], "canlı öğrenme botu")
    for k, src in (("evo_survivors_base40", "turnuva botu"), ("evo_survivors", "turnuva botu"), ("evo_daily_finals", "günlük turnuva botu")):
        for sv in store.kv_get(k) or []:
            if sv.get("conds_abs"):
                add(learn._from_survivor(sv), src)
    for b in store.kv_get("learn_pending_cands") or []:
        add(b, "gün gün evrim finalisti")
    seeds = list(out.values())
    fear = [b for b in seeds if any(c[0] == "vix" and c[1] == ">" for c in b["conds"])]
    daily = [b for b in seeds if b["src"] == "günlük turnuva botu"]
    return {"all": seeds, "fear": fear, "daily": daily}


def _rule_walk(ctx: dict, seed: int, prog) -> dict:
    pool, nd = ctx["pool"], ctx["nd"]
    rng = random.Random(seed)
    seeds = _rule_seeds()
    uid = [0]
    pops: dict[str, list[RuleBot]] = {}
    templates: dict[str, list[dict]] = {}
    for niche in ORDER:
        first = seeds["fear"] if niche == "korku" else seeds["daily"] if niche == "gunluk" else []
        pool_seeds = first + [b for b in seeds["all"] if b not in first]
        templates[niche] = [_rule_clean(b, niche, rng) for b in pool_seeds] or [_rule_clean(
            {"conds": [["rsi2", "<", 0.1, False]], "stop": ["atr", 2.0], "ratio": 1.5, "hold": 5}, niche, rng)]
        start = templates[niche][:RULE_POP]
        while len(start) < RULE_POP:
            c, _o = _rule_child(pool, rng.choice(templates[niche]), None, niche, rng)
            start.append(c)
        pops[niche] = []
        for b in start:
            uid[0] += 1
            pops[niche].append(RuleBot(ctx, b, niche, 0, uid[0], "eski sistemden"))
        prog(f"Kural dalı · {NICHES[niche]['label']}: {len(pops[niche])} başlangıç botu hazır", 0, nd)
    champs = {k: pops[k][0] for k in ORDER}
    champ_rows: dict[str, list] = {k: [] for k in ORDER}
    births, deaths = sum(len(v) for v in pops.values()), 0
    for d in range(nd):
        for k in ORDER:
            i = champs[k].by_day.get(d)
            if i is not None:
                champ_rows[k].append(tuple(a[i].item() for a in champs[k].led))
        k = ORDER[d % len(ORDER)]                     # the elimination rotates over the five groups
        judged = [n for n in pops[k] if d - n.born >= age_min(n.b["hold"])]
        if len(judged) < 3:
            continue
        for n in judged:
            n.fit = n.fitness(d)
        judged.sort(key=lambda n: -n.fit)
        champs[k] = judged[0]
        parents = [n for n in judged[:max(2, len(judged) // 5)] if n.fit > -1.0] or judged[:3]
        dead = judged[-1]
        if dead is champs[k]:
            continue
        deaths += 1
        if rng.random() < 0.04:
            child, origin, par = copy.deepcopy(rng.choice(templates[k])), "eski sistemden", None
        else:
            pa = rng.choice(parents)
            other = rng.choice(judged[:max(3, len(judged) // 2)]).b
            child, origin = _rule_child(pool, pa.b, other if other is not pa.b else None, k, rng)
            par = pa.uid
        uid[0] += 1
        pops[k][pops[k].index(dead)] = RuleBot(ctx, child, k, d, uid[0], origin, par)
        births += 1
        if d % 50 == 0 or d == nd - 1:
            prog(f"Kural dalı gün gün · {pool.udates[d]} ({d + 1}/{nd} gün) · {deaths} elendi, {births} doğdu", d + 1, nd)
    finals = {}
    for k in ORDER:
        finals[k] = _rule_finals(ctx, pops[k], k, rng, uid)
        finals[k]["record"] = neuro._short(neuro._agg_stats([x for x in champ_rows[k] if x[1] >= SCHOOL_DAYS]))
    return {"births": births, "deaths": deaths, "groups": finals}


def _rule_entry(ctx, n: RuleBot, k: str, f: float) -> dict:
    nd = ctx["nd"]
    lo = nd - window(n.b["hold"])
    life, last, ex = (neuro._agg_stats(r) for r in (_rows(n.led), _rows(n.led, lo) or _rows(n.led)[-60:], _rows(n.ex)))
    return dict(n.b, rule=learn._describe(dict(n.b)), origin=n.origin, fit=round(f, 3), niche=k, uid=n.uid,
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
            child, _o = _rule_child(pool, pa, other if other is not pa else None, k, rng)
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


# ---------------------------------------------------------------- neural branch

def _nn_clean(b: dict, niche: str, hold: int, rng) -> dict:
    c = {k: b[k] for k in ("w", "sigma", "stop", "ratio") if k in b}
    c["label"] = b.get("label") or ""
    c["origin"] = b.get("origin") or ""
    c["hold_d"] = _step_hold(int(hold), niche, rng)
    return c


def _nn_child(pa: dict, other: dict | None, niche: str, rng) -> tuple[dict, str]:
    if other is not None and rng.random() < 0.5:
        c = neuro._crossover(dict(pa, hold=0), dict(other, hold=0), rng)
        origin = "çaprazlama"
    else:
        c, origin = dict(pa, hold=0), "klon"
    c = neuro._mutate(dict(c, hold=0), rng)
    return _nn_clean(dict(c, label=pa.get("label")), niche, pa["hold_d"], rng), origin


def _nn_full(ctx: dict, X: np.ndarray, order: np.ndarray, net: dict, niche: str, born: int = 0) -> tuple:
    """A network's whole history at once (for filling the finals): (train arrays, exam arrays)."""
    pool = ctx["pool"]
    sc = neuro.forward(net["w"], X)
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
    di, grp = pool.dayidx, pool.group
    norm = neuro._norm_params(pool, np.nonzero((di < max(250, SCHOOL_DAYS)) & (grp <= 3))[0], rng)
    prog("Sinir ağı dalı: girdiler hazırlanıyor…", 0, nd)
    order = np.argsort(di, kind="stable")
    X = neuro._matrix(pool, order, norm)
    starts = np.searchsorted(di[order], np.arange(nd + 1))
    g_sorted = grp[order]
    H, d_in = neuro.HIDDEN, len(neuro.INPUTS)
    nets: list[dict] = []
    uid = [0]

    def born(b: dict, niche: str, day: int, origin: str, parent=None) -> dict:
        uid[0] += 1
        return dict(b, niche=niche, uid=uid[0], born=day, origin=origin or b.get("origin") or "eski sistemden", parent=parent,
                    led=[], life=[], ex_led=[], open=[], open_ex=[], fit=0.0)

    for niche in ORDER:
        for b in neuro.seed_population(norm, rng, NN_POP):
            hold = rng.choice(NICHES[niche]["holds"]) if niche == "serbest" else NICHES[niche]["holds"][-1]
            nets.append(born(_nn_clean(b, niche, int(hold), rng), niche, 0, b.get("origin") or "eski sistemden"))
    W1 = np.zeros((d_in, len(nets) * H), np.float32)
    B1 = np.zeros(len(nets) * H, np.float32)
    W2 = np.zeros((len(nets), H), np.float32)
    B2 = np.zeros(len(nets), np.float32)

    def sync(p: int) -> None:
        w1, b1, w2, b2 = neuro._unpack(nets[p]["w"].astype(np.float32))
        W1[:, p * H:(p + 1) * H], B1[p * H:(p + 1) * H], W2[p], B2[p] = w1, b1, w2, b2

    for p in range(len(nets)):
        sync(p)
    champs = {k: next(n for n in nets if n["niche"] == k) for k in ORDER}
    champ_rows: dict[str, list] = {k: [] for k in ORDER}
    births, deaths = len(nets), 0
    templates = neuro._archetype_templates()
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
                    room = min(TOP_K, MAX_OPEN - len(op))
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
                r, e = longpath.outcome(pool, idx, 1, *key)
                ed = longpath.exit_day(pool, idx, e)
                for i, rr, ee in zip(idx.tolist(), r.tolist(), ed.tolist()):
                    res_map[(key, i)] = (rr, ee)          # ee = market-day index of the exit (-1 = still open)
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
        # every day, every group: the weakest network goes, a child of the group's best fifth is born
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
            if rng.random() < 0.04:
                t = templates[int(rng.integers(len(templates)))]
                c = neuro.rule_to_nn(t["conds"], t["stop"], t["ratio"], 5, norm, rng, t["origin"], t["label"])
                child, origin = _nn_clean(neuro._mutate(c, rng), k, pa["hold_d"], rng), "eski sistemden"
            else:
                other = nets[judged[int(rng.integers(max(3, len(judged) // 2)))]]
                child, origin = _nn_child(pa, other if other is not pa else None, k, rng)
            nets[dead] = born(child, k, d, origin, pa["uid"])
            sync(dead)
            deaths += 1
            births += 1
        if d % 20 == 0:                      # keep the per-network lists short (the window is at most 270 days)
            for n in nets:
                lo = d - window(n["hold_d"]) - 100
                if len(n["led"]) > 400:
                    n["led"] = [x for x in n["led"] if x[1] >= lo]
        if d % 50 == 0 or d == nd - 1:
            prog(f"Sinir ağı dalı gün gün · {pool.udates[d]} ({d + 1}/{nd} gün) · her gün her grupta 1 eleme "
                 f"({deaths} elendi, {births} doğdu)", d + 1, nd)
    finals = {}
    for k in ORDER:
        finals[k] = _nn_finals(ctx, X, order, [n for n in nets if n["niche"] == k], k, norm, rng, uid)
        finals[k]["record"] = neuro._short(neuro._agg_stats([x for x in champ_rows[k] if x[1] >= SCHOOL_DAYS]))
    del X
    return {"births": births, "deaths": deaths, "groups": finals}


def _nn_entry(ctx: dict, n: dict, k: str, norm: dict, led: tuple, ex: tuple, f: float) -> dict:
    nd = ctx["nd"]
    lo = nd - window(n["hold_d"])
    life, last, exs = (neuro._agg_stats(r) for r in (_rows(led), _rows(led, lo) or _rows(led)[-60:], _rows(ex)))
    stop = list(evolve.STOP_SET[n["stop"]])
    ratio = evolve.RATIO_SET[n["ratio"]]
    return {"w": [round(float(x), 5) for x in n["w"]], "sigma": float(n["sigma"]), "stop_i": int(n["stop"]),
            "ratio_i": int(n["ratio"]), "stop": stop, "ratio": ratio, "hold": int(n["hold_d"]), "norm": norm,
            "inputs": list(neuro.INPUTS), "hidden": neuro.HIDDEN, "label": n.get("label") or "", "origin": n.get("origin") or "",
            "rule": f"Sinir ağı ({neuro.HIDDEN} nöron, 32 gösterge) · "
                    + learn._describe({'conds': [], 'side': 1, 'stop': stop, 'ratio': ratio, 'hold': int(n['hold_d'])}).split(' · ', 1)[-1],
            "niche": k, "uid": n["uid"], "fit": round(f, 3), "alive_since": ctx["pool"].udates[n["born"]],
            "life": neuro._short(life), "last": neuro._short(last), "exam": neuro._short(exs),
            "passed": _passed(nd - n["born"], life, last, exs)}


def _nn_finals(ctx: dict, X, order, pop: list[dict], k: str, norm: dict, rng, uid: list) -> dict:
    nd = ctx["nd"]
    entries = []
    for n in pop:
        led = tuple(np.array(c) for c in zip(*n["life"])) if n["life"] else EMPTY
        ex = tuple(np.array(c) for c in zip(*n["ex_led"])) if n["ex_led"] else EMPTY
        f = fitness(led, nd, n["born"], n["hold_d"], NICHES[k]["cov"])
        entries.append((f, n, led, ex))
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

def run(seed: int | None = None, progress=None, max_symbols: int | None = None, branches=("kural", "sinir")) -> dict:
    t0 = time.time()
    store.init()
    seed = int(seed if seed is not None else now_ts() % 100000)
    prog = progress or (lambda m, i, n: None)
    prog("10 yıllık havuz ve uzun fiyat yolları hazırlanıyor (birkaç dakika)…", 0, 1)
    pool = evolve.Pool(progress=lambda m, i, n: prog(m, 0, 1), max_symbols=max_symbols)
    try:
        ctx = _context(pool)
        out = {}
        # a finished branch is kept (lab.db kv) until the whole tree is done: when the other branch fails (e.g. the
        # computer runs out of memory), a restart on the same data skips the hours of the finished one
        fp = f"{pool.udates[0]}|{pool.last_date}|{pool.symbols}|{pool.n}"
        part = (store.kv_get("tree_partial") or {}) if max_symbols is None else {}
        if part.get("key") != fp:
            part = {"key": fp}
        for br, walk, sd in (("kural", _rule_walk, seed), ("sinir", _nn_walk, seed + 1)):
            if br not in branches:
                continue
            if part.get(br):
                prog(f"{BRANCHES[br]}: önceki çalıştırmada bitmişti, kaydedilen sonuç kullanılıyor", 0, 1)
                out[br] = part[br]
                continue
            out[br] = _plain(walk(ctx, sd, prog))
            gc.collect()
            if max_symbols is None:
                part[br] = out[br]
                store.kv_set("tree_partial", part)
        for br, rep in out.items():
            for k, g in rep["groups"].items():
                for rank, b in enumerate(g["bots"], 1):
                    b["rank"] = rank
                    b["branch"] = br
                    b["account"] = 100.0 if rank in MICRO_RANKS else 100_000.0
                    b["id"] = f"{br}-{k}-{seed}-{b['uid']}"
        report = {"created_at": now_ts(), "seed": seed, "days": int(ctx["nd"]), "range": [pool.udates[0], pool.last_date],
                  "symbols": int(pool.symbols), "branches": out, "max_symbols": max_symbols,
                  "rules": {"top_k": TOP_K, "max_open": MAX_OPEN, "rule_pop": RULE_POP, "nn_pop": NN_POP, "finals": FINALS,
                            "micro_ranks": list(MICRO_RANKS), "niches": {k: {"label": v["label"], "holds": list(v["holds"]),
                                                                            "fear": v["fear"]} for k, v in NICHES.items()}},
                  "seconds": round(time.time() - t0, 1)}
        report = _plain(report)
        if max_symbols is None:
            store.kv_set("tree_finals", report)
            store.kv_set("tree_partial", None)
        return report
    finally:
        del pool
        gc.collect()


def _plain(obj):
    """numpy numbers -> plain Python ones (the report is stored as JSON)."""
    def conv(x):
        if isinstance(x, np.generic):
            return x.item()
        if isinstance(x, np.ndarray):
            return x.tolist()
        raise TypeError(type(x))
    return json.loads(json.dumps(obj, default=conv))


def last_report() -> dict | None:
    return store.kv_get("tree_finals")
