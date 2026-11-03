"""Market data: quotes (Yahoo chart API), movers (Yahoo screeners + extended-hours gappers), CNN Fear & Greed."""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta

import requests

from .. import util
from .base import Collector
from .rss import friendly_error

log = logging.getLogger("marketdesk.market")

YH_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")

# (symbol, Turkish label, group) in display order.
BASE_SYMBOLS: list[tuple[str, str, str]] = [
    ("ES=F", "S&P 500 Vadeli", "vadeli"),
    ("NQ=F", "Nasdaq 100 Vadeli", "vadeli"),
    ("YM=F", "Dow Vadeli", "vadeli"),
    ("RTY=F", "Russell 2000 Vadeli", "vadeli"),
    ("^GSPC", "S&P 500", "endeks"),
    ("^IXIC", "Nasdaq Bileşik", "endeks"),
    ("^DJI", "Dow Jones", "endeks"),
    ("^RUT", "Russell 2000", "endeks"),
    ("^VIX", "VIX (Korku Endeksi)", "endeks"),
    ("DX-Y.NYB", "Dolar Endeksi", "doviz"),
    ("EURUSD=X", "Euro/Dolar", "doviz"),
    ("USDTRY=X", "Dolar/TL", "doviz"),
    ("^TNX", "ABD 10Y Faiz", "faiz"),
    ("CL=F", "Ham Petrol", "emtia"),
    ("GC=F", "Altın", "emtia"),
    ("SI=F", "Gümüş", "emtia"),
    ("BTC-USD", "Bitcoin", "kripto"),
    ("ETH-USD", "Ethereum", "kripto"),
]
_BASE_SET = {s for s, _, _ in BASE_SYMBOLS}

# Liquid names always checked for pre/after-market gaps (in addition to screener/trending/watchlist symbols).
LIQUID = ("AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC MU QCOM ARM SMCI PLTR SNOW "
          "CRWD PANW NET SHOP UBER ABNB COIN HOOD MSTR MARA RIOT SOFI PYPL XYZ BA DIS NKE WMT COST JPM BAC GS "
          "XOM CVX LLY UNH PFE MRNA NVO GME AMC RIVN LCID NIO BABA PDD TSM ASML DELL IBM CSCO RKLB IONQ").split()

QUOTE_FIELDS = ("symbol,shortName,longName,quoteType,marketState,fullExchangeName,regularMarketPrice,regularMarketTime,"
                "regularMarketChange,regularMarketChangePercent,regularMarketPreviousClose,regularMarketVolume,"
                "averageDailyVolume3Month,marketCap,preMarketPrice,preMarketChangePercent,preMarketTime,"
                "postMarketPrice,postMarketChangePercent,postMarketTime")


# ---------------------------------------------------------------- helpers

