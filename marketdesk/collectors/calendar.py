"""Calendars: macro events (ForexFactory weekly JSON, actuals from Nasdaq), earnings and IPOs (Nasdaq API)."""
from __future__ import annotations

import html
import logging
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from ..util import DATA_DIR, ET, http_get, is_trading_day, jdump, jload, make_id, now_ts, to_ts
from .base import Collector
from .rss import friendly_error

log = logging.getLogger("marketdesk.collectors.calendar")

FF_URLS = (("thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"),
           ("nextweek", "https://nfs.faireconomy.media/ff_calendar_nextweek.json"))   # nextweek is often 404
FF_CACHE_PATH = DATA_DIR / "ff_calendar_cache.json"
FF_MIN_GAP = 1800          # ForexFactory rate-limits hard: download at most every 30 min (also across restarts)
FF_NET_RETRY = 120         # after a network failure (no answer at all) try again this soon

NASDAQ_API = "https://api.nasdaq.com/api/"
NASDAQ_HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
                  "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}

# ForexFactory currency -> event country code
FF_COUNTRY = {"USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "CNY": "CN", "CAD": "CA", "AUD": "AU",
              "NZD": "NZ", "CHF": "CH", "ALL": "ALL"}
FF_MAJOR = {"EUR", "GBP", "JPY", "CNY", "ALL"}   # non-USD events are kept only when high impact
FF_IMPACT = {"high": "high", "medium": "medium", "low": "low", "non-economic": "low", "holiday": ""}

# ---------------------------------------------------------------- Turkish titles

