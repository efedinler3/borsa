"""Social buzz: ApeWisdom (Reddit/4chan ticker mentions), StockTwits trending, Yahoo trending, Reddit hot posts (RSS)."""
from __future__ import annotations

import html
import logging
import re
import threading
import time

import feedparser
import requests

from .. import util
from .base import Collector
from .market import _int, _parallel, trending_symbols
from .rss import friendly_error

log = logging.getLogger("marketdesk.social")

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"
STOCKTWITS_TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"

# Reddit wants a unique, descriptive User-Agent; generic browser UAs get 429 immediately.
REDDIT_UA = "windows:piyasamasasi:1.0 (personal market news reader)"
REDDIT_SUBS = ("wallstreetbets", "stocks", "investing", "StockMarket", "pennystocks")
REDDIT_TTL = 600          # one multireddit request serves every subreddit collector for 10 min
MAX_AGE = 3 * 86400

_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)(?![\w.])")
_NOT_TICKERS = {"USD", "EUR", "CAD", "GBP", "TRY", "K", "M", "B"}


def cashtags(text: str) -> list[str]:
    out = []
    for m in _CASHTAG.finditer(text or ""):
        s = m.group(1).upper()
        if s not in _NOT_TICKERS and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- buzz snapshot

def apewisdom(filter_: str = "all-stocks", limit: int = 50) -> list[dict]:
    d = util.http_json(APEWISDOM_URL.format(filter=filter_), headers={"Accept": "application/json"}, timeout=15)
    out = []
    for r in (d or {}).get("results") or []:
        try:
            sym = str(r["ticker"]).strip().upper()
            rank = int(r["rank"])
        except (KeyError, TypeError, ValueError):
            continue
        r24, m, m24 = _int(r.get("rank_24h_ago")), _int(r.get("mentions")), _int(r.get("mentions_24h_ago"))
        out.append({
            "symbol": sym, "name": html.unescape(str(r.get("name") or "")).strip(), "rank": rank,
            "rank_24h_ago": r24, "mentions": m, "mentions_24h_ago": m24, "upvotes": _int(r.get("upvotes")),
            # extras: positive rank_change = climbing the list
            "rank_change": (r24 - rank) if r24 else None,
            "mentions_change_pct": round((m - m24) / m24 * 100, 1) if m is not None and m24 else None,
        })
    out.sort(key=lambda x: x["rank"])
    return out[:limit]


def stocktwits_trending() -> list[dict]:
    try:
        d = util.http_json(STOCKTWITS_TRENDING_URL, headers={"Accept": "application/json"}, timeout=12, retries=0)
    except requests.HTTPError as e:
        r = e.response
        if r is not None and r.status_code == 403 and "Just a moment" in (r.text or "")[:2000]:
            raise RuntimeError("StockTwits Cloudflare engeli (HTTP 403): API otomatik erişime kapalı") from None
        raise
    out = []
    for i, s in enumerate((d or {}).get("symbols") or []):
        if isinstance(s, dict) and s.get("symbol"):
            out.append({"symbol": str(s["symbol"]).upper(), "title": s.get("title") or "",
                        "watchlist_count": _int(s.get("watchlist_count")), "rank": i + 1})
    return out


_ERR_NAMES = {"apewisdom": "ApeWisdom", "apewisdom_wsb": "ApeWisdom WSB", "yahoo_trending": "Yahoo trend",
              "stocktwits": "StockTwits"}


