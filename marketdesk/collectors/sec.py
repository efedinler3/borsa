"""SEC EDGAR collectors: 8-K events, Form 4 insider trades, offerings/IPOs, 13D/13G stakes, 6-K.

All requests send cfg["sec_user_agent"] (SEC fair-access policy) and are globally throttled to < 10 req/s.
Source: the EDGAR "latest filings" Atom feed (browse-edgar?action=getcurrent).
"""
from __future__ import annotations

import copy
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import requests

from ..util import DATA_DIR, http_get, jdump, jload, now_ts, to_ts
from .base import Collector
from .rss import friendly_error

log = logging.getLogger("marketdesk.collectors.sec")

ATOM = "{http://www.w3.org/2005/Atom}"
CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
TICKERS_PATH = DATA_DIR / "sec_company_tickers.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
MAX_AGE = 3 * 86400
MIN_GAP = 0.125            # seconds between SEC requests (8 req/s, limit is 10)
DOC_BUDGET_S = 35          # max seconds per fetch spent downloading filing documents

# ---------------------------------------------------------------- HTTP (throttled, SEC user agent)

_rate_lock = threading.Lock()
_last_req = 0.0


def user_agent(cfg: dict) -> str:
    """cfg["sec_user_agent"]; a bare e-mail gets an app name prefix ("PiyasaMasasi you@mail.com")."""
    ua = re.sub(r"\s+", " ", str(cfg.get("sec_user_agent") or "")).strip()
    return f"PiyasaMasasi {ua}" if ua and "@" in ua and " " not in ua else ua


def sec_get(url: str, cfg: dict, params: dict | None = None, timeout: float = 20) -> requests.Response:
    """GET from sec.gov with the configured User-Agent, globally rate-limited."""
    global _last_req
    ua = user_agent(cfg)
    if not ua:
        raise RuntimeError("SEC User-Agent (ad + e-posta) ayarlanmamış")
    with _rate_lock:
        wait = _last_req + MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_req = time.monotonic()
    try:
        return http_get(url, params=params, timeout=timeout, headers={
            "User-Agent": ua, "Accept-Encoding": "gzip, deflate",
            "Accept": "application/atom+xml, application/xml, application/json, text/plain, */*"})
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status in (403, 429):
            raise RuntimeError(f"SEC erişimi reddetti (HTTP {status}): SEC e-posta / User-Agent ayarını "
                               "kontrol edin ('Ad Soyad eposta@alan.com') veya bir süre bekleyin") from e
        raise


# ---------------------------------------------------------------- CIK -> ticker map

_tick_lock = threading.Lock()
_tick_rows: dict[int, list] | None = None     # cik -> [ticker, exchange]
_tick_checked = 0
EXCHANGE_LISTED = {"Nasdaq", "NYSE", "CBOE", "TXSE"}


def _load_ticker_file() -> dict | None:
    try:
        return jload(TICKERS_PATH.read_text(encoding="utf-8")) if TICKERS_PATH.exists() else None
    except OSError:
        return None


def _download_tickers(cfg: dict) -> dict[int, list]:
    m: dict[int, list] = {}
    try:  # same list as company_tickers.json plus the exchange column
        data = sec_get(TICKERS_EXCHANGE_URL, cfg).json()
        rows = [(r[0], r[2], r[3]) for r in data["data"]]
    except RuntimeError:
        raise
    except Exception:
        data = sec_get(TICKERS_URL, cfg).json()
        rows = [(r["cik_str"], r["ticker"], None) for r in data.values()]
    for cik, sym, exch in rows:  # ordered by size; first row per CIK = primary class
        try:
            m.setdefault(int(cik), [str(sym).strip().upper().replace("-", "."), exch or ""])
        except (TypeError, ValueError):
            continue
    if len(m) < 1000:
        raise ValueError(f"beklenmeyen ticker listesi ({len(m)} satır)")
    return m


