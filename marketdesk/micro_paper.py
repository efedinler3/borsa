"""Mikro sanal hesap: a separate $100 paper account for $1-10 stocks (small caps), next to the $100,000 account.
Nothing is ever sent to a broker.

The bot buys small-cap gainers from the movers list under its settings (written by the live learning: the variant
with the best score; defaults below until there is evidence) and exits on stop / target, the 10-minute cut, the
30-minute cull, the profit lock and 5 minutes before the close. Only during the regular session. Fills pay at least
half a cent of spread per side (more for thin stocks), whole shares only, at most 3 positions of up to $40.
"""
from __future__ import annotations

import importlib
import logging
import math
import re
import uuid
from datetime import date, datetime

from . import db
from .config import load_config, save_config
from .util import ET, close_minutes, is_trading_day, market_session, now_ts

log = logging.getLogger("marketdesk.micro_paper")

START_CASH = 100.00
MAX_POSITIONS = 3
MAX_ALLOC_PER_TRADE = 40.00
MIN_CASH_TO_BUY = 20.00
FLATTEN_BEFORE_CLOSE = 5 * 60
RECORD_EVERY = 60
EQUITY_EVERY = 300

# the Gemini session's rules as the starting point: $1-10, +2-12 %, stop 1.5 %, target 4 %, 10-minute cut, 30-minute cull
DEFAULTS = {"min_price": 1.0, "max_price": 10.0, "min_chg": 2.0, "max_chg": 12.0, "min_rvol": 0.0, "min_dvol": 1e6,
            "sl": 1.5, "tp": 4.0, "cut10": 1, "cull30": 1, "lock": 0.0, "start": "09:35", "last": "15:30",
            "max_trades": 6, "day_stop": 0.0}
_LIMITS = {"min_price": (0.5, 50), "max_price": (1, 100), "min_chg": (0, 100), "max_chg": (0.5, 500), "min_rvol": (0, 50),
           "min_dvol": (0, 1e10), "sl": (0.3, 30), "tp": (0.3, 100), "lock": (0, 50), "max_trades": (1, 50), "day_stop": (0, 100)}

STALE_QUOTE = 5 * 60          # small caps halt often: no stop/target/entry on a price older than this
MOVERS_MAX_AGE = 15 * 60      # candidates only from a fresh movers snapshot

_state = {"last_rec": 0, "last_equity": 0, "marks": {}, "marks_loaded": False, "failed": set()}


# ---------------------------------------------------------------- settings

def settings(cfg: dict | None = None) -> dict:
    raw = (cfg or load_config()).get("micro")
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return _validate(out)


def _validate(s: dict) -> dict:
    out = dict(DEFAULTS)
    for k, v in s.items():
        if k not in DEFAULTS:
            continue
        if k in ("cut10", "cull30"):
            out[k] = 1 if (v.strip().lower() in ("1", "true", "evet", "on") if isinstance(v, str) else bool(v)) else 0
        elif k in ("start", "last"):
            out[k] = v if isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) else DEFAULTS[k]
        else:
            try:
                x = float(v)
            except (TypeError, ValueError):
                x = DEFAULTS[k]
            lo, hi = _LIMITS[k]
            x = min(hi, max(lo, x)) if math.isfinite(x) else DEFAULTS[k]
            out[k] = int(x) if k == "max_trades" else x
    if out["max_price"] <= out["min_price"]:
        out["max_price"] = out["min_price"] + 1
    if out["max_chg"] <= out["min_chg"]:
        out["max_chg"] = out["min_chg"] + 1
    return out


def update_settings(upd: dict, note: str = "") -> dict:
    cur = settings()
    cur.update({k: v for k, v in (upd or {}).items() if k in DEFAULTS})
    cur = _validate(cur)
    save_config({"micro": cur})
    if note:
        _log("info", "", note[:300])
    return cur


# ---------------------------------------------------------------- storage

