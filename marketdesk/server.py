"""Flask JSON API (SPEC "HTTP API") + the static web/ UI. Bound to 127.0.0.1 only."""
from __future__ import annotations

import importlib
import json
import logging
import math
import mimetypes
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from flask import Flask, Response, request, send_file, send_from_directory
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from . import __version__, db
from .collectors.base import GROUPS
from .config import DEFAULTS, SECRET_KEYS, load_config, public_config, save_config, set_source_enabled
from .scheduler import requirement_hint
from .util import (CLAUDE_IN_DIR, CLAUDE_OUT_DIR, DATA_DIR, ET, TR, WEB_DIR, close_minutes, fmt_et, fmt_tr, is_trading_day,
                   market_session, now_ts)

log = logging.getLogger("marketdesk.server")

APP_ID = "piyasa_masasi"  # lets main.py recognise an already running instance

# Windows can map .js to text/plain via the registry; force sane types for the UI files.
for _ext, _type in ((".js", "application/javascript"), (".mjs", "application/javascript"), (".css", "text/css"),
                    (".svg", "image/svg+xml"), (".json", "application/json"), (".woff2", "font/woff2")):
    mimetypes.add_type(_type, _ext)

SESSION_TR = {"premarket": "Piyasa öncesi", "open": "Açık", "afterhours": "Piyasa sonrası", "closed": "Kapalı"}
SOURCE_TYPES_TR = {"news": "Haber", "press": "Basın bülteni", "filing": "SEC dosyası", "official": "Resmi",
                   "social": "Sosyal", "api": "API"}
SENTIMENTS_TR = {"pos": "Olumlu", "neg": "Olumsuz", "neu": "Nötr"}
STATE_TR = {"ok": "OK", "partial": "Kısmi hata", "error": "Hata", "idle": "Bekliyor", "disabled": "Kapalı",
            "needs_config": "Ayar gerekli", "running": "Çalışıyor"}
EXTRA_META = {
    "sessions": SESSION_TR,
    "directions": {"long": "Yükseliş", "short": "Düşüş", "mixed": "Karışık"},
    "brief_kinds": {"sabah": "Sabah", "ogle": "Öğle", "aksam": "Akşam", "manual": "Anlık"},
    "brief_authors": {"kural": "Kural", "ollama": "Ollama", "claude": "Claude"},
    "impacts": {"high": "Yüksek", "medium": "Orta", "low": "Düşük"},
    "earnings_times": {"bmo": "Açılış öncesi", "amc": "Kapanış sonrası", "unknown": "Saat belirsiz"},
    "quote_groups": {"vadeli": "Vadeliler", "endeks": "Endeksler", "emtia": "Emtia", "faiz": "Faiz",
                     "doviz": "Döviz", "kripto": "Kripto", "izleme": "İzleme listesi"},
    "source_states": STATE_TR,
}
# Used only when analysis.classify cannot be imported.
FALLBACK_CATEGORIES = {
    "earnings": ("Bilanço", 0, "#4c8dff"), "guidance_up": ("Beklenti artırımı", 1, "#22c55e"),
    "guidance_down": ("Beklenti indirimi", -1, "#ef4444"), "analyst_up": ("Analist yükseltme", 1, "#16a34a"),
    "analyst_down": ("Analist düşürme", -1, "#dc2626"), "mna": ("Birleşme/Satın alma", 1, "#a855f7"),
    "fda": ("FDA", 0, "#14b8a6"), "clinical": ("Klinik çalışma", 0, "#0ea5e9"),
    "offering": ("Hisse satışı", -1, "#f97316"), "buyback": ("Geri alım", 1, "#84cc16"),
    "dividend": ("Temettü", 1, "#65a30d"), "insider": ("İçeriden işlem", 0, "#eab308"),
    "contract": ("Sözleşme", 1, "#06b6d4"), "lawsuit": ("Dava", -1, "#f43f5e"),
    "bankruptcy": ("İflas", -1, "#b91c1c"), "layoffs": ("İşten çıkarma", -1, "#fb7185"),
    "macro": ("Makro", 0, "#64748b"), "fed": ("Fed", 0, "#6366f1"), "ipo": ("Halka arz", 0, "#8b5cf6"),
    "halt": ("İşlem durdurma", 0, "#f59e0b"), "short_report": ("Açığa satış raporu", -1, "#be123c"),
    "split": ("Hisse bölünmesi", 0, "#0891b2"), "filing": ("SEC bildirimi", 0, "#94a3b8"),
    "product": ("Ürün", 1, "#3b82f6"), "crypto": ("Kripto", 0, "#f59e0b"),
    "geopolitics": ("Jeopolitik", 0, "#78716c"),
}
WATCHLIST_SOURCES = ("yahoo_quotes", "watchlist_news", "alpaca_snapshots", "finnhub_company")
MOVER_LISTS = ("gainers", "losers", "actives", "premarket", "postmarket", "small_caps")
# Watchlist-only alerts (below the importance threshold) need one of these tags or importance >= max(40, thr - 30).
STRONG_ALERT_CATS = {"earnings", "mna", "fda", "clinical", "offering", "halt", "bankruptcy"}
STRONG_ALERT_PREFIXES = ("guidance_", "analyst_")
_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-^=]{0,14}$")
_HHMM_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400) -> None:
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- helpers

def _lazy(modname: str):
    try:
        return importlib.import_module(f"{__package__}.{modname}")
    except Exception as e:  # noqa: BLE001 - modules written in parallel may be missing/broken
        log.debug("module %s unavailable: %s", modname, e)
        return None


def _finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


class _JSON(DefaultJSONProvider):
    """Never emit NaN/Infinity (invalid JSON for the browser)."""
    ensure_ascii = False
    sort_keys = False

    def dumps(self, obj, **kw):
        kw.setdefault("ensure_ascii", False)
        kw.setdefault("sort_keys", False)
        kw.setdefault("default", self.default)
        try:
            return json.dumps(obj, allow_nan=False, **kw)
        except ValueError:
            return json.dumps(_finite(obj), allow_nan=False, **kw)


def _arg(name: str) -> str | None:
    v = request.args.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _int_arg(name: str, default=None, lo: int | None = None, hi: int | None = None):
    v = _arg(name)
    if v is None:
        return default
    try:
        x = int(float(v))
    except (ValueError, OverflowError):
        raise ApiError(f"Geçersiz parametre: {name}")
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _float_arg(name: str, default=None, lo: float | None = None, hi: float | None = None):
    v = _arg(name)
    if v is None:
        return default
    try:
        x = float(v)
    except ValueError:
        raise ApiError(f"Geçersiz parametre: {name}")
    if not math.isfinite(x):
        raise ApiError(f"Geçersiz parametre: {name}")
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _list_arg(name: str) -> list[str] | None:
    v = _arg(name)
    if v is None:
        return None
    out = [s.strip() for s in v.split(",") if s.strip()]
    return out or None


