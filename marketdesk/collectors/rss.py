"""RSS/Atom collectors: media, press-release wires, official agencies, Google News searches,
per-watchlist and per-mover ticker feeds, Nasdaq trading halts. One Collector per feed so every source gets
its own column in the UI."""
from __future__ import annotations

import concurrent.futures as cf
import html
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

from .. import util
from .base import Collector

try:  # quote / option-chain / chart pages are not news (same rule the analysis flags with extra.junk)
    from ..analysis.classify import is_junk_title
except Exception:  # noqa: BLE001 - collectors must keep working without the analysis package
    _JUNK_FALLBACK = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b|stock price, news, quote|interactive stock chart", re.I)

    def is_junk_title(title: str) -> bool:
        return bool(title and _JUNK_FALLBACK.search(title))

log = logging.getLogger("marketdesk.collectors.rss")

MAX_AGE = 3 * 86400
MAX_ITEMS = 100
TIMEOUT = 15
FETCH_BUDGET = 45          # seconds a multi-request fetch may spend before giving up on stragglers
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?$")
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "OTC", "OTCQB", "OTCQX", "OTCBB", "OTCPK", "OTCID", "CBOE", "BATS",
                "NYSEAMERICAN", "NYSEARCA", "NYSEMKT"}

# "(NASDAQ: ABCD)", "(NYSE American: Q)", "(TSXV: X; OTCQB: ABCD)", "(Nasdaq Global Select Market: XYZ, XYZW)" ...
_EXCH_RE = re.compile(
    r"(?:[(\[;,|]|\band\b)\s*(?i:(nasdaq|nyse|amex|otc(?:qb|qx|bb|pk|id)?|cboe|bats))"
    r"(?:[\s\-]+(?i:american|arca|mkt|global|select|capital|markets?|gs|gm|cm|pink|expert|bzx|us|stock|exchange)){0,4}"
    r"\s*[:：]\s*\$?([A-Z]{1,5}(?:\.[A-Z]{1,2})?)(?![A-Za-z])")
