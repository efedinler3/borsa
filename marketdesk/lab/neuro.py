"""Sinir ağı evrimi (neuroevolution): a separate population of small neural networks bred on the 10-year pool.

Each bot is a tiny network (31 indicators + news flag -> 12 tanh units -> 1 score) plus an exit rule (stop, target
ratio, holding days). Every trading day it buys the stocks it scores highest (score > 0, at most 5 a day) at the open,
long only (cash account). Weights are not trained by gradients but evolved: elites, tournament selection, unit-wise
crossover, self-adaptive Gaussian mutation, a few random newcomers.

Honest splits (no look-ahead):
- training: stock groups A–C, years before the inner validation year
- validation (early stopping, choosing the finals): the inner validation year, groups A–D (unseen time; D unseen stocks)
- exam (reported, never used for selection): group E (stocks never seen) over the history years
- locked vault: every stock, the tournament's locked months (unseen time; opened once at the end)
- live (forward test): everything after the first live day, scored by the nightly learning

Fitness = (0.5 × t of the daily net return + 0.5 × t of the return above the same-exit random entry) × (0.4 + 0.6 ×
share of positive years), days clustered (one observation per day). News indicators exist only for the last ~12
months (free Finnhub history), so the networks can learn little from them yet.
"""
from __future__ import annotations

import gc
import json
import logging
import math
import random
import time
from datetime import date

import numpy as np

from ..util import now_ts
from . import evolve, hakem, store

log = logging.getLogger("marketdesk.lab")

FEATURES = list(evolve.COND_FEATURES)
INPUTS = FEATURES + ["has_news"]
HIDDEN = 12
TOP_K = 5
POP, MAX_GEN, ELITE, TOURNAMENT = 32, 40, 4, 3
PATIENCE = 10
HALL = 5
NEWCOMERS = 0.05
EXIT_STOPS = [i for i, s in enumerate(evolve.STOP_SET) if s[0] != "none"]
CHUNK = 400_000
NN: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}


# ---------------------------------------------------------------- inputs

def _norm_params(pool, train_idx: np.ndarray, rng: np.random.Generator) -> dict:
    """Robust scaling per input from the training rows only: (x - median) / IQR (sparse ones: x / std of non-zero)."""
    samp = train_idx if len(train_idx) <= 300_000 else np.sort(rng.choice(train_idx, 300_000, replace=False))
    out = {}
    for k in FEATURES:
        x = pool.F[k][samp].astype(np.float32)
        x = x[~np.isnan(x)]
        if k in evolve.SPARSE:
            nz = x[x != 0]
            out[k] = [0.0, float(nz.std()) if len(nz) > 10 and nz.std() > 0 else 1.0]
        elif len(x):
            q1, med, q3 = np.quantile(x, [0.25, 0.5, 0.75])
            out[k] = [float(med), float(q3 - q1) if q3 > q1 else float(x.std() or 1.0)]
        else:
            out[k] = [0.0, 1.0]
    return out


def _matrix(pool, idx: np.ndarray, norm: dict) -> np.ndarray:
    """Scaled input matrix (rows idx), float16 to keep memory low; missing values are 0 (= typical)."""
    X = np.zeros((len(idx), len(INPUTS)), dtype=np.float16)
    for j, k in enumerate(FEATURES):
        med, sc = norm[k]
        x = pool.F[k][idx].astype(np.float32)
        z = np.clip((x - med) / sc, -4, 4)
        X[:, j] = np.nan_to_num(z, nan=0.0)
    n = np.nan_to_num(pool.F["news_n"][idx].astype(np.float32), nan=0.0)
    X[:, -1] = (n > 0).astype(np.float16)
    return X


def scale_row(f: dict, norm: dict) -> np.ndarray:
    """One live feature dict (evo_live.today_rows) -> scaled input vector."""
    v = np.zeros(len(INPUTS), dtype=np.float32)
    for j, k in enumerate(FEATURES):
        x = f.get(k)
        if x is None or (isinstance(x, float) and math.isnan(x)):
            continue
        med, sc = norm[k]
        v[j] = min(4.0, max(-4.0, (float(x) - med) / sc))
    v[-1] = 1.0 if (f.get("news_n") or 0) > 0 else 0.0
    return v


# ---------------------------------------------------------------- networks

def _n_params() -> int:
    d = len(INPUTS)
    return d * HIDDEN + HIDDEN + HIDDEN + 1


def _unpack(w: np.ndarray):
    d = len(INPUTS)
    a = d * HIDDEN
    W1 = w[:a].reshape(d, HIDDEN)
    b1 = w[a:a + HIDDEN]
    W2 = w[a + HIDDEN:a + 2 * HIDDEN]
    b2 = w[-1]
    return W1, b1, W2, b2


