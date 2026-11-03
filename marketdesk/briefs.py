"""Turkish briefs: rule-based ("kural"), local Ollama, and the Claude Code bridge (export / import).

Everything is built from the DB (news, snapshots, calendar). Analysis modules are imported lazily and
every section degrades to a short "veri yok" line, so an empty DB still yields a valid brief.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db
from .config import load_config
from .util import (CLAUDE_IN_DIR, CLAUDE_OUT_DIR, ET, FOMC_DECISIONS, TR, UTC, close_minutes, fmt_et, fmt_tr,
                   is_trading_day, last_close_ts, make_id, market_session, now_ts)

log = logging.getLogger("marketdesk.briefs")

KINDS = ("sabah", "ogle", "aksam", "manual")
SCHEDULED_KINDS = ("sabah", "ogle", "aksam")
KIND_TITLES = {"sabah": "Sabah Brifingi", "ogle": "Öğle Brifingi", "aksam": "Akşam Brifingi",
               "manual": "Anlık Brifing"}
_KIND_ALIASES = {"öğle": "ogle", "ögle": "ogle", "oğle": "ogle", "akşam": "aksam", "anlik": "manual",
                 "anlık": "manual", "manuel": "manual"}
DISCLAIMER = "_Yatırım tavsiyesi değildir. Otomatik kural tabanlı özet._"
OLLAMA_DISCLAIMER = "_Yatırım tavsiyesi değildir. Yerel model (Ollama) tarafından otomatik üretilmiştir; hata içerebilir._"
CLAUDE_DISCLAIMER = "_Yatırım tavsiyesi değildir._"

TR_MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
             "Kasım", "Aralık")
TR_DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
TR_DAYS_SHORT = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")
SESSION_TR = {"premarket": "Piyasa öncesi", "open": "Açık", "afterhours": "Piyasa sonrası", "closed": "Kapalı"}
SENT_TR = {"pos": "olumlu", "neg": "olumsuz", "neu": "nötr"}
DIR_TR = {"long": "Yükseliş", "short": "Düşüş", "mixed": "Karışık"}
IMPACT_TR = {"high": "Yüksek", "medium": "Orta", "low": "Düşük"}
FG_TR = {"extreme fear": "Aşırı korku", "fear": "Korku", "neutral": "Nötr", "greed": "Açgözlülük",
         "extreme greed": "Aşırı açgözlülük"}
GROUP_TR = {"vadeli": "Vadeliler", "endeks": "Endeksler", "faiz": "Faiz", "doviz": "Döviz", "emtia": "Emtia",
            "kripto": "Kripto", "izleme": "İzleme listesi"}
EARN_TIME_TR = {"bmo": "piyasa öncesi (BMO)", "amc": "kapanış sonrası (AMC)", "unknown": "saati belirsiz"}
# Used only when analysis.classify is not importable.
CATEGORY_TR_FALLBACK = {
    "earnings": "Bilanço", "guidance_up": "Beklenti artırımı", "guidance_down": "Beklenti indirimi",
    "analyst_up": "Analist yükseltme", "analyst_down": "Analist düşürme", "mna": "Birleşme/Satın alma",
    "fda": "FDA", "clinical": "Klinik çalışma", "offering": "Hisse satışı", "buyback": "Geri alım",
    "dividend": "Temettü", "insider": "İçeriden işlem", "contract": "Sözleşme", "lawsuit": "Dava",
    "bankruptcy": "İflas", "layoffs": "İşten çıkarma", "macro": "Makro", "fed": "Fed", "ipo": "Halka arz",
    "halt": "İşlem durdurma", "short_report": "Açığa satış raporu", "split": "Hisse bölünmesi",
    "filing": "SEC bildirimi", "product": "Ürün", "crypto": "Kripto", "geopolitics": "Jeopolitik",
}
MACRO_CATS = {"macro", "fed", "geopolitics"}
MARKET_CATS = {"market", "commodities"}
COMPANY_CATS = {"earnings", "guidance_up", "guidance_down", "analyst_up", "analyst_down", "mna", "fda", "clinical",
                "offering", "buyback", "dividend", "insider", "contract", "lawsuit", "bankruptcy", "layoffs", "ipo",
                "halt", "short_report", "split", "reverse_split", "delisting", "filing", "product", "management",
                "activist"}
INDEX_TICKERS = {"SPY", "QQQ", "DIA", "IWM", "VIX", "VOO", "SPX"}
US_CODES = {"", "US", "USA", "USD", "UNITED STATES"}
FED_RE = re.compile(r"\b(fed|fomc|powell|beige book|monetary policy)\b", re.I)
SPEECH_RE = re.compile(r"\b(speaks|speech|testifies|testimony|press conference)\b", re.I)
MARKET_STATE_TR = {"REGULAR": "Açık", "PRE": "Piyasa öncesi", "PREPRE": "Kapalı", "POST": "Piyasa sonrası",
                   "POSTPOST": "Kapalı", "CLOSED": "Kapalı"}
# FOMC rate decisions (14:00 ET): util.FOMC_DECISIONS. Only a hint for weeks whose macro calendar is not loaded yet.
# Mover "neden" scoring: headline verbs that state a price move, and categories that are real catalysts.
UP_RE = re.compile(r"\b(jump(s|ed|ing)?|soar\w*|surg(e|es|ed|ing)|climb\w*|rall(y|ies|ied|ying)|ris(e|es|ing)|rose|"
                   r"gain(s|ed|ing)?|spik(e|es|ed|ing)|rocket\w*|skyrocket\w*|leap\w*|pop(s|ped)?|rebound\w*|"
                   r"(stock|shares|trading|trades|moves?|moving|opens?|closes?|edges?|ticks?|heads?)\s+(\w+\s+)?higher)\b",
                   re.I)
DOWN_RE = re.compile(r"\b(fall(s|ing)?|fell|drop(s|ped|ping)?|plung\w*|tumbl\w*|slid(e|es|ing)?|slump\w*|sink(s|ing)?|"
                     r"sank|crash\w*|div(e|es|ed|ing)|plummet\w*|sell-?off|slip(s|ped|ping)?|tank(s|ed|ing)?|"
                     r"sag(s|ged|ging)?|retreat\w*|"
                     r"(stock|shares|trading|trades|moves?|moving|opens?|closes?|edges?|ticks?|heads?)\s+(\w+\s+)?lower)\b",
                     re.I)
PCT_RE = re.compile(r"\d+(\.\d+)?\s?%")
LAWFIRM_RE = re.compile(r"\b(investigation notice|(investor|shareholder)s? (alert|notice)|class action|notifies investors|"
                        r"reminds (investors|shareholders)|securities (fraud|litigation)|law firm)\b", re.I)
# "X Announces Date/Timing of Q3 Results and Conference Call": scheduling notices, not results.
EARN_DATE_RE = re.compile(r"\b(announces?|sets|schedules?|to (report|release|host|announce))\b.{0,80}\b(date|timing|"
                          r"conference call|webcast)\b|\bto (announce|report|release)\b.{0,80}\bresults\b.{0,12}\bon\b",
                          re.I)
# Previews of a report that has not happened yet ("Earnings Loom", "Earnings Wave", "Reports Due In Days").
EARN_PREVIEW_RE = re.compile(r"\b(earnings (loom|looms|wave|preview|season|week)|(are |is )?days away|set to report|"
                             r"what to (expect|watch))\b", re.I)
WHY_RE = re.compile(r"^\s*(here's\s+)?why\b.*\b(stock|shares)\b", re.I)
# Earnings-call transcripts are long verbatim pages, not news: never a story, a cluster or a brief line.
TRANSCRIPT_RE = re.compile(r"Earnings Call Transcript", re.I)
CATALYST_CATS = {"earnings", "guidance_up", "guidance_down", "analyst_up", "analyst_down", "fda", "clinical", "mna",
                 "offering", "price_move", "contract", "lawsuit", "bankruptcy", "short_report", "halt", "buyback",
                 "layoffs", "reverse_split", "split", "delisting"}
PACK_MAX_BYTES = 45_000  # Claude Code's Read tool refuses files much above ~25k tokens


# ---------------------------------------------------------------- small helpers

def normalize_kind(kind) -> str | None:
    k = str(kind or "").strip().lower()
    k = _KIND_ALIASES.get(k, k)
    return k if k in KINDS else None


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _num(v, d: int = 2) -> str:
    """Turkish number format: 5.812,25."""
    x = _f(v)
    if x is None:
        return "—"
    s = f"{x:,.{d}f}"
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


def _pct(v, d: int = 2) -> str:
    x = _f(v)
    if x is None:
        return "—"
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    return f"{sign}%{_num(abs(x), d)}"


def _price(v, group: str | None = None) -> str:
    x = _f(v)
    if x is None:
        return "—"
    return _num(x, 4 if abs(x) < 1 or (group == "doviz" and abs(x) < 10) else 2)


def _big(v) -> str:
    x = _f(v)
    if x is None or x <= 0:
        return ""
    for div, suf in ((1e12, "Tn"), (1e9, "Mr"), (1e6, "Mn"), (1e3, "bin")):
        if x >= div:
            return f"{_num(x / div, 1)} {suf}"
    return _num(x, 0)


_NUMTXT_RE = re.compile(r"^([+-]?)(\d+)(?:\.(\d+))?\s*(%|[KMBT])?$", re.I)
_NUMTXT_SUFFIX = {"K": "bin", "M": "Mn", "B": "Mr", "T": "Tn"}


def _numtxt(v, max_len: int = 20) -> str:
    """Calendar / EPS value ('1.27', '4.7%', '-0.3%', '228K', '1.39M') in Turkish format; anything else stays raw."""
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?\s*(%|[KMBT])?", s, re.I):
        s = s.replace(",", "")  # US thousands separators
    m = _NUMTXT_RE.match(s)
    if not m:
        return _md(s, max_len)
    sign, whole, frac, suf = m.groups()
    body = _num(float(f"{whole}.{frac or 0}"), len(frac or ""))
    if suf == "%":
        return f"{sign}%{body}"
    return f"{sign}{body}" + (f" {_NUMTXT_SUFFIX[suf.upper()]}" if suf else "")


def _money(v) -> float | None:
    """'$3,456,789,012' / '1.2B' / 1.2e9 -> float."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return _f(v)
    s = str(v).strip().replace("$", "").replace(",", "").upper()
    mult = 1.0
    if s and s[-1] in "TBMK":
        mult = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}[s[-1]]
        s = s[:-1]
    x = _f(s)
    return x * mult if x is not None else None


