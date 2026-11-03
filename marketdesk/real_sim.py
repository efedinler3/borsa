"""Gerçekçi hesap: a $100 paper account that behaves like a real US cash brokerage account. Nothing is ever sent to a
broker; the point is to see what the bots would really keep after real-world frictions.

Mixed ("karma"): one slot per bot — evolution bot (fear-day rules, holds up to 5 days), opportunity bot and micro bot
(intraday). Each slot gets a third of the equity when it enters, so the sources stay comparable.

Real-world rules modelled:
- Cash account: no margin, no short selling. Buys use settled cash only; sale proceeds settle on the next trading day
  (T+1), so day-traded money is idle until then (no good-faith violations, no pattern-day-trader limit).
- Fractional shares (market orders, regular session only, at least $5 an order).
- Latency: an entry is sent as a market order and fills on a later engine step (>= 15 s) at the price then, not at the
  signal price. Exits fill at the price seen when the stop / target / time rule is detected.
- Costs: bid/ask spread and impact by liquidity (half a cent at least; 3-20 bps by dollar volume; +0.3 % under $2M a
  day), SEC fee and FINRA TAF on sells, no commission.
- A stale or halted price (no update for 5 minutes) cannot be traded: the position is stuck until trading resumes,
  even at the close.
- Daily loss limit: below -5 % of the day's starting equity no new entries today.
"""
from __future__ import annotations

import importlib
import logging
import math
import uuid
from datetime import date, datetime, timedelta

from . import db
from .config import load_config, save_config
from .util import ET, close_minutes, is_trading_day, market_session, now_ts

log = logging.getLogger("marketdesk.real_sim")

START_CASH = 100.00
SOURCES = {"evrim": "Evrim botu", "firsat": "Fırsat botu", "mikro": "Mikro bot"}
MIN_ORDER = 5.00
QTY_DECIMALS = 6
ENTRY_DELAY = 15              # s: a market order fills on a later engine step
PENDING_TIMEOUT = 5 * 60      # an entry that cannot fill within 5 minutes is cancelled
STALE_QUOTE = 5 * 60          # no trade on a price older than this (halt, no data)
FLATTEN_BEFORE_CLOSE = 5 * 60
EQUITY_EVERY = 300
DAILY_LOSS_LIMIT_PCT = 5.0
SEC_FEE_RATE = 27.80 / 1_000_000       # of sell proceeds
FINRA_TAF_PER_SHARE, FINRA_TAF_MAX = 0.000166, 8.30
EVO_FROM, EVO_UNTIL = 9 * 60 + 40, 10 * 60 + 30
DEFAULTS = {"enabled": True}

_state: dict = {"last_equity": 0, "failed": set()}


# ---------------------------------------------------------------- settings

def settings(cfg: dict | None = None) -> dict:
    raw = (cfg or load_config()).get("real_sim")
    out = dict(DEFAULTS)
    if isinstance(raw, dict) and "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    return out


def update_settings(upd: dict) -> dict:
    cur = settings()
    if "enabled" in (upd or {}):
        v = upd["enabled"]
        cur["enabled"] = v.strip().lower() in ("1", "true", "evet", "on") if isinstance(v, str) else bool(v)
        _log("info", "", "Gerçekçi hesap " + ("açıldı" if cur["enabled"] else "durduruldu"))
    save_config({"real_sim": cur})
    return cur


# ---------------------------------------------------------------- storage