def forward(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    W1, b1, W2, b2 = _unpack(w.astype(np.float32))
    out = np.empty(len(X), dtype=np.float32)
    for i in range(0, len(X), CHUNK):
        h = np.tanh(X[i:i + CHUNK].astype(np.float32) @ W1 + b1)
        out[i:i + CHUNK] = h @ W2 + b2
    return out


def random_bot(rng: np.random.Generator) -> dict:
    d = len(INPUTS)
    b2 = float(rng.uniform(-2.2, -1.2))
    w = np.concatenate([rng.normal(0, 1 / math.sqrt(d), d * HIDDEN), np.zeros(HIDDEN),
                        rng.normal(0, 1 / math.sqrt(HIDDEN), HIDDEN), [b2]]).astype(np.float32)
    return {"w": w, "sigma": 0.12, "stop": int(rng.choice(EXIT_STOPS)), "ratio": int(rng.integers(len(evolve.RATIO_SET))),
            "hold": int(rng.integers(len(evolve.HOLD_SET))), "label": "Rastgele keşif ağı"}


def _stop_idx(stop) -> int:
    t = tuple(stop) if isinstance(stop, (list, tuple)) else ("atr", 2.0)
    if t in evolve.STOP_SET and t[0] != "none":
        return evolve.STOP_SET.index(t)
    return evolve.STOP_SET.index(("atr", 2.0))


def _nearest_idx(arr: list | tuple, val: float) -> int:
    return int(min(range(len(arr)), key=lambda i: abs(float(arr[i]) - float(val))))


def rule_to_nn(conds: list, stop, ratio: float, hold: int, norm: dict, rng: np.random.Generator,
               origin: str = "bot-ikizi", label: str = "") -> dict:
    """Translate an existing rule bot into a 32 -> 12 -> 1 neural network twin:
    the first m hidden neurons act as smooth tanh gates for the bot's conditions, while the remaining hidden
    neurons start with small weights across all 32 indicators (including news/events) so evolution can refine them."""
    d = len(INPUTS)
    W1 = rng.normal(0, 0.05, (d, HIDDEN)).astype(np.float32)
    b1 = np.zeros(HIDDEN, dtype=np.float32)
    W2 = rng.normal(0, 0.06, HIDDEN).astype(np.float32)
    valid_conds = [c for c in (conds or []) if c[0] in INPUTS][:HIDDEN - 2]
    m = len(valid_conds)
    gain = 2.6
    for u, c in enumerate(valid_conds):
        feat, op, thr = c[0], c[1], float(c[2])
        j = INPUTS.index(feat)
        med, sc = norm.get(feat, [0.0, 1.0])
        z_thr = max(-3.5, min(3.5, (thr - med) / (sc or 1.0)))
        sgn = 1.0 if op == ">" else -1.0
        W1[:, u] = rng.normal(0, 0.03, d).astype(np.float32)
        W1[j, u] = gain * sgn
        b1[u] = -gain * sgn * z_thr
        W2[u] = 1.0
    b2 = -(max(1, m) - 0.32) if m > 0 else float(rng.uniform(-1.8, -1.2))
    w = np.concatenate([W1.ravel(), b1, W2, [b2]]).astype(np.float32)
    return {
        "w": w,
        "sigma": 0.07,
        "stop": _stop_idx(stop),
        "ratio": _nearest_idx(evolve.RATIO_SET, ratio),
        "hold": _nearest_idx(evolve.HOLD_SET, hold),
        "origin": origin,
        "label": label or origin,
    }


def _archetype_templates() -> list[dict]:
    """Neural counterparts of all core bot families in the system (4 classic bots + Fırsat + Mikro + ATR reversal)."""
    return [
        {"origin": "momentum-nn", "label": "Momentum botu (sinir ağlı ikiz)",
         "conds": [["ret20", ">", 0.08, False], ["dist50", ">", 0.03, False], ["rvol", ">", 1.2, False]],
         "stop": ["atr", 2.0], "ratio": 2.0, "hold": 5},
        {"origin": "ort-donus-nn", "label": "Ortalamaya dönüş botu (sinir ağlı ikiz)",
         "conds": [["rsi2", "<", 0.06, False], ["down_streak", ">", 2.0, False], ["dist20", "<", -0.04, False]],
         "stop": ["atr", 2.0], "ratio": 1.5, "hold": 5},
        {"origin": "bosluk-nn", "label": "Açılış boşluğu botu (sinir ağlı ikiz)",
         "conds": [["gap", "<", -0.012, False], ["rsi14", "<", 0.42, False], ["vix", ">", 0.19, False]],
         "stop": ["atr", 2.0], "ratio": 2.0, "hold": 5},
        {"origin": "olay-haber-nn", "label": "Olay / haber botu (sinir ağlı ikiz)",
         "conds": [["news_sent", ">", 0.15, True], ["rvol", ">", 1.4, False], ["gap", ">", 0.005, False]],
         "stop": ["atr", 1.5], "ratio": 2.0, "hold": 3},
        {"origin": "firsat-nn", "label": "Fırsat botu 100k$ (sinir ağlı ikiz)",
         "conds": [["gap", ">", 0.02, False], ["rvol", ">", 1.5, False], ["dvol", ">", 7.3, False]],
         "stop": ["pct", 1.0], "ratio": 3.0, "hold": 1},
        {"origin": "firsat-genis-nn", "label": "Fırsat geniş-stop botu (sinir ağlı ikiz)",
         "conds": [["gap", ">", 0.02, False], ["rvol", ">", 2.0, False], ["dvol", ">", 7.3, False]],
         "stop": ["pct", 3.0], "ratio": 1.5, "hold": 1},
        {"origin": "mikro-kopus-nn", "label": "Mikro 100$ kopuş botu (sinir ağlı ikiz)",
         "conds": [["atr", ">", 0.045, False], ["gap", ">", 0.02, False], ["rvol", ">", 1.4, False]],
         "stop": ["pct", 2.0], "ratio": 2.0, "hold": 1},
        {"origin": "mikro-donus-nn", "label": "Mikro 100$ geri dönüş botu (sinir ağlı ikiz)",
         "conds": [["atr", ">", 0.057, False], ["rsi2", "<", 0.05, False], ["gap", "<", -0.005, False]],
         "stop": ["atr", 2.0], "ratio": 1.5, "hold": 3},
    ]


def seed_population(norm: dict, rng: np.random.Generator, pop_size: int) -> list[dict]:
    """Build the parallel neural-network population from all existing bots in the system (evrim fear-day bots,
    günlük daily bots, 4 classic referee bots, Fırsat and Mikro bots) plus mutated neural hybrids."""
    out: list[dict] = []
    seen_rules: set[str] = set()
    for t in _archetype_templates():
        if len(out) >= pop_size:
            break
        out.append(rule_to_nn(t["conds"], t["stop"], t["ratio"], t["hold"], norm, rng, t["origin"], t["label"]))
    daily_bots = store.kv_get("evo_daily_finals") or []
    for sv in daily_bots:
        if len(out) >= pop_size or int(sv.get("side", 1)) < 0:
            continue
        rtxt = (sv.get("rule") or {}).get("text") if isinstance(sv.get("rule"), dict) else str(sv.get("rule") or "")
        if rtxt in seen_rules:
            continue
        seen_rules.add(rtxt)
        out.append(rule_to_nn(sv.get("conds_abs") or [], sv.get("stop") or ["atr", 2.0], float(sv.get("ratio") or 1.5),
                              int(sv.get("hold") or 5), norm, rng, "günlük-ikiz", f"Günlük ikiz: {rtxt[:72]}"))
    evo_bots = (store.kv_get("evo_survivors") or []) + (store.kv_get("evo_survivors_base40") or [])
    for sv in evo_bots:
        if len(out) >= max(pop_size - 6, int(pop_size * 0.85)) or int(sv.get("side", 1)) < 0:
            continue
        rtxt = (sv.get("rule") or {}).get("text") if isinstance(sv.get("rule"), dict) else str(sv.get("rule") or "")
        if rtxt in seen_rules:
            continue
        seen_rules.add(rtxt)
        out.append(rule_to_nn(sv.get("conds_abs") or [], sv.get("stop") or ["atr", 2.0], float(sv.get("ratio") or 1.5),
                              int(sv.get("hold") or 5), norm, rng, "evrim-ikiz", f"Evrim ikizi: {rtxt[:72]}"))
    base_len = len(out)
    while len(out) < pop_size:
        if base_len > 0 and rng.random() < 0.7:
            parent = out[int(rng.integers(base_len))]
            c = _mutate(parent, rng)
            c["origin"] = "hibrit-ikiz"
            c["label"] = f"Hibrit ({parent.get('origin', 'ikiz')})"
            out.append(c)
        else:
            out.append(random_bot(rng))
    return out[:pop_size]


def _mutate(b: dict, rng: np.random.Generator) -> dict:
    sigma = float(min(0.5, max(0.01, b["sigma"] * math.exp(0.18 * rng.normal()))))
    w = b["w"].copy()
    m = rng.random(len(w)) < 0.25
    w[m] += rng.normal(0, sigma, int(m.sum())).astype(np.float32)
    w[-1] = min(-0.35, float(w[-1]))      # keep output threshold selective so networks do not buy blindly every day
    c = {"w": w, "sigma": sigma, "stop": b["stop"], "ratio": b["ratio"], "hold": b["hold"],
         "label": b.get("label") or ""}
    if rng.random() < 0.15:
        i = EXIT_STOPS.index(c["stop"])
        c["stop"] = EXIT_STOPS[min(len(EXIT_STOPS) - 1, max(0, i + int(rng.choice((-1, 1)))))]
    if rng.random() < 0.15:
        c["ratio"] = int(min(len(evolve.RATIO_SET) - 1, max(0, c["ratio"] + int(rng.choice((-1, 1))))))
    if rng.random() < 0.1:
        c["hold"] = int(min(len(evolve.HOLD_SET) - 1, max(0, c["hold"] + int(rng.choice((-1, 1))))))
    return c


def _crossover(a: dict, b: dict, rng: np.random.Generator) -> dict:
    """Unit-wise: each hidden unit (its input weights, bias and output weight) comes from one parent."""
    wa, wb = a["w"], b["w"]
    W1a, b1a, W2a, b2a = _unpack(wa)
    W1b, b1b, W2b, b2b = _unpack(wb)
    pick = rng.random(HIDDEN) < 0.5
    W1 = np.where(pick[None, :], W1a, W1b)
    b1 = np.where(pick, b1a, b1b)
    W2 = np.where(pick, W2a, W2b)
    b2 = min(-0.35, float(b2a if rng.random() < 0.5 else b2b))
    src = a if rng.random() < 0.5 else b
    return {"w": np.concatenate([W1.ravel(), b1, W2, [b2]]).astype(np.float32), "sigma": (a["sigma"] + b["sigma"]) / 2,
            "stop": src["stop"], "ratio": src["ratio"], "hold": src["hold"], "label": src.get("label") or ""}


def _genome(b: dict) -> dict:
    return {"conds": [], "side": 1, "stop": b["stop"], "ratio": b["ratio"], "hold": b["hold"]}


def exit_text(b: dict) -> str:
    kind, x = evolve.STOP_SET[b["stop"]]
    r, h = evolve.RATIO_SET[b["ratio"]], evolve.HOLD_SET[b["hold"]]
    f = evolve._fmt
    ex = f"zarar durdur %{f(x, 1)}, kâr al %{f(x * r, 1)}" if kind == "pct" else f"zarar durdur {f(x, 1)}×ATR, kâr al {f(x * r, 1)}×ATR"
    return f"{ex} · en fazla {h} gün"


# ---------------------------------------------------------------- selection and scoring

def top_k_mask(n: int, idx: np.ndarray, days: np.ndarray, score: np.ndarray, k: int = TOP_K) -> np.ndarray:
    """Pool-wide mask of the rows a network buys: per day its k best scores above 0."""
    m = np.zeros(n, dtype=bool)
    pos = score > 0
    if not pos.any():
        return m
    i_, d_, s_ = idx[pos], days[pos], score[pos]
    order = np.lexsort((-s_, d_))
    d_sorted = d_[order]
    start = np.r_[True, d_sorted[1:] != d_sorted[:-1]]
    ar = np.arange(len(order))
    rank = ar - np.maximum.accumulate(np.where(start, ar, 0))
    m[i_[order[rank < k]]] = True
    return m


def _fit(s: dict) -> float:
    n, nd = s.get("n", 0), s.get("days", 0)
    if n < evolve.MIN_TRADES or nd < evolve.MIN_DAYS:
        return -5 + 2.5 * min(1, n / evolve.MIN_TRADES) + 2.5 * min(1, nd / evolve.MIN_DAYS)
    return (0.5 * s.get("t", 0) + 0.5 * s.get("t_excess", 0)) * (0.4 + 0.6 * s.get("pos_years", 0))


class Split:
    def __init__(self, pool, name: str, mask: np.ndarray, norm: dict):
        self.name = name
        self.idx = np.nonzero(mask)[0]
        self.days = pool.dayidx[self.idx]
        self.X = _matrix(pool, self.idx, norm)
        pool.set_mask(name, mask)

    def stats(self, pool, b: dict) -> dict:
        m = top_k_mask(pool.n, self.idx, self.days, forward(b["w"], self.X))
        return evolve.stats(pool, _genome(b), self.name, m)


# ---------------------------------------------------------------- run

def _splits(pool) -> tuple[dict, dict]:
    live_from = store.kv_get("learn_live_from") or pool.last_date
    lock_from = store.kv_get("learn_lock_start") or pool.lock_start
    lock_from = min(lock_from, live_from)
    inner = hakem._months_back(date.fromisoformat(lock_from), evolve.INNER_VAL_MONTHS)
    ud = np.array(pool.udates)
    purge = max(evolve.HOLD_SET)
    i_inner, i_lock, i_live = (int(np.searchsorted(ud, x)) for x in (inner, lock_from, live_from))
    di, g = pool.dayidx, pool.group
    masks = {
        "nn_train": (di < max(0, i_inner - purge)) & (g <= 2),
        "nn_val": (di >= i_inner) & (di < max(i_inner, i_lock - purge)) & (g <= 3),
        "nn_exam": (di < max(0, i_lock - purge)) & (g == 4),
        "nn_lock": (di >= i_lock) & (di < max(i_lock, i_live - purge)),
    }
    ranges = {"train": [pool.first_date, inner], "val": [inner, lock_from], "lock": [lock_from, live_from], "live_from": live_from}
    return masks, ranges


def run(generations: int = MAX_GEN, pop_size: int = POP, seed: int | None = None, progress=None,
        max_symbols: int | None = None) -> dict:
    t0 = time.time()
    store.init()
    seed = int(seed if seed is not None else now_ts() % 100000)
    rng = np.random.default_rng(seed)
    prog = progress or (lambda m, i, n: None)
    prog("10 yıllık havuz hazırlanıyor (birkaç dakika)…", 0, generations)
    pool = evolve.Pool(progress=lambda m, i, n: prog(m, 0, generations), max_symbols=max_symbols)
    try:
        masks, ranges = _splits(pool)
        norm = _norm_params(pool, np.nonzero(masks["nn_train"])[0], rng)
        prog("Sinir ağı girdileri hazırlanıyor…", 0, generations)
        tr = Split(pool, "nn_train", masks["nn_train"], norm)
        va = Split(pool, "nn_val", masks["nn_val"], norm)
        population = [random_bot(rng) for _ in range(pop_size)]
        hall: list[tuple[float, dict, dict]] = []      # (validation fitness, bot, validation stats)
        history, best_val, since, reason = [], -1e9, 0, f"Azami nesil ({generations})"
        evaluated = 0
        for gen in range(generations):
            scored = []
            for b in population:
                if b.get("train") is None:      # elites keep their (deterministic) score
                    b["train"] = tr.stats(pool, b)
                    evaluated += 1
                scored.append((_fit(b["train"]), b))
            scored.sort(key=lambda x: -x[0])
            vbest = -9.0
            for f, b in scored[:5]:
                if b.get("val") is None:
                    b["val"] = va.stats(pool, b)
                vf = _fit(b["val"])
                vbest = max(vbest, vf)
                if b["val"].get("n", 0) >= evolve.MIN_EXAM_TRADES and b["val"].get("days", 0) >= evolve.MIN_EXAM_DAYS:
                    if not any(h[1] is b for h in hall):
                        hall.append((vf, b, b["val"]))
            hall = sorted(hall, key=lambda x: -x[0])[:HALL * 3]
            if vbest > best_val + 0.05:
                best_val, since = vbest, 0
            else:
                since += 1
            fits = [f for f, _ in scored]
            history.append({"gen": gen + 1, "best": round(fits[0], 2), "median": round(float(np.median(fits)), 2),
                            "val_best": round(vbest, 2), "trades_per_day": scored[0][1]["train"].get("trades_per_day")})
            prog(f"Sinir ağı evrimi · nesil {gen + 1}/{generations}: eğitim {fits[0]:.1f} · doğrulama {vbest:.1f} "
                 f"({since}/{PATIENCE} nesildir gelişme yok)", gen + 1, generations)
            if since >= PATIENCE:
                reason = f"Doğrulama {PATIENCE} nesildir gelişmiyor (ezber başlamadan durdu)"
                break
            nxt = [scored[i][1] for i in range(min(ELITE, len(scored)))]

            def pick() -> dict:
                cand = [scored[int(rng.integers(len(scored)))] for _ in range(TOURNAMENT)]
                return max(cand, key=lambda x: x[0])[1]

            while len(nxt) < pop_size:
                if rng.random() < NEWCOMERS:
                    nxt.append(random_bot(rng))
                    continue
                child = _crossover(pick(), pick(), rng) if rng.random() < 0.5 else pick()
                nxt.append(_mutate(child, rng))
            population = nxt
        # finals: best validation bots, then the exams nobody selected on (unseen stocks, locked months)
        prog("Final sınavları: görülmemiş hisseler ve kilitli kasa…", generations, generations)
        del tr, va
        gc.collect()
        ex = Split(pool, "nn_exam", masks["nn_exam"], norm)
        finals = []
        for vf, b, vs in hall[:HALL]:
            finals.append({"b": b, "val": vs, "val_fit": round(vf, 3), "exam": ex.stats(pool, b)})
        del ex
        gc.collect()
        lk = Split(pool, "nn_lock", masks["nn_lock"], norm)
        for f in finals:
            f["lock"] = lk.stats(pool, f["b"])
        del lk
        models = []
        for i, f in enumerate(finals):
            b = f["b"]
            models.append({"id": f"nn{seed}-{i + 1}", "w": [round(float(x), 5) for x in b["w"]], "hidden": HIDDEN, "inputs": INPUTS,
                           "top_k": TOP_K, "norm": norm, "stop": list(evolve.STOP_SET[b["stop"]]),
                           "ratio": evolve.RATIO_SET[b["ratio"]], "hold": evolve.HOLD_SET[b["hold"]],
                           "stop_i": b["stop"], "ratio_i": b["ratio"], "hold_i": b["hold"], "exit": exit_text(b),
                           "train": _short(b.get("train") or {}), "val": _short(f["val"]), "exam": _short(f["exam"]),
                           "lock": _short(f["lock"]), "val_fit": f["val_fit"],
                           "passed": bool(evolve._passes(f["exam"], 0.0))})
        merged = _merge(models, date.fromisoformat(pool.last_date).isoformat())
        report = {"created_at": now_ts(), "seed": seed, "generations": len(history), "population": pop_size, "evaluated": evaluated,
                  "reason": reason, "history": history, "ranges": ranges, "inputs": len(INPUTS), "hidden": HIDDEN, "top_k": TOP_K,
                  "pool": {"rows": int(pool.n), "symbols": int(pool.symbols), "days": int(pool.n_days)},
                  "models": [{k: v for k, v in m.items() if k not in ("w", "norm", "inputs")} for m in models],
                  "merge": merged, "seconds": round(time.time() - t0, 1)}
        store.kv_set("nn_report", report)
        return report
    finally:
        del pool
        gc.collect()


# ---------------------------------------------------------------- walk-forward evolution ("yaşayarak evrim")

WALK_POP = 60
SELECT_EVERY = 1          # every single trading day 1 weak bot is eliminated and 1 new bot is born
ELIM_PER_DAY = 1          # 1 bot eliminated per trading day
AGE_MIN = 20              # a newborn trades 20 days before it can be eliminated (keeps ~40 mature bots in the arena)
WINDOW = 250              # judged on its closed trades of the last ~year (with fallback to last 60 active days for fear-day bots)
ELIM_SHARE = round(ELIM_PER_DAY / WALK_POP, 4)
SCHOOL_DAYS = 120         # initial warm-up (~6 months) before recording the headline champion track record
MIN_JUDGE_DAYS = 5


def _day_stats(rows: list[tuple]) -> dict:
    """rows: (entry_day, ret, excess, year). Daily clustered statistics in the evolve.stats format."""
    if not rows:
        return {"n": 0, "days": 0}
    by: dict[int, list] = {}
    for d, r, e, y in rows:
        x = by.setdefault(d, [0.0, 0.0, 0, y])
        x[0] += r
        x[1] += e
        x[2] += 1
    dr = np.array([v[0] / v[2] for v in by.values()])
    de = np.array([v[1] / v[2] for v in by.values()])
    yrs: dict[int, list] = {}
    for v, r in zip(by.values(), dr):
        yrs.setdefault(int(v[3]), []).append(r)
    years = {y: round(float(np.mean(v)) * 100, 3) for y, v in sorted(yrs.items()) if len(v) >= 3}
    n_days = len(dr)
    allr = np.array([r for _, r, _, _ in rows])

    def t(x):
        return float(x.mean() / (x.std() + 1e-9) * math.sqrt(len(x))) if len(x) > 1 else 0.0
    return {"n": len(rows), "days": n_days, "daily_pct": round(float(dr.mean()) * 100, 3), "avg_pct": round(float(allr.mean()) * 100, 3),
            "t": round(t(dr), 2), "t_excess": round(t(de), 2), "daily_excess_pct": round(float(de.mean()) * 100, 3),
            "win_rate": round(float((allr > 0).mean()) * 100, 1), "years": years,
            "pos_years": round(sum(1 for v in years.values() if v > 0) / len(years), 2) if years else 0,
            "trades_per_day": round(len(rows) / max(1, n_days), 2)}


def _agg_stats(rows: list[tuple]) -> dict:
    """rows: (e_day, en_day, dr, de, cnt, wins, year) pre-aggregated per entry day."""
    if not rows:
        return {"n": 0, "days": 0}
    n_days = len(rows)
    dr = np.fromiter((x[2] for x in rows), dtype=np.float64, count=n_days)
    de = np.fromiter((x[3] for x in rows), dtype=np.float64, count=n_days)
    tot_n = sum(int(x[4]) for x in rows)
    tot_w = sum(int(x[5]) for x in rows)
    sum_r = sum(float(x[2]) * int(x[4]) for x in rows)
    yrs: dict[int, list] = {}
    for x in rows:
        yrs.setdefault(int(x[6]), []).append(float(x[2]))
    years = {y: round(float(np.mean(v)) * 100, 3) for y, v in sorted(yrs.items()) if len(v) >= 3}

    def t(x):
        return float(x.mean() / (x.std() + 1e-9) * math.sqrt(len(x))) if len(x) > 1 else 0.0
    return {"n": tot_n, "days": n_days, "daily_pct": round(float(dr.mean()) * 100, 3),
            "avg_pct": round(sum_r / max(1, tot_n) * 100, 3),
            "t": round(t(dr), 2), "t_excess": round(t(de), 2), "daily_excess_pct": round(float(de.mean()) * 100, 3),
            "win_rate": round(tot_w / max(1, tot_n) * 100, 1), "years": years,
            "pos_years": round(sum(1 for v in years.values() if v > 0) / len(years), 2) if years else 0,
            "trades_per_day": round(tot_n / max(1, n_days), 2)}


def _fast_fit(n: dict, d: int, lo: int) -> float:
    """Fast daily fitness score using only entry days whose trades have completely closed before day d (e_day < d)."""
    closed = [x for x in n["led"] if x[0] < d and x[1] >= lo]
    if len(closed) < MIN_JUDGE_DAYS:
        closed = [x for x in n["life"] if x[0] < d][-60:]
    nd_c = len(closed)
    age = d - n["born"]
    if nd_c < MIN_JUDGE_DAYS:
        if age >= 90 and nd_c == 0:
            return -6.0
        if age >= 60 and nd_c < 3:
            return -4.0 + 0.3 * nd_c
        return -0.4 + 0.05 * nd_c
    dr = np.fromiter((x[2] for x in closed), dtype=np.float64, count=nd_c)
    de = np.fromiter((x[3] for x in closed), dtype=np.float64, count=nd_c)
    mr, me = float(dr.mean()), float(de.mean())
    tr = mr / (float(dr.std()) + 1e-9) * math.sqrt(nd_c)
    te = me / (float(de.std()) + 1e-9) * math.sqrt(nd_c)
    f = 0.45 * tr + 0.55 * te
    if mr < 0:
        f -= 1.5
    if me < 0:
        f -= 1.0
    return float(f)


def build_direct_twins(pool, norm: dict, seed: int = 42) -> list[dict]:
    """Evaluate direct neural-network twins of our existing bots (Momentum, Ortalamaya Dönüş, Açılış Boşluğu,
    Olay/Haber, Fırsat 100k$, Mikro 100$, Günlük, Evrim) so they compete in parallel inside the standing population."""
    rng = np.random.default_rng(seed + 777)
    masks, _ = _splits(pool)
    tr = Split(pool, "nn_train", masks["nn_train"], norm)
    va = Split(pool, "nn_val", masks["nn_val"], norm)
    ex = Split(pool, "nn_exam", masks["nn_exam"], norm)
    lk = Split(pool, "nn_lock", masks["nn_lock"], norm)
    twins = seed_population(norm, rng, 18)
    out = []
    for idx, b in enumerate(twins[:14]):
        s_tr, s_va, s_ex, s_lk = tr.stats(pool, b), va.stats(pool, b), ex.stats(pool, b), lk.stats(pool, b)
        vf = _fit(s_va)
        ok = bool(s_ex.get("days", 0) >= 5 and (s_ex.get("daily_pct", 0) > 0 or s_va.get("daily_pct", 0) > 0))
        out.append({
            "id": f"ikiz-{idx + 1}",
            "w": [round(float(x), 5) for x in b["w"]],
            "hidden": HIDDEN,
            "inputs": INPUTS,
            "top_k": TOP_K,
            "norm": norm,
            "stop": list(evolve.STOP_SET[b["stop"]]),
            "ratio": evolve.RATIO_SET[b["ratio"]],
            "hold": evolve.HOLD_SET[b["hold"]],
            "stop_i": b["stop"],
            "ratio_i": b["ratio"],
            "hold_i": b["hold"],
            "exit": exit_text(b),
            "origin": b.get("origin") or "ikiz",
            "label": b.get("label") or b.get("origin") or "ikiz",
            "train": _short(s_tr),
            "val": _short(s_va),
            "exam": _short(s_ex),
            "lock": _short(s_lk),
            "val_fit": round(vf, 3),
            "passed": ok,
            "alive_since": pool.udates[0],
            "age_days": int(pool.n_days),
        })
    return out


def run_walk(pop_size: int = WALK_POP, seed: int | None = None, progress=None, max_symbols: int | None = None) -> dict:
    """The parallel neural-network population (seeded with neural twins of all our existing bots: Evrim, Günlük,
    Momentum, Ortalamaya Dönüş, Açılış Boşluğu, Olay/Haber, Fırsat, Mikro + hybrids) lives through the 10 years
    day by day. Every trading day, each network evaluates stocks at the open; and every single day (SELECT_EVERY = 1),
    among the networks old enough to be judged on their already-closed trades, the weakest 1 bot is eliminated and
    replaced by 1 mutated/crossed-over child of the winners. Nobody ever sees a trade outcome before it closes."""
    t0 = time.time()
    store.init()
    seed = int(seed if seed is not None else now_ts() % 100000)
    rng = np.random.default_rng(seed)
    prog = progress or (lambda m, i, n: None)
    prog("10 yıllık havuz hazırlanıyor (birkaç dakika)…", 0, 1)
    pool = evolve.Pool(progress=lambda m, i, n: prog(m, 0, 1), max_symbols=max_symbols)
    try:
        nd = pool.n_days
        di, grp = pool.dayidx, pool.group
        # scaling from the initial warm-up period only (no look-ahead)
        norm = _norm_params(pool, np.nonzero((di < max(250, SCHOOL_DAYS)) & (grp <= 3))[0], rng)
        prog("Sinir ağı girdileri ve mevcut botların nöronlu ikizleri hazırlanıyor…", 0, nd)
        order = np.argsort(di, kind="stable")
        X = _matrix(pool, order, norm)                     # rows sorted by day
        day_of = di[order]
        starts = np.searchsorted(day_of, np.arange(nd + 1))
        g_sorted = grp[order]
        # market drift per entry day and holding length (average close / open of every stock): the "random entry"
        drift = {}
        for h in evolve.HOLD_SET:
            c = pool.C[:, h - 1]
            ok = c != evolve.PATH_NAN
            s_ = np.bincount(di[ok], weights=c[ok].astype(np.float64) / 10000, minlength=nd)
            n_ = np.bincount(di[ok], minlength=nd)
            drift[h] = np.where(n_ > 0, s_ / np.maximum(n_, 1), 0.0)
        years = pool.day_year
        H, d_in = HIDDEN, len(INPUTS)
        nets: list[dict] = []
        uid = [0]

        def born(b: dict, day: int, origin: str | None = None, parent=None) -> dict:
            uid[0] += 1
            orig = origin or b.get("origin") or "başlangıç"
            lbl = b.get("label") or orig
            b = dict(b, uid=uid[0], born=day, origin=orig, label=lbl, parent=parent, led=[], life=[], ex_led=[], fit=0.0)
            b.pop("train", None)
            b.pop("val", None)
            return b

        seeded = seed_population(norm, rng, pop_size)
        for sb in seeded:
            nets.append(born(sb, 0, sb.get("origin") or "bot-ikizi"))
        templates = _archetype_templates()
        champion = nets[0] if nets else None
        champ_days: list[tuple] = []          # (day, champion uid)
        champ_rows, champ_ex = [], []
        events, births, deaths = 0, len(nets), 0
        history = []
        W1 = np.stack([_unpack(n["w"])[0] for n in nets], axis=1).reshape(d_in, -1).copy()     # d × (P·H)
        B1 = np.concatenate([_unpack(n["w"])[1] for n in nets]).copy()
        W2 = np.stack([_unpack(n["w"])[2] for n in nets]).copy()                               # P × H
        B2 = np.array([_unpack(n["w"])[3] for n in nets], dtype=np.float32)

        def sync_slot(idx_p: int, net_p: dict) -> None:
            w1, b1, w2, b2 = _unpack(net_p["w"].astype(np.float32))
            W1[:, idx_p * H:(idx_p + 1) * H] = w1
            B1[idx_p * H:(idx_p + 1) * H] = b1
            W2[idx_p, :] = w2
            B2[idx_p] = b2

        for d in range(nd):
            a, b_ = starts[d], starts[d + 1]
            if b_ <= a:
                continue
            Xd = X[a:b_].astype(np.float32)
            rows_d = order[a:b_]
            gd = g_sorted[a:b_]
            S = np.einsum("nph,ph->np", np.tanh(Xd @ W1 + B1).reshape(len(Xd), len(nets), H), W2) + B2
            want: dict[tuple, set] = {}
            picks = []
            for p, n in enumerate(nets):
                for exam in (False, True):
                    sel = np.nonzero((gd == 4) if exam else (gd <= 3))[0]
                    s = S[sel, p]
                    pos = sel[s > 0]
                    if not len(pos):
                        continue
                    top = pos[np.argsort(-S[pos, p])[:TOP_K]]
                    key = (n["stop"], n["ratio"], n["hold"])
                    want.setdefault(key, set()).update(rows_d[top].tolist())
                    picks.append((p, exam, key, rows_d[top]))
            res_map: dict = {}
            for key, rs in want.items():
                idx = np.array(sorted(rs))
                r, ex_j, _m = pool.outcome(idx, 1, *key, detail=True)
                for i, rr, ej in zip(idx.tolist(), r.tolist(), ex_j.tolist()):
                    res_map[(key, i)] = (rr, ej)
            y = int(years[d])
            for p, exam, key, rws in picks:
                n = nets[p]
                dh = float(drift[evolve.HOLD_SET[n["hold"]]][d])
                valid = []
                for i in rws.tolist():
                    rr, ej = res_map[(key, i)]
                    if rr == rr:          # non-NaN
                        valid.append((float(rr), int(ej)))
                if not valid:
                    continue
                cnt = len(valid)
                dr = sum(rr for rr, _ in valid) / cnt
                de = dr - dh
                wins = sum(1 for rr, _ in valid if rr > 0)
                e_day = d + max(0, max(ej for _, ej in valid))
                agg = (e_day, d, dr, de, cnt, wins, y)
                if exam:
                    n["ex_led"].append(agg)
                else:
                    n["led"].append(agg)
                    n["life"].append(agg)
                if n is champion:         # the record: picks of today's champion (chosen from past closed trades only)
                    dst = champ_ex if exam else champ_rows
                    for rr, _ in valid:
                        dst.append((d, rr, rr - dh, y))
            if champion is not None:
                champ_days.append((d, champion["uid"]))
            # daily elimination (SELECT_EVERY = 1): judged strictly on trades closed before today (e_day < d)
            if d >= AGE_MIN and d % SELECT_EVERY == 0:
                lo = d - WINDOW
                judged_idx = []
                for idx_p, n in enumerate(nets):
                    if d - n["born"] < AGE_MIN:
                        continue
                    n["fit"] = _fast_fit(n, d, lo)
                    judged_idx.append(idx_p)
                if judged_idx:
                    events += 1
                    judged_idx.sort(key=lambda idx_p: -nets[idx_p]["fit"])
                    champion = nets[judged_idx[0]]
                    top_n = max(2, len(judged_idx) // 5)
                    parents = [nets[i] for i in judged_idx[:top_n] if nets[i]["fit"] > -1.0] or [nets[i] for i in judged_idx[:3]]
                    # eliminate the weakest ELIM_PER_DAY (1) bot every single day
                    for dead_i in judged_idx[-ELIM_PER_DAY:]:
                        if nets[dead_i] is champion and len(judged_idx) > 1:
                            continue
                        deaths += 1
                        r_draw = rng.random()
                        if r_draw < 0.04 and templates:
                            t = templates[int(rng.integers(len(templates)))]
                            raw = _mutate(rule_to_nn(t["conds"], t["stop"], t["ratio"], t["hold"], norm, rng, t["origin"], t["label"]), rng)
                            child = born(raw, d, t["origin"], None)
                        elif r_draw < 0.07:
                            child = born(random_bot(rng), d, "rastgele", None)
                        else:
                            pa = parents[int(rng.integers(len(parents)))]
                            pb = parents[int(rng.integers(len(parents)))]
                            base = _crossover(pa, pb, rng) if rng.random() < 0.5 and pa is not pb else pa
                            mut = _mutate(base, rng)
                            root_lbl = str(pa.get("label") or pa.get("origin") or "ağ")
                            while root_lbl.startswith("Klon ← "):
                                root_lbl = root_lbl[len("Klon ← "):]
                            lbl = f"Klon ← {root_lbl}"[:76]
                            mut["label"] = lbl
                            child = born(mut, d, f"klon ({pa.get('origin') or 'evrim'})"[:32], pa["uid"])
                        nets[dead_i] = child
                        sync_slot(dead_i, child)
                        births += 1
                    if d % 20 == 0:
                        for n in nets:
                            if len(n["led"]) > 300:
                                n["led"] = [x for x in n["led"] if x[1] >= lo - 20]
                if events > 0 and events % 100 == 0:
                    cs = _day_stats([x for x in champ_rows if x[0] >= SCHOOL_DAYS])
                    history.append({"day": pool.udates[d], "champion_fit": round(champion["fit"], 2) if champion else None,
                                    "record_daily_pct": cs.get("daily_pct"), "births": births, "deaths": deaths})
            if d % 40 == 0 or d == nd - 1:
                cs = _day_stats([x for x in champ_rows if x[0] >= SCHOOL_DAYS])
                prog(f"Gün gün sinir ağı evrimi · {pool.udates[d]} ({d + 1}/{nd} gün) · her gün 1 eleme (toplam {deaths} elendi, {births} doğdu) · "
                     f"şampiyon sicili: günlük {cs.get('daily_pct', 0) or 0:+.3f}% (piyasa üstü {cs.get('daily_excess_pct', 0) or 0:+.3f}%)", d + 1, nd)
        # final population: networks judged on their closed trades in the last year (or last 60 active days for fear-day bots)
        prog("Son popülasyon değerlendiriliyor…", nd, nd)
        lo = nd - WINDOW
        finals = []
        for n in nets:
            life = n["life"]
            last = [x for x in n["led"] if x[1] >= lo]
            if len(last) < 10:
                last = life[-60:]
            ex_life = n["ex_led"]
            st_life, st_last, st_ex = _agg_stats(life), _agg_stats(last), _agg_stats(ex_life)
            f = _fast_fit(n, nd, lo)
            finals.append((f, n, st_life, st_last, st_ex))
        finals.sort(key=lambda x: -x[0])
        models = []
        for i, (f, n, st_life, st_last, st_ex) in enumerate(finals[:max(HALL, 10)]):
            ok = (nd - n["born"] >= 20 and st_life.get("days", 0) >= 8 and st_last.get("days", 0) >= 5
                  and st_last.get("daily_pct", -1) > 0 and st_last.get("daily_excess_pct", -1) > 0
                  and st_ex.get("days", 0) >= 5 and st_ex.get("daily_pct", -1) > 0)
            models.append({"id": f"yw{seed}-{i + 1}", "w": [round(float(x), 5) for x in n["w"]], "hidden": HIDDEN, "inputs": INPUTS,
                           "top_k": TOP_K, "norm": norm, "stop": list(evolve.STOP_SET[n["stop"]]),
                           "ratio": evolve.RATIO_SET[n["ratio"]], "hold": evolve.HOLD_SET[n["hold"]],
                           "stop_i": n["stop"], "ratio_i": n["ratio"], "hold_i": n["hold"], "exit": exit_text(n),
                           "origin": n.get("origin") or "evrim", "label": n.get("label") or "",
                           "train": _short(st_life), "val": _short(st_last), "exam": _short(st_ex), "lock": {},
                           "val_fit": round(f, 3), "passed": bool(ok), "alive_since": pool.udates[n["born"]],
                           "age_days": int(nd - n["born"])})
        models.extend(build_direct_twins(pool, norm, seed=seed))
        rec = _day_stats([x for x in champ_rows if x[0] >= SCHOOL_DAYS])
        rec_ex = _day_stats([x for x in champ_ex if x[0] >= SCHOOL_DAYS])
        school = _day_stats([x for x in champ_rows if x[0] < SCHOOL_DAYS])
        # cumulative record (sum of daily average trade returns) vs the same days' market drift
        by: dict[int, list] = {}
        for dd, rr, xx, _y in champ_rows:
            if dd >= SCHOOL_DAYS:
                v = by.setdefault(dd, [0.0, 0.0, 0])
                v[0] += rr
                v[1] += rr - xx
                v[2] += 1
        curve, cum, cum_m = [], 0.0, 0.0
        for dd in sorted(by):
            v = by[dd]
            cum += v[0] / v[2]
            cum_m += v[1] / v[2]
            curve.append([pool.udates[dd], round(cum * 100, 2), round(cum_m * 100, 2)])
        if len(curve) > 300:
            step = len(curve) / 300
            curve = [curve[int(i * step)] for i in range(300)] + [curve[-1]]
        merged = _merge(models, pool.last_date)
        report = {"created_at": now_ts(), "mode": "walk", "seed": seed, "population": pop_size, "days": int(nd),
                  "range": [pool.udates[0], pool.last_date], "school_until": pool.udates[min(nd - 1, SCHOOL_DAYS)],
                  "births": births, "deaths": deaths, "events": events, "record": rec, "record_unseen": rec_ex,
                  "school": school, "curve": curve, "history": history,
                  "rules": {"select_every": SELECT_EVERY, "elim_per_day": ELIM_PER_DAY, "age_min": AGE_MIN,
                            "window": WINDOW, "elim_share": ELIM_SHARE, "top_k": TOP_K},
                  "pool": {"rows": int(pool.n), "symbols": int(pool.symbols), "days": int(pool.n_days)},
                  "models": [{k: v for k, v in m.items() if k not in ("w", "norm", "inputs")} for m in models],
                  "merge": merged, "seconds": round(time.time() - t0, 1),
                  # kept for the UI table of the batch mode
                  "generations": events, "evaluated": births, "reason": "10 yıl gün gün yaşandı (her gün 1 eleme)",
                  "ranges": {"train": [pool.udates[0], pool.last_date], "val": ["son 1 yıl", ""], "lock": ["", ""], "live_from": store.kv_get("learn_live_from")}}
        store.kv_set("nn_report", report)
        return report
    finally:
        del pool
        gc.collect()


def _short(s: dict) -> dict:
    return {k: s.get(k) for k in ("n", "days", "daily_pct", "avg_pct", "t", "t_excess", "pos_years", "win_rate",
                                  "trades_per_day", "base_pct", "daily_excess_pct", "years")}


def last_report() -> dict | None:
    return store.kv_get("nn_report")


# ---------------------------------------------------------------- the standing population (elimination)

POP_KEEP = 24            # networks kept trading in the shadow population
POP_MIN = 5              # never retire below this many
LIVE_N0 = 20             # live days the prior counts as (like the rule-bot lineage)
PRIOR_HAIRCUT = 0.5      # only half of the exam result counts as expectation
CLONE_PARENTS, CLONE_SIGMA = 3, 0.05
CLONE_FLOOR = 0.95       # a clone keeps at least 95 % of its parent's validation fitness


def _prior(m: dict) -> float:
    """Expected daily net return before live evidence: half of the unseen-stock exam result."""
    return PRIOR_HAIRCUT * float((m.get("exam") or {}).get("daily_pct") or 0) / 100


def _score(m: dict) -> float:
    lv = m.get("live") or {}
    nd = int(lv.get("days") or 0)
    return round((LIVE_N0 * _prior(m) + float(lv.get("sum") or 0)) / (LIVE_N0 + nd) * 100, 4)


def _strength(m: dict) -> tuple:
    """Ranking without live evidence: passed the exam first, then the exam's daily return above random entry."""
    ex = m.get("exam") or {}
    return (1 if m.get("passed") else 0, float(ex.get("daily_excess_pct") or -9), float(ex.get("t") or -9))


def population() -> list[dict]:
    return store.kv_get("nn_models") or []


def _merge(new: list[dict], day: str) -> dict:
    """A finished run: its finals that passed the unseen-stock exam join the population; the weakest leave when it is
    over POP_KEEP (live score once there is live evidence, else exam strength)."""
    pop = population()
    known = {m["id"] for m in pop}
    added, failed = 0, 0
    for m in new:
        if m["id"] in known:
            continue
        m.update(status="aktif" if m.get("passed") else "elendi", born=day,
                 origin=m.get("origin") or "evrim çalıştırması",
                 label=m.get("label") or m.get("origin") or "evrim çalıştırması",
                 why=None if m.get("passed") else "görülmemiş hisse sınavını geçemedi")
        m["score"] = _score(m)
        pop.append(m)
        added += bool(m.get("passed"))
        failed += not m.get("passed")
    retired = _trim(pop, "daha güçlü yeni ağlar geldi")
    store.kv_set("nn_models", pop[-200:])
    return {"added": added, "failed": failed, "retired": retired, "active": sum(1 for m in pop if m["status"] == "aktif")}


def _trim(pop: list[dict], why: str) -> int:
    active = [m for m in pop if m["status"] == "aktif"]
    if len(active) <= POP_KEEP:
        return 0
    def key(m):
        lv = m.get("live") or {}
        return (1 if (lv.get("days") or 0) >= 5 else 0, m.get("score") or 0, _strength(m))
    out = sorted(active, key=key)[:len(active) - POP_KEEP]
    for m in out:
        m["status"], m["why"] = "emekli", why
    return len(out)


def nightly(pool, live_mask: np.ndarray, live_stats_fn, min_days: int = 3, min_results: int = 30, seed: int = 0) -> dict | None:
    """After the close (inside the nightly learning, on its pool): live record of every active network, retirement of
    the ones live evidence says lose money, micro-mutated clones of the best that must pass the exams again."""
    pop = population()
    active = [m for m in pop if m["status"] == "aktif" and m.get("inputs") == INPUTS and m.get("hidden") == HIDDEN]
    if not active:
        return None
    rng = np.random.default_rng(seed)
    changes: list[dict] = []
    idx = np.nonzero(live_mask)[0]
    days = pool.dayidx[idx]
    xcache: dict = {}

    def X_for(m, key, rows):
        k = (key, json.dumps(m["norm"], sort_keys=True))
        if k not in xcache:
            xcache[k] = _matrix(pool, rows, m["norm"])
        return xcache[k]

    def bot(m):
        return {"side": 1, "stop": m["stop"], "ratio": m["ratio"], "hold": m["hold"]}

    for m in active:
        if len(idx):
            mask = top_k_mask(pool.n, idx, days, forward(np.array(m["w"], dtype=np.float32), X_for(m, "live", idx)),
                              int(m.get("top_k") or TOP_K))
            m["live"] = live_stats_fn(pool, bot(m), mask, live_mask)
        m["score"] = _score(m)
    # retire: enough live evidence and an expected return below zero
    for m in sorted(active, key=lambda x: x["score"]):
        if sum(1 for x in active if x["status"] == "aktif") <= POP_MIN:
            break
        lv = m.get("live") or {}
        if (lv.get("n") or 0) >= min_results and (lv.get("days") or 0) >= min_days and m["score"] < 0:
            m["status"], m["why"] = "emekli", "canlı sonuç zararda"
            changes.append({"kind": "emekli", "id": m["id"], "score": m["score"]})
    # clone: the best networks get a slightly mutated child that must pass the unseen-stock exam and keep the
    # parent's validation fitness
    xcache.clear()
    masks, _ranges = _splits(pool)
    for name in ("nn_val", "nn_exam"):
        pool.set_mask(name, masks[name])
    vidx, eidx = np.nonzero(masks["nn_val"])[0], np.nonzero(masks["nn_exam"])[0]
    parents = sorted([m for m in active if m["status"] == "aktif"], key=lambda x: -x["score"])[:CLONE_PARENTS]
    known = {m["id"] for m in pop}
    for p in parents:
        w = np.array(p["w"], dtype=np.float32)
        child_w = w + rng.normal(0, CLONE_SIGMA, len(w)).astype(np.float32) * (rng.random(len(w)) < 0.3)
        b = {"w": child_w, "stop": p["stop_i"], "ratio": p["ratio_i"], "hold": p["hold_i"]}
        vX, eX = X_for(p, "val", vidx), X_for(p, "exam", eidx)
        vs = evolve.stats(pool, _genome(b), "nn_val", top_k_mask(pool.n, vidx, pool.dayidx[vidx], forward(child_w, vX)))
        es = evolve.stats(pool, _genome(b), "nn_exam", top_k_mask(pool.n, eidx, pool.dayidx[eidx], forward(child_w, eX)))
        vf = _fit(vs)
        ok = evolve._passes(es, 0.0) and vf >= CLONE_FLOOR * float(p.get("val_fit") or 0)
        cid = f"{p['id']}k{int(rng.integers(1000, 9999))}"
        changes.append({"kind": "klon", "id": cid, "parent": p["id"], "passed": bool(ok), "val_fit": round(vf, 3)})
        if not ok or cid in known:
            continue
        c = {k: v for k, v in p.items() if k not in ("w", "live", "score", "status", "why", "val", "exam", "val_fit", "born",
                                                      "origin", "parent", "id")}
        c.update(id=cid, w=[round(float(x), 5) for x in child_w], val=_short(vs), exam=_short(es), val_fit=round(vf, 3),
                 passed=True, status="aktif", origin="klon", parent=p["id"], born=pool.last_date, live={})
        c["score"] = _score(c)
        pop.append(c)
        known.add(cid)
    retired = _trim(pop, "popülasyon dolu: daha zayıf")
    if retired:
        changes.append({"kind": "emekli", "count": retired, "why": "popülasyon dolu"})
    store.kv_set("nn_models", pop[-200:])
    out = {"ts": now_ts(), "changes": changes, "active": sum(1 for m in pop if m["status"] == "aktif")}
    store.kv_set("nn_night", out)
    return out


def population_summary() -> list[dict]:
    order = {"aktif": 0, "emekli": 1, "elendi": 2}
    rows = [{k: v for k, v in m.items() if k not in ("w", "norm", "inputs")} for m in population()]
    return sorted(rows, key=lambda m: (order.get(m.get("status"), 9), -(m.get("score") or 0)))[:40]


def score_today(rows: list[dict]) -> list[dict]:
    """Today's picks of every saved network from evo_live.today_rows() (read-only)."""
    models = [m for m in population() if m.get("status") == "aktif"]
    out = []
    if not models or not rows:
        return out
    for m in models:
        if m.get("inputs") != INPUTS:
            continue
        X = np.stack([scale_row(r["f"], m["norm"]) for r in rows])
        s = forward(np.array(m["w"], dtype=np.float32), X)
        order = [i for i in np.argsort(-s) if s[i] > 0][: int(m.get("top_k") or TOP_K)]
        out.append({"id": m["id"], "exit": m["exit"], "picks": [{"symbol": rows[i]["symbol"], "score": round(float(s[i]), 3),
                                                                  "open": rows[i]["open"]} for i in order]})
    return out


def describe_model(m: dict) -> str:
    return json.dumps({k: m.get(k) for k in ("id", "exit", "val", "exam", "lock")}, ensure_ascii=False)
