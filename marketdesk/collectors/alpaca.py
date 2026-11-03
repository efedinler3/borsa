"""Alpaca Market Data API: Benzinga news, screener movers / most-actives, watchlist snapshots.

Docs: https://docs.alpaca.markets/reference/news-3, .../movers-1, .../mostactives-1, .../stocksnapshots-1
Auth: headers APCA-API-KEY-ID / APCA-API-SECRET-KEY (free paper-trading keys work).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from ..util import ET, clean_html, http_get, now_ts, to_ts
from .base import Collector
from .rss import normalize_publisher

log = logging.getLogger("marketdesk.collectors.alpaca")

BASE = "https://data.alpaca.markets"
MAX_AGE = 3 * 86400
_SYM_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")  # US equities only (drops BTCUSD etc.)


# ---------------------------------------------------------------- HTTP

class AlpacaAuthError(RuntimeError):
    pass


def _http_error_tr(e: requests.RequestException) -> str:
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        return f"Alpaca'ya bağlanılamadı: {type(e).__name__}"
    msg = ""
    try:
        body = resp.json()
        msg = str(body.get("message") or body.get("error") or "") if isinstance(body, dict) else ""
    except ValueError:
        pass
    if status == 401:
        return "Alpaca API anahtarı geçersiz (HTTP 401). Key ID ve Secret'ı kontrol edin."
    if status == 403:
        return "Alpaca erişim izni yok (HTTP 403): hesap/abonelik bu veriyi kapsamıyor olabilir."
    if status == 429:
        return "Alpaca istek sınırı aşıldı (HTTP 429), sonraki turda tekrar denenecek."
    return f"Alpaca HTTP {status}" + (f": {msg[:200]}" if msg else "")


def _get(path: str, cfg: dict, params: dict | None = None, timeout: float = 10, retries: int = 1):
    key_id = str(cfg.get("alpaca_key_id") or "").strip()
    secret = str(cfg.get("alpaca_secret") or "").strip()
    if not key_id or not secret:
        raise AlpacaAuthError("Alpaca API anahtarları girilmemiş (Kaynaklar & Ayarlar → API anahtarları).")
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    try:
        r = http_get(BASE + path, params=params, headers=headers, timeout=timeout, retries=retries)
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        raise (AlpacaAuthError if status == 401 else RuntimeError)(_http_error_tr(e)) from e
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"Alpaca geçersiz yanıt döndürdü ({path}).") from e


# ---------------------------------------------------------------- helpers

def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def _et_day(ts: int | None):
    return datetime.fromtimestamp(ts, ET).date() if ts else None


def _pct(price: float | None, prev: float | None) -> float | None:
    if price is None or not prev:
        return None
    return round((price - prev) / prev * 100, 2)


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


def _feed(cfg: dict) -> str:
    return str(cfg.get("alpaca_feed") or "iex").strip().lower()


def _snapshots(cfg: dict, symbols: list[str], feed: str, **kw) -> dict:
    """GET /v2/stocks/snapshots → {SYM: raw snapshot}. Falls back to IEX if the feed is not subscribed."""
    out: dict = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        try:
            data = _get("/v2/stocks/snapshots", cfg, {"symbols": ",".join(chunk), "feed": feed}, **kw)
        except RuntimeError as e:
            if feed != "iex" and "403" in str(e):
                data = _get("/v2/stocks/snapshots", cfg, {"symbols": ",".join(chunk), "feed": "iex"}, **kw)
            else:
                raise
        if isinstance(data, dict):
            # Response is keyed by symbol; older API versions nested it under "snapshots".
            if isinstance(data.get("snapshots"), dict):
                data = data["snapshots"]
            out.update({k.upper(): v for k, v in data.items() if isinstance(v, dict)})
    return out


def _snap_row(s: dict) -> dict:
    lt = s.get("latestTrade") or {}
    lq = s.get("latestQuote") or {}
    mb = s.get("minuteBar") or {}
    day = s.get("dailyBar") or {}
    prev = s.get("prevDailyBar") or {}
    price = _num(lt.get("p")) or _num(mb.get("c")) or _num(day.get("c"))
    trade_ts = to_ts(lt.get("t")) or to_ts(mb.get("t"))
    # Before the first trade of a new day dailyBar is still the previous session's bar.
    if _et_day(to_ts(day.get("t"))) and trade_ts and _et_day(to_ts(day.get("t"))) < _et_day(trade_ts):
        prev_close, day = _num(day.get("c")), {}
    else:
        prev_close = _num(prev.get("c"))
    change = round(price - prev_close, 4) if price is not None and prev_close else None
    vol = day.get("v")
    return {
        "price": price, "prev_close": prev_close, "change": change, "change_pct": _pct(price, prev_close),
        "volume": int(vol) if isinstance(vol, (int, float)) else None,
        "open": _num(day.get("o")), "high": _num(day.get("h")), "low": _num(day.get("l")),
        "vwap": _num(day.get("vw")), "bid": _num(lq.get("bp")), "ask": _num(lq.get("ap")),
        "trade_ts": trade_ts,
        "minute_bar": ({"t": to_ts(mb.get("t")), "o": _num(mb.get("o")), "h": _num(mb.get("h")),
                        "l": _num(mb.get("l")), "c": _num(mb.get("c")), "v": mb.get("v")} if mb else None),
    }


# ---------------------------------------------------------------- collectors

class AlpacaNews(Collector):
    key = "alpaca_news"
    name = "Alpaca · Benzinga"
    group = "broker"
    kind = "news"
    source_type = "api"
    interval = 60
    requires = ("alpaca_key_id", "alpaca_secret")
    description = "Alpaca haber API'si: Benzinga kaynaklı, hisse etiketli, gerçek zamanlı haberler."

    def __init__(self) -> None:
        self._newest = 0  # newest updated_at seen; decides whether a second page is needed

    def _map(self, n: dict, cutoff: int) -> dict | None:
        title = clean_html(n.get("headline") or "", 500)
        pub = to_ts(n.get("created_at")) or to_ts(n.get("updated_at"))
        if not title or not pub or pub < cutoff:
            return None
        tickers = [s for s in (str(x).strip().upper() for x in (n.get("symbols") or [])) if _SYM_RE.match(s)]
        extra = {"provider": n.get("source") or "", "alpaca_id": n.get("id")}
        publisher = normalize_publisher(n.get("source"))
        if publisher:   # one outlet name across collectors ("benzinga" -> "Benzinga"), for source counting
            extra["publisher"] = publisher
        upd = to_ts(n.get("updated_at"))
        if upd and upd - pub > 60:
            extra["updated_at"] = upd
        thumb = next((im.get("url") for im in (n.get("images") or [])
                      if isinstance(im, dict) and im.get("size") == "thumb" and im.get("url")), None)
        if thumb:
            extra["image"] = thumb
        return self.item(title, n.get("url") or "", pub, clean_html(n.get("summary") or ""), tickers,
                         clean_html(n.get("author") or "", 120), extra)

    def fetch(self, cfg: dict) -> dict:
        cutoff = now_ts() - MAX_AGE
        params: dict = {"limit": 50, "sort": "desc", "include_content": "false"}
        items: list[dict] = []
        seen: set = set()
        newest = self._newest
        for _page in range(2):  # ≤ 100 items; page 2 only if page 1 does not reach back to the last run
            data = _get("/v1beta1/news", cfg, params)
            raw = (data.get("news") if isinstance(data, dict) else None) or []
            oldest = None
            for n in raw:
                if not isinstance(n, dict) or n.get("id") in seen:
                    continue
                seen.add(n.get("id"))
                upd = to_ts(n.get("updated_at")) or to_ts(n.get("created_at"))
                if upd:
                    oldest = upd if oldest is None else min(oldest, upd)
                try:
                    it = self._map(n, cutoff)
                except Exception:
                    # A failed entry does not move the cursor (it is not counted as seen).
                    log.warning("alpaca news entry skipped (id=%s)", n.get("id"), exc_info=True)
                    continue
                if upd:
                    newest = max(newest, upd)
                if it:
                    items.append(it)
            token = data.get("next_page_token") if isinstance(data, dict) else None
            if not token or oldest is None or oldest <= self._newest or oldest < cutoff:
                break
            params["page_token"] = token
        self._newest = newest
        return {"news": items}


class AlpacaMovers(Collector):
    key = "alpaca_movers"
    name = "Alpaca Hareketliler"
    group = "broker"
    kind = "market"
    source_type = "api"
    interval = 120
    requires = ("alpaca_key_id", "alpaca_secret")
    description = "En çok yükselen/düşen ve en çok işlem gören hisseler (Alpaca screener, SIP verisi)."

    TOP = 20

    def fetch(self, cfg: dict) -> dict:
        errors: list[str] = []
        movers, actives = {}, {}
        try:
            movers = _get("/v1beta1/screener/stocks/movers", cfg, {"top": self.TOP}) or {}
        except AlpacaAuthError:
            raise
        except RuntimeError as e:
            errors.append(str(e))
        try:
            actives = _get("/v1beta1/screener/stocks/most-actives", cfg, {"by": "volume", "top": self.TOP}) or {}
        except RuntimeError as e:
            errors.append(str(e))
        if len(errors) == 2:
            raise RuntimeError(errors[0])

        name = _name_lookup()

        def mover(m: dict) -> dict | None:
            sym = str(m.get("symbol") or "").strip().upper()
            if not sym:
                return None
            return {"symbol": sym, "name": name(sym), "price": _num(m.get("price")),
                    "change": _num(m.get("change")), "change_pct": _num(m.get("percent_change")),
                    "volume": None}

        gainers = [r for r in map(mover, movers.get("gainers") or []) if r]
        losers = [r for r in map(mover, movers.get("losers") or []) if r]
        act_rows = []
        for a in actives.get("most_actives") or []:
            sym = str(a.get("symbol") or "").strip().upper()
            if sym:
                act_rows.append({"symbol": sym, "name": name(sym), "price": None, "change_pct": None,
                                 "volume": int(a["volume"]) if isinstance(a.get("volume"), (int, float)) else None,
                                 "trade_count": a.get("trade_count")})

        # Screener rows lack price (actives) and volume (movers): one snapshot call fills them.
        feed = _feed(cfg)
        syms = list(dict.fromkeys(r["symbol"] for r in gainers + losers + act_rows))
        if syms:
            try:
                snaps = _snapshots(cfg, syms, feed, timeout=8, retries=0)  # optional; keep run < 60 s
                for r in gainers + losers + act_rows:
                    s = snaps.get(r["symbol"])
                    if not s:
                        continue
                    row = _snap_row(s)
                    if r["price"] is None:
                        r["price"], r["change_pct"] = row["price"], row["change_pct"]
                    if r["volume"] is None and feed != "iex":  # IEX volume is a small slice; don't mislead
                        r["volume"] = row["volume"]
            except Exception as e:
                log.info("alpaca movers enrichment skipped: %s", e)

        updated = to_ts(movers.get("last_updated")) or to_ts(actives.get("last_updated"))
        return {"snapshots": {"movers_alpaca": {
            "ts": now_ts(), "source": "alpaca", "market_type": movers.get("market_type") or "stocks",
            "last_updated": updated, "gainers": gainers, "losers": losers, "actives": act_rows,
        }}}


class AlpacaSnapshots(Collector):
    key = "alpaca_snapshots"
    name = "Alpaca İzleme Listesi"
    group = "broker"
    kind = "market"
    source_type = "api"
    interval = 60
    requires = ("alpaca_key_id", "alpaca_secret")
    description = "İzleme listesindeki hisselerin anlık fiyat, değişim ve dakika barı (Alpaca, IEX)."

    def fetch(self, cfg: dict) -> dict:
        syms = [s for s in dict.fromkeys(str(x).strip().upper() for x in (cfg.get("watchlist") or []))
                if _SYM_RE.match(s)]
        feed = _feed(cfg)
        items: dict = {}
        if syms:
            for sym, s in _snapshots(cfg, syms, feed).items():
                try:
                    items[sym] = _snap_row(s)
                except Exception:
                    log.debug("alpaca snapshot %s skipped", sym, exc_info=True)
        return {"snapshots": {"alpaca_snapshots": {"ts": now_ts(), "feed": feed, "items": items}}}


COLLECTORS = [AlpacaNews(), AlpacaMovers(), AlpacaSnapshots()]
