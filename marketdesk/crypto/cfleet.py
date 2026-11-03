"""Kripto filosu: the 40 bots of the crypto tree (ctree.py), each with its own paper account. Paper money only:
nothing is ever sent to an exchange, no account, no API key — prices come from Binance's public ticker.

Two branches (kural, sinir) x five groups (gunluk, korku, haftalik, aylik, serbest) x 4 bots. In every group the best
bot (rank 1) trades a $100,000 account (10 % of equity per coin, at most 10 coins); the other three trade $100
accounts aiming for the most profit: at most 2 coins, half the equity each, $5 smallest order. Both pay Binance's
0.1 % fee plus a spread that grows for thinner coins, on both sides; fractional coin amounts like the exchange.

Day (crypto never closes; the day is the UTC day, 00:00 UTC = 03:00 Türkiye saati):
  00:05 UTC  yesterday's candles are fetched; positions whose holding period ended are sold; the nightly learning
             runs (in every group the weakest bot with enough live evidence and a negative score is retired and a
             clone of the group's top 3 takes its place, with a fresh account of the same size); then every live bot
             scores today's 150 most traded coins (the same features as the walk) and buys up to 5 of its best.
             The fear group buys only when yesterday's Fear & Greed index was 30 or lower.
  every minute (24/7, while the computer and the app are on): stop and target checks on the last price.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import random
import threading
from datetime import datetime, timezone

import numpy as np

from ..util import friendly_error, now_ts
from . import cstore

log = logging.getLogger("marketdesk.crypto")

NICHES = ("gunluk", "korku", "haftalik", "aylik", "serbest")
BRANCHES = ("kural", "sinir")
TOP_K = 5
MAX_OPEN, MAX_OPEN_MICRO = 10, 2
POS_PCT = 10.0
MIN_ORDER = 5.0
PRIOR_N = 10
MIN_LIVE_TRADES = 8
MIN_LIVE_DAYS = 10
CHECK_EVERY = 60
EQUITY_EVERY = 300
DAILY_AFTER_MIN = 5              # minutes after 00:00 UTC
FEAR_FNG = 30.0
LATE_LIMIT = 3 * 3600             # no buys later than 03:00 UTC (06:00 Türkiye saati)
_lock = threading.RLock()
_state = {"last_check": 0, "last_equity": 0, "prices": {}, "prices_ts": 0, "daily_running": False, "daily_error": None}


def _utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def _day_start(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def _log(c, level: str, bot_id: str | None, msg: str, now: int | None = None) -> None:
    c.execute("INSERT INTO cfleet_log(ts, level, bot_id, msg) VALUES (?,?,?,?)", (now or now_ts(), level, bot_id, msg))


def _micro(start_cash: float) -> bool:
    return start_cash < 1000


def _prior(walk: dict) -> float:
    return 0.5 * float(((walk or {}).get("last") or {}).get("avg_pct") or 0.0)


def _cost_rate(dvol_log: float | None) -> float:
    from . import cpool
    return float(cpool.slippage(np.array([dvol_log if dvol_log is not None else 5.0]))[0])


# ---------------------------------------------------------------- seeding

def seed_from_tree(report: dict) -> int:
    """Retire every live bot (positions are sold at the next minute check) and open the walk finals' accounts."""
    cstore.init()
    now = now_ts()
    rows = []
    for br, rep in (report.get("branches") or {}).items():
        for niche, g in (rep.get("groups") or {}).items():
            for b in g.get("bots") or []:
                genome = {k: v for k, v in b.items() if k not in ("life", "last", "exam", "fit", "passed", "filled", "rank",
                                                                   "branch", "account", "id", "uid", "alive_since")}
                walk = {k: b.get(k) for k in ("life", "last", "exam", "fit", "passed", "filled", "alive_since")}
                rows.append((b["id"], br, niche, b["rank"], "live", now, None, None, b.get("origin") or "gün gün evrim",
                             float(b["account"]), float(b["account"]), json.dumps(genome), json.dumps(walk), None, None,
                             _prior(walk), None, now))
    if not rows:
        return 0
    with _lock, cstore.tx() as c:
        c.execute("UPDATE cfleet_bots SET status='retired', retired_at=?, why='yeni kripto ağacı kuruldu' WHERE status='live'", (now,))
        c.executemany("INSERT OR REPLACE INTO cfleet_bots(id, branch, niche, rank, status, born_at, retired_at, parent, origin, "
                      "start_cash, cash, genome, walk, day_date, day_start_equity, score, why, updated) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        _log(c, "info", None, f"Kripto ağacı kuruldu: {len(rows)} bot ({sum(1 for r in rows if _micro(r[9]))} tanesi 100 $ hesapla)")
    return len(rows)


# ---------------------------------------------------------------- today's picks

def _rule_fires(g: dict, f: dict) -> bool:
    for feat, op, thr, _sp in g.get("conds") or []:
        x = f.get(feat)
        if x is None or not math.isfinite(float(x)):
            return False
        x, t = np.float32(x), np.float32(thr)          # the walk compares float32 features with these thresholds
        if not (x > t if op == ">" else x < t):
            return False
    return True


def picks_for(g: dict, branch: str, rows: list[dict], fear: bool) -> list[dict]:
    from . import ctree
    if g.get("niche") == "korku" and not fear:
        return []
    if not rows:
        return []
    if branch == "kural":
        hit = [r for r in rows if _rule_fires(g, r["f"])]
        rk = g.get("order") if g.get("order") in ctree.RANKS else "rsi2"
        sign = ctree.RANKS[rk][0]

        def key(r):
            x = r["f"].get(rk)
            return sign * x if x is not None and math.isfinite(x) else 9e9
        hit.sort(key=key)
        return hit[:TOP_K]
    X = np.stack([ctree.scale_row(r["f"], g["norm"]) for r in rows])
    sc = ctree.forward(np.array(g["w"], dtype=np.float32), X)
    order = np.argsort(-sc)[:TOP_K]
    return [rows[i] for i in order if sc[i] > 0]


def _sl_tp(g: dict, f: dict) -> tuple[float, float]:
    kind, x = g["stop"]
    if kind == "pct":
        sl = x / 100
    else:
        atr = f.get("atr")
        sl = min(0.25, max(0.003, (atr if atr and math.isfinite(atr) else 0.05) * x))
    return sl, sl * float(g["ratio"])


def _prices(force: bool = False) -> dict[str, float]:
    from . import data
    now = now_ts()
    if force or now - _state["prices_ts"] >= 50:
        _state["prices"] = data.prices()
        _state["prices_ts"] = now
    return _state["prices"]


def _equity(c, b: dict) -> float:
    v = c.execute("SELECT COALESCE(SUM(qty * COALESCE(last_price, avg_price)), 0) FROM cfleet_positions WHERE bot_id=?",
                  (b["id"],)).fetchone()[0]
    return float(b["cash"]) + float(v or 0)


def decide(day: str | None = None) -> dict:
    """Every live bot buys its picks for the UTC day (once a day, after yesterday's candle is in)."""
    from . import cpool
    cstore.init()
    data = cpool.today_rows()
    rows = data["rows"]
    day = day or data.get("day")
    fng = data.get("fng")
    fear = bool(fng is not None and round(fng * 100) <= FEAR_FNG)
    bots = [dict(r) for r in cstore.conn().execute("SELECT * FROM cfleet_bots WHERE status='live'")]
    plan = []
    for b in bots:
        g = json.loads(b["genome"])
        try:
            plan.append((b, g, picks_for(g, b["branch"], rows, fear)))
        except Exception as e:  # noqa: BLE001 - one broken genome must not stop the others
            log.warning("cfleet: %s picks failed: %s", b["id"], e)
    px_all = _prices(force=True)
    now = now_ts()
    exit_base = _day_start(day) if day else now
    bought = 0
    with _lock, cstore.tx() as c:
        for b0, g, ps in plan:
            row = c.execute("SELECT * FROM cfleet_bots WHERE id=?", (b0["id"],)).fetchone()
            if row is None or row["status"] != "live":
                continue
            b = dict(row)
            have = {r[0] for r in c.execute("SELECT symbol FROM cfleet_positions WHERE bot_id=?", (b["id"],))}
            micro = _micro(b["start_cash"])
            room = min(TOP_K, (MAX_OPEN_MICRO if micro else MAX_OPEN) - len(have))
            eq = _equity(c, b)
            cash = float(b["cash"])
            spent = 0.0
            for p in ps:
                if room <= 0:
                    break
                if p["symbol"] in have:
                    continue
                px = px_all.get(p["symbol"])
                if not px or px <= 0:
                    continue
                rate = _cost_rate(p["f"].get("dvol"))
                fill = px * (1 + rate)
                notional = min(cash, eq / MAX_OPEN_MICRO if micro else eq * POS_PCT / 100)
                notional = math.floor(notional * 100) / 100
                if notional < MIN_ORDER:
                    break
                qty = math.floor(notional / fill * 1e8) / 1e8
                cost = qty * fill
                if qty <= 0 or cost > cash + 1e-9:
                    continue
                sl, tp = _sl_tp(g, p["f"])
                hold = int(g["hold"])
                cash -= cost
                spent += cost
                c.execute("INSERT OR REPLACE INTO cfleet_positions(bot_id, symbol, qty, avg_price, cost, opened_at, entry_day, "
                          "stop_price, target_price, exit_at, hold_days, last_price, last_ts, ref_price, rate) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (b["id"], p["symbol"], qty, fill, cost, now, day, fill * (1 - sl), fill * (1 + tp),
                           exit_base + hold * 86400, hold, px, now, p.get("close"), rate))
                _log(c, "info", b["id"], f"ALIŞ {p['symbol']} {qty:.6g} @ {fill:.6g} · zarar durdur %{sl * 100:.1f}, "
                                         f"kâr al %{tp * 100:.1f}, en fazla {hold} gün", now)
                room -= 1
                bought += 1
            if spent:
                c.execute("UPDATE cfleet_bots SET cash = cash - ?, updated=? WHERE id=?", (spent, now, b["id"]))
    out = {"date": day, "ts": now, "fng": None if fng is None else round(fng * 100), "fear": fear, "bought": bought,
           "bots": len(bots), "universe": len(rows)}
    cstore.kv_set("cfleet_decided", out)
    return out


