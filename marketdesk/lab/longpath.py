"""Long holds (up to 90 trading days) for the bot tree.

The Pool keeps only 5 days of price path per stock-day (a 90-day path would need ~3 GB). Here every symbol's raw daily
bars are kept once, in flat float32 arrays (~100 MB), and each Pool row gets the flat index of its entry bar. A trade
is then walked forward one trading day at a time, only for the rows that are still open, with the same rules as
Pool.outcome: gaps through the stop / target exit at the open, the stop is checked before the target, the last day
exits at the close, the same liquidity slippage on both sides.

Difference from Pool.outcome: a stock whose bars end before the data does (delisted, taken over) is sold at its last
close instead of being dropped, so long holds are not flattered by silently losing the stocks that disappeared. A
trade that is still open at the end of the data has no result (NaN).
"""
from __future__ import annotations

import logging

import numpy as np

from . import evolve, store

log = logging.getLogger("marketdesk.lab")

CHUNK = 200_000


def attach(pool) -> None:
    """Load the flat bars and map every Pool row to its entry bar (pool.bar_i, pool.bar_end, pool.bO/bH/bL/bC)."""
    if getattr(pool, "bar_i", None) is not None:
        return
    # read in chunks into preallocated arrays: 5.8M Python row tuples would need ~1 GB on their own
    wanted = set(pool.sym_names)
    counts = {r[0]: r[1] for r in store.conn().execute("SELECT symbol, COUNT(*) FROM bars GROUP BY symbol") if r[0] in wanted}
    names = sorted(counts)
    total = sum(counts.values())
    pool.bO, pool.bH, pool.bL, pool.bC = (np.empty(total, np.float32) for _ in range(4))
    day = np.empty(total, np.int64)
    code = np.empty(total, np.int32)
    at = 0
    for k in range(0, len(names), 100):
        part = names[k:k + 100]
        cur = store.conn().execute(f"SELECT symbol, d, o, h, l, c FROM bars WHERE symbol IN ({','.join('?' * len(part))}) "
                                   "ORDER BY symbol, d", part)
        num = {s: k + i for i, s in enumerate(part)}
        while True:
            rows = cur.fetchmany(50_000)
            if not rows:
                break
            m = len(rows)
            sl_ = slice(at, at + m)
            code[sl_] = [num[r[0]] for r in rows]
            day[sl_] = np.array([r[1] for r in rows], dtype="datetime64[D]").astype(np.int64)
            for arr, j in ((pool.bO, 2), (pool.bH, 3), (pool.bL, 4), (pool.bC, 5)):
                arr[sl_] = np.array([r[j] if r[j] is not None else np.nan for r in rows], dtype=np.float32)
            at += m
    if at != total:
        raise RuntimeError("fiyat verisi okunurken değişti (günlük güncelleme sürüyor olabilir); tekrar deneyin")
    first = np.r_[True, code[1:] != code[:-1]]
    starts = np.nonzero(first)[0]
    key = code.astype(np.int64) * 100_000 + day
    ends = np.r_[starts[1:], len(day)]
    last_day = int(day.max())
    # a symbol whose bars stop more than a week before the data does has left the market
    gone = (day[ends - 1] < last_day - 7)
    pos = {s: i for i, s in enumerate(names)}
    row_sym = np.array([pos.get(s, -1) for s in pool.sym_names], dtype=np.int64)[pool.symcode]
    udays = np.array(pool.udates, dtype="datetime64[D]").astype(np.int64)
    # market-day index of every bar (a stock may miss days: halts, data gaps), so exits map to the right day
    pool.bar_day = np.minimum(np.searchsorted(udays, day), len(udays) - 1).astype(np.int32)
    rkey = row_sym * 100_000 + udays[pool.dayidx]
    bi = np.searchsorted(key, rkey)
    bi = np.minimum(bi, len(key) - 1)
    ok = (row_sym >= 0) & (key[bi] == rkey)
    if not ok.all():
        log.warning("longpath: %d of %d rows have no matching bar", int((~ok).sum()), len(ok))
    pool.bar_i = np.where(ok, bi, -1).astype(np.int32)
    sym_of_row = np.where(ok, code[bi], 0)
    pool.bar_end = ends[sym_of_row].astype(np.int32)              # first bar index after the row's symbol
    pool.bar_gone = gone[sym_of_row]
    del key, rkey, code, day


def exit_day(pool, idx: np.ndarray, exd: np.ndarray) -> np.ndarray:
    """Market-day index of each trade's exit (exd = bar offset from outcome(); -1 = still open -> -1)."""
    idx = np.asarray(idx, dtype=np.int64)
    exd = np.asarray(exd, dtype=np.int64)
    out = np.full(len(idx), -1, np.int64)
    ok = exd >= 0
    if ok.any():
        out[ok] = pool.bar_day[pool.bar_i[idx[ok]].astype(np.int64) + exd[ok]]
    return out


