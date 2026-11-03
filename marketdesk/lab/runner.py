"""Test lab job control: background download / daily update / test runs, and the live referee.

The scheduler calls tick() every few minutes:
  - after the close of a trading day (16:45 ET+) the lab data is brought up to date (only once data exists);
  - at the open (09:35-09:55 ET), when the user enabled it in Sanal İşlem, the referee picks today's trades from
    the four bots and places them as paper orders (origin "hakem"); they are closed before the close like the bot's.
"""
from __future__ import annotations

import importlib
import logging
import math
import threading
from datetime import date, datetime, timedelta

import numpy as np

from ..config import load_config
from ..util import ET, is_trading_day, market_session, now_ts
from . import bots, deepnet, download, evo_live, evolve, hakem, intraday, learn, neuro, store, sweep, tree, walk

log = logging.getLogger("marketdesk.lab")

UPDATE_AFTER_ET = 16 * 60 + 45
DECIDE_FROM_ET, DECIDE_UNTIL_ET = 9 * 60 + 35, 9 * 60 + 55

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
TEST: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}
SWEEP: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}
EVO: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}
AUDIT: dict = {"state": "idle", "message": "", "started_at": None, "finished_at": None}
EVO_FROM_ET, EVO_UNTIL_ET = 9 * 60 + 35, 10 * 60 + 30
LIVE: dict = {"last_decision": None, "message": ""}


def _pkg(name: str):
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{name}")


def _running(name: str) -> bool:
    t = _threads.get(name)
    return bool(t and t.is_alive())


def _start(name: str, fn, *args) -> bool:
    with _lock:
        if _running(name):
            return False
        t = threading.Thread(target=fn, args=args, name=f"lab-{name}", daemon=True)
        _threads[name] = t
        t.start()
        return True


# ---------------------------------------------------------------- download / test

def start_download(full: bool = True) -> dict:
    if _running("download"):
        return {"ok": False, "message": "İndirme zaten sürüyor"}
    _start("download", download.full_download if full else download.update_recent)
    return {"ok": True}


def start_news_backfill() -> dict:
    if _running("download"):
        return {"ok": False, "message": "İndirme zaten sürüyor"}
    _start("download", download.news_backfill)
    return {"ok": True}


def stop_download() -> dict:
    download.STOP.set()
    return {"ok": True}