def _r(x, nd: int | None = None):
    """Round a price-like number with precision that fits its magnitude."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    if nd is None:
        ax = abs(x)
        nd = 2 if ax >= 100 else 4 if ax >= 1 else 6
    return round(x, nd)


def _int(x):
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _pct(a, b):
    if a is None or not b:
        return None
    return round((a - b) / b * 100, 2)


def _downsample(vals: list, n: int = 80) -> list:
    if len(vals) <= n:
        return vals
    step = (len(vals) - 1) / (n - 1)
    return [vals[round(i * step)] for i in range(n)]


def _status(e: Exception) -> int | None:
    return getattr(getattr(e, "response", None), "status_code", None)


# ---------------------------------------------------------------- Yahoo access (plain, then crumb on auth walls)

_crumb_lock = threading.Lock()
_crumb: dict = {"session": None, "crumb": None, "ts": 0}


def _get_crumb(force: bool = False) -> tuple[requests.Session, str]:
    """Cookie + crumb pair required by some Yahoo endpoints (v7 quote). Cached for 6 h."""
    with _crumb_lock:
        if not force and _crumb["crumb"] and time.time() - _crumb["ts"] < 6 * 3600:
            return _crumb["session"], _crumb["crumb"]
        s = requests.Session()
        s.headers.update({"User-Agent": util.BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
        try:
            s.get("https://fc.yahoo.com", timeout=10, allow_redirects=True)  # 404 is normal; it sets the A3 cookie
        except requests.RequestException:
            pass
        crumb = ""
        for host in YH_HOSTS:
            try:
                r = s.get(f"{host}/v1/test/getcrumb", timeout=10)
                if r.ok and r.text and "<" not in r.text and len(r.text.strip()) < 40:
                    crumb = r.text.strip()
                    break
            except requests.RequestException:
                continue
        if not crumb:
            raise RuntimeError("Yahoo crumb alınamadı")
        _crumb.update(session=s, crumb=crumb, ts=time.time())
        return s, crumb


def _crumb_json(path: str, params: dict | None = None, timeout: float = 12):
    last: Exception | None = None
    for attempt in range(2):
        s, crumb = _get_crumb(force=attempt > 0)
        for host in YH_HOSTS:
            try:
                r = s.get(host + path, params={**(params or {}), "crumb": crumb}, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                if _status(e) in (401, 403):
                    break  # stale crumb: refresh and retry
    assert last is not None
    raise last


_yh_backoff: dict = {"until": 0.0}  # set after an HTTP 429: Yahoo calls pause (same IP for every endpoint)
YH_BACKOFF = 300


def _yahoo_json(path: str, params: dict | None = None, timeout: float = 10):
    """query1 → query2 fallback; on 401/403 from both, retry through the crumb session. HTTP 429 is not retried
    on the other host: it backs off for YH_BACKOFF seconds instead."""
    if time.time() < _yh_backoff["until"]:
        raise RuntimeError("Yahoo istek sınırı: kısa süre bekleniyor")
    last: Exception | None = None
    auth_wall = False
    for host in YH_HOSTS:
        try:
            return util.http_json(host + path, params=params, timeout=timeout, retries=0)
        except (requests.RequestException, ValueError) as e:
            last = e
            st = _status(e)
            if st == 404:
                break  # unknown symbol: the other host says the same
            if st == 429:
                _yh_backoff["until"] = time.time() + YH_BACKOFF
                break
            auth_wall = auth_wall or st in (401, 403)
    if auth_wall:
        return _crumb_json(path, params, timeout)
    assert last is not None
    raise last


yahoo_json = _yahoo_json  # public alias for other modules


def _chart(symbol: str, range_: str = "1d", interval: str = "5m", prepost: bool = True) -> dict:
    d = _yahoo_json(f"/v8/finance/chart/{requests.utils.quote(symbol)}",
                    {"range": range_, "interval": interval, "includePrePost": "true" if prepost else "false"})
    res = ((d or {}).get("chart") or {}).get("result") or []
    if not res:
        raise ValueError(f"{symbol}: boş chart yanıtı")
    return res[0]


STALE_REGULAR = 30 * 60    # a future/index/FX "regular" period with no trade for this long is really closed


def _market_state(meta: dict, now: int) -> str:
    inst = meta.get("instrumentType")
    if inst == "CRYPTOCURRENCY":
        return "REGULAR"
    period = meta.get("currentTradingPeriod") or {}

    def inside(k: str) -> bool:
        p = period.get(k) or {}
        return p.get("start") is not None and p.get("end") is not None and p["start"] <= now < p["end"]

    for k, state in (("regular", "REGULAR"), ("pre", "PRE"), ("post", "POST")):
        if inside(k):
            # Yahoo reports a whole-day "regular" period for CME futures, DXY and FX even after the Friday 17:00 ET
            # close and during the daily 17-18 break; no trade for 30 min means the market is not trading.
            reg_t = meta.get("regularMarketTime") or 0
            if (state == "REGULAR" and inst in ("FUTURE", "INDEX", "CURRENCY") and reg_t
                    and now - reg_t > STALE_REGULAR):
                return "CLOSED"
            return state
    return "CLOSED"


def parse_chart_quote(res: dict, name: str | None = None, group: str = "izleme", now: int | None = None) -> dict:
    """Turn one Yahoo chart result into a `quotes` item (SPEC shape + a few extra fields)."""
    now = now or util.now_ts()
    meta = res.get("meta") or {}
    sym = meta.get("symbol") or ""
    ts_list = res.get("timestamp") or []
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    closes = q.get("close") or []
    pts = [(t, c) for t, c in zip(ts_list, closes) if c is not None and t is not None]
    reg = meta.get("regularMarketPrice")
    reg_t = meta.get("regularMarketTime") or 0
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    if reg is None and pts:
        reg = pts[-1][1]
    if reg is None:
        raise ValueError(f"{sym}: fiyat yok")

    # Extended-hours price: last bar newer than the last regular trade, outside regular hours.
    ext_price, ext_session = None, None
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    if meta.get("hasPrePostMarketData") and pts and pts[-1][0] > reg_t + 60:
        t_last = pts[-1][0]
        if regular.get("end") and t_last >= regular["end"]:
            ext_session = "post"
        elif regular.get("start") and t_last < regular["start"]:
            ext_session = "pre"
        if ext_session:
            ext_price = pts[-1][1]
            if ext_session == "pre" and regular.get("start") and reg_t < regular["start"]:
                prev = reg  # new session's premarket: the last regular price is yesterday's close
    price = ext_price if ext_price is not None else reg

    item = {
        "symbol": sym,
        "name": name or meta.get("shortName") or meta.get("longName") or sym,
        "group": group,
        "price": _r(price),
        "prev_close": _r(prev),
        "change": _r(price - prev) if prev else None,
        "change_pct": _pct(price, prev),
        "spark": [_r(c) for _, c in _downsample(pts, 80)],
        "market_state": _market_state(meta, now),
        # extras
        "regular_price": _r(reg),
        "ext_price": _r(ext_price),
        "ext_session": ext_session,
        "ext_change_pct": _pct(ext_price, reg) if ext_price is not None else None,
        "volume": _int(meta.get("regularMarketVolume")) or None,
        "day_high": _r(meta.get("regularMarketDayHigh")),
        "day_low": _r(meta.get("regularMarketDayLow")),
        "currency": meta.get("currency"),
        "instrument": meta.get("instrumentType"),
        "last_ts": int(max(reg_t, pts[-1][0] if pts else 0)) or None,
    }
    return item


def _parallel(fn, args_list: list, workers: int = 8, timeout: float = 40) -> dict:
    """Run fn(*args) in a thread pool; returns {index: result | Exception}. Never blocks past `timeout`."""
    out: dict = {}
    ex = ThreadPoolExecutor(max_workers=max(1, min(workers, len(args_list) or 1)))
    try:
        futs = {ex.submit(fn, *a): i for i, a in enumerate(args_list)}
        done, pending = wait(futs, timeout=timeout)
        for f in done:
            i = futs[f]
            try:
                out[i] = f.result()
            except Exception as e:  # noqa: BLE001 — one bad symbol must not break the fetch
                out[i] = e
        for f in pending:
            out[futs[f]] = TimeoutError("zaman aşımı")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


def _yahoo_symbol(sym: str) -> str:
    """Class shares: BRK.B → BRK-B (Yahoo spelling)."""
    if re.fullmatch(r"[A-Z]{1,5}\.[A-Z]{1,2}", sym):
        return sym.replace(".", "-")
    return sym


def _app_symbol(sym: str) -> str:
    """Yahoo class-share spelling back to the app's: BRK-B → BRK.B."""
    if re.fullmatch(r"[A-Z]{1,5}-[A-Z]{1,2}", sym or ""):
        return sym.replace("-", ".")
    return sym


