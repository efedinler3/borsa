"""Bot filosu: the 100 bots of the bot tree (lab/tree.py), each with its own paper account.

Two branches (kural, sinir) x five groups (gunluk, korku, haftalik, aylik, serbest) x 10 bots. In every group the
bots ranked 2 and 5 trade a $100 account (fractional shares, broker-like costs and fees), the other eight a $100,000
account (whole shares, liquidity-based slippage). Long only, no leverage, paper money only: nothing is ever sent to a
broker.

Day: once between 09:35 and 10:30 ET every live bot scores today's universe at the open (the same features as the
10-year walk), buys up to 5 of its best stocks (at most 10 open positions; $100 accounts at most 2, half the equity
each), with its own stop, target (stop x ratio) and holding period. The fear group trades only when the VIX closed at
20 or higher. Every minute of the session the open positions are checked: time exit (5 minutes before the close of
the last holding day), stop, target. Stops are checked only during the regular session, so an overnight gap fills at
the first price of the day.

Night (live learning): every bot's live record is its closed trades, shrunk toward its walk record
(score = (10 x prior + sum of live trade returns) / (10 + live trades), prior = half its last-year average trade). In
every group the weakest bot with enough live evidence and a negative score is retired, and a clone of the group's top
3 takes its place with a fresh account of the same size.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import random
import threading
from datetime import datetime

import numpy as np

from . import db
from .util import ET, market_session, now_ts

log = logging.getLogger("marketdesk.fleet")

NICHES = ("gunluk", "korku", "haftalik", "aylik", "serbest")
BRANCHES = ("kural", "sinir")
FROM_ET, UNTIL_ET = 9 * 60 + 35, 10 * 60 + 30
TOP_K = 5
MAX_OPEN, MAX_OPEN_MICRO = 10, 2
POS_PCT = 10.0                       # $100k accounts: 10 % of equity per position
MIN_ORDER = 5.0                      # $100 accounts: smallest order
PRIOR_N = 10                         # live trades that weigh as much as the walk record
MIN_LIVE_TRADES = 8                  # evidence needed before a bot can be retired
MIN_LIVE_DAYS = 10
CHECK_EVERY = 60
EQUITY_EVERY = 300
FEAR_VIX = 20.0
_lock = threading.RLock()
_state = {"last_check": 0, "last_equity": 0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_bots (
  id TEXT PRIMARY KEY, branch TEXT NOT NULL, niche TEXT NOT NULL, rank INTEGER, status TEXT NOT NULL DEFAULT 'live',
  born_at INTEGER, retired_at INTEGER, parent TEXT, origin TEXT, start_cash REAL NOT NULL, cash REAL NOT NULL,
  genome TEXT NOT NULL, walk TEXT, day_date TEXT, day_start_equity REAL, score REAL, why TEXT, updated INTEGER);
CREATE INDEX IF NOT EXISTS idx_fleet_bots_status ON fleet_bots(status, branch, niche);
CREATE TABLE IF NOT EXISTS fleet_positions (
  bot_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL NOT NULL, cost REAL NOT NULL,
  opened_at INTEGER, entry_day TEXT, stop_price REAL, target_price REAL, exit_at INTEGER, hold_days INTEGER,
  last_price REAL, last_ts INTEGER, ref_price REAL, PRIMARY KEY (bot_id, symbol));
CREATE TABLE IF NOT EXISTS fleet_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, symbol TEXT, opened_at INTEGER, closed_at INTEGER,
  qty REAL, entry REAL, exit REAL, pnl REAL, ret_pct REAL, why TEXT, hold_days INTEGER);
CREATE INDEX IF NOT EXISTS idx_fleet_trades_bot ON fleet_trades(bot_id, closed_at);
CREATE TABLE IF NOT EXISTS fleet_equity (bot_id TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL, PRIMARY KEY (bot_id, ts));
CREATE TABLE IF NOT EXISTS fleet_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, bot_id TEXT, msg TEXT);
"""
_inited = [False]


def init_db() -> None:
    if _inited[0]:
        return
    with db._tx() as c:
        c.executescript(SCHEMA)
    _inited[0] = True


def _log(c, level: str, bot_id: str | None, msg: str, now: int | None = None) -> None:
    c.execute("INSERT INTO fleet_log(ts, level, bot_id, msg) VALUES (?,?,?,?)", (now or now_ts(), level, bot_id, msg))


def _micro(start_cash: float) -> bool:
    return start_cash < 1000


# ---------------------------------------------------------------- seeding from the walk