def _body() -> dict:
    data = request.get_json(silent=True, force=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError("Gövde bir JSON nesnesi olmalı")
    return data


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "evet", "yes", "on", "açık")
    return bool(v)


def _norm_sym(sym: str | None) -> str:
    return (sym or "").strip().upper().lstrip("$")


def _watchlist(cfg: dict) -> list[str]:
    return [s for s in (_norm_sym(x) for x in (cfg.get("watchlist") or [])) if s]


def _at_et(d: date, h: int = 0, m: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())


def _dedupe_earnings(items: list[dict]) -> list[dict]:
    """Nasdaq and Finnhub both report earnings; keep one row per (ticker, ET date), preferring the richer one."""
    best: dict[tuple, dict] = {}
    out: list[dict] = []
    for e in items:
        if e.get("kind") != "earnings" or not e.get("ticker"):
            out.append(e)
            continue
        ex = e.get("extra") or {}
        key = (e["ticker"], ex.get("date_et") or ex.get("date") or datetime.fromtimestamp(e["ts"], ET).date().isoformat())
        prev = best.get(key)
        if prev is None:
            best[key] = e
            out.append(e)
            continue
        richer = e if (e.get("source") != "finnhub" and prev.get("source") == "finnhub") else prev
        other = prev if richer is e else e
        merged_extra = {**(other.get("extra") or {}), **{k: v for k, v in (richer.get("extra") or {}).items() if v is not None}}
        richer["extra"] = merged_extra
        if richer is e:
            out[out.index(prev)] = e
            best[key] = e
    return out