def _watchlist(cfg: dict) -> list[str]:
    wl = cfg.get("watchlist") or []
    if isinstance(wl, str):
        wl = wl.replace(";", ",").split(",")
    out = []
    for s in wl:
        s = str(s).strip().upper().lstrip("$")
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- quotes

class YahooQuotes(Collector):
    key = "yahoo_quotes"
    name = "Yahoo Fiyatlar"
    group = "data"
    kind = "market"
    source_type = "api"
    interval = 60
    description = ("Vadeli endeksler, endeksler, VIX, dolar endeksi, 10Y faiz, emtia, kripto ve izleme listesi "
                   "fiyatları + gün içi mini grafik (Yahoo chart API; piyasa öncesi/sonrası dahil, bazıları gecikmeli).")

    STALE_KEEP = 15 * 60
    CLOSED_REFRESH = 300   # instruments whose market is closed are re-fetched at most every 5 min

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, dict]] = {}

    def fetch(self, cfg: dict) -> dict:
        now = util.now_ts()
        targets = list(BASE_SYMBOLS) + [(s, None, "izleme") for s in _watchlist(cfg)[:60] if s not in _BASE_SET]
        todo, reuse = [], {}
        for i, t in enumerate(targets):
            cached = self._last.get(t[0])
            if cached and cached[1].get("market_state") == "CLOSED" and now - cached[0] < self.CLOSED_REFRESH:
                reuse[i] = cached[1]
            else:
                todo.append(i)
        res = _parallel(lambda sym, nm, grp: parse_chart_quote(_chart(_yahoo_symbol(sym)), nm, grp, now),
                        [targets[i] for i in todo], workers=8, timeout=40)
        results = {**reuse, **{todo[j]: r for j, r in res.items()}}
        items, failed, fresh, first_err = [], [], len(reuse), None
        for i, (sym, nm, grp) in enumerate(targets):
            res = results.get(i)
            if i in reuse:
                items.append(res)
                continue
            if isinstance(res, dict):
                res["symbol"] = sym  # keep the requested spelling (Yahoo may echo a variant)
                self._last[sym] = (now, res)
                items.append(res)
                fresh += 1
                continue
            failed.append(sym)
            if first_err is None and isinstance(res, Exception):
                first_err = res
            log.debug("quote %s failed: %r", sym, res)
            cached = self._last.get(sym)
            if cached and now - cached[0] < self.STALE_KEEP:
                items.append({**cached[1], "stale": True})
        if not fresh:
            raise RuntimeError(f"Yahoo fiyat verisi alınamadı ({len(failed)} sembol başarısız)"
                               + (f": {friendly_error(first_err)}" if first_err else ""))
        return {"snapshots": {"quotes": {"ts": now, "session": util.market_session(now), "items": items,
                                         "failed": failed}}}