def init_db() -> None:
    with db._tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS micro_account (id INT PRIMARY KEY, cash REAL, start_cash REAL, created_at INT,"
                  " day_date TEXT, day_start_equity REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS micro_positions (symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL, opened_at INT,"
                  " stop_price REAL, target_price REAL, origin TEXT, last_price REAL, last_ts INT)")
        c.execute("CREATE TABLE IF NOT EXISTS micro_orders (id TEXT PRIMARY KEY, created_at INT, symbol TEXT, side TEXT, qty REAL,"
                  " type TEXT, status TEXT, filled_at INT, fill_price REAL, realized_pnl REAL, note TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS micro_equity (ts INT PRIMARY KEY, equity REAL, cash REAL, positions_value REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS micro_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT, level TEXT, symbol TEXT, msg TEXT)")
        if not c.execute("SELECT id FROM micro_account WHERE id=1").fetchone():
            c.execute("INSERT INTO micro_account (id, cash, start_cash, created_at, day_start_equity) VALUES (1,?,?,?,?)",
                      (START_CASH, START_CASH, now_ts(), START_CASH))


def _account(c=None) -> dict:
    c = c or db.conn()
    r = c.execute("SELECT * FROM micro_account WHERE id=1").fetchone()
    if not r:
        init_db()
        r = db.conn().execute("SELECT * FROM micro_account WHERE id=1").fetchone()
    return dict(r)


def _positions(c=None) -> list[dict]:
    c = c or db.conn()
    return [dict(r) for r in c.execute("SELECT * FROM micro_positions ORDER BY opened_at DESC")]


def _log(level: str, symbol: str, msg: str, ts: int | None = None) -> None:
    try:
        with db._tx() as c:
            c.execute("INSERT INTO micro_log (ts, level, symbol, msg) VALUES (?,?,?,?)", (ts or now_ts(), level, symbol, msg[:300]))
            c.execute("DELETE FROM micro_log WHERE id <= (SELECT MAX(id) FROM micro_log) - 2000")
    except Exception:  # noqa: BLE001 - the log must never break trading
        log.exception("micro log write failed")


def _paper():
    return importlib.import_module(f"{__package__}.paper")


def _quotes(symbols: list[str]) -> dict[str, dict]:
    try:
        return _paper().quotes(symbols)
    except Exception:  # noqa: BLE001
        log.exception("micro quotes failed")
        return {}


def _usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".") + " $"


def _pct(v: float) -> str:
    return ("+" if v > 0 else "-" if v < 0 else "") + "%" + f"{abs(v):.2f}".replace(".", ",")


def _slip(price: float, dvol: float | None) -> float:
    """Per-side cost: half a cent of spread at least (0.2 %), +0.3 % for stocks trading under $2M a day."""
    s = max(0.005 / max(price, 0.01), 0.002)
    return s + (0.003 if (dvol or 0) < 2e6 else 0.0)


def _dvol(q: dict) -> float | None:
    try:
        return float(q["avg_volume"]) * float(q["price"]) if q.get("avg_volume") else None
    except (TypeError, ValueError):
        return None


def _fresh(q: dict | None, now: int) -> bool:
    """A live, recent price (a halted or stale quote must not trigger fills, stops or targets)."""
    if not q or not q.get("price") or float(q["price"]) <= 0:
        return False
    return not (q.get("ts") and now - int(q["ts"]) > STALE_QUOTE)


def _load_marks() -> None:
    """Exit checks already done survive a restart (snapshot key `micro_exit_marks`)."""
    if _state["marks_loaded"]:
        return
    _state["marks_loaded"] = True
    try:
        saved = db.get_snapshot_data("micro_exit_marks", {}) or {}
        if isinstance(saved, dict):
            _state["marks"].update({str(k): int(v) for k, v in saved.items()})
    except Exception:  # noqa: BLE001
        log.exception("micro exit marks load failed")


def _save_marks() -> None:
    try:
        db.set_snapshot("micro_exit_marks", dict(_state["marks"]))
    except Exception:  # noqa: BLE001
        log.exception("micro exit marks save failed")


def _rvol(q: dict, now: int) -> float | None:
    try:
        return _paper()._rel_volume_adj(q, now)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- read side

