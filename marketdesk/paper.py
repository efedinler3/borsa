"""Paper trading: a simulated US-equity account inside the app. Nothing is ever sent to a broker.

Orders fill against Yahoo prices (the same source the dashboard uses) with a slippage/commission model; positions,
cash, realized/unrealized P&L, an equity curve and a decision log live in SQLite (db.py paper_* tables). A small
rule-based bot (no AI) can trade the app's own opportunity scores under hard risk limits. The scheduler calls
tick() every few seconds; the HTTP API calls the public functions below.

Simplifications (shown to the user): fills happen at the last trade price +/- slippage, there is no order book,
no partial fills, no borrow fees for shorts and whole shares only.
"""
from __future__ import annotations

import importlib
import logging
import math
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from . import db
from .config import load_config, save_config
from .util import ET, close_minutes, is_trading_day, market_session, now_ts

log = logging.getLogger("marketdesk.paper")

SETTINGS_DEFAULTS: dict = {
    "start_cash": 100000.0,
    "slippage_bps": 5.0,            # adverse fill vs last price for market/stop orders (5 bps = %0,05)
    "commission_per_share": 0.0,
    "min_commission": 0.0,
    "max_position_pct": 20.0,       # one symbol's notional vs equity
    "max_positions": 5,
    "max_leverage": 1.0,            # gross exposure / equity (1 = no margin)
    "daily_loss_limit_pct": 3.0,    # new openings blocked (and bot positions closed) below this day loss
    "allow_short": False,
    "bot_enabled": False,
    "bot_min_score": 60.0,
    "bot_min_change_pct": 2.0,      # momentum filter on today's move (absolute %)
    "bot_max_change_pct": 15.0,     # ... but not an exhausted/halting runner
    "bot_min_rel_volume": 1.5,      # time-of-day adjusted relative volume
    "bot_min_price": 5.0,
    "bot_risk_per_trade_pct": 0.5,  # equity at risk between entry and stop
    "bot_stop_loss_pct": 2.0,
    "bot_take_profit_pct": 4.0,
    "bot_max_trades_per_day": 5,
    "bot_start_et": "09:45",
    "bot_last_entry_et": "15:30",
    "bot_direction_mixed": False,   # also buy "mixed" opportunities (measured as long)
    "bot_min_dollar_volume": 20_000_000.0,   # 3-month average daily $ volume: thin stocks fill far from the quote
    "bot_cut10": False,             # 10 minutes after entry at -0.8 % or worse: exit
    "bot_cull30": False,            # at every half hour of a position's age: exit unless it is at least +0.2 %
    "bot_lock_pct": 0.0,            # once this % in profit, the stop moves to entry + 0.2 % (0 = off)
    "bot_day_stop_pct": 0.0,        # at a half-hour check, the bot's day P&L below -X % of equity: no new entries today
    "eod_flatten": True,            # bot positions are closed 5 minutes before the close
    "hakem_enabled": False,         # test lab referee places its picks at the open (marketdesk/lab/runner.py)
    "hakem_max_trades": 3,
    "evo_enabled": False,           # evolution tournament bots (lab/evo_live.py): enter at the open, hold up to 5 days
    "evo_max_new": 5,               # new evolution positions per day
    "evo_position_pct": 10.0,       # each one this % of equity
    "evo_max_positions": 10,        # evolution positions open at once (counted apart from the other bots)
}
# key -> (min, max, type)
_LIMITS = {
    "start_cash": (1000, 10_000_000, float), "slippage_bps": (0, 200, float), "commission_per_share": (0, 1, float),
    "min_commission": (0, 50, float), "max_position_pct": (1, 100, float), "max_positions": (1, 50, int),
    "max_leverage": (0.1, 4, float), "daily_loss_limit_pct": (0.1, 50, float), "bot_min_score": (0, 100, float),
    "bot_min_change_pct": (0, 50, float), "bot_max_change_pct": (0.5, 200, float), "bot_min_rel_volume": (0, 20, float),
    "bot_min_price": (0, 1000, float), "bot_risk_per_trade_pct": (0.05, 5, float), "bot_stop_loss_pct": (0.2, 20, float),
    "bot_take_profit_pct": (0.2, 50, float), "bot_max_trades_per_day": (0, 50, int), "hakem_max_trades": (0, 10, int),
    "evo_max_new": (0, 20, int), "evo_position_pct": (1, 50, float), "evo_max_positions": (1, 50, int),
    "bot_min_dollar_volume": (0, 1e11, float), "bot_lock_pct": (0, 20, float), "bot_day_stop_pct": (0, 20, float),
}
_BOOLS = ("allow_short", "bot_enabled", "eod_flatten", "hakem_enabled", "evo_enabled", "bot_direction_mixed", "bot_cut10",
          "bot_cull30")
AUTO_ORIGINS = ("bot", "hakem")   # positions opened automatically: closed before the close and on the daily loss limit
_HHMM = ("bot_start_et", "bot_last_entry_et")

TICK_EVERY = 15             # scheduler period (s)
QUOTE_TTL = 10              # price cache (s)
STALE_REGULAR = 15 * 60     # no fill on a regular-session price older than this (halt, bad symbol)
EQUITY_EVERY_SESSION = 300
EQUITY_EVERY_CLOSED = 3600
FLATTEN_BEFORE_CLOSE = 5 * 60
BOT_CANDIDATES = 15
LOG_KEEP = 3000
EQUITY_KEEP_DAYS = 400

ORIGINS_TR = {"manual": "Elle", "bot": "Bot", "hakem": "Hakem bot", "evrim": "Evrim botu", "stop": "Zarar durdur",
              "target": "Kâr al", "eod": "Gün sonu kapanış", "risk": "Risk limiti", "hold": "Tutma süresi doldu",
              "cull": "30 dk eleme (eski)", "cull30": "30 dk eleme", "cut10": "10 dk erken kesme", "lock": "Kâr kilidi (başabaş)"}
STATUS_TR = {"open": "Bekliyor", "filled": "Gerçekleşti", "cancelled": "İptal", "rejected": "Reddedildi",
             "expired": "Süresi doldu"}
TYPES_TR = {"market": "Piyasa", "limit": "Limit", "stop": "Stop"}
SIDES_TR = {"buy": "Al", "sell": "Sat"}

_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_lock = threading.RLock()
_quote_cache: dict[str, tuple[float, dict]] = {}
_quote_lock = threading.Lock()   # quotes() also runs outside _lock (micro engine, HTTP quote endpoint)
_state = {"last_equity": 0, "last_prune": 0, "last_error": None, "last_tick": 0}


class PaperError(Exception):
    """User-facing (Turkish) rejection."""


# ---------------------------------------------------------------- settings

