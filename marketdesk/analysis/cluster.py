"""Cross-source story clustering ("kim ne diyor"): same story from different outlets grouped by title
similarity, shared tickers and a time window."""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from functools import lru_cache

from .. import db
from ..util import now_ts
from . import tickers as T
from .classify import is_junk_title

try:  # the collectors' outlet normalisation (display names, provider / URL-domain fallbacks)
    from ..collectors.rss import canonical_publisher, normalize_publisher
except Exception:  # noqa: BLE001 - analysis must keep working without the collectors package
    canonical_publisher = normalize_publisher = None

log = logging.getLogger("marketdesk.analysis")

WINDOW = 18 * 3600          # an item may join a cluster up to 18 h after the cluster's first item
JACCARD = 0.5
JACCARD_TICKER = 0.3
MAX_COMPARE = 12            # members compared per candidate cluster (first + most recent)

_STOP = set("""
a an the and or but if of to in on at by for from with without into onto over under about above below after before
as is are was were be been being has have had do does did will would can could should may might must shall this that
these those it its it's he she they them their his her we our you your i me my not no yes so than then there here
what which who whom whose why how when where all any both each few more most other some such only own same too very
just also now new says said say report reports reported update updates news today week year years day days month
stock stocks share shares inc corp co company companies ltd plc llc group holdings via amid after vs versus per
one two three four five six seven eight nine ten first second third fourth top big here's what's why's live
yahoo finance reuters bloomberg cnbc marketwatch investing com barron's barrons motley fool zacks benzinga
reserve plug hold holds
transcript transcripts call calls conference webcast announce announces announced announcing date dates release
releases released financial financials result fact facts note notes takeaway takeaways key investor investors long-term
""".split())
_SYN = {
    **dict.fromkeys("soar soars soared soaring surge surges surged surging jump jumps jumped jumping rally rallies "
                    "rallied rallying climb climbs climbed climbing rise rises rose rising gain gains gained gaining "
                    "spike spikes spiked pop pops popped rocket rockets rocketed skyrocket skyrockets skyrocketed "
                    "higher up advance advances advanced".split(), "up"),
    **dict.fromkeys("plunge plunges plunged plunging tumble tumbles tumbled tumbling sink sinks sank sinking fall "
                    "falls fell falling drop drops dropped dropping slide slides slid sliding slump slumps slumped "
                    "plummet plummets plummeted crash crashes crashed tank tanks tanked dive dives dived slip slips "
                    "slipped lower down decline declines declined crater craters cratered".split(), "down"),
    **dict.fromkeys("acquire acquires acquired acquiring acquisition acquisitions buyout takeover".split(), "acq"),
    **dict.fromkeys("guidance outlook forecast forecasts".split(), "guid"),
    **dict.fromkeys("q1 q2 q3 q4 quarter quarterly first-quarter second-quarter third-quarter fourth-quarter".split(), "qtr"),
    **dict.fromkeys("upgrade upgrades upgraded".split(), "upgrad"),
    **dict.fromkeys("downgrade downgrades downgraded".split(), "downgrad"),
    **dict.fromkeys("earnings results profit profits".split(), "earn"),
    **dict.fromkeys("fed federal fomc".split(), "fed"),
    **dict.fromkeys("approval approves approved approve".split(), "approv"),
    **dict.fromkeys("cut cuts cutting lower lowers lowered lowering slash slashes slashed reduce reduces".split(), "cut"),
    **dict.fromkeys("raise raises raised raising hike hikes hiked lift lifts lifted boost boosts boosted".split(), "raise"),
    **dict.fromkeys("scrap scraps scrapped scrapping shelve shelves shelved cancel cancels canceled cancelled axe axes "
                    "axed pause pauses paused withdraw withdraws withdrew".split(), "cancel"),
    **dict.fromkeys("leak leaks leaked".split(), "leak"),
    **dict.fromkeys("layoff layoffs jobs job workforce headcount staff".split(), "jobs"),
}
# Template boilerplate that makes unrelated companies' headlines look alike.
_BOILER = re.compile(r"\b(?:(?:Q[1-4]|FY|fiscal)\s*(?:\d{4}\s+)?)?(?:Earnings Call Transcript|Earnings Conference Call|"
                     r"Earnings Call|Conference Call Transcript)\b", re.I)