class SocialBuzz(Collector):
    key = "social_buzz"
    name = "Sosyal Trendler"
    group = "social"
    kind = "social"
    source_type = "social"
    interval = 600
    description = ("Reddit'te en çok konuşulan hisseler (ApeWisdom: tüm hisse subreddit'leri ve r/wallstreetbets), "
                   "StockTwits trend listesi (erişilebilirse) ve Yahoo Finance trend semboller.")

    ST_RETRY = 3600      # after a StockTwits failure, try again in 1 h
    ST_KEEP = 3600       # keep the last good StockTwits list this long

    def __init__(self) -> None:
        self._st: tuple[int, list] | None = None
        self._st_next = 0
        self._st_err = ""

    def fetch(self, cfg: dict) -> dict:
        now = util.now_ts()
        jobs = [("apewisdom", lambda: apewisdom("all-stocks", 50)),
                ("apewisdom_wsb", lambda: apewisdom("wallstreetbets", 50)),
                ("yahoo_trending", lambda: [{"symbol": s, "rank": i + 1} for i, s in
                                            enumerate(x for x in trending_symbols(30) if x and "^" not in x and "=" not in x)])]
        try_st = now >= self._st_next
        if try_st:
            jobs.append(("stocktwits", stocktwits_trending))
        res = _parallel(lambda f: f(), [(f,) for _, f in jobs], workers=4, timeout=40)

        snap: dict = {"ts": now, "apewisdom": [], "apewisdom_wsb": [], "stocktwits": [], "yahoo_trending": [],
                      "errors": {}}
        first_exc: Exception | None = None
        for i, (key, _) in enumerate(jobs):
            r = res.get(i)
            if isinstance(r, Exception) or r is None:
                snap["errors"][key] = (friendly_error(r) if isinstance(r, Exception) else "yanıt yok")[:200]
                first_exc = first_exc or (r if isinstance(r, Exception) else None)
                log.info("social %s failed: %r", key, r)
            else:
                snap[key] = r
        if try_st:
            if "stocktwits" in snap["errors"]:
                self._st_next, self._st_err = now + self.ST_RETRY, snap["errors"]["stocktwits"]
            else:
                self._st, self._st_err = (now, snap["stocktwits"]), ""
        if not snap["stocktwits"]:
            if self._st and now - self._st[0] < self.ST_KEEP:
                snap["stocktwits"] = self._st[1]
            elif self._st_err:
                snap["errors"]["stocktwits"] = self._st_err
        if not (snap["apewisdom"] or snap["apewisdom_wsb"] or snap["stocktwits"] or snap["yahoo_trending"]):
            raise RuntimeError(("Sosyal veri alınamadı: " + "; ".join(
                f"{_ERR_NAMES.get(k, k)}: {v}" for k, v in snap["errors"].items()))[:400]) from first_exc
        return {"snapshots": {"social": snap}}


# ---------------------------------------------------------------- Reddit posts (shared multireddit RSS)

_reddit_lock = threading.Lock()
_reddit: dict = {"entries": None, "ts": 0.0, "next_ok": 0.0}


def _reddit_rate(resp: requests.Response | None, now: float) -> float:
    """Seconds until Reddit allows the next request, from its rate-limit headers."""
    if resp is None:
        return 60.0
    h = resp.headers
    try:
        reset = float(h.get("x-ratelimit-reset") or h.get("retry-after") or 60)
    except ValueError:
        reset = 60.0
    try:
        remaining = float(h.get("x-ratelimit-remaining") or 1)
    except ValueError:
        remaining = 1.0
    return reset + 1 if (resp.status_code == 429 or remaining < 1) else 0.0