# ---------------------------------------------------------------- the minute check

def tick(now: int | None = None, force: bool = False) -> dict | None:
    """Exits (time, stop, target, retired bots), the day roll and equity snapshots."""
    now = now or now_ts()
    cstore.init()
    if not force and now - _state["last_check"] < CHECK_EVERY:
        return None
    _state["last_check"] = now
    syms = [r[0] for r in cstore.conn().execute("SELECT DISTINCT symbol FROM cfleet_positions")]
    sold = 0
    if syms:
        try:
            px_all = _prices(force=True)
        except Exception as e:  # noqa: BLE001 - offline: try again next minute
            log.warning("cfleet prices: %s", e)
            return None
        today = _utc_day(now)
        with _lock, cstore.tx() as c:
            pos = [dict(r) for r in c.execute(
                "SELECT p.*, b.start_cash, b.status FROM cfleet_positions p JOIN cfleet_bots b ON b.id = p.bot_id")]
            c.execute("UPDATE cfleet_bots SET day_date=?, day_start_equity=cash + COALESCE((SELECT SUM(qty * COALESCE(last_price, "
                      "avg_price)) FROM cfleet_positions WHERE bot_id=cfleet_bots.id), 0) WHERE status='live' AND "
                      "(day_date IS NULL OR day_date<>?)", (today, today))
            for p in pos:
                px = px_all.get(p["symbol"])
                if not px or px <= 0:
                    # no price: a short halt is waited out; a coin that left the exchange (no price for a day), a
                    # retired bot or an ended hold is sold at the last known price, as the walk sells delisted coins
                    stale = now - int(p["last_ts"] or p["opened_at"] or now) > 86400
                    if stale or p["status"] != "live" or now >= int(p["exit_at"] or 0) + 3600:
                        _sell(c, p, float(p["last_price"] or p["avg_price"]), "fiyat yok (borsadan çıkmış olabilir)", now)
                        sold += 1
                    continue
                c.execute("UPDATE cfleet_positions SET last_price=?, last_ts=? WHERE bot_id=? AND symbol=?",
                          (px, now, p["bot_id"], p["symbol"]))
                why = None
                if p["status"] != "live":
                    why = "bot emekli"
                elif now >= int(p["exit_at"] or 0) - 120:
                    why = "süre doldu"
                elif px <= float(p["stop_price"]):
                    why = "zarar durdur"
                elif px >= float(p["target_price"]):
                    why = "kâr al"
                if why:
                    _sell(c, p, px, why, now)
                    sold += 1
    else:
        with _lock, cstore.tx() as c:
            today = _utc_day(now)
            c.execute("UPDATE cfleet_bots SET day_date=?, day_start_equity=cash WHERE status='live' AND "
                      "(day_date IS NULL OR day_date<>?)", (today, today))
    if now - _state["last_equity"] >= EQUITY_EVERY:
        _snapshot(now)
    return {"positions": len(syms), "sold": sold}