_RX_YEAR = re.compile(r"(?:19|20)\d\d")
_RX_TOK = re.compile(r"[a-z0-9$][a-z0-9$&'\-]*")


def _stem(w: str) -> str:
    if w in _SYN:
        return _SYN[w]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _clean_title(item: dict) -> str:
    title = item.get("title") or ""
    pub = str((item.get("extra") or {}).get("publisher") or "")
    if pub:
        for sep in (" - ", " | ", " — ", " – "):
            if title.lower().endswith((sep + pub).lower()):
                return title[: -len(sep + pub)]
    return title


def tokens(item: dict) -> set[str]:
    out = set()
    for raw in _RX_TOK.findall(_BOILER.sub(" ", _clean_title(item)).lower().replace("’", "'")):
        raw = raw.strip("'-")
        if raw.endswith("'s"):
            raw = raw[:-2]
        if raw in _STOP or raw[:1] == "$":
            continue   # "$0.03" is an amount; ticker tokens come from item["tickers"]
        for w in (raw.split("-") if "-" in raw else (raw,)):
            if len(w) >= 2 and w not in _STOP and not _RX_YEAR.fullmatch(w):
                out.add(_stem(w))
    for t in item.get("tickers") or []:
        out.add("$" + str(t).lower())
    return out


def norm_title(item: dict) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_title(item).lower()).strip()


# ---------------------------------------------------------------- publishers ("voices")
# One outlet reached through several collectors (Google News, Yahoo, Nasdaq syndication, two WSJ feeds) is one voice.
_PUB_ALIAS = {
    "motley fool": "motley fool", "motley fool transcribing": "motley fool", "motley fool staff": "motley fool",
    "fool": "motley fool", "investor's business daily": "ibd", "investors business daily": "ibd", "ibd": "ibd",
    "wall street journal": "wsj", "wsj": "wsj", "dow jones newswires": "wsj", "dow jones newswires staff": "wsj",
    "24/7 wall st": "247wallst", "24/7 wall st.": "247wallst", "247wallst": "247wallst", "simply wall st": "simplywallst",
    "simply wall street": "simplywallst", "simplywall.st": "simplywallst", "stocktwits": "stocktwits",
    "yahoo finance": "yahoo finance", "yahoo": "yahoo finance", "kalkine media": "kalkine", "kalkine": "kalkine",
    "barchart": "barchart", "reuters": "reuters", "bloomberg": "bloomberg", "cnbc": "cnbc", "marketwatch": "marketwatch",
    "barron's": "barrons", "barrons": "barrons", "seeking alpha": "seeking alpha", "seekingalpha": "seeking alpha",
    "benzinga": "benzinga", "thestreet": "thestreet", "the street": "thestreet", "thestreet pro": "thestreet",
    "investing": "investing.com", "investing.com": "investing.com", "zacks": "zacks", "rttnews": "rttnews",
    "fintel": "fintel", "marketbeat": "marketbeat", "tradingview": "tradingview", "stock titan": "stock titan",
    "globenewswire": "globenewswire", "pr newswire": "pr newswire", "prnewswire": "pr newswire",
    "business wire": "business wire", "businesswire": "business wire", "accesswire": "access newswire",
    "access newswire": "access newswire", "tmx newsfile": "newsfile", "newsfile": "newsfile", "nasdaq": "nasdaq",
    "financial times": "ft", "ft": "ft", "fox business": "fox business", "business insider": "business insider",
    "associated press": "ap", "ap": "ap", "tipranks": "tipranks", "gurufocus": "gurufocus", "moomoo": "moomoo",
    "investorplace": "investorplace", "morningstar": "morningstar", "cnn": "cnn", "axios": "axios", "quartz": "quartz",
}
_COLLECTOR_OUTLET = (
    ("wsj_", "wsj"), ("marketwatch_", "marketwatch"), ("cnbc_", "cnbc"), ("bloomberg_", "bloomberg"),
    ("globenewswire", "globenewswire"), ("prnewswire", "pr newswire"), ("businesswire", "business wire"),
    ("accessnewswire", "access newswire"), ("nasdaq_", "nasdaq"), ("investing_", "investing.com"), ("ibd", "ibd"),
    ("fool", "motley fool"), ("seekingalpha", "seeking alpha"), ("ft_", "ft"), ("reddit_", "reddit"),
    ("stocktwits", "stocktwits"), ("businessinsider", "business insider"), ("foxbusiness", "fox business"),
    ("barrons", "barrons"), ("benzinga", "benzinga"), ("thestreet", "thestreet"), ("reuters", "reuters"),
    ("yahoo_finance", "yahoo finance"), ("newsfile", "newsfile"),
)
_KNOWN_OUTLETS = set(_PUB_ALIAS.values()) | {o for _, o in _COLLECTOR_OUTLET}
_RX_PUB_COUNTRY = re.compile(
    r"(?:\s+(?:uk|us|usa|canada|australia|singapore|india|south africa|france|germany|españa|espana|deutschland|italia|"
    r"japan|nz|new zealand|ireland|philippines|malaysia|hong kong|nigeria|asia|europe|middle east))+$")