def _md(text, max_len: int = 180) -> str:
    """One-line, Markdown-safe text (no stray emphasis/link syntax)."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.replace("[", "(").replace("]", ")").replace("*", "").replace("`", "'").replace("_", " ")
    if len(t) > max_len:
        t = t[:max_len - 1].rstrip() + "…"
    return t


def _url(u) -> str:
    u = str(u or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _link(u, label: str = "link") -> str:
    u = _url(u)
    return f"[{label}]({u})" if u else ""


def _is_gnews(u) -> bool:
    """Google News redirect links (long opaque base64 URLs)."""
    return "news.google.com/" in str(u or "").lower()


def _src_name(it: dict) -> str:
    """Who actually wrote an item: the relayed publisher (Google News / Yahoo / watchlist feeds put it in
    extra.publisher, like the UI's pubKey) when it looks like a name, else the collector's source name."""
    name = str(it.get("source_name") or it.get("source") or "?")
    x = it.get("extra")
    pub = re.sub(r"\s+", " ", str(x.get("publisher") or "")).strip() if isinstance(x, dict) else ""
    if pub and len(pub) <= 60 and "/" not in pub and not re.match(r"^(https?:|www\.)", pub, re.I):
        # Regional editions are one newsroom: 'Yahoo! Finance Canada' / 'Yahoo Finance UK' -> 'Yahoo Finance'.
        return _PUB_EDITION_RE.sub("", pub.replace("Yahoo!", "Yahoo")).strip() or pub
    return name


_PUB_EDITION_RE = re.compile(r"\s+(canada|uk|australia|singapore|india|south africa|france|deutschland|españa|"
                             r"italia|new zealand|nz|hong kong|hk|japan|media)$", re.I)


def _src_key(name: str) -> str:
    k = re.sub(r"[^\w]+", " ", str(name or "").lower()).strip()
    return k[4:] if k.startswith("the ") else k


def _by_publisher(items: list[dict]) -> list[tuple[str, dict]]:
    """[(publisher name, first item)] in input order; 'The Motley Fool' == 'Motley Fool', 'CNBC' == 'CNBC Manşetler'."""
    keys: list[str] = []
    out: list[tuple[str, dict]] = []
    for it in items:
        name = _src_name(it)
        k = _src_key(name)
        if any(k == o or k.startswith(o + " ") or o.startswith(k + " ") for o in keys):
            continue
        keys.append(k)
        out.append((name, it))
    return out


def _et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, ET)


def _at_et(d: date, h: int = 0, m: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())


def _next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def _prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def tr_date(d) -> str:
    """'26 Ekim 2026 Pazartesi' for a date, datetime or unix ts (ET date). Locale independent."""
    if isinstance(d, datetime):
        d = d.date()
    elif not isinstance(d, date):
        d = _et(int(d)).date()
    return f"{d.day} {TR_MONTHS[d.month - 1]} {d.year} {TR_DAYS[d.weekday()]}"


def _rel_day(d: date, today: date) -> str:
    if d == today:
        return "bugün"
    if d == today + timedelta(days=1):
        return "yarın"
    return TR_DAYS[d.weekday()]


def _short_day(d: date) -> str:
    return f"{TR_DAYS[d.weekday()]} {d.day} {TR_MONTHS[d.month - 1]}"


def _clock(ts: int, now: int) -> str:
    """TR 'HH:MM', prefixed with a short weekday when not on the same TR date as `now`."""
    t, n = datetime.fromtimestamp(ts, TR), datetime.fromtimestamp(now, TR)
    return t.strftime("%H:%M") if t.date() == n.date() else f"{TR_DAYS_SHORT[t.weekday()]} {t.strftime('%H:%M')}"


def _hm(ts: int, now: int | None = None) -> str:
    """'HH:MM TR (HH:MM ET)'. With `now`, a timestamp on another TR date carries its date ('24.10 22:25 TR ...'),
    so an older snapshot is not read as a time later than the brief."""
    if now is not None and datetime.fromtimestamp(ts, TR).date() != datetime.fromtimestamp(now, TR).date():
        return f"{fmt_tr(ts, '%d.%m %H:%M')} TR ({fmt_et(ts, '%d.%m %H:%M')} ET)"
    return f"{fmt_tr(ts, '%H:%M')} TR ({fmt_et(ts, '%H:%M')} ET)"


def _when(ts: int) -> str:
    """'Cuma 23:00 TR (16:00 ET)' — TR weekday of the TR time."""
    return f"{TR_DAYS[datetime.fromtimestamp(ts, TR).weekday()]} {_hm(ts)}"


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return "1 dk'dan az"
    if sec < 3600:
        return f"{sec // 60} dk"
    if sec < 86400:
        h, m = divmod(sec // 60, 60)
        return f"{h} sa {m} dk" if m else f"{h} sa"
    return f"{sec // 86400} gün"


def brief_title(kind: str, d) -> str:
    return f"{KIND_TITLES.get(kind, 'Brifing')} — {tr_date(d)}"


def _category_labels() -> dict:
    labels = dict(CATEGORY_TR_FALLBACK)
    try:
        from .analysis.classify import CATEGORIES
        for k, v in CATEGORIES.items():
            if isinstance(v, dict) and v.get("label"):
                labels[k] = str(v["label"])
    except Exception:
        pass
    return labels


def _snap(key: str) -> tuple[dict | None, int]:
    try:
        s = db.get_snapshot(key)
    except Exception:
        log.exception("snapshot %s unreadable", key)
        return None, 0
    if not s or not isinstance(s[0], dict):
        return None, 0
    data, updated = s
    return data, int(_f(data.get("ts")) or updated or 0)


def _meta(it: dict, key: str):
    """A value stored in an item's `extra` (analysis meta such as earnings_outcome or subject_tickers)."""
    x = it.get("extra")
    return x.get(key) if isinstance(x, dict) else None


def _sent_label(x) -> str:
    x = _f(x) or 0.0
    return "pos" if x > 0.15 else ("neg" if x < -0.15 else "neu")


# ---------------------------------------------------------------- time frame

def _auto_layout(now: int) -> str:
    s = market_session(now)
    if s == "premarket":
        return "sabah"
    if s == "open":
        return "ogle"
    if s == "afterhours":
        return "aksam"
    et = _et(now)
    if is_trading_day(et.date()) and et.hour * 60 + et.minute >= close_minutes(et.date()):
        return "aksam"
    return "sabah"  # overnight / weekend: prepare for the next session


def _frame(kind: str, now: int) -> dict:
    et_now = _et(now)
    today = et_now.date()
    minutes = et_now.hour * 60 + et_now.minute
    trading = is_trading_day(today)
    layout = kind if kind in SCHEDULED_KINDS else _auto_layout(now)
    if layout == "sabah":
        day = today if trading and minutes < close_minutes(today) else _next_trading_day(today)
        window_start = last_close_ts(now)
    elif layout == "ogle":
        day = today if trading else _prev_trading_day(today)
        open_ts = _at_et(day, 9, 30)
        window_start = open_ts if now >= open_ts else last_close_ts(now)
    else:
        day = today if trading and minutes >= 9 * 60 + 30 else _prev_trading_day(today)
        window_start = last_close_ts(_at_et(day, 9, 30))
    cm = close_minutes(day)  # 13:00 ET on early-close days
    return {"layout": layout, "day": day, "today": today, "next_day": _next_trading_day(day),
            "window_start": window_start, "open_ts": _at_et(day, 9, 30), "close_ts": _at_et(day, cm // 60, cm % 60),
            "title_day": today if kind == "manual" else day}


def default_kind(now: int | None = None) -> str:
    """Kind for 'right now' (export CLI default): sabah before 10:30 ET, ogle until the close (16:00 ET, 13:00 on
    early-close days), then aksam; non-trading days: manual."""
    now = int(now or now_ts())
    et = _et(now)
    if not is_trading_day(et.date()):
        return "manual"
    minutes = et.hour * 60 + et.minute
    if minutes < 10 * 60 + 30:
        return "sabah"
    if minutes < close_minutes(et.date()):
        return "ogle"
    return "aksam"


# ---------------------------------------------------------------- stories

_STOP = {"the", "and", "for", "with", "from", "after", "says", "said", "its", "new", "stock", "stocks", "shares",
         "inc", "corp", "ltd", "are", "has", "have", "will", "over", "into", "amid", "than", "this", "that", "what",
         "why", "how", "you", "your", "here", "today",
         # press-release boilerplate ("X Reports Third Quarter 2026 Financial Results")
         "reports", "report", "results", "quarter", "first", "second", "third", "fourth", "financial", "fiscal",
         "announces", "announce", "full", "year", "date", "release", "conference", "call", "webcast", "update",
         "business", "company", "2025", "2026", "2027"}
_NAME_STOP = {"inc", "corp", "corporation", "company", "holdings", "holding", "group", "ltd", "limited", "plc", "the",
              "class", "common", "stock", "sponsored", "adr", "ads", "technologies", "technology", "international",
              "incorporated", "trust", "fund", "etf", "global", "american"}


def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2 and w not in _STOP}


def _merge_similar(groups: dict[str, list], threshold: float = 0.6) -> dict[str, list]:
    """Merge near-identical headlines within 24 h (syndicated copies; fallback when clustering has not run).
    Groups tagged with disjoint tickers are never merged."""
    keys = sorted(groups, key=lambda k: -max(_f(n.get("importance")) or 0 for n in groups[k]))[:300]
    sig = {k: [(t, n.get("published_at") or 0) for n in groups[k][:4] if len(t := _tokens(n.get("title"))) >= 3]
           for k in keys}
    tick = {k: {t for n in groups[k] for t in (n.get("tickers") or [])} - INDEX_TICKERS for k in keys}

    def similar(a: str, b: str) -> bool:
        if tick[a] and tick[b] and not tick[a] & tick[b]:
            return False
        return any(abs(pa - pb) < 86400 and len(ta & tb) / len(ta | tb) >= threshold
                   for ta, pa in sig[a] for tb, pb in sig[b])

    parent: dict[str, str] = {}
    for i, a in enumerate(keys):
        if a in parent or not sig[a]:
            continue
        for b in keys[i + 1:]:
            if b not in parent and sig[b] and similar(a, b):
                parent[b] = a
    for b, a in parent.items():
        groups[a].extend(groups.pop(b))
    return groups


def _story(items: list[dict], title: str | None = None, key: str | None = None) -> dict:
    items = sorted(items, key=lambda n: n.get("published_at") or 0)
    lead = max(items, key=lambda n: (_f(n.get("importance")) or 0, -(n.get("published_at") or 0)))
    # Prefer a direct publisher link over a Google News redirect (long opaque URLs).
    link_item = lead if lead.get("url") and not _is_gnews(lead.get("url")) else next(
        (n for n in sorted(items, key=lambda n: -(_f(n.get("importance")) or 0))
         if n.get("url") and not _is_gnews(n.get("url"))),
        lead if lead.get("url") else next((n for n in items if n.get("url")), lead))
    sents = [_f(n.get("sentiment")) or 0.0 for n in items]
    # One entry per publisher (syndicated copies count once); newsrooms/press before social posts.
    per = _by_publisher(items)
    sources = [name for name, it in sorted(per, key=lambda x: x[1].get("source_type") == "social")]
    tcount = Counter(t for n in items for t in dict.fromkeys(n.get("tickers") or []))
    tickers = sorted(tcount, key=lambda t: (t in INDEX_TICKERS, -tcount[t]))
    cats = list(dict.fromkeys(c for n in items for c in (n.get("categories") or [])))
    importance = max(_f(n.get("importance")) or 0 for n in items)
    title = title or lead.get("title") or ""
    # Ranking: importance, nudged up by cross-source breadth and down for opinion/list pieces
    # ("Why Is X Stock Trading Lower…" explainers are tagged opinion but are exactly what a trader needs).
    opinion = "opinion" in cats and not WHY_RE.search(title)
    rank = importance + min(15, 5 * (len(sources) - 1)) - (10 if opinion else 0)
    return {
        "id": key or lead.get("cluster_id") or lead.get("id"),
        "title": title,
        "url": link_item.get("url") or "",
        "summary": lead.get("summary") or "",
        "sources": sources,
        "source_count": len(sources),
        "tickers": tickers,
        "ticker_share": {t: tcount[t] / len(items) for t in tickers},
        "categories": cats,
        "importance": importance,
        "rank": rank,
        "sentiment": sum(sents) / len(sents),
        "first_seen": items[0].get("published_at") or 0,
        "last_seen": items[-1].get("published_at") or 0,
        "lead_ts": lead.get("published_at") or 0,
        "social_only": all((n.get("source_type") == "social") for n in items),
        "items": items,
    }


def _group_stories(news: list[dict]) -> list[dict]:
    groups: dict[str, list] = {}
    for n in news:
        groups.setdefault(n.get("cluster_id") or n.get("id") or make_id(n.get("title")), []).append(n)
    if news:  # analysis clusters exist -> only merge near-identical copies on top of them
        groups = _merge_similar(groups, 0.8 if any(n.get("cluster_id") for n in news) else 0.6)
    stories = [_story(v) for v in groups.values() if v]
    stories.sort(key=lambda s: (s["rank"], s["last_seen"]), reverse=True)
    return stories


def _clusters(ctx_start: int, window_start: int, now: int, stories: list[dict]) -> list[dict]:
    """Multi-source stories: analysis.cluster.list_clusters when available, else grouped DB rows.
    Stories still active inside the brief window come first."""
    out: list[dict] = []
    hours = max(1, min(96, math.ceil((now_ts() - ctx_start) / 3600)))
    try:
        from .analysis.cluster import list_clusters
        for c in list_clusters(since_hours=hours, min_sources=2, limit=40) or []:
            if TRANSCRIPT_RE.search(str(c.get("title") or "")):
                continue
            items = [n for n in (c.get("items") or []) if isinstance(n, dict) and (n.get("published_at") or 0) <= now + 300
                     and not TRANSCRIPT_RE.search(str(n.get("title") or ""))]
            if not items:
                continue
            s = _story(items, title=c.get("title"), key=c.get("cluster_id"))
            if s["source_count"] >= 2:
                out.append(s)
    except Exception as e:
        log.debug("list_clusters unavailable: %s", e)
    if not out:
        out = [s for s in stories if s["source_count"] >= 2]
    out = [s for s in out if not s["social_only"] and not TRANSCRIPT_RE.search(s.get("title") or "")]
    # Distinct publishers count, but capped: a press release copied by four sites must not outrank real news.
    out.sort(key=lambda s: (s["last_seen"] >= window_start, s["importance"] + 5 * min(s["source_count"], 4)),
             reverse=True)
    return out


def _is_general(s: dict) -> bool:
    """Market-wide story (macro/Fed/geopolitics/market) vs. company story."""
    cats, real = set(s["categories"]), set(s["tickers"]) - INDEX_TICKERS
    if cats & MACRO_CATS:
        return True
    if cats & COMPANY_CATS:
        return False
    return bool(cats & MARKET_CATS) or not real


# ---------------------------------------------------------------- data pack

MOVER_KEYS = ("gainers", "losers", "actives", "small_caps", "premarket", "postmarket")


def _merge_movers(*snaps: tuple[dict | None, int]) -> dict:
    """Screener lists (regular session) plus the extended-hours gappers ('premarket' / 'postmarket': change_pct is
    the extended-hours move, regular_change_pct the regular session's)."""
    out: dict = {k: [] for k in MOVER_KEYS}
    out.update({"ts": 0, "ext_ts": 0, "sources": []})
    for snap, ts in snaps:
        if not isinstance(snap, dict):
            continue
        added = False
        if snap.get("premarket") or snap.get("postmarket"):
            out["ext_ts"] = max(out["ext_ts"], int(_f(snap.get("ext_ts")) or ts or 0))
        for k in MOVER_KEYS:
            seen = {str(r.get("symbol")).upper() for r in out[k]}
            for r in snap.get(k) or []:
                if not isinstance(r, dict) or not r.get("symbol"):
                    continue
                sym = str(r["symbol"]).upper()
                if sym in seen:
                    continue
                seen.add(sym)
                out[k].append(dict(r, symbol=sym))
                added = True
        if added:
            out["sources"].append(str(snap.get("source") or "?").capitalize())
            out["ts"] = max(out["ts"], ts)
    return out


def _event_day(e: dict) -> date:
    x = e.get("extra") or {}
    ds = x.get("date") if isinstance(x, dict) else None
    if isinstance(ds, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        try:
            return date.fromisoformat(ds)
        except ValueError:
            pass
    ts = int(e.get("ts") or 0)
    if e.get("all_day") and ts % 86400 == 0:
        return datetime.fromtimestamp(ts, UTC).date()
    return _et(ts).date()


def _events_on(d: date) -> list[dict]:
    try:
        evs = db.query_events(_at_et(d) - 14 * 3600, _at_et(d + timedelta(days=1)) + 14 * 3600)
    except Exception:
        log.exception("query_events failed")
        return []
    return [e for e in evs if _event_day(e) == d]


def _macro_loaded(d: date) -> bool:
    """Whether the macro calendar of d's ForexFactory week (Sunday-Saturday) is in the DB at all. FF publishes one
    week at a time (the next-week file is often missing), so 'no rows' usually means 'not loaded', not 'no events'."""
    start = d - timedelta(days=(d.weekday() + 1) % 7)
    end = start + timedelta(days=7)
    try:
        evs = db.query_events(_at_et(start) - 14 * 3600, _at_et(end) + 14 * 3600, kind="macro")
    except Exception:
        log.exception("query_events failed")
        return True  # unknown: do not claim a missing calendar
    return any(start <= _event_day(e) < end for e in evs)


def _fomc_hint(d: date, days: int = 7) -> str:
    """'- _Fed takvimine göre FOMC faiz kararı ..._' when a known decision day falls in [d, d+days)."""
    nxt = next((x for x in FOMC_DECISIONS if d <= x < d + timedelta(days=days)), None)
    if not nxt:
        return ""
    ts = _at_et(nxt, 14, 0)
    return (f"- _Fed takvimine göre **FOMC faiz kararı {_short_day(nxt)} {fmt_tr(ts, '%H:%M')} TR "
            f"({fmt_et(ts, '%H:%M')} ET)** — sabit listeden; takvim verisi gelince teyit edin._")


def _earn_time(e: dict) -> str:
    t = str((e.get("extra") or {}).get("time") or "").lower()
    if t == "bmo" or "pre" in t or "before" in t:
        return "bmo"
    if t == "amc" or "after" in t or "post" in t:
        return "amc"
    return "unknown"


def _macro_relevant(e: dict) -> bool:
    if e.get("kind") in ("earnings", "ipo"):  # IPO rows: see _sec_ipo (their price is an offer price, not a forecast)
        return False
    country = str(e.get("country") or "").upper()
    imp = str(e.get("impact") or "").lower()
    if country in US_CODES:
        title = e.get("title") or ""
        return imp in ("high", "medium") or bool(FED_RE.search(title) or SPEECH_RE.search(title))
    return imp == "high"


def _watchlist(cfg: dict) -> list[str]:
    wl = cfg.get("watchlist") or []
    if isinstance(wl, str):
        wl = wl.replace(";", ",").split(",")
    return list(dict.fromkeys(str(s).strip().upper().lstrip("$") for s in wl if str(s).strip()))


def build_pack(kind: str, cfg: dict | None = None, now: int | None = None) -> dict:
    """Collect everything a brief needs from the DB. Never raises for missing data."""
    cfg = cfg or load_config()
    now = int(now or now_ts())
    kind = normalize_kind(kind) or "manual"
    fr = _frame(kind, now)
    # Context window: at least since the previous close (midday briefs still see the overnight stories).
    ctx_start = min(fr["window_start"], last_close_ts(fr["open_ts"]))
    try:
        news_ctx = db.recent_news(ctx_start)
    except Exception:
        log.exception("recent_news failed")
        news_ctx = []
    news_ctx = [n for n in news_ctx if (n.get("published_at") or 0) <= now + 300
                and not TRANSCRIPT_RE.search(str(n.get("title") or ""))]
    news = [n for n in news_ctx if (n.get("published_at") or 0) >= fr["window_start"]]
    all_stories = _group_stories(news)
    ctx_stories = all_stories if ctx_start == fr["window_start"] else _group_stories(news_ctx)
    stories = [s for s in all_stories if not s["social_only"]]
    ticker_stories: dict[str, list[dict]] = {}
    for s in ctx_stories:
        for t in s["tickers"]:
            ticker_stories.setdefault(t, []).append(s)
    for t, lst in ticker_stories.items():  # media before social, stories about t before passing mentions
        lst.sort(key=lambda s, t=t: (s["social_only"], s["ticker_share"].get(t, 0) < 0.5, -s["rank"]))

    movers = _merge_movers(_snap("movers"), _snap("movers_alpaca"))
    # Extended-hours lists must belong to this brief: today's pre-market (from 04:00 ET of the session day) and
    # the after-hours of the most recent close.
    if not _at_et(fr["day"], 4, 0) <= movers["ext_ts"] <= min(fr["close_ts"], now + 300):
        movers["premarket"] = []
    if not last_close_ts(now) <= movers["ext_ts"] <= now + 300:
        movers["postmarket"] = []
    # Mover "neden" lookups: in the morning layout the screener lists are the previous session's, so their news
    # starts at the close before that session.
    why_start = last_close_ts(fr["window_start"] - 60) if fr["layout"] == "sabah" else ctx_start
    why_stories, news_why = ticker_stories, []
    if why_start < ctx_start:
        syms = {str(r.get("symbol")).upper() for k in MOVER_KEYS for r in movers[k]}
        try:
            news_why = [n for n in db.recent_news(why_start, limit=20000)
                        if (n.get("published_at") or 0) <= now + 300 and syms & set(n.get("tickers") or [])]
        except Exception:
            log.exception("recent_news failed")
        why_stories = {}
        for s in _group_stories(news_why):
            for t in s["tickers"]:
                why_stories.setdefault(t, []).append(s)
        for t, lst in why_stories.items():
            lst.sort(key=lambda s, t=t: (s["social_only"], s["ticker_share"].get(t, 0) < 0.5, -s["rank"]))
    clusters = _clusters(ctx_start, fr["window_start"], now, ctx_stories)
    # Google News redirect URL -> news id (the Claude pack prints 'ref:<id>' instead of 300-700 char links).
    # Cluster items can fall outside the context window, so they are included too.
    url_ids = {_url(n.get("url")): n.get("id") for n in news_ctx + news_why + [it for c in clusters for it in c["items"]]
               if _is_gnews(n.get("url")) and n.get("id")}

    quotes_snap, quotes_ts = _snap("quotes")
    quotes = [q for q in (quotes_snap or {}).get("items") or [] if isinstance(q, dict) and q.get("symbol")]
    fear_greed, fg_ts = _snap("fear_greed")
    social, social_ts = _snap("social")
    opps_snap, opps_ts = _snap("opportunities")
    opps = [o for o in (opps_snap or {}).get("items") or [] if isinstance(o, dict) and o.get("ticker")]
    opps.sort(key=lambda o: _f(o.get("score")) or 0, reverse=True)

    ev_day, ev_next = _events_on(fr["day"]), _events_on(fr["next_day"])
    # Morning layout: the previous session's reported earnings (EPS actual / surprise from the Nasdaq calendar).
    prev_day = _prev_trading_day(fr["day"])
    earn_prev: list[dict] = []
    if fr["layout"] == "sabah":
        # After-close reports are dated the day they are published, but the market reacts on the next session: the
        # Monday brief must include Thursday's after-close results (unknown-time rows included), plus prev_day's
        # pre-market and unknown-time reports. One row per ticker, prev_day first.
        before = _prev_trading_day(prev_day)
        cands = [e for e in _events_on(prev_day) if e.get("kind") == "earnings" and _earn_time(e) in ("bmo", "unknown")]
        cands += [e for e in _events_on(before) if e.get("kind") == "earnings" and _earn_time(e) in ("amc", "unknown")]
        seen: set[str] = set()
        for e in cands:
            sym = (e.get("ticker") or "").upper()
            if sym and sym not in seen:
                seen.add(sym)
                earn_prev.append(e)
    pack = {
        "kind": kind, "now": now, "cfg": cfg, "watchlist": _watchlist(cfg), "session": market_session(now),
        "title": brief_title(kind, fr["title_day"]), "ctx_start": ctx_start, **fr,
        "news": news, "stories": stories, "ticker_stories": ticker_stories,
        "why_stories": why_stories, "why_start": why_start, "url_ids": url_ids,
        "social_posts": [s for s in all_stories if s["social_only"]],
        "clusters": clusters,
        "quotes": quotes, "quotes_ts": quotes_ts,
        "fear_greed": fear_greed, "fg_ts": fg_ts,
        "movers": movers,
        "social": social or {}, "social_ts": social_ts,
        "opps": opps, "opps_ts": opps_ts,
        "macro_day": [e for e in ev_day if _macro_relevant(e)],
        "earn_day": [e for e in ev_day if e.get("kind") == "earnings"],
        "macro_next": [e for e in ev_next if _macro_relevant(e)],
        "earn_next": [e for e in ev_next if e.get("kind") == "earnings"],
        "prev_day": prev_day,
        "earn_prev": earn_prev,
        "ipos": [e for e in ev_day + ev_next if e.get("kind") == "ipo"],
        "macro_day_loaded": any(e.get("kind") == "macro" for e in ev_day) or _macro_loaded(fr["day"]),
        "macro_next_loaded": any(e.get("kind") == "macro" for e in ev_next) or _macro_loaded(fr["next_day"]),
        "labels": _category_labels(),
    }
    return pack


# ---------------------------------------------------------------- line renderers

def _tickers_md(tickers: list[str], n: int = 3) -> str:
    if not tickers:
        return ""
    ordered = sorted(tickers, key=lambda t: t in INDEX_TICKERS)
    shown = ", ".join(_md(t, 12) for t in ordered[:n])
    more = f" +{len(ordered) - n}" if len(ordered) > n else ""
    return f"**{shown}**{more} · "


def _sources_md(sources: list[str], n: int = 2) -> str:
    names = ", ".join(_md(s, 40) for s in sources[:n]) or "?"
    extra = len(sources) - n
    return f"*{names}{f' (+{extra})' if extra > 0 else ''}*"


def _story_line(p: dict, s: dict, tags: bool = True, note: str = "") -> str:
    line = f"- {_tickers_md(s['tickers'])}{_md(s['title'], 160)}{f' _({note})_' if note else ''} — {_sources_md(s['sources'])}"
    link = _link(s["url"])
    if link:
        line += " " + link
    if tags and s["categories"]:
        line += " · " + ", ".join(_md(p["labels"].get(c, c), 30) for c in s["categories"][:2])
    return line


def _mark_shown(p: dict, stories: list[dict]) -> None:
    p.setdefault("_shown", set()).update(n.get("id") for s in stories for n in s["items"] if n.get("id"))


def _is_shown(p: dict, s: dict) -> bool:
    shown = p.get("_shown") or set()
    return any(n.get("id") in shown for n in s["items"])


def _primary(s: dict) -> str | None:
    """Main company ticker of a company story (None for market-wide stories)."""
    if _is_general(s):
        return None
    return next((t for t in s["tickers"] if t not in INDEX_TICKERS), None)


def _collapse(stories: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Group company stories on the same primary ticker: [(lead story, other stories)] in rank order."""
    out: list[tuple[dict, list[dict]]] = []
    idx: dict[str, int] = {}
    for s in stories:
        k = _primary(s)
        if k and k in idx:
            out[idx[k]][1].append(s)
            continue
        if k:
            idx[k] = len(out)
        out.append((s, []))
    return out


def _quote_short(q: dict) -> str:
    name = _md(q.get("name") or q.get("symbol"), 30)
    price, chg = _f(q.get("price")), _f(q.get("change_pct"))
    if q.get("group") == "faiz" and price is not None:
        ch = _f(q.get("change"))
        bp = "" if ch is None else f" ({'+' if ch >= 0 else '-'}{_num(abs(ch) * (100 if price < 20 else 10), 1)} bp)"
        return f"{name} %{_num(price if price < 20 else price / 10, 2)}{bp}"
    return f"{name} {_price(price, q.get('group'))} ({_pct(chg)})"


def _qmap(p: dict) -> dict[str, dict]:
    return {str(q.get("symbol")).upper(): q for q in p["quotes"]}


def _tone(p: dict, main_group: str) -> tuple[str, list[str]]:
    qs = [q for q in p["quotes"] if q.get("group") == main_group and q.get("symbol") != "^VIX"
          and _f(q.get("change_pct")) is not None]
    avg = sum(_f(q["change_pct"]) for q in qs) / len(qs) if qs else None
    vix = _qmap(p).get("^VIX")
    fg = p["fear_greed"] or {}
    cnt = Counter(n.get("sentiment_label") or "neu" for n in p["news"])
    bits, score, inputs = [], 0.0, 0
    vp = vc = None
    if avg is not None:
        bits.append(f"{GROUP_TR.get(main_group, main_group).lower()} ortalama {_pct(avg)}")
        score += avg / 0.75  # 'strong' needs roughly a 1%+ average index move on its own
        inputs += 1
    if vix and _f(vix.get("price")) is not None:
        vp, vc = _f(vix.get("price")), _f(vix.get("change_pct"))
        note = "yüksek oynaklık" if vp >= 25 else ("sakin" if vp <= 14 else "")
        bits.append(f"VIX {_num(vp)} ({_pct(vc)}{', ' + note if note else ''})")
        if vc is not None:
            score -= vc / 10
        if vp >= 25:
            score -= 0.5
        inputs += 1
    fg_score = _f(fg.get("score"))
    if fg_score is not None:
        rating = FG_TR.get(str(fg.get("rating") or "").lower(), str(fg.get("rating") or ""))
        extra = []
        if _f(fg.get("previous_close")) is not None:
            extra.append(f"dün {int(round(_f(fg['previous_close'])))}")
        if _f(fg.get("week_ago")) is not None:
            extra.append(f"geçen hafta {int(round(_f(fg['week_ago'])))}")
        bits.append(f"Korku & Açgözlülük {int(round(fg_score))} ({rating}{'; ' + ', '.join(extra) if extra else ''})")
        # The level persists for weeks; cap it so it cannot push a quiet day into 'strong'.
        score += max(-0.25, min(0.25, (fg_score - 50) / 100))
        inputs += 1
    if p["news"]:
        bits.append(f"haber akışı {cnt['pos']} olumlu / {cnt['neg']} olumsuz / {cnt['neu']} nötr")
        score += 0.5 * (cnt["pos"] - cnt["neg"]) / (cnt["pos"] + cnt["neg"] + 5)
        inputs += 1
    risk_off = vp is not None and (vp >= 22 or (vc is not None and vc >= 8))
    risk_on = vc is not None and vc <= -8
    if not inputs:
        label = "Belirsiz (veri yok)"
    elif score >= 1.5:
        label = "Güçlü pozitif" + (" (risk iştahı yüksek)" if risk_on else "")
    elif score >= 0.5:
        label = "Pozitif"
    elif score <= -1.5:
        label = "Güçlü negatif" + (" (riskten kaçış)" if risk_off else "")
    elif score <= -0.5:
        label = "Negatif"
    else:
        label = "Karışık / yatay"
    return label, bits


def _per_source(s: dict) -> list[dict]:
    """Earliest item per publisher (time order)."""
    return [it for _, it in _by_publisher(s["items"])]


def _news_first(items: list[dict]) -> list[dict]:
    """Newsrooms / press releases before social posts (stable)."""
    return sorted(items, key=lambda it: it.get("source_type") == "social")


def _agreement(items: list[dict]) -> str:
    c = Counter(it.get("sentiment_label") or "neu" for it in items)
    pos, neg, neu = c["pos"], c["neg"], c["neu"]
    tally = f"{pos} olumlu / {neg} olumsuz / {neu} nötr"
    if pos and neg:
        return f"**Ayrışma:** kaynaklar farklı yorumluyor ({tally})"
    if pos and pos >= neg + neu:
        return f"**Uzlaşı: olumlu** ({tally})"
    if neg and neg >= pos + neu:
        return f"**Uzlaşı: olumsuz** ({tally})"
    return f"**Ağırlıklı olarak nötr** ({tally})"


def _event_line(e: dict) -> str:
    x = e.get("extra") or {}
    title = _md(e.get("title"), 80)
    tr = _md(x.get("title_tr"), 80) if isinstance(x, dict) else ""
    name = f"{tr} _({title})_" if tr and tr.lower() != title.lower() else (tr or title)
    parts = ["Tüm gün" if e.get("all_day") else f"**{fmt_tr(e['ts'], '%H:%M')} TR** ({fmt_et(e['ts'], '%H:%M')} ET)", name]
    country = str(e.get("country") or "").upper()
    if country not in US_CODES:
        parts.append(country)
    imp = IMPACT_TR.get(str(e.get("impact") or "").lower())
    if imp:
        parts.append(f"{imp} etki")
    if FED_RE.search(e.get("title") or ""):
        parts.append("Fed")
    elif country in US_CODES and SPEECH_RE.search(e.get("title") or ""):
        parts.append("Konuşma")
    if e.get("forecast"):
        parts.append(f"Beklenti {_numtxt(e['forecast'])}")
    if e.get("previous"):
        parts.append(f"Önceki {_numtxt(e['previous'])}")
    if e.get("actual"):
        parts.append(f"**Gerçekleşen {_numtxt(e['actual'])}**")
    return "- " + " · ".join(parts)


def _earn_rank(e: dict, wl: list[str]):
    return (e.get("ticker") in wl, _money((e.get("extra") or {}).get("market_cap")) or 0)


def _earn_line(p: dict, e: dict) -> str:
    x = e.get("extra") or {}
    sym = (e.get("ticker") or "").upper()
    parts = [f"**{_md(sym, 12)}**"]
    comp = x.get("company") or e.get("title")
    if comp:
        parts.append(_md(comp, 40))
    if x.get("eps_forecast") not in (None, ""):
        parts.append(f"EPS beklentisi {_numtxt(x['eps_forecast'], 12)}")
    if x.get("eps_actual") not in (None, ""):
        sp = _f(x.get("surprise_pct"))
        parts.append(f"gerçekleşen {_numtxt(x['eps_actual'], 12)}" + (f" (sürpriz {_pct(sp, 1)})" if sp is not None else ""))
    mc = _big(_money(x.get("market_cap")))
    if mc:
        parts.append(f"piyasa değeri {mc} $")
    if sym in p["watchlist"]:
        parts.append("**izleme listesinde**")
    n = len(p["ticker_stories"].get(sym, []))
    if n:
        parts.append(f"{n} haber")
    return "- " + " · ".join(parts)


# ---------------------------------------------------------------- sections

def _publisher(it: dict) -> str:
    """Newsroom behind an item (canonical name), so one outlet relayed by several collectors counts once."""
    try:
        from .collectors.rss import canonical_publisher
        return canonical_publisher(it) or _src_name(it)
    except Exception:
        return _src_name(it)


def _publisher_counts(items: list[dict]) -> Counter:
    return Counter(_publisher(n) for n in items)


def _sec_header(p: dict) -> list[str]:
    srcs = _publisher_counts(p["news"])
    lines = [f"_{fmt_tr(p['now'], '%d.%m.%Y %H:%M')} TR ({fmt_et(p['now'], '%H:%M')} ET) · Oturum: "
             f"{SESSION_TR.get(p['session'], p['session'])} · Kapsam: {_when(p['window_start'])} → şimdi · "
             f"{len(p['news'])} haber, {len(srcs)} kaynak_"]
    if srcs:
        lines.append("_Kaynak dağılımı: " + " · ".join(f"{_md(k, 45)} {v}" for k, v in srcs.most_common(8)) + "_")
    else:
        lines.append("_Bu aralıkta haber yok — kaynaklar henüz çalışmamış olabilir (Kaynaklar sayfasını kontrol edin)._")
    return lines


def _sec_tone(p: dict, heading: str, groups: list[str], index_note: str = "") -> list[str]:
    out = [heading]
    label, bits = _tone(p, groups[0])
    out.append(f"**Ton: {label}**" + (" — " + " · ".join(bits) if bits else ""))
    out.append("")
    any_q = False
    for g in groups:
        items = [q for q in p["quotes"] if q.get("group") == g]
        if not items:
            continue
        any_q = True
        name = GROUP_TR.get(g, g) + (index_note if g == "endeks" else "")
        out.append(f"- **{name}:** " + " · ".join(_quote_short(q) for q in items[:8]))
    if not any_q:
        out.append("- _Kotasyon verisi yok._")
    elif p["quotes_ts"]:
        out.append(f"- _Kotasyon güncelleme: {_hm(p['quotes_ts'], p['now'])}_")
    return out


def _sec_stories(p: dict, heading: str, stories: list[dict], n: int = 10, split: bool = False,
                 empty: str = "- _Bu aralıkta önemli haber yok._", dedupe: bool = False,
                 notes: dict | None = None) -> list[str]:
    """Story list. Same-company stories collapse into one line ('+N haber'); with dedupe, stories already shown
    in an earlier section of the brief are skipped. notes: story id -> short tag shown after the title."""
    out = [heading]
    if dedupe:
        fresh = [s for s in stories if not _is_shown(p, s)]
        if stories and not fresh:
            return out + ["- _Bu aralığın öne çıkan hikayeleri yukarıdaki bölümlerde yer aldı._"]
        stories = fresh
    if not stories:
        return out + [empty]
    notes = notes or {}

    def lines(group: list[dict], limit: int) -> list[str]:
        res = []
        for s, more in _collapse(group)[:limit]:
            line = _story_line(p, s, note=notes.get(s["id"], ""))
            res.append(line + (f" · _+{len(more)} haber daha_" if more else ""))
            _mark_shown(p, [s] + more)
        return res

    if not split:
        return out + lines(stories, n)
    general = lines([s for s in stories if _is_general(s)], max(3, n // 2))
    company = lines([s for s in stories if not _is_general(s)], n)
    if general:
        out += ["", "### Genel piyasa, makro ve Fed"] + general
    if company:
        out += ["", "### Şirket haberleri"] + company
    return out


def _sec_who_says(p: dict, n: int = 4, heading: str = "## Kim ne diyor") -> list[str]:
    out = [heading]
    # Clusters whose every item already appeared in the story list above are skipped; a partly shown cluster
    # still stays because it may add sources.
    shown = p.get("_shown") or set()
    clusters = [c for c in p["clusters"] if not all(n.get("id") in shown for n in c["items"])][:n]
    if not clusters:
        return out + ["- _Birden fazla kaynağın birlikte yazdığı bir hikaye yok._"]
    _mark_shown(p, clusters)
    for c in clusters:
        per = _per_source(c)
        tk = ", ".join(c["tickers"][:3])
        out += ["", f"### {tk + ' · ' if tk else ''}{_md(c['title'], 110)} ({c['source_count']} kaynak)"]
        first = per[0]
        out.append(f"- {_agreement(per)} · İlk yazan: *{_md(_src_name(first), 45)}* "
                   f"({_clock(first.get('published_at') or 0, p['now'])} TR)")
        for it in _news_first(per)[:5]:
            line = (f"- *{_md(_src_name(it), 45)}* · "
                    f"{_clock(it.get('published_at') or 0, p['now'])} · "
                    f"{SENT_TR.get(it.get('sentiment_label') or 'neu', 'nötr')} — {_md(it.get('title'), 140)}")
            link = _link(it.get("url"))
            out.append(line + (" " + link if link else ""))
        if len(per) > 5:
            out.append(f"- _ve {len(per) - 5} kaynak daha_")
    return out


def _why_score(it: dict, chg: float, cats: set[str]) -> tuple[float, bool, bool]:
    """(score, headline states a move in the same direction, sentiment agrees) for one news item as the 'neden'
    of a chg% move."""
    title = str(it.get("title") or "")
    up, down = bool(UP_RE.search(title)), bool(DOWN_RE.search(title))
    same_dir = (up and not down) if chg > 0 else (down and not up)
    score = 0.0
    if up != down:
        score += 3.0 if same_dir else -2.0
    sent = _f(it.get("sentiment")) or 0.0
    agrees = abs(sent) >= 0.15 and sent * chg > 0
    if abs(sent) >= 0.15:
        score += 1.5 if agrees else -1.5
    if cats & CATALYST_CATS:
        score += 1.5
    if same_dir and PCT_RE.search(title):
        score += 0.5
    if it.get("source_type") == "social":
        score -= 1.5
    if LAWFIRM_RE.search(title):
        score -= 2.0  # "Investigation notice: law firm X notifies investors" follows a drop, it does not cause it
    return score, same_dir, agrees


def _why(p: dict, sym: str, name: str | None = None, chg: float | None = None, since: int | None = None) -> dict | None:
    """News item that best explains a chg% move of `sym` (published since `since`), as {title, url, sources}, or None.
    With a company name, the item must mention it (or an explicit '(SYM)' / '$SYM' / 'NYSE: SYM' tag) so
    mis-tagged tickers (RHI vs 'RHI Magnesita') are not shown. Items of the ticker's top stories are scored on a
    move verb in the right direction, matching sentiment and catalyst categories (importance only breaks ties):
    'Why Is X Stock Trading Lower' beats a bullish headline of the same story. Moves under 2% and contradicting
    headlines get no reason."""
    if chg is None or abs(chg) < 2:
        return None
    stories = (p.get("why_stories") or p["ticker_stories"]).get(sym) or []
    if since:
        stories = [s for s in stories if s["last_seen"] >= since]
    words = [w for w in re.findall(r"[a-z0-9]+", str(name or "").lower()) if len(w) >= 3 and w not in _NAME_STOP][:1]
    tag = re.compile(rf"(\(|\$|:\s?){re.escape(sym)}\b")
    best, best_key = None, None
    for s in stories[:12]:
        scats = set(s["categories"])
        for it in s["items"]:
            if since and (it.get("published_at") or 0) < since:
                continue
            text = f"{it.get('title') or ''} {it.get('summary') or ''}"
            if name and not (tag.search(text) or any(re.search(rf"\b{re.escape(w)}", text.lower()) for w in words)):
                continue
            score, same_dir, agrees = _why_score(it, chg, set(it.get("categories") or []) or scats)
            if score < 2 or (abs(chg) >= 5 and not (same_dir or agrees)):
                continue  # a bullish or neutral headline does not explain a 5%+ drop (and vice versa)
            k = (score, _f(it.get("importance")) or 0, it.get("published_at") or 0)
            if best_key is None or k > best_key:
                best, best_key = (s, it), k
    if not best:
        return None
    s, it = best
    others = [x for x in s["sources"] if x != _src_name(it)]
    return {"title": it.get("title") or s["title"], "url": it.get("url") or s["url"],
            "sources": [_src_name(it)] + others}


# Public names for scheduler._run_movers_why (it falls back to the private ones).
why_for = _why
group_stories = _group_stories


def _why_md(p: dict, sym: str, name, chg, since) -> str:
    s = _why(p, sym, name, chg, since)
    if not s:
        return ""
    link = _link(s["url"])
    return f" — *neden:* {_md(s['title'], 110)} ({_sources_md(s['sources'], 1)}){' ' + link if link else ''}"


def _mover_line(p: dict, r: dict, pre: bool, since: int | None = None) -> str:
    sym = r.get("symbol")
    pct = r.get("pre_change_pct") if pre and _f(r.get("pre_change_pct")) is not None else r.get("change_pct")
    parts = [f"**{_md(sym, 12)}** {_pct(pct)}"]
    if _f(r.get("price")) is not None:
        parts.append(f"{_price(r['price'])} $")
    vol, avg = _f(r.get("volume")), _f(r.get("avg_volume"))
    if vol:
        rel = f" (ort. {_num(vol / avg, 1)}x)" if avg and vol / avg >= 1.5 else ""
        parts.append(f"hacim {_big(vol)}{rel}")
    if r.get("name"):
        parts.append(_md(r["name"], 40))
    return "- " + " · ".join(parts) + _why_md(p, str(sym).upper(), r.get("name"), _f(pct),
                                               p["ctx_start"] if since is None else since)


def _session_day(p: dict) -> date:
    """ET date of the regular session the screener lists describe (the latest one that has opened)."""
    ts = p["movers"]["ts"] or p["now"]
    d = _et(ts).date()
    if not is_trading_day(d) or ts < _at_et(d, 9, 30):
        d = _prev_trading_day(d)
    return d


def _sec_movers(p: dict, heading: str, pre: bool = False, n: int = 6, since: int | None = None) -> list[str]:
    m = p["movers"]
    out = [heading]
    if not (m["gainers"] or m["losers"] or m["actives"]):
        return out + ["- _Hareketli hisse verisi yok._"]
    has_pre = pre and any(_f(r.get("pre_change_pct")) is not None for r in m["gainers"] + m["losers"])

    def key(r):
        v = r.get("pre_change_pct") if has_pre and _f(r.get("pre_change_pct")) is not None else r.get("change_pct")
        return _f(v) or 0.0

    lists = (("Yükselenler", sorted(m["gainers"], key=key, reverse=True)),
             ("Düşenler", sorted(m["losers"], key=key)),
             ("En aktifler", sorted(m["actives"], key=lambda r: _f(r.get("volume")) or 0, reverse=True)))
    for label, rows in lists:
        if rows:
            out += ["", f"### {label}"] + [_mover_line(p, r, has_pre, since) for r in rows[:n]]
    note = " · değişimler piyasa öncesi" if has_pre else (" · değişimler son seanstan (piyasa öncesi veri yok)" if pre else "")
    out += ["", f"- _Kaynak: {', '.join(m['sources']) or '?'} · güncelleme {_hm(m['ts'], p['now']) if m['ts'] else '?'}{note}_"]
    return out


def _ext_rows(p: dict, kind: str) -> list[dict]:
    """Extended-hours movers ('pre' | 'post') with a real move, largest first."""
    rows = p["movers"]["premarket" if kind == "pre" else "postmarket"]
    rows = [r for r in rows if abs(_f(r.get("change_pct")) or 0) >= 1]
    return sorted(rows, key=lambda r: abs(_f(r.get("change_pct")) or 0), reverse=True)


def _ext_line(p: dict, r: dict, kind: str) -> str:
    sym = str(r.get("symbol")).upper()
    pct = _f(r.get("change_pct"))
    tag = "PM" if kind == "pre" else "AH"
    parts = [f"**{_md(sym, 12)}** {tag} {_pct(pct)}"]
    if _f(r.get("price")) is not None:
        parts.append(f"{_price(r['price'])} $")
    reg = _f(r.get("regular_change_pct"))
    if reg is not None:
        parts.append(f"{'son seans' if kind == 'pre' else 'seans'} {_pct(reg)}")
    if r.get("name"):
        parts.append(_md(r["name"], 40))
    since = p["window_start"] if kind == "pre" else last_close_ts(p["now"])
    return "- " + " · ".join(parts) + _why_md(p, sym, r.get("name"), pct, since)


def _sec_ext_movers(p: dict, heading: str, kind: str, n: int = 6,
                    empty: str = "- _Uzatılmış seans hareket verisi yok._") -> list[str]:
    out = [heading]
    rows = _ext_rows(p, kind)
    if not rows:
        return out + [empty]
    up = [r for r in rows if (_f(r.get("change_pct")) or 0) > 0][:n]
    down = [r for r in rows if (_f(r.get("change_pct")) or 0) < 0][:n]
    for label, lst in (("Yükselenler", up), ("Düşenler", down)):
        if lst:
            out += ["", f"### {label}"] + [_ext_line(p, r, kind) for r in lst]
    what = "piyasa öncesi (PM) fiyat ve değişim; 'son seans' = önceki normal seans" if kind == "pre" else \
        "kapanış sonrası (AH) fiyat ve değişim; 'seans' = günün normal seansı"
    ts = p["movers"]["ext_ts"]
    out += ["", f"- _Kaynak: Yahoo · güncelleme {_hm(ts, p['now']) if ts else '?'} · {what}_"]
    return out


def _sec_premarket(p: dict, n: int = 6) -> list[str]:
    """Morning movers: the real pre-market gappers when available, else the last session (+ its after-hours)."""
    m = p["movers"]
    if _ext_rows(p, "pre"):
        return _sec_ext_movers(p, "## Piyasa öncesi hareketliler", "pre", n + 2)
    if any(_f(r.get("pre_change_pct")) is not None for r in m["gainers"] + m["losers"]):
        return _sec_movers(p, "## Piyasa öncesi hareketliler", True, n, since=p["window_start"])
    sd = _session_day(p)
    out = _sec_movers(p, f"## Son seansın hareketlileri — {_short_day(sd)}", False, n, since=p["why_start"])
    if p["session"] == "premarket":
        out.append("- _Piyasa öncesi hareket verisi henüz yok; değişimler son seanstan._")
    if _ext_rows(p, "post"):
        out += [""] + _sec_ext_movers(p, f"## Kapanış sonrası hareketliler (AH) — {_short_day(sd)}", "post", n)
    return out


def _sec_layout_movers(p: dict, n: int = 6) -> list[str]:
    if p["layout"] == "sabah":
        return _sec_premarket(p, n)
    if p["layout"] == "aksam":
        return (_sec_movers(p, "## Günün kazananları ve kaybedenleri", n=n) + [""] +
                _sec_ext_movers(p, "## Kapanış sonrası hareketliler (AH)", "post", n,
                                "- _Kapanış sonrası hareket verisi henüz yok._"))
    return _sec_movers(p, "## Gün içi hareketliler", n=n)


MACRO_MISSING = ("- _Bu gün için makro takvim verisi henüz yok (ForexFactory haftalık yayınlıyor; yeni hafta "
                 "Pazar günü gelir) — takvim boş görünse de önemli veri veya Fed konuşması olabilir._")


def _sec_macro(p: dict, heading: str, events: list[dict], empty: str = "- _Önemli makro veri/konuşma yok._",
               loaded: bool = True, day: date | None = None) -> list[str]:
    out = [heading]
    if not events:
        if loaded:
            return out + [empty]
        hint = _fomc_hint(day) if day else ""
        return out + [MACRO_MISSING] + ([hint] if hint else [])
    out += [_event_line(e) for e in events[:20]]
    if len(events) > 20:
        out.append(f"- _ve {len(events) - 20} etkinlik daha_")
    return out


def _sec_earnings(p: dict, heading: str, events: list[dict], times: tuple[str, ...], n: int = 5) -> list[str]:
    """Per bucket: watchlist names plus the n largest by market cap; the rest only as a count."""
    out = [heading]
    shown = False
    for t in times:
        rows = sorted((e for e in events if _earn_time(e) == t), key=lambda e: _earn_rank(e, p["watchlist"]), reverse=True)
        if not rows:
            continue
        shown = True
        wl = [e for e in rows if (e.get("ticker") or "").upper() in p["watchlist"]]
        top = wl + [e for e in rows if e not in wl][:n]
        out += ["", f"### {EARN_TIME_TR[t][0].upper() + EARN_TIME_TR[t][1:]} · {len(rows)} şirket"]
        out += [_earn_line(p, e) for e in top]
        if len(rows) > len(top):
            out.append(f"- _ve {len(rows) - len(top)} küçük şirket daha (tam liste Takvim sekmesinde)_")
    if not shown:
        out.append("- _Bilanço takvimi verisi yok._")
    return out


def _sec_ipo(p: dict, heading: str, events: list[dict]) -> list[str]:
    """Halka arz (IPO) calendar rows: a company offering, not a macro release. The price is the offer price, never a
    consensus forecast. No rows -> no section."""
    if not events:
        return []
    out = [heading]
    for e in events[:8]:
        x = e.get("extra") if isinstance(e.get("extra"), dict) else {}
        parts = [f"**{_md(e.get('ticker') or '', 12) or '?'}**"]
        comp = x.get("company") or e.get("title")
        if comp:
            parts.append(_md(comp, 45))
        status = _md(x.get("status_tr") or x.get("status") or "", 20)
        if status:
            parts.append(status)
        if _f(x.get("price")) is not None:
            parts.append(f"halka arz fiyatı {_price(x['price'])} $")
        if x.get("exchange"):
            parts.append(_md(x["exchange"], 30))
        out.append("- " + " · ".join(parts))
    if len(events) > 8:
        out.append(f"- _ve {len(events) - 8} halka arz daha (Takvim sekmesinde)_")
    return out


def _sec_prev_earnings(p: dict, n: int = 8) -> list[str]:
    """Morning: the previous session's biggest reported surprises (|surprise %| x market cap)."""
    out = ["## Dünkü bilanço sonuçları" if p["prev_day"] == p["today"] - timedelta(days=1)
           else f"## Önceki seansın bilanço sonuçları ({_short_day(p['prev_day'])})"]
    rows = []
    for e in p.get("earn_prev") or []:
        x = e.get("extra") or {}
        sp = _f(x.get("surprise_pct"))
        if sp is None or x.get("eps_actual") in (None, ""):
            continue
        rows.append((abs(sp) * (_money(x.get("market_cap")) or 0), e))
    if not rows:
        return out + ["- _Açıklanmış bilanço sonucu (EPS / sürpriz) verisi yok._"]
    rows.sort(key=lambda r: r[0], reverse=True)
    out += [_earn_line(p, e) for _, e in rows[:n]]
    if len(rows) > n:
        out.append(f"- _ve {len(rows) - n} şirket daha (Takvim sekmesinde 'sonuçlar')_")
    return out


def _subject_story(s: dict, sym: str) -> bool:
    """Is `sym` the subject of this story (not a passing mention)? Uses the items' subject_tickers attribution; rows
    without that attribution (enriched before it existed) count only when `sym` is the item's sole ticker, the same
    rule the opportunity scoring uses."""
    known = [_meta(it, "subject_tickers") for it in s["items"]]
    known = [k for k in known if k is not None]
    if known:
        return any(sym in k for k in known)
    return any(list(dict.fromkeys(it.get("tickers") or [])) == [sym] for it in s["items"])


def _opp_risks(o: dict) -> list[str]:
    r = []
    d, chg, price = o.get("direction"), _f(o.get("change_pct")), _f(o.get("price"))
    if d == "mixed":
        r.append("haber akışı karışık, yön belirsiz")
    if o.get("earnings_today"):
        r.append("bugün bilanço — sert oynaklık")
    if chg is not None and d == "long" and chg <= -2:
        r.append("fiyat haberin tersine düşüyor")
    if chg is not None and d == "short" and chg >= 2:
        r.append("fiyat haberin tersine yükseliyor")
    if chg is not None and abs(chg) >= 15:
        r.append("fiyat şimdiden çok hareket etti — kovalamak riskli")
    if len(o.get("sources") or []) <= 1:
        r.append("tek kaynak — teyit gerekli")
    sr = _f(o.get("social_rank"))
    if sr is not None and 0 < sr <= 10:
        r.append("sosyal medyada kalabalık — FOMO riski")
    if price is not None and price < 5:
        r.append("düşük fiyatlı hisse — likidite/manipülasyon riski")
    return r


def _sec_opps(p: dict, heading: str = "## Fırsatlar (ilk 10)", n: int = 10) -> list[str]:
    out = [heading]
    opps = p["opps"][:n]
    if not opps:
        return out + ["- _Fırsat skoru henüz hesaplanmadı._"]
    for i, o in enumerate(opps, 1):
        sym = str(o.get("ticker")).upper()
        head = [f"**#{i} {_md(sym, 12)}**", f"skor {int(round(_f(o.get('score')) or 0))}",
                DIR_TR.get(o.get("direction"), _md(o.get("direction"), 12) or "?")]
        if _f(o.get("price")) is not None:
            head.append(f"{_price(o['price'])} $" + (f" ({_pct(o['change_pct'])})" if _f(o.get("change_pct")) is not None else ""))
        if o.get("in_watchlist"):
            head.append("izleme listesi")
        if o.get("earnings_today"):
            head.append("bugün bilanço")
        out.append("- " + " · ".join(head))
        reasons = [str(x) for x in (o.get("reasons") or [])][:4]
        if reasons:
            out.append(f"  - Nedenler: {_md('; '.join(reasons), 260)}")
        srcs = [str(x) for x in (o.get("sources") or [])]
        if srcs:
            out.append(f"  - Kaynaklar: {_md(', '.join(srcs[:3]), 120)}{f' (+{len(srcs) - 3})' if len(srcs) > 3 else ''}")
        st = p["ticker_stories"].get(sym)
        # The Haber line must be about this company: the ticker is a subject of the story (not a passing mention).
        st = [s for s in p["ticker_stories"].get(sym, []) if _subject_story(s, sym)]
        if st:
            link = _link(st[0]["url"])
            out.append(f"  - Haber: {_md(st[0]['title'], 120)}{' ' + link if link else ''}")
        risks = _opp_risks(o)
        if risks:
            out.append(f"  - Risk: {'; '.join(risks)}")
    out.append(f"- _Skorlar kural tabanlıdır (haber kategorisi, kaynak sayısı, duygu, sosyal ilgi, fiyat hareketi)"
               f"{' · güncelleme ' + _hm(p['opps_ts'], p['now']) if p['opps_ts'] else ''}_")
    return out


def _sec_watchlist(p: dict, heading: str = "## İzleme listesi", earn_events: list[dict] | None = None) -> list[str]:
    out = [heading]
    wl = p["watchlist"]
    if not wl:
        return out + ["- _İzleme listesi boş (Ayarlar'dan eklenebilir)._"]
    qmap = _qmap(p)
    earn = {}
    for label, evs in ((_rel_day(p["day"], p["today"]), p["earn_day"]),
                       (_rel_day(p["next_day"], p["today"]), earn_events or [])):
        for e in evs:
            sym = (e.get("ticker") or "").upper()
            if sym in wl and sym not in earn:
                earn[sym] = f"{label} {EARN_TIME_TR[_earn_time(e)].split(' (')[-1].rstrip(')')}"
    quiet = []
    for sym in wl:
        sts = p["ticker_stories"].get(sym, [])
        q = qmap.get(sym)
        qtxt = f"{_price(q.get('price'))} ({_pct(q.get('change_pct'))})" if q and _f(q.get("price")) is not None else ""
        if not sts and sym not in earn:
            quiet.append(f"{sym}{' ' + _pct(q.get('change_pct')) if qtxt else ''}")
            continue
        parts = [f"**{_md(sym, 12)}**"] + ([qtxt] if qtxt else [])
        if sts:
            parts.append(f"{sum(len(s['items']) for s in sts)} haber")
        if sym in earn:
            parts.append(f"bilanço {earn[sym]}")
        line = "- " + " · ".join(parts)
        if sts:
            s = sts[0]
            link = _link(s["url"])
            line += f" — {_md(s['title'], 120)} — {_sources_md(s['sources'])}{' ' + link if link else ''}"
        out.append(line)
    if quiet:
        out.append(f"- _Kayda değer haber yok:_ {', '.join(quiet)}")
    return out


def _sec_social(p: dict, heading: str = "## Sosyal medya gündemi") -> list[str]:
    out = [heading]
    soc = p["social"] or {}
    ape = [a for a in soc.get("apewisdom") or [] if isinstance(a, dict) and a.get("symbol")]
    st = [s for s in soc.get("stocktwits") or [] if isinstance(s, dict) and s.get("symbol")]
    if ape:
        bits = []
        for a in ape[:10]:
            rank, prev = _f(a.get("rank")), _f(a.get("rank_24h_ago"))
            mv = ""
            if rank is not None and prev is not None and prev != rank:
                mv = f"▲{int(prev - rank)}" if prev > rank else f"▼{int(rank - prev)}"
            ment = _f(a.get("mentions"))
            extra = ", ".join(x for x in (mv, f"{_num(ment, 0)} bahsetme" if ment is not None else "") if x)
            bits.append(f"**{_md(a['symbol'], 10)}** #{int(rank) if rank is not None else '?'}{f' ({extra})' if extra else ''}")
        out.append("- **Reddit (ApeWisdom) ilk 10:** " + " · ".join(bits))
        risers = sorted((a for a in ape[:60] if _f(a.get("rank")) and _f(a.get("rank_24h_ago"))
                         and _f(a["rank_24h_ago"]) - _f(a["rank"]) >= 10),
                        key=lambda a: _f(a["rank_24h_ago"]) - _f(a["rank"]), reverse=True)[:5]
        if risers:
            out.append("- **Hızla yükselenler (24 saat):** " + ", ".join(
                f"{_md(a['symbol'], 10)} (#{int(_f(a['rank_24h_ago']))}→#{int(_f(a['rank']))})" for a in risers))
    if st:
        out.append("- **StockTwits trend:** " + ", ".join(_md(s["symbol"], 10) for s in st[:15]))
    posts = p["social_posts"][:3]
    if posts:
        out.append("- **Öne çıkan gönderiler:**")
        out += ["  " + _story_line(p, s, tags=False) for s in posts]
    if len(out) == 1:
        out.append("- _Sosyal medya verisi yok._")
    elif p["social_ts"]:
        out.append(f"- _Güncelleme: {_hm(p['social_ts'], p['now'])}_")
    return out


def _sec_watch_tomorrow(p: dict) -> list[str]:
    tomorrow = p["next_day"] == p["today"] + timedelta(days=1)
    out = ["## Yarın için izlenecekler" if tomorrow else f"## Sonraki seans için izlenecekler ({_short_day(p['next_day'])})"]
    if p["opps"]:
        out.append("- **Fırsat listesinin başı:** " + ", ".join(
            f"{_md(o['ticker'], 10)} ({int(round(_f(o.get('score')) or 0))}, {DIR_TR.get(o.get('direction'), '?')})"
            for o in p["opps"][:5]))
    hi = [e for e in p["macro_next"] if str(e.get("impact")).lower() == "high"
          or FED_RE.search(e.get("title") or "") or SPEECH_RE.search(e.get("title") or "")]
    if hi:
        out.append("- **Kritik veri/konuşmalar:** " + "; ".join(
            f"{fmt_tr(e['ts'], '%H:%M')} TR {_md((e.get('extra') or {}).get('title_tr') or e.get('title'), 50)}"
            for e in hi[:6]))
    elif not p["macro_next_loaded"]:
        hint = _fomc_hint(p["next_day"])
        out.append("- **Takvim:** sonraki seansın makro takvimi henüz yüklenmedi — veri/Fed saatlerini Takvim "
                   "sekmesinden kontrol edin." + (f" {hint[2:]}" if hint else ""))
    wl_earn = [e for e in p["earn_next"] if (e.get("ticker") or "").upper() in p["watchlist"]]
    if wl_earn:
        out.append("- **İzleme listesinde bilanço:** " + ", ".join(
            f"{e['ticker']} ({_earn_time(e).upper() if _earn_time(e) != 'unknown' else 'saat ?'})" for e in wl_earn))
    big = sorted(p["earn_next"], key=lambda e: _earn_rank(e, p["watchlist"]), reverse=True)[:6]
    if big:
        out.append(f"- **{'Yarının' if tomorrow else 'Sonraki seansın'} büyük bilançoları:** " + ", ".join(
            f"{_md(e.get('ticker'), 10)} ({_earn_time(e).upper() if _earn_time(e) != 'unknown' else '?'})" for e in big))
    after = [s for s in p["stories"] if s["lead_ts"] >= p["close_ts"] and s["source_count"] >= 2
             and not _is_shown(p, s)]
    if after:
        s = after[0]
        _mark_shown(p, [s])
        out.append(f"- **Kapanış sonrası en çok konuşulan:** {_md(s['title'], 120)} — {_sources_md(s['sources'])}"
                   f"{' ' + _link(s['url']) if s['url'] else ''}")
    fg = _f((p["fear_greed"] or {}).get("score"))
    if fg is not None and fg <= 25:
        out.append("- **Duygu:** piyasa aşırı korkuda — sert dalgalanma ve tepki hareketlerine dikkat.")
    elif fg is not None and fg >= 75:
        out.append("- **Duygu:** aşırı açgözlülük — kâr realizasyonu riski.")
    if len(out) == 1:
        out.append("- _Sonraki seans için öne çıkan bir veri yok._")
    return out


def _sec_top5(p: dict) -> list[str]:
    """At most five actionable bullets: tone, top 2 opportunities, the biggest extended-hours (or session) mover
    with its catalyst, and the next high-impact macro event (or the biggest multi-source story)."""
    L = p["layout"]
    out = ["## Özet: 5 madde" if p["kind"] == "manual" else "## Bugünün 5 maddesi"]
    label, bits = _tone(p, "vadeli" if L == "sabah" else "endeks")
    if bits:
        out.append(f"- **Ton: {label}** — {' · '.join(bits[:2])}")
    ext_kind = "pre" if L == "sabah" and _ext_rows(p, "pre") else ("post" if L != "ogle" and _ext_rows(p, "post") else "")
    ext_pct = {str(r.get("symbol")).upper(): _f(r.get("change_pct")) for r in (_ext_rows(p, ext_kind) if ext_kind else [])}
    for o in p["opps"][:2]:
        sym = str(o.get("ticker")).upper()
        bits_o = [f"**{_md(sym, 12)}**", DIR_TR.get(o.get("direction"), "?"), f"skor {int(round(_f(o.get('score')) or 0))}"]
        if _f(o.get("price")) is not None:
            bits_o.append(f"{_price(o['price'])} $" + (f" ({_pct(o['change_pct'])})" if _f(o.get("change_pct")) is not None else ""))
        if ext_pct.get(sym) is not None:
            bits_o.append(f"{'PM' if ext_kind == 'pre' else 'AH'} {_pct(ext_pct[sym])}")
        reasons = [str(x) for x in (o.get("reasons") or [])][:2]
        out.append("- **Fırsat:** " + " · ".join(bits_o) + (f" — {_md('; '.join(reasons), 160)}" if reasons else ""))
    # Penny stocks with no news win on percent alone: the headline pick needs a real price (>= 5 $).
    ext_big = [r for r in _ext_rows(p, ext_kind) if (_f(r.get("price")) or 0) >= 5] if ext_kind else []
    if ext_big:
        r = ext_big[0]
        out.append(f"- **En sert {'piyasa öncesi' if ext_kind == 'pre' else 'kapanış sonrası'} hareket:** "
                   + _ext_line(p, r, ext_kind)[2:])
    else:
        m = p["movers"]
        rows = sorted((r for r in m["gainers"] + m["losers"] if (_f(r.get("price")) or 0) >= 5),
                      key=lambda r: abs(_f(r.get("change_pct")) or 0), reverse=True)
        if rows:
            since = p["why_start"] if L == "sabah" else None
            out.append(f"- **En sert {'son seans' if L == 'sabah' else 'seans'} hareketi:** "
                       + _mover_line(p, rows[0], False, since)[2:])
    events = p["macro_next"] if L == "aksam" else [e for e in p["macro_day"] if e["ts"] > p["now"]]
    hi = [e for e in events if str(e.get("impact")).lower() == "high" or FED_RE.search(e.get("title") or "")]
    loaded = p["macro_next_loaded"] if L == "aksam" else p["macro_day_loaded"]
    if hi:
        e = hi[0]
        when = "Tüm gün" if e.get("all_day") else _when(e["ts"])
        out.append(f"- **Sıradaki kritik veri:** {when} · "
                   f"{_md((e.get('extra') or {}).get('title_tr') or e.get('title'), 60)}"
                   + (f" · Beklenti {_numtxt(e['forecast'])}" if e.get("forecast") else ""))
    elif not loaded:
        hint = _fomc_hint(p["next_day"] if L == "aksam" else p["day"])
        out.append("- **Takvim:** makro takvim henüz yüklenmedi." + (f" {hint[2:]}" if hint else ""))
    elif p["clusters"]:
        c = p["clusters"][0]
        tk = ", ".join(c["tickers"][:2])
        out.append(f"- **En çok konuşulan:** {tk + ' · ' if tk else ''}{_md(c['title'], 120)} "
                   f"({c['source_count']} kaynak)")
    if len(out) == 1:
        out.append("- _Özet için yeterli veri yok._")
    return out[:6]


def _safe(fn, *args, **kw) -> str:
    try:
        lines = fn(*args, **kw)
        return "\n".join(lines).strip()
    except Exception:
        log.exception("brief section %s failed", getattr(fn, "__name__", fn))
        return "- _Bu bölüm oluşturulamadı._"


# ---------------------------------------------------------------- rule-based brief

def render_rule(p: dict) -> tuple[str, str]:
    """Return (title, body_md) for a rule-based brief from a build_pack() result."""
    L, day = p["layout"], p["day"]
    p["_shown"] = set()  # item ids already listed: later sections skip repeats
    day_name = "Bugünün takvimi" if day == p["today"] else "Takvim"
    if L == "sabah":
        heading = "## Gece olanlar (son kapanıştan beri)" if p["kind"] == "sabah" else "## Son kapanıştan beri olanlar"
        secs = [
            (_sec_top5, p),
            (_sec_tone, p, "## Piyasa tonu", ["vadeli", "endeks", "faiz", "doviz", "emtia", "kripto"], " (son kapanış)"),
            (_sec_stories, p, heading, p["stories"], 12, True),
            (_sec_who_says, p, 4),
            (_sec_macro, p, f"## {day_name} — {_short_day(day)}", p["macro_day"], "- _Önemli makro veri/konuşma yok._",
             p["macro_day_loaded"], day),
            (_sec_prev_earnings, p),
            (_sec_earnings, p, f"## Bilançolar — {_short_day(day)}", p["earn_day"], ("bmo", "amc", "unknown")),
            (_sec_premarket, p),
            (_sec_ipo, p, "## Halka arzlar", p["ipos"]),
            (_sec_opps, p),
            (_sec_watchlist, p),
            (_sec_social, p),
        ]
    elif L == "ogle":
        upcoming = [e for e in p["macro_day"] if e["ts"] > p["now"]]
        released = [e for e in p["macro_day"] if e["ts"] <= p["now"]]
        by_breadth = sorted(p["stories"], key=lambda s: (s["source_count"], s["importance"]), reverse=True)
        secs = [
            (_sec_top5, p),
            (_sec_tone, p, "## Açılıştan beri", ["endeks", "vadeli", "faiz", "doviz", "emtia", "kripto"]),
            (_sec_stories, p, "## En çok konuşulan hikayeler", by_breadth, 10),
            (_sec_who_says, p, 4),
            (_sec_movers, p, "## Gün içi hareketliler"),
            (_sec_macro, p, "## Bugün açıklananlar", released, "- _Bugün henüz önemli veri açıklanmadı._",
             p["macro_day_loaded"], day),
            (_sec_macro, p, "## Öğleden sonra gelecek veriler ve Fed konuşmaları", upcoming,
             "- _Öğleden sonra önemli veri/konuşma yok._" if p["macro_day_loaded"] else
             "- _Makro takvim verisi yüklenmedi (yukarıya bakın)._"),
            (_sec_earnings, p, "## Bu akşam bilanço açıklayacaklar", p["earn_day"], ("amc", "unknown")),
            (_sec_ipo, p, "## Halka arzlar", p["ipos"]),
            (_sec_opps, p),
            (_sec_watchlist, p),
            (_sec_social, p),
        ]
    else:
        # Each story belongs to exactly one part of the day, decided by its lead (most important) item, so a day
        # story with one late copy is not repeated as an after-close development.
        close, day_start = p["close_ts"], _at_et(day, 4, 0)
        since_close = [s for s in p["stories"] if s["lead_ts"] >= close]
        # Today's news only: previous-evening stories were in last night's / this morning's briefs.
        day_stories = [s for s in p["stories"] if s["lead_ts"] < close and s["last_seen"] >= day_start]
        carried = {s["id"]: "dünden devam" for s in day_stories if s["first_seen"] < day_start}
        amc_syms = {(e.get("ticker") or "").upper() for e in p["earn_day"] if _earn_time(e) == "amc"}
        # Results only: a story needs a reported outcome (beat / miss / mixed, from the classifier's wording), or an
        # after-close ticker with an earnings category. Previews and date notices are not results.
        amc_news = [s for s in since_close if not _is_general(s) and not EARN_DATE_RE.search(s["title"])
                    and not EARN_PREVIEW_RE.search(s["title"])
                    and (any(_meta(it, "earnings_outcome") in ("beat", "miss", "mixed") for it in s["items"])
                         or ("earnings" in s["categories"] and set(s["tickers"]) & amc_syms))]
        other_after = [s for s in since_close if s not in amc_news]
        nd = p["next_day"]
        nxt = "Yarının" if nd == p["today"] + timedelta(days=1) else "Sonraki seansın"
        secs = [
            (_sec_top5, p),
            (_sec_tone, p, "## Kapanış özeti", ["endeks", "vadeli", "faiz", "doviz", "emtia", "kripto"]),
            (_sec_movers, p, "## Günün kazananları ve kaybedenleri"),
            (_sec_stories, p, "## Günün öne çıkan haberleri", day_stories, 10, True,
             "- _Bu aralıkta önemli haber yok._", False, carried),
            (_sec_who_says, p, 4),
            (_sec_ext_movers, p, "## Kapanış sonrası hareketliler (AH)", "post", 6,
             "- _Kapanış sonrası hareket verisi henüz yok._"),
            (_sec_earnings, p, "## Piyasa sonrası bilançolar (AMC)", p["earn_day"], ("amc",)),
            (_sec_stories, p, "### Kapanış sonrası bilanço haberleri", amc_news, 8, False,
             "- _Henüz kapanış sonrası bilanço haberi yok._", True),
            (_sec_stories, p, "## Kapanış sonrası diğer gelişmeler", other_after, 8, False,
             "- _Kapanıştan beri önemli gelişme yok._", True),
            (_sec_macro, p, f"## {nxt} takvimi — {_short_day(nd)}", p["macro_next"], "- _Önemli makro veri/konuşma yok._",
             p["macro_next_loaded"], nd),
            (_sec_earnings, p, f"## {nxt} bilançoları", p["earn_next"], ("bmo", "amc", "unknown")),
            (_sec_watch_tomorrow, p),
            (_sec_ipo, p, "## Halka arzlar", p["ipos"]),
            (_sec_opps, p),
            (_sec_watchlist, p, "## İzleme listesi", p["earn_next"]),
            (_sec_social, p),
        ]
    blocks = [_safe(_sec_header, p), _safe(_freshness, p)] + [_safe(fn, *args) for fn, *args in secs]
    body = "\n\n".join(b for b in blocks if b) + "\n\n---\n\n" + DISCLAIMER + "\n"
    return p["title"], body


def generate(kind: str, cfg: dict | None = None, now: int | None = None) -> dict:
    """Build and save a rule-based brief (author 'kural'). Returns the saved brief dict."""
    kind = normalize_kind(kind) or "manual"
    p = build_pack(kind, cfg, now)
    title, body = render_rule(p)
    return db.save_brief(kind, "kural", title, body, created_at=p["now"])


# ---------------------------------------------------------------- compact data pack (Claude / Ollama)

def _pack_story(p: dict, i: int, s: dict, summaries: bool | int) -> list[str]:
    lab = p["labels"]
    tags = ", ".join(_md(lab.get(c, c), 30) for c in s["categories"][:4]) or "-"
    tk = ", ".join(s["tickers"][:6]) or "-"
    more = len(s["sources"]) - 5
    # Evening pack: flag stories that ended before today's session day (yesterday evening's news).
    old = " · _önceki günden_" if p["layout"] == "aksam" and s["last_seen"] < _at_et(p["day"], 4, 0) else ""
    lines = [f"{i}. [önem {int(round(s['importance']))} · {SENT_TR[_sent_label(s['sentiment'])]} {s['sentiment']:+.2f}] "
             f"{fmt_tr(s['first_seen'], '%d.%m %H:%M')} TR · **{tk}** · {_md(s['title'], 200)}{old}",
             f"   Kaynaklar ({s['source_count']}): {', '.join(_md(x, 45) for x in s['sources'][:5])}"
             f"{f' (+{more})' if more > 0 else ''} · Etiketler: {tags}{' · ' + _url(s['url']) if _url(s['url']) else ''}"]
    limit = 160 if summaries is True else int(summaries or 0)
    if limit and s["summary"] and s["summary"].strip().lower() != s["title"].strip().lower():
        lines.append(f"   Özet: {_md(s['summary'], limit)}")
    return lines


def _pack_cluster(c: dict, max_src: int) -> list[str]:
    per = _per_source(c)
    tk = ", ".join(c["tickers"][:4])
    out = [f"### {tk + ' — ' if tk else ''}{_md(c['title'], 160)} ({c['source_count']} kaynak, "
           f"önem {int(round(c['importance']))})", f"- {_agreement(per)}"]
    for it in _news_first(per)[:max_src]:
        out.append(f"- {_md(_src_name(it), 45)} · {fmt_tr(it.get('published_at') or 0, '%d.%m %H:%M')} TR · "
                   f"{SENT_TR.get(it.get('sentiment_label') or 'neu', 'nötr')} ({(_f(it.get('sentiment')) or 0):+.2f}) — "
                   f"{_md(it.get('title'), 160)}{' · ' + _url(it.get('url')) if _url(it.get('url')) else ''}")
    return out


def _freshness(p: dict) -> list[str]:
    now = p["now"]
    bits, warns = [], []
    if p["news"]:
        last_pub = max(n.get("published_at") or 0 for n in p["news"])
        last_fetch = max(n.get("fetched_at") or 0 for n in p["news"])
        bits.append(f"son haber {_ago(now - last_pub)} önce")
        if last_fetch and now - last_fetch > 3 * 3600:
            warns.append(f"Son veri çekimi {_ago(now - last_fetch)} önce — uygulama çalışmıyor olabilir.")
    else:
        warns.append("Pencerede hiç haber yok — uygulama veri toplamamış olabilir.")
    for label, ts in (("kotasyonlar", p["quotes_ts"]), ("hareketliler", p["movers"]["ts"]),
                      ("fırsat skorları", p["opps_ts"]), ("sosyal", p["social_ts"]), ("korku/açgözlülük", p["fg_ts"])):
        bits.append(f"{label} {_ago(now - ts) + ' önce' if ts else 'yok'}")
    if not p.get("macro_day_loaded", True):
        warns.append(f"Seans gününün ({_short_day(p['day'])}) makro takvimi yüklenmedi — boş takvim 'veri yok' "
                     "anlamına gelmez.")
    if not p.get("macro_next_loaded", True) and p["next_day"] != p["day"]:
        warns.append(f"Sonraki seansın ({_short_day(p['next_day'])}) makro takvimi yüklenmedi — boş takvim 'veri yok' "
                     "anlamına gelmez." + (f" {_fomc_hint(p['next_day'])[2:]}" if _fomc_hint(p["next_day"]) else ""))
    return [f"- Veri tazeliği: {'; '.join(bits)}"] + [f"- **UYARI:** {w}" for w in warns]


_GNEWS_URL_RE = re.compile(r"https?://news\.google\.com/[^\s)\]]+", re.I)


def _ref_links(p: dict, text: str) -> str:
    """Google News redirect URLs -> 'ref:<news id>' (import_claude_outputs expands them back)."""
    ids = p.get("url_ids") or {}
    return _GNEWS_URL_RE.sub(lambda m: f"ref:{ids[m.group(0)]}" if m.group(0) in ids else m.group(0), text)


def render_pack(p: dict, out_path: Path | None = None, max_stories: int = 60, max_clusters: int = 12,
                max_opps: int = 15, summaries: bool | int = True, compact: bool = False, refs: bool = False) -> str:
    """Compact Markdown data pack for an LLM (Claude Code or Ollama). summaries: True (160 chars), False, or a
    max length; refs: print Google News redirect links as 'ref:<id>'."""
    p["_shown"] = set()
    srcs = Counter(n.get("source_name") or n.get("source") for n in p["news"])
    lines = [f"# Piyasa Masası veri paketi — {p['title']}", "",
             f"- Brifing türü: {p['kind']} (düzen: {p['layout']})",
             f"- Önerilen başlık: {p['title']}",
             f"- Oluşturulma: {fmt_tr(p['now'], '%d.%m.%Y %H:%M')} TR ({fmt_et(p['now'], '%H:%M')} ET) · "
             f"Oturum: {SESSION_TR.get(p['session'], p['session'])}",
             f"- Haber penceresi: {_when(p['window_start'])} → şimdi · {len(p['news'])} haber · "
             f"{len(_publisher_counts(p['news']))} kaynak",
             f"- Seans günü: {tr_date(p['day'])} · sonraki işlem günü: {tr_date(p['next_day'])}"]
    if out_path:
        lines.append(f"- ÇIKTI DOSYASI (brifingi buraya yaz): {out_path}")
    if refs:
        lines.append("- Linkler: `ref:<kimlik>` biçimindeki linkleri aynen `[link](ref:<kimlik>)` olarak kullan; "
                     "uygulama içe aktarırken gerçek adrese çevirir.")
    lines += _freshness(p)
    lines.append(f"- İzleme listesi: {', '.join(p['watchlist']) or '-'}")
    groups = ["vadeli", "endeks", "faiz", "doviz", "emtia", "kripto"] if p["layout"] == "sabah" else \
        ["endeks", "vadeli", "faiz", "doviz", "emtia", "kripto"]
    cl = p["clusters"][:max_clusters]
    st = p["stories"][:max_stories]
    wlq = [q for q in p["quotes"] if q.get("group") == "izleme"]
    sec = {
        "top5": _safe(_sec_top5, p),
        "tone": _safe(_sec_tone, p, "## Piyasa", groups),
        "wlq": "\n".join(["## İzleme listesi kotasyonları"] + [
            f"- {_md(q.get('symbol'), 12)} {_price(q.get('price'))} ({_pct(q.get('change_pct'))})"
            f"{' · ' + MARKET_STATE_TR.get(str(q.get('market_state')).upper(), _md(q.get('market_state'), 20)) if q.get('market_state') else ''}"
            for q in wlq]) if wlq else "",
        "opps": _safe(_sec_opps, p, f"## Kural tabanlı fırsat skorları (ilk {max_opps})", max_opps),
        "clusters": "\n".join(["## Çok kaynaklı hikayeler (kim ne diyor)"] + (
            [x for c in cl for x in [""] + _pack_cluster(c, 3 if compact else 5)] if cl else ["- _Yok._"])),
        "stories": "\n".join([f"## En önemli haberler (önem sırasıyla, ilk {len(st)} / {len(p['stories'])})"] + (
            [x for i, s in enumerate(st, 1) for x in _pack_story(p, i, s, summaries)] if st else ["- _Yok._"])),
        "movers": _safe(_sec_layout_movers, p, 5 if compact else 6),
        "macro_day": _safe(_sec_macro, p, f"## Makro takvim — {_short_day(p['day'])}", p["macro_day"],
                           "- _Önemli makro veri/konuşma yok._", p["macro_day_loaded"], p["day"]),
        "earn_day": _safe(_sec_earnings, p, f"## Bilançolar — {_short_day(p['day'])}", p["earn_day"],
                          ("bmo", "amc", "unknown"), 6 if compact else 10),
        "macro_next": _safe(_sec_macro, p, f"## Makro takvim — {_short_day(p['next_day'])}", p["macro_next"],
                            "- _Önemli makro veri/konuşma yok._", p["macro_next_loaded"], p["next_day"]),
        "earn_next": _safe(_sec_earnings, p, f"## Bilançolar — {_short_day(p['next_day'])}", p["earn_next"],
                           ("bmo", "amc", "unknown"), 4 if compact else 8),
        "ipo": _safe(_sec_ipo, p, "## Halka arzlar", p["ipos"]),
    }
    if compact:  # truncated from the end for small models: keep the calendar ahead of the long story list
        order = ["tone", "opps", "clusters", "macro_day", "macro_next", "earn_day", "earn_next", "ipo", "stories",
                 "movers"]
    else:
        # The long story list goes last so a partial read still has the calendar, earnings and watchlist.
        sec["watchlist"] = _safe(_sec_watchlist, p, "## İzleme listesi haberleri", p["earn_next"])
        sec["social"] = _safe(_sec_social, p, "## Sosyal medya")
        sec["sources"] = ("## Kaynak dağılımı (toplayıcıya göre)\n" +
                          " · ".join(f"{_md(k, 45)} {v}" for k, v in srcs.most_common(25)) if srcs else "")
        order = ["top5", "tone", "wlq", "movers", "opps", "macro_day", "earn_day", "macro_next", "earn_next", "ipo",
                 "watchlist", "clusters", "social", "stories", "sources"]
    blocks = ["\n".join(lines)] + [sec[k] for k in order]
    text = "\n\n".join(b for b in blocks if b) + "\n"
    return _ref_links(p, text) if refs else text


# ---------------------------------------------------------------- Ollama

OLLAMA_SYSTEM = ("Sen ABD hisse senedi piyasalarını takip eden deneyimli bir gün içi işlem (day-trade) analistisin. "
                 "Yalnızca sana verilen verilere dayanırsın, sayı veya haber uydurmazsın, kesin al/sat tavsiyesi "
                 "vermezsin. Her zaman akıcı ve sade Türkçe yazarsın.")
OLLAMA_FOCUS = {
    "sabah": "Piyasa açılmadan önceki durum: gece olanlar, vadeliler, bugünün verileri ve bilançoları, açılışta "
             "izlenecek hisseler.",
    "ogle": "Açılıştan bu yana olanlar: endeks hareketi, en çok konuşulan hikayeler, öğleden sonra gelecek veriler "
            "ve Fed konuşmaları.",
    "aksam": "Kapanış değerlendirmesi: günün kazananları/kaybedenleri, kapanış sonrası bilançolar, yarının takvimi "
             "ve yarın izlenecekler.",
}
OLLAMA_MAX_DATA = 7000
OLLAMA_PROMPT = """Aşağıda "{title}" için uygulamanın topladığı piyasa verileri var.
Odak: {focus}

Bu verilere dayanarak kısa ve keskin bir Türkçe piyasa analizi yaz (en fazla 350 kelime). Markdown kullan ve tam olarak şu başlıkları kullan:
## Piyasa tonu
## Öne çıkan hikayeler ve kim ne diyor
## Fırsatlar ve riskler
## Takvim ve dikkat noktaları

Kurallar:
- Hisse kodlarını **kalın** yaz; rakamları verideki gibi kullan, yeni rakam uydurma.
- Kaynaklar arasında yorum farkı varsa belirt (hangi kaynak ne diyor).
- Veri yoksa "veri yok" de. Kesin al/sat önerisi verme. En üste # başlık yazma.

VERİ:
{data}
"""


def _clean_llm(text: str | None) -> str:
    if not text:
        return ""
    t = re.sub(r"<think>.*?</think>", "", str(text), flags=re.S | re.I).strip()
    t = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", t).strip()
    lines = t.splitlines()
    while lines and (not lines[0].strip() or re.match(r"^#\s", lines[0])):
        lines.pop(0)
    return "\n".join(lines).strip()


def generate_ollama(kind: str, cfg: dict | None = None, now: int | None = None) -> dict | None:
    """Ask local Ollama to write the brief from the same data pack. Returns None if Ollama is unavailable."""
    cfg = cfg or load_config()
    kind = normalize_kind(kind) or "manual"
    try:
        from .analysis import ml
        if not ml.ollama_available(cfg):
            return None
    except Exception as e:
        log.info("ollama unavailable: %s", e)
        return None
    p = build_pack(kind, cfg, now)
    data = render_pack(p, None, max_stories=15, max_clusters=4, max_opps=8, summaries=False, compact=True)
    # Links cost many tokens and small models mangle them; real links are appended below the answer instead.
    data = re.sub(r" · https?://\S+", "", re.sub(r"\s*\[link\]\([^)]*\)", "", data))
    if len(data) > OLLAMA_MAX_DATA:  # small local models often run with a 4k-token context
        data = data[:OLLAMA_MAX_DATA].rsplit("\n", 1)[0] + "\n…"
    prompt = OLLAMA_PROMPT.format(title=p["title"], focus=OLLAMA_FOCUS.get(p["layout"], ""), data=data)
    try:
        text = ml.ollama_generate(prompt, cfg, system=OLLAMA_SYSTEM, timeout=240)
    except Exception:
        log.exception("ollama_generate failed")
        return None
    text = _clean_llm(text)
    if len(text) < 80:
        return None
    refs = [_story_line(p, s, tags=False) for s in p["stories"][:8]]
    model = str(cfg.get("ollama_model") or "").strip()
    body = text + ("\n\n### Kaynak haberler\n" + "\n".join(refs) if refs else "") + "\n\n---\n\n" + OLLAMA_DISCLAIMER
    if model:
        body += f"\n\n_Model: {_md(model, 60)}_"
    return db.save_brief(kind, "ollama", p["title"], body + "\n", created_at=int(now or now_ts()))


# ---------------------------------------------------------------- Claude Code bridge

def claude_paths(kind: str, now: int | None = None) -> tuple[Path, Path]:
    """(input pack path, output path Claude should write) for this kind, dated by the session day (ET)."""
    kind = normalize_kind(kind) or "manual"
    now = int(now or now_ts())
    fr = _frame(kind, now)
    stem = f"{fr['title_day'].isoformat()}_{kind}.md"
    return CLAUDE_IN_DIR / stem, CLAUDE_OUT_DIR / stem


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def export_for_claude(kind: str, cfg: dict | None = None, now: int | None = None) -> Path:
    """Write CLAUDE_IN_DIR/<date>_<kind>.md and latest.md; returns the dated path."""
    kind = normalize_kind(kind) or "manual"
    now = int(now or now_ts())
    in_path, out_path = claude_paths(kind, now)
    p = build_pack(kind, cfg, now)
    # Size guard: the pack must fit one Read of a Claude Code task; trim stories / summaries / clusters /
    # opportunities step by step until it does.
    for max_stories, summary_len, max_clusters, max_opps in ((60, 160, 12, 15), (45, 120, 10, 12), (40, 0, 8, 10),
                                                             (30, 0, 6, 10), (20, 0, 5, 8), (12, 0, 4, 6)):
        text = render_pack(p, out_path, max_stories=max_stories, max_clusters=max_clusters, max_opps=max_opps,
                           summaries=summary_len, refs=True)
        if len(text.encode("utf-8")) <= PACK_MAX_BYTES:
            break
    _atomic_write(in_path, text)
    _atomic_write(CLAUDE_IN_DIR / "latest.md", text)
    return in_path


_OUT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})?[_\-\s]*([a-zçğıöşü]+)?", re.I)


def _kind_from_name(name: str, mtime: float) -> tuple[str, date]:
    stem = Path(name).stem
    m = _OUT_NAME_RE.match(stem)
    d = None
    if m and m.group(1):
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            d = None
    kind = normalize_kind(m.group(2)) if m and m.group(2) else None
    if not kind:
        kind = next((k for k in KINDS if k in stem.lower()), None) or "manual"
    return kind, d or _et(int(mtime)).date()


def _split_title(text: str) -> tuple[str, str]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^#\s+\S", lines[0]):
        title = re.sub(r"^#\s+", "", lines[0]).strip().replace("**", "")
        return title[:200], "\n".join(lines[1:]).strip()
    return "", "\n".join(lines).strip()


_REF_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*ref:([0-9a-f]{20})\s*\)|\bref:([0-9a-f]{20})\b")


