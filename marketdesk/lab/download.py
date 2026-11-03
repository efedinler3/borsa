"""Historical data download for the test lab (all free sources). Every step is resumable: finished pieces are
recorded in lab.db `progress`, so a stopped or crashed download continues where it left off.

Steps (full download):
  universe  Nasdaq stock screener -> every US stock above $300M market cap trading $5M+ a day (~2,600), SEC CIK map
  bars      Yahoo daily OHLCV, 5 years (split-adjusted by Yahoo), plus SPY / QQQ / ^VIX as market context
  earnings  Nasdaq earnings calendar day by day for 5 years (EPS, estimate, surprise, pre/after market)
  filings   SEC EDGAR submissions: 8-K (with item codes), S-1/S-3/424B4 (offerings), SC 13D, Form 4
  news      Finnhub company news for the ~130 most important names (the free plan serves about 12 months)
  news_all  (separate backfill, news_backfill()) the same 12 months of Finnhub headlines for every other universe stock

update_recent() is the daily incremental version (last weeks only), run by the scheduler after the close.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import queue
import re
import threading
import time
from datetime import date, datetime, timedelta

import requests

from ..config import load_config
from ..util import ET, http_json, now_ts
from . import store

log = logging.getLogger("marketdesk.lab")

YEARS = 10                  # daily history (2015 crash, 2018 Q4, 2020 crash, 2022 bear market ...)
BARS_RANGE, BARS_TASK, FILINGS_TASK = "10y", "bars10", "filings10"   # new task names: the 5-year marks do not count
MIN_CAP = 3e8               # every US stock above $300M market cap ...
MIN_DOLLAR_VOLUME = 5e6     # ... trading at least $5M a day (≈2,600 stocks)
N_NEWS = 100            # Finnhub headlines for the largest names ...
N_NEWS_ACTIVE = 30      # ... plus the most traded ones (+ watchlist)
SECTOR_ETF = {"Basic Materials": "XLB", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Energy": "XLE",
              "Finance": "XLF", "Health Care": "XLV", "Industrials": "XLI", "Real Estate": "XLRE", "Technology": "XLK",
              "Telecommunications": "XLC", "Utilities": "XLU"}
REFERENCE = ("SPY", "QQQ", "^VIX", "IWM") + tuple(SECTOR_ETF.values())
FILING_FORMS = {"8-K", "S-1", "S-3", "424B4", "SC 13D", "SC 13D/A", "4"}
NEWS_DAYS = 365
NEWS_WINDOW_DAYS = 14   # a window that hits the per-call cap is split in two
NEWS_CAP = 240          # Finnhub returns at most ~250 items per call: a full window is split in two ...
NEWS_MIN_CHUNK = 7      # ... down to 7 days, where the cap is accepted (mega caps: the newest ~240 of the week)
FINNHUB_PAUSE = 1.05    # free plan: 60 calls / minute
SEC_PAUSE = 0.12        # SEC: max 10 requests / second
NASDAQ_PAUSE = 0.6

STEP_LABELS = {"universe": "Hisse listesi", "bars": "Günlük fiyatlar (10 yıl)", "earnings": "Bilanço takvimi (10 yıl)",
               "filings": "SEC bildirimleri (10 yıl)", "news": "Finnhub haber başlıkları (≈12 ay)",
               "news_all": "Finnhub haber başlıkları, bütün hisseler (≈12 ay)", "update": "Günlük güncelleme"}

STOP = threading.Event()
STATUS: dict = {"state": "idle", "step": None, "done": 0, "total": 0, "message": "", "errors": [],
                "started_at": None, "finished_at": None, "steps": {}}
_lock = threading.Lock()


class Stopped(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _status(**kw) -> None:
    with _lock:
        STATUS.update(kw)


def _prog(step: str, done: int | None = None, inc: int = 0, total: int | None = None, message: str | None = None,
          state: str | None = None) -> None:
    """Progress of one step (steps may run at the same time: earnings, filings and news use different servers)."""
    with _lock:
        st = STATUS["steps"].setdefault(step, {"done": 0, "total": 0, "state": "running", "message": ""})
        if total is not None:
            st["total"] = total
        if done is not None:
            st["done"] = done
        st["done"] = min(st["done"] + inc, st["total"] or 10 ** 9)
        if message is not None:
            st["message"] = message
            STATUS["message"] = message
        if state is not None:
            st["state"] = state
        STATUS["step"] = step


def _err(msg: str) -> None:
    log.info("lab download: %s", msg)
    with _lock:
        STATUS["errors"] = (STATUS["errors"] + [f"{datetime.now().strftime('%H:%M')} {msg}"])[-30:]


def _check_stop() -> None:
    if STOP.is_set():
        raise Stopped()


def _num(s) -> float | None:
    if s is None:
        return None
    t = str(s).strip().replace("$", "").replace(",", "").replace("%", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if not t or t.upper() in ("N/A", "NA", "--", "-"):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _market():
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.collectors.market")


def _et_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, ET).date()


def _start_date(years: int = YEARS) -> date:
    t = date.today()
    return date(t.year - years, t.month, min(t.day, 28))


def universe_symbols(news_only: bool = False) -> list[str]:
    q = "SELECT symbol FROM universe" + (" WHERE news=1" if news_only else "") + " ORDER BY rank"
    return [r[0] for r in store.conn().execute(q)]


# ---------------------------------------------------------------- step: universe

def step_universe(cfg: dict) -> int:
    _prog("universe", done=0, total=1, message="Nasdaq hisse listesi alınıyor…")
    d = http_json("https://api.nasdaq.com/api/screener/stocks", params={"tableonly": "true", "limit": "10000", "download": "true"},
                  headers={"Accept": "application/json"}, timeout=40)
    rows = (d.get("data") or {}).get("rows") or []
    if not rows:
        raise RuntimeError("Nasdaq hisse listesi boş geldi")
    cand = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper().replace("/", ".")
        name = str(r.get("name") or "")
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", sym) or re.search(r"warrant|\bunits?\b|\bright|preferred|notes due", name, re.I):
            continue
        price, cap, vol = _num(r.get("lastsale")), _num(r.get("marketCap")), _num(r.get("volume"))
        if not price or price < 5 or not cap:
            continue
        cand.append({"symbol": sym, "name": name, "cap": cap, "dv": price * (vol or 0), "sector": r.get("sector") or "",
                     "industry": r.get("industry") or "", "ipo": _num(r.get("ipoyear"))})
    liquid = [x for x in cand if x["cap"] >= MIN_CAP and x["dv"] >= MIN_DOLLAR_VOLUME]
    by_cap = sorted(liquid, key=lambda x: -x["cap"])
    by_dv = sorted(liquid, key=lambda x: -x["dv"])
    news = {x["symbol"] for x in by_cap[:N_NEWS]} | {x["symbol"] for x in by_dv[:N_NEWS_ACTIVE]}
    by_sym = {x["symbol"]: x for x in cand}
    try:
        tick = importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.analysis.tickers")
        is_etf = getattr(tick, "is_etf", lambda s: False)
    except Exception:  # noqa: BLE001
        is_etf = lambda s: False  # noqa: E731
    chosen: dict[str, dict] = {}
    for x in by_cap + by_dv:
        chosen.setdefault(x["symbol"], x)
    for w in cfg.get("watchlist") or []:
        w = str(w).upper()
        if w in by_sym and not is_etf(w):
            chosen.setdefault(w, by_sym[w])
            news.add(w)
    ciks = {}
    ua = cfg.get("sec_user_agent") or ""
    if ua:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": ua}, timeout=30)
            r.raise_for_status()
            for v in r.json().values():
                ciks[str(v.get("ticker") or "").upper().replace("-", ".")] = int(v.get("cik_str"))
        except Exception as e:  # noqa: BLE001
            _err(f"SEC CIK listesi alınamadı: {e}")
    sig = hashlib.sha1(",".join(sorted(chosen)).encode()).hexdigest()
    if store.kv_get("universe_sig") != sig:
        # a different stock list: the earnings calendar is stored per day for the listed stocks only -> fetch it again
        with store.tx() as c:
            c.execute("DELETE FROM progress WHERE task='earnings'")
        store.kv_set("universe_sig", sig)
    with store.tx() as c:
        c.execute("DELETE FROM universe")
        for i, x in enumerate(sorted(chosen.values(), key=lambda x: -x["cap"])):
            c.execute("INSERT INTO universe(symbol, name, cik, sector, industry, market_cap, ipo_year, rank, news) VALUES (?,?,?,?,?,?,?,?,?)",
                      (x["symbol"], x["name"], ciks.get(x["symbol"]), x["sector"], x["industry"], x["cap"],
                       int(x["ipo"]) if x["ipo"] else None, i + 1, 1 if x["symbol"] in news else 0))
    store.mark("universe", "list", {"n": len(chosen), "news": len(news)})
    _prog("universe", done=1, message=f"{len(chosen)} hisse seçildi ({len(news)} tanesi için haber başlığı indirilecek)")
    return len(chosen)


# ---------------------------------------------------------------- step: bars

def _fetch_bars(market, sym: str, rng: str) -> list[tuple]:
    ysym = market._yahoo_symbol(sym) if hasattr(market, "_yahoo_symbol") and not sym.startswith("^") else sym
    d = market._yahoo_json(f"/v8/finance/chart/{requests.utils.quote(ysym)}",
                           {"range": rng, "interval": "1d", "includePrePost": "false"})
    res = ((d or {}).get("chart") or {}).get("result") or []
    if not res:
        return []
    res = res[0]
    q = (((res.get("indicators") or {}).get("quote")) or [{}])[0] or {}
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        try:
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]  # noqa: E741
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c) or c <= 0 or o <= 0:
            continue
        out.append((sym, _et_date(int(t)).isoformat(), float(o), float(h), float(l), float(c), float(v or 0)))
    return out


def step_bars(rng: str = BARS_RANGE, update: bool = False) -> int:
    market = _market()
    syms = list(REFERENCE) + universe_symbols()
    todo = syms if update else [s for s in syms if not store.done(BARS_TASK, s)]
    step = "bars"
    _prog(step, done=len(syms) - len(todo), total=len(syms), message="Yahoo'dan günlük fiyatlar indiriliyor…")
    n = 0
    for i in range(0, len(todo), 6):
        _check_stop()
        chunk = todo[i:i + 6]
        res = market._parallel(lambda s: _fetch_bars(market, s, rng), [(s,) for s in chunk], workers=3, timeout=60)
        for j, sym in enumerate(chunk):
            rows = res.get(j)
            if isinstance(rows, Exception) or rows is None:
                _err(f"{sym} fiyatları alınamadı: {rows}")
                continue
            with store.tx() as c:
                c.executemany("INSERT OR REPLACE INTO bars(symbol, d, o, h, l, c, v) VALUES (?,?,?,?,?,?,?)", rows)
            if not update:
                store.mark(BARS_TASK, sym, {"bars": len(rows)})
            n += len(rows)
        _prog(step, inc=len(chunk))
        time.sleep(0.4)
    return n


# ---------------------------------------------------------------- step: earnings

def _earnings_day(d: date, wanted: set[str]) -> list[tuple]:
    js = http_json("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()},
                   headers={"Accept": "application/json"}, timeout=25)
    rows = ((js or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = str(r.get("symbol") or "").upper().replace("/", ".")
        if sym not in wanted:
            continue
        eps, est = _num(r.get("eps")), _num(r.get("epsForecast"))
        sur = _num(r.get("surprise"))
        if sur is None and eps is not None and est not in (None, 0):
            sur = round((eps - est) / abs(est) * 100, 2)
        t = str(r.get("time") or "")
        tm = "bmo" if "pre-market" in t else "amc" if "after-hours" in t else "unknown"
        out.append((sym, d.isoformat(), tm, eps, est, sur))
    return out


def step_earnings(start: date | None = None, task: str = "earnings") -> int:
    start = start or _start_date()
    wanted = set(universe_symbols())
    days = []
    d = start
    today = date.today()
    while d <= today:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    have = store.done_keys(task)
    todo = [x for x in days if x.isoformat() not in have or x >= today - timedelta(days=3)]
    step = "earnings"
    _prog(step, done=len(days) - len(todo), total=len(days), message="Nasdaq bilanço takvimi indiriliyor…")
    n = 0
    market = _market()
    for i in range(0, len(todo), 2):
        _check_stop()
        chunk = todo[i:i + 2]
        res = market._parallel(lambda x: _earnings_day(x, wanted), [(x,) for x in chunk], workers=2, timeout=60)
        for j, x in enumerate(chunk):
            rows = res.get(j)
            if isinstance(rows, Exception) or rows is None:
                _err(f"{x} bilanço takvimi alınamadı: {rows}")
                continue
            with store.tx() as c:
                c.executemany("INSERT OR REPLACE INTO earnings(symbol, d, time, eps, eps_est, surprise) VALUES (?,?,?,?,?,?)", rows)
            if x < today:
                store.mark(task, x.isoformat(), {"n": len(rows)})
            n += len(rows)
        _prog(step, inc=len(chunk))
        time.sleep(NASDAQ_PAUSE)
    return n


# ---------------------------------------------------------------- step: filings

def _parse_filings(sym: str, block: dict, start_ts: int) -> list[tuple]:
    forms, accs = block.get("form") or [], block.get("accessionNumber") or []
    acc_dt, items = block.get("acceptanceDateTime") or [], block.get("items") or []
    out = []
    for i, f in enumerate(forms):
        if f not in FILING_FORMS:
            continue
        try:
            ts = int(datetime.fromisoformat(str(acc_dt[i]).replace("Z", "+00:00")).timestamp())
        except (ValueError, IndexError):
            continue
        if ts < start_ts:
            continue
        out.append((sym, accs[i], ts, f, (items[i] if i < len(items) else "") or ""))
    return out


def step_filings(cfg: dict, start: date | None = None, task: str = "filings", recent_only: bool = False) -> int:
    ua = cfg.get("sec_user_agent") or ""
    if not ua:
        _err("SEC e-postası Ayarlar'da girilmemiş: SEC bildirimleri atlandı")
        return 0
    start_ts = int(datetime.combine(start or _start_date(), datetime.min.time(), tzinfo=ET).timestamp())
    rows_u = [(r[0], r[1]) for r in store.conn().execute("SELECT symbol, cik FROM universe WHERE cik IS NOT NULL ORDER BY rank")]
    todo = [(s, cik) for s, cik in rows_u if recent_only or not store.done(task, s)]
    step = "filings"
    # (task FILINGS_TASK: symbols fetched for 5 years are fetched again with their older filing files)
    _prog(step, done=len(rows_u) - len(todo), total=len(rows_u), message="SEC EDGAR bildirim geçmişi indiriliyor…")
    sess = requests.Session()
    sess.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    n = 0
    for sym, cik in todo:
        _check_stop()
        try:
            r = sess.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", timeout=30)
            r.raise_for_status()
            j = r.json()
            rows = _parse_filings(sym, (j.get("filings") or {}).get("recent") or {}, start_ts)
            if not recent_only:
                for f in (j.get("filings") or {}).get("files") or []:
                    if str(f.get("filingTo") or "9999") < (start or _start_date()).isoformat():
                        continue
                    time.sleep(SEC_PAUSE)
                    r2 = sess.get(f"https://data.sec.gov/submissions/{f['name']}", timeout=30)
                    r2.raise_for_status()
                    rows += _parse_filings(sym, r2.json(), start_ts)
        except Exception as e:  # noqa: BLE001
            _err(f"{sym} SEC bildirimleri alınamadı: {e}")
            time.sleep(1)
            continue
        with store.tx() as c:
            c.executemany("INSERT OR REPLACE INTO filings(symbol, accession, ts, form, items) VALUES (?,?,?,?,?)", rows)
        if not recent_only:
            store.mark(task, sym, {"n": len(rows)})
        n += len(rows)
        _prog(step, inc=1)
        time.sleep(SEC_PAUSE)
    return n


# ---------------------------------------------------------------- step: news (Finnhub)

def _enrich(items: list[dict], cfg: dict) -> list[dict]:
    try:
        analysis = importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.analysis")
        return analysis.enrich(items, cfg)
    except Exception as e:  # noqa: BLE001 - store unclassified headlines rather than nothing
        _err(f"Haber sınıflandırma hatası: {e}")
        return items


def _store_news(sym: str, raw: list[dict], cfg: dict) -> int:
    items = []
    for x in raw:
        if not x.get("headline") or not x.get("datetime"):
            continue
        items.append({"_id": f"{x.get('id')}:{sym}", "title": str(x["headline"])[:400], "summary": str(x.get("summary") or "")[:600],
                      "url": x.get("url") or "", "source": "finnhub", "source_name": x.get("source") or "Finnhub",
                      "source_type": "news", "published_at": int(x["datetime"]), "tickers": [sym],
                      "extra": {"publisher": x.get("source") or ""}})
    if not items:
        return 0
    items = _enrich(items, dict(cfg, watchlist=[]))
    rows = []
    for it in items:
        ex = it.get("extra") if isinstance(it.get("extra"), dict) else {}
        subj = sym in (ex.get("subject_tickers") or []) or sym in (ex.get("title_tickers") or [])
        cats = [c for c in (it.get("categories") or []) if isinstance(c, str)]
        rows.append((it["_id"], sym, int(it["published_at"]), it.get("source_name"), it["title"],
                     json.dumps(cats), float(it.get("sentiment") or 0), float(it.get("importance") or 0),
                     1 if subj else 0))
    with store.tx() as c:
        c.executemany("INSERT OR REPLACE INTO news(id, symbol, ts, source, headline, categories, sentiment, importance, subject)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def step_news(cfg: dict, days: int = NEWS_DAYS, task: str = "news") -> int:
    key = cfg.get("finnhub_key") or ""
    if not key:
        _err("Finnhub anahtarı Ayarlar'da yok: haber başlıkları atlandı")
        return 0
    syms = universe_symbols(news_only=True)
    end = date.today()
    start = end - timedelta(days=days)
    # windows are anchored to a fixed Monday so their keys (and the resume marks) stay the same from day to day
    anchor = date(2020, 1, 6)
    d = anchor + timedelta(days=((start - anchor).days // NEWS_WINDOW_DAYS) * NEWS_WINDOW_DAYS)
    windows = []
    while d <= end:
        windows.append((d, min(end, d + timedelta(days=NEWS_WINDOW_DAYS - 1))))
        d += timedelta(days=NEWS_WINDOW_DAYS)
    have = store.done_keys(task)
    if task == "news":
        # symbols downloaded under the old (date-relative) windows: their past is in lab.db already
        latest = {r[0]: r[1] for r in store.conn().execute("SELECT symbol, MAX(ts) FROM news GROUP BY symbol")}
        bulk = []
        for s_ in syms:
            if s_ in latest and not any(k.startswith(s_ + ":") and k in have for k in (f"{s_}:{a}:{b}" for a, b in windows)):
                last_day = datetime.fromtimestamp(latest[s_], ET).date()
                bulk += [f"{s_}:{a}:{b}" for a, b in windows if b < last_day - timedelta(days=1)]
        if bulk:
            with store.tx() as c:
                c.executemany("INSERT OR REPLACE INTO progress(task, key, done_at, info) VALUES (?,?,?,?)",
                              [(task, k, now_ts(), None) for k in bulk])
            have = store.done_keys(task)
    plan = [(s, a, b) for s in syms for a, b in windows if f"{s}:{a}:{b}" not in have or b >= end - timedelta(days=1)]
    span = lambda w: (w[-1] - w[-2]).days + 1  # noqa: E731 - progress unit: symbol-days covered
    total = len(syms) * sum(span(w) for w in windows)
    step = "news"
    _prog(step, done=total - sum(span(w) for w in plan), total=total, message="Finnhub haber başlıkları indiriliyor (dakikada en fazla 57 istek)…")
    sess = requests.Session()
    sess.headers["X-Finnhub-Token"] = key
    stored = [0]
    # Classifying headlines costs ~10 ms each; a separate thread does it so the Finnhub calls keep their pace.
    work: queue.Queue = queue.Queue(maxsize=40)

    def classifier() -> None:
        while True:
            job = work.get()
            try:
                if job is None:
                    return
                w_sym, w_raw, w_key = job
                stored[0] += _store_news(w_sym, w_raw, cfg)
                if w_key:
                    store.mark(task, w_key, {"n": len(w_raw)})
            except Exception as e:  # noqa: BLE001
                _err(f"Haber kaydedilemedi: {e}")
            finally:
                work.task_done()

    worker = threading.Thread(target=classifier, name="lab-news-classify", daemon=True)
    worker.start()
    try:
        _fetch_news_windows(sess, plan, end, task, step, work)
    finally:
        work.put(None)
        worker.join()
    return stored[0]


class _AuthError(Exception):
    pass


def _finnhub_get(sess, sym: str, a: date, b: date, step: str) -> list | None:
    """One company-news call (paced); None on a network error (the window is retried on the next run)."""
    for _ in range(4):
        _check_stop()
        try:
            r = sess.get("https://finnhub.io/api/v1/company-news", params={"symbol": sym, "from": a.isoformat(), "to": b.isoformat()},
                         timeout=25)
            if r.status_code == 429:
                _prog(step, message="Finnhub istek sınırı: 30 sn bekleniyor…")
                time.sleep(30)
                continue
            if r.status_code in (401, 403):
                raise _AuthError()
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except _AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            _err(f"{sym} {a} haberleri alınamadı: {e}")
            time.sleep(3)
            return None
        finally:
            time.sleep(FINNHUB_PAUSE)
    return None


def _fetch_news_windows(sess, plan: list, end: date, task: str, step: str, work: queue.Queue) -> None:
    """Each planned 14-day window is fetched in chunks whose size adapts per symbol: a chunk that hits the per-call cap
    is halved (down to NEWS_MIN_CHUNK days, where the cap is accepted to keep the download to about an hour), a quiet
    chunk doubles the size again. Mega caps (NVDA: ~90 headlines a day) therefore keep only part of their news."""
    sizes: dict[str, int] = {}
    for sym, a, b in plan:
        size = sizes.get(sym, NEWS_WINDOW_DAYS)
        cur, collected, ok = a, [], True
        while cur <= b:
            stop = min(b, cur + timedelta(days=size - 1))
            try:
                raw = _finnhub_get(sess, sym, cur, stop, step)
            except _AuthError:
                _err("Finnhub anahtarı reddedildi (Ayarlar'dan kontrol edin)")
                return
            if raw is None:
                ok = False
                break
            span = (stop - cur).days + 1
            if len(raw) >= NEWS_CAP and span > NEWS_MIN_CHUNK:
                size = max(NEWS_MIN_CHUNK, span // 2)
                continue
            collected.extend(raw)
            if len(raw) < NEWS_CAP // 3 and size < NEWS_WINDOW_DAYS:
                size = min(NEWS_WINDOW_DAYS, size * 2)
            cur = stop + timedelta(days=1)
        sizes[sym] = size
        if not ok:
            continue
        key = f"{sym}:{a}:{b}" if task == "news" and b < end - timedelta(days=1) else None
        work.put((sym, collected, key))
        _prog(step, inc=(b - a).days + 1, message=f"Finnhub: {sym} {a:%d.%m.%Y}–{b:%d.%m.%Y} ({len(collected)} başlık)")


def step_news_all(cfg: dict, days: int = NEWS_DAYS) -> int:
    """12 months of headlines for every universe stock outside the ~130 news names. Most of them have a few hundred
    headlines a year, so each stock starts with one call for the whole range and only a range that hits the per-call
    cap is split in two (down to NEWS_MIN_CHUNK days): about 3-6 calls a stock instead of 26 fixed windows."""
    key = cfg.get("finnhub_key") or ""
    if not key:
        _err("Finnhub anahtarı Ayarlar'da yok: haber başlıkları atlandı")
        return 0
    task = step = "news_all"
    news_syms = set(universe_symbols(news_only=True))
    have = store.done_keys(task)
    syms = [s_ for s_ in universe_symbols() if s_ not in news_syms]
    todo = [s_ for s_ in syms if s_ not in have]
    end = date.today()
    start = end - timedelta(days=days)
    _prog(step, done=len(syms) - len(todo), total=len(syms), message="Bütün hisselerin haber başlıkları indiriliyor (dakikada en fazla 57 istek)…")
    sess = requests.Session()
    sess.headers["X-Finnhub-Token"] = key
    stored = [0]
    work: queue.Queue = queue.Queue(maxsize=40)

    def classifier() -> None:
        while True:
            job = work.get()
            try:
                if job is None:
                    return
                w_sym, w_raw = job
                stored[0] += _store_news(w_sym, w_raw, cfg)
                store.mark(task, w_sym, {"n": len(w_raw)})
            except Exception as e:  # noqa: BLE001
                _err(f"Haber kaydedilemedi: {e}")
            finally:
                work.task_done()

    worker = threading.Thread(target=classifier, name="lab-news-all-classify", daemon=True)
    worker.start()
    try:
        for sym in todo:
            ranges, collected, ok = [(start, end)], [], True
            while ranges:
                a, b = ranges.pop()
                try:
                    raw = _finnhub_get(sess, sym, a, b, step)
                except _AuthError:
                    _err("Finnhub anahtarı reddedildi (Ayarlar'dan kontrol edin)")
                    return stored[0]
                if raw is None:
                    ok = False
                    break
                if len(raw) >= NEWS_CAP and (b - a).days + 1 > NEWS_MIN_CHUNK:
                    mid = a + timedelta(days=((b - a).days + 1) // 2 - 1)
                    ranges += [(a, mid), (mid + timedelta(days=1), b)]
                    continue
                collected.extend(raw)
            if ok:
                work.put((sym, collected))
                _prog(step, inc=1, message=f"Finnhub: {sym} ({len(collected)} başlık)")
    finally:
        work.put(None)
        worker.join()
    return stored[0]


# ---------------------------------------------------------------- orchestration

def _step(name: str, cfg: dict, update: bool) -> None:
    if name == "universe":
        if update and store.conn().execute("SELECT COUNT(*) FROM universe").fetchone()[0]:
            return
        step_universe(cfg)
    elif name == "bars":
        step_bars("1mo" if update else BARS_RANGE, update=update)
    elif name == "earnings":
        step_earnings(date.today() - timedelta(days=21) if update else None)
    elif name == "filings":
        step_filings(cfg, recent_only=update, task="filings_upd" if update else FILINGS_TASK)
    elif name == "news":
        step_news(cfg, days=10 if update else NEWS_DAYS, task="news_upd" if update else "news")
    _prog(name, state="done")


def _run(update: bool) -> None:
    """universe -> bars, then earnings (Nasdaq), filings (SEC) and news (Finnhub) side by side: three different
    servers, each paced to its own limit, so the slowest one (Finnhub) sets the total time."""
    cfg = load_config()
    store.init()
    _status(state="running", started_at=now_ts(), finished_at=None, errors=[], steps={}, message="", update=update)
    STOP.clear()
    failures: list[BaseException] = []

    def guarded(name: str) -> None:
        try:
            _step(name, cfg, update)
        except Stopped:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("lab step %s failed", name)
            _prog(name, state="error", message=f"{STEP_LABELS.get(name, name)}: {e}")
            failures.append(e)

    try:
        for name in ("universe", "bars"):
            _check_stop()
            _step(name, cfg, update)
        threads = [threading.Thread(target=guarded, args=(n,), name=f"lab-{n}", daemon=True)
                   for n in ("earnings", "filings", "news")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _check_stop()
        if failures:
            raise failures[0]
        _status(state="done", finished_at=now_ts(), message="Günlük güncelleme tamamlandı" if update else "Veri indirme tamamlandı")
        store.kv_set("last_update" if update else "last_download", now_ts())
    except Stopped:
        _status(state="stopped", finished_at=now_ts(), message="Durduruldu; tekrar başlatınca kaldığı yerden devam eder")
    except Exception as e:  # noqa: BLE001
        log.exception("lab download failed")
        _status(state="error", finished_at=now_ts(), message=f"Hata: {e}")
    finally:
        store.kv_set("download_status", {k: STATUS[k] for k in ("state", "message", "started_at", "finished_at")})


def news_backfill() -> None:
    """One-off: 12 months of headlines for every universe stock (resumable; runs as the "download" job)."""
    cfg = load_config()
    store.init()
    _status(state="running", started_at=now_ts(), finished_at=None, errors=[], steps={}, message="", update=False)
    STOP.clear()
    try:
        n = step_news_all(cfg)
        _prog("news_all", state="done")
        _status(state="done", finished_at=now_ts(), message=f"Haber arşivi tamamlandı: {n:,} yeni başlık".replace(",", "."))
    except Stopped:
        _status(state="stopped", finished_at=now_ts(), message="Durduruldu; tekrar başlatınca kaldığı yerden devam eder")
    except Exception as e:  # noqa: BLE001
        log.exception("news backfill failed")
        _status(state="error", finished_at=now_ts(), message=f"Hata: {e}")
    finally:
        store.kv_set("download_status", {k: STATUS[k] for k in ("state", "message", "started_at", "finished_at")})


def full_download() -> None:
    _run(update=False)


def update_recent() -> None:
    _run(update=True)


def status() -> dict:
    with _lock:
        out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in STATUS.items()}
        out["steps"] = {k: dict(v) for k, v in STATUS["steps"].items()}
    out["step_label"] = STEP_LABELS.get(out.get("step") or "", "")
    out["labels"] = STEP_LABELS
    if out["state"] == "idle":
        try:
            out["last"] = store.kv_get("download_status")
        except Exception:  # noqa: BLE001 - lab.db busy or not created yet
            out["last"] = None
    return out