_TITLE_TR = {
    "CPI": "TÜFE (Enflasyon)",
    "Core CPI": "Çekirdek TÜFE",
    "CPI Flash Estimate": "Öncü TÜFE",
    "Core CPI Flash Estimate": "Öncü Çekirdek TÜFE",
    "PPI": "ÜFE (Üretici Fiyatları)",
    "Core PPI": "Çekirdek ÜFE",
    "Non-Farm Employment Change": "Tarım Dışı İstihdam",
    "ADP Non-Farm Employment Change": "ADP Özel Sektör İstihdamı",
    "ADP Weekly Employment Change": "ADP Haftalık İstihdam Değişimi",
    "Unemployment Rate": "İşsizlik Oranı",
    "Average Hourly Earnings": "Ortalama Saatlik Kazançlar",
    "Unemployment Claims": "Haftalık İşsizlik Başvuruları",
    "Challenger Job Cuts": "Challenger İşten Çıkarmalar",
    "Employment Cost Index": "İstihdam Maliyet Endeksi",
    "JOLTS Job Openings": "JOLTS Açık İş Pozisyonları",
    "FOMC Statement": "FOMC Faiz Kararı (Bildiri)",
    "Federal Funds Rate": "FOMC Faiz Kararı",
    "FOMC Press Conference": "FOMC Basın Toplantısı",
    "FOMC Meeting Minutes": "FOMC Toplantı Tutanakları",
    "FOMC Economic Projections": "FOMC Ekonomik Projeksiyonları",
    "Beige Book": "Bej Kitap (Fed bölgesel raporu)",
    "Advance GDP": "GSYH (Öncü)",
    "Prelim GDP": "GSYH (İkinci tahmin)",
    "Final GDP": "GSYH (Nihai)",
    "GDP": "GSYH",
    "Advance GDP Price Index": "GSYH Fiyat Endeksi (Öncü)",
    "Prelim GDP Price Index": "GSYH Fiyat Endeksi (İkinci tahmin)",
    "Final GDP Price Index": "GSYH Fiyat Endeksi (Nihai)",
    "Core PCE Price Index": "Çekirdek PCE Fiyat Endeksi (Fed'in enflasyon ölçüsü)",
    "PCE Price Index": "PCE Fiyat Endeksi",
    "Personal Spending": "Kişisel Harcamalar",
    "Personal Income": "Kişisel Gelir",
    "Retail Sales": "Perakende Satışlar",
    "Core Retail Sales": "Çekirdek Perakende Satışlar",
    "ISM Manufacturing PMI": "ISM İmalat PMI",
    "ISM Services PMI": "ISM Hizmet PMI",
    "ISM Manufacturing Prices": "ISM İmalat Fiyatları",
    "ISM Services Prices": "ISM Hizmet Fiyatları",
    "Flash Manufacturing PMI": "Öncü İmalat PMI",
    "Flash Services PMI": "Öncü Hizmet PMI",
    "Final Manufacturing PMI": "Nihai İmalat PMI",
    "Final Services PMI": "Nihai Hizmet PMI",
    "Manufacturing PMI": "İmalat PMI",
    "Non-Manufacturing PMI": "Hizmet (İmalat dışı) PMI",
    "Caixin Manufacturing PMI": "Caixin İmalat PMI",
    "Caixin Services PMI": "Caixin Hizmet PMI",
    "Chicago PMI": "Chicago PMI",
    "CB Consumer Confidence": "Conference Board Tüketici Güveni",
    "Prelim UoM Consumer Sentiment": "Michigan Tüketici Güveni (Öncü)",
    "Revised UoM Consumer Sentiment": "Michigan Tüketici Güveni (Revize)",
    "Prelim UoM Inflation Expectations": "Michigan Enflasyon Beklentisi (Öncü)",
    "Revised UoM Inflation Expectations": "Michigan Enflasyon Beklentisi (Revize)",
    "CB Leading Index": "Conference Board Öncü Göstergeler",
    "Durable Goods Orders": "Dayanıklı Mal Siparişleri",
    "Core Durable Goods Orders": "Çekirdek Dayanıklı Mal Siparişleri",
    "Factory Orders": "Fabrika Siparişleri",
    "Industrial Production": "Sanayi Üretimi",
    "Capacity Utilization Rate": "Kapasite Kullanım Oranı",
    "Empire State Manufacturing Index": "Empire State İmalat Endeksi (NY Fed)",
    "Philly Fed Manufacturing Index": "Philadelphia Fed İmalat Endeksi",
    "Richmond Manufacturing Index": "Richmond Fed İmalat Endeksi",
    "Building Permits": "İnşaat İzinleri",
    "Housing Starts": "Konut Başlangıçları",
    "Existing Home Sales": "Mevcut Konut Satışları",
    "New Home Sales": "Yeni Konut Satışları",
    "Pending Home Sales": "Bekleyen Konut Satışları",
    "NAHB Housing Market Index": "NAHB Konut Piyasası Endeksi",
    "S&P/CS Composite-20 HPI": "S&P/Case-Shiller Konut Fiyatları (20 şehir)",
    "HPI": "FHFA Konut Fiyat Endeksi",
    "Crude Oil Inventories": "Ham Petrol Stokları (EIA)",
    "API Weekly Statistical Bulletin": "API Haftalık Petrol Stokları",
    "Natural Gas Storage": "Doğal Gaz Stokları",
    "Trade Balance": "Dış Ticaret Dengesi",
    "Goods Trade Balance": "Mal Ticaret Dengesi",
    "Current Account": "Cari Denge",
    "Import Prices": "İthalat Fiyatları",
    "Prelim Nonfarm Productivity": "Tarım Dışı Verimlilik (Öncü)",
    "Revised Nonfarm Productivity": "Tarım Dışı Verimlilik (Revize)",
    "Prelim Unit Labor Costs": "Birim İşgücü Maliyetleri (Öncü)",
    "Revised Unit Labor Costs": "Birim İşgücü Maliyetleri (Revize)",
    "Business Inventories": "İşletme Stokları",
    "Wholesale Inventories": "Toptan Satış Stokları",
    "Federal Budget Balance": "Federal Bütçe Dengesi",
    "Consumer Credit": "Tüketici Kredileri",
    "NFIB Small Business Index": "NFIB Küçük İşletme Endeksi",
    "TIC Long-Term Purchases": "TIC Uzun Vadeli Menkul Kıymet Alımları",
    "Treasury Currency Report": "Hazine Döviz Raporu",
    "Bank Holiday": "Resmi Tatil (bankalar kapalı)",
    "Daylight Saving Time Shift": "Yaz Saati Değişimi (ABD–TR saat farkı değişir)",
    "Main Refinancing Rate": "ECB Faiz Kararı",
    "Monetary Policy Statement": "Para Politikası Bildirisi",
    "ECB Press Conference": "ECB Basın Toplantısı",
    "Official Bank Rate": "BoE Faiz Kararı",
    "BOJ Policy Rate": "BoJ Faiz Kararı",
    "BOJ Press Conference": "BoJ Basın Toplantısı",
    "Claimant Count Change": "İşsizlik Ödeneği Başvuruları",
    "German ifo Business Climate": "Almanya ifo İş Ortamı",
    "German ZEW Economic Sentiment": "Almanya ZEW Ekonomik Güven",
    # Nasdaq economic-calendar names (provisional next-week events)
    "Fed Interest Rate Decision": "FOMC Faiz Kararı",
    "FOMC Minutes": "FOMC Toplantı Tutanakları",
    "Initial Jobless Claims": "Haftalık İşsizlik Başvuruları",
    "Continuing Jobless Claims": "Devam Eden İşsizlik Başvuruları",
    "Nonfarm Payrolls": "Tarım Dışı İstihdam",
    "Private Nonfarm Payrolls": "Özel Sektör Tarım Dışı İstihdam",
    "ISM Non-Manufacturing PMI": "ISM Hizmet PMI",
    "ISM Non-Manufacturing Prices": "ISM Hizmet Fiyatları",
    "Michigan Consumer Sentiment": "Michigan Tüketici Güveni",
    "Michigan Consumer Expectations": "Michigan Tüketici Beklentileri",
    "Philadelphia Fed Manufacturing Index": "Philadelphia Fed İmalat Endeksi",
    "NY Empire State Manufacturing Index": "Empire State İmalat Endeksi (NY Fed)",
    "Chicago Fed National Activity": "Chicago Fed Ulusal Aktivite Endeksi",
    "Dallas Fed Mfg Business Index": "Dallas Fed İmalat Endeksi",
    "House Price Index": "FHFA Konut Fiyat Endeksi",
    "US Leading Index": "Conference Board Öncü Göstergeler",
    "Core PCE Prices": "Çekirdek PCE Fiyatları (GSYH içinde, çeyreklik)",
    "Export Prices": "İhracat Fiyatları",
    "OPEC-JMMC Meetings": "OPEC+ İzleme Komitesi Toplantısı",
    "OPEC Meetings": "OPEC Toplantısı",
    "IMF Meetings": "IMF Toplantıları",
    "G7 Meetings": "G7 Toplantıları",
    "G20 Meetings": "G20 Toplantıları",
}
_TITLE_TR_LOWER = {k.lower(): v for k, v in _TITLE_TR.items()}
_PERIOD_TR = {"m/m": "Aylık", "y/y": "Yıllık", "q/q": "Çeyreklik", "q/y": "Yıllık", "ytd/y": "Yılbaşından beri"}
_COUNTRY_PREFIX_TR = {"German": "Almanya", "French": "Fransa", "Italian": "İtalya", "Spanish": "İspanya"}
_SPEAKER_TR = [
    (r"Fed Chair(?:man)? (.+)", "Fed Başkanı {} konuşuyor"),
    (r"Fed Vice Chair(?: for Supervision)? (.+)", "Fed Başkan Yardımcısı {} konuşuyor"),
    (r"Fed Governor (.+)", "Fed Yönetim Kurulu Üyesi {} konuşuyor"),
    (r"FOMC Member (.+)", "FOMC Üyesi {} konuşuyor"),
    (r"Treasury Sec(?:retary)? (.+)", "ABD Hazine Bakanı {} konuşuyor"),
    (r"President (.+)", "ABD Başkanı {} konuşuyor"),
    (r"ECB President (.+)", "ECB Başkanı {} konuşuyor"),
    (r"BOE Gov(?:ernor)? (.+)", "BoE Başkanı {} konuşuyor"),
    (r"BOJ Gov(?:ernor)? (.+)", "BoJ Başkanı {} konuşuyor"),
    (r"(.+)", "{} konuşuyor"),
]


