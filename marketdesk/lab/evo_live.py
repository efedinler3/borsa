"""Live evolution bot: evaluates the tournament's final bots on today's opening data and (when enabled in Sanal İşlem)
opens their best signals as paper positions held up to the rule's number of days.

Today's features are computed exactly like the tournament's pool (bots.compute_symbol on daily bars up to yesterday
plus today's open; sector / SPY strength, market breadth, calendar flags), so the stored absolute thresholds apply.
The referee ranks stocks by the summed weights of the bots that fire on them (weight = the bot's t-statistic on
stocks it had never seen). Entry happens a few minutes after the open (the backtest assumed the open itself).
"""
from __future__ import annotations

import importlib
import logging
import math
from datetime import date, datetime, timedelta

import numpy as np

from ..util import ET, is_trading_day, market_session, now_ts
from . import bots, download, evolve, store

log = logging.getLogger("marketdesk.lab")


def _pkg(name: str):
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{name}")


def _month_turn(d: date) -> int:
    """1 on the first 3 or the last 2 trading days of the month (as in the pool)."""
    days = [x for x in (date(d.year, d.month, 1) + timedelta(days=i) for i in range(31))
            if x.month == d.month and is_trading_day(x)]
    if d not in days:
        return 0
    i = days.index(d)
    return 1 if i < 3 or i >= len(days) - 2 else 0


def _append_today(b: dict, tiso: str, px: float) -> dict:
    if b["d"] and b["d"][-1] == tiso:            # a partial bar of today from the daily update: drop it
        b = {k: (v[:-1] if k != "d" else v[:-1]) for k, v in b.items()}
    b = dict(b)
    b["d"] = b["d"] + [tiso]
    b["o"] = np.append(b["o"], px)
    for k in ("h", "l", "c", "v"):
        b[k] = np.append(b[k], np.nan)
    return b


_ROWS: dict = {"key": None, "ts": 0, "data": None}
ROWS_TTL = 15 * 60          # the trading bot and the live learning share one computation after the open


def today_rows(progress=None) -> dict:
    """Features of every universe stock for today (needs today's open, or the premarket price before the open).
    Reused for 15 minutes within the same session (2,600 stocks take a few minutes)."""
    now = now_ts()
    session = market_session(now)
    key = (datetime.fromtimestamp(now, ET).date().isoformat(), session)
    if _ROWS["key"] == key and now - _ROWS["ts"] < ROWS_TTL and _ROWS["data"] is not None:
        return _ROWS["data"]
    data = _today_rows(progress)
    _ROWS.update(key=key, ts=now, data=data)
    return data


def _today_rows(progress=None) -> dict:
    store.init()
    runner = importlib.import_module(f"{__package__}.runner")
    now = now_ts()
    session = market_session(now)
    if session == "closed":
        raise RuntimeError("Piyasa kapalı: evrim botu bugünün açılış (ya da piyasa öncesi) fiyatını gerektirir")
    today = datetime.fromtimestamp(now, ET).date()
    tiso = today.isoformat()
    uni = {r[0]: r[1] or "" for r in store.conn().execute("SELECT symbol, sector FROM universe ORDER BY rank")}
    syms = list(uni)
    refs = list(download.REFERENCE)
    if progress:
        progress("Açılış fiyatları alınıyor…", 0, 1)
    opens = runner._open_quotes(syms + refs, session)
    ref_bars = bots.load_bars(refs)
    for k, b in list(ref_bars.items()):
        if k in opens:
            ref_bars[k] = _append_today(b, tiso, opens[k])
    ctx = bots._context(ref_bars)
    spy20 = evolve._ret20_by_date(ref_bars["SPY"]) if "SPY" in ref_bars else {}
    sect20 = {e: evolve._ret20_by_date(ref_bars[e]) for e in download.SECTOR_ETF.values() if e in ref_bars}
    ev = runner._live_events(syms, today, set(syms))
    news_from = {r[0]: r[1] for r in store.conn().execute("SELECT symbol, MIN(ts) FROM news GROUP BY symbol")}
    params = dict(bots.DEFAULT_PARAMS)
    rows = []
    for i0 in range(0, len(syms), 200):
        chunk = syms[i0:i0 + 200]
        bars = bots.load_bars(chunk)
        for sym in chunk:
            b, px = bars.get(sym), opens.get(sym)
            if not b or not px:
                continue
            b = _append_today(b, tiso, px)
            r = bots.compute_symbol(sym, b, ev, ctx, params, news_from.get(sym), live=True)
            if not r:
                continue
            f = {k: float(v[0]) for k, v in r["feats"].items()}
            own = f.get("ret20", float("nan"))
            f["rs_spy20"] = (1 + own) / (1 + spy20.get(tiso, float("nan"))) - 1
            e20 = sect20.get(download.SECTOR_ETF.get(uni[sym], ""), {})
            f["rs_sector20"] = (1 + own) / (1 + e20.get(tiso, float("nan"))) - 1 if e20 else float("nan")
            f["dvol_usd"] = f.get("dvol", float("nan"))
            f["dvol"] = math.log10(max(f["dvol_usd"], 1.0)) if not math.isnan(f["dvol_usd"]) else float("nan")
            rows.append({"symbol": sym, "open": px, "f": f})
        if progress:
            progress(f"Göstergeler hesaplanıyor: {min(len(syms), i0 + 200)}/{len(syms)} hisse", i0 + 200, len(syms))
    above = [1.0 if r["f"].get("dist50", float("nan")) > 0 else 0.0 for r in rows if not math.isnan(r["f"].get("dist50", float("nan")))]
    breadth = sum(above) / len(above) if above else float("nan")
    for r in rows:
        r["f"]["breadth50"] = breadth
        r["f"]["dow"] = float(today.weekday())
        r["f"]["month_turn"] = float(_month_turn(today))
    vix = ctx.get(tiso, {}).get("vix")
    return {"ts": now, "date": tiso, "session": session, "rows": rows, "priced": len(opens), "breadth50": breadth,
            "vix": vix * 100 if vix is not None and not math.isnan(vix) else None}