def init_db() -> None:
    with db._tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS real_account (id INT PRIMARY KEY, cash REAL, settled_cash REAL, start_cash REAL,"
                  " created_at INT, day_date TEXT, day_start_equity REAL, halted_date TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS real_unsettled (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, settle_date TEXT,"
                  " created_at INT)")
        c.execute("CREATE TABLE IF NOT EXISTS real_positions (symbol TEXT PRIMARY KEY, source TEXT, qty REAL, avg_price REAL,"
                  " cost REAL, opened_at INT, stop_price REAL, target_price REAL, exit_at INT, last_price REAL, last_ts INT,"
                  " ref_price REAL, cut_done INT DEFAULT 0, cull_k INT DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS real_orders (id TEXT PRIMARY KEY, created_at INT, symbol TEXT, source TEXT, side TEXT,"
                  " qty REAL, notional REAL, status TEXT, ref_price REAL, filled_at INT, fill_price REAL, fees REAL,"
                  " realized_pnl REAL, note TEXT, sl_pct REAL, tp_pct REAL, hold_days INT)")
        c.execute("CREATE TABLE IF NOT EXISTS real_equity (ts INT PRIMARY KEY, equity REAL, cash REAL, settled_cash REAL,"
                  " positions_value REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS real_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT, level TEXT, symbol TEXT, msg TEXT)")
        if not c.execute("SELECT id FROM real_account WHERE id=1").fetchone():
            c.execute("INSERT INTO real_account (id, cash, settled_cash, start_cash, created_at, day_start_equity)"
                      " VALUES (1,?,?,?,?,?)", (START_CASH, START_CASH, START_CASH, now_ts(), START_CASH))


def _account() -> dict:
    r = db.conn().execute("SELECT * FROM real_account WHERE id=1").fetchone()
    if not r:
        init_db()
        r = db.conn().execute("SELECT * FROM real_account WHERE id=1").fetchone()
    return dict(r)


def _positions() -> list[dict]:
    return [dict(r) for r in db.conn().execute("SELECT * FROM real_positions ORDER BY opened_at")]


def _pending() -> list[dict]:
    return [dict(r) for r in db.conn().execute("SELECT * FROM real_orders WHERE status='pending' ORDER BY created_at")]


def _log(level: str, symbol: str, msg: str, ts: int | None = None) -> None:
    try:
        with db._tx() as c:
            c.execute("INSERT INTO real_log (ts, level, symbol, msg) VALUES (?,?,?,?)", (ts or now_ts(), level, symbol or "", msg[:400]))
            c.execute("DELETE FROM real_log WHERE id <= (SELECT MAX(id) FROM real_log) - 3000")
    except Exception:  # noqa: BLE001 - the log must never break trading
        log.exception("real_sim log write failed")


def _pkg(name: str):
    return importlib.import_module(f"{__package__}.{name}")


# ---------------------------------------------------------------- helpers

def _usd(x: float) -> str:
    return f"{x:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".") + " $"


def _pct(v: float) -> str:
    return ("+" if v > 0 else "-" if v < 0 else "") + "%" + f"{abs(v):.2f}".replace(".", ",")


def _et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def _at(d: date, minutes: int) -> int:
    return int(datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=ET).timestamp())


def _hhmm(v: str) -> int:
    h, m = str(v).split(":")
    return int(h) * 60 + int(m)


def _next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def _fresh(q: dict | None, now: int) -> bool:
    if not q or not q.get("price") or float(q["price"]) <= 0:
        return False
    return not (q.get("ts") and now - int(q["ts"]) > STALE_QUOTE)


def _dvol(q: dict) -> float | None:
    try:
        return float(q["avg_volume"]) * float(q["price"]) if q.get("avg_volume") else None
    except (TypeError, ValueError):
        return None


def cost_rate(q: dict) -> float:
    """Per-side cost as a fraction of the price: spread + impact by liquidity."""
    px = float(q["price"])
    dv = _dvol(q)
    if dv is None:
        bps = 20.0
    else:
        bps = 3.0 if dv >= 1e8 else 5.0 if dv >= 2e7 else 10.0 if dv >= 5e6 else 20.0
    r = max(0.005 / max(px, 0.01), bps / 10000)
    return r + (0.003 if (dv or 0) < 2e6 else 0.0)


def sell_fees(qty: float, proceeds: float) -> float:
    if proceeds <= 0:
        return 0.0
    fee = proceeds * SEC_FEE_RATE + min(FINRA_TAF_MAX, qty * FINRA_TAF_PER_SHARE)
    return math.ceil(fee * 100) / 100     # brokers round regulatory fees up to the cent


def _floor_qty(x: float) -> float:
    f = 10 ** QTY_DECIMALS
    return math.floor(x * f) / f


# ---------------------------------------------------------------- settlement / valuation

