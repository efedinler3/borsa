"""Batch test (toplu deneme): the same four bots and referees under many exit rules.

Exit rules: fixed stop/target pairs (1/2 ... 5/10 %), stops scaled to each stock's volatility (0.5 / 1 / 1.5 x ATR,
target = 2 x stop), or no stop at all; each held 1, 3 or 5 trading days. Entry is always the open of the signal day.
On later days a gap through the stop or target fills at that day's open (worse / better than the level). A day that
touches both levels counts as the stop. Returns are net of slippage on both sides.

For every rule: each bot, the simple majority, the weighted vote and the learning model on the test window, plus the
12-month walk-forward check. Picking the best of 24 rules after the fact flatters it (multiple testing), so a rule is
called "tutarlı" only if the learning model is positive in training, in the walk-forward total AND in at least 4 of the
6 walk-forward windows, and beats simply buying every flagged stock (market drift) over the walk-forward year.
"""
from __future__ import annotations

import bisect
import json
import logging
import time
from datetime import date, timedelta

import numpy as np

from ..util import now_ts
from . import bots, hakem, store

log = logging.getLogger("marketdesk.lab")

STOPS = (("pct", 1.0), ("pct", 2.0), ("pct", 3.0), ("pct", 5.0), ("atr", 0.5), ("atr", 1.0), ("atr", 1.5), ("none", 0.0))
HOLDS = (1, 3, 5)
TP_RATIO = 2.0


def stop_label(kind: str, x: float) -> str:
    if kind == "pct":
        return f"Zarar durdur %{x:g} / kâr al %{x * TP_RATIO:g}"
    if kind == "atr":
        return f"Zarar durdur {x:g}×ATR / kâr al {x * TP_RATIO:g}×ATR"
    return "Zarar durdur yok (süre sonunda çık)"


def _index_rows(ds: dict, bars: dict) -> tuple[dict, np.ndarray]:
    """Row -> position of its date in the symbol's bar arrays."""
    pos = np.full(ds["n"], -1, dtype=np.int64)
    by_sym: dict[str, list[int]] = {}
    for i, s in enumerate(ds["sym"].tolist()):
        by_sym.setdefault(s, []).append(i)
    for s, rows in by_sym.items():
        b = bars.get(s)
        if not b:
            continue
        idx = {d: k for k, d in enumerate(b["d"])}
        for i in rows:
            pos[i] = idx.get(str(ds["date"][i]), -1)
    return by_sym, pos


def outcomes(ds: dict, bars: dict, by_sym: dict, pos: np.ndarray, kind: str, x: float, hold: int, slip: float) -> tuple[np.ndarray, np.ndarray]:
    """Net returns of a long and a short trade from each row's open under one exit rule."""
    r_long = np.full(ds["n"], np.nan)
    r_short = np.full(ds["n"], np.nan)
    atr = ds["feats"]["atr"]
    for s, rows in by_sym.items():
        b = bars.get(s)
        if not b:
            continue
        rows = np.array(rows)
        t = pos[rows]
        ok = (t >= 0) & (t + hold - 1 < len(b["d"]))
        rows, t = rows[ok], t[ok]
        if not len(rows):
            continue
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]  # noqa: E741
        if kind == "pct":
            sl = np.full(len(rows), x / 100)
        elif kind == "atr":
            sl = np.clip(np.nan_to_num(atr[rows], nan=0.02) * x, 0.003, 0.25)
        else:
            sl = np.full(len(rows), np.inf)
        tp = sl * TP_RATIO
        for side in (1, -1):
            entry = o[t] * (1 + slip * side)
            stop = entry * (1 - sl * side)
            target = entry * (1 + tp * side)
            res = np.full(len(rows), np.nan)
            open_ = np.ones(len(rows), dtype=bool)
            for j in range(hold):
                k = t + j
                oj, hj, lj, cj = o[k], h[k], l[k], c[k]
                if j > 0:   # gap through a level: filled at the open
                    gap_stop = open_ & ((oj <= stop) if side > 0 else (oj >= stop))
                    res[gap_stop] = side * (oj[gap_stop] * (1 - slip * side) / entry[gap_stop] - 1)
                    open_ &= ~gap_stop
                    gap_tgt = open_ & ((oj >= target) if side > 0 else (oj <= target))
                    res[gap_tgt] = side * (oj[gap_tgt] * (1 - slip * side) / entry[gap_tgt] - 1)
                    open_ &= ~gap_tgt
                hit_stop = open_ & ((lj <= stop) if side > 0 else (hj >= stop))
                res[hit_stop] = -sl[hit_stop] - slip
                open_ &= ~hit_stop
                hit_tgt = open_ & ((hj >= target) if side > 0 else (lj <= target))
                res[hit_tgt] = tp[hit_tgt] - slip
                open_ &= ~hit_tgt
                if j == hold - 1:
                    res[open_] = side * (cj[open_] * (1 - slip * side) / entry[open_] - 1)
            (r_long if side > 0 else r_short)[rows] = res
    return r_long, r_short