# ---------------------------------------------------------------- movers

SCREENERS = (("gainers", "day_gainers"), ("losers", "day_losers"), ("actives", "most_actives"),
             ("small_caps", "small_cap_gainers"))


def _screener(scr_id: str, count: int = 25) -> list[dict]:
    d = _yahoo_json("/v1/finance/screener/predefined/saved",
                    {"scrIds": scr_id, "count": count, "lang": "en-US", "region": "US"}, timeout=12)
    res = ((d or {}).get("finance") or {}).get("result") or []
    return (res[0].get("quotes") or []) if res else []


def parse_mover(q: dict) -> dict | None:
    """Yahoo quote object (screener / v7 quote) → movers item."""
    sym = q.get("symbol")
    price = q.get("regularMarketPrice")
    if not sym or price is None:
        return None
    reg_t = q.get("regularMarketTime") or 0
    vol, avg = _int(q.get("regularMarketVolume")), _int(q.get("averageDailyVolume3Month"))
    d = {
        "symbol": sym,
        "name": util.clean_html(q.get("shortName") or q.get("longName") or q.get("displayName") or sym, 120),
        "price": _r(price),
        "change": _r(q.get("regularMarketChange")),
        "change_pct": _r(q.get("regularMarketChangePercent"), 2),
        "volume": vol,
        "avg_volume": avg,
        "market_cap": _int(q.get("marketCap")),
        "rel_volume": round(vol / avg, 2) if vol and avg else None,
        "prev_close": _r(q.get("regularMarketPreviousClose")),
        "exchange": q.get("fullExchangeName") or q.get("exchange"),
        "market_state": q.get("marketState"),
    }
    for k in ("pre", "post"):
        t, pct, px = q.get(f"{k}MarketTime"), q.get(f"{k}MarketChangePercent"), q.get(f"{k}MarketPrice")
        if t and t > reg_t and pct is not None and px:
            d[f"{k}_change_pct"] = _r(pct, 2)
            d[f"{k}_price"] = _r(px)
    return d