def _sell(c, p: dict, px: float, why: str, now: int) -> None:
    from . import cpool
    rate = float(p.get("rate") or (cpool.FEE + 0.0005))      # the same fee + spread as the buy
    qty = float(p["qty"])
    fill = px * (1 - rate)
    proceeds = qty * fill
    fees = qty * px * rate + qty * float(p["avg_price"]) * rate / (1 + rate)     # both sides
    pnl = proceeds - float(p["cost"])
    ret = pnl / float(p["cost"]) * 100 if p["cost"] else 0.0
    c.execute("DELETE FROM cfleet_positions WHERE bot_id=? AND symbol=?", (p["bot_id"], p["symbol"]))
    c.execute("UPDATE cfleet_bots SET cash = cash + ?, updated=? WHERE id=?", (proceeds, now, p["bot_id"]))
    c.execute("INSERT INTO cfleet_trades(bot_id, symbol, opened_at, closed_at, qty, entry, exit, pnl, ret_pct, why, hold_days, fees) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (p["bot_id"], p["symbol"], p["opened_at"], now, qty, p["avg_price"], fill,
                                                    pnl, ret, why, p["hold_days"], fees))
    _log(c, "info", p["bot_id"], f"SATIŞ {p['symbol']} @ {fill:.6g} · {why} · {ret:+.2f}% ({pnl:+.2f} $)", now)