def _ticker_rows(cfg: dict) -> dict[int, list]:
    """CIK -> [primary ticker, exchange], cached in DATA_DIR and refreshed daily. Never raises."""
    global _tick_rows, _tick_checked
    with _tick_lock:
        now = now_ts()
        if _tick_rows is not None and now - _tick_checked < 86400:
            return _tick_rows
        cached = _load_ticker_file()
        if cached and cached.get("map") and now - int(cached.get("ts") or 0) < 86400:
            _tick_rows, _tick_checked = {int(k): v for k, v in cached["map"].items()}, int(cached["ts"])
            return _tick_rows
        stale = {int(k): v for k, v in cached["map"].items()} if cached and cached.get("map") else None
        if not str(cfg.get("sec_user_agent") or "").strip():
            return stale or _tick_rows or {}  # cannot download yet; do not mark as checked
        try:
            m = _download_tickers(cfg)
            TICKERS_PATH.write_text(jdump({"ts": now, "map": m}), encoding="utf-8")
            _tick_rows, _tick_checked = m, now
        except Exception as e:  # keep stale data and retry in an hour (5 min when we have nothing)
            log.warning("SEC ticker map refresh failed: %s", e)
            _tick_rows = stale or _tick_rows or {}
            _tick_checked = now - 86400 + (3600 if _tick_rows else 300)
        return _tick_rows


_views: tuple[object, dict[int, str], dict[int, str]] = (None, {}, {})


def _ticker_views(cfg: dict) -> tuple[dict[int, str], dict[int, str]]:
    global _views
    rows = _ticker_rows(cfg)
    if _views[0] is not rows:
        _views = (rows, {k: v[0] for k, v in rows.items()}, {k: v[1] for k, v in rows.items()})
    return _views[1], _views[2]


def cik_tickers(cfg: dict) -> dict[int, str]:
    """CIK -> primary ticker from SEC company_tickers(_exchange).json. Class-share dashes become dots
    (BRK-B -> BRK.B). Returns {} if unavailable."""
    return _ticker_views(cfg)[0]


def cik_exchange(cfg: dict) -> dict[int, str]:
    """CIK -> exchange ("Nasdaq", "NYSE", "OTC", "CBOE", ... or "" when unknown)."""
    return _ticker_views(cfg)[1]


# ---------------------------------------------------------------- Atom feed

_TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{1,10})\) \((?P<role>[^()]*)\)\s*$")
_ACC_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
_ITEM_RE = re.compile(r"Item\s+(\d{1,2}\.\d{2})")


def current_filings(form_type: str, cfg: dict, start: int = 0, count: int = 100, owner: str = "include") -> list[dict]:
    """Latest filings of a form type (EDGAR prefix match, e.g. "424B" -> 424B2/424B5...).
    owner="only" restricts to insider forms 3/4/5.
    -> [{form, name, cik, role, url, accession, ts, items}] (one entry per filer/subject/issuer)."""
    r = sec_get(CURRENT_URL, cfg, params={"action": "getcurrent", "type": form_type, "company": "", "dateb": "",
                                         "owner": owner, "start": start, "count": count, "output": "atom"})
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise RuntimeError("SEC yanıtı okunamadı (Atom akışı bekleniyordu)") from e
    out = []
    for e in root.iter(ATOM + "entry"):
        try:
            m = _TITLE_RE.match((e.findtext(ATOM + "title") or "").strip())
            if not m:
                continue
            summary = e.findtext(ATOM + "summary") or ""
            acc = _ACC_RE.search(e.findtext(ATOM + "id") or "") or _ACC_RE.search(summary)
            if not acc:
                continue
            link = e.find(ATOM + "link")
            cat = e.find(ATOM + "category")
            out.append({
                "form": ((cat.get("term") if cat is not None else "") or m["form"]).strip().upper(),
                "name": re.sub(r"\s+", " ", m["name"]).strip(),
                "cik": int(m["cik"]),
                "role": m["role"].strip(),
                "url": link.get("href", "") if link is not None else "",
                "accession": acc.group(1),
                "ts": to_ts(e.findtext(ATOM + "updated")),
                "items": list(dict.fromkeys(_ITEM_RE.findall(summary))),
            })
        except Exception:
            continue
    return out