def _settle(today: date) -> None:
    """Proceeds whose settlement day has come become usable cash (T+1)."""
    rows = db.conn().execute("SELECT id, amount FROM real_unsettled WHERE settle_date <= ?", (today.isoformat(),)).fetchall()
    if not rows:
        return
    total = sum(float(r["amount"]) for r in rows)
    with db._tx() as c:
        c.executemany("DELETE FROM real_unsettled WHERE id=?", [(r["id"],) for r in rows])
        c.execute("UPDATE real_account SET settled_cash = MIN(cash, settled_cash + ?) WHERE id=1", (total,))
    _log("info", "", f"Takas: {_usd(total)} satış geliri kullanılabilir oldu (T+1)")


def _equity(acct: dict, pos: list[dict]) -> tuple[float, float]:
    pv = sum(float(p["qty"]) * float(p.get("last_price") or p["avg_price"]) for p in pos)
    return float(acct["cash"]) + pv, pv


def _day_roll(today: date, now: int) -> None:
    acct = _account()
    if acct.get("day_date") == today.isoformat():
        return
    eq, _ = _equity(acct, _positions())
    with db._tx() as c:
        c.execute("UPDATE real_account SET day_date=?, day_start_equity=? WHERE id=1", (today.isoformat(), round(eq, 4)))


# ---------------------------------------------------------------- execution

