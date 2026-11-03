"""Derin sinir ağı: a gradient-trained neural network (backpropagation, Adam) next to the evolved ones.

Network: 32 inputs (31 indicators + news flag, robust-scaled) -> 64 -> 32 -> 16 ReLU units -> 1 output, about
5,000 weights. Target: the net return of buying the stock at the open with the lab's common exit (stop 2 x ATR,
target 1.5 x stop, at most 5 days, liquidity-based costs), clipped to +-15 %. Each day the network buys the 5 stocks
it scores highest (only those with a positive predicted return).

Walk-forward, no look-ahead: for every test year the network is trained again from scratch on the days before that
year only (5 days purged so no training trade ends inside the test year), stock groups A-D; early stopping on the
last 15 % of the training days (time order), judged by what matters (the daily top-5 return). The test year is then
scored on every stock; group E (never trained on) is reported apart. The final network, trained on all days, scores
the live market.

Honest limits: the signal in daily stock data is weak and noisy, so a bigger network mostly learns noise; the
early stopping and the out-of-sample years are what show whether anything real was learned.
"""
from __future__ import annotations

import gc
import logging
import math
import pickle
import time

import numpy as np

from ..util import DATA_DIR, now_ts
from . import evolve, neuro, store

log = logging.getLogger("marketdesk.lab")

HIDDEN = (64, 32, 16)
EXIT = (evolve.STOP_SET.index(("atr", 2.0)), evolve.RATIO_SET.index(1.5), evolve.HOLD_SET.index(5))
TRAIN_ROWS = 400_000
VAL_SHARE = 0.15
MAX_EPOCHS, PATIENCE = 25, 4
TOP_K = 5
PURGE_DAYS = 5
CLIP = 15.0                      # target clip in %
MODEL_PATH = DATA_DIR / "deepnet.pkl"
DEEP: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}


def _top5(idx: np.ndarray, days: np.ndarray, score: np.ndarray) -> np.ndarray:
    """Positions (into idx) of each day's best TOP_K rows with a positive score."""
    order = np.lexsort((-score, days))
    d = days[order]
    first = np.r_[True, d[1:] != d[:-1]]
    start = np.maximum.accumulate(np.where(first, np.arange(len(d)), 0))
    keep = ((np.arange(len(d)) - start) < TOP_K) & (score[order] > 0)
    return order[keep]


def _day_stats(days: np.ndarray, r: np.ndarray, base: np.ndarray, years: np.ndarray) -> dict:
    """Daily clustered statistics; base = same day's average return of every stock with the same exit."""
    if not len(r):
        return {"n": 0, "days": 0}
    ud, inv = np.unique(days, return_inverse=True)
    cnt = np.bincount(inv)
    dr = np.bincount(inv, weights=r) / cnt
    de = dr - base[ud]
    yrs: dict = {}
    for y, v in zip(years[ud].tolist(), dr.tolist()):
        yrs.setdefault(int(y), []).append(v)
    ys = {y: round(float(np.mean(v)) * 100, 3) for y, v in sorted(yrs.items()) if len(v) >= 5}

    def t(x):
        return float(x.mean() / (x.std() + 1e-9) * math.sqrt(len(x))) if len(x) > 1 else 0.0
    return {"n": int(len(r)), "days": int(len(ud)), "daily_pct": round(float(dr.mean()) * 100, 3),
            "daily_excess_pct": round(float(de.mean()) * 100, 3), "avg_pct": round(float(r.mean()) * 100, 3),
            "win_rate": round(float((r > 0).mean()) * 100, 1), "pos_days": round(float((dr > 0).mean()) * 100, 1),
            "t": round(t(dr), 2), "t_excess": round(t(de), 2), "years": ys}


def _new_model(seed: int):
    from sklearn.neural_network import MLPRegressor
    return MLPRegressor(hidden_layer_sizes=HIDDEN, activation="relu", solver="adam", alpha=1e-4, batch_size=1024,
                        learning_rate_init=1e-3, random_state=seed)


def _fit(pool, tr_idx: np.ndarray, res: np.ndarray, norm: dict, base: np.ndarray, rng, seed: int, progress=None, label: str = ""):
    """Train with early stopping on the last VAL_SHARE of the training days (time order). Returns (model, val stats)."""
    days = pool.dayidx[tr_idx]
    cut = np.quantile(days, 1 - VAL_SHARE)
    fit_idx, val_idx = tr_idx[days < cut - PURGE_DAYS], tr_idx[days >= cut]
    if len(fit_idx) > TRAIN_ROWS:
        fit_idx = np.sort(rng.choice(fit_idx, TRAIN_ROWS, replace=False))
    X = neuro._matrix(pool, fit_idx, norm).astype(np.float32)
    y = np.clip(res[fit_idx] * 100, -CLIP, CLIP)
    Xv = neuro._matrix(pool, val_idx, norm).astype(np.float32)
    dv = pool.dayidx[val_idx]
    model = _new_model(seed)
    best, best_state, since = -1e9, None, 0
    for ep in range(MAX_EPOCHS):
        perm = rng.permutation(len(X))
        model.partial_fit(X[perm], y[perm])
        sel = _top5(val_idx, dv, model.predict(Xv))
        st = _day_stats(dv[sel], res[val_idx][sel], base, pool.day_year)
        score = st.get("daily_excess_pct", -9) if st.get("days", 0) >= 20 else -9
        if progress:
            progress(f"{label} · tur {ep + 1}: doğrulama günlük {st.get('daily_pct', 0):+.3f}% (piyasa üstü {st.get('daily_excess_pct', 0):+.3f}%)")
        if score > best + 1e-4:
            best, since = score, 0
            best_state = ([w.copy() for w in model.coefs_], [b.copy() for b in model.intercepts_], st)
        else:
            since += 1
            if since >= PATIENCE:
                break
    if best_state:
        model.coefs_, model.intercepts_ = best_state[0], best_state[1]
    return model, (best_state[2] if best_state else {})