def seed_from_tree(report: dict) -> int:
    """Retire every live bot (their history stays) and open the walk finals' 100 accounts."""
    init_db()
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
    with db._tx() as c:
        c.execute("UPDATE fleet_bots SET status='retired', retired_at=?, why='yeni ağaç kuruldu' WHERE status='live'", (now,))
        c.executemany("INSERT OR REPLACE INTO fleet_bots(id, branch, niche, rank, status, born_at, retired_at, parent, origin, "
                      "start_cash, cash, genome, walk, day_date, day_start_equity, score, why, updated) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        _log(c, "info", None, f"Bot ağacı kuruldu: {len(rows)} bot ({sum(1 for r in rows if _micro(r[9]))} tanesi 100 $ hesapla)")
    return len(rows)


def _prior(walk: dict) -> float:
    last = (walk or {}).get("last") or {}
    return 0.5 * float(last.get("avg_pct") or 0.0)


# ---------------------------------------------------------------- scoring today's universe

def _rule_fires(g: dict, f: dict) -> bool:
    for feat, op, thr, sparse in g.get("conds") or []:
        x = f.get(feat)
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return False
        if sparse and x == 0:
            return False
        if not (x > thr if op == ">" else x < thr):
            return False
    return True


def picks_for(g: dict, branch: str, rows: list[dict], fear: bool) -> list[dict]:
    """Today's best (at most TOP_K) stocks of one bot, best first."""
    if g.get("niche") == "korku" and not fear:
        return []
    if branch == "kural":
        hit = [r for r in rows if _rule_fires(g, r["f"])]
        hit.sort(key=lambda r: r["f"].get("rsi2") if r["f"].get("rsi2") is not None else 9)
        return hit[:TOP_K]
    from .lab import neuro
    if not rows:
        return []
    norm = g["norm"]
    X = np.stack([neuro.scale_row(r["f"], norm) for r in rows])
    sc = neuro.forward(np.array(g["w"], dtype=np.float32), X)
    order = np.argsort(-sc)[:TOP_K]
    return [rows[i] for i in order if sc[i] > 0]


def _sl_tp(g: dict, f: dict) -> tuple[float, float]:
    kind, x = g["stop"]
    if kind == "pct":
        sl = x / 100
    else:
        atr = f.get("atr")
        sl = min(0.25, max(0.003, (atr if atr and atr == atr else 0.02) * x))
    return sl, sl * float(g["ratio"])


# ---------------------------------------------------------------- the morning pass

def decide(cfg: dict | None = None, progress=None) -> dict:
    """Once a day after the open: every live bot buys its picks."""
    from . import paper, real_sim
    from .lab import evo_live
    init_db()
    data = evo_live.today_rows(progress)
    rows = [r for r in data["rows"] if r.get("open")]
    vix = data.get("vix")
    fear = bool(vix is not None and vix >= FEAR_VIX)
    bots = [dict(r) for r in db.conn().execute("SELECT * FROM fleet_bots WHERE status='live'")]
    held = {}
    for r in db.conn().execute("SELECT bot_id, symbol FROM fleet_positions"):
        held.setdefault(r["bot_id"], set()).add(r["symbol"])
    plan = []
    for b in bots:
        g = json.loads(b["genome"])
        try:
            ps = picks_for(g, b["branch"], rows, fear)
        except Exception as e:  # noqa: BLE001 - one broken genome must not stop the other 99
            log.warning("fleet: %s picks failed: %s", b["id"], e)
            continue
        plan.append((b, g, [p for p in ps if p["symbol"] not in held.get(b["id"], set())]))
    syms = sorted({p["symbol"] for _b, _g, ps in plan for p in ps})
    q = paper.quotes(syms) if syms else {}
    now = now_ts()
    bought = 0
    with _lock, db._tx() as c:
        for b0, g, ps in plan:
            # re-read inside the transaction: the minute check may have sold something (cash changed) and a rebuild
            # of the tree may have retired the bot while the universe was being scored
            row = c.execute("SELECT * FROM fleet_bots WHERE id=?", (b0["id"],)).fetchone()
            if row is None or row["status"] != "live":
                continue
            b = dict(row)
            have = {r[0] for r in c.execute("SELECT symbol FROM fleet_positions WHERE bot_id=?", (b["id"],))}
            ps = [p for p in ps if p["symbol"] not in have]
            micro = _micro(b["start_cash"])
            room = min(TOP_K, (MAX_OPEN_MICRO if micro else MAX_OPEN) - len(have))
            eq = _equity(c, b)
            cash = float(b["cash"])
            spent = 0.0
            for p in ps:
                if room <= 0:
                    break
                qt = q.get(p["symbol"])
                if not real_sim._fresh(qt, now):
                    continue
                px = float(qt["price"])
                if micro:
                    notional = math.floor(min(cash, eq / MAX_OPEN_MICRO) * 100) / 100
                    if notional < MIN_ORDER:
                        break
                    fill = px * (1 + real_sim.cost_rate(qt))
                    qty = real_sim._floor_qty(notional / fill)
                else:
                    bps = max(5.0, paper.liquidity_bps(qt) or 20.0)
                    fill = px * (1 + bps / 1e4)
                    qty = math.floor(min(cash, eq * POS_PCT / 100) / fill)
                cost = qty * fill
                if qty <= 0 or cost > cash + 1e-9:
                    continue
                sl, tp = _sl_tp(g, p["f"])
                hold = int(g["hold"])
                cash -= cost
                spent += cost
                c.execute("INSERT OR REPLACE INTO fleet_positions(bot_id, symbol, qty, avg_price, cost, opened_at, entry_day, "
                          "stop_price, target_price, exit_at, hold_days, last_price, last_ts, ref_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (b["id"], p["symbol"], qty, fill, cost, now, datetime.fromtimestamp(now, ET).date().isoformat(),
                           fill * (1 - sl), fill * (1 + tp), paper.hold_exit_ts(now, hold), hold, px, now, p.get("open")))
                _log(c, "info", b["id"], f"ALIŞ {p['symbol']} {qty:g} adet @ {fill:.4g} (açılış {p.get('open') or 0:.4g}) · "
                                         f"zarar durdur %{sl * 100:.1f}, kâr al %{tp * 100:.1f}, en fazla {hold} gün", now)
                room -= 1
                bought += 1
            if spent:
                c.execute("UPDATE fleet_bots SET cash = cash - ?, updated=? WHERE id=?", (spent, now, b["id"]))
    from .lab import store
    store.kv_set("fleet_decided", {"date": datetime.fromtimestamp(now, ET).date().isoformat(), "ts": now, "vix": vix,
                                   "fear": fear, "bought": bought, "bots": len(bots)})
    return {"bought": bought, "bots": len(bots), "fear": fear, "vix": vix}


