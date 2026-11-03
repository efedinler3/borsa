"""Gün içi denetim: how wrong is the backtest's daily-bar assumption?

With daily bars, a day that touches both the stop and the target cannot tell which came first, so the lab counts it
as a stop (pessimistic). Yahoo serves 5-minute bars for the last ~60 days; this audit replays stop/target brackets
from the open on those days for the most traded stocks and measures how often the target actually came first and what
that is worth per trade.
"""
from __future__ import annotations

import importlib
import logging
from collections import defaultdict
from datetime import datetime

import numpy as np

from ..util import ET, now_ts
from . import bots, store

log = logging.getLogger("marketdesk.lab")

MULTS = (0.5, 1.0, 1.5, 2.0)     # stop = target = m x ATR (the evolved bots mostly use ratio 1 and 1.5-2 x ATR)


def _market():
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.collectors.market")


def _bars_5m(market, sym: str) -> dict[str, list[tuple]]:
    ysym = market._yahoo_symbol(sym) if hasattr(market, "_yahoo_symbol") else sym
    res = market._chart(ysym, range_="60d", interval="5m", prepost=False)
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    out: dict[str, list[tuple]] = defaultdict(list)
    for i, t in enumerate(res.get("timestamp") or []):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]  # noqa: E741
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c):
            continue
        out[datetime.fromtimestamp(t, ET).date().isoformat()].append((t, o, h, l, c))
    return out


def run(n_symbols: int = 120, progress=None) -> dict:
    market = _market()
    syms = [r[0] for r in store.conn().execute("SELECT symbol FROM universe ORDER BY rank LIMIT ?", (n_symbols,))]
    daily = bots.load_bars(syms)
    stats = {(m, side): {"days": 0, "both": 0, "target_first": 0, "same_bar": 0, "bias": 0.0} for m in MULTS for side in (1, -1)}
    done = 0
    for i0 in range(0, len(syms), 8):
        chunk = syms[i0:i0 + 8]
        res = market._parallel(lambda s: _bars_5m(market, s), [(s,) for s in chunk], workers=4, timeout=90)
        for j, sym in enumerate(chunk):
            intr = res.get(j)
            b = daily.get(sym)
            if isinstance(intr, Exception) or not intr or not b:
                continue
            atr = bots._shift(bots._atr(b["h"], b["l"], b["c"])) / bots._shift(b["c"])   # known at the open
            pos = {d: k for k, d in enumerate(b["d"])}
            for d, bars5 in intr.items():
                k = pos.get(d)
                if k is None or np.isnan(atr[k]) or len(bars5) < 10:
                    continue
                bars5.sort()
                o = bars5[0][1]
                hi, lo = max(x[2] for x in bars5), min(x[3] for x in bars5)
                for m in MULTS:
                    w = atr[k] * m
                    for side in (1, -1):
                        st = stats[(m, side)]
                        st["days"] += 1
                        stop, tgt = o * (1 - w * side), o * (1 + w * side)
                        hit_stop = lo <= stop if side > 0 else hi >= stop
                        hit_tgt = hi >= tgt if side > 0 else lo <= tgt
                        if not (hit_stop and hit_tgt):
                            continue
                        st["both"] += 1
                        for _, _o, h5, l5, _c in bars5:
                            s5 = l5 <= stop if side > 0 else h5 >= stop
                            t5 = h5 >= tgt if side > 0 else l5 <= tgt
                            if s5 and t5:
                                st["same_bar"] += 1      # still unknown inside one 5-minute bar: stays a stop
                                break
                            if t5:
                                st["target_first"] += 1
                                st["bias"] += 2 * w          # booked as -w, really +w
                                break
                            if s5:
                                break
            done += 1
        if progress:
            progress(f"5 dakikalık veri: {min(len(syms), i0 + 8)}/{len(syms)} hisse", i0 + 8, len(syms))
    rows = []
    for m in MULTS:
        for side in (1, -1):
            st = stats[(m, side)]
            both = st["both"]
            tf = st["target_first"]
            rows.append({"mult": m, "side": "long" if side > 0 else "short", "days": st["days"], "ambiguous": both,
                         "ambiguous_pct": round(both / st["days"] * 100, 2) if st["days"] else None,
                         "target_first_pct": round(tf / both * 100, 1) if both else None,
                         "same_bar_pct": round(st["same_bar"] / both * 100, 1) if both else None,
                         "bias_per_trade_pct": round(st["bias"] / st["days"] * 100, 3) if st["days"] else None})
    rep = {"created_at": now_ts(), "symbols": done, "rows": rows,
           "note": "bias_per_trade_pct: işlem başına ortalama, ihtiyatlı varsayımın sonucu ne kadar kötü gösterdiği (yüzde puan)"}
    store.kv_set("intraday_audit", rep)
    return rep