_EXCH_REV_RE = re.compile(r"\(([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\s*:\s*(?i:nasdaq|nyse|amex|otc[a-z]*|cboe)\)")  # (CUBI:NYSE)
_EXCH_MORE_RE = re.compile(r"\s*[,/]\s*([A-Z]{1,5}(?:\.[A-Z]{1,2})?)(?![A-Za-z:])")
_BOILERPLATE_RE = re.compile(r"The post .{0,300}? appeared first on .{0,80}?$", re.S)
_TRACKING_PARAM_RE = re.compile(r"^(?:\.tsrc|feedref|feed_item_type|source=iedfolrf|rss=|x-yahoo)", re.I)
_DATE_FMTS = ("%b %d, %Y %H:%M GMT", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%B %d, %Y %I:%M %p")

PUBLISHERS = {  # domain -> display name for items whose feed does not name the publisher
    "fool.com": "Motley Fool", "finance.yahoo.com": "Yahoo Finance", "yahoo.com": "Yahoo",
    "reuters.com": "Reuters", "bloomberg.com": "Bloomberg", "cnbc.com": "CNBC", "marketwatch.com": "MarketWatch",
    "wsj.com": "WSJ", "barrons.com": "Barron's", "investors.com": "Investor's Business Daily",
    "seekingalpha.com": "Seeking Alpha", "benzinga.com": "Benzinga", "zacks.com": "Zacks", "thestreet.com": "TheStreet",
    "investopedia.com": "Investopedia", "businessinsider.com": "Business Insider", "insidermonkey.com": "Insider Monkey",
    "simplywall.st": "Simply Wall St", "gurufocus.com": "GuruFocus", "tipranks.com": "TipRanks",
    "prnewswire.com": "PR Newswire", "globenewswire.com": "GlobeNewswire", "businesswire.com": "Business Wire",
    "accessnewswire.com": "ACCESS Newswire", "investing.com": "Investing.com", "nasdaq.com": "Nasdaq",
    "ft.com": "Financial Times", "foxbusiness.com": "Fox Business", "moby.co": "Moby", "motleyfool.com": "Motley Fool",
    "marketbeat.com": "MarketBeat", "barchart.com": "Barchart", "rttnews.com": "RTTNews", "fintel.io": "Fintel",
    "247wallst.com": "24/7 Wall St.", "stocktwits.com": "Stocktwits", "newsfilecorp.com": "Newsfile",
    "markets.businessinsider.com": "Business Insider", "apnews.com": "AP",
}

# Spelling variants of one newsroom -> one canonical name (matched on the lower-cased name without "The ").
_PUB_PATTERNS: list[tuple[re.Pattern, str]] = [(re.compile(p), name) for p, name in (
    (r"yahoo!?(?:\s*finance\b.*)?$", "Yahoo Finance"),          # Yahoo! Finance Canada / UK / Australia ...
    (r"(?:wall street journal|wsj)(?:\.com)?$", "WSJ"),
    (r"(?:thomson )?reuters(?:\.com)?$", "Reuters"),
    (r"bloomberg(?:\.com| news)?$", "Bloomberg"),
    (r"cnbc(?:\.com)?$", "CNBC"),
    (r"marketwatch(?:\.com)?$", "MarketWatch"),
    (r"barron['’]?s(?:\.com)?$", "Barron's"),
    (r"(?:investor['’]?s business daily|ibd|investors\.com)$", "Investor's Business Daily"),
    (r"(?:motley fool\b.*|fool\.com)$", "Motley Fool"),
    (r"seeking ?alpha(?:\.com)?$", "Seeking Alpha"),
    (r"benzinga\b.*$", "Benzinga"),
    (r"zacks\b.*$", "Zacks"),
    (r"(?:thestreet|the street)\b.*$", "TheStreet"),
    (r"investing\.com\b.*$", "Investing.com"),
    (r"(?:financial times|ft(?:\.com)?)$", "Financial Times"),
    (r"(?:fox business(?: network)?|foxbusiness(?:\.com)?)$", "Fox Business"),
    (r"(?:business insider|markets insider|businessinsider(?:\.com)?)$", "Business Insider"),
    (r"simply wall st(?:reet|\.)?$", "Simply Wall St"),
    (r"(?:24/7 wall st\.?|247wallst(?:\.com)?)$", "24/7 Wall St."),
    (r"nasdaq(?:\.com)?$", "Nasdaq"),
    (r"barchart(?:\.com)?$", "Barchart"),
    (r"marketbeat(?:\.com)?$", "MarketBeat"),
    (r"rttnews(?:\.com)?$", "RTTNews"),
    (r"fintel(?:\.io)?$", "Fintel"),
    (r"tipranks(?:\.com)?$", "TipRanks"),
    (r"gurufocus(?:\.com)?$", "GuruFocus"),
    (r"stocktwits(?:\.com)?$", "Stocktwits"),
    (r"investopedia(?:\.com)?$", "Investopedia"),
    (r"(?:pr ?newswire|prnewswire\.com)$", "PR Newswire"),
    (r"(?:globe ?newswire|globenewswire\.com)$", "GlobeNewswire"),
    (r"(?:business ?wire|businesswire\.com)$", "Business Wire"),
    (r"(?:access ?newswire|accesswire|accessnewswire\.com)$", "ACCESS Newswire"),
    (r"(?:tmx )?newsfile(?: corp)?(?:\.com)?$", "Newsfile"),
    (r"(?:associated press|ap news|ap)$", "AP"),
)]
KNOWN_OUTLETS = {name for _, name in _PUB_PATTERNS} | set(PUBLISHERS.values())


# ---------------------------------------------------------------- helpers

def friendly_error(e: BaseException) -> str:
    """Short Turkish error text (the scheduler's translator; util.friendly_error once the core moves it there)."""
    fn = getattr(util, "friendly_error", None)
    if fn is None:
        try:
            from ..scheduler import friendly_error as fn  # imported lazily: the scheduler loads collectors lazily too
        except Exception:  # noqa: BLE001 - never let error formatting fail
            fn = None
    if fn is not None:
        try:
            return fn(e)
        except Exception:  # noqa: BLE001
            pass
    return " ".join(str(e).split())[:300] or type(e).__name__


def _err_text(url: str, err: BaseException) -> str:
    """friendly_error plus the feed's host when the message does not already name it."""
    text = friendly_error(err)
    host = _domain(url)
    return f"{text} ({host})" if host and host not in text else text


def _raise_all_failed(failed: list[tuple[str, BaseException]], what: str = "akış") -> None:
    """Every URL failed: one URL -> its own exception (the scheduler translates it); several -> a Turkish summary."""
    if len(failed) == 1:
        raise failed[0][1]
    n = len(failed)
    raise RuntimeError(f"{n}/{n} {what} alınamadı: {_err_text(*failed[0])}") from failed[0][1]


def _partial_warning(failed: list[tuple[str, BaseException]], total: int, what: str = "akış") -> str:
    return f"Kısmi hata — {len(failed)}/{total} {what} alınamadı: {_err_text(*failed[0])}"[:300]


def normalize_publisher(name) -> str:
    """'The Motley Fool' -> 'Motley Fool', 'Yahoo! Finance Canada' -> 'Yahoo Finance', 'The Wall Street Journal' -> 'WSJ',
    'Reuters.com' -> 'Reuters'; unknown names are returned trimmed ('' for empty)."""
    s = re.sub(r"\s+", " ", html.unescape(str(name or ""))).strip(" -|·–—")
    if not s:
        return ""
    s = re.sub(r"^the\s+", "", s, flags=re.I)
    low = s.lower()
    for rx, canon in _PUB_PATTERNS:
        if rx.match(low):
            return canon
    if re.fullmatch(r"[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}", low):   # bare domain ("247wallst.com")
        mapped = _publisher_from_host(low)
        if mapped:
            return mapped
    return s


def _publisher_from_host(host: str) -> str:
    """Known outlet for a host name ('' when the domain is not a known newsroom)."""
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    for i in range(len(parts) - 1):
        name = PUBLISHERS.get(".".join(parts[i:]))
        if name:
            return normalize_publisher(name) if name != "Yahoo" else "Yahoo Finance"
    return ""


def known_outlet(name) -> str:
    """Canonical outlet name when `name` is a known newsroom (e.g. a syndicated item's author), else ''."""
    p = normalize_publisher(name)
    return p if p in KNOWN_OUTLETS else ""


def canonical_publisher(item: dict) -> str:
    """One stable name per newsroom, for counting independent sources ("kim ne diyor", "N kaynak"):
    extra.publisher (or an API's provider) -> author when it is a known outlet -> URL domain when it is a known
    outlet -> the collector's own name (so aggregator items without a publisher never collapse together)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for cand in (extra.get("publisher"), extra.get("provider")):
        p = normalize_publisher(cand)
        if p:
            return p
    p = known_outlet(item.get("author"))
    if p:
        return p
    host = _domain(str(item.get("url") or ""))
    p = _publisher_from_host(host) if host and host != "news.google.com" else ""
    return p or str(item.get("source_name") or item.get("source") or "")


# Nasdaq.com (MarketBeat) summaries often start with an unrelated "related article" headline glued to the
# real text: "Jersey Mike's Serves Fresh Gains After IPO StumbleBarclays (NYSE:BCS) reported ...".
_GLUE_TAG_RE = re.compile(r"\(\s*(?i:nyse american|nyseamerican|nysearca|nyse|nasdaq(?:gs|gm|cm)?|amex)\s*:\s*[A-Z][A-Z.]{0,6}\s*\)")
_GLUE_POINT_RE = re.compile(r"(?<=[a-z0-9’'?!%)])(?=[A-Z])")


def strip_glued_headline(title: str, summary: str) -> str:
    """Drop a headline glued (no separator) in front of the first 'Company (EXCH:TICK)' of `summary`, but only
    when the company's first word appears as a whole word in `title`, the glued token itself is not in the
    title (camelCase names such as PayPal / CrowdStrike stay intact) and the cut part has at least 3 words."""
    if not summary or not title:
        return summary
    m = _GLUE_TAG_RE.search(summary)
    if not m:
        return summary
    prefix = summary[:m.start()].rstrip()
    low_title = title.lower()
    for gp in reversed([g.start() for g in _GLUE_POINT_RE.finditer(prefix)]):
        name = prefix[gp:]
        if len(name) > 60:
            break
        first = re.match(r"[A-Z][\w&’'\-]*", name)
        if not first or not re.search(rf"(?<![\w]){re.escape(first.group(0))}(?![\w])", title):
            continue
        cut_words = prefix[:gp].split()
        if len(cut_words) < 3:
            continue
        if (cut_words[-1] + first.group(0)).lower() in low_title:   # "PayPal" is a name, not "...Pay" + "Pal ..."
            continue
        return summary[gp:]
    return summary


# Google News site searches also return quote / option-chain pages; Zacks-style auto-generated price notes
# ("Jabil (JBL) Increases Despite Market Slip: Here's What You Need to Know") carry no news and merge unrelated tickers.
TEMPLATE_FILLER = (r"\((?:[A-Z.]{1,6})\) (?:Stock )?(?:Advances|Declines|Dips|Rises|Falls|Increases|Decreases|Registers|"
                   r"Outpaces|Lags|Trails|Surpasses)\b.{0,40}Market.{0,5}:\s*(?:Some Information|Here['’]s What|"
                   r"What You Should|Important Facts|Key Facts)")
_GOOGLE_JUNK = re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{5,8}\b|(?i:interactive stock chart|stock price, news, quote|options chain|"
                          r"stock (?:forum|community) (?:and|&) discussion|quote & history|historical prices)|" + TEMPLATE_FILLER)


def gnews(query: str) -> str:
    """Google News RSS search URL (US English edition)."""
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"


def yahoo_ticker_feed(sym: str) -> str:
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(sym)}&region=US&lang=en-US"


def exchange_tickers(text: str) -> list[str]:
    """US symbols written as exchange tags in press releases: (NASDAQ: ABCD), (NYSE American: Q), (OTCQB: XYZ)."""
    out: list[str] = []
    if not text:
        return out
    for m in _EXCH_RE.finditer(text):
        out.append(m.group(2))
        pos = m.end()
        while True:   # "(NASDAQ: ABC, ABCW)"
            more = _EXCH_MORE_RE.match(text, pos)
            if not more:
                break
            out.append(more.group(1))
            pos = more.end()
    out += _EXCH_REV_RE.findall(text)
    return list(dict.fromkeys(s for s in out if SYMBOL_RE.match(s)))


def _tag_tickers(entry) -> list[str]:
    """Symbols the feed itself attaches (Seeking Alpha / Motley Fool / GlobeNewswire tags, Nasdaq field)."""
    out: list[str] = []
    for t in entry.get("tags") or []:
        term = str(t.get("term") or "").strip()
        scheme = str(t.get("scheme") or "")
        sym = ""
        if "seekingalpha.com/symbol/" in scheme:
            sym = scheme.rsplit("/", 1)[-1]
        elif "fool.com/stocks/" in scheme:
            if scheme.rstrip("/").rsplit("/", 1)[-1].upper() in US_EXCHANGES:
                sym = term
        elif ":" in term and len(term) <= 24:
            exch, _, s = term.partition(":")
            if exch.strip().upper().replace(" ", "") in US_EXCHANGES:
                sym = s
        sym = sym.strip().upper()
        if sym and SYMBOL_RE.match(sym):
            out.append(sym)
    raw = entry.get("nasdaq_tickers") or ""
    for tok in re.split(r"[,\s;]+", str(raw)):
        tok = tok.split(":")[-1].strip().upper()
        if tok and SYMBOL_RE.match(tok):
            out.append(tok)
    return out


def _plain_tags(entry, limit: int = 8) -> list[str]:
    tags = []
    for t in entry.get("tags") or []:
        term = str(t.get("term") or "").strip()
        scheme = str(t.get("scheme") or "")
        if not term or len(term) > 60 or "symbol" in scheme or "/stock" in scheme or "guid" in scheme or "ISIN" in scheme:
            continue
        if re.fullmatch(r"[0-9a-f\-]{20,}", term):
            continue
        tags.append(term)
    return list(dict.fromkeys(tags))[:limit]


def _entry_ts(entry) -> int | None:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        v = entry.get(k)
        if v:
            ts = util.to_ts(v)
            if ts:
                return ts
    for k in ("published", "updated", "created", "dc_date"):
        s = str(entry.get(k) or "").strip()
        if not s:
            continue
        ts = util.to_ts(s)
        if ts:
            return ts
        for fmt in _DATE_FMTS:
            try:
                d = datetime.strptime(s, fmt)
            except ValueError:
                continue
            return int(d.replace(tzinfo=util.ET if "%p" in fmt else util.UTC).timestamp())
    return None


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    if "?" in url:
        base, query = url.split("?", 1)
        keep = [p for p in query.split("&") if p and not _TRACKING_PARAM_RE.match(p)]
        url = base + ("?" + "&".join(keep) if keep else "")
    return util.normalize_url(url)


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def publisher_for(url: str) -> str:
    host = _domain(url)
    parts = host.split(".")
    for i in range(len(parts) - 1):
        name = PUBLISHERS.get(".".join(parts[i:]))
        if name:
            return name
    return host


def _non_latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) > 0x24F) / len(letters)


_FOREIGN_WORDS = {"do", "da", "dos", "das", "el", "los", "las", "les", "des", "du", "und", "der", "il", "della",
                  "di", "het", "een", "van", "contra", "para", "pour", "avec", "sur", "por", "com", "em", "não",
                  "mais", "una", "uno", "que", "na", "nas", "sobre", "como", "ist", "mit", "zu", "auf", "nel", "per"}
_ENGLISH_WORDS = {"the", "and", "to", "of", "in", "for", "on", "with", "is", "as", "at", "by", "after", "from",
                  "its", "how", "why", "what", "are", "will", "new", "says", "stock", "stocks", "shares"}


def _looks_foreign(title: str) -> bool:
    """Latin-script but not English (e.g. Bloomberg Línea items in the Bloomberg feed)."""
    words = set(re.findall(r"[a-zà-ÿ]+", title.lower()))
    return len(words & _FOREIGN_WORDS) >= 2 and not words & _ENGLISH_WORDS


def _is_english(entry, link: str, title: str) -> bool:
    lang = str(entry.get("language") or "").lower()
    if lang and not lang.startswith("en"):
        return False
    m = re.search(r"businesswire\.com/news/home/\d+/([a-z]{2})(?:[/?]|$)", link)
    if m and m.group(1) != "en":
        return False
    m = re.search(r"globenewswire\.com/.*?/news-release/\d{4}/\d\d/\d\d/\d+/\d+/([a-z]{2})/", link)
    if m and m.group(1) != "en":
        return False
    return _non_latin_ratio(title) < 0.3 and not _looks_foreign(title)


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:120]


def _split_publisher(title: str, publisher: str) -> str:
    """Google News titles end with " - Publisher"; drop it (publisher goes to extra)."""
    if publisher:
        for sep in (" - ", " – ", " | "):
            suffix = sep + publisher
            if title.endswith(suffix):
                return title[: -len(suffix)].strip()
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if head and len(tail) <= 40:
            return head.strip()
    return title


def _download(url: str, headers: dict | None = None, timeout: float = TIMEOUT):
    r = util.http_get(url, headers=headers, timeout=timeout)
    parsed = feedparser.parse(r.content)
    if not parsed.entries and parsed.bozo:
        raise ValueError(f"geçersiz akış: {type(parsed.get('bozo_exception')).__name__}")
    return parsed


def _parallel(jobs: list, fn, budget: float = FETCH_BUDGET, workers: int = 6) -> list[tuple]:
    """Run fn(job) for each job concurrently; returns [(job, result | None, error | None)] within budget."""
    if len(jobs) == 1:
        try:
            return [(jobs[0], fn(jobs[0]), None)]
        except Exception as e:  # noqa: BLE001 - reported to caller
            return [(jobs[0], None, e)]
    out = []
    ex = cf.ThreadPoolExecutor(max_workers=min(workers, len(jobs)))
    try:
        futs = {ex.submit(fn, j): j for j in jobs}
        done, pending = cf.wait(futs, timeout=budget)
        for f in done:
            err = f.exception()
            out.append((futs[f], None if err else f.result(), err))
        for f in pending:
            f.cancel()
            out.append((futs[f], None, TimeoutError("süre aşıldı")))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


# ---------------------------------------------------------------- collectors

class FeedCollector(Collector):
    """One logical source backed by one or more RSS/Atom URLs (merged and de-duplicated)."""

    def __init__(self, key: str, name: str, group: str, urls: str | list[str], *, source_type: str = "news",
                 interval: int = 300, description: str = "", requires: tuple[str, ...] = (),
                 ua_config: str = "", skip_tags: tuple[str, ...] = (), via: str = "", exclude: str = "",
                 title_prefix: str = "", max_items: int = MAX_ITEMS, publisher: str = "", syndicated: bool = False):
        self.key, self.name, self.group = key, name, group
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.source_type, self.interval, self.description = source_type, interval, description
        self.requires = requires
        self.ua_config = ua_config            # config key holding a User-Agent to send (SEC)
        self.skip_tags = {t.lower() for t in skip_tags}
        self.via = via                        # e.g. "Google News" when the publisher is reached through it
        self.exclude = re.compile(exclude, re.I) if exclude else None   # titles to drop (site-search noise)
        self.title_prefix = title_prefix      # for feeds whose titles are bare names (SEC suspensions)
        self.max_items = max_items
        self.publisher = publisher            # the newsroom behind a single-outlet feed (extra.publisher)
        self.syndicated = syndicated          # feed republishes other outlets: a known-outlet author wins

    # -- fetching
    def _headers(self, cfg: dict) -> dict | None:
        if self.ua_config:
            ua = str(cfg.get(self.ua_config) or "").strip()
            if ua:
                return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        return None

    def fetch(self, cfg: dict) -> dict:
        headers = self._headers(cfg)
        results = _parallel(self.urls, lambda u: _download(u, headers), workers=4)
        items, failed = [], []
        for url, parsed, err in results:
            if err is not None:
                failed.append((url, err))
                continue
            items.extend(self.parse_entries(parsed, url))
        if failed and len(failed) == len(results):
            _raise_all_failed(failed)
        res = {"news": self.finalize(items)}
        if failed:   # some URLs still work: report OK, but say what is missing
            for url, err in failed:
                log.warning("%s: %s alınamadı: %s", self.key, url, err)
            res["warnings"] = [_partial_warning(failed, len(results))]
        return res

    # -- parsing
    def parse_entries(self, parsed, feed_url: str, tickers: list[str] | None = None, extra: dict | None = None) -> list[dict]:
        is_google = "news.google.com" in feed_url
        is_yahoo = "feeds.finance.yahoo.com" in feed_url
        cutoff = util.now_ts() - MAX_AGE
        out = []
        for e in parsed.entries:
            try:
                it = self._entry(e, is_google, cutoff, tickers, extra)
            except Exception:  # noqa: BLE001 - one bad entry never breaks the feed
                log.debug("%s: bad entry skipped", self.key, exc_info=True)
                continue
            if it:
                if is_yahoo:   # Yahoo ticker feeds link straight to the original publisher
                    it["extra"]["publisher"] = normalize_publisher(publisher_for(it["url"]))
                out.append(it)
        return out

    def _item_publisher(self, author: str, url: str) -> str:
        """extra.publisher of a non-Google entry: syndication feeds (Nasdaq, Investing.com, Markets Insider) credit
        the real outlet when the author or the link names a known one (Reuters, Motley Fool, Seeking Alpha ...),
        else the feed's own newsroom."""
        if self.syndicated:
            return known_outlet(author) or _publisher_from_host(_domain(url)) or self.publisher
        return self.publisher

    def _entry(self, e, is_google: bool, cutoff: int, tickers: list[str] | None, extra_in: dict | None) -> dict | None:
        title = html.unescape(re.sub(r"\s+", " ", str(e.get("title") or ""))).strip()
        link = str(e.get("link") or e.get("id") or "").strip()
        if not title or not link.startswith("http"):
            return None
        ts = _entry_ts(e)
        if not ts or ts < cutoff:
            return None
        if self.skip_tags and any(str(t.get("term") or "").lower() in self.skip_tags for t in e.get("tags") or []):
            return None
        if not _is_english(e, link, title):
            return None
        extra = dict(extra_in or {})
        if self.via:
            extra["via"] = self.via
        author = ""
        ad = e.get("author_detail") or {}
        if ad.get("name"):
            author = str(ad["name"])
        elif e.get("author"):
            author = re.sub(r"^\S+@\S+\s*\((.*)\)$", r"\1", str(e["author"]))
        if is_google:
            src = e.get("source") or {}
            raw_publisher = str(src.get("title") or "").strip()
            title = _split_publisher(title, raw_publisher)
            if _GOOGLE_JUNK.search(title):   # quote / option-chain pages, template price notes
                return None
            extra["via"] = "Google News"
            publisher = self.publisher or normalize_publisher(raw_publisher)
            if publisher:
                extra["publisher"] = publisher
            if src.get("href"):
                extra["publisher_url"] = src.get("href")
            raw_summary = ""   # Google's description only repeats title + publisher
            url = link
        else:
            raw_summary = str(e.get("summary") or e.get("description") or "")
            url = _clean_url(link)
            publisher = self._item_publisher(author, url)
            if publisher:
                extra["publisher"] = publisher
        if not title or (self.exclude and self.exclude.search(title)):
            return None
        if is_junk_title(title):   # Yahoo option quotes ('... call (QQQ261023C00885000)'), '(EETH.N) | Stock Price ...'
            return None
        title = self.title_prefix + title
        full_text = html.unescape(util.clean_html(raw_summary, 4000))
        full_text = _BOILERPLATE_RE.sub("", full_text).strip()
        if _title_key(full_text) == _title_key(title):
            full_text = ""
        if full_text and _domain(url).endswith("nasdaq.com"):
            full_text = strip_glued_headline(title, full_text)
        summary = full_text[:600] + ("…" if len(full_text) > 600 else "")
        syms = list(tickers or []) + _tag_tickers(e) + exchange_tickers(title) + exchange_tickers(full_text)
        tags = _plain_tags(e)
        if tags:
            extra["tags"] = tags
        return self.item(title, url, ts, summary, list(dict.fromkeys(syms)), author[:120], extra)

    def finalize(self, items: list[dict]) -> list[dict]:
        """De-duplicate (URL, then title) keeping the union of tickers; newest first; cap."""
        by_key: dict[str, dict] = {}
        title_seen: dict[str, str] = {}
        for it in items:
            k = util.normalize_url(it["url"]) or it["title"]
            tk = _title_key(it["title"])
            if len(tk) >= 12:
                k = title_seen.get(tk, k)
            if k in by_key:
                prev = by_key[k]
                prev["tickers"] = list(dict.fromkeys(prev["tickers"] + it["tickers"]))
                continue
            by_key[k] = it
            if len(tk) >= 12:
                title_seen.setdefault(tk, k)
        out = sorted(by_key.values(), key=lambda x: x["published_at"] or 0, reverse=True)
        return out[: self.max_items]


class NewsroomPageCollector(FeedCollector):
    """ACCESS Newswire has no public RSS anymore; parse its server-rendered newsroom cards
    (times are US Eastern), falling back to a Google News site search when the layout changes."""

    CARD_RE = re.compile(r'<article class="nr-card[^"]*">(.*?)</article>', re.S)
    TITLE_RE = re.compile(r'class="nr-card-title"[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    META_RE = re.compile(r'class="nr-card-meta"[^>]*>([^<]+)<')
    SUMMARY_RE = re.compile(r'class="nr-card-summary"[^>]*>(.*?)</p>', re.S)
    TAG_RE = re.compile(r'class="nr-card-tag"[^>]*>([^<]+)<')

    def __init__(self, *a, pages: list[str], fallback: str, **kw):
        super().__init__(*a, **kw)
        self.pages, self.fallback = pages, fallback

    def _page_items(self, url: str) -> list[dict]:
        text = util.http_get(url, timeout=TIMEOUT).text
        cutoff = util.now_ts() - MAX_AGE
        out = []
        for card in self.CARD_RE.findall(text):
            try:
                m = self.TITLE_RE.search(card)
                meta = self.META_RE.search(card)
                if not m or not meta:
                    continue
                title = html.unescape(util.clean_html(m.group(2), 500))
                lang = re.search(r"/newsroom/([a-z]{2})/", m.group(1))
                if (lang and lang.group(1) != "en") or is_junk_title(title):
                    continue
                d = datetime.strptime(re.sub(r"\s+", " ", meta.group(1)).strip(), "%B %d, %Y %I:%M %p")
                ts = int(d.replace(tzinfo=util.ET).timestamp())
                if ts < cutoff or not title:
                    continue
                sm = self.SUMMARY_RE.search(card)
                summary = html.unescape(util.clean_html(sm.group(1), 600)) if sm else ""
                tags = [html.unescape(t).strip() for t in self.TAG_RE.findall(card)]
                syms = exchange_tickers(title) + exchange_tickers(summary)
                extra = {"tags": tags[:8]} if tags else {}
                if self.publisher:
                    extra["publisher"] = self.publisher
                out.append(self.item(title, _clean_url(html.unescape(m.group(1))), ts, summary,
                                     list(dict.fromkeys(syms)), "", extra))
            except Exception:  # noqa: BLE001
                continue
        return out

    def fetch(self, cfg: dict) -> dict:
        items, failed = [], []
        for url, res, err in _parallel(self.pages, self._page_items, workers=2):
            if err is not None:
                failed.append((url, err))
            else:
                items.extend(res)
        warnings = []
        if not items:
            try:
                items = self.parse_entries(_download(self.fallback), self.fallback)
            except Exception as e:  # noqa: BLE001
                _raise_all_failed(failed + [(self.fallback, e)], "kaynak (haber odası + Google News yedeği)")
            warnings.append(("Kısmi hata — haber odası sayfası okunamadı, Google News yedeği kullanıldı: "
                             + (_err_text(*failed[0]) if failed else "sayfa düzeni değişmiş olabilir"))[:300])
        elif failed:
            warnings.append(_partial_warning(failed, len(self.pages), "sayfa"))
        for url, err in failed:
            log.warning("%s: %s alınamadı: %s", self.key, url, err)
        res = {"news": self.finalize(items)}
        if warnings:
            res["warnings"] = warnings
        return res


# First words of company names too common to prove an article is about that company on their own.
_GENERIC_NAME_WORDS = {
    "american", "first", "united", "general", "national", "international", "global", "new", "next", "great",
    "southern", "northern", "western", "eastern", "central", "pacific", "atlantic", "capital", "financial", "bank",
    "energy", "health", "digital", "advanced", "applied", "standard", "universal", "community", "citizens", "home",
    "world", "blue", "green", "smart", "pure", "true", "power", "data", "life", "one", "air", "service", "trust",
    "best", "live", "real", "royal", "public", "premier", "select", "summit", "focus", "core", "alpha",
    "omega", "prime", "golden", "silver", "liberty", "freedom", "peoples", "people", "security", "federal", "state",
    "city", "texas", "florida", "california", "carolina", "ohio", "western", "old", "big", "bio", "oil", "gas",
    "medical", "pharma", "insight", "vision", "media", "motor", "motors", "tech", "technologies", "solutions",
}


class WatchlistNewsCollector(FeedCollector):
    """Per-symbol Yahoo Finance + Google News feeds for cfg["watchlist"]; every item is tagged with its symbol."""

    max_age = MAX_AGE
    symbol_key = "watch_symbol"       # extra field naming the symbol whose feed produced the item

    def __init__(self, key: str = "watchlist_news", name: str = "İzleme Listesi Haberleri",
                 description: str = "İzleme listesindeki her hisse için Yahoo Finance ve Google News akışları; "
                                    "haberler ilgili sembolle etiketlenir.", interval: int = 300):
        super().__init__(key, name, "aggregator", [], interval=interval, description=description)

    # Index ETFs are rarely named by symbol in headlines.
    ALIASES = {"SPY": ["S&P 500", "S&P"], "VOO": ["S&P 500"], "IVV": ["S&P 500"], "QQQ": ["Nasdaq"],
               "DIA": ["Dow"], "IWM": ["Russell"], "TLT": ["Treasury", "bond"], "GLD": ["gold"], "SLV": ["silver"],
               "USO": ["oil", "crude"], "XLE": ["energy"], "XLF": ["bank", "financial"], "SMH": ["chip", "semiconductor"],
               "SOXX": ["chip", "semiconductor"], "ARKK": ["ARK", "Cathie Wood"], "VIX": ["VIX", "volatility"]}
    _NAME_NOISE = re.compile(r"\b(inc|corp|corporation|co|company|holdings?|group|ltd|limited|plc|class [a-c]|"
                             r"common stock|ordinary shares|ads|adr|the|trust|n\.?v|s\.?a|lp|llc)\b\.?", re.I)

    @staticmethod
    def symbols(cfg: dict) -> list[str]:
        out = []
        for s in cfg.get("watchlist") or []:
            s = str(s).strip().upper().lstrip("$")
            if s and SYMBOL_RE.match(s) and s not in out:
                out.append(s)
        return out[:40]

    def _keywords(self, sym: str) -> list[str] | None:
        """Words that show an item is really about `sym`; None = no reliable name, do not filter."""
        if sym in self.ALIASES:
            return [sym] + self.ALIASES[sym]
        name = None
        try:
            from ..analysis import tickers as _tk  # optional: built by the analysis module
            name = _tk.company_name(sym)
        except Exception:  # noqa: BLE001
            name = None
        if not name:
            return None
        name = self._NAME_NOISE.sub(" ", str(name).split(" - ")[0].replace(".com", " "))
        words = [w for w in re.split(r"[\s,&]+", name) if len(w) >= 3]
        if not words:
            return None
        if words[0].lower() in _GENERIC_NAME_WORDS:   # "American" Express, "First" Citizens, "Advanced" Micro
            return [sym, f"{words[0]} {words[1]}"] if len(words) > 1 else [sym]
        return [sym, words[0]]

    @staticmethod
    def _relevant(it: dict, sym: str, keywords: list[str] | None) -> bool:
        if keywords is None and len(sym) > 1:   # no reliable company name: keep everything
            return True
        text = f"{it['title']} {it['summary'][:300]}"
        if len(sym) == 1:   # "A", "T", "F": only as a tagged symbol, never as a plain capital letter
            if re.search(rf"(?:\$|\(\s*(?:[A-Za-z ]+:\s*)?){re.escape(sym)}\s*\)?(?![A-Za-z])", text):
                return True
        elif re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", text):
            return True
        low = text.lower()
        return any(re.search(rf"(?<![a-z]){re.escape(k.lower())}(?:s|es)?(?![a-z])", low) for k in (keywords or [])[1:])

    def _jobs(self, syms: list[str]) -> list[tuple[str, str, str]]:
        jobs = [(s, "Yahoo Finance", yahoo_ticker_feed(s)) for s in syms]
        jobs += [(s, "Google News", gnews(f"{s} stock when:2d")) for s in syms]
        return jobs

    def fetch(self, cfg: dict) -> dict:
        syms = self.symbols(cfg)
        if not syms:
            return {"news": []}
        jobs = self._jobs(syms)
        vias = list(dict.fromkeys(j[1] for j in jobs))
        per_source = max(2, MAX_ITEMS // len(syms) // len(vias) + 1)
        keywords = {s: self._keywords(s) for s in syms}
        cutoff = util.now_ts() - self.max_age
        items: list[dict] = []
        failed: dict[str, list[tuple[str, BaseException]]] = {}
        ok_via: Counter = Counter()
        for (sym, via, url), parsed, err in _parallel(jobs, lambda j: _download(j[2], timeout=12), workers=6):
            if err is not None:
                failed.setdefault(via, []).append((url, err))
                continue
            ok_via[via] += 1
            got = [it for it in self.parse_entries(parsed, url, tickers=[sym], extra={self.symbol_key: sym})
                   if (it["published_at"] or 0) >= cutoff and self._relevant(it, sym, keywords[sym])]
            for it in got:
                it["extra"]["via"] = via
            got.sort(key=lambda x: x["published_at"] or 0, reverse=True)
            items.extend(got[:per_source])
        if not ok_via:
            _raise_all_failed([f for fs in failed.values() for f in fs])
        res = {"news": self.finalize(items)}
        # One failed symbol is noise; a whole dead half (all Google or all Yahoo feeds) is worth a warning.
        dead = [v for v in vias if v in failed and not ok_via[v]]
        if dead:
            res["warnings"] = [f"Kısmi hata — {v} akışlarının tümü alınamadı ({len(failed[v])}/{len(failed[v])}): "
                               f"{_err_text(*failed[v][0])}"[:300] for v in dead]
            for v in dead:
                log.warning("%s: tüm %s akışları alınamadı: %s", self.key, v, failed[v][0][1])
        elif failed:
            log.info("%s: %d/%d akış alınamadı", self.key, sum(len(f) for f in failed.values()), len(jobs))
        return res


class MoversNewsCollector(WatchlistNewsCollector):
    """Yahoo per-ticker headlines for today's biggest movers (incl. pre/after-market gappers) and large-cap
    earnings reporters, so "why is it moving?" has an answer. Yahoo only: Google's 'SYM stock' searches are where
    quote-page junk comes from, and one source halves the requests."""

    MIN_MOVE = 4.0          # |change %|
    MIN_PRICE = 1.0
    MIN_EARN_CAP = 2e9
    CAP = 25
    max_age = 36 * 3600
    symbol_key = "mover_symbol"

    def __init__(self):
        super().__init__("movers_news", "Hareketli Hisse Haberleri",
                         "Günün en çok yükselen/düşen hisseleri (piyasa öncesi ve sonrası dahil) ile bilanço açıklayan "
                         "büyük şirketler için Yahoo Finance hisse haberleri: 'neden hareket ediyor?' sorusunun cevabı.")

    def symbols(self, cfg: dict) -> list[str]:
        watch = set(WatchlistNewsCollector.symbols(cfg))
        try:
            from .. import db
            movers = db.get_snapshot_data("movers") or {}
        except Exception:  # noqa: BLE001 - no DB yet (tests) or no snapshot: nothing to follow
            return []
        moved: dict[str, float] = {}
        for lst in ("premarket", "postmarket", "gainers", "losers", "small_caps"):
            for r in (movers.get(lst) or []) if isinstance(movers, dict) else []:
                try:
                    sym = str(r.get("symbol") or "").strip().upper()
                    chg, price = float(r.get("change_pct")), float(r.get("price"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if (SYMBOL_RE.match(sym) and sym not in watch and abs(chg) >= self.MIN_MOVE
                        and price >= self.MIN_PRICE):
                    moved[sym] = max(moved.get(sym, 0.0), abs(chg))
        ranked = sorted(moved, key=lambda s: -moved[s])
        earn = [s for s in self._earnings_symbols(db) if s not in watch and s not in ranked[:18]]
        out = list(dict.fromkeys(ranked[:18] + earn + ranked[18:]))
        return out[:self.CAP]

    def _earnings_symbols(self, db) -> list[str]:
        """Today's BMO reporters, the previous session's AMC reporters (and today's AMC after the close), >= $2B."""
        now = datetime.now(util.ET)
        today = now.date()
        prev = today - timedelta(days=1)
        while not util.is_trading_day(prev):
            prev -= timedelta(days=1)
        want = {(today.isoformat(), "bmo"), (prev.isoformat(), "amc")}
        if now.hour >= 16:
            want.add((today.isoformat(), "amc"))
        start = int(datetime(prev.year, prev.month, prev.day, tzinfo=util.ET).timestamp())
        end = int(datetime(today.year, today.month, today.day, tzinfo=util.ET).timestamp()) + 86400
        out: list[tuple[float, str]] = []
        try:
            for e in db.query_events(start, end, kind="earnings"):
                ex = e.get("extra") or {}
                sym = str(e.get("ticker") or "").upper()
                cap = ex.get("market_cap") or 0
                if (SYMBOL_RE.match(sym) and (ex.get("date_et"), ex.get("time")) in want
                        and isinstance(cap, (int, float)) and cap >= self.MIN_EARN_CAP):
                    out.append((cap, sym))
        except Exception:  # noqa: BLE001
            return []
        return [s for _, s in sorted(out, reverse=True)]

    def _jobs(self, syms: list[str]) -> list[tuple[str, str, str]]:
        return [(s, "Yahoo Finance", yahoo_ticker_feed(s)) for s in syms]


# ---------------------------------------------------------------- Nasdaq trading halts

HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
HALTS_PAGE = "https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts"
HALT_REASONS_TR = {
    "T1": "Haber bekleniyor", "T2": "Haber yayımlandı", "T3": "Haber açıklandı, işlem saati belirlendi",
    "T5": "Tek hisse işlem molası", "T6": "Olağandışı piyasa hareketi", "T7": "Tek hisse işlem molası (kotasyon dönemi)",
    "T8": "ETF durdurması", "T12": "Nasdaq ek bilgi talep etti", "H4": "Kotasyon şartlarına uyumsuzluk",
    "H9": "SEC raporları güncel değil", "H10": "SEC işlem askısı", "H11": "Düzenleyici endişe",
    "O1": "Operasyonel durdurma", "IPO1": "Halka arz: işlem henüz başlamadı", "M1": "Kurumsal işlem",
    "M2": "Kotasyon yok", "LUDP": "Volatilite molası (LULD)", "LUDS": "Volatilite molası (LULD)",
    "M": "Volatilite molası", "D": "Kotasyondan çıkarma", "MWC1": "Piyasa geneli devre kesici (seviye 1)",
    "MWC2": "Piyasa geneli devre kesici (seviye 2)", "MWC3": "Piyasa geneli devre kesici (seviye 3)",
    "MWC0": "Piyasa geneli devre kesici (önceki günden)", "C3": "Haber gelmeyecek, işlem yeniden açılıyor",
    "C4": "Kotasyon şartı sağlandı, işlem yeniden açılıyor", "C9": "Bildirimler tamamlandı, işlem yeniden açılıyor",
    "C11": "Başka kurumun durdurması sona erdi", "R4": "Kotasyon sorunu çözüldü, işlem yeniden açılıyor",
    "R9": "Bildirim şartları sağlandı, işlem yeniden açılıyor",
}
_NEWS_HALTS = {"T1", "T2", "T6", "T12", "H4", "H9", "H10", "H11", "D"}   # one news item per halt
_RESUME_HALTS = (_NEWS_HALTS - {"D"}) | {"T3"}                          # + one item when trading resumes
_PAUSE_HALTS = {"LUDP", "LUDS", "M"}                                    # aggregated per symbol per day
_PAUSE_STEPS = (1, 3, 10)                                               # 1st, 3rd and 10th pause of the day


def _halt_reason(code: str) -> str:
    return HALT_REASONS_TR.get(code, f"Durdurma kodu {code}" if code else "Neden belirtilmedi")


NYSE_HALTS_URL = "https://www.nyse.com/api/trade-halts/current"
# NYSE's consolidated halt list names the reason in words; map them to the Nasdaq codes used above.
_NYSE_REASON_CODES = {
    "news pending": "T1", "news dissemination": "T2", "news released": "T2", "news and resumption times": "T3",
    "luld pause": "LUDP", "volatility trading pause": "M", "additional information requested": "T12",
    "regulatory concern": "H11", "sec trading suspension": "H10", "non compliance": "H4", "non-compliance": "H4",
    "not current": "H9", "extraordinary market activity": "T6", "corporate action": "M1", "merger effective": "D",
    "ipo issue not yet trading": "IPO1", "operations halt": "O1", "quotation not available": "M2",
}


class _HaltsBlocked(RuntimeError):
    pass


class NasdaqHaltsCollector(Collector):
    """Official US trading-halt list: Nasdaq Trader RSS (all US exchanges), or NYSE's consolidated list while
    Nasdaq Trader's bot protection blocks us. News/regulatory halts and their resumptions become news items;
    LULD volatility pauses are folded into at most three items per symbol per day; the snapshot `halts` lists
    every halt that has not resumed yet (for the dashboard and the bot)."""

    key = "nasdaq_halts"
    name = "Nasdaq İşlem Durdurmaları"
    group = "official"
    kind = "news"
    source_type = "official"
    interval = 60
    description = ("Resmi ABD işlem durdurma listesi (Nasdaq Trader; engellenirse NYSE listesi — tüm ABD borsaları): "
                   "haber bekleniyor (T1), ek bilgi talebi (T12), SEC askısı, kotasyondan çıkarma ve yeniden açılış "
                   "saatleri; LULD volatilite molaları hisse başına günde en çok 3 kayıtta toplanır.")

    BLOCK_PAUSE = 900       # after a bot-protection page, leave Nasdaq Trader alone this long (NYSE meanwhile)
    CLOSED_REUSE = 600      # outside market sessions halts barely change: re-download at most every 10 min

    def __init__(self) -> None:
        self._blocked_until = 0
        self._last: tuple[int, dict] | None = None

    @staticmethod
    def _ts(day: str, hhmmss: str) -> int | None:
        """ET date ("10/23/2026" or "2026-10-23") + "HH:MM[:SS[.fff]]" -> unix ts; None when not a time."""
        d = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                d = datetime.strptime(str(day or "").strip(), fmt)
                break
            except ValueError:
                continue
        m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(hhmmss or "").strip())
        if d is None or not m:
            return None
        return int(d.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0),
                             tzinfo=util.ET).timestamp())

    @staticmethod
    def _is_rss(content: bytes) -> bool:
        head = content[:3000].lower()
        return b"<rss" in head or b"<channel" in head

    def _nasdaq_rows(self) -> list[dict]:
        r = util.http_get(HALTS_URL, timeout=TIMEOUT)
        if not self._is_rss(r.content):
            # Incapsula challenge page instead of RSS: once more with a plain UA and no session cookies.
            r = requests.get(HALTS_URL, timeout=TIMEOUT, headers={
                "User-Agent": "PiyasaMasasi/1.0 (trading halts reader)",
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
            r.raise_for_status()
            if not self._is_rss(r.content):
                raise _HaltsBlocked("Nasdaq Trader bot koruması (Incapsula) RSS yerine doğrulama sayfası döndürdü")
        parsed = feedparser.parse(r.content)
        if not parsed.entries and parsed.bozo:
            raise ValueError(f"geçersiz akış: {type(parsed.get('bozo_exception')).__name__}")
        rows = []
        for e in parsed.entries:
            try:
                rdate = e.get("ndaq_resumptiondate")
                rows.append(self._row(e.get("ndaq_issuesymbol") or e.get("title"), e.get("ndaq_issuename"),
                                      e.get("ndaq_market"), str(e.get("ndaq_reasoncode") or "").strip().upper(),
                                      self._ts(e.get("ndaq_haltdate"), e.get("ndaq_halttime")),
                                      self._ts(rdate, e.get("ndaq_resumptiontradetime")),
                                      self._ts(rdate, e.get("ndaq_resumptionquotetime"))))
            except Exception:  # noqa: BLE001 - skip a malformed row
                continue
        return [r for r in rows if r]

    def _nyse_rows(self) -> list[dict]:
        j = util.http_json(NYSE_HALTS_URL, params={"offset": 0, "max": 500}, timeout=TIMEOUT,
                           headers={"Accept": "application/json"})
        halts = ((j or {}).get("results") or {}).get("tradeHalts")
        if not isinstance(halts, list):
            raise ValueError("NYSE durdurma listesi beklenmeyen yanıt")
        rows = []
        for h in halts:
            try:
                reason = re.sub(r"\s+", " ", str(h.get("reason") or "")).strip()
                code = _NYSE_REASON_CODES.get(reason.lower(), "")
                rdate = h.get("formatedResumptionDate")
                sym = str(h.get("symbol") or "").strip().upper()
                # NYSE spelling -> Nasdaq/app spelling: "AHT PRF" -> "AHT-F" (preferred), "WSO B" -> "WSO.B"
                sym = re.sub(r"^([A-Z]+) PR([A-Z])$", r"\1-\2", sym)
                sym = re.sub(r"^([A-Z]+) ([A-Z]{1,2})$", r"\1.\2", sym)
                row = self._row(sym, h.get("issuerName"), h.get("sourceExchange"), code,
                                self._ts(h.get("formatedHaltDate"), h.get("formatedHaltTime")),
                                self._ts(rdate, h.get("formatedResumptionTime")), None)
                if row and not code and reason:
                    row["reason_tr"] = f"Durdurma: {reason}"
                rows.append(row)
            except Exception:  # noqa: BLE001
                continue
        return [r for r in rows if r]

    @staticmethod
    def _row(sym, name, market, code: str, halt_ts, resume_ts, quote_ts) -> dict | None:
        sym = str(sym or "").strip().upper()
        if not sym or not halt_ts:
            return None
        return {"symbol": sym, "name": html.unescape(str(name or "")).strip(), "market": str(market or "").strip(),
                "reason_code": code, "reason_tr": _halt_reason(code), "halt_ts": halt_ts, "resume_ts": resume_ts,
                "resume_quote_ts": quote_ts}

    def _fetch_rows(self, now: int) -> tuple[list[dict], str]:
        if now >= self._blocked_until:
            try:
                return self._nasdaq_rows(), "nasdaqtrader"
            except Exception as e:  # noqa: BLE001 - any failure: NYSE's list covers the same halts
                status = getattr(getattr(e, "response", None), "status_code", None)
                if isinstance(e, _HaltsBlocked) or status in (403, 429):   # bot protection: back off for a while
                    self._blocked_until = now + self.BLOCK_PAUSE
                first = f"Nasdaq Trader: {friendly_error(e)}"
                log.info("%s: %s; NYSE listesi kullanılıyor", self.key, first)
        else:
            first = "Nasdaq Trader bot koruması nedeniyle bekleniyor"
        try:
            return self._nyse_rows(), "nyse"
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{first}; NYSE yedek listesi de alınamadı: {friendly_error(e)}") from e

    def _news(self, r: dict, nid: str, title: str, ts: int, summary: str, **extra) -> dict:
        sym = r["symbol"]
        it = self.item(title, HALTS_PAGE, ts, summary[:600], [sym] if SYMBOL_RE.match(sym) else [], "", {
            "reason_code": r["reason_code"], "reason_tr": r["reason_tr"], "halt_ts": r["halt_ts"],
            "resume_ts": r["resume_ts"], "resume_quote_ts": r["resume_quote_ts"], "market": r["market"],
            "issue_name": r["name"], **extra})
        it["id"] = nid                    # many halts share the page URL: never let URL de-duplication merge them
        it["categories"] = ["halt"]
        return it

    def fetch(self, cfg: dict) -> dict:
        now = util.now_ts()
        if util.market_session(now) == "closed" and self._last and now - self._last[0] < self.CLOSED_REUSE:
            return self._last[1]
        rows, origin = self._fetch_rows(now)
        res = self._build(rows, now)
        res["snapshots"]["halts"]["source"] = origin
        self._last = (now, res)
        return res

    def _build(self, rows: list[dict], now: int) -> dict:
        cutoff = now - MAX_AGE
        hm = lambda ts: util.fmt_et(ts, "%H:%M")  # noqa: E731
        news: list[dict] = []
        pauses: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            sym, code, day = r["symbol"], r["reason_code"], util.fmt_et(r["halt_ts"], "%Y-%m-%d")
            who = f"{r['name']} ({sym}{', ' + r['market'] if r['market'] else ''})" if r["name"] else sym
            if code in _PAUSE_HALTS:
                pauses.setdefault((sym, day), []).append(r)
                continue
            if r["halt_ts"] >= cutoff and code in _NEWS_HALTS:
                summary = (f"{who} hissesinde işlemler {util.fmt_et(r['halt_ts'], '%d.%m.%Y %H:%M')} ET itibarıyla "
                           f"durduruldu. Neden: {r['reason_tr']} (kod {code}).")
                if r["resume_ts"]:
                    summary += f" Yeniden açılış: {util.fmt_et(r['resume_ts'], '%d.%m.%Y %H:%M')} ET."
                # id without the code: the same halt keeps one row when its code moves on (T1 -> T3) or the source
                # switches between Nasdaq Trader and NYSE
                news.append(self._news(r, util.make_id("halt", sym, r["halt_ts"]),
                                       f"{sym} işlem durduruldu — {r['reason_tr']} ({hm(r['halt_ts'])} ET)",
                                       r["halt_ts"], summary))
            if r["resume_ts"] and code in _RESUME_HALTS and r["resume_ts"] >= cutoff:
                verb = "yeniden açılıyor" if r["resume_ts"] > now else "yeniden açıldı"
                news.append(self._news(
                    r, util.make_id("halt-resume", sym, r["halt_ts"]),
                    f"{sym}: işlemler {hm(r['resume_ts'])} ET'de {verb} ({r['reason_tr']})", min(r["resume_ts"], now),
                    f"{who}: {util.fmt_et(r['halt_ts'], '%d.%m %H:%M')} ET'de başlayan işlem durdurması "
                    f"({r['reason_tr']}) sona eriyor; işlem {util.fmt_et(r['resume_ts'], '%d.%m %H:%M')} ET'de başlıyor.",
                    resumed=True))
        today = util.fmt_et(now, "%Y-%m-%d")
        for (sym, day), lst in pauses.items():
            lst.sort(key=lambda x: x["halt_ts"])
            if lst[-1]["halt_ts"] < cutoff:
                continue
            r0 = lst[0]
            who = f"{r0['name']} ({sym})" if r0["name"] else sym
            when = "bugün" if day == today else util.fmt_et(lst[0]["halt_ts"], "%d.%m")
            for k in _PAUSE_STEPS:
                if len(lst) < k:
                    break
                r = lst[k - 1]
                title = (f"{sym}: volatilite molası (LULD) — {hm(r['halt_ts'])} ET" if k == 1 else
                         f"{sym}: {when} {k}. volatilite molası (LULD) — {hm(r['halt_ts'])} ET")
                summary = (f"{who} {when} {len(lst)} kez volatilite molasına girdi (sert fiyat hareketi; her mola "
                           f"yaklaşık 5 dk). Son mola: {hm(lst[-1]['halt_ts'])} ET.")
                news.append(self._news(r, util.make_id("halt-luld", sym, day, k), title, r["halt_ts"], summary,
                                       pause_index=k, pause_count=len(lst),
                                       pause_times=[hm(x["halt_ts"]) for x in lst][-20:]))
        news.sort(key=lambda x: x["published_at"] or 0, reverse=True)
        active = [r for r in rows if not r["resume_ts"] or r["resume_ts"] > now]
        active.sort(key=lambda r: -r["halt_ts"])
        luld_today = {sym: len(lst) for (sym, day), lst in pauses.items() if day == today}
        snap = {"ts": now, "items": active[:300], "luld_today": luld_today, "total": len(rows)}
        return {"news": news[:MAX_ITEMS], "snapshots": {"halts": snap}}


# ---------------------------------------------------------------- feed list

def _cnbc(feed_id: str) -> str:
    return f"https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id={feed_id}"


_DJ = "https://feeds.content.dowjones.io/public/rss/"
_PRN = "https://www.prnewswire.com/rss/"
_GNW = "https://rss.globenewswire.com/en/RssFeed/"
_BW = "https://feed.businesswire.com/rss/home/?rss="
_FED = "https://www.federalreserve.gov/feeds/"

F = FeedCollector

COLLECTORS: list[Collector] = [
    # ---- media
    F("cnbc_top", "CNBC Manşetler", "media", _cnbc("100003114"), interval=120,
      publisher="CNBC",
      description="CNBC ABD manşet haberleri ve analizleri."),
    F("cnbc_markets", "CNBC Piyasalar", "media", [_cnbc("20409666"), _cnbc("15839069")], interval=180,
      publisher="CNBC",
      description="CNBC Market Insider ve yatırım haberleri: günün en çok hareket eden hisseleri, piyasa yorumları."),
    F("cnbc_earnings", "CNBC Bilançolar", "media", _cnbc("15839135"), interval=300,
      publisher="CNBC",
      description="CNBC şirket bilançoları ve kâr açıklamaları."),
    F("cnbc_tech", "CNBC Teknoloji", "media", _cnbc("19854910"), interval=300,
      publisher="CNBC",
      description="CNBC teknoloji şirketleri haberleri."),
    F("cnbc_economy", "CNBC Ekonomi", "media", _cnbc("20910258"), interval=300,
      publisher="CNBC",
      description="CNBC ABD ekonomisi, makro veriler ve Fed haberleri."),
    F("marketwatch_top", "MarketWatch Manşetler", "media", _DJ + "mw_topstories", interval=180,
      publisher="MarketWatch",
      description="MarketWatch öne çıkan haberler."),
    F("marketwatch_bulletins", "MarketWatch Son Dakika", "media", _DJ + "mw_bulletins", interval=120,
      publisher="MarketWatch",
      description="MarketWatch son dakika bültenleri (endeks kapanışları, sert hareketler)."),
    F("wsj_markets", "WSJ Piyasalar", "media", _DJ + "RSSMarketsMain", interval=180,
      publisher="WSJ",
      description="Wall Street Journal piyasa haberleri ve gün içi piyasa özeti."),
    F("wsj_business", "WSJ İş & Ekonomi", "media", [_DJ + "WSJcomUSBusiness", _DJ + "socialeconomyfeed"], interval=300,
      publisher="WSJ",
      description="Wall Street Journal ABD iş dünyası, sektör 'Market Talk' notları ve ekonomi haberleri."),
    F("bloomberg_markets", "Bloomberg Piyasalar", "media", "https://feeds.bloomberg.com/markets/news.rss", interval=180,
      publisher="Bloomberg",
      description="Bloomberg piyasa haberleri (başlık ve kısa özet)."),
    F("bloomberg_economics", "Bloomberg Ekonomi", "media", "https://feeds.bloomberg.com/economics/news.rss", interval=300,
      publisher="Bloomberg",
      description="Bloomberg ekonomi ve merkez bankası haberleri."),
    F("reuters", "Reuters", "media", [gnews("site:reuters.com/markets when:1d"), gnews("site:reuters.com/business when:1d")],
      interval=240, via="Google News",
      publisher="Reuters",
      description="Reuters piyasa ve iş dünyası haberleri (doğrudan RSS olmadığı için Google News üzerinden)."),
    F("ft_markets", "Financial Times Piyasalar", "media", "https://www.ft.com/markets?format=rss", interval=300,
      publisher="Financial Times",
      description="Financial Times piyasa haberleri."),
    F("yahoo_finance", "Yahoo Finance Piyasa", "media",
      "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US", interval=180,
      publisher="Yahoo Finance", exclude=TEMPLATE_FILLER,
      description="Yahoo Finance'in S&P 500, Dow ve Nasdaq endeks haber akışı (genel piyasa manşetleri)."),
    F("seekingalpha_news", "Seeking Alpha Haberler", "media",
      ["https://seekingalpha.com/market-news/all.xml", "https://seekingalpha.com/market_currents.xml"], interval=120,
      publisher="Seeking Alpha",
      description="Seeking Alpha anlık piyasa haberleri (Market Currents); hisse sembolleri kaynaktan gelir."),
    F("benzinga", "Benzinga", "media", ["https://www.benzinga.com/feed", gnews("site:benzinga.com when:1d")], interval=240,
      publisher="Benzinga",
      description="Benzinga haberleri (sitenin kendi akışı + Google News üzerinden güncel Benzinga başlıkları)."),
    F("nasdaq_markets", "Nasdaq Piyasalar", "media", "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
      interval=300, publisher="Nasdaq", syndicated=True,
      description="Nasdaq.com piyasa haberleri (Zacks, Barchart, RTTNews vb. içerikler)."),
    F("nasdaq_earnings", "Nasdaq Bilançolar", "media", "https://www.nasdaq.com/feed/rssoutbound?category=Earnings",
      interval=300, publisher="Nasdaq", syndicated=True,
      description="Nasdaq.com bilanço haberleri ve önizlemeler."),
    F("nasdaq_stocks", "Nasdaq Hisseler", "media", "https://www.nasdaq.com/feed/rssoutbound?category=Stocks",
      interval=300, publisher="Nasdaq", syndicated=True,
      description="Nasdaq.com hisse senedi haberleri."),
    F("investing_stocks", "Investing.com Borsa", "media", "https://www.investing.com/rss/news_25.rss", interval=180,
      publisher="Investing.com", syndicated=True,
      description="Investing.com borsa haberleri."),
    F("investing_company", "Investing.com Şirket Haberleri", "media", "https://www.investing.com/rss/news_356.rss",
      interval=180, publisher="Investing.com", syndicated=True,
      description="Investing.com şirket haberleri (bilanço, analist, birleşme)."),
    F("investing_economy", "Investing.com Ekonomi", "media",
      ["https://www.investing.com/rss/news_14.rss", "https://www.investing.com/rss/news_95.rss"], interval=300,
      publisher="Investing.com", syndicated=True,
      description="Investing.com ekonomi ve ekonomik gösterge haberleri."),
    F("foxbusiness", "Fox Business", "media", ["https://moxie.foxbusiness.com/google-publisher/markets.xml",
                                               "https://moxie.foxbusiness.com/google-publisher/economy.xml"],
      interval=300, publisher="Fox Business",
      description="Fox Business piyasa ve ekonomi haberleri."),
    F("barrons", "Barron's", "media", gnews("site:barrons.com when:1d"), interval=300, via="Google News",
      publisher="Barron's",
      description="Barron's haber ve analizleri (Barron's RSS'i kapalı olduğu için Google News üzerinden)."),
    F("ibd", "Investor's Business Daily", "media", "https://www.investors.com/feed/", interval=300,
      publisher="Investor's Business Daily",
      description="Investor's Business Daily piyasa ve hisse analizleri."),
    F("fool", "Motley Fool", "media", "https://www.fool.com/feeds/index.aspx", interval=600,
      publisher="Motley Fool",
      description="Motley Fool makaleleri ve bilanço görüşme dökümleri; semboller kaynaktan gelir."),
    F("thestreet", "TheStreet", "media", "https://www.thestreet.com/.rss/full/", interval=600,
      publisher="TheStreet",
      description="TheStreet piyasa ve şirket haberleri."),
    F("businessinsider_markets", "Business Insider Piyasalar", "media", "https://markets.businessinsider.com/rss/news",
      interval=600, publisher="Business Insider", syndicated=True,
      description="Markets Insider kısa şirket haberleri (EPS/gelir açıklamaları)."),

    # ---- press releases
    F("prnewswire", "PR Newswire", "press", _PRN + "news-releases-list.rss", source_type="press", interval=120,
      publisher="PR Newswire",
      description="PR Newswire tüm şirket basın bültenleri (yalnızca İngilizce)."),
    F("prnewswire_finance", "PR Newswire Finans", "press",
      [_PRN + "financial-services-latest-news/financial-services-latest-news-list.rss",
       _PRN + "financial-services-latest-news/earnings-list.rss",
       _PRN + "financial-services-latest-news/acquisitions-mergers-and-takeovers-list.rss",
       _PRN + "health-latest-news/fda-approval-list.rss"],
      source_type="press", interval=180,
      publisher="PR Newswire",
      description="PR Newswire finans, bilanço, birleşme/satın alma ve FDA onayı bültenleri."),
    F("globenewswire", "GlobeNewswire", "press", _GNW + "orgclass/1", source_type="press", interval=120,
      publisher="GlobeNewswire",
      description="GlobeNewswire halka açık şirket bültenleri; borsa sembolleri kaynaktan gelir."),
    F("globenewswire_earnings", "GlobeNewswire Bilanço & Birleşme", "press",
      [_GNW + "subjectcode/13-Earnings%20Releases%20And%20Operating%20Results",
       _GNW + "subjectcode/27-Mergers%20And%20Acquisitions"],
      source_type="press", interval=180,
      publisher="GlobeNewswire",
      description="GlobeNewswire bilanço sonuçları ve birleşme/satın alma bültenleri."),
    F("businesswire", "Business Wire", "press", [_BW + "G1QFDERJXkJeEF9YXA==", _BW + "G1QFDERJXkJeEFtRWA=="],
      source_type="press", interval=150,
      publisher="Business Wire",
      description="Business Wire bilanço ve birleşme/satın alma bültenleri (genel akış boş döndüğü için konu akışları)."),
    NewsroomPageCollector("accessnewswire", "ACCESS Newswire", "press", [], source_type="press", interval=300,
                          pages=["https://www.accessnewswire.com/newsroom",
                                 "https://www.accessnewswire.com/newsroom?page=2"],
                          fallback=gnews("site:accessnewswire.com when:1d"),
                          publisher="ACCESS Newswire",
                          description="ACCESS Newswire (eski Accesswire) basın bültenleri; küçük ve OTC şirketler."),
    F("newsfile", "Newsfile", "press", gnews("site:newsfilecorp.com when:1d"), source_type="press", interval=300,
      via="Google News",
      publisher="Newsfile",
      description="Newsfile basın bültenleri (site bot korumalı olduğu için Google News üzerinden)."),

    # ---- official
    F("fed_monetary", "Fed Para Politikası (FOMC)", "official", _FED + "press_monetary.xml", source_type="official",
      interval=180, description="Federal Reserve para politikası açıklamaları: FOMC kararları, tutanaklar."),
    F("fed_press", "Fed Basın Bültenleri", "official", _FED + "press_all.xml", source_type="official",
      interval=600, skip_tags=("Monetary Policy",),
      description="Federal Reserve diğer basın bültenleri (denetim, bankacılık düzenlemeleri)."),
    F("fed_speeches", "Fed Konuşmaları", "official", [_FED + "speeches.xml", _FED + "testimony.xml"],
      source_type="official", interval=900, description="Fed başkanı ve yönetim kurulu üyelerinin konuşmaları ve Kongre ifadeleri."),
    F("sec_press", "SEC Basın Bültenleri", "official", "https://www.sec.gov/news/pressreleases.rss",
      source_type="official", interval=900, requires=("sec_user_agent",), ua_config="sec_user_agent",
      description="ABD Menkul Kıymetler Komisyonu (SEC) basın bültenleri. SEC e-posta ayarı gerekir."),
    F("sec_suspensions", "SEC İşlem Durdurmaları", "official",
      "https://www.sec.gov/enforcement-litigation/trading-suspensions/rss",
      source_type="official", interval=1800, requires=("sec_user_agent",), ua_config="sec_user_agent",
      title_prefix="SEC trading suspension: ",
      description="SEC'in hisse işlem durdurma (trading suspension) kararları. SEC e-posta ayarı gerekir."),
    F("fda_press", "FDA Basın Bültenleri", "official",
      "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
      source_type="official", interval=600, description="ABD Gıda ve İlaç Dairesi (FDA) basın bültenleri: onaylar, uyarılar."),
    F("bls", "BLS (Çalışma İstatistikleri)", "official", gnews('site:bls.gov "news release" when:3d'),
      source_type="official", interval=900, via="Google News",
      exclude=r"^(table\b|naics\b|series\b|databases?\b|cps home|.*\bhome$)|system maintenance|career outlook",
      description="ABD Çalışma İstatistikleri Bürosu: TÜFE, ÜFE, istihdam verileri (BLS RSS'i engelli, Google News üzerinden)."),
    F("bea", "BEA (Ekonomik Analiz)", "official", "https://apps.bea.gov/rss/rss.xml", source_type="official",
      interval=1800, description="ABD Ekonomik Analiz Bürosu: GSYH, PCE enflasyonu, dış ticaret açıklamaları."),
    F("census", "ABD Nüfus Bürosu Göstergeleri", "official", "https://www.census.gov/economic-indicators/indicator.xml",
      source_type="official", interval=1800,
      description="Census Bureau ekonomik göstergeleri: perakende satışlar, konut başlangıçları, dayanıklı mallar."),
    F("treasury", "ABD Hazine Bakanlığı", "official", gnews("site:home.treasury.gov/news when:3d"), source_type="official",
      interval=1800, via="Google News",
      description="ABD Hazine Bakanlığı açıklamaları ve yaptırımlar (Google News üzerinden)."),

    # ---- aggregator (Google News searches)
    F("gnews_stock_market", "Google News: Borsa", "aggregator", gnews("stock market when:1d"), interval=300,
      exclude=TEMPLATE_FILLER,
      description="Google News 'stock market' araması; gerçek yayıncı adı ayrıca gösterilir."),
    F("gnews_premarket", "Google News: Açılış Öncesi Hareketliler", "aggregator",
      gnews('(premarket OR "pre-market") stocks movers when:1d'), interval=180,
      description="Piyasa açılışı öncesi en çok hareket eden hisseler haberleri."),
    F("gnews_earnings_beat", "Google News: Beklentiyi Aşan Bilançolar", "aggregator",
      gnews('("earnings beat" OR "beats estimates" OR "tops estimates") (NYSE OR Nasdaq) when:1d'), interval=300,
      description="Beklentileri aşan bilanço haberleri."),
    F("gnews_fda", "Google News: FDA Onayları", "aggregator", gnews("FDA approval stock when:2d"), interval=300,
      description="FDA onayı ve hisse etkisi haberleri."),
    F("gnews_mna", "Google News: Birleşme & Satın Alma", "aggregator", gnews("merger acquisition stock when:1d"),
      interval=300, description="Birleşme, satın alma ve devralma teklifleri haberleri."),
    F("gnews_analyst", "Google News: Analist Not Değişimleri", "aggregator",
      gnews("analyst upgrade OR downgrade stock when:1d"), interval=300,
      description="Analist not artırımı / indirimi ve hedef fiyat değişiklikleri."),
    WatchlistNewsCollector(),
    MoversNewsCollector(),
    NasdaqHaltsCollector(),
]
