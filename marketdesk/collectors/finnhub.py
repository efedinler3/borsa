"""Finnhub REST API: market news (general / merger), watchlist company news, earnings calendar.

Docs: https://finnhub.io/docs/api (swagger: https://finnhub.io/static/swagger.json)
Auth: X-Finnhub-Token header (same key as the `token` query param, but keeps it out of URLs/logs).
Free tier: 60 calls/min, so every call goes through one module-wide throttle.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

import requests

from ..util import ET, clean_html, http_get, make_id, now_ts, to_ts
from .base import Collector
from .rss import normalize_publisher

log = logging.getLogger("marketdesk.collectors.finnhub")

BASE = "https://finnhub.io/api/v1"
MAX_AGE = 3 * 86400
MIN_GAP = 1.05  # seconds between calls across all Finnhub collectors (< 60/min)
_SYM_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")

_rate_lock = threading.Lock()
_last_call = 0.0


class FinnhubRateLimit(RuntimeError):
    pass


class FinnhubAuthError(RuntimeError):
    pass


def _throttle() -> None:
    global _last_call
    with _rate_lock:
        wait = _last_call + MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _token(cfg: dict) -> str:
    token = str(cfg.get("finnhub_key") or "").strip()
    if not token:
        raise FinnhubAuthError("Finnhub API anahtarı girilmemiş (Kaynaklar & Ayarlar → API anahtarları).")
    return token


def _get(path: str, cfg: dict, params: dict | None = None, timeout: float = 15):
    token = _token(cfg)
    _throttle()
    try:
        r = http_get(BASE + path, params=params, timeout=timeout,
                     headers={"X-Finnhub-Token": token, "Accept": "application/json"})
    except requests.RequestException as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        msg = ""
        try:
            body = resp.json() if resp is not None else None
            msg = str(body.get("error") or "") if isinstance(body, dict) else ""
        except ValueError:
            pass
        if status is None:
            raise RuntimeError(f"Finnhub'a bağlanılamadı: {type(e).__name__}") from e
        if status == 401:
            raise FinnhubAuthError("Finnhub API anahtarı geçersiz (HTTP 401).") from e
        if status == 403:
            raise RuntimeError("Finnhub erişim izni yok (HTTP 403): bu uç nokta ücretli planda olabilir.") from e
        if status == 429:
            raise FinnhubRateLimit("Finnhub istek sınırı aşıldı (HTTP 429), sonraki turda tekrar denenecek.") from e
        raise RuntimeError(f"Finnhub HTTP {status}" + (f": {msg[:200]}" if msg else "")) from e
    try:
        data = r.json()
    except ValueError as e:
        raise RuntimeError(f"Finnhub geçersiz yanıt döndürdü ({path}).") from e
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Finnhub hatası: {str(data['error'])[:200]}")
    return data


def _today_et() -> date:
    return datetime.now(ET).date()


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _name_lookup():
    """Optional company-name resolver from analysis.tickers (other module); time-boxed, never fatal."""
    try:
        from ..analysis.tickers import company_name
    except Exception:
        return lambda s: ""
    deadline = time.monotonic() + 3.0

    def lookup(sym: str) -> str:
        if time.monotonic() > deadline:
            return ""
        try:
            return company_name(sym) or ""
        except Exception:
            return ""
    return lookup


def _day_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp())


def _covered_earnings(own_key: str, start: date, end: date) -> set:
    """(ticker, ET day) pairs of earnings rows stored by other sources (the Nasdaq calendar), so one report is
    not listed twice with different ids. Never fatal: an unready DB means nothing is skipped."""
    try:
        from .. import db
        rows = db.query_events(_day_ts(start), _day_ts(end + timedelta(days=1)), kind="earnings")
    except Exception:  # noqa: BLE001
        return set()
    return {(str(r.get("ticker") or "").upper(), datetime.fromtimestamp(int(r["ts"]), ET).date().isoformat())
            for r in rows if r.get("source") != own_key and r.get("ticker") and r.get("ts")}


def _news_item(col: Collector, n: dict, cutoff: int, base_tickers: tuple[str, ...] = ()) -> dict | None:
    """Finnhub news object {category, datetime, headline, id, image, related, source, summary, url}."""
    title = clean_html(n.get("headline") or "", 500)
    pub = to_ts(n.get("datetime"))
    if not title or not pub or pub < cutoff:
        return None
    related = [s.strip().upper() for s in str(n.get("related") or "").split(",")]
    tickers = list(dict.fromkeys([*base_tickers, *(s for s in related if _SYM_RE.match(s))]))
    extra = {"provider": n.get("source") or "", "finnhub_id": n.get("id"), "category": n.get("category") or ""}
    publisher = normalize_publisher(n.get("source"))
    if publisher:   # "SeekingAlpha" -> "Seeking Alpha", "Yahoo" -> "Yahoo Finance": one outlet name across collectors
        extra["publisher"] = publisher
    if n.get("image"):
        extra["image"] = n["image"]
    summary = clean_html(n.get("summary") or "")
    if summary == title:
        summary = ""
    return col.item(title, n.get("url") or "", pub, summary, tickers, "", extra)


# ---------------------------------------------------------------- collectors

class FinnhubMarketNews(Collector):
    group = "broker"
    kind = "news"
    source_type = "api"
    requires = ("finnhub_key",)

    def __init__(self, key: str, name: str, category: str, interval: int, description: str) -> None:
        self.key, self.name, self.category, self.interval, self.description = key, name, category, interval, description
        self._max_id = 0  # minId: only news newer than the last run

    def fetch(self, cfg: dict) -> dict:
        data = _get("/news", cfg, {"category": self.category, "minId": self._max_id})
        if not isinstance(data, list):
            raise RuntimeError("Finnhub beklenmeyen yanıt biçimi (/news).")
        cutoff = now_ts() - MAX_AGE
        items, max_id = [], self._max_id
        for n in data:
            if not isinstance(n, dict):
                continue
            try:
                if isinstance(n.get("id"), int):
                    max_id = max(max_id, n["id"])
                it = _news_item(self, n, cutoff)
            except Exception:
                log.debug("finnhub news entry skipped", exc_info=True)
                continue
            if it:
                items.append(it)
        self._max_id = max_id
        items.sort(key=lambda x: x["published_at"], reverse=True)
        return {"news": items[:100]}


class FinnhubCompanyNews(Collector):
    key = "finnhub_company"
    name = "Finnhub Şirket"
    group = "broker"
    kind = "news"
    source_type = "api"
    interval = 900
    requires = ("finnhub_key",)
    description = "İzleme listesindeki her hisse için Finnhub şirket haberleri (son 2 gün)."

    BUDGET = 35.0  # seconds per run (worst case stays < 60 s); a long watchlist continues next run

    def __init__(self) -> None:
        self._cursor = 0

    def fetch(self, cfg: dict) -> dict:
        _token(cfg)
        syms = [s for s in dict.fromkeys(str(x).strip().upper() for x in (cfg.get("watchlist") or []))
                if _SYM_RE.match(s)]
        if not syms:
            return {"news": []}
        today = _today_et()
        params = {"from": (today - timedelta(days=2)).isoformat(), "to": today.isoformat()}
        cutoff = now_ts() - MAX_AGE
        quota = max(5, -(-100 // len(syms)))  # keep ~100 items per run in total
        start = self._cursor % len(syms)
        order = syms[start:] + syms[:start]
        deadline = time.monotonic() + self.BUDGET
        by_id: dict = {}
        done, errors = 0, []
        rate_limited = False
        for sym in order:
            if time.monotonic() > deadline:
                break
            try:
                data = _get("/company-news", cfg, {"symbol": sym, **params}, timeout=10)
            except FinnhubRateLimit as e:
                errors.append(str(e))
                done += 1
                rate_limited = True
                break
            except FinnhubAuthError:
                raise
            except RuntimeError as e:
                errors.append(f"{sym}: {e}")
                done += 1
                continue
            done += 1
            rows = sorted((n for n in data if isinstance(n, dict)) if isinstance(data, list) else [],
                          key=lambda n: to_ts(n.get("datetime")) or 0, reverse=True)
            kept = 0
            for n in rows:
                if kept >= quota:
                    break
                try:
                    it = _news_item(self, n, cutoff, (sym,))
                except Exception:
                    continue
                if not it:
                    continue
                kept += 1
                k = n.get("id") or it["url"] or it["title"]
                if k in by_id:  # same article for several watchlist symbols → merge tickers
                    by_id[k]["tickers"] = list(dict.fromkeys(by_id[k]["tickers"] + it["tickers"]))
                else:
                    by_id[k] = it
        self._cursor = (start + done) % len(syms)
        if not by_id and errors and (rate_limited or done == len(errors)):
            raise RuntimeError(errors[0])
        if errors:
            log.info("finnhub company news partial: %s", "; ".join(errors[:3]))
        res = {"news": sorted(by_id.values(), key=lambda x: x["published_at"], reverse=True)}
        if rate_limited:
            res["warnings"] = ["Kısmi hata — Finnhub istek sınırı aşıldı, kalan hisseler sonraki turda alınacak"]
        return res


class FinnhubEarnings(Collector):
    key = "finnhub_earnings"
    name = "Finnhub Bilanço Takvimi"
    group = "broker"
    kind = "calendar"
    source_type = "api"
    interval = 3600
    requires = ("finnhub_key",)
    description = "Önümüzdeki 7 günün bilanço takvimi: açılış öncesi/kapanış sonrası, EPS beklentisi."

    # Sort anchors (ET) for the day; exact times are not published, so events stay all_day.
    ANCHOR = {"bmo": (8, 0), "dmh": (12, 0), "amc": (16, 5), "unknown": (0, 0)}

    def fetch(self, cfg: dict) -> dict:
        today = _today_et()
        end = today + timedelta(days=7)
        data = _get("/calendar/earnings", cfg, {"from": today.isoformat(), "to": end.isoformat()})
        rows = data.get("earningsCalendar") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Finnhub beklenmeyen yanıt biçimi (/calendar/earnings).")
        name = _name_lookup()
        covered = _covered_earnings(self.key, today, end)  # (ticker, day) already listed by another source
        events = []
        for e in rows:
            try:
                sym = str(e.get("symbol") or "").strip().upper()
                if not _SYM_RE.match(sym):
                    continue
                d = date.fromisoformat(str(e.get("date"))[:10])
                if (sym, d.isoformat()) in covered:
                    continue
                hour = str(e.get("hour") or "").strip().lower()
                when = hour if hour in ("bmo", "amc", "dmh") else "unknown"
                h, m = self.ANCHOR[when]
                ts = int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())
                eps_f, eps_a = _num(e.get("epsEstimate")), _num(e.get("epsActual"))
                events.append({
                    "id": make_id("finnhub_earnings", sym, d.isoformat()),
                    "kind": "earnings", "title": f"{sym} bilanço açıklaması", "country": "US",
                    "ts": ts, "all_day": True, "impact": "",
                    "forecast": f"{eps_f:.2f}" if eps_f is not None else "", "previous": "",
                    "actual": f"{eps_a:.2f}" if eps_a is not None else "",
                    "ticker": sym, "source": "finnhub",
                    "extra": {"time": when, "date": d.isoformat(), "eps_forecast": eps_f, "eps_actual": eps_a,
                              "revenue_forecast": _num(e.get("revenueEstimate")),
                              "revenue_actual": _num(e.get("revenueActual")),
                              "quarter": e.get("quarter"), "year": e.get("year"),
                              "company": name(sym), "market_cap": None},
                })
            except Exception:
                continue
        events.sort(key=lambda x: (x["ts"], x["ticker"]))
        res = {"events": events[:3000]}
        if rows and events:
            # Finnhub answers the whole range, so a successful non-empty answer is the full list: a moved or
            # withdrawn report is removed (the window is ignored for an empty answer).
            res["event_windows"] = [{"start": _day_ts(today), "end": _day_ts(end + timedelta(days=1)),
                                     "kind": "earnings"}]
        return res


COLLECTORS = [
    FinnhubMarketNews("finnhub_news", "Finnhub Genel", "general", 120,
                      "Finnhub genel piyasa haberleri (farklı yayın kuruluşlarından derlenmiş)."),
    FinnhubMarketNews("finnhub_merger", "Finnhub Birleşme", "merger", 300,
                      "Finnhub birleşme ve satın alma (M&A) haberleri."),
    FinnhubCompanyNews(),
    FinnhubEarnings(),
]
