"""Per-ticker opportunity scoring from news flow, catalysts, social buzz, price action and earnings."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .. import db
from ..util import ET, is_trading_day, last_close_ts, market_session, now_ts
from . import EVENT_CATEGORIES, derive_meta, strip_publisher
from . import tickers as T
from .classify import CATEGORIES, is_junk_title, is_opinion
from .cluster import norm_title, publisher_key, publisher_name

log = logging.getLogger("marketdesk.analysis")

HALF_LIFE_H = 6.0
TOP_N = 40

# Category -> Turkish reason phrase (default wording; outcome-specific wording below).
_REASON = {
    "fda": "FDA gelişmesi",
    "clinical": "Klinik çalışma haberi",
    "earnings": "Bilanço haberi",
    "guidance_up": "Beklenti (guidance) artırımı",
    "guidance_down": "Beklenti (guidance) indirimi",
    "analyst_up": "Analist not / hedef fiyat artırımı",
    "analyst_down": "Analist not / hedef fiyat indirimi",
    "mna": "Birleşme / satın alma haberi",
    "offering": "Hisse arzı → sulandırma riski",
    "buyback": "Hisse geri alım programı",
    "dividend": "Temettü haberi",
    "insider": "İçeriden işlem bildirimi",
    "contract": "Yeni sözleşme / anlaşma",
    "lawsuit": "Dava / soruşturma haberi",
    "accounting": "Muhasebe / denetim sorunu",
    "bankruptcy": "İflas riski",
    "layoffs": "İşten çıkarma haberi",
    "ipo": "Halka arz gündemi",
    "halt": "İşlem durdurma",
    "short_report": "Açığa satış (short) raporu",
    "split": "Hisse bölünmesi",
    "reverse_split": "Ters bölünme (genelde olumsuz)",
    "delisting": "Borsadan çıkarılma / kotasyon uyarısı",
    "management": "Yönetim değişikliği",
    "activist": "Aktivist yatırımcı / büyük pay alımı",
    "index_change": "Endekse dahil olma",
    "product": "Ürün / lansman haberi",
    "crypto": "Kripto bağlantılı haber",
    "filing": "SEC bildirimi",
}
# What happened, read from the wording (classify.outcomes) — never from the average tone.
_OUTCOME_REASON = {
    "earnings": {"beat": "Beklentileri aşan bilanço", "miss": "Beklentilerin altında bilanço"},
    "fda": {"approve": "FDA onayı haberi", "reject": "FDA'dan olumsuz karar"},
    "clinical": {"positive": "Olumlu klinik veri", "negative": "Olumsuz klinik sonuç"},
    "dividend": {"raise": "Temettü artırımı", "cut": "Temettü kesintisi"},
    "insider": {"buy": "İçeriden alım", "sell": "İçeriden satış"},
    "delisting": {"compliance": "Kotasyon uyumu yeniden sağlandı"},
    "index_change": {"removed": "Endeksten çıkarılma"},
}
_STRONG = {"fda", "clinical", "mna", "guidance_up", "guidance_down", "earnings", "offering", "halt", "bankruptcy",
           "short_report", "index_change", "activist", "accounting", "analyst_up", "analyst_down", "buyback",
           "reverse_split", "delisting", "contract"}
_SKIP_REASON = {"opinion", "market", "price_move", "ai", "macro", "fed", "geopolitics", "commodities"}
_MARKET_LEVEL = {"market", "macro", "fed", "geopolitics", "commodities"}   # not a ticker's own news in a wrap
_META_KEYS = ("title_tickers", "subject_tickers", "title_categories", "summary_categories", "roundup")
_META_CACHE: dict[str, dict] = {}


def _num(v):
    try:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v = re.sub(r"[^\d.\-eE]", "", v)
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _pct(v: float) -> str:
    """'+%8,20' / '-%8,00' (sign first, two decimals, Turkish decimal comma) - same form as briefs._pct."""
    return f"{'+' if v > 0 else '-' if v < 0 else ''}%{abs(v):.2f}".replace(".", ",")


_NOT_OUTLET = re.compile(r"\((?:NASDAQ|NYSE|AMEX|NYSEAMERICAN|NYSEARCA)[^)]*\)|\d{1,2}:\d{2}|[():]", re.I)


def _outlet_name(r: dict) -> str:
    """Display name of the newsroom behind a row for the 'sources' list: canonical publisher, never a collector
    label ('Google News: Borsa', 'Nasdaq Bilançolar') or a ticker / clock string. Falls back to the title's
    ' - Outlet' tail when the name is a collector label or not an outlet name."""
    name = publisher_name(r)
    src = str(r.get("source_name") or r.get("source") or "")
    if name and name != src and not _NOT_OUTLET.search(name):
        return name
    m = re.search(r"\s[-|–—]\s([^-|–—]{2,40})$", str(r.get("title") or ""))
    tail = m.group(1).strip() if m else ""
    if tail and not _NOT_OUTLET.search(tail):
        return tail
    return name or src


def _rows(data) -> list[dict]:
    """Tolerant extraction of [{symbol,...}] from snapshot shapes (list, {"items": [...]}, {sym: {...}})."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if not isinstance(data, dict):
        return []
    for k in ("items", "snapshots", "data", "quotes"):
        v = data.get(k)
        if isinstance(v, list):
            return [d for d in v if isinstance(d, dict)]
        if isinstance(v, dict):
            return [dict(d, symbol=d.get("symbol") or s) for s, d in v.items() if isinstance(d, dict)]
    return [dict(d, symbol=d.get("symbol") or s) for s, d in data.items()
            if isinstance(d, dict) and T.normalize_symbol(str(s))]