def settings(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    raw = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    out = dict(SETTINGS_DEFAULTS)
    for k, v in raw.items():
        if k in out:
            out[k] = v
    return _validate(out)


def _validate(s: dict) -> dict:
    out = dict(s)
    for k, (lo, hi, typ) in _LIMITS.items():
        try:
            v = typ(float(out.get(k, SETTINGS_DEFAULTS[k])))
        except (TypeError, ValueError):
            v = SETTINGS_DEFAULTS[k]
        if isinstance(v, float) and not math.isfinite(v):
            v = SETTINGS_DEFAULTS[k]
        out[k] = min(hi, max(lo, v))
    for k in _BOOLS:
        v = out.get(k)
        out[k] = v.strip().lower() in ("1", "true", "evet", "on") if isinstance(v, str) else bool(v)
    for k in _HHMM:
        v = str(out.get(k) or "")
        out[k] = v if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) else SETTINGS_DEFAULTS[k]
    if out["bot_max_change_pct"] <= out["bot_min_change_pct"]:
        out["bot_max_change_pct"] = out["bot_min_change_pct"] + 1
    return out


def update_settings(updates: dict) -> dict:
    with _lock:
        cur = settings()
        for k, v in (updates or {}).items():
            if k in SETTINGS_DEFAULTS and k != "start_cash":   # start_cash changes only through reset()
                cur[k] = v
        cur = _validate(cur)
        save_config({"paper": cur})
        if updates and "bot_enabled" in updates:
            _log("info", "", "Bot " + ("açıldı" if cur["bot_enabled"] else "kapatıldı"))
        if updates and "hakem_enabled" in updates:
            _log("info", "", "Hakem bot " + ("açıldı" if cur["hakem_enabled"] else "kapatıldı"))
        if updates and "evo_enabled" in updates:
            _log("info", "", "Evrim botu " + ("açıldı" if cur["evo_enabled"] else "kapatıldı"))
        return cur


# ---------------------------------------------------------------- time helpers

def _at_et(d: date, minutes: int) -> int:
    return int(datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET).timestamp())


def _et_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, ET).date()


def _next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def _close_ts(d: date) -> int:
    return _at_et(d, close_minutes(d))


def _order_expiry(now: int, ext: bool) -> int:
    """End of the session a day order belongs to: today's close (+4 h with extended hours) while it is still
    ahead, otherwise the next trading day's."""
    d = _et_date(now)
    extra = 4 * 3600 if ext else 0
    if is_trading_day(d) and now < _close_ts(d) + extra:
        return _close_ts(d) + extra
    return _close_ts(_next_trading_day(d)) + extra


def hold_exit_ts(now: int, days: int) -> int:
    """Close of the `days`-th trading day of a position opened now (today counts as day 1 when it trades)."""
    d = _et_date(now)
    if not is_trading_day(d) or now >= _close_ts(d):
        d = _next_trading_day(d)
    for _ in range(max(1, int(days)) - 1):
        d = _next_trading_day(d)
    return _close_ts(d)


def _hhmm_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- prices

def _market():
    try:
        return importlib.import_module(f"{__package__}.collectors.market")
    except Exception:  # noqa: BLE001
        log.exception("collectors.market unavailable")
        return None


def _parse_v7(q: dict, market) -> tuple[str, dict] | None:
    sym = market._app_symbol(str(q.get("symbol") or "")) if hasattr(market, "_app_symbol") else q.get("symbol")
    reg = q.get("regularMarketPrice")
    if not sym or reg is None:
        return None
    reg_t = int(q.get("regularMarketTime") or 0)
    best, best_t, kind = float(reg), reg_t, "regular"
    for k in ("pre", "post"):
        px, t = q.get(f"{k}MarketPrice"), int(q.get(f"{k}MarketTime") or 0)
        if px and t > best_t:
            best, best_t, kind = float(px), t, k
    vol, avg = q.get("regularMarketVolume"), q.get("averageDailyVolume3Month")
    return sym, {"price": best, "ts": best_t or None, "kind": kind, "regular": float(reg),
                 "change_pct": q.get("regularMarketChangePercent"), "prev_close": q.get("regularMarketPreviousClose"),
                 "volume": vol, "avg_volume": avg, "name": q.get("shortName") or q.get("longName") or sym}


def quotes(symbols: list[str]) -> dict[str, dict]:
    """Latest price per symbol (cached QUOTE_TTL s): Yahoo v7 batch, per-symbol chart as a fallback."""
    syms = sorted({s for s in symbols if s})
    now = time.time()
    with _quote_lock:
        cached = {s: _quote_cache.get(s) for s in syms}
    out = {s: c[1] for s, c in cached.items() if c and now - c[0] < QUOTE_TTL}
    need = [s for s in syms if s not in out]
    if not need:
        return out
    market = _market()
    if market is None:
        return out
    got: dict[str, dict] = {}
    try:
        for q in market.v7_quotes(need):
            p = _parse_v7(q, market)
            if p:
                got[p[0]] = p[1]
    except Exception as e:  # noqa: BLE001 - fall back to the chart endpoint
        log.info("paper v7 quotes failed: %s", e)
    for s in [s for s in need if s not in got][:10]:
        try:
            ysym = market._yahoo_symbol(s) if hasattr(market, "_yahoo_symbol") else s
            q = market.parse_chart_quote(market._chart(ysym), group="izleme")
            got[s] = {"price": float(q["price"]), "ts": q.get("last_ts"), "kind": "chart", "regular": q.get("regular_price"),
                      "change_pct": q.get("change_pct"), "prev_close": q.get("prev_close"), "volume": q.get("volume"),
                      "avg_volume": None, "name": q.get("name") or s}
        except Exception as e:  # noqa: BLE001 - unknown symbol / Yahoo down
            log.info("paper chart quote %s failed: %s", s, e)
    with _quote_lock:
        for s, q in got.items():
            _quote_cache[s] = (now, q)
        if len(_quote_cache) > 500:
            for k, _ in sorted(list(_quote_cache.items()), key=lambda kv: kv[1][0])[:200]:
                _quote_cache.pop(k, None)
    out.update(got)
    return out


def _quote(sym: str) -> dict | None:
    return quotes([sym]).get(sym)


def _usable(q: dict | None, session: str, now: int) -> bool:
    """A price good enough to fill against in the current session."""
    if not q or not q.get("price") or q["price"] <= 0:
        return False
    if session == "open" and q.get("ts") and now - int(q["ts"]) > STALE_REGULAR:
        return False
    return True


# ---------------------------------------------------------------- storage helpers

def _log(level: str, symbol: str, msg: str, ts: int | None = None) -> None:
    try:
        with db._tx() as c:
            c.execute("INSERT INTO paper_log(ts, level, symbol, msg) VALUES (?,?,?,?)",
                      (ts or now_ts(), level, symbol or "", msg[:500]))
    except Exception:  # noqa: BLE001 - the log must never break trading
        log.exception("paper log write failed")


def _account(c=None) -> dict:
    c = c or db.conn()
    r = c.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    if r is None:
        start = float(settings()["start_cash"])
        with db._tx() as w:
            w.execute("INSERT OR IGNORE INTO paper_account(id, cash, start_cash, created_at) VALUES (1,?,?,?)",
                      (start, start, now_ts()))
        r = db.conn().execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    return dict(r)


