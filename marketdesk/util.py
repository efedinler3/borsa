"""Shared helpers: paths, HTTP, ids, time zones, market session."""
from __future__ import annotations

import calendar
import email.utils
import hashlib
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import time
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("marketdesk")

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"

# Data lives outside OneDrive (SQLite + OneDrive sync = locking trouble, and keys should not sync).
DATA_DIR = Path(os.environ.get("MARKETDESK_DATA") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PiyasaMasasi"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_DIR = DATA_DIR / "claude"
CLAUDE_IN_DIR = CLAUDE_DIR / "girdi"
CLAUDE_OUT_DIR = CLAUDE_DIR / "cikti"
for _d in (CLAUDE_IN_DIR, CLAUDE_OUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")
UTC = timezone.utc

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})


def _read_body(r: requests.Response, total_timeout: float) -> None:
    """Read the body under a total deadline. `timeout` only bounds each socket read, so a server trickling
    bytes could otherwise hold a collector thread far beyond the limit. Sets r.content like a normal GET."""
    deadline = time.monotonic() + total_timeout
    chunks: list[bytes] = []

    def expired() -> None:
        if time.monotonic() > deadline:
            r.close()
            raise requests.Timeout(f"yanıt {int(total_timeout)} sn içinde tamamlanmadı")

    read1 = getattr(getattr(r, "raw", None), "read1", None)
    if read1 is not None:  # returns as soon as any bytes arrive (iter_content would wait for a full chunk)
        while True:
            data = read1(65536, decode_content=True)
            if not data:
                break
            chunks.append(data)
            expired()
    else:
        for chunk in r.iter_content(4096):
            if chunk:
                chunks.append(chunk)
            expired()
    r._content = b"".join(chunks)  # requests' own attribute: makes .content/.text/.json() work as usual
    r._content_consumed = True


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: float = 15, retries: int = 1, total_timeout: float = 60) -> requests.Response:
    """GET with a browser UA and one retry on network errors / 5xx. Raises on final failure (incl. 4xx).
    `total_timeout` bounds the whole response body read (per request, not per socket read)."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout, stream=True)
            if r.status_code >= 500 and attempt < retries:
                r.close()
                time.sleep(1.5)
                continue
            r.raise_for_status()
            _read_body(r, total_timeout)
            return r
        except requests.RequestException as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status < 500:
                break
            if attempt < retries:
                time.sleep(1.5)
    assert last_exc is not None
    raise last_exc


def http_json(url: str, **kw):
    return http_get(url, **kw).json()


# ---------------------------------------------------------------- Turkish error texts (source_status.last_error)

HTTP_LABELS = {
    400: "geçersiz istek", 401: "yetkisiz — API anahtarı geçersiz, Ayarlar'dan kontrol edin",
    402: "ücretli plan gerekli", 403: "erişim engellendi", 404: "adres bulunamadı", 405: "yönteme izin yok",
    408: "zaman aşımı", 410: "kaynak kaldırılmış", 429: "istek sınırı aşıldı (çok sık istek)",
    451: "yasal nedenle engelli", 500: "sunucu hatası", 502: "ağ geçidi hatası",
    503: "hizmet geçici olarak kullanılamıyor", 504: "ağ geçidi zaman aşımı",
}
_PY_ERRORS = (KeyError, TypeError, AttributeError, IndexError, NameError, ImportError, AssertionError,
              NotImplementedError, ZeroDivisionError, UnicodeError, RecursionError)


def _host(e: BaseException) -> str:
    for obj in (getattr(e, "response", None), getattr(e, "request", None)):
        url = getattr(obj, "url", None)
        if url:
            try:
                return (urlparse(url).hostname or "").removeprefix("www.")
            except ValueError:
                return ""
    return ""


def friendly_error(e: BaseException) -> str:
    """Short Turkish error text for source_status.last_error."""
    host = _host(e)
    where = f" ({host})" if host else ""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(e, requests.RequestException) and code:
        label = HTTP_LABELS.get(code) or ("sunucu hatası" if code >= 500 else "istek reddedildi")
        return f"HTTP {code} — {label}{where}"
    if isinstance(e, (requests.Timeout, socket.timeout, TimeoutError, FuturesTimeout)):
        return f"Zaman aşımı{where}"
    if isinstance(e, requests.exceptions.SSLError):
        return f"SSL/sertifika hatası{where}"
    if isinstance(e, requests.ConnectionError):
        return f"Bağlantı kurulamadı — internet ya da site erişilemiyor{where}"
    if isinstance(e, (json.JSONDecodeError, requests.exceptions.JSONDecodeError)):
        return f"Geçersiz yanıt (JSON okunamadı){where}"
    msg = " ".join(str(e).split())
    low = msg.lower()
    if ("api key" in low or "apikey" in low or "api_key" in low) and ("missing" in low or "required" in low):
        return "Ayarlar'da API anahtarı gerekli"
    if isinstance(e, _PY_ERRORS) or not msg:
        return f"Beklenmeyen hata ({type(e).__name__}: {msg[:160]})" if msg else f"Beklenmeyen hata ({type(e).__name__})"
    return msg[:400]


def now_ts() -> int:
    return int(time.time())


def make_id(*parts: object) -> str:
    raw = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def normalize_url(url: str) -> str:
    """Strip tracking query params / fragments so the same article dedups."""
    if not url:
        return ""
    url = url.strip().split("#", 1)[0]
    if "?" in url:
        base, query = url.split("?", 1)
        keep = [p for p in query.split("&") if p and not re.match(r"(utm_|cmpid|mod=|yptr|ncid|src=|guccounter)", p, re.I)]
        url = base + ("?" + "&".join(keep) if keep else "")
    return url.rstrip("/")


def to_ts(value) -> int | None:
    """Best-effort conversion of RSS/ISO/epoch/struct_time/datetime to unix seconds (UTC)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            return int(v / 1000) if v > 1e12 else int(v)
        if isinstance(value, time.struct_time):
            return int(calendar.timegm(value))
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return int(value.timestamp())
        s = str(value).strip()
        if s.isdigit():
            return to_ts(int(s))
        try:
            d = email.utils.parsedate_to_datetime(s)
            if d is not None:
                return to_ts(d)
        except (TypeError, ValueError, IndexError):
            pass
        s2 = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s2)
        return to_ts(d)
    except (ValueError, OverflowError, TypeError):
        return None