def _expand_refs(text: str) -> str:
    """'[link](ref:<news id>)' / bare 'ref:<id>' (pack shorthand for Google News links) -> the stored URL.
    Unknown ids keep only the link label."""
    ids = {m.group(2) or m.group(3) for m in _REF_LINK_RE.finditer(text)}
    if not ids:
        return text
    urls: dict[str, str] = {}
    try:
        ids_l = sorted(ids)
        for i in range(0, len(ids_l), 500):
            chunk = ids_l[i:i + 500]
            rows = db.conn().execute(f"SELECT id, url FROM news WHERE id IN ({','.join('?' * len(chunk))})",
                                     chunk).fetchall()
            urls.update({r[0]: _url(r[1]) for r in rows if _url(r[1])})
    except Exception:
        log.exception("ref lookup failed")

    def sub(m: re.Match) -> str:
        rid = m.group(2) or m.group(3)
        u = urls.get(rid)
        if m.group(2):  # [label](ref:id)
            label = m.group(1)
            return f"[{label}]({u})" if u else ("" if label.strip().lower() in ("", "link") else label)
        return u or ""

    return _REF_LINK_RE.sub(sub, text)


def import_claude_outputs() -> int:
    """Save new or updated CLAUDE_OUT_DIR/*.md files as briefs (author 'claude'). Returns how many were saved."""
    now = time.time()
    try:
        existing = {b["id"]: b for b in db.list_briefs(limit=500, author="claude")}
    except Exception:
        log.exception("list_briefs failed")
        existing = {}
    saved = 0
    for path in sorted(CLAUDE_OUT_DIR.glob("*.md")):
        try:
            st = path.stat()
            if st.st_size == 0 or now - st.st_mtime < 5 or now - st.st_mtime > 7 * 86400:
                continue  # empty, still being written, or too old
            bid = make_id(path.name)
            prev = existing.get(bid)
            if prev and int(prev.get("created_at") or 0) >= int(st.st_mtime):
                continue
            title, body = _split_title(path.read_text(encoding="utf-8-sig", errors="replace"))
            body = _expand_refs(body)
            if len(body) < 20:
                continue
            kind, d = _kind_from_name(path.name, st.st_mtime)
            title = title or f"{brief_title(kind, d)} (Claude)"
            if "tavsiye" not in body.lower():
                body += "\n\n---\n\n" + CLAUDE_DISCLAIMER
            db.save_brief(kind, "claude", title, body + "\n", created_at=int(st.st_mtime), brief_id=bid)
            saved += 1
        except Exception:
            log.exception("claude output %s could not be imported", path.name)
    return saved