def run(seed: int | None = None, progress=None, max_symbols: int | None = None) -> dict:
    t0 = time.time()
    store.init()
    seed = int(seed if seed is not None else now_ts() % 100000)
    rng = np.random.default_rng(seed)
    prog = progress or (lambda m: None)
    prog("10 yıllık havuz hazırlanıyor (birkaç dakika)…")
    pool = evolve.Pool(progress=lambda m, i, n: prog(m), max_symbols=max_symbols)
    try:
        prog("Hedef getiriler hesaplanıyor (2×ATR zarar durdur, 1,5 kat kâr al, en fazla 5 gün)…")
        res = pool.outcome(np.arange(pool.n), 1, *EXIT).astype(np.float64)
        ok = ~np.isnan(res)
        di, yr = pool.dayidx, pool.day_year
        s_ = np.bincount(di[ok], weights=res[ok], minlength=pool.n_days)
        n_ = np.bincount(di[ok], minlength=pool.n_days)
        base = np.where(n_ > 0, s_ / np.maximum(n_, 1), 0.0)
        years_all = sorted(set(yr.tolist()))
        test_years = [y for y in years_all if y >= years_all[0] + 2]
        norm = neuro._norm_params(pool, np.nonzero(ok & (pool.group <= 3) & (yr[di] <= years_all[0] + 1))[0], rng)
        per_year, all_d, all_r, ex_d, ex_r = [], [], [], [], []
        for k, Y in enumerate(test_years):
            y0 = int(np.searchsorted(yr, Y))                       # first day index of year Y
            tr_idx = np.nonzero(ok & (pool.group <= 3) & (di < y0 - PURGE_DAYS))[0]
            te_idx = np.nonzero(ok & (yr[di] == Y))[0]
            if len(tr_idx) < 100_000 or not len(te_idx):
                continue
            model, val = _fit(pool, tr_idx, res, norm, base, rng, seed + k, progress=prog, label=f"{Y} için eğitim ({k + 1}/{len(test_years)})")
            sc = np.zeros(len(te_idx), np.float32)
            for a in range(0, len(te_idx), 200_000):
                part = te_idx[a:a + 200_000]
                sc[a:a + 200_000] = model.predict(neuro._matrix(pool, part, norm).astype(np.float32))
            for exam in (False, True):
                m = (pool.group[te_idx] == 4) if exam else np.ones(len(te_idx), bool)
                sub, ssc = te_idx[m], sc[m]
                sel = _top5(sub, di[sub], ssc)
                if exam:
                    ex_d.append(di[sub][sel]); ex_r.append(res[sub][sel])
                else:
                    all_d.append(di[sub][sel]); all_r.append(res[sub][sel])
                    st = _day_stats(di[sub][sel], res[sub][sel], base, yr)
            per_year.append({"year": Y, "test": st, "val": val, "train_rows": int(len(tr_idx))})
            prog(f"{Y}: görülmemiş yılda günlük {st.get('daily_pct', 0):+.3f}% (piyasa üstü {st.get('daily_excess_pct', 0):+.3f}%), "
                 f"{st.get('n', 0)} işlem")
        oos = _day_stats(np.concatenate(all_d), np.concatenate(all_r), base, yr) if all_d else {}
        oos_ex = _day_stats(np.concatenate(ex_d), np.concatenate(ex_r), base, yr) if ex_d else {}
        prog("Canlı için son ağ bütün günlerle eğitiliyor…")
        final_idx = np.nonzero(ok & (pool.group <= 3))[0]
        model, val = _fit(pool, final_idx, res, norm, base, rng, seed + 99, progress=prog, label="Son ağ")
        with open(MODEL_PATH, "wb") as fh:
            pickle.dump({"model": model, "norm": norm, "trained_at": now_ts(), "data_to": pool.last_date, "exit": EXIT}, fh)
        report = {"created_at": now_ts(), "seed": seed, "hidden": list(HIDDEN), "weights": int(sum(w.size for w in model.coefs_)
                  + sum(b.size for b in model.intercepts_)), "exit": "zarar durdur 2×ATR, kâr al 3×ATR, en fazla 5 gün",
                  "years": per_year, "oos": oos, "oos_unseen_stocks": oos_ex, "final_val": val,
                  "range": [pool.first_date, pool.last_date], "seconds": round(time.time() - t0, 1)}
        store.kv_set("deep_report", report)
        return report
    finally:
        del pool
        gc.collect()


_cache: dict = {"mtime": None, "obj": None}


def _load():
    try:
        mt = MODEL_PATH.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] != mt:
        with open(MODEL_PATH, "rb") as fh:
            _cache["obj"], _cache["mtime"] = pickle.load(fh), mt
    return _cache["obj"]


def score_today(rows: list[dict]) -> list[dict]:
    """Today's top picks of the final network (rows from evo_live.today_rows)."""
    obj = _load()
    if not obj or not rows:
        return []
    X = np.stack([neuro.scale_row(r["f"], obj["norm"]) for r in rows])
    sc = obj["model"].predict(X)
    order = np.argsort(-sc)[:TOP_K]
    return [{"symbol": rows[i]["symbol"], "open": rows[i]["open"], "pred_pct": round(float(sc[i]), 3)} for i in order if sc[i] > 0]


def last_report() -> dict | None:
    return store.kv_get("deep_report")
