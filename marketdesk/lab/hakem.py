"""The referee ("hakem"): decides from the four bots' votes, and the honest test of everything.

Two referees:
  weighted vote   each bot's vote x confidence x a weight learned from its record in the training years
                  (bots that lost money get weight 0); trade when the weighted score clears a threshold
  learning model  logistic regression (scikit-learn): "given the four votes and the market context, does the trade
                  in the bots' consensus direction make money?" Trade when the probability clears a threshold
                  chosen on the last part of the training years (never on the test year).

Test: everything is fitted on the data before the test window (default the last 2 months) and judged on that window.
A walk-forward check repeats this over the last 12 months in windows of the same length, each judged by referees fitted
only on the data before it, so the months with Finnhub headlines are learned from as well. A portfolio simulation takes
at most 5 trades a day (20 % of the account each) and is compared with simply holding SPY. The model used live is
refitted on all data afterwards.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

from ..util import now_ts
from . import bots, store

log = logging.getLogger("marketdesk.lab")

MAX_TRADES_PER_DAY = 5
SLOT = 0.2
MIN_TRAIN_TRADES = 300

SIGNED = ("gap", "ret1", "dist20", "dist50", "dist200", "spy_ret1", "spy_gap", "surprise", "filing_score", "news_score",
          "rsi2", "rsi14")
UNSIGNED = ("rvol", "atr", "spy_up", "vix", "vix_chg", "earn", "offering", "form4", "news_n", "news_era", "young")
FEATURE_TR = {
    "vote_momentum": "Momentum botu aynı yönde", "vote_meanrev": "Ortalamaya dönüş botu aynı yönde",
    "vote_gap": "Açılış boşluğu botu aynı yönde", "vote_event": "Olay / haber botu aynı yönde",
    "agree": "Aynı yönde oy veren bot sayısı", "disagree": "Ters yönde oy veren bot sayısı", "dir": "Yön: yükseliş (+) / düşüş (−)",
    "gap": "Açılış boşluğu (işlem yönünde)", "ret1": "Dünkü getiri (işlem yönünde)",
    "dist20": "20 günlük ortalamaya uzaklık (yönde)", "dist50": "50 günlük ortalamaya uzaklık (yönde)",
    "dist200": "200 günlük ortalamaya uzaklık (yönde)", "spy_ret1": "SPY dünkü getiri (yönde)",
    "spy_gap": "SPY açılış boşluğu (yönde)", "surprise": "EPS sürprizi (yönde)", "filing_score": "8-K olay puanı (yönde)",
    "news_score": "Gece haberlerinin tonu (yönde)", "rsi2": "RSI(2) (yönde)", "rsi14": "RSI(14) (yönde)",
    "rvol": "Dünkü göreli hacim", "atr": "Oynaklık (ATR %)", "spy_up": "SPY 200 günlük ortalamanın üstünde",
    "vix": "VIX seviyesi", "vix_chg": "VIX dünkü değişim", "earn": "Bilanço günü", "offering": "Hisse arzı bildirimi",
    "form4": "Son 5 gün içeriden işlem bildirimi", "news_n": "Gece haber sayısı", "news_era": "Haber başlığı verisi var",
    "young": "İlk işlem yılında (yeni halka arz)",
}
METHOD_TR = {"momentum": "Momentum / kırılım botu", "meanrev": "Ortalamaya dönüş botu", "gap": "Açılış boşluğu botu",
             "event": "Olay / haber botu", "majority": "Basit oy çokluğu (4 bot)", "weighted": "Hakem: ağırlıklı oy",
             "model": "Hakem: öğrenen model"}


# ---------------------------------------------------------------- metrics

def metrics(r: np.ndarray) -> dict:
    r = r[~np.isnan(r)]
    n = len(r)
    if not n:
        return {"n": 0}
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": int(n), "win_rate": round(float((r > 0).mean() * 100), 1), "avg_pct": round(float(r.mean() * 100), 3),
            "median_pct": round(float(np.median(r) * 100), 3), "sum_pct": round(float(r.sum() * 100), 1),
            "profit_factor": round(float(pos / neg), 2) if neg > 0 else None}


def _curve(dates: list[str], rets: list[float], points: int = 260) -> tuple[list, dict]:
    eq, peak, mdd = 1.0, 1.0, 0.0
    series = []
    for d, x in zip(dates, rets):
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        series.append([d, round(eq, 4)])
    arr = np.array(rets) if rets else np.zeros(1)
    sharpe = float(arr.mean() / arr.std() * np.sqrt(252)) if arr.std() > 0 else None
    if len(series) > points:
        step = len(series) / points
        series = [series[int(i * step)] for i in range(points)] + [series[-1]]
    return series, {"total_pct": round((eq - 1) * 100, 2), "max_dd_pct": round(mdd * 100, 2),
                    "sharpe": round(sharpe, 2) if sharpe is not None else None}


def portfolio(days: list[str], dates: np.ndarray, rank: np.ndarray, r: np.ndarray, take: np.ndarray) -> dict:
    """At most MAX_TRADES_PER_DAY trades a day (highest rank first), SLOT of the account each."""
    by_day: dict[str, list[int]] = defaultdict(list)
    for i in np.nonzero(take & ~np.isnan(r))[0]:
        by_day[dates[i]].append(i)
    rets, n_trades = [], 0
    for d in days:
        idx = sorted(by_day.get(d, ()), key=lambda i: -rank[i])[:MAX_TRADES_PER_DAY]
        n_trades += len(idx)
        rets.append(float(SLOT * sum(r[i] for i in idx)))
    series, stats = _curve(days, rets)
    stats.update(trades=n_trades, days=len(days), active_days=sum(1 for x in rets if x != 0))
    return {"stats": stats, "curve": series}


def spy_benchmark(days: list[str]) -> dict:
    rows = {r[0]: r[1] for r in store.conn().execute("SELECT d, c FROM bars WHERE symbol='SPY'")}
    closes = [rows[d] for d in days if d in rows]
    if len(closes) < 2:
        return {"stats": {}, "curve": []}
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    series, stats = _curve([d for d in days if d in rows], rets)
    return {"stats": stats, "curve": series}


# ---------------------------------------------------------------- features for the learning model

def build_X(ds: dict, direction: np.ndarray) -> tuple[np.ndarray, list[str]]:
    f, votes, conf = ds["feats"], ds["votes"], ds["conf"]
    d = direction.astype(float)
    cols, names = [], []
    for i, b in enumerate(bots.BOTS):
        cols.append(votes[:, i] * conf[:, i] * d)
        names.append(f"vote_{b}")
    cols.append((votes * d[:, None] > 0).sum(1).astype(float)); names.append("agree")
    cols.append((votes * d[:, None] < 0).sum(1).astype(float)); names.append("disagree")
    cols.append(d); names.append("dir")
    for k in SIGNED:
        x = f[k]
        if k in ("rsi2", "rsi14"):
            x = x - 0.5
        if k == "surprise":
            x = np.clip(x, -50, 50) / 50
        cols.append(x * d); names.append(k)
    for k in UNSIGNED:
        x = f[k]
        if k in ("rvol", "form4", "news_n"):
            x = np.log1p(np.clip(np.nan_to_num(x), 0, 50))
        cols.append(x); names.append(k)
    X = np.nan_to_num(np.column_stack(cols), nan=0.0, posinf=0.0, neginf=0.0)
    return X, names


def _consensus(ds: dict) -> np.ndarray:
    return np.sign((ds["votes"] * ds["conf"]).sum(1)).astype(int)


def _fit_lr(X: np.ndarray, y: np.ndarray):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler().fit(X)
    Xs = np.clip(sc.transform(X), -5, 5)
    lr = LogisticRegression(C=0.3, max_iter=1000).fit(Xs, y)
    return sc, lr


def _as_model(sc, lr) -> dict:
    return {"mean": sc.mean_.tolist(), "scale": sc.scale_.tolist(), "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])}


def _sides(p_long: np.ndarray, p_short: np.ndarray, r_long: np.ndarray, r_short: np.ndarray):
    """Per row: chosen side (+1/-1), its probability, its return."""
    side = np.where(p_long >= p_short, 1, -1)
    return side, np.maximum(p_long, p_short), np.where(side > 0, r_long, r_short)


def _predict(model: dict, X: np.ndarray) -> np.ndarray:
    mean, scale = np.array(model["mean"]), np.array(model["scale"])
    z = np.clip((X - mean) / np.where(scale > 0, scale, 1), -5, 5) @ np.array(model["coef"]) + model["intercept"]
    return 1 / (1 + np.exp(-z))


# ---------------------------------------------------------------- weighted vote

def bot_weights(ds: dict, mask: np.ndarray) -> dict[str, float]:
    """Weight = training expectancy shrunk towards 0 for small samples; losing bots get 0. Normalised to sum 1."""
    w = {}
    for i, b in enumerate(bots.BOTS):
        sel = mask & (ds["votes"][:, i] != 0)
        r = bots.directional(ds, ds["votes"][:, i])[sel]
        r = r[~np.isnan(r)]
        w[b] = max(0.0, float(r.mean())) * len(r) / (len(r) + 200) if len(r) else 0.0
    tot = sum(w.values())
    return {b: (v / tot if tot > 0 else 0.0) for b, v in w.items()}   # no profitable bot: the vote never trades


def weighted_score(ds: dict, weights: dict[str, float]) -> np.ndarray:
    wv = np.array([weights[b] for b in bots.BOTS])
    return (ds["votes"] * ds["conf"] * wv).sum(1)


def _pick_threshold(values: np.ndarray, r: np.ndarray, grid, min_n: int) -> float:
    best, best_thr = -1e9, grid[0]
    for thr in grid:
        sel = values >= thr
        rr = r[sel & ~np.isnan(r)]
        if len(rr) >= min_n and rr.mean() > best:
            best, best_thr = float(rr.mean()), float(thr)
    return best_thr


# ---------------------------------------------------------------- fitting / applying the referees

def _features_both(ds: dict) -> tuple[np.ndarray, np.ndarray, list[str]]:
    ones = np.ones(ds["n"], dtype=int)
    XL, names = build_X(ds, ones)
    XS, _ = build_X(ds, -ones)
    return XL, XS, names


def fit_referees(ds: dict, XL: np.ndarray, XS: np.ndarray, train: np.ndarray) -> dict:
    """Weighted-vote weights/threshold and the two logistic models, using only rows in `train`."""
    w = bot_weights(ds, train)
    ws = weighted_score(ds, w)
    wdir = np.sign(ws).astype(int)
    r_w = bots.directional(ds, wdir)
    nz = np.abs(ws[(wdir != 0) & train])
    grid = [0.0] + ([float(np.quantile(nz, q)) for q in (0.25, 0.5, 0.75)] if len(nz) else [])
    out = {"weights": w, "w_thr": _pick_threshold(np.where(train & (wdir != 0), np.abs(ws), -1), r_w, grid, MIN_TRAIN_TRADES)}
    try:
        yl, ys = (ds["r_long"] > 0).astype(int), (ds["r_short"] > 0).astype(int)
        usable = ~np.isnan(ds["r_long"]) & ~np.isnan(ds["r_short"])
        tr_idx = np.nonzero(usable & train)[0]
        if len(tr_idx) >= 2 * MIN_TRAIN_TRADES:
            # threshold chosen on the last 20 % of the training rows (time order), models refitted on all of them
            cut = tr_idx[int(len(tr_idx) * 0.8)]
            fit_idx, val_idx = tr_idx[tr_idx < cut], tr_idx[tr_idx >= cut]
            ml = _as_model(*_fit_lr(XL[fit_idx], yl[fit_idx]))
            ms = _as_model(*_fit_lr(XS[fit_idx], ys[fit_idx]))
            _, p_v, r_v = _sides(_predict(ml, XL[val_idx]), _predict(ms, XS[val_idx]), ds["r_long"][val_idx], ds["r_short"][val_idx])
            out["p_thr"] = _pick_threshold(p_v, r_v, [0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.65], 150)
            out["long"] = _as_model(*_fit_lr(XL[tr_idx], yl[tr_idx]))
            out["short"] = _as_model(*_fit_lr(XS[tr_idx], ys[tr_idx]))
            out["rows_train"], out["rows_validation"] = int(len(tr_idx)), int(len(val_idx))
    except ImportError:
        log.warning("scikit-learn not installed: learning referee skipped")
    except Exception:  # noqa: BLE001
        log.exception("learning referee failed")
    return out


def apply_referees(ds: dict, XL: np.ndarray, XS: np.ndarray, fit: dict) -> dict:
    """method -> (take mask, net return per row, rank per row[, side])."""
    ws = weighted_score(ds, fit["weights"])
    wdir = np.sign(ws).astype(int)
    take_w = (wdir != 0) & (np.abs(ws) >= fit["w_thr"]) & (np.abs(ws) > 0)
    res = {"weighted": (take_w, bots.directional(ds, wdir), np.abs(ws))}
    if fit.get("long"):
        side, p, r_m = _sides(_predict(fit["long"], XL), _predict(fit["short"], XS), ds["r_long"], ds["r_short"])
        usable = ~np.isnan(ds["r_long"]) & ~np.isnan(ds["r_short"])
        res["model"] = (usable & (p >= fit["p_thr"]), r_m, p, side)
    return res


def _months_back(last: date, months: float) -> str:
    return (last - timedelta(days=int(round(months * 30.44)))).isoformat()


TEST_LABELS = {1: "son 1 ay", 2: "son 2 ay", 3: "son 3 ay", 6: "son 6 ay", 12: "son 12 ay"}


# ---------------------------------------------------------------- the test

def run_test(params: dict | None = None, progress=None) -> dict:
    """Fit on everything before the test window (default: the last 2 months), judge on the window. Then a walk-forward
    check over the last 12 months in windows of the same length: each window is judged by referees fitted only on the
    data before it (so the months with Finnhub headlines are also learned from)."""
    t0 = time.time()
    params = dict(bots.DEFAULT_PARAMS, **(params or {}))
    test_months = int(params.pop("test_months", 2) or 2)
    if progress:
        progress("Botlar 5 yıllık veride çalıştırılıyor…", 0, 1)
    ds = bots.build_dataset(params, progress=(lambda i, n: progress(f"Botlar çalışıyor: {i}/{n} hisse", i, n)) if progress else None)
    if not ds.get("n"):
        raise RuntimeError("Veri yok: önce 5 yıllık veriyi indirin")
    dates = ds["date"]
    last = date.fromisoformat(max(dates.tolist()))
    test_start = _months_back(last, test_months)
    train, test = dates < test_start, dates >= test_start
    all_days = [r[0] for r in store.conn().execute("SELECT DISTINCT d FROM bars WHERE symbol='SPY' ORDER BY d")]
    test_days = [d for d in all_days if test_start <= d <= last.isoformat()]
    years = sorted({str(d)[:4] for d in dates})
    if progress:
        progress("Hakem eğitiliyor…", 0, 1)
    XL, XS, names = _features_both(ds)

    methods: dict[str, dict] = {}
    for i, b in enumerate(bots.BOTS):
        v = ds["votes"][:, i]
        r = bots.directional(ds, v)
        take = v != 0
        methods[b] = {"train": metrics(r[take & train]), "test": metrics(r[take & test]),
                      "years": {y: metrics(r[take & np.char.startswith(dates.astype(str), y)]) for y in years},
                      "portfolio": portfolio(test_days, dates, ds["conf"][:, i], r, take & test)}
    cons = _consensus(ds)
    r_cons = bots.directional(ds, cons)
    rank_cons = np.abs((ds["votes"] * ds["conf"]).sum(1))
    methods["majority"] = {"train": metrics(r_cons[(cons != 0) & train]), "test": metrics(r_cons[(cons != 0) & test]),
                           "portfolio": portfolio(test_days, dates, rank_cons, r_cons, (cons != 0) & test)}

    fit = fit_referees(ds, XL, XS, train)
    applied = apply_referees(ds, XL, XS, fit)
    take_w, r_w, rank_w = applied["weighted"]
    methods["weighted"] = {"train": metrics(r_w[take_w & train]), "test": metrics(r_w[take_w & test]),
                           "portfolio": portfolio(test_days, dates, rank_w, r_w, take_w & test),
                           "weights": fit["weights"], "threshold": round(fit["w_thr"], 4)}
    model_info = None
    if "model" in applied:
        take_m, r_m, p, side = applied["model"]
        agree = take_m & (side == cons)
        imp = sorted(zip(names, fit["long"]["coef"]), key=lambda kv: -abs(kv[1]))
        methods["model"] = {
            "train": metrics(r_m[take_m & train]), "test": metrics(r_m[take_m & test]),
            "portfolio": portfolio(test_days, dates, p, r_m, take_m & test), "threshold": fit["p_thr"],
            "test_long": int((take_m & test & (side > 0)).sum()), "test_short": int((take_m & test & (side < 0)).sum()),
            "test_against_bots": int((take_m & test).sum() - (agree & test).sum()),
            "importance": [{"feature": k, "label": FEATURE_TR.get(k, k), "coef": round(c, 3)} for k, c in imp if k != "dir"],
        }
        model_info = {"rows_train": fit.get("rows_train"), "rows_validation": fit.get("rows_validation")}

    # ---- walk-forward over the last 12 months, windows of test_months
    if progress:
        progress("Kayan pencere kontrolü (son 12 ay)…", 0, 1)
    wf = _walk_forward(ds, XL, XS, last, test_months, all_days, cons, r_cons, rank_cons, progress)

    # ---- live referee: same recipe on every row
    if progress:
        progress("Canlı hakem tüm verilerle eğitiliyor…", 0, 1)
    live_fit = fit_referees(ds, XL, XS, np.ones(len(dates), dtype=bool))
    live_model = ({"features": names, "long": live_fit["long"], "short": live_fit["short"], "p_thr": live_fit["p_thr"]}
                  if live_fit.get("long") else None)
    model = {"trained_at": now_ts(), "params": params, "weights": live_fit["weights"], "w_threshold": live_fit["w_thr"],
             "lr": live_model, "train_range": [min(dates.tolist()), last.isoformat()], "test_range": [test_start, last.isoformat()]}
    store.kv_set("hakem_model", model)

    report = {
        "created_at": now_ts(), "seconds": round(time.time() - t0, 1), "params": dict(params, test_months=test_months),
        "test_months": test_months, "test_label": TEST_LABELS.get(test_months, f"son {test_months} ay"),
        "rows": int(ds["n"]), "symbols": int(len(set(ds["sym"].tolist()))),
        "train_range": [min(dates.tolist()), test_start], "test_range": [test_start, last.isoformat()],
        "test_days": len(test_days), "news_rows": int(ds["feats"]["news_era"].sum()), "model_info": model_info,
        "methods": {k: dict(v, label=METHOD_TR.get(k, k)) for k, v in methods.items()},
        "spy": spy_benchmark(test_days), "bots": {b: bots.BOT_TR[b] for b in bots.BOTS},
        "live_weights": live_fit["weights"], "walk_forward": wf,
    }
    with store.tx() as c:
        c.execute("INSERT INTO runs(created_at, kind, params, report) VALUES (?,?,?,?)",
                  (report["created_at"], "test", json.dumps(report["params"]), json.dumps(report, ensure_ascii=False)))
    return report


def _walk_forward(ds: dict, XL: np.ndarray, XS: np.ndarray, last: date, step_months: int, all_days: list[str],
                  cons: np.ndarray, r_cons: np.ndarray, rank_cons: np.ndarray, progress=None) -> dict:
    dates = ds["date"]
    n_blocks = max(1, 12 // step_months)
    bounds = [_months_back(last, step_months * (n_blocks - k)) for k in range(n_blocks)] + [(last + timedelta(days=1)).isoformat()]
    combined = {m: [np.zeros(len(dates), dtype=bool), np.full(len(dates), np.nan), np.zeros(len(dates))] for m in ("weighted", "model")}
    blocks = []
    for k in range(n_blocks):
        a, b = bounds[k], bounds[k + 1]
        if progress:
            progress(f"Kayan pencere {k + 1}/{n_blocks}: {a} – {b}", k, n_blocks)
        train, blk = dates < a, (dates >= a) & (dates < b)
        fit = fit_referees(ds, XL, XS, train)
        applied = apply_referees(ds, XL, XS, fit)
        row = {"from": a, "to": b, "majority": metrics(r_cons[(cons != 0) & blk])}
        for m, val in applied.items():
            take, r, rank = val[0], val[1], val[2]
            sel = take & blk
            row[m] = metrics(r[sel])
            combined[m][0] |= sel
            combined[m][1][blk] = r[blk]
            combined[m][2][blk] = rank[blk]
        blocks.append(row)
    days = [d for d in all_days if bounds[0] <= d <= last.isoformat()]
    span = (dates >= bounds[0])
    out = {"step_months": step_months, "from": bounds[0], "to": last.isoformat(), "blocks": blocks, "days": len(days),
           "methods": {"majority": {"test": metrics(r_cons[(cons != 0) & span]),
                                    "portfolio": portfolio(days, dates, rank_cons, r_cons, (cons != 0) & span)}},
           "spy": spy_benchmark(days)}
    for m, (take, r, rank) in combined.items():
        if take.any():
            out["methods"][m] = {"test": metrics(r[take]), "portfolio": portfolio(days, dates, rank, r, take)}
    for k, v in out["methods"].items():
        v["label"] = METHOD_TR.get(k, k)
    return out


def last_report() -> dict | None:
    r = store.conn().execute("SELECT report FROM runs WHERE kind='test' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r else None


# ---------------------------------------------------------------- live decision for one day

def decide(ds_today: dict, model: dict) -> list[dict]:
    """Rank today's rows (one per symbol, from bots.compute_symbol(live=True)) with the trained referee."""
    n = ds_today.get("n") or 0
    if not n:
        return []
    cons = _consensus(ds_today)
    ws = weighted_score(ds_today, model["weights"])
    lrm = model.get("lr")
    side = p = None
    if lrm:
        ones = np.ones(n, dtype=int)
        side, p, _ = _sides(_predict(lrm["long"], build_X(ds_today, ones)[0]), _predict(lrm["short"], build_X(ds_today, -ones)[0]),
                            np.zeros(n), np.zeros(n))
    out = []
    for i in range(n):
        votes = {b: int(ds_today["votes"][i, j]) for j, b in enumerate(bots.BOTS)}
        if not any(votes.values()):
            continue
        if p is not None:
            d, ok, rank = int(side[i]), bool(p[i] >= lrm["p_thr"]), float(p[i])
        else:
            d = int(np.sign(ws[i]))
            ok, rank = d != 0 and abs(ws[i]) >= model.get("w_threshold", 0), float(abs(ws[i]))
        if d == 0:
            continue
        out.append({"symbol": str(ds_today["sym"][i]), "direction": "long" if d > 0 else "short", "take": ok,
                    "probability": round(float(p[i]), 3) if p is not None else None, "score": round(float(ws[i]), 3),
                    "bots_direction": int(cons[i]), "rank": rank, "votes": votes, "open": float(ds_today["open"][i]),
                    "gap_pct": round(float(ds_today["feats"]["gap"][i]) * 100, 2)})
    out.sort(key=lambda x: -x["rank"])
    return out