def drift(pool, hold: int) -> np.ndarray:
    """Per day index: the average close-to-open return after `hold` days over every stock (the 'random entry')."""
    attach(pool)
    bi = pool.bar_i
    j = bi + hold - 1
    ok = (bi >= 0) & (j < pool.bar_end)
    r = np.full(pool.n, np.nan, np.float64)
    r[ok] = pool.bC[j[ok]] / pool.bO[bi[ok]] - 1
    ok &= np.isfinite(r)
    s_ = np.bincount(pool.dayidx[ok], weights=r[ok], minlength=pool.n_days)
    n_ = np.bincount(pool.dayidx[ok], minlength=pool.n_days)
    return np.where(n_ > 0, s_ / np.maximum(n_, 1), 0.0)


def outcome(pool, idx: np.ndarray, side: int, stop: int, ratio: int, hold: int) -> tuple[np.ndarray, np.ndarray]:
    """Net return of a trade from each row's open, held at most `hold` trading days (a number of days, not an index),
    and the day offset of its exit (0 = entry day). NaN / -1 for trades still open at the end of the data."""
    attach(pool)
    idx = np.asarray(idx, dtype=np.int64)
    res = np.full(len(idx), np.nan, np.float32)
    exd = np.full(len(idx), -1, np.int16)
    for a in range(0, len(idx), CHUNK):
        r, e = _outcome(pool, idx[a:a + CHUNK], side, stop, ratio, hold)
        res[a:a + CHUNK], exd[a:a + CHUNK] = r, e
    return res, exd


def _outcome(pool, idx, side, stop, ratio, hold):
    kind, xk = evolve.STOP_SET[stop]
    n = len(idx)
    res = np.full(n, np.nan, np.float32)
    exd = np.full(n, -1, np.int16)
    b0 = pool.bar_i[idx].astype(np.int64)
    end = pool.bar_end[idx].astype(np.int64)
    gone = pool.bar_gone[idx]
    valid = b0 >= 0
    base = np.where(valid, pool.bO[np.maximum(b0, 0)], np.nan)
    slip = pool.slip[idx].astype(np.float32)
    if kind == "pct":
        sl = np.full(n, xk / 100, dtype=np.float32)
    elif kind == "atr":
        sl = np.clip(np.nan_to_num(pool.atr[idx], nan=0.02) * xk, 0.003, 0.25).astype(np.float32)
    else:
        sl = np.full(n, np.inf, dtype=np.float32)
    tp = sl * evolve.RATIO_SET[ratio]
    entry = 1 + slip * side
    stop_p, tgt_p = entry * (1 - sl * side), entry * (1 + tp * side)
    live = np.nonzero(valid & np.isfinite(base) & (base > 0))[0]
    last_c = np.full(n, np.nan, np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        for j in range(hold):
            if not len(live):
                break
            bj = b0[live] + j
            inside = bj < end[live]
            if not inside.all():
                out = live[~inside]
                # bars ended: a delisted stock is sold at its last close, a stock still trading is an open trade
                g = out[gone[out] & np.isfinite(last_c[out])]
                res[g] = side * (last_c[g] * (1 - slip[g] * side) / entry[g] - 1)
                exd[g] = j - 1
                live, bj = live[inside], bj[inside]
                if not len(live):
                    break
            bs = base[live]
            oj, hj, lj, cj = pool.bO[bj] / bs, pool.bH[bj] / bs, pool.bL[bj] / bs, pool.bC[bj] / bs
            sp, tg, en, sg = stop_p[live], tgt_p[live], entry[live], slip[live]
            done = np.zeros(len(live), bool)
            if j > 0:
                g = (oj <= sp) if side > 0 else (oj >= sp)
                g |= (oj >= tg) if side > 0 else (oj <= tg)
                g &= np.isfinite(oj)
                res[live[g]] = side * (oj[g] * (1 - sg[g] * side) / en[g] - 1)
                done |= g
            g = ~done & ((lj <= sp) if side > 0 else (hj >= sp))
            res[live[g]] = -sl[live[g]] - sg[g]
            done |= g
            g = ~done & ((hj >= tg) if side > 0 else (lj <= tg))
            res[live[g]] = tp[live[g]] - sg[g]
            done |= g
            if j == hold - 1:
                g = ~done & np.isfinite(cj)
                res[live[g]] = side * (cj[g] * (1 - sg[g] * side) / en[g] - 1)
                done |= g
            exd[live[done]] = j
            last_c[live] = np.where(np.isfinite(cj), cj, last_c[live])
            live = live[~done]
    return res, exd