def _first(d: dict, *keys):
    for k in keys:
        v = _num(d.get(k))
        if v is not None:
            return v
    return None


def _price_map() -> dict[str, dict]:
    """symbol -> {price, change_pct, volume, avg_volume, pre_change_pct, lists}; first source wins per field."""
    out: dict[str, dict] = {}

    def put(raw: dict, lst: str | None = None) -> None:
        sym = str(raw.get("symbol") or raw.get("S") or "")
        if not sym or any(c in sym for c in "^=") or sym.endswith("-USD"):
            return
        s = T.normalize_symbol(sym)
        if not s:
            return
        cur = out.setdefault(s, {"lists": set()})
        vals = {
            "price": _first(raw, "price", "last", "latest_price", "close", "regularMarketPrice", "p"),
            "change_pct": _first(raw, "change_pct", "changePercent", "pct_change", "change_percent",
                                 "regularMarketChangePercent", "percent_change"),
            "volume": _first(raw, "volume", "daily_volume", "regularMarketVolume", "v"),
            "avg_volume": _first(raw, "avg_volume", "averageDailyVolume3Month", "average_volume"),
            "rel_volume": _first(raw, "rel_volume", "relative_volume"),
            "pre_change_pct": _first(raw, "pre_change_pct", "preMarketChangePercent", "premarket_change_pct"),
            "post_change_pct": _first(raw, "post_change_pct", "postMarketChangePercent"),
        }
        # Quotes carry the extended-hours move vs the last regular price as ext_change_pct + ext_session.
        ext = {"pre": "pre_change_pct", "post": "post_change_pct"}.get(str(raw.get("ext_session") or ""))
        if ext and vals[ext] is None:
            vals[ext] = _num(raw.get("ext_change_pct"))
        for k, v in vals.items():
            if v is not None and cur.get(k) is None:
                cur[k] = v
        if lst:
            cur["lists"].add(lst)

    for key in ("quotes", "alpaca_snapshots"):
        for r in _rows(db.get_snapshot_data(key)):
            put(r)
    for key in ("movers_alpaca", "movers"):
        data = db.get_snapshot_data(key) or {}
        if isinstance(data, dict):
            for lst in ("gainers", "losers", "actives", "small_caps"):
                for r in data.get(lst) or []:
                    if isinstance(r, dict):
                        put(r, lst)
            # Gapper rows describe the extended-hours move in change_pct/price (pre_/post_change_pct are set too);
            # their regular-session move is regular_change_pct.
            for lst in ("premarket", "postmarket"):
                for r in data.get(lst) or []:
                    if isinstance(r, dict):
                        put(dict(r, change_pct=r.get("regular_change_pct"), price=r.get("price")), lst)
    # Prices of followed symbols no screener lists (one v7 quote each); no list tag: it does not make a mover.
    mv = db.get_snapshot_data("movers") or {}
    extra = mv.get("quotes_extra") if isinstance(mv, dict) else None
    for r in (extra.values() if isinstance(extra, dict) else ()):
        if isinstance(r, dict):
            put(r)
    return out