def _group(entries: list[dict]) -> list[list[dict]]:
    """Entries sharing an accession number (issuer + reporting owner, co-registrants) -> one filing."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["accession"], []).append(e)
    return list(groups.values())


def _pick(group: list[dict], role: str, tick: dict[int, str] | None = None) -> dict:
    """Entry with the given role, preferring one whose CIK maps to a ticker."""
    cands = [e for e in group if e["role"].lower() == role.lower()] or group
    if tick:
        for e in cands:
            if e["cik"] in tick:
                return e
    return cands[0]


# ---------------------------------------------------------------- filing documents

_doc_lock = threading.Lock()
_doc_cache: OrderedDict[str, dict | None] = OrderedDict()   # accession -> parsed news item (None = skip)


def _cache_get(acc: str):
    with _doc_lock:
        return _doc_cache.get(acc, ...)


def _cache_put(acc: str, value: dict | None) -> None:
    with _doc_lock:
        _doc_cache[acc] = value
        while len(_doc_cache) > 6000:
            _doc_cache.popitem(last=False)


def submission_xml(cik: int, accession: str, cfg: dict) -> ET.Element | None:
    """First <XML> document of a filing's complete submission text (Form 4, Schedule 13D/G)."""
    url = ARCHIVE_URL.format(cik=cik, acc_nodash=accession.replace("-", ""), acc=accession)
    raw = sec_get(url, cfg).content
    m = re.search(rb"<XML>\s*(.*?)\s*</XML>", raw, re.S | re.I)
    if not m:
        return None
    return ET.fromstring(m.group(1))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _texts(root: ET.Element, name: str) -> list[str]:
    return [(el.text or "").strip() for el in root.iter() if _local(el.tag) == name and (el.text or "").strip()]


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def tr_num(x: float, dec: int = 0) -> str:
    """Turkish number format: 1.234.567,8"""
    return f"{x:,.{dec}f}".translate(str.maketrans(",.", ".,"))


def fmt_int(n: float | None) -> str:
    return tr_num(n) if n is not None else "?"


def fmt_money(v: float | None) -> str:
    """Turkish-style dollar amount: $1,2 milyon / $350 bin."""
    if not v:
        return ""
    for div, unit in ((1e9, "milyar"), (1e6, "milyon"), (1e3, "bin")):
        if v >= div:
            s = tr_num(v / div, 1)
            return f"${s[:-2] if s.endswith(',0') else s} {unit}"
    return f"${tr_num(v)}"


# ---------------------------------------------------------------- 8-K items

# item -> (Turkish label, English name, category hints)
ITEMS_8K: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "1.01": ("Önemli anlaşma", "Entry into a Material Definitive Agreement", ("contract",)),
    "1.02": ("Önemli anlaşmanın feshi", "Termination of a Material Definitive Agreement", ("contract",)),
    "1.03": ("İflas / kayyum", "Bankruptcy or Receivership", ("bankruptcy",)),
    "1.04": ("Maden güvenliği ihlali", "Mine Safety Reporting", ()),
    "1.05": ("Siber güvenlik olayı", "Material Cybersecurity Incidents", ()),
    "2.01": ("Varlık alımı/satışı tamamlandı", "Completion of Acquisition or Disposition of Assets", ("mna",)),
    "2.02": ("Finansal sonuçlar", "Results of Operations and Financial Condition", ("earnings",)),
    "2.03": ("Yeni borç yükümlülüğü", "Creation of a Direct Financial Obligation", ()),
    "2.04": ("Borcu hızlandıran tetikleyici olay", "Triggering Events That Accelerate a Financial Obligation", ()),
    "2.05": ("Yeniden yapılanma / çıkış maliyetleri", "Costs Associated with Exit or Disposal Activities", ("layoffs",)),
    "2.06": ("Önemli değer düşüklüğü", "Material Impairments", ()),
    "3.01": ("Borsadan çıkarma uyarısı", "Notice of Delisting or Failure to Satisfy a Continued Listing Rule", ()),
    "3.02": ("Kayıtsız hisse satışı", "Unregistered Sales of Equity Securities", ("offering",)),
    "3.03": ("Hissedar haklarında değişiklik", "Material Modification to Rights of Security Holders", ()),
    "4.01": ("Denetçi değişikliği", "Changes in Registrant's Certifying Accountant", ()),
    "4.02": ("Geçmiş finansallar geçersiz (düzeltme)", "Non-Reliance on Previously Issued Financial Statements", ()),
    "5.01": ("Kontrol değişikliği", "Changes in Control of Registrant", ("mna",)),
    "5.02": ("Yönetici değişikliği", "Departure/Election of Directors or Officers", ()),
    "5.03": ("Esas sözleşme / mali yıl değişikliği", "Amendments to Articles or Bylaws; Change in Fiscal Year", ()),
    "5.04": ("Emeklilik planında işlem askısı", "Temporary Suspension of Trading Under Employee Benefit Plans", ()),
    "5.05": ("Etik kuralları değişikliği", "Amendments to the Code of Ethics", ()),
    "5.06": ("Kabuk şirket statüsü değişti", "Change in Shell Company Status", ()),
    "5.07": ("Genel kurul oylama sonuçları", "Submission of Matters to a Vote of Security Holders", ()),
    "5.08": ("Hissedar aday gösterme", "Shareholder Director Nominations", ()),
    "6.01": ("ABS bilgileri", "ABS Informational and Computational Material", ()),
    "6.02": ("ABS hizmet sağlayıcı değişikliği", "Change of Servicer or Trustee", ()),
    "6.03": ("ABS kredi desteği değişikliği", "Change in Credit Enhancement", ()),
    "6.04": ("ABS dağıtım yapılamadı", "Failure to Make a Required Distribution", ()),
    "6.05": ("ABS menkul kıymet değişikliği", "Securities Act Updating Disclosure", ()),
    "7.01": ("Reg FD açıklaması (sunum)", "Regulation FD Disclosure", ()),
    "8.01": ("Diğer olaylar", "Other Events", ()),
    "9.01": ("Finansal tablolar ve ekler", "Financial Statements and Exhibits", ()),
}
# Display order in titles: most market-moving first.
_ITEM_ORDER = ["1.03", "4.02", "3.01", "2.02", "1.01", "2.01", "5.01", "1.05", "2.06", "2.05", "1.02", "5.02",
               "3.02", "2.03", "2.04", "3.03", "4.01", "8.01", "7.01", "5.07", "5.03", "5.06", "5.08"]