def purge_before(day: str, hold: int, trading_days: list[str]) -> str:
    """Training must end `hold` trading days before a test window: a trade entered on one of the last hold-1 days
    before the window is still open inside it, so its outcome would leak test prices into training."""
    i = bisect.bisect_left(trading_days, day)
    return trading_days[max(0, i - (hold - 1))] if hold > 1 and trading_days else day


def _wf(ds: dict, XL, XS, last: date, step_months: int, hold: int, trading_days: list[str]) -> dict:
    dates = ds["date"]
    n_blocks = max(1, 12 // step_months)
    bounds = [hakem._months_back(last, step_months * (n_blocks - k)) for k in range(n_blocks)] + [(last + timedelta(days=1)).isoformat()]
    take_all = {"weighted": [], "model": []}
    sides = {"long": [], "short": []}
    windows = []
    for k in range(n_blocks):
        a, b = bounds[k], bounds[k + 1]
        blk = (dates >= a) & (dates < b)
        fit = hakem.fit_referees(ds, XL, XS, dates < purge_before(a, hold, trading_days))
        applied = hakem.apply_referees(ds, XL, XS, fit)
        w = {}
        for m, val in applied.items():
            sel = val[0] & blk
            take_all[m].append(val[1][sel])
            w[m] = hakem.metrics(val[1][sel])
            if m == "model":
                sides["long"].append(val[1][sel & (val[3] > 0)])
                sides["short"].append(val[1][sel & (val[3] < 0)])
        windows.append(w)
    out = {m: hakem.metrics(np.concatenate(v)) if v else {"n": 0} for m, v in take_all.items()}
    out["model_long"] = hakem.metrics(np.concatenate(sides["long"])) if sides["long"] else {"n": 0}
    out["model_short"] = hakem.metrics(np.concatenate(sides["short"])) if sides["short"] else {"n": 0}
    out["windows_positive"] = sum(1 for w in windows if (w.get("model") or {}).get("avg_pct", -1) > 0)
    out["windows"] = len(windows)
    out["window_avgs"] = [(w.get("model") or {}).get("avg_pct") for w in windows]
    return out


def run_sweep(test_months: int = 2, slippage_bps: float = 5.0, progress=None) -> dict:
    t0 = time.time()
    slip = slippage_bps / 10000
    if progress:
        progress("Botlar 5 yıllık veride çalıştırılıyor…", 0, 1)
    ds = bots.build_dataset(dict(bots.DEFAULT_PARAMS, slippage_bps=slippage_bps))
    if not ds.get("n"):
        raise RuntimeError("Veri yok: önce 5 yıllık veriyi indirin")
    syms = sorted(set(ds["sym"].tolist()))
    bars = bots.load_bars(syms)
    by_sym, pos = _index_rows(ds, bars)
    XL, XS, _ = hakem._features_both(ds)
    dates = ds["date"]
    last = date.fromisoformat(max(dates.tolist()))
    test_start = hakem._months_back(last, test_months)
    test = dates >= test_start
    trading_days = [r[0] for r in store.conn().execute("SELECT DISTINCT d FROM bars WHERE symbol='SPY' ORDER BY d")]
    cons = hakem._consensus(ds)
    wf_start = hakem._months_back(last, 12)
    rows = []
    configs = [(k, x, h) for h in HOLDS for k, x in STOPS]
    for n_cfg, (kind, x, hold) in enumerate(configs):
        label = stop_label(kind, x)
        if progress:
            progress(f"Ayar {n_cfg + 1}/{len(configs)}: {label}, {hold} gün", n_cfg, len(configs))
        rl, rs = outcomes(ds, bars, by_sym, pos, kind, x, hold, slip)
        d2 = dict(ds, r_long=rl, r_short=rs)
        train = dates < purge_before(test_start, hold, trading_days)
        row = {"kind": kind, "x": x, "hold": hold, "label": label, "bots": {}}
        for i, b in enumerate(bots.BOTS):
            v = ds["votes"][:, i]
            r = bots.directional(d2, v)
            row["bots"][b] = {"train": hakem.metrics(r[(v != 0) & train]), "test": hakem.metrics(r[(v != 0) & test])}
        r_c = bots.directional(d2, cons)
        row["majority"] = {"train": hakem.metrics(r_c[(cons != 0) & train]), "test": hakem.metrics(r_c[(cons != 0) & test])}
        fit = hakem.fit_referees(d2, XL, XS, train)
        applied = hakem.apply_referees(d2, XL, XS, fit)
        for m in ("weighted", "model"):
            if m in applied:
                take, r = applied[m][0], applied[m][1]
                row[m] = {"train": hakem.metrics(r[take & train]), "test": hakem.metrics(r[take & test])}
            else:
                row[m] = {"train": {"n": 0}, "test": {"n": 0}}
        row["weights"] = fit["weights"]
        row["wf"] = _wf(d2, XL, XS, last, test_months, hold, trading_days)
        # market drift baseline: buy every row a bot flagged, no selection (long holds earn the bull market by themselves)
        row["baseline"] = {"train": hakem.metrics(rl[train]), "test": hakem.metrics(rl[test]), "wf": hakem.metrics(rl[dates >= wf_start])}
        mt, wf = row["model"]["train"], row["wf"].get("model") or {}
        base = row["baseline"]["wf"].get("avg_pct", 0) or 0
        row["consistent"] = bool(mt.get("n") and mt.get("avg_pct", 0) > 0 and wf.get("n") and wf.get("avg_pct", 0) > 0
                                 and wf.get("avg_pct", 0) > base and row["wf"]["windows_positive"] >= 4)
        rows.append(row)
    rows.sort(key=lambda r: -((r["wf"].get("model") or {}).get("avg_pct") if (r["wf"].get("model") or {}).get("n") else -9))
    report = {"created_at": now_ts(), "seconds": round(time.time() - t0, 1), "test_months": test_months,
              "test_label": hakem.TEST_LABELS.get(test_months, f"son {test_months} ay"), "slippage_bps": slippage_bps,
              "train_range": [min(dates.tolist()), test_start], "test_range": [test_start, last.isoformat()],
              "wf_range": [hakem._months_back(last, 12), last.isoformat()], "configs": len(rows), "rows": rows,
              "consistent": sum(1 for r in rows if r["consistent"]), "bots": {b: bots.BOT_TR[b] for b in bots.BOTS}}
    with store.tx() as c:
        c.execute("INSERT INTO runs(created_at, kind, params, report) VALUES (?,?,?,?)",
                  (report["created_at"], "sweep", json.dumps({"test_months": test_months, "slippage_bps": slippage_bps}),
                   json.dumps(report, ensure_ascii=False)))
    return report


def last_sweep() -> dict | None:
    r = store.conn().execute("SELECT report FROM runs WHERE kind='sweep' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r else None