def _social_map() -> tuple[dict[str, dict], set[str]]:
    data = db.get_snapshot_data("social") or {}
    ape: dict[str, dict] = {}
    for r in (data.get("apewisdom") or []) if isinstance(data, dict) else []:
        if not isinstance(r, dict):
            continue
        s = T.normalize_symbol(str(r.get("symbol") or r.get("ticker") or ""))
        if s and s not in ape:
            ape[s] = r
    st = set()
    for r in (data.get("stocktwits") or []) if isinstance(data, dict) else []:
        if isinstance(r, dict):
            s = T.normalize_symbol(str(r.get("symbol") or ""))
            if s:
                st.add(s)
    return ape, st


_DAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")


def _earnings_map(now: int) -> tuple[dict[str, dict], dict[str, dict], str, dict[str, float]]:
    """(today's earnings, next trading day's pre-market earnings, TR label of that day, reported EPS surprise % of
    the previous trading day and today) keyed by ticker, ET dates."""
    d = datetime.fromtimestamp(now, ET)
    start = datetime(d.year, d.month, d.day, tzinfo=ET)
    today, tomorrow, label, surprise = {}, {}, "Yarın", {}
    try:
        prev = start - timedelta(days=1)
        while not is_trading_day(prev.date()):
            prev -= timedelta(days=1)
        # Ordered by ts: a later report (today's) overrides the previous day's.
        for e in db.query_events(int(prev.timestamp()), int((start + timedelta(days=1)).timestamp()), kind="earnings"):
            sp = _num((e.get("extra") or {}).get("surprise_pct"))
            if e.get("ticker") and sp is not None:
                surprise[T.normalize_symbol(e["ticker"]) or e["ticker"]] = sp
        for e in db.query_events(int(start.timestamp()), int((start + timedelta(days=1)).timestamp()), kind="earnings"):
            if e.get("ticker"):
                today.setdefault(T.normalize_symbol(e["ticker"]) or e["ticker"], e)
        if d.hour >= 16 or not is_trading_day(d.date()):
            nxt = start + timedelta(days=1)
            while not is_trading_day(nxt.date()):
                nxt += timedelta(days=1)
            if (nxt.date() - d.date()).days > 1:
                label = _DAYS_TR[nxt.weekday()]
            for e in db.query_events(int(nxt.timestamp()), int((nxt + timedelta(days=1)).timestamp()), kind="earnings"):
                if e.get("ticker") and (e.get("extra") or {}).get("time") == "bmo":
                    tomorrow.setdefault(T.normalize_symbol(e["ticker"]) or e["ticker"], e)
    except Exception as e:  # noqa: BLE001
        log.warning("earnings lookup failed: %s", e)
    return today, tomorrow, label, surprise


def _new_agg() -> dict:
    return {"w": 0.0, "dir": 0.0, "sent": [], "sources": Counter(), "src_names": {}, "title_voice": {}, "items": [],
            "cat_w": Counter(), "cat_src": defaultdict(set), "cat_out": defaultdict(lambda: defaultdict(set)),
            "strong": 0.0, "halt": None}


# Official halts that matter for a day trader (news pending / exchange or SEC action), from the nasdaq_halts collector.
_HALT_CODES = {"T1", "T12", "H4", "H9", "H10", "H11"}


def _halt_text(reason_tr: str) -> str:
    """'İşlem durduruldu (haber bekleniyor)'; acronyms / exchange names keep their case ('SEC raporları ...')."""
    rt = " ".join(str(reason_tr or "").split())
    first = rt.split(" ", 1)[0] if rt else ""
    if first and not first.isupper() and first not in ("Nasdaq", "NYSE"):
        rt = rt[:1].lower() + rt[1:]
    return f"İşlem durduruldu ({rt})" if rt else "İşlem durduruldu"