def _item_rank(i: str) -> int:
    if i == "9.01":
        return 999
    return _ITEM_ORDER.index(i) if i in _ITEM_ORDER else 500


def _item_label(i: str) -> str:
    return f"{i} {ITEMS_8K[i][0]}" if i in ITEMS_8K else f"Madde {i}"


# ---------------------------------------------------------------- collectors

class _SecBase(Collector):
    group = "official"
    kind = "news"
    source_type = "filing"
    requires = ("sec_user_agent",)
    feeds: tuple[str, ...] = ()
    pages = 1
    owner = "include"

    def _filings(self, cfg: dict) -> list[list[dict]]:
        def one(job: tuple[str, int]) -> list[dict] | Exception:
            try:
                return current_filings(job[0], cfg, start=job[1] * 100, owner=self.owner)
            except Exception as e:
                return e

        jobs = [(f, p) for f in self.feeds for p in range(self.pages)]
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as ex:
            results = list(ex.map(one, jobs))
        entries = [e for r in results if not isinstance(r, Exception) for e in r]
        errors = [r for r in results if isinstance(r, Exception)]
        if not entries and errors:
            raise errors[0]
        # Some form queries failed but others worked: report OK with a warning instead of staying silent.
        self._warnings = [f"Kısmi hata — {len(errors)}/{len(jobs)} EDGAR sorgusu alınamadı: "
                          f"{friendly_error(errors[0])}"[:300]] if errors else []
        if errors:
            log.warning("%s: %d/%d EDGAR queries failed: %s", self.key, len(errors), len(jobs), errors[0])
        cutoff = now_ts() - MAX_AGE
        return [g for g in _group(entries) if (g[0]["ts"] or 0) >= cutoff]

    def _finish(self, news: list[dict], cfg: dict) -> dict:
        exch = cik_exchange(cfg)
        for n in news:
            n["extra"]["exchange"] = exch.get(n["extra"].get("cik"), "")
        news.sort(key=lambda x: x["published_at"] or 0, reverse=True)
        res = {"news": news[:100]}
        warnings, self._warnings = getattr(self, "_warnings", None), []
        if warnings:
            res["warnings"] = warnings
        return res

    def _with_docs(self, groups: list[list[dict]], parse, max_docs: int) -> list[dict]:
        """Run parse(group) once per accession (results cached across runs), a few in parallel;
        the global throttle keeps the request rate under SEC's limit."""
        todo = [g for g in groups if _cache_get(g[0]["accession"]) is ...][:max_docs]
        deadline = time.monotonic() + DOC_BUDGET_S
        refused: list[RuntimeError] = []

        def work(g: list[dict]) -> None:
            if refused or time.monotonic() > deadline:
                return
            try:
                _cache_put(g[0]["accession"], parse(g))
            except RuntimeError as e:  # SEC refused us: stop the run
                refused.append(e)
            except requests.RequestException as e:  # transient: retry next run
                log.debug("%s: %s download failed: %s", self.key, g[0]["accession"], e)
            except Exception as e:  # malformed document: do not retry
                log.debug("%s: %s parse failed: %s", self.key, g[0]["accession"], e)
                _cache_put(g[0]["accession"], None)

        if todo:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(work, todo))
        if refused:
            raise refused[0]
        out = []
        for g in groups:
            hit = _cache_get(g[0]["accession"])
            if hit and hit is not ...:
                out.append(copy.deepcopy(hit))
        return out