@lru_cache(maxsize=8192)
def _norm_pub(name: str) -> str:
    s = str(name or "").lower().replace("’", "'").strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s).replace("yahoo!", "yahoo")
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" /.-,")
    s = _RX_PUB_COUNTRY.sub("", s)
    s = re.sub(r"\.(?:com|co|org|net|io)(?:\.\w\w)?$", "", s) if s not in _PUB_ALIAS else s
    return _PUB_ALIAS.get(s, s)


def _collector_outlet(r: dict) -> str:
    src = str(r.get("source") or "")
    for prefix, outlet in _COLLECTOR_OUTLET:
        if src.startswith(prefix):
            return outlet
    return _norm_pub(str(r.get("source_name") or src))


def _canonical(r: dict) -> str:
    """collectors.rss.canonical_publisher(r) when it found an outlet of its own (API provider, known URL domain),
    else ''."""
    if canonical_publisher is None:
        return ""
    try:
        cp = str(canonical_publisher(r) or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return "" if cp in (str(r.get("source_name") or ""), str(r.get("source") or "")) else cp


def _display(name: str) -> str:
    if normalize_publisher is None:
        return name
    try:
        return normalize_publisher(name) or name
    except Exception:  # noqa: BLE001
        return name


def publisher_key(r: dict) -> str:
    """Normalized outlet behind a news row: an author that names an outlet ("The Motley Fool", "Reuters"), else
    extra.publisher (relay collectors), else an API provider / known URL domain (old rows without extra.publisher),
    else the collector's own outlet (all "WSJ ..." feeds -> wsj)."""
    author = _norm_pub(str(r.get("author") or ""))
    if author in _KNOWN_OUTLETS:
        return author
    extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
    pub = _norm_pub(str(extra.get("publisher") or ""))
    if pub:
        return pub
    cp = _canonical(r)
    return _norm_pub(cp) if cp else _collector_outlet(r)


def publisher_name(r: dict) -> str:
    """Display name for publisher_key(r) ('The Motley Fool' -> 'Motley Fool', 'Yahoo! Finance UK' -> 'Yahoo Finance')."""
    extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
    author = str(r.get("author") or "").strip()
    if author and _norm_pub(author) in _KNOWN_OUTLETS:
        return _display(author)
    pub = str(extra.get("publisher") or "").strip()
    if pub:
        return _display(pub)
    return _canonical(r) or str(r.get("source_name") or r.get("source") or "")


def _is_relay(r: dict) -> bool:
    return publisher_key(r) != _collector_outlet(r)


def voices(rows: list[dict]) -> list[list[dict]]:
    """Rows grouped into independent voices: same publisher, or an identical title (a syndicated copy).
    Each group is sorted so its first row is the direct (non-social, non-relay) collector's copy, else the earliest."""
    parent: dict[str, str] = {}

    def find(v: str) -> str:
        while parent.setdefault(v, v) != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    keys = [publisher_key(r) for r in rows]
    first_by_title: dict[str, str] = {}
    for r, k in zip(rows, keys):
        find(k)
        nt = norm_title(r)
        if len(nt) >= 12:
            other = first_by_title.setdefault(nt, k)
            if other != k:
                parent[find(k)] = find(other)
    groups: dict[str, list[dict]] = {}
    for r, k in zip(rows, keys):
        groups.setdefault(find(k), []).append(r)
    out = [sorted(g, key=lambda r: (r.get("source_type") == "social", _is_relay(r), r["published_at"], r["id"]))
           for g in groups.values()]
    out.sort(key=lambda g: min(r["published_at"] for r in g))
    return out


# ---------------------------------------------------------------- clustering

_RX_LEAD = re.compile(r"^[^:(|]*?(?=\s+(?-i:[a-z])|\s*[:(|]|$)")


def _lead_tokens(item: dict) -> frozenset[str]:
    """Tokens of the headline's leading proper-noun span (text before the first lowercase word, '(' or ':')."""
    m = _RX_LEAD.match(_clean_title(item))
    return frozenset(t for t in tokens({"title": m.group(0)}) if t[0] != "$") if m else frozenset()


def _tick_set(toks: set[str]) -> frozenset[str]:
    """Non-ETF ticker tokens of a title ($spy / $qqq are context, not the story's subject)."""
    return frozenset(t for t in toks if t[0] == "$" and not T.is_etf(t[1:].upper()))


def assign_clusters(since_hours: int = 48) -> int:
    """Greedy time-ordered clustering of recent news; writes cluster_id (= id of the earliest item) for rows whose
    value changed. Returns the number of rows updated."""
    now = now_ts()
    cutoff = now - since_hours * 3600
    rows = db.recent_news(cutoff - WINDOW, limit=50000)
    rows.sort(key=lambda r: (r["published_at"], r["id"]))
    clusters: list[dict] = []
    by_token: dict[str, list[int]] = {}
    by_norm: dict[str, int] = {}
    active = 0
    assign: dict[str, str] = {}
    for r in rows:
        if is_junk_title(r.get("title") or ""):
            assign[r["id"]] = r["id"]   # quote / option-chain pages stay singletons
            continue
        ts = r["published_at"]
        while active < len(clusters) and ts - clusters[active]["first_ts"] > WINDOW:
            active += 1
        toks = tokens(r)
        tset = _tick_set(toks)
        lead = _lead_tokens(r)
        norm = norm_title(r)
        tick = set(r.get("tickers") or [])
        cid = by_norm.get(norm) if len(norm) >= 12 else None
        if cid is None and toks:
            seen: set[int] = set()
            best, best_j = None, 0.0
            for t in toks:
                lst = by_token.get(t)
                if not lst:
                    continue
                if len(lst) > 64:
                    lst[:] = [ci for ci in lst if ci >= active]
                for ci in reversed(lst):
                    if ci < active or ci in seen:
                        continue
                    seen.add(ci)
                    c = clusters[ci]
                    shared = bool(c["tickers"] & tick)
                    members = c["members"]
                    for mt, ms, ml in (members[:1] + members[-MAX_COMPARE:]) if len(members) > MAX_COMPARE else members:
                        common = toks & mt
                        inter = len(common)
                        if inter < 2:
                            continue
                        j = inter / len(toks | mt)
                        if j < 0.75 and ((tset and ms and not (tset & ms))
                                         or ((not tset or not ms) and lead and ml and not (lead & ml))):
                            continue   # different companies in a look-alike headline template
                        words = sum(1 for x in common if x[0] != "$")
                        if ((j >= JACCARD and inter >= 3) or (shared and j >= JACCARD_TICKER and words >= 2)
                                or (inter >= 5 and j >= 0.4) or (j >= 0.75 and words >= 2)):
                            if j > best_j:
                                best, best_j = ci, j
            cid = best
        if cid is None:
            cid = len(clusters)
            clusters.append({"id": r["id"], "first_ts": ts, "members": [], "tickers": set()})
        c = clusters[cid]
        c["members"].append((toks, tset, lead))
        c["tickers"] |= tick
        for t in toks:
            lst = by_token.setdefault(t, [])
            if not lst or lst[-1] != cid:
                lst.append(cid)
        if len(norm) >= 12:
            by_norm.setdefault(norm, cid)
        assign[r["id"]] = c["id"]
    updates = [(r["id"], {"cluster_id": assign[r["id"]]}) for r in rows
               if r["published_at"] >= cutoff and r.get("cluster_id") != assign[r["id"]]]
    if updates:
        db.update_news_fields(updates)
    log.debug("clusters: %d rows, %d clusters, %d updated", len(rows), len(clusters), len(updates))
    return len(updates)


def list_clusters(since_hours: int = 24, min_sources: int = 2, limit: int = 100) -> list[dict]:
    """Multi-source stories: {cluster_id, title, tickers, categories, first_seen, last_seen, sentiment_avg,
    importance_max, source_count, collector_count, item_count, labels, agreement, items}. source_count counts
    independent voices (publishers; syndicated identical titles count once); items = one row per voice (the direct
    collector's copy, else the earliest) with `publisher` and `collectors` (every feed that carried it)."""
    now = now_ts()
    rows = db.recent_news(now - since_hours * 3600, limit=20000)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if not is_junk_title(r.get("title") or ""):
            groups.setdefault(r.get("cluster_id") or r["id"], []).append(r)
    out = []
    for cid, items in groups.items():
        if len(items) < max(1, min_sources):
            continue
        items.sort(key=lambda r: (r["published_at"], r["id"]))
        vs = voices(items)
        if len(vs) < max(1, min_sources):
            continue
        uniq = []
        for g in vs:
            rep = dict(g[0], publisher=publisher_name(g[0]),
                       collectors=list(dict.fromkeys(r.get("source_name") or r["source"] for r in g)))
            uniq.append(rep)
        main = [r for r in uniq if r.get("source_type") != "social"] or uniq
        head = max(main, key=lambda r: (r.get("importance") or 0, -r["published_at"]))
        tick = Counter(t for r in uniq for t in (r.get("tickers") or []))
        cats = Counter(c for r in uniq for c in (r.get("categories") or []) if c != "opinion")
        labels = Counter(r.get("sentiment_label") or "neu" for r in uniq)
        sents = [float(r.get("sentiment") or 0.0) for r in uniq]
        imp_max = max(float(r.get("importance") or 0.0) for r in items)
        last_seen = max(r["published_at"] for r in items)
        age_h = max(0.0, (now - last_seen) / 3600)
        rank = (imp_max + min(20.0, 4.0 * (len(uniq) - 1))) * math.pow(0.5, age_h / 12)
        out.append({
            "cluster_id": cid,
            "title": _clean_title(head),
            "tickers": [t for t, _ in tick.most_common(6)],
            "categories": [c for c, _ in cats.most_common(6)],
            "first_seen": items[0]["published_at"],
            "last_seen": last_seen,
            "sentiment_avg": round(sum(sents) / len(sents), 3),
            "importance_max": round(imp_max, 1),
            "source_count": len(uniq),
            "collector_count": len({r["source"] for r in items}),
            "item_count": len(items),
            "labels": {k: labels.get(k, 0) for k in ("pos", "neg", "neu")},
            "agreement": round(labels.most_common(1)[0][1] / len(uniq), 2),
            "items": uniq,
            "_rank": rank,
        })
    out.sort(key=lambda c: -c["_rank"])
    for c in out:
        c.pop("_rank", None)
    return out[:limit]


def cluster_sizes(cluster_ids) -> dict[str, tuple[int, int]]:
    """{cluster_id: (rows, independent voices)} for the given clusters ("+N kaynak" badges)."""
    cids = [c for c in dict.fromkeys(cluster_ids or ()) if c]
    out: dict[str, tuple[int, int]] = {}
    c = db.conn()
    for i in range(0, len(cids), 500):
        chunk = cids[i:i + 500]
        by: dict[str, list[dict]] = {}
        for r in c.execute(f"SELECT id, cluster_id, source, source_name, author, title, extra, published_at FROM news "
                           f"WHERE cluster_id IN ({','.join('?' * len(chunk))})", chunk):
            d = dict(r)
            try:
                d["extra"] = json.loads(d.get("extra") or "{}")
            except (TypeError, ValueError):
                d["extra"] = {}
            by.setdefault(d["cluster_id"], []).append(d)
        for cid, rows in by.items():
            out[cid] = (len(rows), len(voices(rows)))
    return out


__all__ = ["assign_clusters", "list_clusters", "cluster_sizes", "tokens", "norm_title", "publisher_key",
           "publisher_name", "voices"]