def translate_title(title: str) -> str | None:
    """Turkish name for a ForexFactory event title, e.g. "Core CPI m/m" -> "Aylık Çekirdek TÜFE"."""
    t = (title or "").strip()
    if not t:
        return None
    m = re.fullmatch(r"(.+?) Speaks", t)
    if m:
        for pat, tpl in _SPEAKER_TR:
            sm = re.fullmatch(pat, m.group(1))
            if sm:
                return tpl.format(sm.group(1))
    m = re.fullmatch(r"(\d+)-y (Bond|Note) Auction", t)
    if m:
        return f"{m.group(1)} Yıllık Tahvil İhalesi"
    period = ""
    m = re.fullmatch(r"(.+?) (m/m|y/y|q/q|q/y|ytd/y)", t)
    if m:
        t, period = m.group(1), _PERIOD_TR[m.group(2)]
    prefix = ""
    first, _, rest = t.partition(" ")
    if first in _COUNTRY_PREFIX_TR and rest and rest.lower() in _TITLE_TR_LOWER:
        prefix, t = _COUNTRY_PREFIX_TR[first] + " ", rest
    base = _TITLE_TR_LOWER.get(t.lower())
    if not base:
        return None
    return f"{prefix}{period + ' ' if period else ''}{base}"


# ---------------------------------------------------------------- helpers

def _clean(v) -> str:
    s = html.unescape(str(v if v is not None else "")).replace("\xa0", " ").strip()
    return "" if s in ("-", "N/A", "n/a") else s


def _money(v) -> int | None:
    """"$1,234,567" -> 1234567"""
    s = re.sub(r"[^\d.]", "", str(v or ""))
    try:
        return int(float(s)) if s else None
    except ValueError:
        return None


def _eps(v) -> float | None:
    """"$1.23" -> 1.23, "($0.21)" -> -0.21"""
    s = _clean(v)
    neg = s.startswith("(") or s.startswith("-")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return (-1 if neg else 1) * float(s) if s else None
    except ValueError:
        return None


def _et_ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET).timestamp())


def _today_et() -> date:
    return datetime.fromtimestamp(now_ts(), ET).date()


def _parallel(fn, args: list, workers: int = 4) -> list[tuple[object, object]]:
    """[(arg, result-or-exception)] in input order; a few requests at a time."""
    def safe(a):
        try:
            return a, fn(a)
        except Exception as e:
            return a, e
    if not args:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(args))) as ex:
        return list(ex.map(safe, args))


def nasdaq_data(path: str, params: dict | None = None) -> dict:
    """GET api.nasdaq.com/api/<path> with the browser-like headers it requires -> the "data" object ({} if null)."""
    j = http_get(NASDAQ_API + path, params=params, headers=NASDAQ_HEADERS, timeout=20).json()
    if not isinstance(j, dict):
        raise ValueError("Nasdaq API beklenmeyen yanıt")
    return j.get("data") or {}


# ---------------------------------------------------------------- ForexFactory

_ff_lock = threading.Lock()