def summary() -> dict:
    init_db()
    acct, pos = _account(), _positions()
    pos_val, rows = 0.0, []
    for p in pos:
        last = float(p.get("last_price") or p["avg_price"])
        mv = round(p["qty"] * last, 2)
        pos_val += mv
        rows.append(dict(p, market_value=mv, unrealized_pnl=round(p["qty"] * (last - p["avg_price"]), 2),
                         unrealized_pct=round((last / p["avg_price"] - 1) * 100, 2) if p["avg_price"] else 0.0))
    cash = float(acct.get("cash") or 0.0)
    equity = round(cash + pos_val, 2)
    start = float(acct.get("start_cash") or START_CASH)
    c = db.conn()
    orders = [dict(r) for r in c.execute("SELECT * FROM micro_orders ORDER BY created_at DESC LIMIT 30")]
    logs = [dict(r) for r in c.execute("SELECT * FROM micro_log ORDER BY id DESC LIMIT 30")]
    closed = [r[0] for r in c.execute("SELECT realized_pnl FROM micro_orders WHERE realized_pnl IS NOT NULL")]
    main_eq, main_pct = None, None
    try:
        p = _paper()
        a = p._account()
        main_eq = p._valuation(a, p._positions())["equity"]
        main_pct = round((main_eq / a["start_cash"] - 1) * 100, 2) if a.get("start_cash") else None
    except Exception:  # noqa: BLE001
        pass
    today = datetime.fromtimestamp(now_ts(), ET).date().isoformat()
    day_start = acct.get("day_start_equity") if acct.get("day_date") == today else None
    return {
        "account": {"cash": round(cash, 2), "equity": equity, "start_cash": start, "total_pnl": round(equity - start, 2),
                    "total_pnl_pct": round((equity / start - 1) * 100, 2) if start else 0.0, "positions_value": round(pos_val, 2),
                    "open_positions_count": len(rows),
                    "day_pnl": round(equity - day_start, 2) if day_start else None,
                    "day_pnl_pct": round((equity / day_start - 1) * 100, 2) if day_start else None},
        "positions": rows, "orders": orders, "log": logs, "session": market_session(now_ts()), "settings": settings(),
        "stats": {"trades": len(closed), "wins": sum(1 for x in closed if x > 0), "realized_total": round(sum(closed), 2)},
        "comparison": {"main_equity": round(main_eq, 2) if main_eq is not None else None, "main_pnl_pct": main_pct,
                       "micro_equity": equity, "micro_pnl_pct": round((equity / start - 1) * 100, 2) if start else 0.0},
    }


# ---------------------------------------------------------------- execution