def _today_et_ts(hour: int, minute: int = 0) -> int:
    n = datetime.now(util.ET)
    return int(n.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


def v7_quotes(symbols: list[str]) -> list[dict]:
    """Raw Yahoo v7 quote objects for `symbols` (equities; 50 per request, crumb session)."""
    syms = list(dict.fromkeys(_yahoo_symbol(s) for s in symbols if s and "^" not in s and "=" not in s))
    chunks = [syms[i:i + 50] for i in range(0, len(syms), 50)]
    res = _parallel(lambda c: _crumb_json("/v7/finance/quote", {"symbols": ",".join(c), "fields": QUOTE_FIELDS}),
                    [(c,) for c in chunks], workers=4, timeout=20)
    quotes, errors = [], []
    for r in res.values():
        if isinstance(r, Exception):
            errors.append(r)
            continue
        quotes += ((r or {}).get("quoteResponse") or {}).get("result") or []
    if chunks and len(errors) == len(chunks):
        raise RuntimeError(f"Yahoo v7 fiyat sorgusu alınamadı: {friendly_error(errors[0])}") from errors[0]
    return quotes


def ext_hours_movers(symbols: list[str], session: str, limit: int = 30,
                     quotes: list[dict] | None = None) -> tuple[str | None, list[dict]]:
    """Pre-market (04:00-09:30 ET) or after-hours gappers among `symbols` (or among already fetched v7 `quotes`).
    Returns (kind "pre"|"post"|None, items sorted by absolute extended-hours move)."""
    if session == "premarket":
        kind, since = "pre", _today_et_ts(4)
    elif session in ("afterhours", "closed"):
        kind, since = "post", util.last_close_ts()
    else:
        return None, []
    if quotes is None:
        quotes = v7_quotes(symbols)
    out = []
    for q in quotes:
        try:
            if q.get("quoteType") not in (None, "EQUITY"):
                continue
            t, pct, px = q.get(f"{kind}MarketTime"), q.get(f"{kind}MarketChangePercent"), q.get(f"{kind}MarketPrice")
            if not t or t < since or pct is None or not px or px < 1:
                continue
            if (q.get("marketCap") or 0) < 50e6 and (q.get("averageDailyVolume3Month") or 0) < 200_000:
                continue
            m = parse_mover(q)
            if not m:
                continue
            # price/change/change_pct now describe the extended-hours move vs the last regular close.
            m["regular_price"], m["regular_change_pct"] = m["price"], m["change_pct"]
            m["prev_close"] = m["price"]
            m["change"] = _r(px - m["price"]) if m["price"] else None
            m["price"], m["change_pct"] = _r(px), _r(pct, 2)
            m[f"{kind}_change_pct"], m[f"{kind}_price"] = _r(pct, 2), _r(px)
            out.append(m)
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda m: abs(m["change_pct"] or 0), reverse=True)
    return kind, out[:limit]


def trending_symbols(count: int = 30) -> list[str]:
    d = _yahoo_json("/v1/finance/trending/US", {"count": count}, timeout=10)
    res = ((d or {}).get("finance") or {}).get("result") or []
    return [q.get("symbol") for q in (res[0].get("quotes") or [])] if res else []