def _snapshot(now: int) -> None:
    _state["last_equity"] = now
    with _lock, cstore.tx() as c:
        rows = c.execute("SELECT b.id, b.cash + COALESCE(SUM(p.qty * COALESCE(p.last_price, p.avg_price)), 0) FROM cfleet_bots b "
                         "LEFT JOIN cfleet_positions p ON p.bot_id = b.id WHERE b.status='live' GROUP BY b.id").fetchall()
        c.executemany("INSERT OR REPLACE INTO cfleet_equity(bot_id, ts, equity) VALUES (?,?,?)", [(r[0], now, r[1]) for r in rows])
        c.execute("DELETE FROM cfleet_equity WHERE ts < ?", (now - 120 * 86400,))


# ---------------------------------------------------------------- nightly learning

def _live(c, bot_id: str) -> dict:
    r = c.execute("SELECT COUNT(*), COALESCE(SUM(ret_pct), 0), COUNT(DISTINCT date(closed_at, 'unixepoch')), "
                  "COALESCE(SUM(pnl), 0), COALESCE(SUM(ret_pct > 0), 0) FROM cfleet_trades WHERE bot_id=?", (bot_id,)).fetchone()
    return {"n": int(r[0]), "sum_pct": float(r[1]), "days": int(r[2]), "pnl": float(r[3]), "wins": int(r[4])}