def _send_buy(source: str, sym: str, notional: float, ref: float, now: int, note: str, sl: float | None, tp: float | None,
              hold: int | None) -> None:
    """A market buy for `notional` dollars; the money is reserved from settled cash until it fills or is cancelled."""
    oid = uuid.uuid4().hex[:16]
    with db._tx() as c:
        acct = c.execute("SELECT settled_cash FROM real_account WHERE id=1").fetchone()
        if float(acct[0]) + 1e-9 < notional:
            raise ValueError("takası tamamlanmış nakit yetersiz")
        c.execute("UPDATE real_account SET settled_cash = settled_cash - ?, cash = cash - ? WHERE id=1", (notional, notional))
        c.execute("INSERT INTO real_orders (id, created_at, symbol, source, side, qty, notional, status, ref_price, note, sl_pct,"
                  " tp_pct, hold_days) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, now, sym, source, "buy", None, round(notional, 2), "pending", ref, note[:200], sl, tp, hold))
    _log("order", sym, f"{SOURCES[source]}: piyasa alış emri {_usd(notional)} (sinyal fiyatı {_usd(ref)}) · {note}", now)


def _cancel(o: dict, why: str, now: int) -> None:
    with db._tx() as c:
        c.execute("UPDATE real_orders SET status='cancelled', note=? WHERE id=?", (f"{o.get('note') or ''} · iptal: {why}"[:200], o["id"]))
        c.execute("UPDATE real_account SET settled_cash = settled_cash + ?, cash = cash + ? WHERE id=1", (o["notional"], o["notional"]))
    _log("warn", o["symbol"], f"Alış emri iptal edildi: {why}", now)


def _fill_buys(q: dict, now: int) -> None:
    for o in _pending():
        if now - int(o["created_at"]) < ENTRY_DELAY:
            continue
        qq = q.get(o["symbol"])
        if not _fresh(qq, now):
            if now - int(o["created_at"]) > PENDING_TIMEOUT:
                _cancel(o, "5 dakika içinde güncel fiyat yok (işlem durdurulmuş olabilir)", now)
            continue
        px = float(qq["price"])
        fill = round(px * (1 + cost_rate(qq)), 4)
        qty = _floor_qty(float(o["notional"]) / fill)
        if qty <= 0:
            _cancel(o, "adet sıfır", now)
            continue
        cost = round(qty * fill, 4)
        left = round(float(o["notional"]) - cost, 4)      # rounding remainder goes back to settled cash
        sl, tp = o.get("sl_pct"), o.get("tp_pct")
        stop = round(fill * (1 - sl / 100), 4) if sl else None
        tgt = round(fill * (1 + tp / 100), 4) if tp else None
        exit_at = _pkg("paper").hold_exit_ts(now, int(o["hold_days"])) if o.get("hold_days") else None
        with db._tx() as c:
            if c.execute("SELECT 1 FROM real_positions WHERE symbol=?", (o["symbol"],)).fetchone():
                c.execute("UPDATE real_orders SET status='cancelled', note=? WHERE id=?", ("aynı hisse zaten açık", o["id"]))
                c.execute("UPDATE real_account SET settled_cash = settled_cash + ?, cash = cash + ? WHERE id=1", (o["notional"], o["notional"]))
                continue
            c.execute("UPDATE real_account SET settled_cash = settled_cash + ?, cash = cash + ? WHERE id=1", (left, left))
            c.execute("INSERT INTO real_positions (symbol, source, qty, avg_price, cost, opened_at, stop_price, target_price, exit_at,"
                      " last_price, last_ts, ref_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (o["symbol"], o["source"], qty, fill, cost, now, stop, tgt, exit_at, px, now, o["ref_price"]))
            c.execute("UPDATE real_orders SET status='filled', qty=?, filled_at=?, fill_price=?, fees=0 WHERE id=?",
                      (qty, now, fill, o["id"]))
        drift = (fill / float(o["ref_price"]) - 1) * 100 if o.get("ref_price") else 0.0
        _log("trade", o["symbol"], f"{SOURCES[o['source']]}: alındı {qty:g} adet @ {_usd(fill)} = {_usd(cost)}"
             f" · sinyale göre {_pct(drift)} (gecikme + makas)", now)


def _sell(p: dict, qq: dict, why: str, now: int, today: date) -> None:
    px = float(qq["price"])
    fill = round(px * (1 - cost_rate(qq)), 4)
    qty = float(p["qty"])
    proceeds = round(qty * fill, 4)
    fees = sell_fees(qty, proceeds)
    net = round(proceeds - fees, 4)
    pnl = round(net - float(p["cost"]), 4)
    settle = _next_trading_day(today)
    oid = uuid.uuid4().hex[:16]
    with db._tx() as c:
        c.execute("DELETE FROM real_positions WHERE symbol=?", (p["symbol"],))
        c.execute("UPDATE real_account SET cash = cash + ? WHERE id=1", (net,))
        c.execute("INSERT INTO real_unsettled (amount, settle_date, created_at) VALUES (?,?,?)", (net, settle.isoformat(), now))
        c.execute("INSERT INTO real_orders (id, created_at, symbol, source, side, qty, notional, status, ref_price, filled_at,"
                  " fill_price, fees, realized_pnl, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, now, p["symbol"], p["source"], "sell", qty, round(proceeds, 2), "filled", px, now, fill, fees, pnl, why[:200]))
    ret = (net / float(p["cost"]) - 1) * 100 if p["cost"] else 0.0
    _log("trade", p["symbol"], f"{SOURCES.get(p['source'], p['source'])}: satıldı {qty:g} @ {_usd(fill)} · K/Z {pnl:+.2f} $"
         f" ({_pct(ret)}, ücret {_usd(fees)}) · {why} · para {settle.strftime('%d.%m')} günü kullanılabilir (T+1)", now)


def _exits(pos: list[dict], q: dict, now: int, today: date, close_ts: int, ps: dict, ms: dict) -> None:
    stuck_logged = _state.setdefault("stuck", set())
    for p in pos:
        sym, qq = p["symbol"], q.get(p["symbol"])
        eod = now >= close_ts - FLATTEN_BEFORE_CLOSE
        if not _fresh(qq, now):
            if eod and (sym, today.isoformat()) not in stuck_logged and (p["source"] != "evrim" or (p.get("exit_at") and now >= int(p["exit_at"]) - FLATTEN_BEFORE_CLOSE)):
                stuck_logged.add((sym, today.isoformat()))
                _log("warn", sym, "Kapanışta satılamadı: güncel fiyat yok (işlem durdurulmuş olabilir); pozisyon ertesi güne kaldı", now)
            continue
        last = float(qq["price"])
        with db._tx() as c:
            c.execute("UPDATE real_positions SET last_price=?, last_ts=? WHERE symbol=?", (last, now, sym))
        avg = float(p["avg_price"])
        gain = last / avg - 1
        age = now - int(p["opened_at"])
        why = None
        if p["source"] == "evrim":
            if p.get("exit_at") and now >= int(p["exit_at"]) - FLATTEN_BEFORE_CLOSE:
                why = "Tutma süresi doldu"
        elif eod:
            why = "Gün sonu kapanış"
        if why is None and p.get("stop_price") and last <= float(p["stop_price"]):
            why = "Kâr kilidi (başabaş)" if float(p["stop_price"]) >= avg else f"Zarar durdur ({_pct(gain * 100)})"
        elif why is None and p.get("target_price") and last >= float(p["target_price"]):
            why = f"Kâr al ({_pct(gain * 100)})"
        if why is None and p["source"] in ("firsat", "mikro"):
            cut10, cull30, lock = ((ps["bot_cut10"], ps["bot_cull30"], ps["bot_lock_pct"]) if p["source"] == "firsat"
                                   else (ms["cut10"], ms["cull30"], ms["lock"]))
            upd = {}
            if cut10 and age >= 600 and not p.get("cut_done"):
                upd["cut_done"] = 1
                if gain <= -0.008:
                    why = f"10. dakikada {_pct(gain * 100)}"
            k = age // 1800
            if why is None and cull30 and k >= 1 and int(p.get("cull_k") or 0) < k:
                upd["cull_k"] = k
                if gain < 0.002:
                    why = f"{k * 30}. dakikada kâr yok ({_pct(gain * 100)})"
            if why is None and lock and lock > 0 and gain >= lock / 100 and float(p.get("stop_price") or 0) < avg * 1.002:
                upd["stop_price"] = round(avg * 1.002, 4)
                _log("info", sym, f"Kâr kilidi: {_pct(gain * 100)} kârda, zarar durdur başabaşa çekildi", now)
            if upd and why is None:
                sets = ", ".join(f"{k_}=?" for k_ in upd)
                with db._tx() as c:
                    c.execute(f"UPDATE real_positions SET {sets} WHERE symbol=?", (*upd.values(), sym))
        if why:
            _sell(p, qq, why, now, today)


# ---------------------------------------------------------------- signals per source

def _evo_candidates(today: date) -> list[dict]:
    try:
        store = importlib.import_module(f"{__package__}.lab.store")
        store.init()
        t = store.kv_get("evo_today") or {}
    except Exception:  # noqa: BLE001
        return []
    if t.get("date") != today.isoformat() or t.get("provisional"):
        return []
    return [c for c in t.get("candidates") or [] if c.get("side") == "long" and c.get("symbol")]


def _entries(now: int, today: date, minute: int, close_ts: int, ps: dict, ms: dict) -> None:
    acct = _account()
    if acct.get("halted_date") == today.isoformat() or now >= close_ts - FLATTEN_BEFORE_CLOSE:
        return
    pos, pend = _positions(), _pending()
    busy = {p["source"] for p in pos} | {o["source"] for o in pend}
    held = {p["symbol"] for p in pos} | {o["symbol"] for o in pend}
    eq, _ = _equity(acct, pos)
    eq += sum(float(o["notional"]) for o in pend)
    day0 = _at(today, 0)
    c = db.conn()
    bought = {(r[0], r[1]) for r in c.execute("SELECT source, symbol FROM real_orders WHERE side='buy' AND created_at>=? AND status IN ('filled','pending')", (day0,))}
    n_src = {}
    for s_, _sym in bought:
        n_src[s_] = n_src.get(s_, 0) + 1
    paper = _pkg("paper")
    micro = _pkg("micro_paper")
    plans: list[tuple] = []     # (source, symbol, ref, note, sl, tp, hold)
    if "evrim" not in busy and EVO_FROM <= minute <= EVO_UNTIL and n_src.get("evrim", 0) == 0:
        for cnd in _evo_candidates(today):
            if cnd["symbol"] not in held and (today.isoformat(), cnd["symbol"]) not in _state["failed"]:
                plans.append(("evrim", cnd["symbol"], None, f"{cnd.get('bots')} bot, skor {cnd.get('score')} · {cnd.get('rule') or ''}",
                              cnd.get("sl_pct"), cnd.get("tp_pct"), int(cnd.get("hold") or 1)))
                break
    if ("firsat" not in busy and _hhmm(ps["bot_start_et"]) <= minute <= _hhmm(ps["bot_last_entry_et"])
            and n_src.get("firsat", 0) < ps["bot_max_trades_per_day"]):
        cands = [o for o in paper._bot_candidates(ps, {}, now) if o.get("direction") in ("long", "mixed")]
        qs = paper.quotes([o["ticker"] for o in cands]) if cands else {}
        for o in cands:
            sym = o["ticker"]
            if sym in held or ("firsat", sym) in bought or (today.isoformat(), sym) in _state["failed"]:
                continue
            qq = qs.get(sym)
            if not _fresh(qq, now):
                continue
            why, chg, rvol = paper.bot_filter(o, qq, ps, now)
            if not why:
                plans.append(("firsat", sym, float(qq["price"]),
                              f"skor {float(o.get('score') or 0):.0f}, değişim {_pct(float(chg or 0))}"
                              + (f", göreli hacim {rvol:.1f}x" if rvol else ""),
                              ps["bot_stop_loss_pct"], ps["bot_take_profit_pct"], None))
                break
    if ("mikro" not in busy and _hhmm(ms["start"]) <= minute <= _hhmm(ms["last"]) and n_src.get("mikro", 0) < ms["max_trades"]):
        caps = micro.small_caps(now)
        qs = paper.quotes([x["symbol"] for x in caps]) if caps else {}
        for x in caps:
            sym = x["symbol"]
            if sym in held or ("mikro", sym) in bought or (today.isoformat(), sym) in _state["failed"]:
                continue
            qq = qs.get(sym)
            if not _fresh(qq, now) or not micro.candidate_ok(qq, ms, now):
                continue
            plans.append(("mikro", sym, float(qq["price"]), f"küçük hisse, değişim {_pct(float(qq.get('change_pct') or 0))}",
                          ms["sl"], ms["tp"], None))
            break
    for source, sym, ref, note, sl, tp, hold in plans:
        if ref is None:
            qq = paper.quotes([sym]).get(sym)
            if not _fresh(qq, now):
                continue
            ref = float(qq["price"])
        settled = float(_account()["settled_cash"])
        notional = math.floor(min(settled, eq / len(SOURCES)) * 100) / 100
        if notional < MIN_ORDER:
            if (today.isoformat(), "cash") not in _state["failed"]:
                _state["failed"].add((today.isoformat(), "cash"))
                _log("info", "", f"{SOURCES[source]} sinyali ({sym}) atlandı: takası tamamlanmış nakit {_usd(settled)} "
                     f"(en az {_usd(MIN_ORDER)} gerekli; satış gelirleri T+1 kullanılabilir)", now)
            continue
        try:
            _send_buy(source, sym, notional, ref, now, note, sl, tp, hold)
        except Exception as e:  # noqa: BLE001
            _state["failed"].add((today.isoformat(), sym))
            _log("warn", sym, f"{SOURCES[source]}: emir verilemedi: {e}", now)


def _daily_loss(today: date, now: int) -> None:
    acct = _account()
    if acct.get("halted_date") == today.isoformat() or acct.get("day_date") != today.isoformat() or not acct.get("day_start_equity"):
        return
    eq, _ = _equity(acct, _positions())
    eq += sum(float(o["notional"]) for o in _pending())
    start = float(acct["day_start_equity"])
    if eq <= start * (1 - DAILY_LOSS_LIMIT_PCT / 100):
        with db._tx() as c:
            c.execute("UPDATE real_account SET halted_date=? WHERE id=1", (today.isoformat(),))
        _log("warn", "", f"Günlük zarar sınırı ({_pct((eq / start - 1) * 100)}): bugün yeni işlem açılmayacak", now)


def _record_equity(now: int) -> None:
    if now - _state["last_equity"] < EQUITY_EVERY:
        return
    _state["last_equity"] = now
    acct = _account()
    eq, pv = _equity(acct, _positions())
    pend = sum(float(o["notional"]) for o in _pending())
    with db._tx() as c:
        c.execute("INSERT OR REPLACE INTO real_equity (ts, equity, cash, settled_cash, positions_value) VALUES (?,?,?,?,?)",
                  (now, round(eq + pend, 4), round(float(acct["cash"]) + pend, 4), round(float(acct["settled_cash"]), 4), round(pv, 4)))
        c.execute("DELETE FROM real_equity WHERE ts < ?", (now - 400 * 86400,))


def tick(cfg: dict | None = None, now: int | None = None) -> dict:
    """One engine step (with the paper engine, every 15 s)."""
    cfg = cfg or load_config()
    if not settings(cfg)["enabled"]:
        return {"enabled": False}
    init_db()
    now = now or now_ts()
    et = _et(now)
    today = et.date()
    session = market_session(now)
    if is_trading_day(today):
        _settle(today)
    if session != "open" or not is_trading_day(today):
        _record_equity(now)
        return {"session": session}
    _state["failed"] = {x for x in _state["failed"] if x[0] == today.isoformat()}
    minute = et.hour * 60 + et.minute
    close_ts = _at(today, close_minutes(today))
    paper = _pkg("paper")
    ps = paper.settings(cfg)
    ms = _pkg("micro_paper").settings(cfg)
    _day_roll(today, now)
    pos, pend = _positions(), _pending()
    q = paper.quotes(sorted({p["symbol"] for p in pos} | {o["symbol"] for o in pend})) if (pos or pend) else {}
    _fill_buys(q, now)
    _exits(_positions(), q, now, today, close_ts, ps, ms)
    _daily_loss(today, now)
    fl = (cfg or {}).get("fleet") if isinstance((cfg or {}).get("fleet"), dict) else {}
    if not fl.get("retire_old", True):          # the bot tree replaced these bots: exits and settlement keep running
        _entries(now, today, minute, close_ts, ps, ms)
    _record_equity(now)
    return {"session": "open", "positions": len(_positions()), "pending": len(_pending())}


# ---------------------------------------------------------------- read side

def summary() -> dict:
    init_db()
    acct, pos = _account(), _positions()
    pend = _pending()
    rows = []
    for p in pos:
        last = float(p.get("last_price") or p["avg_price"])
        mv = float(p["qty"]) * last
        rows.append(dict(p, source_tr=SOURCES.get(p["source"], p["source"]), market_value=round(mv, 2),
                         unrealized_pnl=round(mv - float(p["cost"]), 2),
                         unrealized_pct=round((mv / float(p["cost"]) - 1) * 100, 2) if p["cost"] else 0.0))
    eq, pv = _equity(acct, pos)
    pend_sum = sum(float(o["notional"]) for o in pend)
    eq += pend_sum
    start = float(acct.get("start_cash") or START_CASH)
    c = db.conn()
    unsettled = [dict(r) for r in c.execute("SELECT amount, settle_date FROM real_unsettled ORDER BY settle_date")]
    orders = [dict(r) for r in c.execute("SELECT * FROM real_orders ORDER BY created_at DESC LIMIT 40")]
    logs = [dict(r) for r in c.execute("SELECT * FROM real_log ORDER BY id DESC LIMIT 40")]
    sells = [dict(r) for r in c.execute("SELECT source, realized_pnl, fees, fill_price, ref_price, qty FROM real_orders"
                                        " WHERE side='sell' AND status='filled'")]
    buys = [dict(r) for r in c.execute("SELECT fill_price, ref_price, qty FROM real_orders WHERE side='buy' AND status='filled'")]
    by_src = {}
    for k, label in SOURCES.items():
        xs = [s for s in sells if s["source"] == k]
        by_src[k] = {"label": label, "trades": len(xs), "wins": sum(1 for s in xs if (s["realized_pnl"] or 0) > 0),
                     "realized": round(sum(s["realized_pnl"] or 0 for s in xs), 2),
                     "open": sum(1 for p in pos if p["source"] == k) + sum(1 for o in pend if o["source"] == k)}
    fees = round(sum(s["fees"] or 0 for s in sells), 2)
    # implementation cost: what the fills cost against the price at the signal / exit decision
    slip = sum(max(0.0, (float(b["fill_price"]) - float(b["ref_price"]))) * float(b["qty"]) for b in buys if b["ref_price"] and b["qty"])
    slip += sum(max(0.0, (float(s["ref_price"]) - float(s["fill_price"]))) * float(s["qty"]) for s in sells if s["ref_price"] and s["qty"])
    today = _et(now_ts()).date().isoformat()
    day_start = acct.get("day_start_equity") if acct.get("day_date") == today else None
    comp = {}
    try:
        paper = _pkg("paper")
        a = paper._account()
        main_eq = paper._valuation(a, paper._positions())["equity"]
        comp["main_pnl_pct"] = round((main_eq / a["start_cash"] - 1) * 100, 2) if a.get("start_cash") else None
    except Exception:  # noqa: BLE001
        comp["main_pnl_pct"] = None
    try:
        m = _pkg("micro_paper").summary()["account"]
        comp["micro_pnl_pct"] = m.get("total_pnl_pct")
    except Exception:  # noqa: BLE001
        comp["micro_pnl_pct"] = None
    curve = [dict(r) for r in c.execute("SELECT ts, equity FROM real_equity WHERE ts >= ? ORDER BY ts", (now_ts() - 90 * 86400,))]
    if len(curve) > 400:
        step = len(curve) / 400
        curve = [curve[int(i * step)] for i in range(400)] + [curve[-1]]
    return {
        "enabled": settings()["enabled"],
        "account": {"equity": round(eq, 2), "cash": round(float(acct["cash"]) + pend_sum, 2),
                    "settled_cash": round(float(acct["settled_cash"]), 2),
                    "unsettled_cash": round(sum(u["amount"] for u in unsettled), 2), "pending": round(pend_sum, 2),
                    "positions_value": round(pv, 2), "start_cash": start, "total_pnl": round(eq - start, 2),
                    "total_pnl_pct": round((eq / start - 1) * 100, 2) if start else 0.0,
                    "day_pnl": round(eq - day_start, 2) if day_start else None,
                    "day_pnl_pct": round((eq / day_start - 1) * 100, 2) if day_start else None,
                    "halted_today": acct.get("halted_date") == today},
        "costs": {"fees": fees, "slippage": round(slip, 2)},
        "unsettled": unsettled, "positions": rows, "pending": pend, "orders": orders, "log": logs, "by_source": by_src,
        "comparison": dict(comp, real_pnl_pct=round((eq / start - 1) * 100, 2) if start else 0.0),
        "curve": curve, "session": market_session(now_ts()),
        "rules": ["Nakit hesap: kaldıraç ve açığa satış yok",
                  "Alışlar yalnızca takası tamamlanmış nakitle; satış geliri ertesi iş günü kullanılabilir (T+1)",
                  "Kesirli hisse, piyasa emri, yalnızca normal seans, emir başına en az 5 $",
                  "Emir sinyalden sonraki motor adımında (≥ 15 sn) o anki fiyattan dolar",
                  "Makas + likiditeye göre kayma; satışta SEC ücreti ve FINRA TAF",
                  "Fiyatı 5 dakikadır güncellenmeyen (durdurulmuş) hisse alınıp satılamaz",
                  "Her bota bir yuva (özsermayenin 1/3'ü); günlük %5 zararda yeni işlem yok"],
    }


def reset() -> dict:
    init_db()
    with db._tx() as c:
        c.execute("UPDATE real_account SET cash=?, settled_cash=?, start_cash=?, day_start_equity=?, day_date=NULL, halted_date=NULL,"
                  " created_at=? WHERE id=1", (START_CASH, START_CASH, START_CASH, START_CASH, now_ts()))
        for t in ("real_unsettled", "real_positions", "real_orders", "real_equity", "real_log"):
            c.execute(f"DELETE FROM {t}")
    _state["failed"] = set()
    _log("info", "", "Gerçekçi hesap (100 $) sıfırlandı.")
    return summary()