def place_order(data: dict) -> dict:
    """Market order on the $100 account at the live price ± spread (regular session only)."""
    init_db()
    sym = str(data.get("symbol") or "").upper().strip().lstrip("$")
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
        raise ValueError("Geçersiz sembol")
    side = str(data.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise ValueError("Geçersiz yön")
    now = now_ts()
    if market_session(now) != "open" and not data.get("_force"):
        raise ValueError("Mikro hesap yalnızca normal seansta işlem yapar")
    q = data.get("_quote") or _quotes([sym]).get(sym) or {}
    px = float(q.get("price") or 0)
    if px <= 0:
        raise ValueError(f"{sym} için anlık fiyat alınamadı")
    slip = _slip(px, _dvol(q))
    note = str(data.get("note") or ("Mikro alış" if side == "buy" else "Mikro satış"))[:200]
    oid = uuid.uuid4().hex[:16]
    with db._tx() as c:
        cash = float(c.execute("SELECT cash FROM micro_account WHERE id=1").fetchone()[0])
        if side == "buy":
            qty = math.floor(float(data.get("qty") or 0))
            fill = round(px * (1 + slip), 4)
            cost = round(qty * fill, 2)
            if qty < 1:
                raise ValueError("Adet en az 1 olmalı")
            if cost > cash + 1e-9:
                raise ValueError(f"Yetersiz bakiye: {_usd(cost)} gerekli, kasada {_usd(cash)} var")
            if c.execute("SELECT 1 FROM micro_positions WHERE symbol=?", (sym,)).fetchone():
                raise ValueError(f"{sym} zaten açık")
            if c.execute("SELECT COUNT(*) FROM micro_positions").fetchone()[0] >= MAX_POSITIONS:
                raise ValueError(f"En fazla {MAX_POSITIONS} açık pozisyon")
            if cost > MAX_ALLOC_PER_TRADE + 0.01:
                raise ValueError(f"Tek işlemde en fazla {_usd(MAX_ALLOC_PER_TRADE)} ({_usd(cost)} istendi)")
            s = settings()
            stop, tgt = round(fill * (1 - s["sl"] / 100), 4), round(fill * (1 + s["tp"] / 100), 4)
            c.execute("UPDATE micro_account SET cash=? WHERE id=1", (round(cash - cost, 4),))
            c.execute("INSERT INTO micro_positions (symbol, qty, avg_price, opened_at, stop_price, target_price, origin, last_price,"
                      " last_ts) VALUES (?,?,?,?,?,?,?,?,?)", (sym, qty, fill, now, stop, tgt, data.get("origin", "micro_bot"), px, now))
            c.execute("INSERT INTO micro_orders (id, created_at, symbol, side, qty, type, status, filled_at, fill_price, realized_pnl,"
                      " note) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (oid, now, sym, "buy", qty, "market", "filled", now, fill, None, note))
            msg = f"Alış: {qty:g} adet @ {_usd(fill)} (maliyet {_usd(cost)}, kalan nakit {_usd(cash - cost)}) · {note}"
            out = {"ok": True, "symbol": sym, "side": "buy", "qty": qty, "price": fill, "cost": cost}
        else:
            p = c.execute("SELECT * FROM micro_positions WHERE symbol=?", (sym,)).fetchone()
            if not p:
                raise ValueError(f"{sym} pozisyonu bulunamadı")
            qty, avg = float(p["qty"]), float(p["avg_price"])
            fill = round(px * (1 - slip), 4)
            pnl = round(qty * (fill - avg), 4)
            c.execute("UPDATE micro_account SET cash=? WHERE id=1", (round(cash + qty * fill, 4),))
            c.execute("DELETE FROM micro_positions WHERE symbol=?", (sym,))
            c.execute("INSERT INTO micro_orders (id, created_at, symbol, side, qty, type, status, filled_at, fill_price, realized_pnl,"
                      " note) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (oid, now, sym, "sell", qty, "market", "filled", now, fill, pnl, note))
            msg = f"Satış: {qty:g} adet @ {_usd(fill)} · K/Z {pnl:+.2f} $ ({_pct((fill / avg - 1) * 100)}) · {note}"
            out = {"ok": True, "symbol": sym, "side": "sell", "qty": qty, "price": fill, "pnl": pnl}
    _log("trade", sym, msg, now)
    return out


def _sell(sym: str, q: dict, note: str) -> None:
    try:
        place_order({"symbol": sym, "side": "sell", "note": note, "_quote": q})
    except Exception as e:  # noqa: BLE001
        _log("warn", sym, f"Satılamadı: {e}")


def _day_roll(acct: dict, today: str, now: int) -> None:
    if acct.get("day_date") == today:
        return
    eq = float(acct["cash"]) + sum(p["qty"] * float(p.get("last_price") or p["avg_price"]) for p in _positions())
    with db._tx() as c:
        c.execute("UPDATE micro_account SET day_date=?, day_start_equity=? WHERE id=1", (today, round(eq, 4)))


def _record_equity(now: int) -> None:
    if now - _state["last_equity"] < EQUITY_EVERY:
        return
    _state["last_equity"] = now
    acct = _account()
    pv = sum(p["qty"] * float(p.get("last_price") or p["avg_price"]) for p in _positions())
    with db._tx() as c:
        c.execute("INSERT OR REPLACE INTO micro_equity (ts, equity, cash, positions_value) VALUES (?,?,?,?)",
                  (now, round(acct["cash"] + pv, 4), round(acct["cash"], 4), round(pv, 4)))
        c.execute("DELETE FROM micro_equity WHERE ts < ?", (now - 400 * 86400,))


def _small_caps(now: int) -> list[dict]:
    snap = db.get_snapshot("movers")
    data, ts = snap if snap else (None, 0)
    if not isinstance(data, dict) or now - int(ts or 0) > MOVERS_MAX_AGE:
        return []
    return [x for x in (data.get("small_caps") or []) if isinstance(x, dict) and x.get("symbol")]


def small_caps(now: int) -> list[dict]:
    """Today's small-cap gainers (fresh movers snapshot) — the micro bot's candidate list."""
    return _small_caps(now)


def candidate_ok(qq: dict, s: dict, now: int) -> bool:
    """The micro bot's entry filters (price band, today's move, relative and dollar volume) on a live quote."""
    px, chg, rv, dv = float(qq["price"]), qq.get("change_pct"), _rvol(qq, now), _dvol(qq)
    if chg is None or not s["min_price"] <= px <= s["max_price"] or not s["min_chg"] <= float(chg) <= s["max_chg"]:
        return False
    if s["min_rvol"] > 0 and (rv is None or rv < s["min_rvol"]):
        return False
    return (dv or 0) >= s["min_dvol"]


def check_and_trade_micro_cands(now: int | None = None) -> dict:
    """One step (with the paper engine, every 15 s): marks, exits, candidate recording for the live learning, entries."""
    init_db()
    now = now or now_ts()
    if market_session(now) != "open":
        return {"session": market_session(now)}
    et = datetime.fromtimestamp(now, ET)
    d = et.date()
    if not is_trading_day(d):
        return {"session": "closed"}
    minute = et.hour * 60 + et.minute
    s = settings()
    pos = _positions()
    caps = _small_caps(now)
    q = _quotes(sorted({p["symbol"] for p in pos} | {x["symbol"] for x in caps}))
    _day_roll(_account(), d.isoformat(), now)
    close_ts = int(datetime(d.year, d.month, d.day, close_minutes(d) // 60, close_minutes(d) % 60, tzinfo=ET).timestamp())
    _state["failed"] = {x for x in _state["failed"] if x[0] == d.isoformat()}
    _load_marks()
    marks_before = dict(_state["marks"])
    # exits
    for p in pos:
        sym, qq = p["symbol"], q.get(p["symbol"])
        if not _fresh(qq, now):
            # no stop/target on a halted or stale price, but never carry a day position overnight: at the close
            # it is sold at the last known price
            if now >= close_ts - FLATTEN_BEFORE_CLOSE:
                px = float((qq or {}).get("price") or p.get("last_price") or p["avg_price"])
                _sell(sym, dict(qq or {}, price=px), "Gün sonu kapanış (güncel fiyat yok, son fiyat)")
            continue
        last = float(qq["price"])
        with db._tx() as c:
            c.execute("UPDATE micro_positions SET last_price=?, last_ts=? WHERE symbol=?", (last, now, sym))
        avg = float(p["avg_price"])
        gain = last / avg - 1
        age = now - int(p["opened_at"])
        key = f"{sym}:{p['opened_at']}"
        marks = _state["marks"]
        why = None
        if now >= close_ts - FLATTEN_BEFORE_CLOSE:
            why = "Gün sonu kapanış"
        elif p.get("stop_price") and last <= float(p["stop_price"]):
            why = "Kâr kilidi (başabaş)" if float(p["stop_price"]) >= avg else f"Zarar durdur ({_pct(gain * 100)})"
        elif p.get("target_price") and last >= float(p["target_price"]):
            why = f"Kâr al ({_pct(gain * 100)})"
        else:
            if s["cut10"] and age >= 600 and f"{key}:cut" not in marks:
                marks[f"{key}:cut"] = 1
                if gain <= -0.008:
                    why = f"10 dk erken kesme ({_pct(gain * 100)})"
            k = age // 1800
            if why is None and s["cull30"] and k >= 1 and marks.get(key, 0) < k:
                marks[key] = k
                if gain < 0.002:
                    why = f"{k * 30}. dakikada kâr yok ({_pct(gain * 100)})"
        if why:
            _sell(sym, qq, why)
            continue
        if s["lock"] > 0 and gain >= s["lock"] / 100 and float(p.get("stop_price") or 0) < avg * 1.002:
            with db._tx() as c:
                c.execute("UPDATE micro_positions SET stop_price=? WHERE symbol=?", (round(avg * 1.002, 4), sym))
            _log("info", sym, f"Kâr kilidi: {_pct(gain * 100)} kârda, zarar durdur başabaşa çekildi", now)
    # recording for the live learning (every bot variant replays these after the close)
    if now - _state["last_rec"] >= RECORD_EVERY:
        _state["last_rec"] = now
        rows = []
        for x in caps:
            qq = q.get(x["symbol"])
            if qq and qq.get("price"):
                rows.append({"symbol": x["symbol"], "price": qq["price"], "chg": qq.get("change_pct"), "rvol": _rvol(qq, now),
                             "dvol": _dvol(qq), "dir": "long"})
        try:
            importlib.import_module(f"{__package__}.lab.learn").record("mikro", now, rows)
        except Exception:  # noqa: BLE001
            log.exception("micro learn record failed")
    # entries
    acct = _account()
    pos = _positions()
    c = db.conn()
    day0 = int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp())
    bought = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM micro_orders WHERE side='buy' AND created_at>=?", (day0,))}
    eq = float(acct["cash"]) + sum(p["qty"] * float(p.get("last_price") or p["avg_price"]) for p in pos)
    day_start = float(acct.get("day_start_equity") or eq)
    stopped = s["day_stop"] > 0 and minute >= 600 and eq < day_start * (1 - s["day_stop"] / 100)
    if (not stopped and len(pos) < MAX_POSITIONS and len(bought) < s["max_trades"] and acct["cash"] >= MIN_CASH_TO_BUY
            and _hhmm(s["start"]) <= minute <= _hhmm(s["last"]) and now < close_ts - FLATTEN_BEFORE_CLOSE):
        held = {p["symbol"] for p in pos}
        for x in caps:
            sym = x["symbol"]
            qq = q.get(sym)
            if sym in held or sym in bought or (d.isoformat(), sym) in _state["failed"] or not _fresh(qq, now):
                continue
            if not candidate_ok(qq, s, now):
                continue
            px, chg, rv, dv = float(qq["price"]), qq.get("change_pct"), _rvol(qq, now), _dvol(qq)
            # a cent of headroom: the fill and cost are rounded, which must not push the cost above cash / the cap
            budget = min(float(acct["cash"]), MAX_ALLOC_PER_TRADE) - 0.01
            qty = math.floor(budget / (px * (1 + _slip(px, dv))))
            if qty < 1:
                continue
            try:
                place_order({"symbol": sym, "side": "buy", "qty": qty, "origin": "small_cap_bot", "_quote": qq,
                             "note": f"Küçük hisse botu: değişim {_pct(float(chg))}, fiyat {_usd(px)}"
                                     + (f", göreli hacim {rv:.1f}x" if rv else "")})
            except Exception as e:  # noqa: BLE001 - try the next candidate instead of blocking on this one
                _log("warn", sym, f"Alış yapılamadı: {e}", now)
                _state["failed"].add((d.isoformat(), sym))   # not retried today (the scan repeats every 15 s)
                continue
            break
    live = {f"{p['symbol']}:{p['opened_at']}" for p in _positions()}
    for k in [k for k in _state["marks"] if k.rsplit(":cut", 1)[0] not in live]:
        _state["marks"].pop(k, None)
    if _state["marks"] != marks_before:
        _save_marks()
    _record_equity(now)
    return {"session": "open", "positions": len(_positions())}


def _hhmm(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def reset() -> dict:
    """Reset the $100 micro account."""
    init_db()
    with db._tx() as c:
        c.execute("UPDATE micro_account SET cash=?, start_cash=?, day_start_equity=?, day_date=NULL WHERE id=1",
                  (START_CASH, START_CASH, START_CASH))
        for t in ("micro_positions", "micro_orders", "micro_equity", "micro_log"):
            c.execute(f"DELETE FROM {t}")
    _log("info", "", "Mikro sanal hesap (100 $) sıfırlandı.")
    return summary()