def _clone(g: dict, branch: str, niche: str, rng: random.Random) -> dict:
    """A small step from a top-3 parent: thresholds move ±3 % (rule bots) or 30 % of the weights a little (networks);
    the stop / target may step to a neighbour; the free group may change its holding period."""
    from ..lab import evolve
    from . import ctree
    c = copy.deepcopy(g)
    if branch == "kural":
        for cond in c["conds"]:
            if rng.random() < 0.6:
                t = float(cond[2])
                # dollar volume and age are stored as log10: an absolute step there; elsewhere ±3 % of the threshold
                cond[2] = t + rng.gauss(0, 0.03) if cond[0] in ("dvol", "age") else t * (1 + rng.gauss(0, 0.03)) if t else rng.gauss(0, 0.005)
    else:
        w = np.array(c["w"], dtype=np.float32)
        nprng = np.random.default_rng(rng.randrange(1 << 30))
        m = nprng.random(len(w)) < 0.3
        w[m] += nprng.normal(0, 0.05, int(m.sum())).astype(np.float32)
        w[-1] = min(-0.35, float(w[-1]))
        c["w"] = [round(float(x), 5) for x in w]
    stops = ctree.STOPS
    if rng.random() < 0.3 and tuple(c["stop"]) in stops:
        i = stops.index(tuple(c["stop"]))
        c["stop"] = list(stops[min(len(stops) - 1, max(0, i + rng.choice((-1, 1))))])
    if rng.random() < 0.3:
        i = evolve.RATIO_SET.index(c["ratio"]) if c["ratio"] in evolve.RATIO_SET else 1
        c["ratio"] = evolve.RATIO_SET[min(len(evolve.RATIO_SET) - 1, max(0, i + rng.choice((-1, 1))))]
    c["hold"] = ctree._step_hold(int(c["hold"]), niche, rng)
    if branch == "kural":
        c["rule"] = ctree.describe(c)
    else:
        c["rule"] = (c.get("rule") or "").split(" · ")[0] + " · " + ctree.describe(
            {"conds": [], "stop": c["stop"], "ratio": c["ratio"], "hold": c["hold"]}).split(" · ", 1)[-1]
    return c


def nightly(day: str, seed: int | None = None) -> dict:
    cstore.init()
    prev = cstore.kv_get("cfleet_night") or {}
    if prev.get("day") == day:                     # a retried daily pass must not retire a second bot per group
        return {"day": day, "changes": prev.get("changes") or [], "repeat": True}
    rng = random.Random(seed if seed is not None else now_ts())
    now = now_ts()
    changes = []
    with _lock, cstore.tx() as c:
        bots = [dict(r) for r in c.execute("SELECT * FROM cfleet_bots WHERE status='live'")]
        for b in bots:
            lv = _live(c, b["id"])
            prior = _prior(json.loads(b["walk"] or "{}"))
            b["score"] = (PRIOR_N * prior + lv["sum_pct"]) / (PRIOR_N + lv["n"])
            b["live"] = lv
            c.execute("UPDATE cfleet_bots SET score=?, updated=? WHERE id=?", (b["score"], now, b["id"]))
        for br in BRANCHES:
            for niche in NICHES:
                grp = sorted((b for b in bots if b["branch"] == br and b["niche"] == niche), key=lambda b: -b["score"])
                if len(grp) < 3:
                    continue
                cand = [b for b in grp if b["live"]["n"] >= MIN_LIVE_TRADES and b["live"]["days"] >= MIN_LIVE_DAYS and b["score"] < 0]
                if not cand:
                    continue
                dead = cand[-1]
                parent = rng.choice([b for b in grp if b["id"] != dead["id"]][:3])
                g = _clone(json.loads(parent["genome"]), br, niche, rng)
                walk = json.loads(parent["walk"] or "{}")
                if walk.get("last"):
                    walk["last"] = dict(walk["last"], avg_pct=(walk["last"].get("avg_pct") or 0) * 0.9)
                new_id = f"c-{br}-{niche}-k{now % 100000}-{rng.randrange(1000)}"
                c.execute("UPDATE cfleet_bots SET status='retired', retired_at=?, why=? WHERE id=?",
                          (now, f"canlı skor {dead['score']:+.2f} (canlı {dead['live']['n']} işlem)", dead["id"]))
                c.execute("INSERT INTO cfleet_bots(id, branch, niche, rank, status, born_at, parent, origin, start_cash, cash, genome, "
                          "walk, score, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (new_id, br, niche, dead["rank"], "live", now, parent["id"], "en iyi 3'ün klonu (canlı)",
                           dead["start_cash"], dead["start_cash"], json.dumps(g), json.dumps(walk), _prior(walk), now))
                _log(c, "info", new_id, f"{day}: {dead['id']} emekli (skor {dead['score']:+.2f}), yerine {parent['id']} klonu doğdu")
                changes.append({"branch": br, "niche": niche, "retired": dead["id"], "born": new_id, "parent": parent["id"],
                                "score": round(dead["score"], 3)})
        c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", ("cfleet_night", json.dumps(
            {"day": day, "ts": now, "changes": changes, "bots": len(bots)}, ensure_ascii=False)))
    return {"day": day, "changes": changes}