class YahooMovers(Collector):
    key = "yahoo_movers"
    name = "Yahoo Hareketliler"
    group = "data"
    kind = "market"
    source_type = "api"
    interval = 120
    description = ("En çok yükselenler / düşenler / en aktifler / küçük şirket yükselenleri (Yahoo screener) ve "
                   "piyasa öncesi / sonrası en çok hareket edenler.")

    EXTRA_MAX = 150          # cap on opportunity / earnings symbols added to the v7 quote pass
    EARN_MIN_CAP = 2e9

    def __init__(self) -> None:
        self._cache: tuple[int, dict] | None = None
        self._pre_cache: tuple[int, list] | None = None

    def _extra_symbols(self) -> list[str]:
        """Tickers of the last opportunities snapshot + today's and the next trading day's earnings reporters
        (market cap >= $2B): they need a price even when no screener lists them."""
        out: list[str] = []
        try:
            from .. import db
            opp = db.get_snapshot_data("opportunities") or {}
            for it in (opp.get("items") or []) if isinstance(opp, dict) else []:
                if isinstance(it, dict) and it.get("ticker"):
                    out.append(str(it["ticker"]).upper())
            today = datetime.now(util.ET).date()
            nxt = today + timedelta(days=1)
            while not util.is_trading_day(nxt):
                nxt += timedelta(days=1)
            start = int(datetime(today.year, today.month, today.day, tzinfo=util.ET).timestamp())
            end = int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=util.ET).timestamp()) + 86400
            earn = []
            for e in db.query_events(start, end, kind="earnings"):
                cap = (e.get("extra") or {}).get("market_cap") or 0
                if e.get("ticker") and isinstance(cap, (int, float)) and cap >= self.EARN_MIN_CAP:
                    earn.append((cap, str(e["ticker"]).upper()))
            out += [s for _, s in sorted(earn, reverse=True)]
        except Exception as e:  # noqa: BLE001 — DB not ready (tests, first start): screener symbols only
            log.debug("extra quote symbols unavailable: %s", e)
        syms = [s for s in dict.fromkeys(out) if re.fullmatch(r"[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?", s)]
        return syms[:self.EXTRA_MAX]

    def fetch(self, cfg: dict) -> dict:
        now = util.now_ts()
        session = util.market_session(now)
        # Nothing moves overnight/weekends: reuse the last snapshot for 15 min to stay polite.
        if session == "closed" and self._cache and now - self._cache[0] < 900:
            return {"snapshots": {"movers": self._cache[1]}}

        jobs = [(_screener, scr, 25) for _, scr in SCREENERS] + [(trending_symbols, 30)]
        res = _parallel(lambda f, *a: f(*a), jobs, workers=5, timeout=25)
        snap: dict = {"ts": now, "source": "yahoo", "session": session}
        universe: list[str] = []
        ok = 0
        for i, (key, _scr) in enumerate(SCREENERS):
            r = res.get(i)
            if isinstance(r, Exception) or r is None:
                log.warning("yahoo screener %s failed: %r", key, r)
                snap[key] = []
                continue
            ok += 1
            items = [m for m in (parse_mover(q) for q in r if isinstance(q, dict)) if m]
            snap[key] = items
            universe += [m["symbol"] for m in items]
        if not ok:
            first = next((r for r in res.values() if isinstance(r, Exception)), None)
            raise RuntimeError("Yahoo screener verisi alınamadı" + (f": {friendly_error(first)}" if first else ""))
        trending = res.get(len(SCREENERS))
        if isinstance(trending, list):
            universe += [s for s in trending if isinstance(s, str)]

        # One v7 quote pass: extended-hours gappers (pre-market / after-hours) and `quotes_extra`, the price of
        # every symbol the app cares about but no screener lists (opportunity tickers, big earnings reporters).
        snap["premarket"], snap["postmarket"] = [], []
        try:
            universe += _watchlist(cfg) + LIQUID + self._extra_symbols()
            quotes = v7_quotes(universe)
            extra: dict[str, dict] = {}
            for q in quotes:
                try:
                    if q.get("quoteType") not in (None, "EQUITY", "ETF"):
                        continue
                    m = parse_mover(q)
                except (TypeError, ValueError):
                    continue
                if m:
                    m["symbol"] = _app_symbol(m["symbol"])
                    extra[m["symbol"]] = m
            snap["quotes_extra"] = extra
            kind, ext = ext_hours_movers(universe, session, quotes=quotes)
            if kind == "pre":
                snap["premarket"] = ext
                self._pre_cache = (now, ext)
            elif kind == "post":
                snap["postmarket"] = ext
            if kind:
                snap["ext_ts"] = now
        except Exception as e:  # noqa: BLE001 — gappers are optional
            log.warning("extended-hours movers failed: %r", e)
            snap["ext_error"] = friendly_error(e)[:200]
        # During the regular session keep showing this morning's gappers.
        if session == "open" and self._pre_cache and self._pre_cache[0] >= _today_et_ts(4):
            snap["premarket"] = self._pre_cache[1]
            snap["ext_ts"] = self._pre_cache[0]
        self._cache = (now, snap)
        return {"snapshots": {"movers": snap}}


# ---------------------------------------------------------------- Fear & Greed

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": "https://edition.cnn.com",
               "Referer": "https://edition.cnn.com/"}