def _positions(c=None) -> dict[str, dict]:
    c = c or db.conn()
    return {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM paper_positions ORDER BY opened_at")}


def _open_orders(c=None) -> list[dict]:
    c = c or db.conn()
    return [dict(r) for r in c.execute("SELECT * FROM paper_orders WHERE status='open' ORDER BY created_at")]


def _mark(p: dict) -> float:
    return float(p.get("last_price") or p["avg_price"])


def _valuation(acct: dict, pos: dict[str, dict]) -> dict:
    value = sum(p["qty"] * _mark(p) for p in pos.values())
    gross = sum(abs(p["qty"]) * _mark(p) for p in pos.values())
    unreal = sum(p["qty"] * (_mark(p) - p["avg_price"]) for p in pos.values())
    equity = acct["cash"] + value
    return {"equity": equity, "positions_value": value, "gross": gross, "unrealized": unreal}


def _usd(x: float, d: int = 2, sign: bool = False) -> str:
    """Turkish money text: 12.345,67 $ (log and rejection messages)."""
    t = f"{x:{'+' if sign else ''},.{d}f}"
    return t.replace(",", "#").replace(".", ",").replace("#", ".") + " $"


def _pct(v: float) -> str:
    """Turkish percent text: +%3,25 / -%1,40."""
    return ("+" if v > 0 else "-" if v < 0 else "") + "%" + f"{abs(v):.2f}".replace(".", ",")


def _fee(qty: float, s: dict) -> float:
    if not s["commission_per_share"] and not s["min_commission"]:
        return 0.0
    return round(max(s["min_commission"], abs(qty) * s["commission_per_share"]), 4)


# ---------------------------------------------------------------- risk

def _opening_qty(cur_qty: float, signed: float) -> float:
    """Shares of an order that open/increase exposure (the rest closes the existing position)."""
    if cur_qty == 0 or (cur_qty > 0) == (signed > 0):
        return abs(signed)
    return max(0.0, abs(signed) - abs(cur_qty))


def _check_risk(sym: str, signed: float, price: float, s: dict, acct: dict, pos: dict[str, dict], now: int,
                origin: str | None = None) -> None:
    """Raise PaperError when an order that opens exposure breaks a limit. Closing orders always pass."""
    p = pos.get(sym)
    cur = p["qty"] if p else 0.0
    if _opening_qty(cur, signed) <= 0:
        return
    if acct.get("halted_date") == _et_date(now).isoformat():
        raise PaperError(f"Günlük zarar limiti (%{s['daily_loss_limit_pct']:g}) aşıldı; bugün yeni pozisyon açılamaz")
    new_qty = cur + signed
    if new_qty < 0 and not s["allow_short"]:
        raise PaperError("Açığa satış kapalı (Ayarlar: açığa satışa izin ver)")
    if not p:
        if origin == "evrim":   # the evolution bot has its own position budget
            n_evo = sum(1 for x in pos.values() if x.get("origin") == "evrim")
            if n_evo >= s["evo_max_positions"]:
                raise PaperError(f"Evrim botu en fazla {s['evo_max_positions']} pozisyon açabilir")
        elif sum(1 for x in pos.values() if x.get("origin") != "evrim") >= s["max_positions"]:
            raise PaperError(f"En fazla {s['max_positions']} açık pozisyon")
    val = _valuation(acct, pos)
    equity = val["equity"]
    if equity <= 0:
        raise PaperError("Özsermaye sıfırın altında")
    notional = abs(new_qty) * price
    if notional > equity * s["max_position_pct"] / 100 + 1e-6:
        raise PaperError(f"Pozisyon büyüklüğü limiti: tek hissede en fazla "
                         f"{_usd(equity * s['max_position_pct'] / 100, 0)} (özsermaye × %{s['max_position_pct']:g})")
    gross = val["gross"] - (abs(cur) * _mark(p) if p else 0) + notional
    if gross > equity * s["max_leverage"] + 1e-6:
        raise PaperError(f"Alım gücü yetersiz (toplam pozisyon özsermayenin {s['max_leverage']:g} katını aşamaz)")


# ---------------------------------------------------------------- execution

def _apply_fill(c, order: dict, price: float, now: int, s: dict, origin_pos: str | None = None) -> float:
    """Book a fill: cash, position (avg cost, flips, realized P&L), order row. Returns realized P&L."""
    sym, signed = order["symbol"], order["qty"] if order["side"] == "buy" else -order["qty"]
    fee = _fee(order["qty"], s)
    row = c.execute("SELECT * FROM paper_positions WHERE symbol=?", (sym,)).fetchone()
    p = dict(row) if row else None
    cur, avg = (p["qty"], p["avg_price"]) if p else (0.0, 0.0)
    new = cur + signed
    realized = 0.0
    if cur != 0 and (cur > 0) != (signed > 0):
        closed = min(abs(signed), abs(cur))
        realized = closed * (price - avg) * (1 if cur > 0 else -1)
    realized -= fee
    c.execute("UPDATE paper_account SET cash = cash - ? WHERE id=1", (signed * price + fee,))
    if abs(new) < 1e-9:
        c.execute("DELETE FROM paper_positions WHERE symbol=?", (sym,))
    elif cur == 0 or (cur > 0) != (new > 0):
        # new position, or flipped through zero: the remainder is opened at this price
        sl, tp = order.get("stop_loss_pct"), order.get("take_profit_pct")
        long_ = new > 0
        stop = round(price * (1 - sl / 100), 4) if sl and long_ else round(price * (1 + sl / 100), 4) if sl else None
        tgt = round(price * (1 + tp / 100), 4) if tp and long_ else round(price * (1 - tp / 100), 4) if tp else None
        exit_at = hold_exit_ts(now, order["hold_days"]) if order.get("hold_days") else None
        c.execute("INSERT OR REPLACE INTO paper_positions(symbol, qty, avg_price, opened_at, stop_price, target_price, origin,"
                  " last_price, last_ts, exit_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (sym, new, price, now, stop, tgt, origin_pos or order["origin"], price, now, exit_at))
    elif abs(new) > abs(cur):
        new_avg = (abs(cur) * avg + abs(signed) * price) / abs(new)
        c.execute("UPDATE paper_positions SET qty=?, avg_price=?, last_price=?, last_ts=? WHERE symbol=?",
                  (new, new_avg, price, now, sym))
    else:
        c.execute("UPDATE paper_positions SET qty=?, last_price=?, last_ts=? WHERE symbol=?", (new, price, now, sym))
    is_close = cur != 0 and (cur > 0) != (signed > 0)
    c.execute("UPDATE paper_orders SET status='filled', filled_at=?, fill_price=?, fee=?, realized_pnl=? WHERE id=?",
              (now, price, fee, round(realized, 4) if is_close else None, order["id"]))
    return realized if is_close else 0.0


def liquidity_bps(q: dict | None) -> float | None:
    """Per-side slippage implied by the 3-month average daily dollar volume (as in the test lab): 3 bps for the most
    liquid stocks up to 20 bps for thin ones; None when the volume is unknown."""
    try:
        dv = float(q["avg_volume"]) * float(q["price"])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(dv) or dv <= 0:
        return None
    return 3.0 if dv >= 1e8 else 5.0 if dv >= 2e7 else 10.0 if dv >= 5e6 else 20.0


def _fill_price(order: dict, q: dict, s: dict) -> float | None:
    """Price this order fills at now, or None when its condition is not met. Market and stop orders pay the larger of
    the configured slippage and the stock's liquidity-based slippage."""
    last = float(q["price"])
    buy = order["side"] == "buy"
    slip = last * max(s["slippage_bps"], liquidity_bps(q) or 0.0) / 10000
    if order["type"] == "market":
        return round(last + slip if buy else last - slip, 4)
    if order["type"] == "limit":
        lim = float(order["limit_price"])
        if buy and last <= lim:
            return round(min(last, lim), 4)
        if not buy and last >= lim:
            return round(max(last, lim), 4)
        return None
    if order["type"] == "stop":
        stp = float(order["stop_price"])
        if (buy and last >= stp) or (not buy and last <= stp):
            return round(last + slip if buy else last - slip, 4)
    return None


def _can_fill_now(order: dict, session: str) -> bool:
    if session == "open":
        return True
    # extended hours: limit orders flagged ext_hours only (as at real brokers)
    return bool(order.get("ext_hours")) and order["type"] == "limit" and session in ("premarket", "afterhours")


def _try_fill(order: dict, q: dict | None, session: str, now: int, s: dict) -> bool:
    """Fill one open order if its session/price conditions hold. Re-checks risk for opening fills."""
    if not _can_fill_now(order, session) or not _usable(q, session, now):
        return False
    price = _fill_price(order, q, s)
    if price is None:
        return False
    rejected = None
    with db._tx() as c:
        acct, pos = _account(c), _positions(c)
        signed = order["qty"] if order["side"] == "buy" else -order["qty"]
        try:
            _check_risk(order["symbol"], signed, price, s, acct, pos, now, order.get("origin"))
        except PaperError as e:
            c.execute("UPDATE paper_orders SET status='rejected', note=? WHERE id=?", (str(e), order["id"]))
            rejected = e
        else:
            realized = _apply_fill(c, order, price, now, s)
    if rejected is not None:   # logged after the commit: _log opens its own transaction
        _log("warn", order["symbol"], f"Emir reddedildi: {rejected}")
        return False
    msg = (f"{SIDES_TR[order['side']]} {order['qty']:g} @ {_usd(price)} ({TYPES_TR[order['type']]}, "
           f"{ORIGINS_TR.get(order['origin'], order['origin'])})")
    if realized:
        msg += f" · gerçekleşen K/Z {_usd(realized, sign=True)}"
    _log("trade", order["symbol"], msg, now)
    return True


def _new_order(sym: str, side: str, qty: float, typ: str, now: int, *, limit_price=None, stop_price=None,
               tif: str = "day", ext: bool = False, origin: str = "manual", note: str = "",
               stop_loss_pct=None, take_profit_pct=None, hold_days: int | None = None) -> dict:
    order = {"id": uuid.uuid4().hex[:16], "created_at": now, "symbol": sym, "side": side, "qty": float(qty), "type": typ,
             "limit_price": limit_price, "stop_price": stop_price, "tif": tif, "ext_hours": 1 if ext else 0,
             "expires_at": None if tif == "gtc" else _order_expiry(now, ext), "status": "open", "origin": origin,
             "note": note, "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct, "hold_days": hold_days}
    with db._tx() as c:
        c.execute("INSERT INTO paper_orders(id, created_at, symbol, side, qty, type, limit_price, stop_price, tif, ext_hours,"
                  " expires_at, status, origin, note, stop_loss_pct, take_profit_pct, hold_days) VALUES"
                  " (:id,:created_at,:symbol,:side,:qty,:type,:limit_price,:stop_price,:tif,:ext_hours,:expires_at,:status,"
                  " :origin,:note,:stop_loss_pct,:take_profit_pct,:hold_days)", order)
    return order


# ---------------------------------------------------------------- public API (HTTP)

def _num(v, name: str, lo: float = 0.0) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        raise PaperError(f"Geçersiz değer: {name}")
    if not math.isfinite(x) or x <= lo:
        raise PaperError(f"Geçersiz değer: {name}")
    return x


def place_order(data: dict, origin: str = "manual") -> dict:
    """Validate and create an order; a market order fills at once when the market is open and a price is known."""
    sym = str(data.get("symbol") or "").strip().upper().lstrip("$")
    if not _SYM_RE.match(sym):
        raise PaperError("Geçersiz sembol")
    side = str(data.get("side") or "").lower()
    if side not in ("buy", "sell"):
        raise PaperError("Yön 'al' ya da 'sat' olmalı")
    typ = str(data.get("type") or "market").lower()
    if typ not in TYPES_TR:
        raise PaperError("Geçersiz emir türü")
    tif = "gtc" if str(data.get("tif") or "day").lower() == "gtc" else "day"
    ext = bool(data.get("ext_hours")) and typ == "limit"
    limit_price = _num(data.get("limit_price"), "limit fiyatı") if typ == "limit" else None
    stop_price = _num(data.get("stop_price"), "stop fiyatı") if typ == "stop" else None
    if typ == "limit" and limit_price is None:
        raise PaperError("Limit emri için fiyat girin")
    if typ == "stop" and stop_price is None:
        raise PaperError("Stop emri için tetik fiyatı girin")
    sl = _num(data.get("stop_loss_pct"), "zarar durdur %")
    tp = _num(data.get("take_profit_pct"), "kâr al %")
    if sl is not None and sl >= 50 or tp is not None and tp >= 500:
        raise PaperError("Zarar durdur / kâr al yüzdesi çok büyük")
    try:
        hold_days = int(data.get("hold_days") or 0) or None
    except (TypeError, ValueError):
        raise PaperError("Geçersiz tutma süresi")
    if hold_days is not None and not 1 <= hold_days <= 20:
        raise PaperError("Tutma süresi 1–20 işlem günü olmalı")
    s = settings()
    now = now_ts()
    session = market_session(now)
    with _lock:
        q = _quote(sym)
        ref = limit_price or stop_price or (q or {}).get("price")
        if not ref:
            raise PaperError(f"{sym} için fiyat alınamadı (sembolü kontrol edin)")
        qty = _num(data.get("qty"), "adet")
        notional = _num(data.get("notional"), "tutar")
        if qty is None and notional is not None:
            qty = math.floor(notional / ref)
        if qty is None or qty < 1:
            raise PaperError("Adet en az 1 olmalı (tam hisse)")
        qty = float(math.floor(qty))
        signed = qty if side == "buy" else -qty
        acct, pos = _account(), _positions()
        _check_risk(sym, signed, float(ref), s, acct, pos, now, origin)
        order = _new_order(sym, side, qty, typ, now, limit_price=limit_price, stop_price=stop_price, tif=tif, ext=ext,
                           origin=origin, note=str(data.get("note") or "")[:200], stop_loss_pct=sl, take_profit_pct=tp,
                           hold_days=hold_days)
        _log("info", sym, f"Emir: {SIDES_TR[side]} {qty:g} {TYPES_TR[typ]}"
             + (f" @ {_usd(limit_price)}" if limit_price else f" tetik {_usd(stop_price)}" if stop_price else "")
             + (" (uzatılmış seans)" if ext else ""), now)
        filled = _try_fill(order, q, session, now, s)
        row = db.conn().execute("SELECT * FROM paper_orders WHERE id=?", (order["id"],)).fetchone()
        out = dict(row) if row else order
        if not filled and out["status"] == "open":
            out["pending_reason"] = _pending_reason(out, q, session, now)
        return out


def _pending_reason(order: dict, q: dict | None, session: str, now: int) -> str:
    if not _can_fill_now(order, session):
        if session in ("premarket", "afterhours") and order["type"] != "limit":
            return "Uzatılmış seansta yalnızca 'uzatılmış seans' işaretli limit emirleri gerçekleşir; emir açılışta işlenecek"
        return "Piyasa kapalı; emir seans açılınca işlenecek"
    if not _usable(q, session, now):
        return "Güncel fiyat yok; fiyat gelince işlenecek"
    return "Fiyat koşulu henüz oluşmadı"


def cancel_order(order_id: str) -> dict:
    with _lock, db._tx() as c:
        r = c.execute("SELECT * FROM paper_orders WHERE id=?", (str(order_id),)).fetchone()
        if r is None:
            raise PaperError("Emir bulunamadı")
        if r["status"] != "open":
            raise PaperError("Bu emir artık açık değil")
        c.execute("UPDATE paper_orders SET status='cancelled', note=? WHERE id=?", ("Kullanıcı iptal etti", r["id"]))
    _log("info", r["symbol"], f"Emir iptal edildi ({SIDES_TR[r['side']]} {r['qty']:g})")
    return {"ok": True}


def close_position(symbol: str | None = None, origin: str = "manual", only_origin: str | tuple | None = None) -> list[dict]:
    """Market order(s) flattening one position, or every position (`symbol` None). Open orders of those symbols are
    cancelled first so a pending order cannot reopen the position."""
    out = []
    with _lock:
        pos = _positions()
        allowed = (only_origin,) if isinstance(only_origin, str) else only_origin
        targets = [p for s_, p in pos.items() if (symbol is None or s_ == symbol) and (allowed is None or p["origin"] in allowed)]
        if symbol and not targets:
            raise PaperError("Bu sembolde açık pozisyon yok")
        for p in targets:
            with db._tx() as c:
                c.execute("UPDATE paper_orders SET status='cancelled', note='Pozisyon kapatıldı' WHERE status='open' AND symbol=?",
                          (p["symbol"],))
            out.append(place_order({"symbol": p["symbol"], "side": "sell" if p["qty"] > 0 else "buy", "qty": abs(p["qty"]),
                                    "type": "market", "note": ORIGINS_TR.get(origin, origin)}, origin=origin))
    return out


def set_position_levels(symbol: str, stop_price=None, target_price=None) -> dict:
    with _lock, db._tx() as c:
        r = c.execute("SELECT * FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
        if r is None:
            raise PaperError("Bu sembolde açık pozisyon yok")
        sp = _num(stop_price, "zarar durdur fiyatı") if stop_price not in (None, "") else None
        tp = _num(target_price, "kâr al fiyatı") if target_price not in (None, "") else None
        long_ = r["qty"] > 0
        mark = float(r["last_price"] or r["avg_price"])
        if sp and ((long_ and sp >= mark) or (not long_ and sp <= mark)):
            raise PaperError("Zarar durdur fiyatı mevcut fiyatın " + ("altında" if long_ else "üstünde") + " olmalı")
        if tp and ((long_ and tp <= mark) or (not long_ and tp >= mark)):
            raise PaperError("Kâr al fiyatı mevcut fiyatın " + ("üstünde" if long_ else "altında") + " olmalı")
        c.execute("UPDATE paper_positions SET stop_price=?, target_price=? WHERE symbol=?", (sp, tp, symbol))
    _log("info", symbol, f"Seviyeler: zarar durdur {sp if sp else '—'} · kâr al {tp if tp else '—'}")
    return {"ok": True}


def reset(start_cash=None) -> dict:
    with _lock:
        s = settings()
        start = _num(start_cash, "başlangıç bakiyesi") if start_cash not in (None, "") else s["start_cash"]
        lo, hi, _ = _LIMITS["start_cash"]
        start = min(hi, max(lo, float(start)))
        with db._tx() as c:
            for t in ("paper_orders", "paper_positions", "paper_equity", "paper_log"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM paper_account")
            c.execute("INSERT INTO paper_account(id, cash, start_cash, created_at) VALUES (1,?,?,?)", (start, start, now_ts()))
        s["start_cash"] = start
        save_config({"paper": s})
        _state["last_equity"] = 0
        _log("info", "", f"Hesap sıfırlandı · başlangıç bakiyesi {_usd(start, 0)}")
        return summary()


# ---------------------------------------------------------------- tick (scheduler)

def tick(cfg: dict | None = None, now: int | None = None) -> dict:
    """One engine step: day roll, expiries, fills, stops/targets, risk, end-of-day flatten, bot, equity point."""
    with _lock:
        now = now or now_ts()
        s = settings(cfg)
        session = market_session(now)
        acct = _account()
        pos, orders = _positions(), _open_orders()
        today = _et_date(now)
        _expire(orders, now)
        orders = [o for o in orders if not o.get("expires_at") or now < o["expires_at"]]
        bot_on = s["bot_enabled"] and session == "open"
        cands = _bot_candidates(s, pos, now) if bot_on else []
        rec = _learn_candidates(now) if session == "open" else []
        need = {o["symbol"] for o in orders} | set(pos) | {o["ticker"] for o in rec}
        if session != "closed":
            need |= {c_["ticker"] for c_ in cands}
        prices = quotes(sorted(need)) if need and session != "closed" else {}
        if prices:
            _mark_positions(prices, now)
        _learn_record(rec, prices, now)
        _day_roll(acct, today, now)
        for o in orders:
            try:
                _try_fill(o, prices.get(o["symbol"]), session, now, s)
            except Exception:  # noqa: BLE001 - one bad order must not stop the others
                log.exception("paper fill failed for %s", o.get("id"))
        if session == "open":
            _stops_targets(prices, now, s)
            _bot_exits(prices, now, s)
            _hold_expiry(now)
            _daily_loss(now, s)
            _bot_day_stop(now, s, today)
            if s["eod_flatten"] and now >= _close_ts(today) - FLATTEN_BEFORE_CLOSE:
                if any(p["origin"] in AUTO_ORIGINS for p in _positions().values()):
                    if _state.get("eod_logged") != today.isoformat():   # retried every tick until filled; log once
                        _state["eod_logged"] = today.isoformat()
                        _log("info", "", "Gün sonu: bot ve hakem pozisyonları kapatılıyor")
                    close_position(None, origin="eod", only_origin=AUTO_ORIGINS)
        if bot_on:
            _bot_entries(s, cands, prices, now, today)
        _record_equity(session, now)
        if now - _state["last_prune"] > 6 * 3600:
            _prune(now)
        _state["last_tick"] = now
        return {"session": session, "orders": len(orders), "positions": len(_positions()), "priced": len(prices)}


def _expire(orders: list[dict], now: int) -> None:
    gone = [o for o in orders if o.get("expires_at") and now >= o["expires_at"]]
    if not gone:
        return
    with db._tx() as c:
        for o in gone:
            c.execute("UPDATE paper_orders SET status='expired', note='Seans sonu (günlük emir)' WHERE id=?", (o["id"],))
    for o in gone:
        _log("info", o["symbol"], f"Emrin süresi doldu ({SIDES_TR[o['side']]} {o['qty']:g} {TYPES_TR[o['type']]})", now)


def _mark_positions(prices: dict, now: int) -> None:
    with db._tx() as c:
        for sym, q in prices.items():
            if q and q.get("price"):
                c.execute("UPDATE paper_positions SET last_price=?, last_ts=? WHERE symbol=?", (q["price"], q.get("ts") or now, sym))


def _day_roll(acct: dict, today: date, now: int) -> None:
    if not is_trading_day(today) or acct.get("day_date") == today.isoformat():
        return
    # the first tick of a new trading day (from 04:00 ET) fixes the day's starting equity
    if market_session(now) == "closed":
        return
    eq = _valuation(_account(), _positions())["equity"]
    with db._tx() as c:
        c.execute("UPDATE paper_account SET day_date=?, day_start_equity=? WHERE id=1", (today.isoformat(), eq))


def _stops_targets(prices: dict, now: int, s: dict) -> None:
    for sym, p in _positions().items():
        q = prices.get(sym)
        if not _usable(q, "open", now):
            continue
        last, long_ = float(q["price"]), p["qty"] > 0
        hit = None
        if p.get("stop_price") and ((long_ and last <= p["stop_price"]) or (not long_ and last >= p["stop_price"])):
            locked = (long_ and p["stop_price"] >= p["avg_price"]) or (not long_ and p["stop_price"] <= p["avg_price"])
            hit = "lock" if locked else "stop"
        elif p.get("target_price") and ((long_ and last >= p["target_price"]) or (not long_ and last <= p["target_price"])):
            hit = "target"
        if hit:
            _log("info", sym, f"{ORIGINS_TR[hit]} tetiklendi @ {_usd(last)}", now)
            try:
                close_position(sym, origin=hit)
            except PaperError as e:
                _log("warn", sym, f"Kapatılamadı: {e}", now)


def _hold_expiry(now: int) -> None:
    """Multi-day positions (evolution bot) are closed in the last minutes of their final day, like the backtest's
    exit at that day's close."""
    for sym, p in _positions().items():
        if p.get("exit_at") and now >= int(p["exit_at"]) - FLATTEN_BEFORE_CLOSE:
            _log("info", sym, "Tutma süresi doldu: pozisyon kapatılıyor", now)
            try:
                close_position(sym, origin="hold")
            except PaperError as e:
                _log("warn", sym, f"Kapatılamadı: {e}", now)


def _daily_loss(now: int, s: dict) -> None:
    acct = _account()
    today = _et_date(now).isoformat()
    if acct.get("halted_date") == today or acct.get("day_date") != today or not acct.get("day_start_equity"):
        return
    eq = _valuation(acct, _positions())["equity"]
    start = float(acct["day_start_equity"])
    if eq <= start * (1 - s["daily_loss_limit_pct"] / 100):
        with db._tx() as c:
            c.execute("UPDATE paper_account SET halted_date=? WHERE id=1", (today,))
        _log("warn", "", f"Günlük zarar limiti aşıldı ({_pct((eq / start - 1) * 100)}): yeni pozisyon açılmayacak, "
             "bot pozisyonları kapatılıyor", now)
        close_position(None, origin="risk", only_origin=AUTO_ORIGINS)


def _record_equity(session: str, now: int) -> None:
    every = EQUITY_EVERY_SESSION if session != "closed" else EQUITY_EVERY_CLOSED
    if now - _state["last_equity"] < every:
        return
    last = db.conn().execute("SELECT MAX(ts) FROM paper_equity").fetchone()[0] or 0
    if now - last < every:
        _state["last_equity"] = last
        return
    acct, pos = _account(), _positions()
    v = _valuation(acct, pos)
    with db._tx() as c:
        c.execute("INSERT OR REPLACE INTO paper_equity(ts, equity, cash, positions_value) VALUES (?,?,?,?)",
                  (now, round(v["equity"], 2), round(acct["cash"], 2), round(v["positions_value"], 2)))
    _state["last_equity"] = now


def _prune(now: int) -> None:
    with db._tx() as c:
        c.execute("DELETE FROM paper_equity WHERE ts < ?", (now - EQUITY_KEEP_DAYS * 86400,))
        c.execute("DELETE FROM paper_log WHERE id <= (SELECT MAX(id) FROM paper_log) - ?", (LOG_KEEP,))
    _state["last_prune"] = now


# ---------------------------------------------------------------- bot (rule-based, no AI)

def _bot_candidates(s: dict, pos: dict, now: int) -> list[dict]:
    """Top opportunity rows passing the score/direction filters (prices are checked later, live)."""
    data, ts = (db.get_snapshot("opportunities") or (None, 0))
    if not isinstance(data, dict) or now - int(ts or 0) > 15 * 60:
        return []
    out = []
    for o in data.get("items") or []:
        if not isinstance(o, dict) or not o.get("ticker"):
            continue
        d = o.get("direction")
        if float(o.get("score") or 0) < s["bot_min_score"] or o["ticker"] in pos:
            continue
        if d == "long" or (d == "mixed" and s["bot_direction_mixed"]) or (d == "short" and s["allow_short"]):
            out.append(o)
        if len(out) >= BOT_CANDIDATES:
            break
    return out


# ---------------------------------------------------------------- live learning hooks (lab/learn.py)

LEARN_RECORD_EVERY = 60
LEARN_MIN_SCORE = 40.0
LEARN_MAX = 40


def _learn_candidates(now: int) -> list[dict]:
    """Once a minute: the opportunities every bot variant could buy (score >= 40, long or mixed), recorded with live
    prices so the variants can be replayed after the close."""
    if now - _state.get("last_learn_rec", 0) < LEARN_RECORD_EVERY or not is_trading_day(_et_date(now)):
        return []
    _state["last_learn_rec"] = now
    data, ts = (db.get_snapshot("opportunities") or (None, 0))
    if not isinstance(data, dict) or now - int(ts or 0) > 15 * 60:
        return []
    out = [o for o in data.get("items") or [] if isinstance(o, dict) and o.get("ticker")
           and o.get("direction") in ("long", "mixed") and float(o.get("score") or 0) >= LEARN_MIN_SCORE]
    return out[:LEARN_MAX]


def _learn_record(rec: list[dict], prices: dict, now: int) -> None:
    if not rec:
        return
    rows = []
    for o in rec:
        q = prices.get(o["ticker"])
        if not _usable(q, "open", now):
            continue
        try:
            dv = float(q["avg_volume"]) * float(q["price"]) if q.get("avg_volume") else None
        except (TypeError, ValueError):
            dv = None
        rows.append({"symbol": o["ticker"], "price": q["price"], "chg": q.get("change_pct"), "rvol": _rel_volume_adj(q, now),
                     "dvol": dv, "score": o.get("score"), "dir": o.get("direction")})
    try:
        importlib.import_module(f"{__package__}.lab.learn").record("firsat", now, rows)
    except Exception:  # noqa: BLE001 - recording must never stop trading
        log.exception("learn record failed")


_exit_marks: dict[str, int] = {}   # "SYM:opened_at" -> last check done (10 = the 10-minute cut, k = k-th half hour)
_marks_loaded = False


def _load_marks() -> None:
    """The checks already done survive a restart (snapshot key `paper_exit_marks`); otherwise a restarted app would
    re-run a passed half-hour check on a position that had already cleared it."""
    global _marks_loaded
    if _marks_loaded:
        return
    _marks_loaded = True
    try:
        saved = db.get_snapshot_data("paper_exit_marks", {}) or {}
        if isinstance(saved, dict):
            _exit_marks.update({str(k): int(v) for k, v in saved.items()})
    except Exception:  # noqa: BLE001
        log.exception("paper exit marks load failed")


def _save_marks() -> None:
    try:
        db.set_snapshot("paper_exit_marks", dict(_exit_marks))
    except Exception:  # noqa: BLE001
        log.exception("paper exit marks save failed")


def _bot_exits(prices: dict, now: int, s: dict) -> None:
    """In-session exits of the opportunity bot's positions (only when the learned settings switch them on): at the
    10th minute -0.8 % or worse -> out; at every half hour of the position's age below +0.2 % -> out; profit lock:
    once the lock % is reached the stop moves to entry + 0.2 %."""
    if not (s["bot_cut10"] or s["bot_cull30"] or s["bot_lock_pct"] > 0):
        return
    _load_marks()
    before = dict(_exit_marks)
    live_keys = set()
    for sym, p in _positions().items():
        if p.get("origin") != "bot":
            continue
        key = f"{sym}:{p['opened_at']}"
        live_keys.add(key)
        q = prices.get(sym)
        if not _usable(q, "open", now):
            continue
        last, long_ = float(q["price"]), p["qty"] > 0
        gain = (last / p["avg_price"] - 1) * (1 if long_ else -1)
        age = now - int(p["opened_at"])
        why = None
        if s["bot_cut10"] and age >= 600 and f"{key}:cut" not in _exit_marks:
            _exit_marks[f"{key}:cut"] = 1
            if gain <= -0.008:
                why = ("cut10", f"10. dakikada {_pct(gain * 100)}")
        if why is None and s["bot_cull30"]:
            k = age // 1800
            if k >= 1 and _exit_marks.get(key, 0) < k:
                _exit_marks[key] = k
                if gain < 0.002:
                    why = ("cull30", f"{k * 30}. dakikada kâr yok ({_pct(gain * 100)})")
        if why:
            _log("info", sym, f"{ORIGINS_TR[why[0]]}: {why[1]}", now)
            try:
                close_position(sym, origin=why[0])
            except PaperError as e:
                _log("warn", sym, f"Kapatılamadı: {e}", now)
            continue
        if s["bot_lock_pct"] > 0 and gain >= s["bot_lock_pct"] / 100:
            be = round(p["avg_price"] * (1.002 if long_ else 0.998), 4)
            cur = p.get("stop_price")
            if cur is None or (long_ and cur < be) or (not long_ and cur > be):
                with db._tx() as c:
                    c.execute("UPDATE paper_positions SET stop_price=? WHERE symbol=?", (be, sym))
                _log("info", sym, f"Kâr kilidi: pozisyon {_pct(gain * 100)} kârda, zarar durdur başabaşa çekildi ({_usd(be)})", now)
    for k in [k for k in _exit_marks if k.rsplit(":cut", 1)[0] not in live_keys]:
        _exit_marks.pop(k, None)
    if _exit_marks != before:
        _save_marks()


def _bot_day_stop(now: int, s: dict, today: date) -> None:
    """Intraday stop (gün içi eleme): at each half hour from 10:00 ET, when the opportunity bot's P&L of the day
    (closed + open, its own positions) is below -X % of the day's starting equity, it opens nothing new today."""
    if s["bot_day_stop_pct"] <= 0 or _state.get("bot_day_stop") == today.isoformat():
        return
    et = datetime.fromtimestamp(now, ET)
    cp = (et.hour * 60 + et.minute) // 30 * 30
    if cp < 600 or _state.get("bot_day_cp") == (today.isoformat(), cp):
        return
    _state["bot_day_cp"] = (today.isoformat(), cp)
    acct = _account()
    start = acct.get("day_start_equity") if acct.get("day_date") == today.isoformat() else None
    if not start:
        return
    day0 = _at_et(today, 0)
    c = db.conn()
    syms = [r[0] for r in c.execute("SELECT DISTINCT symbol FROM paper_orders WHERE origin='bot' AND status='filled' AND created_at>=?", (day0,))]
    if not syms:
        return
    marks = ",".join("?" * len(syms))
    realized = c.execute(f"SELECT COALESCE(SUM(realized_pnl),0) FROM paper_orders WHERE status='filled' AND filled_at>=? AND realized_pnl IS NOT NULL"
                         f" AND symbol IN ({marks})", (day0, *syms)).fetchone()[0] or 0.0
    unreal = sum(p["qty"] * (_mark(p) - p["avg_price"]) for p in _positions().values() if p.get("origin") == "bot")
    pnl_pct = (realized + unreal) / float(start) * 100
    if pnl_pct < -s["bot_day_stop_pct"]:
        _state["bot_day_stop"] = today.isoformat()
        _log("warn", "", f"Gün içi eleme: fırsat botu bugün {_pct(pnl_pct)} zararda (sınır −%{s['bot_day_stop_pct']:g}); "
             "bugün yeni işlem açmayacak", now)


def _rel_volume_adj(q: dict, now: int) -> float | None:
    vol, avg = q.get("volume"), q.get("avg_volume")
    if not vol or not avg:
        return None
    d = _et_date(now)
    open_ts = _at_et(d, 9 * 60 + 30)
    frac = min(1.0, max(0.08, (now - open_ts) / max(1, _close_ts(d) - open_ts)))
    return float(vol) / (float(avg) * frac)


def bot_filter(o: dict, q: dict, s: dict, now: int) -> tuple[list[str], float | None, float | None]:
    """The opportunity bot's live entry filters for one candidate (also used by the realistic $100 account).
    Returns (reasons it fails — empty when it passes, today's change %, time-adjusted relative volume)."""
    price = float(q["price"])
    chg = q.get("change_pct")
    chg = float(chg) if chg is not None else o.get("change_pct")
    rvol = _rel_volume_adj(q, now)
    short = o.get("direction") == "short"
    why = []
    if price < s["bot_min_price"]:
        why.append(f"fiyat {_usd(price)} < {_usd(s['bot_min_price'])}")
    if chg is None:
        why.append("günlük değişim yok")
    else:
        mv = -chg if short else chg
        if mv < s["bot_min_change_pct"]:
            why.append(f"hareket {_pct(chg)} zayıf")
        elif mv > s["bot_max_change_pct"]:
            why.append(f"hareket {_pct(chg)} aşırı")
    if s["bot_min_rel_volume"] > 0 and (rvol is None or rvol < s["bot_min_rel_volume"]):
        why.append("göreli hacim " + (f"{rvol:.1f}x" if rvol is not None else "yok") + f" < {s['bot_min_rel_volume']:g}x")
    if s["bot_min_dollar_volume"] > 0:
        try:
            dv = float(q.get("avg_volume") or 0) * price
        except (TypeError, ValueError):
            dv = 0.0
        if dv < s["bot_min_dollar_volume"]:
            why.append(f"günlük işlem hacmi {dv / 1e6:.1f} M$ < {s['bot_min_dollar_volume'] / 1e6:.0f} M$")
    return why, chg, rvol


def _bot_entries(s: dict, cands: list[dict], prices: dict, now: int, today: date) -> None:
    et = datetime.fromtimestamp(now, ET)
    minute = et.hour * 60 + et.minute
    if minute < _hhmm_minutes(s["bot_start_et"]) or minute > _hhmm_minutes(s["bot_last_entry_et"]):
        return
    acct = _account()
    if acct.get("halted_date") == today.isoformat() or _state.get("bot_day_stop") == today.isoformat():
        return
    day_start = _at_et(today, 0)
    c = db.conn()
    traded = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM paper_orders WHERE origin='bot' AND created_at>=?", (day_start,))}
    n_today = c.execute("SELECT COUNT(*) FROM paper_orders WHERE origin='bot' AND status='filled' AND created_at>=?",
                        (day_start,)).fetchone()[0]
    for o in cands:
        if n_today >= s["bot_max_trades_per_day"]:
            return
        sym = o["ticker"]
        if sym in traded:
            continue
        q = prices.get(sym)
        if not _usable(q, "open", now):
            continue
        price = float(q["price"])
        short = o.get("direction") == "short"
        why, chg, rvol = bot_filter(o, q, s, now)
        if why:
            continue   # filtered silently: the scan repeats every tick, logging each skip would flood the log
        acct, pos = _account(), _positions()
        equity = _valuation(acct, pos)["equity"]
        risk_amt = equity * s["bot_risk_per_trade_pct"] / 100
        qty = math.floor(risk_amt / (price * s["bot_stop_loss_pct"] / 100))
        # the fill re-checks the position cap at the slipped price, so size against that price
        fill_ref = price * (1 + max(s["slippage_bps"], liquidity_bps(q) or 0.0) / 10000)
        qty = min(qty, math.floor(equity * s["max_position_pct"] / 100 / fill_ref))
        if qty < 1:
            continue
        reason = (f"skor {float(o.get('score') or 0):.0f}, değişim {_pct(chg)}"
                  + (f", göreli hacim {rvol:.1f}x" if rvol is not None else "")
                  + (f" · {'; '.join(str(r) for r in (o.get('reasons') or [])[:2])}" if o.get("reasons") else ""))
        try:
            od = place_order({"symbol": sym, "side": "sell" if short else "buy", "qty": qty, "type": "market",
                              "stop_loss_pct": s["bot_stop_loss_pct"], "take_profit_pct": s["bot_take_profit_pct"],
                              "note": reason[:200]}, origin="bot")
        except PaperError as e:
            _log("warn", sym, f"Bot girişi reddedildi: {e}", now)
            traded.add(sym)
            continue
        traded.add(sym)
        status = od.get("status")
        if status == "rejected":
            _log("warn", sym, f"Bot girişi reddedildi: {od.get('note') or 'risk kontrolü'}", now)
            continue
        verb = 'açığa sattı' if short else 'aldı'
        if status != "filled":
            verb = 'açığa satış emri verdi' if short else 'alış emri verdi'
        _log("bot", sym, f"Bot {verb}: {qty} adet — {reason}", now)
        if status == "filled":
            n_today += 1


# ---------------------------------------------------------------- read side

def _stats(c) -> dict:
    rows = [r[0] for r in c.execute("SELECT realized_pnl FROM paper_orders WHERE status='filled' AND realized_pnl IS NOT NULL")]
    wins = [x for x in rows if x > 0]
    losses = [x for x in rows if x <= 0]
    eq = [r[0] for r in c.execute("SELECT equity FROM paper_equity ORDER BY ts")]
    peak, mdd = 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return {
        "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if wins and losses and sum(losses) != 0 else None,
        "realized_total": round(sum(rows), 2),
        "max_drawdown_pct": round(mdd * 100, 2) if eq else None,
    }


def _equity_series(c, now: int, days: int = 30, points: int = 300) -> list[list]:
    rows = [(r[0], r[1]) for r in c.execute("SELECT ts, equity FROM paper_equity WHERE ts>=? ORDER BY ts", (now - days * 86400,))]
    if len(rows) > points:
        step = len(rows) / points
        rows = [rows[int(i * step)] for i in range(points)] + [rows[-1]]
    return [list(r) for r in rows]


def summary(limit_orders: int = 100) -> dict:
    s = settings()
    now = now_ts()
    c = db.conn()
    acct, pos = _account(c), _positions(c)
    v = _valuation(acct, pos)
    today = _et_date(now)
    day_start = acct.get("day_start_equity") if acct.get("day_date") == today.isoformat() else None
    positions = []
    for p in pos.values():
        mark = _mark(p)
        cost = abs(p["qty"]) * p["avg_price"]
        upnl = p["qty"] * (mark - p["avg_price"])
        positions.append(dict(p, side="long" if p["qty"] > 0 else "short", market_value=round(p["qty"] * mark, 2),
                              unrealized=round(upnl, 2), unrealized_pct=round(upnl / cost * 100, 2) if cost else None,
                              weight_pct=round(abs(p["qty"]) * mark / v["equity"] * 100, 2) if v["equity"] > 0 else None))
    positions.sort(key=lambda p: -abs(p["market_value"]))
    open_orders = [dict(o) for o in c.execute("SELECT * FROM paper_orders WHERE status='open' ORDER BY created_at DESC")]
    history = [dict(o) for o in c.execute("SELECT * FROM paper_orders WHERE status!='open' ORDER BY COALESCE(filled_at, created_at) DESC"
                                          " LIMIT ?", (limit_orders,))]
    realized_today = c.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM paper_orders WHERE status='filled' AND filled_at>=?",
                               (_at_et(today, 0),)).fetchone()[0]
    logs = [dict(r) for r in c.execute("SELECT ts, level, symbol, msg FROM paper_log ORDER BY id DESC LIMIT 150")]
    session = market_session(now)
    return {
        "now": now, "session": session,
        "account": {
            "cash": round(acct["cash"], 2), "start_cash": acct["start_cash"], "created_at": acct["created_at"],
            "equity": round(v["equity"], 2), "positions_value": round(v["positions_value"], 2),
            "gross_exposure": round(v["gross"], 2), "unrealized": round(v["unrealized"], 2),
            "total_pnl": round(v["equity"] - acct["start_cash"], 2),
            "total_pnl_pct": round((v["equity"] / acct["start_cash"] - 1) * 100, 2) if acct["start_cash"] else None,
            "day_start_equity": day_start,
            "day_pnl": round(v["equity"] - day_start, 2) if day_start else None,
            "day_pnl_pct": round((v["equity"] / day_start - 1) * 100, 2) if day_start else None,
            "realized_today": round(realized_today or 0, 2),
            "buying_power": round(max(0.0, v["equity"] * s["max_leverage"] - v["gross"]), 2),
            "halted": acct.get("halted_date") == today.isoformat(),
        },
        "positions": positions, "open_orders": open_orders, "history": history,
        "equity": _equity_series(c, now), "stats": _stats(c), "log": logs, "settings": s,
        "engine": {"last_tick": _state["last_tick"] or None, "tick_every": TICK_EVERY},
        "labels": {"origins": ORIGINS_TR, "status": STATUS_TR, "types": TYPES_TR, "sides": SIDES_TR},
    }