def _ff_rate_limited(e: BaseException) -> bool:
    """A real "slow down" answer (HTTP 429/403, or the HTML deny page instead of JSON), not a network failure."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status in (403, 429) or isinstance(e, ValueError)   # requests' JSONDecodeError is a ValueError too


def ff_rows() -> list[dict]:
    """Raw ForexFactory rows for this week (+ next week when published); see ff_rows_ex."""
    return ff_rows_ex()[0]


def ff_rows_ex() -> tuple[list[dict], bool]:
    """(raw ForexFactory rows for this week (+ next week when published), fresh). Downloads at most every
    FF_MIN_GAP seconds (file cache survives restarts); falls back to the cached copy when unavailable
    (fresh=False: never delete stored rows on the strength of a cached copy).
    Only a real rate-limit answer starts the 30-min cooldown; a network failure (offline start, sleep/wake)
    is retried after FF_NET_RETRY seconds."""
    with _ff_lock:
        try:
            cache = jload(FF_CACHE_PATH.read_text(encoding="utf-8"), {}) if FF_CACHE_PATH.exists() else {}
        except OSError:
            cache = {}
        now = now_ts()
        weeks_old = cache.get("weeks") or {}
        cached = weeks_old.get("thisweek", []) + weeks_old.get("nextweek", [])
        retry_at = int(cache.get("retry_ts") or 0)
        if now - int(cache.get("ts") or 0) < FF_MIN_GAP or now < retry_at:
            if weeks_old.get("thisweek"):
                return cached, False
            if now < retry_at:
                raise RuntimeError(f"ForexFactory'ye bağlanılamadı; {retry_at - now} sn sonra tekrar denenecek "
                                   "(nfs.faireconomy.media)")
            wait_min = max(1, -(-(FF_MIN_GAP - (now - int(cache.get("ts") or 0))) // 60))
            raise RuntimeError(f"ForexFactory hız sınırı: son deneme reddedildi, {wait_min} dk sonra tekrar denenecek")
        weeks, err = {}, None
        for name, url in FF_URLS:
            try:
                data = http_get(url, timeout=20, retries=0).json()  # rate-limit page is HTML -> ValueError
                if isinstance(data, list):
                    weeks[name] = data
            except Exception as e:
                if name == "thisweek":  # rate-limited / down: do not also hit nextweek
                    err = e
                    break
        if "thisweek" not in weeks:
            limited = err is not None and _ff_rate_limited(err)
            # Record the failed attempt: a rate limit is not retried before FF_MIN_GAP, a network error after a short pause.
            state = {**cache, "ts": now} if limited else {**cache, "retry_ts": now + FF_NET_RETRY}
            try:
                FF_CACHE_PATH.write_text(jdump(state), encoding="utf-8")
            except OSError:
                pass
            if weeks_old.get("thisweek") and now - int(cache.get("ok_ts") or 0) < 6 * 86400:
                log.warning("ForexFactory unavailable (%s); using cached calendar", err)
                return cached, False
            if err is None:
                raise RuntimeError("ForexFactory takvimi alınamadı: beklenmeyen yanıt (liste değil)")
            if limited:
                raise RuntimeError(f"ForexFactory hız sınırı: {friendly_error(err)}; "
                                   f"{FF_MIN_GAP // 60} dk sonra tekrar denenecek") from err
            raise err   # scheduler.friendly_error: "Bağlantı kurulamadı ... (nfs.faireconomy.media)"
        FF_CACHE_PATH.write_text(jdump({"ts": now, "ok_ts": now, "weeks": weeks}), encoding="utf-8")
        return weeks["thisweek"] + weeks.get("nextweek", []), True


def ff_events(rows: list[dict], source: str) -> list[dict]:
    """ForexFactory rows -> macro events: every USD event + high-impact EUR/GBP/JPY/CNY/global ones."""
    out, seen = [], set()
    for r in rows:
        try:
            cur = str(r.get("country") or "").strip().upper()
            imp_raw = str(r.get("impact") or "").strip().lower()
            impact = FF_IMPACT.get(imp_raw, "")
            if cur != "USD" and not (cur in FF_MAJOR and impact == "high"):
                continue
            title = str(r.get("title") or "").strip()
            ts = to_ts(r.get("date"))
            if not title or not ts:
                continue
            dt = datetime.fromtimestamp(ts, ET)
            all_day = imp_raw == "holiday" or (dt.hour == 0 and dt.minute == 0)
            eid = make_id("ff", cur, title, dt.date().isoformat())
            if eid in seen:  # same title twice a day (e.g. two speeches)
                eid = make_id("ff", cur, title, ts)
            seen.add(eid)
            extra = {"currency": cur, "date_et": dt.strftime("%Y-%m-%d"), "time_et": "" if all_day else dt.strftime("%H:%M"),
                     "impact_raw": r.get("impact") or ""}
            tr = translate_title(title)
            if tr:
                extra["title_tr"] = tr
            if imp_raw == "holiday":
                extra["holiday"] = True
            out.append({"id": eid, "kind": "macro", "title": title, "country": FF_COUNTRY.get(cur, cur), "ts": ts,
                        "all_day": all_day, "impact": impact, "forecast": _clean(r.get("forecast")),
                        "previous": _clean(r.get("previous")), "actual": _clean(r.get("actual")), "ticker": "",
                        "source": source, "extra": extra})
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- actuals from Nasdaq economic calendar

NASDAQ_COUNTRY_CUR = {"United States": "USD", "Euro Zone": "EUR", "Germany": "EUR", "France": "EUR", "Italy": "EUR",
                      "Spain": "EUR", "United Kingdom": "GBP", "Japan": "JPY", "China": "CNY", "Canada": "CAD",
                      "Australia": "AUD", "New Zealand": "NZD", "Switzerland": "CHF"}
_ALIASES = [(r"\bunemployment claims\b", "initial jobless claims"),
            (r"\bnon-farm employment change\b", "nonfarm payrolls"),
            (r"\bnon-farm\b", "nonfarm"),
            (r"\buom inflation expectations\b", "michigan 1-year inflation expectations"),
            (r"\buom\b", "michigan"),
            (r"\bapi weekly statistical bulletin\b", "api weekly crude oil stock"),
            (r"\bfederal funds rate\b", "fed interest rate decision"),
            (r"\bism services\b", "ism non-manufacturing"),
            (r"\bm/m\b", " mom "), (r"\by/y\b", " yoy "), (r"\bq/q\b", " qoq "), (r"\bq/y\b", " yoy ")]
_STOP = {"the", "of", "s", "p", "global", "us", "u", "flash", "final", "prelim", "revised", "advance", "index",
         "cb", "a", "and", "sa", "nsa", "mom", "yoy", "qoq"}
_PERIODS = ("mom", "yoy", "qoq")

_econ_lock = threading.Lock()
_econ_cache: dict[str, tuple[int, list]] = {}   # query date -> (fetched_at, rows)


def _tokens(title: str, ff: bool) -> tuple[set[str], set[str]]:
    s = title.lower()
    if ff:
        for pat, rep in _ALIASES:
            s = re.sub(pat, rep, s)
    s = s.replace("(mom)", " mom ").replace("(yoy)", " yoy ").replace("(qoq)", " qoq ")
    words = re.sub(r"[^a-z0-9]+", " ", s).split()
    return {w for w in words if w not in _STOP}, {w for w in words if w in _PERIODS}


def _parse_val(s) -> tuple[float, str, int] | None:
    m = re.fullmatch(r"[<>]?\s*(-?[\d,]*\.?\d+)\s*([%KMBT]?)", _clean(s), re.I)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    return float(num), m.group(2).upper(), len(num.split(".")[1]) if "." in num else 0


def _val_match(ff_val, nq_val) -> bool:
    a, b = _parse_val(ff_val), _parse_val(nq_val)
    return bool(a and b and a[1] == b[1] and abs(a[0] - b[0]) <= 0.5 * 10 ** -a[2] + 1e-9)


def _econ_rows(q: date, fresh: bool = False) -> list[dict]:
    key = q.isoformat()
    with _econ_lock:
        hit = _econ_cache.get(key)
    if not fresh and hit and (datetime.fromtimestamp(hit[0], ET).date() > q + timedelta(days=1)
                              or now_ts() - hit[0] < 240):
        return hit[1]
    # Nasdaq serves stale per-URL cached copies of this endpoint (e.g. a 1-row answer for a busy day, another
    # day's rows); a changing extra parameter returns the current data.
    rows = nasdaq_data("calendar/economicevents", {"date": key, "_": now_ts() // 60}).get("rows") or []
    with _econ_lock:
        _econ_cache[key] = (now_ts(), rows)
        for k in [k for k in _econ_cache if k < (q - timedelta(days=21)).isoformat()]:
            _econ_cache.pop(k, None)
    return rows


def fill_actuals(events: list[dict]) -> int:
    """Best effort: copy released values from Nasdaq's economic calendar into FF events (same ET time,
    matching name, and forecast or previous value agreeing). Nasdaq's `date` parameter is not reliably
    aligned (currently it returns the previous day), so both D and D+1 are scanned."""
    now = now_ts()
    pending = [e for e in events if not e["actual"] and not e["all_day"] and e["ts"] <= now + 60
               and now - e["ts"] < 6 * 86400 and (e["forecast"] or e["previous"])]
    if not pending:
        return 0
    today = _today_et()
    qdates = sorted({datetime.fromtimestamp(e["ts"], ET).date() + timedelta(days=k) for e in pending for k in (0, 1)})
    rows: list[dict] = []
    for q, res in _parallel(_econ_rows, [q for q in qdates if q <= today + timedelta(days=1)]):
        if isinstance(res, Exception):
            log.debug("nasdaq economic events %s failed: %s", q, res)
        else:
            rows += res
    filled = 0
    for e in pending:
        cur = e["extra"].get("currency")
        hhmm = datetime.fromtimestamp(e["ts"], ET).strftime("%H:%M")
        ftoks, fper = _tokens(e["title"], True)
        best = None
        for r in rows:
            try:
                if NASDAQ_COUNTRY_CUR.get(r.get("country")) != cur or (r.get("gmt") or "").strip() != hhmm:
                    continue
                actual = _clean(r.get("actual"))
                if not actual:
                    continue
                ntoks, nper = _tokens(r.get("eventName") or "", False)
                if fper and nper and fper != nper:
                    continue
                union = ftoks | ntoks
                sim = len(ftoks & ntoks) / len(union) if union else 0
                if sim < 0.75:
                    continue
                score = (_val_match(e["forecast"], r.get("consensus")) + _val_match(e["previous"], r.get("previous")), sim)
                if score[0] and (best is None or score > best[0]):
                    best = (score, actual, r.get("eventName"))
            except Exception:
                continue
        if best:
            e["actual"] = best[1]
            e["extra"]["actual_source"] = "nasdaq"
            e["extra"]["actual_name"] = best[2]
            filled += 1
    return filled


# ---------------------------------------------------------------- provisional USD events beyond ForexFactory's week

PROV_SOURCE = "nasdaq_econ"
PROV_TTL = 3600            # future events rarely change: rebuild the provisional days at most hourly
PROV_RETRY = 600           # ... or sooner when some days could not be built
PROV_DAYS = 7              # look this many days ahead (today .. today+7)

# Weekly US releases that pin a Nasdaq answer to its real weekday (its `date` parameter is not reliably aligned).
_NQ_WEEKDAY = {"3-month bill auction": 0, "6-month bill auction": 0,
               "redbook": 1, "api weekly crude oil stock": 1,
               "mba mortgage applications": 2, "crude oil inventories": 2,
               "initial jobless claims": 3, "continuing jobless claims": 3, "natural gas storage": 3,
               "fed's balance sheet": 3, "4-week bill auction": 3, "8-week bill auction": 3,
               "u.s. baker hughes oil rig count": 4, "u.s. baker hughes total rig count": 4,
               "cftc s&p 500 speculative net positions": 4}
# Sub-series and plumbing data nobody trades on.
_NQ_SKIP = re.compile(r"bill auction|^mba (?!mortgage applications)|^mortgage (?:market|refinance) index|^cftc |"
                      r"baker hughes|^redbook|^eia |cushing|distillate|gasoline|heating oil|refinery|crude oil imports|"
                      r"balance sheet|reserve balances|^m2 money|gdpnow|ex auto$|^employment (?:benefits|wages)$|"
                      r"durables excluding|goods orders non defense|4-week avg|^texas services|services revenues|"
                      r"manufacturing shipments|^richmond services|^gdp sales|^real consumer spending|^dallas fed pce", re.I)
_NQ_HIGH = re.compile(r"fomc (?:statement|press conference)|interest rate decision|fed chair|powell|nonfarm payrolls|"
                      r"^unemployment rate|\bcpi\b|\bppi\b|^gdp\b(?! price)|\bpce price index|retail sales|^ism .*pmi$|"
                      r"initial jobless claims|jolts|average hourly earnings", re.I)
_NQ_MEDIUM = re.compile(r"durable goods|consumer confidence|michigan|sentiment|\bpmi\b|housing starts|building permits|"
                        r"home sales|industrial production|\badp\b|philadelphia fed|empire state|personal (?:income|spending)|"
                        r"trade balance|crude oil inventories|employment cost|productivity|unit labor|factory orders|"
                        r"challenger|speaks|beige book|fomc minutes|continuing jobless|import prices|gdp price|"
                        r"leading index|natural gas storage|\bpce\b|^ism ", re.I)
_NQ_TO_FF = [(r"\s*\(MoM\)$", " m/m"), (r"\s*\(YoY\)$", " y/y"), (r"\s*\(QoQ\)$", " q/q"),
             (r"^(\d+)-Year (Note|Bond) Auction$", r"\1-y \2 Auction"),
             (r"^ADP Nonfarm Employment Change$", "ADP Non-Farm Employment Change"),
             (r"^ADP Employment Change Weekly$", "ADP Weekly Employment Change"),
             (r"^API Weekly Crude Oil Stock$", "API Weekly Statistical Bulletin"),
             (r"^S&P Global (?:US )?(Manufacturing|Services|Composite) PMI$", r"\1 PMI"),
             (r"^S&P/CS HPI Composite - 20 n\.s\.a\.$", "S&P/CS Composite-20 HPI"),
             (r"^JOLTs", "JOLTS")]


def _nq_title_tr(name: str) -> str | None:
    tr = translate_title(name)
    if tr:
        return tr
    ff = name
    for pat, rep in _NQ_TO_FF:
        ff = re.sub(pat, rep, ff)
    return translate_title(ff) if ff != name else None


def _nq_impact(name: str) -> str:
    return "high" if _NQ_HIGH.search(name) else "medium" if _NQ_MEDIUM.search(name) else "low"


def _nq_weekday(rows: list[dict]) -> int | None:
    """Weekday the rows really belong to (majority of weekly anchor releases); None when unknown or ambiguous."""
    votes = Counter(_NQ_WEEKDAY[n] for n in {_clean(r.get("eventName")).lower() for r in rows} if n in _NQ_WEEKDAY)
    if not votes:
        return None
    (wd, n), *rest = votes.most_common()
    return None if rest and rest[0][1] == n else wd


def _nq_events(d: date, rows: list[dict]) -> list[dict]:
    """Nasdaq US rows of trading day `d` -> provisional macro events (duplicate names at one time collapsed,
    'FOMC Statement' folded into the rate decision)."""
    picked: dict[tuple[str, str], dict] = {}
    for r in rows:
        name = re.sub(r"\s+", " ", _clean(r.get("eventName")))
        if not name or _NQ_SKIP.search(name):
            continue
        hhmm = _clean(r.get("gmt"))
        if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
            hhmm = ""
        key = (name.lower(), hhmm)
        if key not in picked or (not _clean(picked[key].get("consensus")) and _clean(r.get("consensus"))):
            picked[key] = r
    merged = set()
    for k in [k for k in picked if k[0] == "fomc statement" and ("fed interest rate decision", k[1]) in picked]:
        picked.pop(k)
        merged.add(("fed interest rate decision", k[1]))
    out = []
    for key, r in picked.items():
        hhmm = key[1]
        name = re.sub(r"\s+", " ", _clean(r.get("eventName")))
        h, m = (int(x) for x in hhmm.split(":")) if hhmm else (0, 0)
        extra = {"currency": "USD", "date_et": d.isoformat(), "time_et": hhmm, "provisional": True, "provider": "nasdaq",
                 "note_tr": "Geçici kayıt (Nasdaq takvimi); ForexFactory bu haftayı yayımlayınca yerini onun kaydı alır."}
        tr = _nq_title_tr(name)
        if tr:
            extra["title_tr"] = tr
        if key in merged:
            extra["merged"] = ["FOMC Statement"]
        out.append({"id": make_id(PROV_SOURCE, name, d.isoformat(), hhmm), "kind": "macro", "title": name,
                    "country": "US", "ts": _et_ts(d, h, m), "all_day": not hhmm, "impact": _nq_impact(name),
                    "forecast": _clean(r.get("consensus")), "previous": _clean(r.get("previous")),
                    "actual": _clean(r.get("actual")), "ticker": "", "source": PROV_SOURCE, "extra": extra})
    return out


def nasdaq_provisional(days: list[date]) -> tuple[list[dict], list[date]]:
    """USD macro events for `days` from Nasdaq's economic calendar -> (events, days actually built).
    Nasdaq's `date=D` currently answers with D-1, but not reliably, so each answer is pinned to its real weekday
    with weekly anchor releases (jobless claims = Thursday, MBA = Wednesday ...); an unpinned answer is used only
    when the other answers of this run agree on one offset."""
    # q -> the distinct answers seen for it (Nasdaq's servers disagree: the same query may return either day)
    answers: dict[date, list[tuple[int | None, list[dict]]]] = {}
    errors: list[Exception] = []

    def ask(qs, fresh: bool) -> None:
        for q, res in _parallel(lambda x: _econ_rows(x, fresh), sorted(qs)):
            if isinstance(res, Exception):
                errors.append(res)
                continue
            rows = [r for r in res if r.get("country") == "United States"]
            seen = answers.setdefault(q, [])
            if all(rows != v for _, v in seen):
                seen.append((_nq_weekday(rows), rows))

    def choose(d: date, majority: int | None, fallback: int | None) -> list[dict] | None:
        cands = [(q, rows) for q in (d, d + timedelta(days=1)) for wd, rows in answers.get(q, []) if wd == d.weekday()]
        if len({(q - d).days for q, _ in cands}) > 1:   # both offsets claim the day: trust the usual one
            cands = [(q, rows) for q, rows in cands if (q - d).days == majority]
        if cands:
            return cands[0][1]
        if fallback is not None:
            unpinned = [rows for wd, rows in answers.get(d + timedelta(days=fallback), []) if wd is None]
            if unpinned and len(answers[d + timedelta(days=fallback)]) == 1:
                return unpinned[0]
        return None

    ask({d + timedelta(days=k) for d in days for k in (0, 1)}, False)
    if not answers and errors:
        raise errors[0]
    for attempt in range(3):
        offsets = Counter(k for q, variants in answers.items() for wd, _ in variants if wd is not None
                          for k in (0, 1) if (q - timedelta(days=k)).weekday() == wd)
        ranked = offsets.most_common(2)
        majority = ranked[0][0] if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]) else None
        fallback = majority if len(offsets) == 1 and sum(offsets.values()) >= 2 else None
        picked = {d: choose(d, majority, fallback) for d in days}
        missing = [d for d in days if picked[d] is None]
        if not missing or attempt == 2:
            break
        ask({d + timedelta(days=k) for d in missing for k in (0, 1)}, True)   # ask again, fresh
    events, built = [], []
    for d in days:
        evs = _nq_events(d, picked[d]) if picked[d] else []
        if evs:
            events += evs
            built.append(d)
    return events, built


class EconCalendarCollector(Collector):
    key = "ff_calendar"
    name = "Ekonomik Takvim (ForexFactory)"
    group = "data"
    kind = "calendar"
    source_type = "api"
    interval = 300   # FF itself is downloaded at most every 30 min; runs in between only refresh actuals
    description = ("ABD makro veri takvimi (TÜFE, istihdam, FOMC...) + yüksek etkili Euro/Çin/İngiltere/Japonya "
                   "verileri; etki derecesi, beklenti/önceki ve açıklanan değerler (Nasdaq'tan). ForexFactory gelecek "
                   "haftayı henüz yayımlamadıysa ABD verileri Nasdaq takviminden geçici olarak eklenir.")

    def __init__(self) -> None:
        self._prov: tuple[int, tuple, list[dict]] | None = None   # (reuse_until, (ff_last, targets), events)

    def fetch(self, cfg: dict) -> dict:
        rows, fresh = ff_rows_ex()
        events = ff_events(rows, self.key)
        self._restore_actuals(events)
        try:
            fill_actuals(events)
        except Exception as e:  # never lose the calendar because of the actuals step
            log.warning("actuals fill failed: %s", e)
        res = {"events": events}
        # A freshly downloaded FF file is the full list of its days: rows it no longer has (a cancelled / renamed
        # event) are removed by the scheduler. Never for the cached fallback.
        days = sorted({datetime.fromtimestamp(ts, ET).date() for ts in (to_ts(r.get("date")) for r in rows) if ts})
        if fresh and days and events:
            res["event_windows"] = [{"start": _et_ts(days[0], 0), "end": _et_ts(days[-1] + timedelta(days=1), 0),
                                     "kind": "macro"}]
        try:
            events += self._provisional(rows)
        except Exception as e:  # noqa: BLE001 - the provisional days are a bonus
            log.warning("provisional macro calendar failed: %s", e)
        return res

    def _provisional(self, rows: list[dict]) -> list[dict]:
        """USD events from Nasdaq for trading days after the last ForexFactory-covered day (up to today+7).
        Provisional rows of days FF now covers are deleted, so FF and Nasdaq never show the same day twice."""
        days = [datetime.fromtimestamp(ts, ET).date() for ts in (to_ts(r.get("date")) for r in rows) if ts]
        if not days:
            return []
        from .. import db
        first, last = min(days), max(days)
        if last.weekday() < 5:   # an FF week runs Sunday..Saturday
            last += timedelta(days=5 - last.weekday())
        db.delete_events(PROV_SOURCE, _et_ts(first, 0), _et_ts(last + timedelta(days=1), 0), keep_ids=())
        today = _today_et()
        targets = tuple(d for d in (today + timedelta(days=i) for i in range(PROV_DAYS + 1))
                        if d > last and is_trading_day(d))
        if not targets:
            self._prov = None
            return []
        key = (last.isoformat(), targets)
        if self._prov and self._prov[1] == key and now_ts() < self._prov[0]:
            return [dict(e, extra=dict(e["extra"])) for e in self._prov[2]]
        events, built = nasdaq_provisional(list(targets))
        for d in built:   # replace what an earlier run stored for these days (renamed / moved events)
            db.delete_events(PROV_SOURCE, _et_ts(d, 0), _et_ts(d + timedelta(days=1), 0),
                             keep_ids=[e["id"] for e in events if e["extra"]["date_et"] == d.isoformat()])
        # Some days could not be pinned (Nasdaq served a stale copy): try again sooner than the hourly rebuild.
        self._prov = (now_ts() + (PROV_TTL if len(built) == len(targets) else PROV_RETRY), key, events)
        return [dict(e, extra=dict(e["extra"])) for e in events]

    def _restore_actuals(self, events: list[dict]) -> None:
        """Re-use actuals found earlier (events are upserted with INSERT OR REPLACE)."""
        if not events:
            return
        known = {}
        try:
            from .. import db
            lo, hi = min(e["ts"] for e in events), max(e["ts"] for e in events) + 1
            known = {e["id"]: e for e in db.query_events(lo, hi, kind="macro") if e.get("actual")}
        except Exception:
            pass
        for e in events:
            old = known.get(e["id"])
            if old and not e["actual"]:
                e["actual"] = old["actual"]
                for k in ("actual_source", "actual_name"):
                    if (old.get("extra") or {}).get(k):
                        e["extra"][k] = old["extra"][k]


# ---------------------------------------------------------------- Nasdaq earnings

EARN_TIME = {"time-pre-market": "bmo", "time-after-hours": "amc"}
EARN_TIME_TR = {"bmo": "Açılış öncesi", "amc": "Kapanış sonrası", "unknown": "Saat belirsiz"}
_earn_lock = threading.Lock()
_earn_times: dict[tuple[str, str], str] = {}   # (ticker, YYYY-MM-DD) -> bmo/amc seen earlier


def _earnings_days(today: date) -> list[date]:
    """Previous trading day (for reported EPS) + today + next 6 trading days."""
    prev = today - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    days, d = [prev, today], today
    while len(days) < 8:
        d += timedelta(days=1)
        if is_trading_day(d):
            days.append(d)
    return days


def _known_earning_times(start_ts: int, end_ts: int, source: str) -> dict[tuple[str, str], str]:
    """bmo/amc already stored (Nasdaq drops the time once a report is out)."""
    out = {}
    try:
        from .. import db
        for e in db.query_events(start_ts, end_ts, kind="earnings"):
            t = (e.get("extra") or {}).get("time")
            if e.get("source") == source and t in ("bmo", "amc"):
                out[(e["ticker"], datetime.fromtimestamp(e["ts"], ET).strftime("%Y-%m-%d"))] = t
    except Exception:
        pass
    return out


class NasdaqEarningsCollector(Collector):
    key = "nasdaq_earnings_cal"
    name = "Bilanço Takvimi (Nasdaq)"
    group = "data"
    kind = "calendar"
    source_type = "api"
    interval = 3600
    description = "Bugün ve önümüzdeki 6 işlem günü bilanço açıklayacak şirketler: açılış öncesi / kapanış sonrası, EPS beklentisi."

    def fetch(self, cfg: dict) -> dict:
        days = _earnings_days(_today_et())
        with _earn_lock:
            for k in [k for k in _earn_times if k[1] < days[0].isoformat()]:
                del _earn_times[k]
        known = _known_earning_times(_et_ts(days[0], 0), _et_ts(days[-1] + timedelta(days=1), 0), self.key)
        events, errors, windows = [], [], []
        today = _today_et()
        for d, res in _parallel(lambda d: nasdaq_data("calendar/earnings", {"date": d.isoformat()}), days):
            if isinstance(res, Exception):
                errors.append(res)
                continue
            rows = res.get("rows") if isinstance(res, dict) else None
            for r in rows or []:
                try:
                    ev = self._event(r, d, known)
                    if ev:
                        events.append(ev)
                except Exception:
                    continue
            # A day answered with rows is that day's full list: a moved / withdrawn report disappears.
            # Not the previous day (kept for the reported EPS), never an errored or empty answer.
            if d >= today and isinstance(rows, list) and rows:
                windows.append({"start": _et_ts(d, 0), "end": _et_ts(d + timedelta(days=1), 0), "kind": "earnings"})
        if not events and errors:
            raise errors[0]
        res = {"events": events}
        if windows:
            res["event_windows"] = windows
        if errors:   # some days missing: OK, but say so
            res["warnings"] = [f"Kısmi hata — {len(errors)}/{len(days)} günün bilanço listesi alınamadı: "
                               f"{friendly_error(errors[0])}"[:300]]
        return res

    def _event(self, r: dict, d: date, known: dict) -> dict | None:
        sym = _clean(r.get("symbol")).upper().replace("/", ".")
        if not sym:
            return None
        day = d.isoformat()
        t = EARN_TIME.get(r.get("time"), "unknown")
        with _earn_lock:
            if t == "unknown":
                t = _earn_times.get((sym, day)) or known.get((sym, day)) or "unknown"
            else:
                _earn_times[(sym, day)] = t
        h, m = {"bmo": (8, 0), "amc": (16, 5)}.get(t, (12, 0))
        company = _clean(r.get("name")) or sym
        mcap = _money(r.get("marketCap"))
        eps_fc, eps_act = _clean(r.get("epsForecast")), _clean(r.get("eps"))
        impact = "high" if (mcap or 0) >= 50e9 else "medium" if (mcap or 0) >= 2e9 else "low"
        try:
            ests = int(_clean(r.get("noOfEsts")))
        except ValueError:
            ests = None
        extra = {"time": t, "time_tr": EARN_TIME_TR[t], "company": company, "market_cap": mcap,
                 "eps_forecast": _eps(eps_fc), "num_estimates": ests,
                 "fiscal_quarter": _clean(r.get("fiscalQuarterEnding")),
                 "last_year_eps": _eps(r.get("lastYearEPS")), "last_year_date": _clean(r.get("lastYearRptDt")),
                 "date_et": day}
        if eps_act:
            extra["eps_actual"] = _eps(eps_act)
            try:
                extra["surprise_pct"] = float(_clean(r.get("surprise")))
            except ValueError:
                pass
        return {"id": make_id("earnings", sym, day), "kind": "earnings", "title": f"{company} ({sym}) bilançosu",
                "country": "US", "ts": _et_ts(d, h, m), "all_day": t == "unknown", "impact": impact,
                "forecast": eps_fc, "previous": _clean(r.get("lastYearEPS")), "actual": eps_act,
                "ticker": sym, "source": self.key, "extra": extra}


# ---------------------------------------------------------------- Nasdaq IPO calendar

class NasdaqIpoCollector(Collector):
    key = "nasdaq_ipo_cal"
    name = "Halka Arz Takvimi (Nasdaq)"
    group = "data"
    kind = "calendar"
    source_type = "api"
    interval = 3600
    description = "Yaklaşan ve son 2 haftada fiyatlanan ABD halka arzları (fiyat aralığı, hisse adedi, büyüklük)."

    def fetch(self, cfg: dict) -> dict:
        today = _today_et()
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        events, errors, oldest, windows = [], [], today - timedelta(days=14), []
        for month in (today, nxt):
            try:
                data = nasdaq_data("ipo/calendar", {"date": month.strftime("%Y-%m")})
            except Exception as e:
                errors.append(e)
                continue
            sections = [("upcoming", ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []),
                        ("priced", (data.get("priced") or {}).get("rows") or [])]
            if sections[0][1]:   # this month's upcoming list answered with rows: a withdrawn IPO disappears
                first = month.replace(day=1)
                windows.append({"start": _et_ts(first, 0),
                                "end": _et_ts((first + timedelta(days=32)).replace(day=1), 0),
                                "kind": "ipo", "status": "upcoming"})
            for status, rows in sections:
                for r in rows:
                    try:
                        ev = self._event(r, status)
                        if ev and (status == "upcoming" or datetime.fromtimestamp(ev["ts"], ET).date() >= oldest):
                            events.append(ev)
                    except Exception:
                        continue
        if not events and errors:
            raise errors[0]
        res = {"events": list({e["id"]: e for e in events}.values())}
        if windows:
            res["event_windows"] = windows
        return res

    def _event(self, r: dict, status: str) -> dict | None:
        raw_date = _clean(r.get("expectedPriceDate") if status == "upcoming" else r.get("pricedDate"))
        try:
            d = datetime.strptime(raw_date, "%m/%d/%Y").date()
        except ValueError:
            return None
        sym = _clean(r.get("proposedTickerSymbol")).upper()
        company = _clean(r.get("companyName"))
        if not company:
            return None
        price = _clean(r.get("proposedSharePrice"))
        amount = _money(r.get("dollarValueOfSharesOffered"))
        shares = _money(r.get("sharesOffered"))
        spac = bool(re.search(r"\b(acquisition|spac|capital corp\.? [ivx]+)\b", company, re.I))
        impact = "low" if spac else "high" if (amount or 0) >= 500e6 else "medium" if (amount or 0) >= 100e6 else "low"
        title = f"Halka arz: {company}" + (f" ({sym})" if sym else "")
        return {"id": make_id("ipo", r.get("dealID") or f"{sym}|{company}"), "kind": "ipo", "title": title,
                "country": "US", "ts": _et_ts(d, 9, 30), "all_day": True, "impact": impact,
                "forecast": price if status == "upcoming" else "", "previous": "",
                "actual": price if status == "priced" else "", "ticker": sym, "source": self.key,
                "extra": {"status": status, "status_tr": "Yaklaşan" if status == "upcoming" else "Fiyatlandı",
                          "company": company, "exchange": _clean(r.get("proposedExchange")), "price": price,
                          "shares": shares, "offer_amount": amount, "spac": spac, "deal_id": r.get("dealID"),
                          "date_et": d.isoformat()}}


COLLECTORS = [EconCalendarCollector(), NasdaqEarningsCollector(), NasdaqIpoCollector()]