def _equity(c, b: dict) -> float:
    v = c.execute("SELECT COALESCE(SUM(qty * COALESCE(last_price, avg_price)), 0) FROM fleet_positions WHERE bot_id=?",
                  (b["id"],)).fetchone()[0]
    return float(b["cash"]) + float(v or 0)


# ---------------------------------------------------------------- the minute check

def tick(cfg: dict | None = None, now: int | None = None) -> None:
    """Exits (time, stop, target), sells of retired bots, the day roll and equity snapshots."""
    now = now or now_ts()
    session = market_session(now)
    init_db()
    if session != "open":
        if now - _state["last_equity"] >= 3600:
            _snapshot(now)
        return
    if now - _state["last_check"] < CHECK_EVERY:
        return
    _state["last_check"] = now
    from . import paper, real_sim
    syms = [r[0] for r in db.conn().execute("SELECT DISTINCT symbol FROM fleet_positions")]
    q = paper.quotes(sorted(syms)) if syms else {}
    today = datetime.fromtimestamp(now, ET).date().isoformat()
    with _lock, db._tx() as c:
        pos = [dict(r) for r in c.execute(
            "SELECT p.*, b.start_cash, b.status FROM fleet_positions p JOIN fleet_bots b ON b.id = p.bot_id")]
        c.execute("UPDATE fleet_bots SET day_date=?, day_start_equity=cash + COALESCE((SELECT SUM(qty * COALESCE(last_price, "
                  "avg_price)) FROM fleet_positions WHERE bot_id=fleet_bots.id), 0) WHERE status='live' AND "
                  "(day_date IS NULL OR day_date<>?)", (today, today))
        for p in pos:
            qt = q.get(p["symbol"])
            if not real_sim._fresh(qt, now):
                continue
            px = float(qt["price"])
            c.execute("UPDATE fleet_positions SET last_price=?, last_ts=? WHERE bot_id=? AND symbol=?", (px, now, p["bot_id"], p["symbol"]))
            why = None
            if p["status"] != "live":
                why = "bot emekli"
            elif now >= int(p["exit_at"] or 0) - 300:
                why = "süre doldu"
            elif px <= float(p["stop_price"]):
                why = "zarar durdur"
            elif px >= float(p["target_price"]):
                why = "kâr al"
            if why:
                _sell(c, p, qt, px, why, now)
    if now - _state["last_equity"] >= EQUITY_EVERY:
        _snapshot(now)