# ---------------------------------------------------------------- the daily cycle (called every minute)

def has_bots() -> bool:
    cstore.init()
    return cstore.conn().execute("SELECT 1 FROM cfleet_bots WHERE status='live' LIMIT 1").fetchone() is not None


def _daily(today: str) -> None:
    from datetime import date as _date
    from . import data
    try:
        data.update_recent()
        yday = _date.fromordinal(_date.fromisoformat(today).toordinal() - 1).isoformat()
        last = cstore.conn().execute("SELECT MAX(d) FROM bars WHERE symbol='BTCUSDT'").fetchone()[0]
        if last != yday:
            raise RuntimeError(f"dünün mumu henüz yok (son mum {last})")
        tick(force=True)                       # holding periods that ended at 00:00 UTC are sold first
        night = nightly(yday)
        late = now_ts() - _day_start(today)
        if late > LATE_LIMIT:                  # the app was off at 00:05 UTC: buying now would shorten every hold
            dec = {"bought": 0, "skipped": f"geç kalındı ({late // 3600} saat): bugün alış yok"}
            cstore.kv_set("cfleet_decided", {"date": today, "ts": now_ts(), "bought": 0, "skipped": dec["skipped"]})
        else:
            dec = decide(today)
        cstore.kv_set("cfleet_daily", {"day": today, "ts": now_ts(), "night": len(night["changes"]), "bought": dec["bought"]})
        _state["daily_error"] = None
    except Exception as e:  # noqa: BLE001 - retried at the next minute
        log.warning("crypto daily: %s", e)
        _state["daily_error"] = friendly_error(e)
    finally:
        _state["daily_running"] = False


def cycle(cfg: dict | None = None, now: int | None = None) -> dict | None:
    """Minute step: exits, and once per UTC day (from 00:05 UTC) the update -> nightly -> buy pass in its own thread."""
    now = now or now_ts()
    cfg = cfg or {}
    cc = cfg.get("crypto") if isinstance(cfg.get("crypto"), dict) else {}
    if cc.get("enabled") is False or not has_bots():
        return None
    out = tick(now)
    dt = datetime.fromtimestamp(now, timezone.utc)
    today = dt.date().isoformat()
    done = (cstore.kv_get("cfleet_daily") or {}).get("day")
    if done != today and dt.hour * 60 + dt.minute >= DAILY_AFTER_MIN and not _state["daily_running"]:
        _state["daily_running"] = True
        threading.Thread(target=_daily, args=(today,), name="crypto_daily", daemon=True).start()
    return out


# ---------------------------------------------------------------- summary for the screen