def _run_test(params: dict) -> None:
    TEST.update(state="running", message="Başlıyor…", done=0, total=0, started_at=now_ts(), finished_at=None)

    def progress(msg: str, i: int, n: int) -> None:
        TEST.update(message=msg, done=i, total=n)

    try:
        rep = hakem.run_test(params, progress=progress)
        TEST.update(state="done", message=f"Test tamamlandı ({rep['seconds']:.0f} sn)", finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("lab test failed")
        TEST.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def start_test(params: dict | None = None) -> dict:
    if _running("download") and download.STATUS.get("state") == "running" and not download.STATUS.get("update"):
        return {"ok": False, "message": "Önce veri indirmenin bitmesini bekleyin (yarım veriyle test yanıltıcı olur)"}
    clean = {}
    for k, (lo, hi) in {"sl_pct": (0.3, 20), "tp_pct": (0.3, 50), "slippage_bps": (0, 100)}.items():
        try:
            if params and params.get(k) not in (None, ""):
                clean[k] = min(hi, max(lo, float(params[k])))
        except (TypeError, ValueError):
            pass
    try:
        tm = int((params or {}).get("test_months") or 2)
    except (TypeError, ValueError):
        tm = 2
    clean["test_months"] = tm if tm in (1, 2, 3, 6, 12) else 2
    if _running("sweep"):
        return {"ok": False, "message": "Önce çalışan toplu denemenin bitmesini bekleyin"}
    if not _start("test", _run_test, clean):
        return {"ok": False, "message": "Test zaten çalışıyor"}
    return {"ok": True}


def _run_sweep(test_months: int) -> None:
    SWEEP.update(state="running", message="Başlıyor…", done=0, total=0, started_at=now_ts(), finished_at=None)

    def progress(msg: str, i: int, n: int) -> None:
        SWEEP.update(message=msg, done=i, total=n)

    try:
        rep = sweep.run_sweep(test_months=test_months, progress=progress)
        SWEEP.update(state="done", message=f"Toplu deneme tamamlandı ({rep['seconds'] / 60:.1f} dk, {rep['configs']} ayar)",
                     finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("lab sweep failed")
        SWEEP.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def start_sweep(params: dict | None = None) -> dict:
    if _running("download") and download.STATUS.get("state") == "running" and not download.STATUS.get("update"):
        return {"ok": False, "message": "Önce veri indirmenin bitmesini bekleyin"}
    if _running("test"):
        return {"ok": False, "message": "Önce çalışan testin bitmesini bekleyin"}
    try:
        tm = int((params or {}).get("test_months") or 2)
    except (TypeError, ValueError):
        tm = 2
    if not _start("sweep", _run_sweep, tm if tm in (1, 2, 3, 6, 12) else 2):
        return {"ok": False, "message": "Toplu deneme zaten çalışıyor"}
    return {"ok": True}


def _run_evolve(generations: int, pop: int, runs: int = 1, mode: str = "fear") -> None:
    EVO.update(state="running", message="Başlıyor…", done=0, total=generations, started_at=now_ts(), finished_at=None)
    finals_all: list = []
    try:
        base_seed = now_ts() % 100000
        for k in range(runs):
            def progress(msg: str, i: int, n: int, k=k) -> None:
                EVO.update(message=(f"Turnuva {k + 1}/{runs} · " if runs > 1 else "") + msg, done=i, total=n)

            rep = evolve.run(max_generations=generations, pop_size=pop, seed=base_seed + 7919 * k, progress=progress, mode=mode)
            finals_all += store.kv_get("evo_daily_finals" if mode == "daily" else "evo_survivors") or []
        if mode == "daily":
            # daily bots join the live lineage next to the fear-day bots (learn.py picks them up in the nightly run)
            uniq, seen = [], set()
            for f in sorted(finals_all, key=lambda x: -float(x.get("weight") or 0)):
                key = repr((f.get("conds_abs"), f.get("side"), f.get("stop"), f.get("ratio"), f.get("hold")))
                if key not in seen and tuple(f.get("stop") or ("none",))[0] != "none":
                    seen.add(key)
                    uniq.append(f)
            store.kv_set("evo_daily_finals", uniq[:30])
            store.kv_set("learn_pending_bots", uniq[:30])
            EVO.update(state="done", message=f"Günlük bot turnuvası: {runs} turnuva, {len(uniq[:30])} zarar durdurlu final bot; "
                                             "bu gecenin öğrenmesinde mevcut botların yanına eklenecek", finished_at=now_ts())
            return
        if runs > 1:
            # the live bot uses the finals of every run (independent starts), duplicates removed
            uniq, seen = [], set()
            for f in sorted(finals_all, key=lambda x: -float(x.get("weight") or 0)):
                key = repr((f.get("conds_abs"), f.get("side"), f.get("stop"), f.get("ratio"), f.get("hold")))
                if key not in seen:
                    seen.add(key)
                    uniq.append(f)
            store.kv_set("evo_survivors", uniq[:40])
        # a new tournament: the live lineage (ana / evrim arms) starts again from its finals
        store.kv_set("evo_survivors_base40", store.kv_get("evo_survivors") or [])
        learn.reset_lineage()
        store.kv_set("evo_stability", evolve.stability(max(runs, 3)))
        EVO.update(state="done", message=f"{runs} turnuva tamamlandı · son turnuva: {rep['evaluated']} farklı bot, "
                                         f"{len(rep['finals'])} final bot ({rep['seconds'] / 60:.1f} dk)", finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("lab evolution failed")
        EVO.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def _run_audit() -> None:
    AUDIT.update(state="running", message="Başlıyor…", started_at=now_ts(), finished_at=None)
    try:
        rep = intraday.run(progress=lambda m, i, n: AUDIT.update(message=m))
        AUDIT.update(state="done", message=f"Denetim tamamlandı ({rep['symbols']} hisse)", finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("intraday audit failed")
        AUDIT.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def start_audit() -> dict:
    if not _start("audit", _run_audit):
        return {"ok": False, "message": "Denetim zaten çalışıyor"}
    return {"ok": True}


def start_learn() -> dict:
    return learn.start_now(_start, _running)


def learn_status() -> dict:
    return learn.status(_running)


def start_evolve(params: dict | None = None) -> dict:
    if _running("download") and download.STATUS.get("state") == "running" and not download.STATUS.get("update"):
        return {"ok": False, "message": "Önce veri indirmenin bitmesini bekleyin"}
    if _running("test") or _running("sweep") or _running("learn_nightly"):
        return {"ok": False, "message": "Önce çalışan test / toplu deneme / gece öğrenmesinin bitmesini bekleyin"}
    try:
        gens = min(500, max(5, int((params or {}).get("generations") or evolve.MAX_GEN)))
        pop = min(200, max(20, int((params or {}).get("population") or evolve.POP)))
        runs = min(5, max(1, int((params or {}).get("runs") or 1)))
    except (TypeError, ValueError):
        gens, pop, runs = evolve.MAX_GEN, evolve.POP, 1
    mode = "daily" if (params or {}).get("mode") == "daily" else "fear"
    if not _start("evolve", _run_evolve, gens, pop, runs, mode):
        return {"ok": False, "message": "Evrim zaten çalışıyor"}
    return {"ok": True}


def _run_nn(generations: int, pop: int, runs: int = 1) -> None:
    neuro.NN.update(state="running", message="Başlıyor…", done=0, total=generations, started_at=now_ts(), finished_at=None)
    try:
        base = now_ts() % 100000
        for k in range(runs):     # independent starts: each run's exam-passing finals join the population
            def progress(msg: str, i: int, n: int, k=k) -> None:
                neuro.NN.update(message=(f"Çalıştırma {k + 1}/{runs} · " if runs > 1 else "") + msg, done=i, total=n)

            rep = neuro.run_walk(pop_size=pop, seed=base + 7919 * k, progress=progress)
        ok = sum(1 for m in rep["models"] if m.get("passed"))
        mg = rep.get("merge") or {}
        r = rep.get("record") or {}
        neuro.NN.update(state="done", message=f"Yaşayarak evrim tamamlandı: {rep['days']} gün, {rep['births']} ağ doğdu, {rep['deaths']} elendi · "
                                              f"şampiyonların sicili günlük {r.get('daily_pct', 0) or 0:+.3f}% (piyasaya göre "
                                              f"{r.get('daily_excess_pct', 0) or 0:+.3f}%) · {ok} final ağ popülasyona girdi, "
                                              f"popülasyonda {mg.get('active', 0)} aktif ağ · {rep['seconds'] / 60:.0f} dk", finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("neuroevolution failed")
        neuro.NN.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def start_nn(params: dict | None = None) -> dict:
    if _running("download") and download.STATUS.get("state") == "running" and not download.STATUS.get("update"):
        return {"ok": False, "message": "Önce veri indirmenin bitmesini bekleyin"}
    if _running("evolve") or _running("learn_nightly") or _running("test") or _running("sweep"):
        return {"ok": False, "message": "Önce çalışan turnuva / gece öğrenmesi / testin bitmesini bekleyin (ikisi birden belleği zorlar)"}
    try:
        gens = min(200, max(5, int((params or {}).get("generations") or neuro.MAX_GEN)))
        pop = min(100, max(12, int((params or {}).get("population") or neuro.WALK_POP)))
        runs = min(5, max(1, int((params or {}).get("runs") or 1)))
    except (TypeError, ValueError):
        gens, pop, runs = neuro.MAX_GEN, neuro.POP, 1
    if not _start("nn_evolve", _run_nn, gens, pop, runs):
        return {"ok": False, "message": "Sinir ağı evrimi zaten çalışıyor"}
    return {"ok": True}


def _run_walk() -> None:
    walk.WALK.update(state="running", message="Başlıyor…", done=0, total=0, started_at=now_ts(), finished_at=None)
    try:
        rep = walk.run(progress=lambda m, i, n: walk.WALK.update(message=m, done=i, total=n))
        r = rep.get("record") or {}
        ok = sum(1 for m in rep["finals"] if m.get("passed"))
        walk.WALK.update(state="done", message=f"Gün gün kural botu evrimi tamamlandı: {rep['days']} gün, {rep['births']} bot doğdu, "
                                              f"{rep['deaths']} elendi · şampiyonların sicili günlük {r.get('daily_pct', 0) or 0:+.3f}% · "
                                              f"{ok} final bot bu gece aday olarak eklenecek · {rep['seconds'] / 60:.0f} dk", finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("rule walk failed")
        walk.WALK.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def start_walk() -> dict:
    if _running("download") and download.STATUS.get("state") == "running" and not download.STATUS.get("update"):
        return {"ok": False, "message": "Önce veri indirmenin bitmesini bekleyin"}
    if any(_running(k) for k in ("evolve", "nn_evolve", "learn_nightly", "test", "sweep")):
        return {"ok": False, "message": "Önce çalışan turnuva / sinir ağı / gece öğrenmesinin bitmesini bekleyin (ikisi birden belleği zorlar)"}
    if not _start("rule_walk", _run_walk):
        return {"ok": False, "message": "Gün gün evrim zaten çalışıyor"}
    return {"ok": True}


def _run_deep() -> None:
    deepnet.DEEP.update(state="running", message="Başlıyor…", started_at=now_ts(), finished_at=None)
    try:
        rep = deepnet.run(progress=lambda m: deepnet.DEEP.update(message=m))
        o = rep.get("oos") or {}
        deepnet.DEEP.update(state="done", message=f"Derin sinir ağı tamamlandı: görülmemiş yıllarda günlük {o.get('daily_pct', 0) or 0:+.3f}% "
                                                  f"(piyasa üstü {o.get('daily_excess_pct', 0) or 0:+.3f}%), {o.get('n', 0)} işlem · "
                                                  f"{rep['seconds'] / 60:.0f} dk", finished_at=now_ts())
    except Exception as e:  # noqa: BLE001
        log.exception("deep network failed")
        deepnet.DEEP.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


LONG_JOBS = ("evolve", "nn_evolve", "rule_walk", "learn_nightly", "test", "sweep", "deepnet", "tree", "fleet_night")


def retired(cfg: dict | None = None) -> bool:
    """True once the bot tree replaced the old live learning (config fleet.retire_old, on by default)."""
    f = (cfg or load_config()).get("fleet") or {}
    return bool(f.get("retire_old", True)) if isinstance(f, dict) else True


def retire_old_system() -> list[str]:
    """Switch the old bots off without deleting anything: the main paper account's opportunity / referee / evolution
    bots, the realistic $100 account's entries and the old live learning's automatic changes."""
    done = []
    for name, fn, upd in (("paper", lambda u: _pkg("paper").update_settings(u), {"bot_enabled": False, "hakem_enabled": False, "evo_enabled": False}),
                          ("learn", learn.update_settings, {"auto_apply": False})):
        try:
            fn(upd)
            done.append(name)
        except Exception:  # noqa: BLE001
            log.exception("retiring %s failed", name)
    return done


def _run_tree() -> None:
    tree.TREE.update(state="running", message="Başlıyor…", started_at=now_ts(), finished_at=None, done=0, total=0)
    try:
        rep = tree.run(progress=lambda m, i, n: tree.TREE.update(message=m, done=i, total=n))
        n = _pkg("fleet").seed_from_tree(rep)
        retire_old_system()
        tree.TREE.update(state="done", finished_at=now_ts(), message=f"Bot ağacı hazır: {n} bot kendi hesabıyla işleme başlıyor "
                                                                     f"({rep['seconds'] / 60:.0f} dk)")
    except Exception as e:  # noqa: BLE001
        log.exception("bot tree walk failed")
        tree.TREE.update(state="error", message=f"Hata: {e}", finished_at=now_ts())


def start_tree() -> dict:
    if _running("download") and download.STATUS.get("state") == "running":
        return {"ok": False, "message": "Önce veri indirmenin bitmesini bekleyin"}
    if any(_running(k) for k in LONG_JOBS):
        return {"ok": False, "message": "Önce çalışan uzun işin bitmesini bekleyin (ikisi birden belleği zorlar)"}
    _start("tree", _run_tree)
    return {"ok": True}


def fleet_has_bots() -> bool:
    try:
        _pkg("fleet").init_db()
        return bool(_pkg("db").conn().execute("SELECT 1 FROM fleet_bots WHERE status='live' LIMIT 1").fetchone())
    except Exception:  # noqa: BLE001
        return False


def _fleet_jobs(cfg: dict, now: int, today, minute: int) -> dict:
    """The bot tree's day: the morning buys after the open, the nightly learning after the daily update."""
    fleet = _pkg("fleet")
    out: dict = {}
    if not fleet_has_bots():
        return out
    if (is_trading_day(today) and EVO_FROM_ET <= minute <= EVO_UNTIL_ET and market_session(now) == "open"
            and (store.kv_get("fleet_decided") or {}).get("date") != today.isoformat() and not _running("fleet_decide")):
        def _decide() -> None:
            try:
                LIVE["fleet"] = fleet.decide(cfg)
            except Exception as e:  # noqa: BLE001
                log.exception("fleet morning pass failed")
                LIVE["fleet"] = {"error": str(e)}
                store.kv_set("fleet_decided", {"date": today.isoformat(), "ts": now, "error": str(e)})
        _start("fleet_decide", _decide)
        out["fleet_decide"] = "started"
    upd = store.kv_get("last_update") or 0
    if (market_session(now) != "open" and is_trading_day(today) and datetime.fromtimestamp(upd, ET).date() == today
            and (store.kv_get("fleet_night") or {}).get("day") != today.isoformat() and not _running("download")
            and not any(_running(k) for k in LONG_JOBS)):
        def _night() -> None:
            try:
                fleet.nightly(today.isoformat())
            except Exception:  # noqa: BLE001
                log.exception("fleet nightly learning failed")
                store.kv_set("fleet_night", {"day": today.isoformat(), "ts": now_ts(), "error": True, "changes": []})
        _start("fleet_night", _night)
        out["fleet_night"] = "started"
    return out


def start_deep() -> dict:
    if any(_running(k) for k in ("evolve", "nn_evolve", "rule_walk", "learn_nightly", "test", "sweep", "deepnet")):
        return {"ok": False, "message": "Önce çalışan uzun işin bitmesini bekleyin (ikisi birden belleği zorlar)"}
    _start("deepnet", _run_deep)
    return {"ok": True}


_counts_cache: dict = {"ts": 0.0, "data": None}


def _refresh_counts() -> None:
    import time as _t
    try:
        _counts_cache["data"] = store.counts()
    except Exception:  # noqa: BLE001
        log.exception("lab counts failed")
    _counts_cache["ts"] = _t.time()


def _counts() -> dict:
    """Row counts of lab.db. COUNT(*) over millions of rows takes ~40 s, so it runs in a background thread every
    10 minutes and requests always get the last value at once (they used to block on it and pile up)."""
    import time as _t
    if _t.time() - _counts_cache["ts"] > 600 and not _running("counts"):
        _start("counts", _refresh_counts)
    return _counts_cache["data"] or {}


def status() -> dict:
    store.init()
    model = store.kv_get("hakem_model")
    return {
        "download": download.status(), "download_running": _running("download"),
        "test": dict(TEST), "test_running": _running("test"),
        "sweep": dict(SWEEP), "sweep_running": _running("sweep"),
        "evo": dict(EVO), "evo_running": _running("evolve"),
        "deep": dict(deepnet.DEEP), "deep_running": _running("deepnet"), "deep_report": deepnet.last_report(),
        "tree": dict(tree.TREE), "tree_running": _running("tree"),
        "walk": dict(walk.WALK), "walk_running": _running("rule_walk"), "walk_report": walk.last_report(),
        "nn": dict(neuro.NN), "nn_running": _running("nn_evolve"), "nn_report": neuro.last_report(),
        "nn_population": neuro.population_summary(), "nn_night": store.kv_get("nn_night"),
        "audit": dict(AUDIT), "audit_running": _running("audit"), "intraday": store.kv_get("intraday_audit"),
        "evo_today": store.kv_get("evo_today"), "evo_stability": store.kv_get("evo_stability"),
        "evo_bots": len(store.kv_get("evo_survivors") or []),
        "counts": _counts(), "last_download": store.kv_get("last_download"), "last_update": store.kv_get("last_update"),
        "model": {k: model.get(k) for k in ("trained_at", "train_range", "test_range", "weights", "params")} if model else None,
        "model_kind": ("model" if model and model.get("lr") else "weighted") if model else None,
        "live": dict(LIVE, today=store.kv_get("hakem_today")),
    }


# ---------------------------------------------------------------- live referee

def _open_quotes(symbols: list[str], session: str) -> dict[str, float]:
    """Today's opening price per symbol (premarket: the latest premarket trade as a provisional open)."""
    market = _pkg("collectors.market")
    fields = "symbol,regularMarketOpen,regularMarketPrice,preMarketPrice,regularMarketTime,preMarketTime"
    ysyms = [market._yahoo_symbol(s) if not s.startswith("^") else s for s in symbols]
    chunks = [ysyms[i:i + 50] for i in range(0, len(ysyms), 50)]
    res = market._parallel(lambda c: market._crumb_json("/v7/finance/quote", {"symbols": ",".join(c), "fields": fields}),
                           [(c,) for c in chunks], workers=4, timeout=40)
    out = {}
    for r in res.values():
        if isinstance(r, Exception):
            continue
        for q in ((r or {}).get("quoteResponse") or {}).get("result") or []:
            sym = market._app_symbol(str(q.get("symbol") or ""))
            px = q.get("preMarketPrice") if session == "premarket" else q.get("regularMarketOpen")
            if px:
                out[sym] = float(px)
    return out


def _live_events(symbols: list[str], today: date, wanted: set[str]) -> dict:
    """lab events plus what happened since the last lab update: today's earnings (Nasdaq) and overnight news /
    SEC items the live app collected (same classifier as the lab headlines)."""
    ev = bots.load_events(symbols)
    try:
        for row in download._earnings_day(today, wanted):
            ev["earnings"][row[0]].append((row[1], row[2], row[5]))
    except Exception as e:  # noqa: BLE001
        log.info("live earnings failed: %s", e)
    try:
        db = _pkg("db")
        bias = bots._category_bias()
        since = int(datetime.combine(today - timedelta(days=4), datetime.min.time(), tzinfo=ET).timestamp())
        for it in db.recent_news(since, limit=20000):
            ex = it.get("extra") if isinstance(it.get("extra"), dict) else {}
            subj = set(ex.get("subject_tickers") or ex.get("title_tickers") or [])
            cats = [c for c in (it.get("categories") or []) if isinstance(c, str)]
            b = max(-1.0, min(1.0, sum(bias.get(c, 0.0) for c in cats[:3])))
            for sym in it.get("tickers") or []:
                if sym in wanted:
                    w = max(0.0, float(it.get("importance") or 0)) / 100 * (1.0 if sym in subj else 0.4)
                    ev["news"][sym].append((int(it["published_at"]), w * (0.6 * float(it.get("sentiment") or 0) + 0.4 * b), w))
        for sym in ev["news"]:
            ev["news"][sym].sort()
    except Exception as e:  # noqa: BLE001
        log.info("live news events failed: %s", e)
    return ev


def decide_today(cfg: dict | None = None) -> dict:
    """The referee's view of today: one row per symbol where a bot votes. Needs today's open (or premarket)."""
    store.init()
    model = store.kv_get("hakem_model")
    if not model:
        raise RuntimeError("Önce testi çalıştırın: hakem henüz eğitilmedi")
    now = now_ts()
    session = market_session(now)
    today = datetime.fromtimestamp(now, ET).date()
    if session == "closed":
        raise RuntimeError("Piyasa kapalı: hakem kararı bugünün açılış (ya da piyasa öncesi) fiyatını gerektirir")
    syms = [r[0] for r in store.conn().execute("SELECT symbol FROM universe ORDER BY rank")]
    bars = bots.load_bars(syms + ["SPY", "^VIX"])
    opens = _open_quotes(syms + ["SPY"], session)
    tiso = today.isoformat()
    for sym, b in bars.items():
        if b["d"] and b["d"][-1] == tiso:      # the daily update already stored today's (partial) bar: drop it
            for k in ("o", "h", "l", "c", "v"):
                b[k] = b[k][:-1]
            b["d"] = b["d"][:-1]
    stale = [s for s in syms if s in bars and bars[s]["d"] and (today - date.fromisoformat(bars[s]["d"][-1])).days > 5]
    for sym, px in opens.items():
        b = bars.get(sym)
        if not b:
            continue
        b["d"] = b["d"] + [tiso]
        b["o"] = np.append(b["o"], px)
        for k in ("h", "l", "c", "v"):
            b[k] = np.append(b[k], np.nan)
    ctx = bots._context(bars)
    ev = _live_events(syms, today, set(syms))
    news_from = {r[0]: r[1] for r in store.conn().execute("SELECT symbol, MIN(ts) FROM news GROUP BY symbol")}
    params = dict(bots.DEFAULT_PARAMS, **(model.get("params") or {}))
    parts = []
    for sym in syms:
        b = bars.get(sym)
        if not b or not b["d"] or b["d"][-1] != tiso:
            continue
        r = bots.compute_symbol(sym, b, ev, ctx, params, news_from.get(sym), live=True)
        if r and np.any(r["votes"] != 0):
            parts.append(r)
    ds = bots._concat(parts)
    rows = hakem.decide(ds, model)
    out = {"ts": now, "date": tiso, "session": session, "provisional": session == "premarket", "rows": rows[:60],
           "candidates": len(rows), "take": sum(1 for r in rows if r["take"]), "priced": len(opens), "stale": len(stale),
           "model_kind": "model" if model.get("lr") else "weighted", "params": params}
    store.kv_set("hakem_today", out)
    LIVE["last_decision"] = now
    return out


def trade_today(cfg: dict) -> dict:
    """Place today's referee trades as paper orders (once a day, at the open)."""
    paper = _pkg("paper")
    s = paper.settings(cfg)
    res = decide_today(cfg)
    if res["provisional"]:
        return {"placed": 0, "message": "Piyasa öncesi: işlem açılışta yapılır"}
    params = res["params"]
    acct, pos = paper._account(), paper._positions()
    equity = paper._valuation(acct, pos)["equity"]
    placed, skipped = 0, []
    for r in [x for x in res["rows"] if x["take"]]:
        if placed >= s["hakem_max_trades"]:
            break
        sym = r["symbol"]
        if sym in pos:
            continue
        if r["direction"] == "short" and not s["allow_short"]:
            skipped.append(f"{sym} (açığa satış kapalı)")
            continue
        price = r["open"]
        qty = math.floor(equity * s["bot_risk_per_trade_pct"] / 100 / (price * params["sl_pct"] / 100))
        qty = min(qty, math.floor(equity * s["max_position_pct"] / 100 / price))
        if qty < 1:
            continue
        why = (f"Hakem: {('yükseliş' if r['direction'] == 'long' else 'düşüş')}"
               + (f", olasılık %{r['probability'] * 100:.0f}" if r.get("probability") is not None else f", skor {r['score']:+.2f}")
               + " · oylar " + ", ".join(f"{bots.BOT_TR[k]} {'+' if v > 0 else '−'}" for k, v in r["votes"].items() if v))
        try:
            paper.place_order({"symbol": sym, "side": "buy" if r["direction"] == "long" else "sell", "qty": qty, "type": "market",
                               "stop_loss_pct": params["sl_pct"], "take_profit_pct": params["tp_pct"], "note": why[:200]},
                              origin="hakem")
            paper._log("bot", sym, why)
            placed += 1
        except paper.PaperError as e:
            skipped.append(f"{sym} ({e})")
    if skipped:
        paper._log("info", "", "Hakem atladı: " + "; ".join(skipped)[:400])
    LIVE["message"] = f"{res['date']}: {placed} sanal işlem açıldı"
    store.kv_set("hakem_traded", res["date"])
    return {"placed": placed, "skipped": skipped}


def tick(cfg: dict | None = None, now: int | None = None) -> dict:
    cfg = cfg or load_config()
    now = now or now_ts()
    store.init()
    et = datetime.fromtimestamp(now, ET)
    today = et.date()
    minute = et.hour * 60 + et.minute
    out: dict = {}
    has_data = bool(store.kv_get("last_download"))
    # daily update after the close (only when the full download has finished once)
    if (has_data and is_trading_day(today) and minute >= UPDATE_AFTER_ET and not _running("download")
            and not _running("nn_evolve") and not _running("rule_walk") and not _running("deepnet")
            and not _running("tree") and not _running("fleet_night")):     # bars must not change under a running neuroevolution
        last = store.kv_get("last_update") or 0
        if datetime.fromtimestamp(last, ET).date() < today:
            start_download(full=False)
            out["update"] = "started"
    old_off = retired(cfg)
    learn.RETIRED[0] = old_off
    if has_data:
        try:
            out.update(_fleet_jobs(cfg, now, today, minute))
        except Exception:  # noqa: BLE001
            log.exception("fleet jobs failed")
    # live learning (learn.py): morning signals, the 30-minute shadow scoreboard, the nightly cloning after the update
    if has_data and not old_off:
        try:
            out["learn"] = learn.tick(now, _start, _running)
        except Exception:  # noqa: BLE001
            log.exception("learn tick failed")
    # live referee at the open
    try:
        s = _pkg("paper").settings(cfg)
    except Exception:  # noqa: BLE001
        s = {}
    if (not old_off and s.get("evo_enabled") and is_trading_day(today) and EVO_FROM_ET <= minute <= EVO_UNTIL_ET
            and market_session(now) == "open" and store.kv_get("evo_traded") != today.isoformat() and not _running("evo_trade")):
        def _evo_trade() -> None:
            try:
                LIVE["evo"] = evo_live.trade(cfg)
            except Exception as e:  # noqa: BLE001
                log.exception("evolution bot trading failed")
                LIVE["evo"] = {"error": str(e)}
                store.kv_set("evo_traded", today.isoformat())
        _start("evo_trade", _evo_trade)      # takes a few minutes (2,600 stocks): own thread
        out["evo"] = "started"
    # the realistic $100 account reads today's evolution signals (read-only; computed once after the open)
    real_on = bool((cfg.get("real_sim") or {}).get("enabled", True)) if isinstance(cfg.get("real_sim"), dict) else True
    if (not old_off and real_on and has_data and is_trading_day(today) and EVO_FROM_ET <= minute <= EVO_UNTIL_ET
            and market_session(now) == "open" and store.kv_get("evo_signals_day") != today.isoformat()
            and (store.kv_get("evo_today") or {}).get("date") != today.isoformat()
            and not _running("evo_trade") and not _running("evo_signals")):
        def _evo_signals() -> None:
            try:
                evo_live.signals()
            except Exception:  # noqa: BLE001
                log.exception("evolution signals for the realistic account failed")
            finally:
                store.kv_set("evo_signals_day", today.isoformat())
        _start("evo_signals", _evo_signals)
        out["evo_signals"] = "started"
    if (not old_off and s.get("hakem_enabled") and has_data and is_trading_day(today) and DECIDE_FROM_ET <= minute <= DECIDE_UNTIL_ET
            and market_session(now) == "open" and store.kv_get("hakem_traded") != today.isoformat()):
        try:
            out["trade"] = trade_today(cfg)
        except Exception as e:  # noqa: BLE001
            log.exception("referee trading failed")
            LIVE["message"] = f"Hakem işlem yapamadı: {e}"
            store.kv_set("hakem_traded", today.isoformat())
    return out