class Sec8KCollector(_SecBase):
    key = "sec_8k"
    name = "SEC 8-K"
    interval = 180
    feeds = ("8-K",)
    description = "SEC EDGAR'a düşen son 8-K önemli olay bildirimleri (bilanço, anlaşma, yönetici değişikliği, iflas...)."

    def fetch(self, cfg: dict) -> dict:
        tick = cik_tickers(cfg)
        news = []
        for g in self._filings(cfg):
            try:
                e = _pick(g, "Filer", tick)
                if not e["form"].startswith("8-K"):
                    continue
                items = sorted({i for x in g for i in x["items"]}, key=_item_rank)
                shown = [i for i in items if i != "9.01"][:2] or items[:1]
                label = ", ".join(_item_label(i) for i in shown)
                title = f"{e['form']}: {e['name']}" + (f" — {label}" if label else "")
                tickers = list(dict.fromkeys(tick[x["cik"]] for x in g if x["cik"] in tick))
                parts = [f"{e['name']} SEC'e {e['form']} bildirimi yaptı."]
                if items:
                    parts.append("Maddeler: " + "; ".join(
                        f"{i} {ITEMS_8K[i][0]} ({ITEMS_8K[i][1]})" if i in ITEMS_8K else f"Madde {i}" for i in items) + ".")
                if e["form"].endswith("/A"):
                    parts.append("Önceki bildirimin düzeltmesi.")
                cats = list(dict.fromkeys(c for i in items for c in ITEMS_8K.get(i, ("", "", ()))[2])) + ["filing"]
                it = self.item(title, e["url"], e["ts"], " ".join(parts)[:600], tickers, e["name"], {
                    "form": e["form"], "cik": e["cik"], "accession": e["accession"], "company": e["name"],
                    "items": items, "items_tr": {i: ITEMS_8K[i][0] for i in items if i in ITEMS_8K},
                })
                it["categories"] = cats
                news.append(it)
            except Exception:
                continue
        return self._finish(news, cfg)