RATING_TR = {"extreme fear": "Aşırı Korku", "fear": "Korku", "neutral": "Nötr",
             "greed": "Açgözlülük", "extreme greed": "Aşırı Açgözlülük"}
CNN_INDICATORS = (
    ("market_momentum_sp500", "Piyasa momentumu (S&P 500 / 125 gün ort.)"),
    ("stock_price_strength", "Hisse fiyat gücü (52 hafta zirve/dip)"),
    ("stock_price_breadth", "Piyasa genişliği (McClellan hacim)"),
    ("put_call_options", "Put/Call opsiyon oranı"),
    ("market_volatility_vix", "Volatilite (VIX / 50 gün ort.)"),
    ("safe_haven_demand", "Güvenli liman talebi (hisse vs tahvil)"),
    ("junk_bond_demand", "Yüksek getirili tahvil talebi"),
)


def _rating(score: float | None) -> str:
    if score is None:
        return ""
    if score < 25:
        return "extreme fear"
    if score < 45:
        return "fear"
    if score <= 55:
        return "neutral"
    if score < 75:
        return "greed"
    return "extreme greed"


def _f1(x):
    try:
        return round(float(x), 1) if x is not None else None
    except (TypeError, ValueError):
        return None


def fetch_cnn_fear_greed() -> dict:
    d = util.http_json(CNN_URL, headers=CNN_HEADERS, timeout=15, retries=0)
    fg = d.get("fear_and_greed") or {}
    score = _f1(fg.get("score"))
    if score is None:
        raise ValueError("CNN yanıtında skor yok")
    rating = str(fg.get("rating") or _rating(score)).lower()
    indicators = []
    for key, label in CNN_INDICATORS:
        ind = d.get(key) or {}
        if ind.get("score") is None:
            continue
        r = str(ind.get("rating") or "").lower()
        indicators.append({"key": key, "name": label, "score": _f1(ind.get("score")), "rating": r,
                           "rating_tr": RATING_TR.get(r, r)})
    hist = []
    for p in ((d.get("fear_and_greed_historical") or {}).get("data") or [])[-60:]:
        try:
            hist.append({"ts": util.to_ts(p["x"]), "score": _f1(p["y"])})
        except (KeyError, TypeError):
            continue
    return {
        "ts": util.now_ts(), "source": "cnn", "is_proxy": False, "label": "CNN Korku & Açgözlülük Endeksi",
        "score": score, "rating": rating, "rating_tr": RATING_TR.get(rating, rating),
        "previous_close": _f1(fg.get("previous_close")), "week_ago": _f1(fg.get("previous_1_week")),
        "month_ago": _f1(fg.get("previous_1_month")), "year_ago": _f1(fg.get("previous_1_year")),
        "as_of": util.to_ts(fg.get("timestamp")), "indicators": indicators, "history": hist,
    }