def _fires(f: dict, conds: list) -> bool:
    for feat, op, thr, sparse in conds:
        x = f.get(feat)
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return False
        if sparse and x == 0:
            return False
        if not (x > thr if op == ">" else x < thr):
            return False
    return True


def _exit_pct(sv: dict, atr: float) -> tuple[float | None, float | None]:
    kind, x = sv["stop"]
    if kind == "pct":
        sl = x
    elif kind == "atr":
        sl = min(25.0, max(0.3, (atr if atr and not math.isnan(atr) else 0.02) * x * 100))
    else:
        return None, None
    return round(sl, 2), round(sl * sv["ratio"], 2)


def signals(progress=None) -> dict:
    """Today's ranked candidates from the final bots (read-only, places no orders)."""
    survivors = store.kv_get("evo_survivors") or []
    if not survivors:
        raise RuntimeError("Önce evrim turnuvasını çalıştırın: kayıtlı bot yok")
    data = today_rows(progress)
    vix = data.get("vix")
    regime = evolve.regime_of(vix / 100 if vix is not None else None, data.get("breadth50"))

    def weight(sv: dict) -> float:
        """The bot's referee weight in today's regime (its record on days like today), else its overall weight."""
        wr = (sv.get("weights_by_regime") or {}).get(regime) if regime else None
        return float(wr if wr is not None else sv.get("weight") or 0)

    cands: dict[tuple, dict] = {}
    for row in data["rows"]:
        f = row["f"]
        for sv in survivors:
            if not sv.get("conds_abs") or not _fires(f, sv["conds_abs"]):
                continue
            w = weight(sv)
            if w <= 0 and regime and (sv.get("weights_by_regime") or {}).get(regime) is not None:
                continue    # its record in today's regime says it loses money: it does not vote today
            key = (row["symbol"], sv["side"])
            c = cands.setdefault(key, {"symbol": row["symbol"], "side": "long" if sv["side"] > 0 else "short", "score": 0.0,
                                       "bots": 0, "open": row["open"], "best": None, "best_w": -1.0,
                                       "rsi2": f.get("rsi2"), "gap_pct": f.get("gap", 0) * 100, "atr": f.get("atr")})
            c["score"] += w
            c["bots"] += 1
            if w > c["best_w"]:
                c["best_w"], c["best"] = w, sv
    out = []
    def tie(x):        # equal referee scores: the more oversold stock first (as in the portfolio simulation)
        r = x["rsi2"]
        return 9.0 if r is None or math.isnan(r) else r
    for c in sorted(cands.values(), key=lambda x: (-x["score"], tie(x))):
        sv = c.pop("best")
        c.pop("best_w")
        sl, tp = _exit_pct(sv, c["atr"])
        c.update(rule=sv["rule"]["text"], hold=sv["hold"], sl_pct=sl, tp_pct=tp, score=round(c["score"], 2),
                 gap_pct=round(c["gap_pct"], 2), rsi2=round(c["rsi2"] * 100, 1) if c["rsi2"] is not None and not math.isnan(c["rsi2"]) else None)
        out.append(c)
    res = {"ts": data["ts"], "date": data["date"], "session": data["session"], "provisional": data["session"] == "premarket",
           "evaluated": len(data["rows"]), "priced": data["priced"], "vix": data["vix"], "breadth50": data["breadth50"],
           "regime": regime, "regime_tr": evolve.REGIME_TR.get(regime) if regime else None,
           "bots": len(survivors), "candidates": out[:60], "total": len(out)}
    store.kv_set("evo_today", res)
    return res


def trade(cfg: dict) -> dict:
    """Open today's best evolution signals as paper positions (once a day, after the open)."""
    paper = _pkg("paper")
    s = paper.settings(cfg)
    res = signals()
    if res["provisional"]:
        return {"placed": 0, "message": "Piyasa öncesi: işlem açılışta yapılır"}
    acct, pos = paper._account(), paper._positions()
    equity = paper._valuation(acct, pos)["equity"]
    placed, skipped = 0, []
    for c in res["candidates"]:
        if placed >= s["evo_max_new"]:
            break
        if c["symbol"] in pos:
            continue
        if c["side"] == "short" and not s["allow_short"]:
            skipped.append(f"{c['symbol']} (açığa satış kapalı)")
            continue
        qty = math.floor(equity * s["evo_position_pct"] / 100 / c["open"])
        if qty < 1:
            continue
        note = f"Evrim: {c['bots']} bot, skor {c['score']:.1f} · {c['rule']}"
        try:
            paper.place_order({"symbol": c["symbol"], "side": "buy" if c["side"] == "long" else "sell", "qty": qty, "type": "market",
                               "stop_loss_pct": c["sl_pct"], "take_profit_pct": c["tp_pct"], "hold_days": c["hold"],
                               "note": note[:200]}, origin="evrim")
            paper._log("bot", c["symbol"], note[:400])
            placed += 1
        except paper.PaperError as e:
            skipped.append(f"{c['symbol']} ({e})")
    if skipped:
        paper._log("info", "", "Evrim botu atladı: " + "; ".join(skipped)[:400])
    store.kv_set("evo_traded", res["date"])
    return {"placed": placed, "candidates": res["total"], "skipped": skipped}