def _sell(c, p: dict, qt: dict, px: float, why: str, now: int) -> None:
    from . import paper, real_sim
    micro = _micro(p["start_cash"])
    qty = float(p["qty"])
    if micro:
        fill = px * (1 - real_sim.cost_rate(qt))
        proceeds = qty * fill - real_sim.sell_fees(qty, qty * fill)
    else:
        fill = px * (1 - max(5.0, paper.liquidity_bps(qt) or 20.0) / 1e4)
        proceeds = qty * fill
    pnl = proceeds - float(p["cost"])
    ret = pnl / float(p["cost"]) * 100 if p["cost"] else 0.0
    c.execute("DELETE FROM fleet_positions WHERE bot_id=? AND symbol=?", (p["bot_id"], p["symbol"]))
    c.execute("UPDATE fleet_bots SET cash = cash + ?, updated=? WHERE id=?", (proceeds, now, p["bot_id"]))
    c.execute("INSERT INTO fleet_trades(bot_id, symbol, opened_at, closed_at, qty, entry, exit, pnl, ret_pct, why, hold_days) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (p["bot_id"], p["symbol"], p["opened_at"], now, qty, p["avg_price"], fill,
                                                  pnl, ret, why, p["hold_days"]))
    _log(c, "info", p["bot_id"], f"SATIŞ {p['symbol']} @ {fill:.4g} · {why} · {ret:+.2f}% ({pnl:+.2f} $)", now)


def _snapshot(now: int) -> None:
    _state["last_equity"] = now
    with db._tx() as c:
        rows = c.execute("SELECT b.id, b.cash + COALESCE(SUM(p.qty * COALESCE(p.last_price, p.avg_price)), 0) FROM fleet_bots b "
                         "LEFT JOIN fleet_positions p ON p.bot_id = b.id WHERE b.status='live' GROUP BY b.id").fetchall()
        c.executemany("INSERT OR REPLACE INTO fleet_equity(bot_id, ts, equity) VALUES (?,?,?)", [(r[0], now, r[1]) for r in rows])
        c.execute("DELETE FROM fleet_equity WHERE ts < ?", (now - 120 * 86400,))


# ---------------------------------------------------------------- nightly learning

def _live(c, bot_id: str) -> dict:
    r = c.execute("SELECT COUNT(*), COALESCE(SUM(ret_pct), 0), COUNT(DISTINCT date(closed_at, 'unixepoch')), "
                  "COALESCE(SUM(pnl), 0), COALESCE(SUM(ret_pct > 0), 0) FROM fleet_trades WHERE bot_id=?", (bot_id,)).fetchone()
    return {"n": int(r[0]), "sum_pct": float(r[1]), "days": int(r[2]), "pnl": float(r[3]), "wins": int(r[4])}


def _clone(g: dict, branch: str, niche: str, rng: random.Random) -> dict:
    """A small step from a top-3 parent: thresholds move ±3 % (rule bots) or 30 % of the weights a little (networks);
    the stop / target may step to a neighbour; the free group may change its holding period."""
    from .lab import evolve, neuro, tree
    c = copy.deepcopy(g)
    if branch == "kural":
        for cond in c["conds"]:
            if rng.random() < 0.6:
                t = float(cond[2])
                # dollar volume is stored as log10: an absolute step there; elsewhere ±3 % of the threshold
                cond[2] = t + rng.gauss(0, 0.03) if cond[0] == "dvol" else t * (1 + rng.gauss(0, 0.03)) if t else rng.gauss(0, 0.005)
    else:
        w = np.array(c["w"], dtype=np.float32)
        nprng = np.random.default_rng(rng.randrange(1 << 30))
        m = nprng.random(len(w)) < 0.3
        w[m] += nprng.normal(0, 0.05, int(m.sum())).astype(np.float32)
        w[-1] = min(-0.35, float(w[-1]))
        c["w"] = [round(float(x), 5) for x in w]
    stops = tree.STOPS
    if rng.random() < 0.3 and tuple(c["stop"]) in stops:
        i = stops.index(tuple(c["stop"]))
        c["stop"] = list(stops[min(len(stops) - 1, max(0, i + rng.choice((-1, 1))))])
    if rng.random() < 0.3:
        i = evolve.RATIO_SET.index(c["ratio"]) if c["ratio"] in evolve.RATIO_SET else 1
        c["ratio"] = evolve.RATIO_SET[min(len(evolve.RATIO_SET) - 1, max(0, i + rng.choice((-1, 1))))]
    c["hold"] = tree._step_hold(int(c["hold"]), niche, rng)
    if branch == "kural":
        from .lab import learn
        c["rule"] = learn._describe(dict(c, side=1))
    return c