# ---------------------------------------------------------------- scheduling helpers

def _brief_times(cfg: dict) -> list[tuple[str, int, int]]:
    out = []
    for kind, hhmm in (cfg.get("brief_times_et") or {}).items():
        if kind not in SCHEDULED_KINDS:
            continue
        m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", str(hhmm or ""))
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.append((kind, int(m.group(1)), int(m.group(2))))
    return sorted(out, key=lambda x: (x[1], x[2]))


def _brief_target(d: date, kind: str, h: int, m: int) -> int:
    """Scheduled ts on day d. An aksam set after the regular 16:00 close moves with an early close (13:00 ->
    16:30 becomes 13:30)."""
    shift = 16 * 60 - close_minutes(d)
    if kind == "aksam" and shift > 0 and h * 60 + m >= 16 * 60:
        return _at_et(d, h, m) - shift * 60
    return _at_et(d, h, m)


def due_briefs(cfg: dict, now_ts: int, last_run: dict, max_late: int | None = 4 * 3600) -> list[str]:
    """Kinds whose ET time (cfg['brief_times_et']) has passed today on a trading day and that have not run
    today (last_run: kind -> ts). Briefs more than `max_late` seconds overdue are skipped (app started late)."""
    now = int(now_ts)
    et = _et(now)
    if not is_trading_day(et.date()):
        return []
    day_start = _at_et(et.date())
    due = []
    for kind, h, m in _brief_times(cfg):
        target = _brief_target(et.date(), kind, h, m)
        if now < target or (max_late is not None and now - target > max_late):
            continue
        if int((last_run or {}).get(kind) or 0) >= day_start:
            continue
        due.append(kind)
    return due


def last_runs_from_db(author: str = "kural") -> dict[str, int]:
    """kind -> created_at of the newest brief by `author`; lets the scheduler survive restarts."""
    out: dict[str, int] = {}
    try:
        for b in db.list_briefs(limit=100, author=author):
            out[b["kind"]] = max(out.get(b["kind"], 0), int(b["created_at"]))
    except Exception:
        log.exception("list_briefs failed")
    return out


def next_brief_times(cfg: dict, now: int | None = None, limit: int = 3) -> list[dict]:
    """Upcoming scheduled briefs [{kind, ts}] on trading days (for /api/status next_briefs)."""
    now = int(now or now_ts())
    times = _brief_times(cfg)
    out: list[dict] = []
    d = _et(now).date()
    for _ in range(14):
        if is_trading_day(d):
            out += [{"kind": k, "ts": _brief_target(d, k, h, m)} for k, h, m in times if _brief_target(d, k, h, m) > now]
        if len(out) >= limit:
            break
        d += timedelta(days=1)
    return sorted(out, key=lambda x: x["ts"])[:limit]