def _row_meta(r: dict, watch: list[str]) -> dict:
    """Attribution data enrich stored in `extra`; recomputed (and cached) for rows enriched before it existed."""
    ex = r.get("extra") if isinstance(r.get("extra"), dict) else {}
    if all(k in ex for k in _META_KEYS):
        return ex
    m = _META_CACHE.get(r["id"])
    if m is None:
        title = strip_publisher(str(r.get("title") or ""), str(ex.get("publisher") or ""))
        try:
            m = derive_meta(title, str(r.get("summary") or ""), list(r.get("tickers") or []),
                            list(r.get("categories") or []), ex, watch, r.get("source_type") == "social")
        except Exception:  # noqa: BLE001 - never let one row break scoring
            log.debug("derive_meta failed for %s", r.get("id"), exc_info=True)
            m = {}
        if len(_META_CACHE) > 40000:
            _META_CACHE.clear()
        _META_CACHE[r["id"]] = m
    return dict(ex, **m)


def _ipo_calendar(now: int) -> set[str]:
    """Tickers with an IPO on the calendar (last 30 days .. next 14 days)."""
    try:
        return {T.normalize_symbol(e["ticker"]) for e in db.query_events(now - 30 * 86400, now + 14 * 86400, kind="ipo")
                if e.get("ticker") and T.normalize_symbol(e["ticker"])}
    except Exception as e:  # noqa: BLE001
        log.debug("ipo calendar lookup failed: %s", e)
        return set()


def _report_times(start: int, end: int) -> dict[str, list[int]]:
    """Ticker -> sorted earnings calendar timestamps in [start, end)."""
    out: dict[str, list[int]] = defaultdict(list)
    try:
        for e in db.query_events(start, end, kind="earnings"):
            if e.get("ticker"):
                out[T.normalize_symbol(e["ticker"]) or e["ticker"]].append(int(e["ts"]))
    except Exception as e:  # noqa: BLE001
        log.warning("earnings calendar lookup failed: %s", e)
    return out


def _pick_outcome(c: str, outs: dict) -> str | None:
    """The outcome reported by strictly more voices than any other one (mixed / preview do not vote)."""
    counts = sorted(((len(v), o) for o, v in outs.items() if o in _OUTCOME_REASON.get(c, {})), reverse=True)
    if counts and (len(counts) == 1 or counts[0][0] > counts[1][0]):
        return counts[0][1]
    return None