def nightly(day: str, seed: int | None = None) -> dict:
    """Score every live bot on its live record; in every group retire the weakest proven loser and clone the top 3."""
    init_db()
    rng = random.Random(seed if seed is not None else now_ts())
    now = now_ts()
    changes = []
    with _lock, db._tx() as c:
        bots = [dict(r) for r in c.execute("SELECT * FROM fleet_bots WHERE status='live'")]
        for b in bots:
            lv = _live(c, b["id"])
            prior = _prior(json.loads(b["walk"] or "{}"))
            b["score"] = (PRIOR_N * prior + lv["sum_pct"]) / (PRIOR_N + lv["n"])
            b["live"] = lv
            c.execute("UPDATE fleet_bots SET score=?, updated=? WHERE id=?", (b["score"], now, b["id"]))
        for br in BRANCHES:
            for niche in NICHES:
                grp = sorted((b for b in bots if b["branch"] == br and b["niche"] == niche), key=lambda b: -b["score"])
                if len(grp) < 4:
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
                new_id = f"{br}-{niche}-k{now % 100000}-{rng.randrange(1000)}"
                c.execute("UPDATE fleet_bots SET status='retired', retired_at=?, why=? WHERE id=?",
                          (now, f"canlı skor {dead['score']:+.2f} (canlı {dead['live']['n']} işlem)", dead["id"]))
                c.execute("INSERT INTO fleet_bots(id, branch, niche, rank, status, born_at, parent, origin, start_cash, cash, genome, "
                          "walk, score, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (new_id, br, niche, dead["rank"], "live", now, parent["id"], "en iyi 3'ün klonu (canlı)",
                           dead["start_cash"], dead["start_cash"], json.dumps(g), json.dumps(walk), _prior(walk), now))
                _log(c, "info", new_id, f"{day}: {dead['id']} emekli (skor {dead['score']:+.2f}), yerine {parent['id']} klonu doğdu")
                changes.append({"branch": br, "niche": niche, "retired": dead["id"], "born": new_id, "parent": parent["id"],
                                "score": round(dead["score"], 3)})
    from .lab import store
    store.kv_set("fleet_night", {"day": day, "ts": now, "changes": changes, "bots": len(bots)})
    return {"day": day, "changes": changes}


# ---------------------------------------------------------------- summary for the screen

def summary(limit_closed: int = 8) -> dict:
    from .lab import store
    init_db()
    c = db.conn()
    bots = [dict(r) for r in c.execute("SELECT * FROM fleet_bots WHERE status='live' ORDER BY branch, niche, rank")]
    pos: dict[str, list] = {}
    for r in c.execute("SELECT * FROM fleet_positions ORDER BY opened_at"):
        pos.setdefault(r["bot_id"], []).append(dict(r))
    out_b = []
    for b in bots:
        g = json.loads(b["genome"])
        walk = json.loads(b["walk"] or "{}")
        ps = pos.get(b["id"], [])
        value = sum(p["qty"] * (p["last_price"] or p["avg_price"]) for p in ps)
        eq = b["cash"] + value
        lv = _live(c, b["id"])
        closed = [dict(r) for r in c.execute("SELECT symbol, opened_at, closed_at, ret_pct, pnl, why FROM fleet_trades WHERE bot_id=? "
                                             "ORDER BY closed_at DESC LIMIT ?", (b["id"], limit_closed))]
        curve = [[r[0], r[1]] for r in c.execute("SELECT ts, equity FROM fleet_equity WHERE bot_id=? ORDER BY ts DESC LIMIT 60",
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
    retired = [dict(r) for r in c.execute("SELECT id, branch, niche, retired_at, why FROM fleet_bots WHERE status='retired' "
                                          "AND retired_at IS NOT NULL ORDER BY retired_at DESC LIMIT 20")]
    log_rows = [dict(r) for r in c.execute("SELECT ts, level, bot_id, msg FROM fleet_log ORDER BY id DESC LIMIT 40")]
    return {"ts": now_ts(), "bots": out_b, "retired": retired, "log": log_rows, "decided": store.kv_get("fleet_decided"),
            "night": store.kv_get("fleet_night"), "tree": _tree_meta()}


def _tree_meta() -> dict | None:
    from .lab import store
    r = store.kv_get("tree_finals")
    if not r:
        return None
    return {"created_at": r.get("created_at"), "range": r.get("range"), "seconds": r.get("seconds"),
            "groups": {br: {k: {"passed": g.get("passed"), "filled": g.get("filled"), "record": g.get("record")}
                            for k, g in (rep.get("groups") or {}).items()} for br, rep in (r.get("branches") or {}).items()}}