def _daily_closes(symbol: str, range_: str = "1y") -> dict:
    res = _chart(symbol, range_, "1d", prepost=False)
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    out = {}
    for t, c in zip(res.get("timestamp") or [], q.get("close") or []):
        if t and c is not None:
            out[datetime.fromtimestamp(t, util.ET).date()] = float(c)
    return out


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def sentiment_proxy() -> dict:
    """Transparent fallback when CNN is unreachable ("piyasa_duygu"): average of
    (a) S&P 500 vs its 125-day average (±10% → 0/100) and
    (b) VIX: level (10 → 100, 35 → 0) and VIX vs its 50-day average (+25% → 0, -25% → 100)."""
    res = _parallel(_daily_closes, [("^GSPC",), ("^VIX",)], workers=2, timeout=25)
    for r in res.values():
        if not isinstance(r, dict):
            raise RuntimeError("vekil için Yahoo günlük verisi alınamadı: "
                               + (friendly_error(r) if isinstance(r, BaseException) else "yanıt yok"))
    spx, vix = res[0], res[1]
    days = sorted(d for d in spx if d in vix)
    if len(days) < 150:
        raise ValueError("vekil için yeterli günlük veri yok")
    spx_s = [spx[d] for d in days]
    vix_s = [vix[d] for d in days]
    series = []
    for i in range(125, len(days)):
        ma125 = sum(spx_s[i - 124:i + 1]) / 125
        ma50v = sum(vix_s[i - 49:i + 1]) / 50
        mom = _clamp(50 + (spx_s[i] / ma125 - 1) * 500)
        lvl = _clamp(100 - (vix_s[i] - 10) * 4)
        rel = _clamp(50 - (vix_s[i] / ma50v - 1) * 200)
        series.append((days[i], round((mom + (lvl + rel) / 2) / 2, 1), mom, lvl, rel, spx_s[i], ma125, vix_s[i], ma50v))
    last = series[-1]

    def back(n: int):
        return series[-1 - n][1] if len(series) > n else None

    score = last[1]
    rating = _rating(score)
    return {
        "ts": util.now_ts(), "source": "proxy", "is_proxy": True, "kind": "piyasa_duygu",
        "label": "Piyasa Duygu Vekili (VIX + S&P 500 / 125 gün ort.) — CNN verisi değil",
        "score": score, "rating": rating, "rating_tr": RATING_TR.get(rating, rating),
        "previous_close": back(1), "week_ago": back(5), "month_ago": back(21), "year_ago": None,
        "as_of": int(datetime(last[0].year, last[0].month, last[0].day, 16, tzinfo=util.ET).timestamp()),
        "indicators": [
            {"key": "proxy_momentum", "name": f"S&P 500 {last[5]:.0f} / 125 gün ort. {last[6]:.0f}",
             "score": round(last[2], 1), "rating": _rating(last[2]), "rating_tr": RATING_TR.get(_rating(last[2]), "")},
            {"key": "proxy_vix_level", "name": f"VIX seviyesi {last[7]:.2f}",
             "score": round(last[3], 1), "rating": _rating(last[3]), "rating_tr": RATING_TR.get(_rating(last[3]), "")},
            {"key": "proxy_vix_rel", "name": f"VIX {last[7]:.2f} / 50 gün ort. {last[8]:.2f}",
             "score": round(last[4], 1), "rating": _rating(last[4]), "rating_tr": RATING_TR.get(_rating(last[4]), "")},
        ],
        "history": [{"ts": int(datetime(d.year, d.month, d.day, 16, tzinfo=util.ET).timestamp()), "score": s}
                    for d, s, *_ in series[-60:]],
    }


class FearGreed(Collector):
    key = "cnn_fear_greed"
    name = "CNN Korku & Açgözlülük"
    group = "data"
    kind = "market"
    source_type = "api"
    interval = 900
    description = ("CNN Fear & Greed endeksi (0-100) ve 7 alt göstergesi. CNN erişilemezse VIX ve S&P 500 / "
                   "125 gün ortalamasından hesaplanan, açıkça 'vekil' olarak işaretli bir duygu skoru yazılır.")

    KEEP_CNN = 3 * 3600

    def __init__(self) -> None:
        self._cnn_ok = 0

    def _last_cnn_ts(self) -> int:
        if self._cnn_ok:
            return self._cnn_ok
        try:
            from .. import db
            s = db.get_snapshot("fear_greed")
            if s and isinstance(s[0], dict) and s[0].get("source") == "cnn":
                return int(s[1])
        except Exception:  # noqa: BLE001 — DB may not be initialised in tests
            pass
        return 0

    def fetch(self, cfg: dict) -> dict:
        try:
            snap = fetch_cnn_fear_greed()
            self._cnn_ok = snap["ts"]
            return {"snapshots": {"fear_greed": snap}}
        except Exception as e:  # noqa: BLE001
            cnn_err = e
        if util.now_ts() - self._last_cnn_ts() < self.KEEP_CNN:
            raise RuntimeError(f"CNN alınamadı ({friendly_error(cnn_err)}); son CNN verisi korunuyor") from cnn_err
        try:
            snap = sentiment_proxy()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"CNN alınamadı ({friendly_error(cnn_err)}); vekil skor da hesaplanamadı "
                               f"({friendly_error(e)})") from e
        snap["cnn_error"] = friendly_error(cnn_err)[:200]
        return {"snapshots": {"fear_greed": snap}}


COLLECTORS = [YahooQuotes(), YahooMovers(), FearGreed()]