def compute(cfg: dict, store: bool = True) -> dict:
    """Opportunity snapshot (SPEC shape). Also stored under snapshot key "opportunities" unless store=False."""
    cfg = cfg or {}
    now = now_ts()
    window_start = min(last_close_ts(now) - 8 * 3600, now - 24 * 3600)
    watch = {s for s in (T.normalize_symbol(str(w)) for w in (cfg.get("watchlist") or [])) if s}
    universe = T.universe()
    ipo_cal = _ipo_calendar(now)
    # A report due within a week of an item is not yet what the item says (a preview is not a beat).
    report_ts = _report_times(window_start, now + 8 * 86400)

    def report_ahead(sym: str, published: int) -> bool:
        return any(published < ts <= published + 7 * 86400 for ts in report_ts.get(sym, ()))

    def eligible(sym: str) -> bool:
        if not sym:
            return False
        if T.is_etf(sym):
            return False   # index ETFs are on the market dashboard, not single-stock setups
        if sym in watch:
            return True
        return not universe or sym in universe

    agg: dict[str, dict] = {}
    watch_l = sorted(watch)
    for r in db.recent_news(window_start, limit=20000):
        title = str(r.get("title") or "")
        ex = r.get("extra") if isinstance(r.get("extra"), dict) else {}
        if ex.get("junk") or is_junk_title(title):
            continue
        syms = [s for s in (T.normalize_symbol(str(t)) for t in (r.get("tickers") or [])) if s]
        syms = [s for s in dict.fromkeys(syms) if eligible(s)]
        if not syms:
            continue
        meta = _row_meta(r, watch_l)
        meta_subj = meta.get("subject_tickers") or []
        title_t = [s for s in meta.get("title_tickers") or [] if s in syms]
        subj = [s for s in meta_subj if s in syms]
        if len(syms) == 1 and (not meta_subj or syms[0] in meta_subj):
            subj = syms   # the only ticker is the subject unless the title is about another company
        lead = subj[0] if subj else None

        imp = max(0.0, float(r.get("importance") or 0.0)) / 100.0
        age_h = max(0.0, (now - int(r["published_at"])) / 3600.0)
        decay = math.pow(0.5, age_h / HALF_LIFE_H)
        share = 1.0 if len(syms) == 1 else 1.0 / math.sqrt(len(syms))
        w = imp * decay * share
        cats_all = [c for c in (r.get("categories") or []) if c in CATEGORIES]
        opinion = "opinion" in cats_all or is_opinion(title)
        social = r.get("source_type") == "social"
        sent = float(r.get("sentiment") or 0.0)
        voice, vname = publisher_key(r), _outlet_name(r)
        nt = norm_title(r)

        # Categories this item can credit to its subject: from the title, or a clear summary sentence; discrete
        # events (IPO, M&A, FDA, ...) need the title or the subject named in the same sentence, never in roundups.
        tcats = set(meta.get("title_categories") or [])
        scats = meta.get("summary_categories") or {}
        roundup = bool(meta.get("roundup")) or len(meta_subj) >= 3

        def creditable(c: str, s: str) -> bool:
            if c == "opinion" or (roundup and c in _MARKET_LEVEL):
                return False
            if c in EVENT_CATEGORIES:
                if roundup:
                    return False
                ok = c in tcats or s in (scats.get(c) or ())
                if ok and c == "ipo":
                    # "Halka arz" only for the company actually going public.
                    ok = s in ipo_cal or bool(ex.get("ipo")) or (len(syms) == 1 and c in tcats)
                return ok
            return c in tcats or c in scats

        halt = None
        if r.get("source") == "nasdaq_halts" and not ex.get("resumed") and str(ex.get("reason_code") or "") in _HALT_CODES:
            halt = (int(r["published_at"]), _halt_text(str(ex.get("reason_tr") or "")))
        for s in syms:
            a = agg.setdefault(s, _new_agg())
            if halt and (a["halt"] is None or halt[0] > a["halt"][0]):
                a["halt"] = halt
            if s in subj:
                role, ww = "subject", w * (1.0 if s == lead else 0.85)
            elif s in title_t:
                role, ww = "title", w * 0.5     # named in the title, but the story is about another company
            else:
                role, ww = "other", w * 0.3     # only in the summary / source tags
            if social:
                ww *= 0.3                       # buzz: feeds weight and the Reddit/StockTwits reasons only
            elif opinion:
                ww *= 0.5
            a["w"] += ww
            a["items"].append((float(r.get("importance") or 0.0), r["published_at"], r["id"]))
            if social or role == "other":
                continue
            v = a["title_voice"].setdefault(nt, voice) if len(nt) >= 12 else voice   # syndicated copy = one voice
            a["sources"][v] += 1
            a["src_names"].setdefault(v, vname)
            if role != "subject":
                continue
            a["sent"].append(sent)
            if opinion:
                a["dir"] += ww * 0.65 * sent
                continue
            cats = [c for c in cats_all if creditable(c, s)]
            bias = max(-1.0, min(1.0, sum(CATEGORIES[c]["bias"] for c in cats[:3])))
            a["dir"] += ww * max(-1.0, min(1.0, 0.65 * sent + 0.35 * bias))
            for c in cats:
                a["cat_w"][c] += ww
                a["cat_src"][c].add(v)
                o = meta.get(c + "_outcome")
                if o and c == "earnings" and o in ("beat", "miss", "mixed") and report_ahead(s, int(r["published_at"])):
                    o = None                 # the reported number does not exist yet for this ticker
                if o:
                    a["cat_out"][c][o].add(v)
                if c in _STRONG and imp >= 0.55:
                    a["strong"] = max(a["strong"], imp * decay)

    prices = _price_map()
    ape, stwits = _social_map()
    earn_today, earn_next, next_label, surprises = _earnings_map(now)
    session = market_session(now)

    def session_move(p: dict):
        """(move %, TR label, bot code premarket | afterhours | open | last_session)."""
        if session == "premarket" and p.get("pre_change_pct") is not None:
            return p["pre_change_pct"], "Piyasa öncesi", "premarket"
        if session == "afterhours" and p.get("post_change_pct") is not None:
            return p["post_change_pct"], "Piyasa sonrası", "afterhours"
        if session == "open":
            return p.get("change_pct"), "Gün içi", "open"
        return p.get("change_pct"), "Son seans", "last_session"

    # Tickers without news can still qualify: big movers, Reddit spikes, watchlist names reporting today.
    for s, p in prices.items():
        chg = session_move(p)[0]
        if chg is not None and abs(chg) >= 5 and p.get("lists") and eligible(s):
            agg.setdefault(s, _new_agg())
    for s, r in list(ape.items())[:25]:
        if eligible(s):
            agg.setdefault(s, _new_agg())
    for s in earn_today:
        if s in watch and eligible(s):
            agg.setdefault(s, _new_agg())

    items = []
    for s, a in agg.items():
        reasons: list[tuple[float, str]] = []
        n_src = len(a["sources"])
        news_part = 60.0 * (1.0 - math.exp(-a["w"] / 0.9))
        score = news_part + min(14.0, 4.5 * max(0, n_src - 1)) + 10.0 * a["strong"]
        avg_sent = sum(a["sent"]) / len(a["sent"]) if a["sent"] else 0.0

        p = prices.get(s) or {}
        price, vol, avg_vol = p.get("price"), p.get("volume"), p.get("avg_volume")
        move, label, move_code = session_move(p)
        e = earn_today.get(s)
        e_time = (e.get("extra") or {}).get("time") if e else None
        report_pending = s in earn_next or (e is not None and e_time == "amc" and session in ("premarket", "open"))

        reason_cats: list[str] = []
        for c, cw in a["cat_w"].most_common():
            if c in _SKIP_REASON or c not in _REASON or len(reason_cats) >= 3:
                continue
            outs = a["cat_out"].get(c) or {}
            o = _pick_outcome(c, outs)
            text = _OUTCOME_REASON[c][o] if o else _REASON[c]
            if c == "earnings" and not o:
                previews = len(outs.get("preview") or ())
                if report_pending or (previews and previews >= len(a["cat_src"][c])):
                    text = "Bilanço beklentisi"
            k = len(a["cat_src"][c])
            reasons.append((cw * 100, f"{text} ({k} kaynak)" if k >= 2 else text))
            reason_cats.append(c)
            if c == "earnings" and move is not None:
                if o == "beat" and move <= -5:
                    reasons.append((cw * 100 + 5, "Güçlü bilançoya rağmen satış"))
                elif o == "miss" and move >= 5:
                    reasons.append((cw * 100 + 5, "Zayıf bilançoya rağmen yükseliş"))
        if a["halt"]:
            # The specific official halt reason replaces the generic "İşlem durdurma" category text.
            reasons = [(w, t) for w, t in reasons if not t.startswith(_REASON["halt"])]
            reasons.append((45.0, a["halt"][1]))
        if n_src >= 3:
            reasons.append((n_src * 8.0, f"{n_src} farklı kaynakta haber"))
        if len(a["sent"]) >= 2 and abs(avg_sent) >= 0.35:
            reasons.append((abs(avg_sent) * 20, "Haber tonu güçlü olumlu" if avg_sent > 0 else "Haber tonu güçlü olumsuz"))

        social_rank = social_change = None
        r = ape.get(s)
        if r:
            rank = _num(r.get("rank"))
            prev = _num(r.get("rank_24h_ago"))
            ment = _num(r.get("mentions"))
            ment_prev = _num(r.get("mentions_24h_ago"))
            social_rank = int(rank) if rank is not None else None
            if rank is not None and prev is not None:
                social_change = int(prev - rank)
            if rank is not None:
                score += 8 if rank <= 10 else 5 if rank <= 25 else 2 if rank <= 100 else 0
            ratio = (ment / ment_prev) if ment and ment_prev else None
            if ratio and ratio >= 2 and ment >= 15:
                score += 9 if ratio >= 4 else 6
                reasons.append((ratio * 6, f"Reddit'te bahsedilme {ratio:.0f} kat arttı"))
            elif rank is not None and rank <= 10:
                reasons.append((14.0, f"Reddit'te en çok konuşulanlar arasında (#{int(rank)})"))
            elif social_change is not None and social_change >= 15 and rank is not None and rank <= 60:
                reasons.append((10.0, f"Reddit sıralamasında {social_change} basamak yükseldi"))
        if s in stwits:
            score += 3
            reasons.append((6.0, "StockTwits'te trend"))

        if move is not None:
            am = abs(move)
            score += 15 if am >= 25 else 12 if am >= 12 else 8 if am >= 6 else 4 if am >= 3 else 0
            if am >= 3:
                reasons.append((am * 2.5, f"{label} {_pct(move)}"))
        rv = vol / avg_vol if vol and avg_vol and avg_vol > 0 else p.get("rel_volume")
        if rv:
            if rv >= 2:
                score += 7 if rv >= 5 else 4
                reasons.append((rv * 4, f"Hacim ortalamanın {rv:.1f} katı".replace(".", ",")))

        surp = surprises.get(s)
        if surp is not None and abs(surp) >= 5:
            # Reported EPS vs. the estimate: a catalyst, never a direction by itself (beats get sold, misses bought).
            score += 4 + min(2.0, (abs(surp) - 5) / 10)
            reasons.append((25.0 + min(15.0, abs(surp) / 4),
                            f"EPS beklentiyi %{min(abs(surp), 999):.2f} {'aştı' if surp > 0 else 'kaçırdı'}".replace(".", ",")))
        if e:
            score += 7
            txt = {"bmo": "Bugün bilanço (açılış öncesi)", "amc": "Bugün bilanço (kapanış sonrası)"}.get(e_time, "Bugün bilanço")
            reasons.append((30.0, txt))
        elif s in earn_next:
            score += 4
            reasons.append((20.0, f"{next_label} bilanço (açılış öncesi)"))
        if s in watch:
            score += 4

        dir_news = a["dir"] / a["w"] if a["w"] > 0 else 0.0
        comb = dir_news if move is None else (0.75 * dir_news + 0.25 * max(-1.0, min(1.0, move / 8.0))
                                             if a["w"] > 0 else max(-1.0, min(1.0, move / 8.0)))
        direction = "long" if comb >= 0.15 else "short" if comb <= -0.15 else "mixed"

        reasons.sort(key=lambda x: -x[0])
        texts = list(dict.fromkeys(t for _, t in reasons))[:6]
        if s in watch:
            texts.append("İzleme listesinde")
        top_items = sorted(a["items"], key=lambda x: (-x[0], -x[1]))
        # Tags = the reason categories first, then the rest of the credited categories.
        cats_out = list(dict.fromkeys(reason_cats + [c for c, _ in a["cat_w"].most_common() if c != "opinion"]))[:6]
        news_ts = [t for _, t, _ in a["items"] if t]
        codes = list(cats_out)
        if a["halt"] and "halt" not in codes:
            codes.append("halt")
        if move is not None and abs(move) >= 3:
            codes.append("gap")
        if rv and rv >= 2:
            codes.append("rvol")
        if e:
            codes.append("earnings_today")
        if s in earn_next:
            codes.append("earnings_next")
        if surp is not None and abs(surp) >= 5:
            codes.append("earnings_surprise")
        if r or s in stwits:
            codes.append("social")
        if s in watch:
            codes.append("watchlist")
        items.append({
            "ticker": s,
            "company": T.company_name(s),
            "score": round(max(0.0, min(100.0, score)), 1),
            "direction": direction,
            "reasons": texts,
            "categories": cats_out,
            "sources": [a["src_names"].get(v, v) for v, _ in a["sources"].most_common()],
            "news_ids": list(dict.fromkeys(i for _, _, i in top_items))[:12],
            "news_count": len(a["items"]),
            "sentiment_avg": round(avg_sent, 3),
            "social_rank": social_rank,
            "social_change": social_change,
            "price": round(price, 4) if price is not None else None,
            "change_pct": round(move, 2) if move is not None else None,
            "volume": int(vol) if vol is not None else None,
            "in_watchlist": s in watch,
            "earnings_today": bool(e),
            # bot fields (db.append_signals copies them into `signals`)
            "rel_volume": round(float(rv), 2) if rv else None,
            "first_news_ts": min(news_ts) if news_ts else None,
            "last_news_ts": max(news_ts) if news_ts else None,
            "move_label": move_code if move is not None else None,
            "reason_codes": list(dict.fromkeys(codes)),
        })
    items.sort(key=lambda x: (-x["score"], -x["news_count"], x["ticker"]))
    snap = {"ts": now, "window_start": window_start, "items": items[:TOP_N]}
    if store:
        try:
            db.set_snapshot("opportunities", snap)
        except Exception as ex:  # noqa: BLE001
            log.warning("could not store opportunities snapshot: %s", ex)
    return snap


__all__ = ["compute"]