def reddit_entries() -> list:
    """Hot posts of all REDDIT_SUBS via one multireddit RSS request, cached REDDIT_TTL s.
    Reddit allows ~1 unauthenticated request/minute, so all subreddit collectors share this."""
    if not _reddit_lock.acquire(timeout=30):  # another collector is fetching; keep our own fetch < 60 s
        if _reddit["entries"] is not None:
            return _reddit["entries"]
        raise RuntimeError("Reddit isteği meşgul, sonra tekrar denenecek")
    try:
        now = time.time()
        cached = _reddit["entries"]
        if cached is not None and now - _reddit["ts"] < REDDIT_TTL:
            return cached
        url = f"https://www.reddit.com/r/{'+'.join(REDDIT_SUBS)}/hot/.rss"
        headers = {"User-Agent": REDDIT_UA, "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
        for attempt in range(2):
            wait_s = _reddit["next_ok"] - time.time()
            if wait_s > 0:
                if wait_s > 12:
                    if cached is not None and now - _reddit["ts"] < 3 * REDDIT_TTL:
                        return cached
                    raise RuntimeError(f"Reddit hız sınırı: {int(wait_s)} sn sonra tekrar denenecek")
                time.sleep(wait_s)
            try:
                r = util.http_get(url, params={"limit": 100}, headers=headers, timeout=15, retries=0)
            except requests.HTTPError as e:
                _reddit["next_ok"] = time.time() + _reddit_rate(e.response, time.time())
                if e.response is not None and e.response.status_code == 429 and attempt == 0:
                    continue
                raise
            _reddit["next_ok"] = time.time() + _reddit_rate(r, time.time())
            feed = feedparser.parse(r.content)
            if not feed.entries and feed.bozo:
                raise ValueError(f"Reddit RSS çözümlenemedi: {feed.bozo_exception!r}")
            _reddit.update(entries=list(feed.entries), ts=time.time())
            return _reddit["entries"]
        raise RuntimeError("Reddit hız sınırı (429)")
    finally:
        _reddit_lock.release()


def _entry_sub(e) -> str:
    for t in e.get("tags") or []:
        if t.get("term"):
            return str(t["term"])
    m = re.search(r"/r/([^/]+)/", e.get("link") or "")
    return m.group(1) if m else ""


_SUBMITTED = re.compile(r"submitted\s+by", re.I)
_LINK = re.compile(r'<a href="([^"]+)">\s*\[link\]\s*</a>')


class RedditSub(Collector):
    group = "social"
    kind = "news"
    source_type = "social"
    interval = 900

    def __init__(self, sub: str) -> None:
        self.sub = sub
        self.key = f"reddit_{sub.lower()}"
        self.name = f"Reddit r/{sub}"
        self.description = (f"r/{sub} alt forumunun öne çıkan (hot) gönderileri (Reddit RSS). "
                            "Bireysel yatırımcı yorumlarıdır, haber değildir; $TICKER etiketleri hisseye bağlanır.")

    def _item(self, e, rank: int, now: int) -> dict | None:
        title = html.unescape(e.get("title") or "").strip()
        link = e.get("link") or ""
        pub = util.to_ts(e.get("published_parsed") or e.get("updated_parsed") or e.get("published"))
        if not title or not link or not pub or now - pub > MAX_AGE:
            return None
        content = ""
        if e.get("content"):
            content = e["content"][0].get("value") or ""
        content = content or e.get("summary") or ""
        m = _LINK.search(content)
        ext_link = html.unescape(m.group(1)) if m else ""
        cut = _SUBMITTED.search(content)
        body = content[:cut.start()] if cut else content
        summary = util.clean_html(html.unescape(re.sub(r"<[^>]+>", " ", body)))
        thumb = ((e.get("media_thumbnail") or [{}])[0] or {}).get("url") or ""
        extra = {"subreddit": self.sub, "listing": "hot", "hot_rank": rank, "post_id": e.get("id") or ""}
        if ext_link and "reddit.com" not in ext_link and "redd.it" not in ext_link:
            extra["link"] = ext_link
        if thumb:
            extra["thumbnail"] = html.unescape(thumb)
        author = (e.get("author") or "").replace("/u/", "u/").strip()
        return self.item(title, link, pub, summary, cashtags(f"{title} {summary}"), author, extra)

    def fetch(self, cfg: dict) -> dict:
        now = util.now_ts()
        items, rank = [], 0
        for e in reddit_entries():
            try:
                if _entry_sub(e).lower() != self.sub.lower():
                    continue
                rank += 1
                it = self._item(e, rank, now)
                if it:
                    items.append(it)
            except Exception:  # noqa: BLE001 — skip malformed entries
                continue
        return {"news": items[:100]}


# StockTwits per-symbol streams (api.stocktwits.com/api/2/streams/symbol/SYM.json) are not registered:
# the whole StockTwits API sits behind a Cloudflare browser challenge (HTTP 403) for non-browser clients.
COLLECTORS = [SocialBuzz()] + [RedditSub(s) for s in REDDIT_SUBS]