def next_open_close(now: int) -> tuple[int | None, int | None]:
    """Next regular-session open (09:30 ET) and close (16:00 ET, 13:00 on early-close days) strictly after `now`."""
    d = datetime.fromtimestamp(now, ET).date()
    nxt_open = nxt_close = None
    for i in range(15):
        day = d + timedelta(days=i)
        if not is_trading_day(day):
            continue
        cm = close_minutes(day)
        o, c = _at_et(day, 9, 30), _at_et(day, cm // 60, cm % 60)
        if nxt_open is None and o > now:
            nxt_open = o
        if nxt_close is None and c > now:
            nxt_close = c
        if nxt_open and nxt_close:
            break
    return nxt_open, nxt_close


def session_end_ts(now: int, session: str | None = None) -> int | None:
    """End of the current session part: premarket -> 09:30 ET, open -> close (13:00 on early-close days),
    afterhours -> close + 4 h (20:00, or 17:00 on early-close days); None while closed."""
    session = session or market_session(now)
    d = datetime.fromtimestamp(now, ET).date()
    cm = close_minutes(d)
    end = {"premarket": 9 * 60 + 30, "open": cm, "afterhours": cm + 240}.get(session)
    return _at_et(d, end // 60, end % 60) if end is not None else None


def _utc_offset(tz, now: int) -> int:
    off = datetime.fromtimestamp(now, tz).utcoffset()
    return int(off.total_seconds()) if off else 0


def _snap(key: str):
    s = db.get_snapshot(key)
    return (s[0], s[1]) if s else (None, None)


def _cluster_sizes_sql(cids: list[str]) -> dict[str, tuple[int, int]]:
    """Fallback when analysis.cluster is unavailable: rows and distinct publishers (extra.publisher, else the
    collector's source_name) per cluster."""
    sizes: dict[str, tuple[int, int]] = {}
    c = db.conn()
    for i in range(0, len(cids), 500):
        chunk = cids[i:i + 500]
        for r in c.execute("SELECT cluster_id, COUNT(*), COUNT(DISTINCT lower(COALESCE(NULLIF(json_extract(extra, "
                           "'$.publisher'), ''), source_name))) FROM news "
                           f"WHERE cluster_id IN ({','.join('?' * len(chunk))}) GROUP BY cluster_id", chunk):
            sizes[r[0]] = (r[1], r[2])
    return sizes


def _annotate_clusters(items: list[dict]) -> list[dict]:
    """Adds cluster_size = cluster_sources (independent publishers in the story, for "+N kaynak") and
    cluster_items (rows in the cluster) to news rows."""
    cids = list({it["cluster_id"] for it in items if it.get("cluster_id")})
    sizes: dict[str, tuple[int, int]] = {}
    if cids:
        cluster = _lazy("analysis.cluster")
        try:
            sizes = (cluster.cluster_sizes(cids) if cluster is not None and hasattr(cluster, "cluster_sizes")
                     else _cluster_sizes_sql(cids))
        except Exception:  # noqa: BLE001 - a counting problem must not break the news list
            log.exception("cluster_sizes failed; using the SQL fallback")
            sizes = _cluster_sizes_sql(cids)
    for it in items:
        n, s = sizes.get(it.get("cluster_id") or "", (1, 1))
        it["cluster_size"] = it["cluster_sources"] = s
        it["cluster_items"] = n
    return items


def _categories() -> dict:
    classify = _lazy("analysis.classify")
    cats = getattr(classify, "CATEGORIES", None) if classify else None
    if isinstance(cats, dict) and cats:
        return cats
    return {k: {"label": lab, "bias": b, "weight": 1.0, "color": col} for k, (lab, b, col) in FALLBACK_CATEGORIES.items()}


def _collectors(sched) -> list:
    if sched is not None:
        return sched.collectors()
    try:
        from .collectors import all_collectors
        return list(all_collectors())
    except Exception:
        log.exception("collector registry failed")
        return []


def _group_rank(group: str) -> int:
    keys = list(GROUPS)
    return keys.index(group) if group in keys else len(keys)


# ---------------------------------------------------------------- app factory

def _strong_alert(it: dict, thr: float) -> bool:
    """A watchlist mention worth a toast even below the importance threshold."""
    if float(it.get("importance") or 0) >= max(40.0, thr - 30):
        return True
    return any(c in STRONG_ALERT_CATS or str(c).startswith(STRONG_ALERT_PREFIXES) for c in it.get("categories") or [])


def create_app(scheduler=None, *, shutdown=None, headless: bool = False) -> Flask:
    """`shutdown`: callable that stops the whole app (enables POST /api/shutdown); `headless`: the process
    was started without its own window (--no-window, e.g. the Windows startup shortcut)."""
    app = Flask(__name__, static_folder=None)
    app.json = _JSON(app)
    app.url_map.strict_slashes = False
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    ml_cache = {"ts": 0.0, "data": {"finbert": False, "ollama": False, "ollama_models": []}, "busy": False}
    quote_cache: dict[str, tuple[float, dict | None]] = {}
    quote_lock = threading.Lock()
    quote_pool = ThreadPoolExecutor(2, thread_name_prefix="live-quote")

    def sched():
        return scheduler

    # ------------------------------------------------------------ request guards / errors

    @app.before_request
    def _guard():
        if not request.path.startswith("/api/"):
            return None
        host = urlparse("//" + (request.host or "")).hostname or ""
        if host not in _LOCAL_HOSTS:  # DNS-rebinding protection
            return {"error": "Yalnızca yerel erişime izin verilir"}, 403
        origin = request.headers.get("Origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):  # CSRF from other web pages
            try:
                o = urlparse(origin)
                same = o.hostname in _LOCAL_HOSTS and (o.port or 80) == (urlparse("//" + request.host).port or 80)
            except ValueError:
                same = False
            if not same:
                return {"error": "Farklı kaynaktan istek reddedildi"}, 403
        return None

    @app.after_request
    def _headers(resp: Response):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    @app.errorhandler(ApiError)
    def _bad(e: ApiError):
        return {"error": str(e)}, e.status

    @app.errorhandler(HTTPException)
    def _http(e: HTTPException):
        msg = {404: "Bulunamadı", 405: "Bu yönteme izin verilmiyor", 400: "Geçersiz istek"}.get(e.code or 0, e.name)
        if request.path.startswith("/api/") or e.code != 404:
            return {"error": msg}, e.code or 500
        return Response(msg, status=404, mimetype="text/plain")

    @app.errorhandler(Exception)
    def _err(e: Exception):
        log.exception("unhandled error on %s %s", request.method, request.path)
        return {"error": f"Sunucu hatası: {type(e).__name__}: {str(e)[:200]}"}, 500

    # ------------------------------------------------------------ status / meta

    def _ml_status(cfg: dict) -> dict:
        def refresh():
            ml = _lazy("analysis.ml")
            data = {"finbert": False, "ollama": False, "ollama_models": []}
            if ml is not None:
                try:
                    data["finbert"] = bool(ml.finbert_available())
                except Exception:
                    log.debug("finbert check failed", exc_info=True)
                try:
                    models = list(ml.ollama_models(cfg) or [])
                    data["ollama_models"] = models
                    data["ollama"] = bool(models) or bool(ml.ollama_available(cfg))
                except Exception:
                    log.debug("ollama check failed", exc_info=True)
            ml_cache["data"], ml_cache["ts"], ml_cache["busy"] = data, time.time(), False

        if ml_cache["ts"] == 0:
            refresh()
        elif time.time() - ml_cache["ts"] > 30 and not ml_cache["busy"]:
            ml_cache["busy"] = True
            threading.Thread(target=refresh, name="ml-status", daemon=True).start()
        return dict(ml_cache["data"], finbert_enabled=bool(cfg.get("use_finbert")),
                    ollama_url=cfg.get("ollama_url"), ollama_model=cfg.get("ollama_model") or "")

    def _source_counts(cfg: dict) -> dict:
        disabled = set(cfg.get("disabled_sources") or [])
        status = db.list_source_status()
        ok = err = idle = off = 0
        last_update = 0
        groups: dict[str, dict[str, int]] = {}   # group -> {"err", "total"}: shows which source group is failing
        cols = _collectors(sched())
        for c in cols:
            s = status.get(c.key) or {}
            last_update = max(last_update, int(s.get("last_ok") or 0))
            g = groups.setdefault(str(c.group or "diger"), {"err": 0, "total": 0})
            g["total"] += 1
            if c.key in disabled:
                off += 1
                idle += 1
            elif c.missing_requirements(cfg) or not s.get("last_run"):
                idle += 1
            elif s.get("last_error"):
                err += 1
                g["err"] += 1
            else:
                ok += 1
        return {"sources_ok": ok, "sources_err": err, "sources_idle": idle, "sources_disabled": off,
                "sources_total": len(cols), "source_groups": groups, "last_update": last_update or None}

    @app.get("/api/status")
    def api_status():
        cfg = load_config()
        now = now_ts()
        session = market_session(now)
        nxt_open, nxt_close = next_open_close(now)
        sc = _source_counts(cfg)
        last_update = sc.pop("last_update")
        counts = {"news_24h": db.count_news(now - 86400), "news_total": db.count_news(), **sc}
        briefs = _lazy("briefs")
        next_briefs = []
        if briefs is not None and hasattr(briefs, "next_brief_times"):
            try:
                next_briefs = briefs.next_brief_times(cfg, now)
            except Exception:
                log.exception("next_brief_times failed")
        s = sched()
        sst = s.status() if s is not None else {}
        return {
            "app": APP_ID, "version": __version__, "now": now,
            "et_time": fmt_et(now, "%H:%M"), "tr_time": fmt_tr(now, "%H:%M"),
            "et_date": fmt_et(now, "%Y-%m-%d"), "tr_date": fmt_tr(now, "%Y-%m-%d"),
            "et_offset": _utc_offset(ET, now), "tr_offset": _utc_offset(TR, now),
            "session": session, "session_label": SESSION_TR.get(session, session),
            "trading_day": is_trading_day(datetime.fromtimestamp(now, ET).date()),
            "next_open_ts": nxt_open, "next_close_ts": nxt_close, "session_end_ts": session_end_ts(now, session),
            "counts": counts, "last_update": last_update,
            "instance": {"pid": os.getpid(), "headless": headless, "can_shutdown": shutdown is not None},
            "ml": _ml_status(cfg), "next_briefs": next_briefs,
            "scheduler": {"running": bool(sst.get("running")), "active": sst.get("active", []),
                          "started_at": sst.get("started_at")},
            "paths": {"data": str(DATA_DIR), "claude_in": str(CLAUDE_IN_DIR), "claude_out": str(CLAUDE_OUT_DIR),
                      "log": str(DATA_DIR / "marketdesk.log")},
        }

    @app.get("/api/meta")
    def api_meta():
        return {"categories": _categories(), "groups": GROUPS, "source_types": SOURCE_TYPES_TR,
                "sentiments": SENTIMENTS_TR, **EXTRA_META}

    # ------------------------------------------------------------ news

    @app.get("/api/news")
    def api_news():
        now = now_ts()
        since_h = _float_arg("since_hours", 24, 0, 24 * 90)
        order = _arg("order") or "published"
        if order not in ("published", "fetched", "importance"):
            raise ApiError("Geçersiz parametre: order")
        sentiment = _arg("sentiment")
        if sentiment and sentiment not in SENTIMENTS_TR:
            raise ApiError("Geçersiz parametre: sentiment")
        items, total = db.query_news(
            q=(_arg("q") or "")[:200] or None, sources=_list_arg("source"), source_types=_list_arg("source_type"),
            category=_arg("category"), ticker=_norm_sym(_arg("ticker")) or None, sentiment=sentiment,
            since_ts=int(now - since_h * 3600) if since_h and since_h > 0 else None,
            after_fetched=_int_arg("after_fetched"), min_importance=_float_arg("min_importance"),
            order=order, limit=_int_arg("limit", 200, 1, 500), offset=_int_arg("offset", 0, 0, 10 ** 7))
        return {"items": _annotate_clusters(items), "total": total}

    @app.get("/api/news/by_source")
    def api_news_by_source():
        now = now_ts()
        since_h = _float_arg("since_hours", 24, 0, 24 * 90)
        since = int(now - since_h * 3600) if since_h > 0 else 0
        per = _int_arg("per_source", 15, 1, 100)
        groups = set(_list_arg("group") or [])
        include_empty = _bool(_arg("include_empty") or "")
        counts = db.news_source_counts(since)
        cols = _collectors(sched())
        known = {c.key for c in cols}
        disabled = set(load_config().get("disabled_sources") or [])
        entries = []
        for i, c in enumerate(cols):
            if groups and c.group not in groups:
                continue
            if not counts.get(c.key) and not (include_empty and c.kind == "news"):
                continue
            entries.append((_group_rank(c.group), i, c.key, c.name, c.group, c.source_type, c.key not in disabled))
        if not groups or "" in groups:  # news from sources no longer registered
            for k in sorted(set(counts) - known):
                entries.append((len(GROUPS) + 1, 0, k, None, "", None, True))
        entries.sort()
        out = []
        for _, _, key, name, group, stype, enabled in entries:
            items, _ = db.query_news(sources=[key], since_ts=since or None, limit=per)
            if items:
                name = name or items[0]["source_name"]
                stype = stype or items[0]["source_type"]
            out.append({"source": key, "source_name": name or key, "group": group,
                        "group_label": GROUPS.get(group, "Diğer"), "source_type": stype or "news",
                        "enabled": enabled, "total": counts.get(key, 0), "items": _annotate_clusters(items)})
        return {"sources": out}

    @app.get("/api/clusters")
    def api_clusters():
        cluster = _lazy("analysis.cluster")
        if cluster is None or not hasattr(cluster, "list_clusters"):
            return {"items": [], "available": False}
        items = cluster.list_clusters(since_hours=_int_arg("since_hours", 24, 1, 24 * 30),
                                      min_sources=_int_arg("min_sources", 2, 1, 50),
                                      limit=_int_arg("limit", 100, 1, 300))
        return {"items": items or [], "available": True}

    # ------------------------------------------------------------ ticker

    def _find_quote(sym: str) -> tuple[dict | None, str | None]:
        quotes, _ = _snap("quotes")
        for q in (quotes or {}).get("items") or []:
            if isinstance(q, dict) and _norm_sym(q.get("symbol")) == sym:
                return q, "quotes"
        snaps, _ = _snap("alpaca_snapshots")
        items = (snaps or {}).get("items")
        if isinstance(items, dict) and isinstance(items.get(sym), dict):
            return dict(items[sym], symbol=sym), "alpaca_snapshots"
        for key in ("movers", "movers_alpaca"):
            m, _ = _snap(key)
            for lst in MOVER_LISTS:
                for q in (m or {}).get(lst) or []:
                    if isinstance(q, dict) and _norm_sym(q.get("symbol")) == sym:
                        return q, key
            # quotes_extra: one v7 quote per symbol the app follows that no screener lists (movers collector)
            extra = (m or {}).get("quotes_extra") if isinstance(m, dict) else None
            if isinstance(extra, dict) and isinstance(extra.get(sym), dict):
                return dict(extra[sym], symbol=sym), key
        return None, None

    def _live_quote(sym: str) -> dict | None:
        hit = quote_cache.get(sym)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        market = _lazy("collectors.market")
        q = None
        if market is not None and hasattr(market, "_chart") and hasattr(market, "parse_chart_quote"):
            ysym = market._yahoo_symbol(sym) if hasattr(market, "_yahoo_symbol") else sym   # BRK.B -> BRK-B

            def fetch():
                q = market.parse_chart_quote(market._chart(ysym), group="izleme")
                if isinstance(q, dict) and ysym != sym:
                    q = dict(q, symbol=sym)
                return q
            try:
                fut = quote_pool.submit(fetch)
                q = fut.result(timeout=12)
            except Exception as e:  # noqa: BLE001 - unknown symbol, timeout, Yahoo down
                log.info("live quote %s failed: %s", sym, e)
        quote_cache[sym] = (time.time(), q)
        with quote_lock:  # several request threads may evict at once: sort a snapshot, never the live dict
            if len(quote_cache) > 300:
                for k, _ in sorted(list(quote_cache.items()), key=lambda kv: kv[1][0])[:100]:
                    quote_cache.pop(k, None)
        return q

    @app.get("/api/ticker/<sym>")
    def api_ticker(sym: str):
        sym = _norm_sym(sym)
        if not _SYM_RE.match(sym):
            raise ApiError("Geçersiz sembol")
        cfg = load_config()
        now = now_ts()
        since_h = _float_arg("since_hours", 168, 1, 24 * 30)
        news, _ = db.query_news(ticker=sym, since_ts=int(now - since_h * 3600), limit=_int_arg("limit", 300, 1, 500))
        _annotate_clusters(news)
        by_source: dict[str, list] = {}
        for it in news:
            by_source.setdefault(it.get("source_name") or it.get("source") or "?", []).append(it)
        # Independent voices (publisher, syndicated identical titles once), not collectors.
        source_count = len(by_source)
        cluster = _lazy("analysis.cluster")
        if cluster is not None and hasattr(cluster, "voices") and news:
            try:
                source_count = len(cluster.voices(news))
            except Exception:  # noqa: BLE001
                log.debug("voices failed", exc_info=True)
        today = datetime.fromtimestamp(now, ET).date()
        events = db.query_events(_at_et(today - timedelta(days=3)), _at_et(today + timedelta(days=60)), ticker=sym)
        quote, quote_src = _find_quote(sym)
        if quote is None and _bool(_arg("live") or "1"):
            quote = _live_quote(sym)
            quote_src = "yahoo_live" if quote else None
        social_snap, social_ts = _snap("social")
        social = {"apewisdom": None, "stocktwits": None, "ts": social_ts}
        for k in ("apewisdom", "stocktwits"):
            for r in (social_snap or {}).get(k) or []:
                if isinstance(r, dict) and _norm_sym(r.get("symbol")) == sym:
                    social[k] = r
                    break
        opp_snap, _ = _snap("opportunities")
        opportunity = next((o for o in (opp_snap or {}).get("items") or []
                            if isinstance(o, dict) and _norm_sym(o.get("ticker")) == sym), None)
        company = None
        tickers = _lazy("analysis.tickers")
        if tickers is not None and hasattr(tickers, "company_name"):
            try:
                company = tickers.company_name(sym)
            except Exception:
                log.debug("company_name failed", exc_info=True)
        if not company and quote and quote.get("name") and quote.get("name") != sym:
            company = quote.get("name")
        sents = [float(it.get("sentiment") or 0) for it in news]
        return {
            "ticker": sym, "company": company, "news": news, "by_source": by_source, "events": events,
            "quote": quote, "quote_source": quote_src, "social": social, "opportunity": opportunity,
            "sentiment_avg": round(sum(sents) / len(sents), 3) if sents else None,
            "news_count": len(news), "source_count": source_count, "in_watchlist": sym in _watchlist(cfg),
        }

    # ------------------------------------------------------------ opportunities / market / calendar

    @app.get("/api/opportunities")
    def api_opportunities():
        data, ts = _snap("opportunities")
        if not isinstance(data, dict):
            return {"items": [], "ts": None, "updated": None}
        data = dict(data)
        if not isinstance(data.get("items"), list):
            data["items"] = []
        data["updated"] = ts
        return data

    @app.get("/api/market")
    def api_market():
        empty_movers = {"ts": None, "source": None, "gainers": [], "losers": [], "actives": []}
        defaults = {"quotes": {"ts": None, "items": []}, "movers": empty_movers, "movers_alpaca": empty_movers,
                    "fear_greed": None, "social": {"ts": None, "apewisdom": [], "stocktwits": []},
                    "alpaca_snapshots": {"ts": None, "items": {}},
                    "halts": {"ts": None, "items": [], "luld_today": {}}}
        out: dict = {}
        updated: dict = {}
        for key, default in defaults.items():
            data, ts = _snap(key)
            out[key] = data if data is not None else default
            updated[key] = ts
        # "Neden?" column: catalyst headline per mover, precomputed by the scheduler (snapshot movers_why).
        why, why_ts = _snap("movers_why")
        cats = why.get("items") if isinstance(why, dict) else None
        if isinstance(cats, dict):
            for key in ("movers", "movers_alpaca"):
                m = out.get(key)
                if not isinstance(m, dict):
                    continue
                m = dict(m)
                for lst in MOVER_LISTS:
                    rows = m.get(lst)
                    if isinstance(rows, list):
                        m[lst] = [dict(r, catalyst=cats.get(_norm_sym(str(r.get("symbol") or ""))))
                                  if isinstance(r, dict) else r for r in rows]
                out[key] = m
        updated["movers_why"] = why_ts
        out["updated"] = updated
        return out

    @app.get("/api/signals")
    def api_signals():
        """Opportunity score history for the day-trade bot (db.signals). With `since` the oldest rows come
        first, so a poller continues from the largest `ts` it has received."""
        ticker = _norm_sym(_arg("ticker"))
        if ticker and not _SYM_RE.match(ticker):
            raise ApiError("Geçersiz sembol")
        items = db.query_signals(since=_int_arg("since"), until=_int_arg("until"), ticker=ticker or None,
                                 min_score=_float_arg("min_score", None, 0, 100),
                                 limit=_int_arg("limit", 1000, 1, 5000))
        return {"schema_version": db.SIGNALS_SCHEMA_VERSION, "items": items, "now": now_ts()}

    @app.get("/api/calendar")
    def api_calendar():
        cfg = load_config()
        days = _int_arg("days", 7, 1, 60)
        kind = _arg("kind")
        if kind and kind not in ("macro", "earnings", "ipo"):
            raise ApiError("Geçersiz parametre: kind")
        impacts = set(_list_arg("impact") or [])
        prev = _int_arg("prev", 0, 0, 1)
        today = datetime.fromtimestamp(now_ts(), ET).date()
        first = today
        if prev:  # also the previous trading day (its earnings rows carry the reported EPS / surprise)
            first = today - timedelta(days=1)
            while not is_trading_day(first):
                first -= timedelta(days=1)
        start, end = _at_et(first), _at_et(today + timedelta(days=days))
        items = _dedupe_earnings(db.query_events(start, end, kind=kind, ticker=_norm_sym(_arg("ticker")) or None))
        if impacts:
            items = [e for e in items if (e.get("impact") or "") in impacts]
        wl = set(_watchlist(cfg))
        for e in items:
            e["in_watchlist"] = bool(e.get("ticker")) and e["ticker"] in wl
        return {"items": items, "start": start, "end": end}

    # ------------------------------------------------------------ briefs

    def _briefs_mod():
        b = _lazy("briefs")
        if b is None:
            raise ApiError("Brifing modülü yüklenemedi", 503)
        return b

    def _kind(b, body: dict) -> str:
        raw = body.get("kind") or "manual"
        kind = b.normalize_kind(raw) if hasattr(b, "normalize_kind") else str(raw)
        if not kind:
            raise ApiError("Geçersiz brifing türü (sabah | ogle | aksam | manual)")
        return kind

    @app.get("/api/briefs")
    def api_briefs():
        return {"items": db.list_briefs(limit=_int_arg("limit", 30, 1, 200), kind=_arg("kind"), author=_arg("author"))}

    @app.post("/api/briefs/generate")
    def api_briefs_generate():
        body = _body()
        b = _briefs_mod()
        kind = _kind(b, body)
        engine = str(body.get("engine") or "kural").lower()
        cfg = load_config()
        if engine == "kural":
            return b.generate(kind, cfg)
        if engine == "ollama":
            ml = _lazy("analysis.ml")
            if ml is None or not ml.ollama_available(cfg):
                raise ApiError("Ollama çalışmıyor ya da yüklü model yok (ollama.com → `ollama pull qwen2.5:7b`)", 503)
            brief = b.generate_ollama(kind, cfg)
            if not brief:
                raise ApiError("Ollama brifing üretemedi (model yanıt vermedi veya yanıt çok kısa)", 502)
            return brief
        raise ApiError("Geçersiz motor (kural | ollama)")

    @app.post("/api/briefs/claude_export")
    def api_claude_export():
        body = _body()
        b = _briefs_mod()
        kind = _kind(b, body)
        path = b.export_for_claude(kind, load_config())
        out = {"path": str(path), "latest": str(CLAUDE_IN_DIR / "latest.md"), "out_dir": str(CLAUDE_OUT_DIR)}
        if hasattr(b, "claude_paths"):
            try:
                out["out_path"] = str(b.claude_paths(kind)[1])
            except Exception:
                log.debug("claude_paths failed", exc_info=True)
        return out

    # ------------------------------------------------------------ sources

    @app.get("/api/sources")
    def api_sources():
        cfg = load_config()
        s = sched()
        status = db.list_source_status()
        counts = db.news_source_counts(now_ts() - 86400)
        cols = sorted(enumerate(_collectors(s)), key=lambda x: (_group_rank(x[1].group), x[0]))
        items = []
        for _, c in cols:
            info = c.info(cfg)
            st = status.get(c.key) or {}
            job = s.job(c.key) if s is not None else None
            for k in ("last_run", "last_ok", "last_error"):
                info[k] = st.get(k)
            info["items_last"] = int(st.get("items_last") or 0)
            info["items_total"] = int(st.get("items_total") or 0)
            info["news_24h"] = counts.get(c.key, 0)
            info["group_label"] = GROUPS.get(c.group, c.group)
            info["running"] = bool(job and job["running"])
            info["next_run"] = job["next_due"] if job and info["enabled"] and not info["requires_missing"] else None
            info["fails"] = job["fails"] if job else 0
            info["fetched_last"] = job["fetched_last"] if job else None
            info["duration_last"] = job["last_duration"] if job else None
            missing = info["requires_missing"]
            info["hint"] = requirement_hint(missing)
            # Partial failure of the last successful run (some feeds / days failed): this process's job state,
            # else the value stored by an earlier run. Never written to last_error (that would turn it red).
            warning = job.get("warning") if job and job.get("last_end") else st.get("last_warning")
            info["warning"] = warning or None
            if not info["enabled"]:
                state = "disabled"
            elif missing:
                state = "needs_config"
            elif info["running"]:
                state = "running"
            elif st.get("last_error"):
                state = "error"
            elif st.get("last_ok"):
                state = "partial" if warning else "ok"
            else:
                state = "idle"
            info["state"], info["state_label"] = state, STATE_TR[state]
            items.append(info)
        return {"items": items, "groups": GROUPS}

    @app.post("/api/sources/toggle")
    def api_sources_toggle():
        body = _body()
        key = str(body.get("source") or "").strip()
        if not key:
            raise ApiError("source gerekli")
        if key not in {c.key for c in _collectors(sched())}:
            raise ApiError("Bilinmeyen kaynak", 404)
        enabled = _bool(body.get("enabled", True))
        cfg = set_source_enabled(key, enabled)  # read-modify-write under the config lock (overlapping toggles)
        queued = sched().refresh_now(key, rerun_running=True) if enabled and sched() is not None else []
        return {"ok": True, "source": key, "enabled": enabled, "disabled_sources": cfg["disabled_sources"],
                "queued": queued}

    @app.post("/api/refresh")
    def api_refresh():
        body = _body()
        s = sched()
        if s is None:
            raise ApiError("Zamanlayıcı çalışmıyor", 503)
        source = str(body.get("source") or "").strip() or None
        if source and source not in {c.key for c in _collectors(s)}:
            raise ApiError("Bilinmeyen kaynak", 404)
        return {"queued": s.refresh_now(source)}

    # ------------------------------------------------------------ alerts

    @app.get("/api/alerts")
    def api_alerts():
        cfg = load_config()
        now = now_ts()
        upto = now - 1  # rows fetched during this very second are returned by the next poll
        since = _int_arg("since", 0)
        if not since or since <= 0:
            return {"items": [], "now": upto}
        thr = float(cfg.get("alert_min_importance") or 70)
        wl = set(_watchlist(cfg))
        rows, _ = db.query_news(after_fetched=since, since_ts=now - 6 * 3600, order="fetched", limit=500)
        out = []
        for it in rows:
            if int(it.get("fetched_at") or 0) > upto:
                continue
            hits = sorted(set(it.get("tickers") or []) & wl)
            important = float(it.get("importance") or 0) >= thr
            if not important and not (hits and _strong_alert(it, thr)):
                continue
            it["watchlist_hits"] = hits
            it["alert_reason"] = ("Önemli haber" if important else "") + \
                (" · " if important and hits else "") + ("İzleme listesi: " + ", ".join(hits) if hits else "")
            out.append(it)
        out.sort(key=lambda r: (-float(r.get("importance") or 0), -int(r.get("fetched_at") or 0)))
        out = sorted(out[:20], key=lambda r: -int(r.get("fetched_at") or 0))
        return {"items": _annotate_clusters(out), "now": upto}

    # ------------------------------------------------------------ config

    def _config_out(cfg: dict | None = None) -> dict:
        out = public_config(cfg)
        out.update({"data_dir": str(DATA_DIR), "claude_in_dir": str(CLAUDE_IN_DIR),
                    "claude_out_dir": str(CLAUDE_OUT_DIR)})
        return out

    def _validate_config(body: dict) -> dict:
        upd: dict = {}
        for k, v in body.items():
            if k not in DEFAULTS or k in ("paper", "micro", "real_sim"):   # bot settings have their own validated endpoints
                continue
            if k in SECRET_KEYS:
                if v is None:
                    continue
                upd[k] = str(v).strip()
            elif k == "watchlist":
                if isinstance(v, str):
                    v = re.split(r"[,;\s]+", v)
                if not isinstance(v, list):
                    raise ApiError("Geçersiz değer: watchlist")
                syms = [_norm_sym(str(s)) for s in v if str(s).strip()]
                bad = [s for s in syms if not _SYM_RE.match(s)]
                if bad:
                    raise ApiError("Geçersiz sembol: " + ", ".join(bad[:5]))
                upd[k] = syms
            elif k == "disabled_sources":
                if not isinstance(v, list):
                    raise ApiError("Geçersiz değer: disabled_sources")
                upd[k] = list(dict.fromkeys(str(s).strip() for s in v if str(s).strip()))
            elif k == "brief_times_et":
                if not isinstance(v, dict):
                    raise ApiError("Geçersiz değer: brief_times_et")
                times = {}
                for kind, hhmm in v.items():
                    if kind not in ("sabah", "ogle", "aksam"):
                        continue
                    hhmm = str(hhmm or "").strip()
                    if not hhmm:
                        continue  # empty = that brief is not scheduled
                    m = _HHMM_RE.match(hhmm)
                    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                        raise ApiError(f"Geçersiz saat ({kind}): {hhmm} — SS:DD biçiminde girin")
                    times[kind] = f"{int(m.group(1)):02d}:{m.group(2)}"
                upd[k] = times
            elif k == "alert_min_importance":
                try:
                    upd[k] = max(0, min(100, int(float(v))))
                except (TypeError, ValueError):
                    raise ApiError("Geçersiz değer: alert_min_importance (0–100)")
            elif k == "port":
                try:
                    p = int(v)
                except (TypeError, ValueError):
                    raise ApiError("Geçersiz değer: port")
                if not 1024 <= p <= 65535:
                    raise ApiError("Port 1024–65535 arasında olmalı")
                upd[k] = p
            elif k == "use_finbert":
                upd[k] = _bool(v)
            elif k == "ollama_url":
                v = str(v or "").strip().rstrip("/") or DEFAULTS["ollama_url"]
                if not re.match(r"^https?://", v):
                    raise ApiError("Ollama adresi http:// ile başlamalı")
                upd[k] = v
            elif k == "sec_user_agent":
                v = " ".join(str(v or "").split())
                if v and "@" not in v:
                    raise ApiError("SEC için geçerli bir e-posta adresi girin (ör. Ad Soyad ad@ornek.com)")
                if v and " " not in v:
                    v = f"PiyasaMasasi {v}"  # SEC wants "Name email"
                upd[k] = v
            else:
                upd[k] = v if not isinstance(DEFAULTS[k], str) else str(v or "").strip()
        return upd

    @app.get("/api/config")
    def api_config_get():
        return _config_out()

    @app.post("/api/config")
    def api_config_post():
        body = _body()
        before = load_config()
        upd = _validate_config(body)
        cfg = save_config(upd) if upd else before
        s = sched()
        if s is not None and "watchlist" in upd and upd["watchlist"] != before.get("watchlist"):
            for key in WATCHLIST_SOURCES:
                try:
                    s.refresh_now(key, rerun_running=True)  # a run in progress still uses the old watchlist
                except Exception:
                    log.exception("refresh after watchlist change failed")
        return _config_out(cfg)

    # ------------------------------------------------------------ paper trading (simulated, never a real broker)

    def _paper():
        mod = _lazy("paper")
        if mod is None:
            raise ApiError("Sanal işlem modülü yüklenemedi", 503)
        return mod

    def _paper_call(fn, *a, **kw):
        mod = _paper()
        try:
            return fn(mod)(*a, **kw)
        except mod.PaperError as e:
            raise ApiError(str(e))

    @app.get("/api/paper")
    def api_paper():
        return _paper().summary(limit_orders=_int_arg("limit", 100, 1, 500))

    @app.get("/api/paper/quote")
    def api_paper_quote():
        sym = _norm_sym(_arg("symbol"))
        if not _SYM_RE.match(sym):
            raise ApiError("Geçersiz sembol")
        mod = _paper()
        q = mod.quotes([sym]).get(sym)
        if not q:
            raise ApiError(f"{sym} için fiyat alınamadı", 404)
        return dict(q, symbol=sym, session=market_session(now_ts()))

    @app.post("/api/paper/order")
    def api_paper_order():
        order = _paper_call(lambda m: m.place_order, _body())
        return {"order": order, "summary": _paper().summary()}

    @app.post("/api/paper/cancel")
    def api_paper_cancel():
        _paper_call(lambda m: m.cancel_order, str(_body().get("id") or ""))
        return {"ok": True, "summary": _paper().summary()}

    @app.post("/api/paper/close")
    def api_paper_close():
        body = _body()
        sym = None if _bool(body.get("all")) else _norm_sym(body.get("symbol"))
        if sym is not None and not _SYM_RE.match(sym):
            raise ApiError("Geçersiz sembol")
        orders = _paper_call(lambda m: m.close_position, sym)
        return {"orders": orders, "summary": _paper().summary()}

    @app.post("/api/paper/position")
    def api_paper_position():
        body = _body()
        sym = _norm_sym(body.get("symbol"))
        _paper_call(lambda m: m.set_position_levels, sym, body.get("stop_price"), body.get("target_price"))
        return {"ok": True, "summary": _paper().summary()}

    @app.post("/api/paper/settings")
    def api_paper_settings():
        s = _paper_call(lambda m: m.update_settings, _body())
        return {"settings": s, "summary": _paper().summary()}

    @app.post("/api/paper/reset")
    def api_paper_reset():
        body = _body()
        if not _bool(body.get("confirm")):
            raise ApiError("Sıfırlamak için onay gerekli")
        return _paper_call(lambda m: m.reset, body.get("start_cash"))

    @app.get("/api/paper/micro")
    def api_paper_micro():
        mod = _lazy("micro_paper")
        if mod is None:
            raise ApiError("Mikro sanal işlem modülü yüklenemedi", 503)
        return mod.summary()

    @app.post("/api/paper/micro/reset")
    def api_paper_micro_reset():
        mod = _lazy("micro_paper")
        if mod is None:
            raise ApiError("Mikro sanal işlem modülü yüklenemedi", 503)
        if not _bool(_body().get("confirm")):
            raise ApiError("Sıfırlamak için onay gerekli")
        return mod.reset()

    @app.post("/api/paper/micro/order")
    def api_paper_micro_order():
        mod = _lazy("micro_paper")
        if mod is None:
            raise ApiError("Mikro sanal işlem modülü yüklenemedi", 503)
        body = {k: v for k, v in _body().items() if not str(k).startswith("_")}   # internal flags are not for the API
        try:
            return mod.place_order(body)
        except ValueError as e:
            raise ApiError(str(e), 400)

    def _real():
        mod = _lazy("real_sim")
        if mod is None:
            raise ApiError("Gerçekçi hesap modülü yüklenemedi", 503)
        return mod

    @app.get("/api/paper/real")
    def api_paper_real():
        return _real().summary()

    @app.post("/api/paper/real/settings")
    def api_paper_real_settings():
        return _real().update_settings(_body())

    @app.post("/api/paper/real/reset")
    def api_paper_real_reset():
        if not _bool(_body().get("confirm")):
            raise ApiError("Sıfırlamak için onay gerekli")
        return _real().reset()

    # ------------------------------------------------------------ signal scorecard

    def _scorecard():
        mod = _lazy("scorecard")
        if mod is None:
            raise ApiError("Sinyal karnesi modülü yüklenemedi", 503)
        return mod

    @app.get("/api/scorecard")
    def api_scorecard():
        direction = _arg("direction") or ""
        if direction and direction not in ("long", "short", "mixed"):
            raise ApiError("Geçersiz parametre: direction")
        unit = "all" if _arg("unit") == "all" else "first"
        return _scorecard().aggregate(days=_int_arg("days", 30, 1, 180), min_score=_float_arg("min_score", 0, 0, 100),
                                      direction=direction, unit=unit)

    @app.post("/api/scorecard/run")
    def api_scorecard_run():
        """Evaluate due signals now (normally every 30 minutes in the background)."""
        return {"result": _scorecard().evaluate()}

    # ------------------------------------------------------------ test lab (5-year data, four bots, referee)

    def _lab():
        mod = _lazy("lab.runner")
        if mod is None:
            raise ApiError("Test laboratuvarı modülü yüklenemedi", 503)
        return mod

    @app.get("/api/lab")
    def api_lab():
        mod = _lab()
        out = mod.status()
        out["report"] = mod.hakem.last_report()
        out["sweep_report"] = mod.sweep.last_sweep()
        out["evo_report"] = mod.evolve.last_run()
        return out

    @app.post("/api/lab/audit")
    def api_lab_audit():
        r = _lab().start_audit()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/evo_today")
    def api_lab_evo_today():
        """Today's evolution-bot signals (needs today's open or premarket prices); read-only, places no orders."""
        mod = _lab()
        try:
            return mod.evo_live.signals()
        except RuntimeError as e:
            raise ApiError(str(e), 409)

    # ------------------------------------------------------------ live learning (lab/learn.py)

    @app.get("/api/learn")
    def api_learn():
        return _lab().learn_status()

    @app.post("/api/learn/settings")
    def api_learn_settings():
        body = _body()
        return {"settings": _lab().learn.update_settings({k: body[k] for k in ("speed", "auto_apply") if k in body})}

    @app.post("/api/learn/run")
    def api_learn_run():
        """Run the nightly learning now (normally automatic after the daily data update)."""
        r = _lab().start_learn()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/learn/rollback")
    def api_learn_rollback():
        if not _bool(_body().get("confirm")):
            raise ApiError("Geri almak için onay gerekli")
        try:
            return _lab().learn.rollback()
        except RuntimeError as e:
            raise ApiError(str(e), 409)

    @app.post("/api/lab/evolve")
    def api_lab_evolve():
        r = _lab().start_evolve(_body())
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/deep")
    def api_lab_deep():
        """Train the gradient-based deep network (yearly walk-forward over 10 years)."""
        r = _lab().start_deep()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/walk")
    def api_lab_walk():
        """Day-by-day evolution of our rule bots over the 10 years (one elimination a day)."""
        r = _lab().start_walk()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/nn_evolve")
    def api_lab_nn_evolve():
        r = _lab().start_nn(_body())
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/nn_today")
    def api_lab_nn_today():
        """Today's picks of the neural-network bots (needs today's open); read-only, places no orders."""
        mod = _lab()
        try:
            data = mod.evo_live.today_rows()
        except RuntimeError as e:
            raise ApiError(str(e), 409)
        return {"date": data["date"], "ts": data["ts"], "items": mod.neuro.score_today(data["rows"])}

    @app.post("/api/lab/sweep")
    def api_lab_sweep():
        r = _lab().start_sweep(_body())
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/download")
    def api_lab_download():
        r = _lab().start_download(full=not _bool(_body().get("update")))
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.get("/api/fleet")
    def api_fleet():
        """The bot tree: every live bot with its account, open positions, last trades and equity curve (read-only)."""
        fl = _lazy("fleet")
        if fl is None:
            raise ApiError("Bot filosu yüklenemedi", 503)
        out = fl.summary()
        lab = _lazy("lab.runner")
        if lab is not None:
            out["job"] = {"running": lab._running("tree"), "state": dict(lab.tree.TREE)}
        return out

    @app.post("/api/lab/tree")
    def api_lab_tree():
        """Build the bot tree: the 5-group day-by-day walk of both branches, then 100 accounts (retires the old bots)."""
        r = _lab().start_tree()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.get("/api/crypto/fleet")
    def api_crypto_fleet():
        """The crypto tree: every live crypto bot with its paper account, positions, trades and curve (read-only)."""
        from .crypto import cfleet, jobs
        out = cfleet.summary()
        out["job"] = jobs.status()
        return out

    @app.post("/api/crypto/tree")
    def api_crypto_tree():
        """Build the crypto tree (waits for the stock tree if that is running), then 40 paper accounts."""
        from .crypto import jobs
        r = jobs.start_tree()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/crypto/download")
    def api_crypto_download():
        from .crypto import jobs
        r = jobs.start_download()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/news_backfill")
    def api_lab_news_backfill():
        """12 months of Finnhub headlines for every universe stock (one-off, resumable)."""
        r = _lab().start_news_backfill()
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/stop")
    def api_lab_stop():
        return _lab().stop_download()

    @app.post("/api/lab/test")
    def api_lab_test():
        r = _lab().start_test(_body())
        if not r.get("ok"):
            raise ApiError(r.get("message") or "Başlatılamadı", 409)
        return r

    @app.post("/api/lab/today")
    def api_lab_today():
        """Referee's picks for today (needs today's open or premarket prices); read-only, places no orders."""
        try:
            return _lab().decide_today()
        except RuntimeError as e:
            raise ApiError(str(e), 409)

    @app.get("/api/scheduler")
    def api_scheduler():
        """Debug view of the background loop (per-source timing, analysis results, brief runs)."""
        s = sched()
        return s.status() if s is not None else {"running": False}

    # ------------------------------------------------------------ desktop helpers (optional for the UI)

    @app.post("/api/shutdown")
    def api_shutdown():
        """Stop the whole app (collection included), e.g. a background instance started by the startup
        shortcut. JSON body required: a cross-site form cannot send one without a CORS preflight."""
        if shutdown is None:
            raise ApiError("Bu çalışma kipinde uygulama buradan kapatılamaz", 503)
        if not request.is_json:
            raise ApiError("JSON gövde gerekli")
        _body()
        log.info("shutdown requested via /api/shutdown")
        threading.Timer(0.5, shutdown).start()  # let this response reach the window first
        return {"ok": True, "message": "Piyasa Masası kapatılıyor; veri toplama durdu."}

    @app.post("/api/open_folder")
    def api_open_folder():
        which = str(_body().get("which") or "data")
        path = {"data": DATA_DIR, "claude_in": CLAUDE_IN_DIR, "claude_out": CLAUDE_OUT_DIR}.get(which)
        if path is None:
            raise ApiError("Geçersiz klasör (data | claude_in | claude_out)")
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # noqa: S606 - fixed local folder
        return {"ok": True, "path": str(path)}

    # ------------------------------------------------------------ static UI

    @app.get("/")
    def index():
        page = WEB_DIR / "index.html"
        if page.exists():
            return send_file(page, max_age=0)
        return Response("<!doctype html><meta charset='utf-8'><title>Piyasa Masası</title>"
                        "<body style='background:#0d1117;color:#c9d1d9;font-family:Segoe UI,sans-serif;padding:40px'>"
                        "<h2>Piyasa Masası</h2><p>Arayüz dosyaları bulunamadı (<code>web/index.html</code>). "
                        "API çalışıyor: <a style='color:#58a6ff' href='/api/status'>/api/status</a></p></body>",
                        mimetype="text/html")

    @app.get("/<path:filename>")
    def static_files(filename: str):
        if filename.startswith("api/"):
            return {"error": "Bulunamadı"}, 404
        return send_from_directory(WEB_DIR, filename, max_age=0)

    return app


__all__ = ["create_app", "next_open_close", "session_end_ts", "APP_ID"]