class SecForm4Collector(_SecBase):
    key = "sec_form4"
    name = "SEC Form 4 (içeriden)"
    interval = 300
    feeds = ("4",)
    pages = 2
    owner = "only"
    description = ("İçeriden öğrenen (yönetici, YK üyesi, %10+ ortak) açık piyasa alım/satımları — "
                   "sadece P (alım) ve S (satış) işlemleri; hibe/opsiyon kayıtları atlanır.")

    def fetch(self, cfg: dict) -> dict:
        tick = cik_tickers(cfg)
        groups = [g for g in self._filings(cfg) if g[0]["form"] in ("4", "4/A")]
        news = self._with_docs(groups, lambda g: self._parse(g, cfg, tick), max_docs=100)
        return self._finish(news, cfg)

    def _parse(self, g: list[dict], cfg: dict, tick: dict[int, str]) -> dict | None:
        issuer_e = _pick(g, "Issuer")
        root = submission_xml(issuer_e["cik"], issuer_e["accession"], cfg)
        if root is None or _local(root.tag) != "ownershipDocument":
            return None

        def val(el, path: str) -> str:
            x = el.find(path)
            return (x.text or "").strip() if x is not None and x.text else ""

        buy_sh = buy_val = sell_sh = sell_val = 0.0
        txs, after, dates = [], None, []
        for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            code = val(tx, "transactionCoding/transactionCode").upper()
            if code not in ("P", "S"):
                continue
            sh = _num(val(tx, "transactionAmounts/transactionShares/value")) or 0.0
            px = _num(val(tx, "transactionAmounts/transactionPricePerShare/value"))
            if sh <= 0:
                continue
            if code == "P":
                buy_sh += sh
                buy_val += sh * (px or 0)
            else:
                sell_sh += sh
                sell_val += sh * (px or 0)
            v = _num(val(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
            if v is not None:   # 0 = the insider sold the whole stake
                after = v
            d = val(tx, "transactionDate/value")[:10]
            dates.append(d)
            txs.append({"code": code, "date": d, "shares": sh, "price": px,
                        "security": val(tx, "securityTitle/value")})
        if not txs:
            return None  # grants, option exercises, tax withholding, gifts: not trading signals

        issuer = val(root, "issuer/issuerName") or issuer_e["name"]
        sym = val(root, "issuer/issuerTradingSymbol").upper().replace("-", ".")
        if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,6}", sym) or sym in ("NONE", "NA", "N.A"):
            sym = tick.get(issuer_e["cik"], "")
        owners = root.findall("reportingOwner")
        owner_el = owners[0] if owners else root
        owner = val(owner_el, "reportingOwnerId/rptOwnerName") or _pick(g, "Reporting")["name"]
        rel = owner_el.find("reportingOwnerRelationship")
        flag = lambda p: rel is not None and val(rel, p).lower() in ("1", "true")
        roles = []
        if flag("isOfficer"):
            title_raw = val(rel, "officerTitle")
            roles.append("Yönetici" if not title_raw or title_raw.lower().startswith("see remark") else title_raw)
        if flag("isDirector"):
            roles.append("YK üyesi")
        if flag("isTenPercentOwner"):
            roles.append("%10+ ortak")
        if flag("isOther"):
            other = val(rel, "otherText")
            roles.append("Diğer" if not other or other.lower().startswith("see remark") else other)
        role = ", ".join(roles) or "İçeriden"
        plan_raw = val(root, "aff10b5One").lower()
        plan = True if plan_raw in ("1", "true") else False if plan_raw in ("0", "false") else None

        direction = "buy" if buy_sh and not sell_sh else "sell" if sell_sh and not buy_sh else "mixed"
        if direction == "buy":
            action = f"ALIM {fmt_int(buy_sh)} hisse" + (f", ~{fmt_money(buy_val)}" if buy_val else "")
        elif direction == "sell":
            action = f"SATIŞ {fmt_int(sell_sh)} hisse" + (f", ~{fmt_money(sell_val)}" if sell_val else "")
        else:
            action = f"ALIM {fmt_int(buy_sh)} / SATIŞ {fmt_int(sell_sh)} hisse"
        more = f" +{len(owners) - 1} kişi" if len(owners) > 1 else ""
        title = f"Form 4: {issuer} — {owner}{more} ({role}): {action}" + (" · 10b5-1 planlı" if plan else "")

        avg_buy = buy_val / buy_sh if buy_sh and buy_val else None
        avg_sell = sell_val / sell_sh if sell_sh and sell_val else None
        parts = [f"İçeriden işlem bildirimi: {owner} ({role}), {issuer}" + (f" ({sym})" if sym else "") + "."]
        if buy_sh:
            parts.append(f"Açık piyasa alımı: {fmt_int(buy_sh)} hisse" +
                         (f", ort. ${tr_num(avg_buy, 2)}, toplam ~{fmt_money(buy_val)}" if avg_buy else "") + ".")
        if sell_sh:
            parts.append(f"Açık piyasa satışı: {fmt_int(sell_sh)} hisse" +
                         (f", ort. ${tr_num(avg_sell, 2)}, toplam ~{fmt_money(sell_val)}" if avg_sell else "") + ".")
        if dates:
            parts.append(f"İşlem tarihi: {min(dates)}" + (f" – {max(dates)}" if max(dates) != min(dates) else "") + ".")
        if after is not None:
            parts.append(f"İşlem sonrası pay: {fmt_int(after)} hisse.")
        if plan is not None:
            parts.append("Önceden planlı (10b5-1) işlem." if plan else "10b5-1 planı dışında.")
        it = self.item(title, issuer_e["url"], issuer_e["ts"], " ".join(parts)[:600], [sym] if sym else [], owner, {
            "form": issuer_e["form"], "cik": issuer_e["cik"], "accession": issuer_e["accession"], "issuer": issuer,
            "owner": owner, "owner_count": len(owners), "role": role,
            "is_officer": flag("isOfficer"), "is_director": flag("isDirector"), "is_ten_pct": flag("isTenPercentOwner"),
            "direction": direction, "buy_shares": buy_sh, "buy_value": round(buy_val, 2),
            "sell_shares": sell_sh, "sell_value": round(sell_val, 2),
            "avg_buy_price": round(avg_buy, 4) if avg_buy else None,
            "avg_sell_price": round(avg_sell, 4) if avg_sell else None,
            "shares_after": after, "plan_10b5_1": plan, "transactions": txs[:20],
        })
        it["categories"] = ["insider", "filing"]
        return it


# form -> (Turkish label, label when filer is not yet listed, kind)
OFFER_FORMS: dict[str, tuple[str, str, str]] = {
    "S-1": ("Hisse kayıt başvurusu (ek arz / satış olabilir)", "Halka arz başvurusu (IPO)", "registration"),
    "S-1/A": ("Kayıt başvurusu güncellemesi", "Halka arz başvurusu güncellemesi", "registration"),
    "S-1MEF": ("Arz büyüklüğü artırımı", "Halka arz büyüklüğü artırımı", "registration"),
    "F-1": ("Yabancı şirket hisse kayıt başvurusu", "Yabancı şirket halka arz başvurusu (IPO)", "registration"),
    "F-1/A": ("Yabancı şirket kayıt güncellemesi", "Yabancı şirket halka arz güncellemesi", "registration"),
    "F-1MEF": ("Arz büyüklüğü artırımı", "Halka arz büyüklüğü artırımı", "registration"),
    "424B1": ("Nihai izahname (arz)", "Nihai izahname (halka arz)", "pricing"),
    "424B4": ("Arz fiyatlandı (nihai izahname)", "Halka arz fiyatlandı (nihai izahname)", "pricing"),
    "424B5": ("Hisse/tahvil satışı (izahname eki)", "Menkul kıymet satışı (izahname eki)", "takedown"),
    "S-3": ("Raf kaydı: ileride hisse satışı olabilir", "Raf kaydı", "shelf"),
    "S-3ASR": ("Otomatik raf kaydı (büyük şirket)", "Otomatik raf kaydı", "shelf"),
    "F-3": ("Yabancı şirket raf kaydı", "Yabancı şirket raf kaydı", "shelf"),
}


class SecOfferingsCollector(_SecBase):
    key = "sec_offerings"
    name = "SEC Arz & İhraç"
    interval = 600
    # Per-form 424B queries: the "424B" prefix query is ~80% bank structured notes (424B2/424B3), so its
    # 100-row window can miss the dilutive 424B5 takedowns and 424B4 IPO pricings.
    feeds = ("S-1", "F-1", "424B1", "424B4", "424B5", "S-3", "F-3")
    description = ("Halka arz başvuruları (S-1/F-1), arz fiyatlamaları (424B4) ve hisse satışları / raf kayıtları "
                   "(424B5, S-3) — sulanma (dilution) riski.")

    def fetch(self, cfg: dict) -> dict:
        tick = cik_tickers(cfg)
        news = []
        for g in self._filings(cfg):
            try:
                e = _pick(g, "Filer", tick)
                info = OFFER_FORMS.get(e["form"])
                if not info:
                    continue  # 424B2/424B3 structured notes, S-3D etc.
                listed_label, ipo_label, kind = info
                sym = tick.get(e["cik"], "")
                is_ipo = not sym and kind in ("registration", "pricing")
                label = ipo_label if not sym else listed_label
                title = f"{e['form']}: {e['name']} — {label}"
                summary = (f"{e['name']}" + (f" ({sym})" if sym else " (henüz borsada işlem görmüyor)") +
                           f" SEC'e {e['form']} sundu: {label}.")
                it = self.item(title, e["url"], e["ts"], summary, [sym] if sym else [], e["name"], {
                    "form": e["form"], "cik": e["cik"], "accession": e["accession"], "company": e["name"],
                    "offer_kind": kind, "ipo": is_ipo,
                })
                it["categories"] = (["ipo"] if is_ipo else ["offering"]) + ["filing"]
                news.append(it)
            except Exception:
                continue
        return self._finish(news, cfg)


# form -> (short label, Turkish meaning, category hints)
OWNERSHIP_FORMS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "SCHEDULE 13D": ("13D", "aktivist / kontrol amaçlı %5+ pay bildirimi", ("filing",)),
    "SCHEDULE 13D/A": ("13D/A", "aktivist pay güncellemesi", ("filing",)),
    "SCHEDULE 13G": ("13G", "pasif %5+ pay bildirimi", ("filing",)),
    "SC TO-T": ("SC TO-T", "satın alma (tender) teklifi", ("mna", "filing")),
    "SC TO-T/A": ("SC TO-T/A", "satın alma teklifi güncellemesi", ("mna", "filing")),
    "SC 14D9": ("SC 14D9", "hedef şirketin teklife yanıtı", ("mna", "filing")),
    "SC 14D9/A": ("SC 14D9/A", "teklif yanıtı güncellemesi", ("mna", "filing")),
}


