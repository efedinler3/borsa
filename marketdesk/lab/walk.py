"""Gün gün evrim (rule bots): our real rule bots live through the 10 years day by day, like the neural walk in
neuro.py, but with the bots themselves (not neural twins).

Every trading day each bot buys (or sells) up to 5 of the stocks its conditions select at the open (the most oversold
first, as in the portfolio simulation). A trade counts for the bot only once it has closed; every day, among the bots
at least 20 days old, the weakest one (judged on its closed trades of the last ~year) is eliminated and a child of
the best fifth is born: mostly a micro-mutation (thresholds move a little, the backbone stays), sometimes a bigger
mutation (a condition may change), now and then an old tournament bot comes back. No bot without a stop is born.

Honest limits: the starting bots were chosen by tournaments that saw these 10 years, so the early record is
optimistic; stock group E is never used for judging (exam), and only the live days after the tournament are clean.
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

from ..util import now_ts
from . import evolve, learn, neuro, store

log = logging.getLogger("marketdesk.lab")

TOP_K = 5
AGE_MIN, WINDOW, MIN_JUDGE_DAYS = neuro.AGE_MIN, neuro.WINDOW, neuro.MIN_JUDGE_DAYS
SCHOOL_DAYS = neuro.SCHOOL_DAYS
FINALS = 10
WALK: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}


def _q_genome(pool, b: dict) -> dict:
    """Absolute-threshold bot -> quantile genome (for evolve.mutate)."""
    s_i, r_i, h_i = learn._idx(b)
    conds = [[f, op, float(np.searchsorted(pool.Q[f], thr) / (evolve.QGRID - 1))] for f, op, thr, _sp in b["conds"]]
    return {"conds": conds, "side": b["side"], "stop": s_i, "ratio": r_i, "hold": h_i}


def _from_genome(pool, g: dict) -> dict:
    stop = evolve.STOP_SET[g["stop"]]
    if stop[0] == "none":                       # no stop-less bots (the user's rule)
        stop = ("atr", 2.0)
    return {"conds": evolve.conds_abs(pool, g), "side": g["side"], "stop": list(stop), "ratio": evolve.RATIO_SET[g["ratio"]],
            "hold": evolve.HOLD_SET[g["hold"]]}


class Bot:
    """One living bot: its per-entry-day results (computed once at birth for the whole history; only days from its
    birth on count)."""

    def __init__(self, pool, b: dict, born: int, uid: int, origin: str, parent, ctx: dict):
        self.b, self.born, self.uid, self.origin, self.parent = b, born, uid, origin, parent
        self.fit = 0.0
        m = learn._mask(pool, b)
        self.led = self._days(pool, m & ctx["train"], ctx, born)
        self.ex = self._days(pool, m & ctx["exam"], ctx, born)
        self.by_day = {int(d): i for i, d in enumerate(self.led[1])}

    def _days(self, pool, m, ctx, born):
        """Arrays sorted by entry day: (exit day, entry day, day return, return over drift, count, wins, year)."""
        idx = np.nonzero(m & (pool.dayidx >= born))[0]
        empty = tuple(np.zeros(0, t) for t in (np.int32, np.int32, np.float64, np.float64, np.int32, np.int32, np.int32))
        if not len(idx):
            return empty
        side = self.b["side"]
        days = pool.dayidx[idx]
        key = ctx["rsi2"][idx] * side                 # long: most oversold first; short: most overbought first
        order = np.lexsort((key, days))
        idx, days = idx[order], days[order]
        first = np.r_[True, days[1:] != days[:-1]]
        start = np.maximum.accumulate(np.where(first, np.arange(len(days)), 0))
        keep = (np.arange(len(days)) - start) < TOP_K
        idx, days = idx[keep], days[keep]
        s_i, r_i, h_i = learn._idx(self.b)
        res, ex_j, _mk = pool.outcome(idx, side, s_i, r_i, h_i, detail=True)
        ok = ~np.isnan(res)
        if not ok.any():
            return empty
        days, res, ex_j = days[ok], res[ok].astype(np.float64), ex_j[ok].astype(np.int32)
        ud, inv = np.unique(days, return_inverse=True)
        cnt = np.bincount(inv)
        dr = np.bincount(inv, weights=res) / cnt
        wins = np.bincount(inv, weights=(res > 0).astype(np.float64)).astype(np.int32)
        emax = np.zeros(len(ud), np.int32)
        np.maximum.at(emax, inv, ex_j)
        de = dr - side * ctx["drift"][self.b["hold"]][ud]
        return ((ud + emax).astype(np.int32), ud.astype(np.int32), dr, de, cnt.astype(np.int32), wins, ctx["years"][ud].astype(np.int32))

    def fitness(self, d: int, lo: int) -> float:
        """neuro._fast_fit on arrays: closed entry days (exit < d) of the window, else its last 60 closed active days."""
        E, D, DR, DE = self.led[0], self.led[1], self.led[2], self.led[3]
        j1 = int(np.searchsorted(D, d))
        j0 = int(np.searchsorted(D, lo))
        sel = np.nonzero(E[j0:j1] < d)[0] + j0
        if len(sel) < MIN_JUDGE_DAYS:
            sel = np.nonzero(E[:j1] < d)[0][-60:]
        nd = len(sel)
        age = d - self.born
        if nd < MIN_JUDGE_DAYS:
            if age >= 90 and nd == 0:
                return -6.0
            if age >= 60 and nd < 3:
                return -4.0 + 0.3 * nd
            return -0.4 + 0.05 * nd
        dr, de = DR[sel], DE[sel]
        mr, me = float(dr.mean()), float(de.mean())
        f = 0.45 * mr / (float(dr.std()) + 1e-9) * math.sqrt(nd) + 0.55 * me / (float(de.std()) + 1e-9) * math.sqrt(nd)
        return f - (1.5 if mr < 0 else 0.0) - (1.0 if me < 0 else 0.0)

    def rows(self, arrs, lo: int = -1) -> list[tuple]:
        E, D = arrs[0], arrs[1]
        return [tuple(x) for x in zip(*(a.tolist() for a in arrs))] if lo < 0 else \
            [tuple(x) for x, dd in zip(zip(*(a.tolist() for a in arrs)), D.tolist()) if dd >= lo]


def run(seed: int | None = None, progress=None, max_symbols: int | None = None) -> dict:
    t0 = time.time()
    store.init()
    seed = int(seed if seed is not None else now_ts() % 100000)
    rng = random.Random(seed)
    prog = progress or (lambda m, i, n: None)
    prog("10 yıllık havuz hazırlanıyor (birkaç dakika)…", 0, 1)
    pool = evolve.Pool(progress=lambda m, i, n: prog(m, 0, 1), max_symbols=max_symbols)
    try:
        nd = pool.n_days
        di = pool.dayidx
        drift = {}
        for h in evolve.HOLD_SET:
            c = pool.C[:, h - 1]
            ok = c != evolve.PATH_NAN
            s_ = np.bincount(di[ok], weights=c[ok].astype(np.float64) / 10000, minlength=nd)
            n_ = np.bincount(di[ok], minlength=nd)
            drift[h] = np.where(n_ > 0, s_ / np.maximum(n_, 1), 0.0)
        ctx = {"train": pool.group <= 3, "exam": pool.group == 4, "drift": drift, "years": pool.day_year,
               "rsi2": np.nan_to_num(pool.F["rsi2"].astype(np.float32), nan=9.0)}
        # the starting population: every bot we have (trading bots of both arms and the candidates), duplicates removed
        start, seen = [], set()
        for x in learn._load_lineage():
            if x["status"] in ("aktif", "aday") and x["id"] not in seen:
                seen.add(x["id"])
                start.append({k: x["bot"][k] for k in ("conds", "side", "stop", "ratio", "hold")})
        templates = [learn._from_survivor(sv) for sv in (store.kv_get("evo_survivors_base40") or [])]
        templates = [{k: t[k] for k in ("conds", "side", "stop", "ratio", "hold")} for t in templates if t["stop"][0] != "none"]
        if not start:
            start = list(templates)
        uid = [0]

        def born(b: dict, day: int, origin: str, parent=None) -> Bot:
            uid[0] += 1
            return Bot(pool, b, day, uid[0], origin, parent, ctx)

        prog(f"{len(start)} bot gün gün yaşamaya hazırlanıyor…", 0, nd)
        bots = [born(b, 0, "elimizdeki bot") for b in start]
        champion = bots[0]
        champ_rows, champ_ex = [], []
        births, deaths, history = len(bots), 0, []
        for d in range(nd):
            # today's champion's trades enter the record (chosen from past closed trades only)
            i = champion.by_day.get(d)
            if i is not None:
                champ_rows.append(tuple(a[i].item() for a in champion.led))
            j = int(np.searchsorted(champion.ex[1], d))
            if j < len(champion.ex[1]) and champion.ex[1][j] == d:
                champ_ex.append(tuple(a[j].item() for a in champion.ex))
            if d < AGE_MIN:
                continue
            lo = d - WINDOW
            judged = [n for n in bots if d - n.born >= AGE_MIN]
            if not judged:
                continue
            for n in judged:
                n.fit = n.fitness(d, lo)
            judged.sort(key=lambda n: -n.fit)
            champion = judged[0]
            parents = [n for n in judged[:max(2, len(judged) // 5)] if n.fit > -1.0] or judged[:3]
            dead = judged[-1]
            if dead is champion:
                continue
            deaths += 1
            r = rng.random()
            if r < 0.04 and templates:
                child, origin, par = copy.deepcopy(rng.choice(templates)), "eski turnuva botu", None
            else:
                pa = rng.choice(parents)
                par = pa.uid
                if r < 0.20:     # bigger mutation: a condition may change, appear or disappear
                    child, origin = _from_genome(pool, evolve.mutate(_q_genome(pool, pa.b), rng)), "mutasyon"
                else:
                    c = learn._clone(pool, pa.b, rng)
                    child, origin = {k: c[k] for k in ("conds", "side", "stop", "ratio", "hold")}, "klon"
            bots[bots.index(dead)] = born(child, d, origin, par)
            births += 1
            if d % 40 == 0 or d == nd - 1:
                cs = neuro._agg_stats([x for x in champ_rows if x[1] >= SCHOOL_DAYS])
                history.append({"day": pool.udates[d], "champion_fit": round(champion.fit, 2), "record_daily_pct": cs.get("daily_pct"),
                                "births": births, "deaths": deaths})
                prog(f"Gün gün kural botu evrimi · {pool.udates[d]} ({d + 1}/{nd} gün) · her gün 1 eleme (toplam {deaths} elendi, "
                     f"{births} doğdu) · şampiyon sicili: günlük {cs.get('daily_pct', 0) or 0:+.3f}% "
                     f"(piyasa üstü {cs.get('daily_excess_pct', 0) or 0:+.3f}%)", d + 1, nd)
        # final population: judged on the last year
        lo = nd - WINDOW
        finals = []
        for n in bots:
            f = n.fitness(nd, lo)
            last = n.rows(n.led, lo) or n.rows(n.led)[-60:]
            finals.append((f, n, neuro._agg_stats(n.rows(n.led)), neuro._agg_stats(last), neuro._agg_stats(n.rows(n.ex))))
        finals.sort(key=lambda x: -x[0])
        models, pending = [], []
        for f, n, st_life, st_last, st_ex in finals[:FINALS]:
            ok = bool(nd - n.born >= 20 and st_life.get("days", 0) >= 8 and st_last.get("days", 0) >= 5
                      and st_last.get("daily_pct", -1) > 0 and st_last.get("daily_excess_pct", -1) > 0
                      and st_ex.get("days", 0) >= 5 and st_ex.get("daily_pct", -1) > 0)
            rule = learn._describe(n.b)
            models.append({"uid": n.uid, "rule": rule, "origin": n.origin, "alive_since": pool.udates[n.born], "fit": round(f, 3),
                           "life": neuro._short(st_life), "last": neuro._short(st_last), "exam": neuro._short(st_ex), "passed": ok})
            if ok:
                pending.append(dict(n.b, rule=rule, origin="gün gün evrim"))
        rec = neuro._agg_stats([x for x in champ_rows if x[1] >= SCHOOL_DAYS])
        rec_ex = neuro._agg_stats([x for x in champ_ex if x[1] >= SCHOOL_DAYS])
        origins: dict = {}
        for n in bots:
            origins[n.origin] = origins.get(n.origin, 0) + 1
        report = {"created_at": now_ts(), "seed": seed, "days": int(nd), "range": [pool.udates[0], pool.last_date],
                  "start_bots": len(start), "population": len(bots), "births": births, "deaths": deaths,
                  "record": rec, "record_unseen": rec_ex, "history": history, "finals": models, "origins_alive": origins,
                  "rules": {"elim_per_day": 1, "age_min": AGE_MIN, "window": WINDOW, "top_k": TOP_K},
                  "seconds": round(time.time() - t0, 1)}
        store.kv_set("walk_report", report)
        store.kv_set("learn_pending_cands", pending)
        return report
    finally:
        del pool
        gc.collect()


def last_report() -> dict | None:
    return store.kv_get("walk_report")