def summary(limit_closed: int = 8) -> dict:
    cstore.init()
    c = cstore.conn()
    bots = [dict(r) for r in c.execute("SELECT * FROM cfleet_bots WHERE status='live' ORDER BY branch, niche, rank")]
    pos: dict[str, list] = {}
    for r in c.execute("SELECT * FROM cfleet_positions ORDER BY opened_at"):
        pos.setdefault(r["bot_id"], []).append(dict(r))
    out_b = []
    for b in bots:
        g = json.loads(b["genome"])
        walk = json.loads(b["walk"] or "{}")
        ps = pos.get(b["id"], [])
        value = sum(p["qty"] * (p["last_price"] or p["avg_price"]) for p in ps)
        eq = b["cash"] + value
        lv = _live(c, b["id"])
        closed = [dict(r) for r in c.execute("SELECT symbol, opened_at, closed_at, ret_pct, pnl, why FROM cfleet_trades WHERE bot_id=? "
                                             "ORDER BY closed_at DESC LIMIT ?", (b["id"], limit_closed))]
        curve = [[r[0], r[1]] for r in c.execute("SELECT ts, equity FROM cfleet_equity WHERE bot_id=? ORDER BY ts DESC LIMIT 60",
                                                 (b["id"],))][::-1]
        out_b.append({
            "id": b["id"], "branch": b["branch"], "niche": b["niche"], "rank": b["rank"], "origin": b["origin"],
            "parent": b["parent"], "born_at": b["born_at"], "start_cash": b["start_cash"], "cash": round(b["cash"], 2),
            "equity": round(eq, 2), "total_pnl": round(eq - b["start_cash"], 2),
            "total_pnl_pct": round((eq / b["start_cash"] - 1) * 100, 3),
            "day_pnl_pct": round((eq / b["day_start_equity"] - 1) * 100, 3) if b["day_start_equity"] else None,
            "score": b["score"], "rule": g.get("rule") or g.get("label") or "", "hold": g.get("hold"), "label": g.get("label") or "",
            "walk": {k: walk.get(k) for k in ("passed", "filled", "fit")} | {"last": {k: (walk.get("last") or {}).get(k)
                                                                                      for k in ("daily_pct", "avg_pct", "days", "win_rate")}},
            "live": lv, "open": [{"symbol": p["symbol"], "qty": p["qty"], "avg_price": p["avg_price"], "last_price": p["last_price"],
                                  "unrealized_pct": round(((p["last_price"] or p["avg_price"]) / p["avg_price"] - 1) * 100, 2),
                                  "stop_price": p["stop_price"], "target_price": p["target_price"], "opened_at": p["opened_at"],
                                  "exit_at": p["exit_at"], "hold_days": p["hold_days"]} for p in ps],
            "closed": closed, "curve": curve})
    retired = [dict(r) for r in c.execute("SELECT id, branch, niche, retired_at, why FROM cfleet_bots WHERE status='retired' "
                                          "AND retired_at IS NOT NULL ORDER BY retired_at DESC LIMIT 20")]
    log_rows = [dict(r) for r in c.execute("SELECT ts, level, bot_id, msg FROM cfleet_log ORDER BY id DESC LIMIT 40")]
    return {"ts": now_ts(), "bots": out_b, "retired": retired, "log": log_rows, "decided": cstore.kv_get("cfleet_decided"),
            "night": cstore.kv_get("cfleet_night"), "daily": cstore.kv_get("cfleet_daily"),
            "daily_error": _state["daily_error"], "tree": _tree_meta(), "data": cstore.counts()}


def _tree_meta() -> dict | None:
    r = cstore.kv_get("ctree_finals")
    if not r:
        return None
    return {"created_at": r.get("created_at"), "range": r.get("range"), "seconds": r.get("seconds"), "symbols": r.get("symbols"),
            "groups": {br: {k: {"passed": g.get("passed"), "filled": g.get("filled"), "record": g.get("record")}
                            for k, g in (rep.get("groups") or {}).items()} for br, rep in (r.get("branches") or {}).items()}}