class SecOwnershipCollector(_SecBase):
    key = "sec_13d"
    name = "SEC 13D/13G & Teklifler"
    interval = 600
    feeds = ("SCHEDULE 13D", "SCHEDULE 13G", "SC TO-T", "SC 14D9")
    description = ("Aktivist yatırımcı (13D) ve büyük pasif ortak (13G) %5+ pay bildirimleri, "
                   "satın alma (tender) teklifleri ve hedef şirket yanıtları.")

    def fetch(self, cfg: dict) -> dict:
        tick = cik_tickers(cfg)
        groups = [g for g in self._filings(cfg) if g[0]["form"] in OWNERSHIP_FORMS]
        news = self._with_docs(groups, lambda g: self._parse(g, cfg, tick), max_docs=60)
        return self._finish(news, cfg)

    def _parse(self, g: list[dict], cfg: dict, tick: dict[int, str]) -> dict | None:
        subj = _pick(g, "Subject", tick)
        filer = next((e for e in g if e["role"].lower() == "filed by"), None)
        form = subj["form"]
        short, meaning, cats = OWNERSHIP_FORMS[form]
        pct = shares = None
        event_date = purpose = ""
        if form.startswith("SCHEDULE 13"):
            root = submission_xml(subj["cik"], subj["accession"], cfg)
            if root is not None:
                pcts = [p for p in map(_num, _texts(root, "percentOfClass") + _texts(root, "classPercent")) if p is not None]
                amts = [a for a in map(_num, _texts(root, "aggregateAmountOwned") +
                                       _texts(root, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")) if a is not None]
                pct = max(pcts) if pcts else None
                shares = max(amts) if amts else None
                event_date = (_texts(root, "dateOfEvent") + _texts(root, "eventDateRequiresFilingThisStatement") + [""])[0]
                purpose = (_texts(root, "transactionPurpose") + [""])[0]
                if not filer:
                    names = _texts(root, "reportingPersonName")
                    filer = {"name": names[0]} if names else None
        filer_name = filer["name"] if filer else ""
        sym = tick.get(subj["cik"], "")
        pct_s = f"%{pct:.1f}".replace(".", ",") if pct is not None else ""
        title = f"{short}: {subj['name']}" + (f" — {filer_name}" if filer_name else "") + f": {meaning}" + \
                (f" ({pct_s})" if pct_s else "")
        parts = [(f"{filer_name}, " if filer_name else "") + f"{subj['name']}" + (f" ({sym})" if sym else "") +
                 f" için {form} sundu: {meaning}."]
        if pct_s:
            parts.append(f"Bildirilen pay: {pct_s}" + (f" ({fmt_int(shares)} hisse)" if shares else "") + ".")
        if event_date:
            parts.append(f"Olay tarihi: {event_date}.")
        if purpose:
            parts.append("Amaç (Item 4): " + re.sub(r"\s+", " ", purpose)[:300])
        it = self.item(title, subj["url"], subj["ts"], " ".join(parts)[:600], [sym] if sym else [], filer_name, {
            "form": form, "cik": subj["cik"], "accession": subj["accession"], "company": subj["name"],
            "filer": filer_name, "percent": pct, "shares": shares, "event_date": event_date,
            "activist": form.startswith("SCHEDULE 13D"),
        })
        it["categories"] = list(cats)
        return it


class Sec6KCollector(_SecBase):
    key = "sec_6k"
    name = "SEC 6-K (yabancı şirket)"
    interval = 900
    feeds = ("6-K",)
    description = ("ABD borsalarında işlem gören yabancı şirketlerin (ADR) 6-K raporları — çoğu zaman bilanço ve "
                   "önemli duyurular (OTC hisseleri atlanır).")

    def fetch(self, cfg: dict) -> dict:
        tick, exch = cik_tickers(cfg), cik_exchange(cfg)
        news = []
        for g in self._filings(cfg):
            try:
                e = _pick(g, "Filer", tick)
                sym = tick.get(e["cik"])
                if not sym or exch.get(e["cik"]) not in EXCHANGE_LISTED or not e["form"].startswith("6-K"):
                    continue  # only exchange-listed ADRs / foreign issuers (OTC skipped)
                title = f"{e['form']}: {e['name']} — Yabancı şirket raporu"
                summary = f"{e['name']} ({sym}) SEC'e {e['form']} raporu sundu (bilanço, duyuru veya genel kurul belgesi olabilir)."
                it = self.item(title, e["url"], e["ts"], summary, [sym], e["name"], {
                    "form": e["form"], "cik": e["cik"], "accession": e["accession"], "company": e["name"]})
                it["categories"] = ["filing"]
                news.append(it)
            except Exception:
                continue
        return self._finish(news, cfg)


COLLECTORS = [Sec8KCollector(), SecForm4Collector(), SecOfferingsCollector(), SecOwnershipCollector(), Sec6KCollector()]