def fmt_et(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, ET).strftime(fmt)


def fmt_tr(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, TR).strftime(fmt)


# NYSE full-day holidays (observed dates). Explicit table (wins for the years it covers); other years are
# computed by rule in _nyse_year().
NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}


# NYSE early closes (13:00 ET).
NYSE_EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}

# FOMC rate-decision days (statement 14:00 ET), from the Fed's published calendar. Add the next year once the Fed
# publishes it (federalreserve.gov/monetarypolicy/fomccalendars.htm); briefs only use it as a hint when the macro
# calendar is not loaded yet.
FOMC_DECISIONS = (date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17), date(2026, 7, 29),
                  date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9))

_TABLE_YEARS = frozenset(d.year for d in NYSE_HOLIDAYS)
_nyse_cache: dict[int, tuple[frozenset, frozenset]] = {}


def _easter(y: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    n = h + l - 7 * m + 114
    return date(y, n // 31, n % 31 + 1)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) `weekday` (Mon=0) of the month; n=-1 = the last one."""
    if n > 0:
        first = date(y, month, 1)
        return date.fromordinal(first.toordinal() + (weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(y + (month == 12), month % 12 + 1, 1)
    last = date.fromordinal(nxt.toordinal() - 1)
    return date.fromordinal(last.toordinal() - (last.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """NYSE observance: Saturday -> Friday, Sunday -> Monday; New Year's Day on a Saturday is not observed."""
    if d.weekday() == 5:
        return None if (d.month, d.day) == (1, 1) else date.fromordinal(d.toordinal() - 1)
    if d.weekday() == 6:
        return date.fromordinal(d.toordinal() + 1)
    return d


def _nyse_rules(y: int) -> tuple[frozenset, frozenset]:
    hol = {_observed(date(y, 1, 1)), _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3),
           date.fromordinal(_easter(y).toordinal() - 2), _nth_weekday(y, 5, 0, -1),
           _observed(date(y, 6, 19)) if y >= 2022 else None, _observed(date(y, 7, 4)),
           _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4), _observed(date(y, 12, 25))}
    hol.discard(None)
    thanksgiving = _nth_weekday(y, 11, 3, 4)
    early = {date.fromordinal(thanksgiving.toordinal() + 1)}
    for d in (date(y, 7, 3), date(y, 12, 24)):
        if d.weekday() < 5 and d not in hol:
            early.add(d)
    return frozenset(hol), frozenset(early)


def _nyse_year(y: int) -> tuple[frozenset, frozenset]:
    """(holidays, early closes) of one year: the explicit table when it covers the year, else the rules."""
    hit = _nyse_cache.get(y)
    if hit is None:
        if y in _TABLE_YEARS:
            hit = (frozenset(d for d in NYSE_HOLIDAYS if d.year == y),
                   frozenset(d for d in NYSE_EARLY_CLOSES if d.year == y))
        else:
            hit = _nyse_rules(y)
            if y > max(_TABLE_YEARS):
                log.warning("NYSE holiday table does not cover %d; using rule-based holidays (check util.NYSE_HOLIDAYS)", y)
        _nyse_cache[y] = hit
    return hit


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _nyse_year(d.year)[0]


def close_minutes(d: date) -> int:
    """Regular-session close in minutes after midnight ET."""
    return 13 * 60 if d in _nyse_year(d.year)[1] else 16 * 60


def market_session(ts: int | None = None) -> str:
    """'premarket' (04:00-09:30 ET), 'open' (09:30-close), 'afterhours' (close-20:00; 17:00 on early closes), else 'closed'."""
    dt = datetime.fromtimestamp(ts if ts is not None else now_ts(), ET)
    if not is_trading_day(dt.date()):
        return "closed"
    minutes = dt.hour * 60 + dt.minute
    close = close_minutes(dt.date())
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= minutes < close:
        return "open"
    if close <= minutes < close + 4 * 60:
        return "afterhours"
    return "closed"


def last_close_ts(ts: int | None = None) -> int:
    """Unix ts of the most recent regular-session close strictly before `ts`."""
    dt = datetime.fromtimestamp(ts if ts is not None else now_ts(), ET)
    d = dt.date()
    if dt.hour * 60 + dt.minute < close_minutes(d) or not is_trading_day(d):
        d = date.fromordinal(d.toordinal() - 1)
    while not is_trading_day(d):
        d = date.fromordinal(d.toordinal() - 1)
    cm = close_minutes(d)
    return int(datetime(d.year, d.month, d.day, cm // 60, cm % 60, tzinfo=ET).timestamp())


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jload(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def clean_html(text: str, max_len: int = 600) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def setup_logging(level: int = logging.INFO, file: bool = True) -> None:
    """Console (when there is one; pythonw has no stderr) + marketdesk.log rotated at 5 MB (3 backups).
    `file=False` for a short-lived helper process that must not hold the log file open (rotation)."""
    if logging.getLogger().handlers:
        return
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if file:
        handlers.append(logging.handlers.RotatingFileHandler(DATA_DIR / "marketdesk.log", maxBytes=5_000_000,
                                                             backupCount=3, encoding="utf-8", delay=True))
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
