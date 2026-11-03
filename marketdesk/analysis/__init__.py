"""Local, rule-based analysis: tickers, categories, sentiment, importance, clusters, opportunities."""
from __future__ import annotations

import logging
import re

from . import classify, ml, tickers
from .classify import CATEGORIES

log = logging.getLogger("marketdesk.analysis")

MAX_TICKERS = tickers.MAX_TICKERS

# Bump on every classification / importance / sentiment rule change: the scheduler re-enriches stored news whose
# extra.analysis_version differs, then re-clusters the whole retained window once.
ANALYSIS_VERSION = 1

# Discrete corporate events: credited to a ticker only when the title says so, or the same summary sentence
# names that ticker (multi-story digests mention other companies' IPOs / deals).
EVENT_CATEGORIES = {"ipo", "mna", "fda", "offering", "split", "reverse_split", "bankruptcy", "halt"}
OUTCOME_KEYS = ("earnings", "fda", "dividend", "insider", "clinical", "delisting")


def _glue_stripper():
    """collectors.rss.strip_glued_headline (Nasdaq.com summaries with a glued 'related article' headline), or None."""
    try:
        from ..collectors.rss import strip_glued_headline
        return strip_glued_headline
    except Exception:  # noqa: BLE001 - analysis works without the collectors package
        return None


def strip_publisher(title: str, publisher: str = "") -> str:
    """'Nvidia jumps - Reuters' -> 'Nvidia jumps' (Google News style titles)."""
    t = (title or "").strip()
    if publisher:
        for sep in (" - ", " | ", " — ", " – "):
            suffix = sep + publisher.strip()
            if t.lower().endswith(suffix.lower()):
                return t[: -len(suffix)].rstrip()
    return t


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _dedupe_summary(title: str, summary: str, publisher: str = "") -> str:
    """Drop summaries that only repeat the title (+ publisher), common for aggregators."""
    if not summary:
        return ""
    ns, nt = _norm(summary), _norm(title)
    if not nt:
        return summary
    if ns.startswith(nt[:60]):
        rest = ns[len(nt):].strip()
        if not rest or rest == _norm(publisher):
            return ""
    return summary


# WSJ newsletter digests append unrelated stories after "Plus, ..."; crypto pages end with exchange promos.
_RX_DIGEST = re.compile(r"(?:^|(?<=[.!?])\s+)Plus,\s.*$", re.S)
_RX_PROMO = re.compile(r"You can (?:trade|buy) .*?(?:\.|$)|you could earn up to[^.]*\.|"
                       r"Feeling (?:confident|bullish|bearish) about[^?]*\?", re.I | re.S)


def clean_summary(summary: str) -> str:
    """Summary text used for tickers / categories: digest tails and promo boilerplate removed."""
    if not summary:
        return ""
    s = _RX_DIGEST.sub("", summary)
    s = _RX_PROMO.sub(" ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _as_list(v) -> list:
    if not v:
        return []
    if isinstance(v, str):
        return [x for x in re.split(r"[\s,;]+", v) if x]
    return [x for x in v if x is not None and x != ""] if isinstance(v, (list, tuple, set)) else []


# Text between two company mentions that makes them one list ("Coca-Cola (KO) and PepsiCo", "Tesla-SpaceX").
_RX_LIST_SEP = re.compile(r"(?:'s)?\s*(?:\([^()]{0,40}\))?\s*(?:,\s*(?:and\s+|&\s*)?|&|\band\b|•|/|\+|-|–)\s*",
                          re.I)


def title_subjects(title: str, tags, watch=None, social: bool = False,
                   spans: list[tuple[str, int, int]] | None = None) -> tuple[list[str], list[str]]:
    """(title tickers in order of appearance, subject tickers). Title tickers = symbols extracted from the title
    plus source tags whose symbol / company name appears in it. The subject is the first non-ETF title ticker,
    together with the tickers listed with it ("Meta, Microsoft and Alphabet", "Tesla-SpaceX merger")."""
    title = title or ""
    if spans is None:
        spans = tickers.extract_spans(title, watch, social=social)
    pos = {s: (a, b) for s, a, b in spans}
    for s in tags or ():
        if s not in pos:
            sp = tickers.mention_span(s, title)
            if sp:
                pos[s] = sp
    order = sorted(pos.items(), key=lambda kv: kv[1][0])
    subj: list[str] = []
    for k, (s, (_, end)) in enumerate(order):
        if tickers.is_etf(s):
            continue
        subj = [s]
        for s2, (a2, b2) in order[k + 1:]:
            if a2 < end or _RX_LIST_SEP.fullmatch(title[end:a2]):
                subj.append(s2)
                end = max(end, b2)
            else:
                break
        break
    return [s for s, _ in order], subj


_RX_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
# Abbreviations whose dot is not a sentence end ("U.S. IPO plan", "Apple Inc. Shares"): masked before splitting.
_RX_ABBR = re.compile(r"(?<![A-Za-z])(U\.S|U\.K|Inc|Corp|Co|Ltd|St|Jr|No)\.(?=\s)")


def _split_sentences(text: str) -> list[str]:
    masked = _RX_ABBR.sub(lambda m: m.group(1) + "\x00", text or "")
    return [p.replace("\x00", ".") for p in _RX_SENT.split(masked)]


def _summary_categories(summary: str, tc: dict, sc: dict, tags, watch, social: bool) -> dict[str, list[str]]:
    """{category: tickers in the sentences that triggered it} for categories found only in the summary with a
    clear rule (factor >= 0.7). Ticker lists are only computed for EVENT_CATEGORIES."""
    out: dict[str, list[str]] = {}
    cats = [c for c, f in sc.items() if c not in tc and f >= 0.7]
    events = [c for c in cats if c in EVENT_CATEGORIES]
    sent_info = []
    if events:
        for sent in _split_sentences(summary):
            f = classify.features(sent)[0]
            if any(c in f for c in events):
                syms = tickers.extract(sent, watch, social=social)
                syms += [s for s in tags or () if s not in syms and tickers.mention_span(s, sent)]
                sent_info.append((f, syms))
    for c in cats:
        if c in EVENT_CATEGORIES:
            syms: list[str] = []
            for f, ss in sent_info:
                if f.get(c, 0.0) >= 0.7:
                    syms += [s for s in ss if s not in syms]
            out[c] = syms
        else:
            out[c] = []
    return out


def derive_meta(title: str, summary: str, tags, categories, extra: dict | None = None, watch=None,
                social: bool = False, tc: dict | None = None, sc: dict | None = None,
                title_spans: list | None = None, given=(), summary_syms: list | None = None) -> dict:
    """Per-item attribution data stored in `extra` by enrich (and recomputed for older rows by opportunities):
    title_tickers, subject_tickers, summary_tickers, title_categories, summary_categories, roundup (market wrap /
    digest / movers list) and <category>_outcome (classify.outcomes)."""
    summary = clean_summary(summary)
    if tc is None:
        tc = classify.features(title)[0]
    if sc is None:
        sc = classify.features(summary)[0]
    tt, subj = title_subjects(title, tags, watch, social, title_spans)
    s_syms = summary_syms if summary_syms is not None else tickers.extract(summary, watch, social=social)
    given = set(given or ())
    meta = {
        "title_tickers": tt,
        "subject_tickers": subj,
        "summary_tickers": [s for s in s_syms if s not in tt],
        "title_categories": [c for c in categories or () if tc.get(c, 0.0) >= 0.5 or c in given],
        "summary_categories": _summary_categories(summary, tc, sc, tags, watch, social),
        "roundup": classify.is_roundup(title, tc),
    }
    for k, v in classify.outcomes(title, summary, categories, extra).items():
        meta[k + "_outcome"] = v
    return meta


def enrich(items: list[dict], cfg: dict) -> list[dict]:
    """Fill tickers / categories / sentiment / sentiment_label / importance in place and return the list.
    Also stores attribution data in `extra` (see derive_meta) and flags quote pages (`extra.junk`)."""
    cfg = cfg or {}
    watch = [w for w in (tickers.normalize_symbol(x) for x in (cfg.get("watchlist") or [])) if w]
    acfg = dict(cfg, watchlist=watch)
    analysed: list[tuple[dict, str, dict]] = []
    unglue = _glue_stripper()
    for it in items:
        try:
            extra = dict(it["extra"]) if isinstance(it.get("extra"), dict) else {}
            publisher = str(extra.get("publisher") or "")
            title = strip_publisher(str(it.get("title") or ""), publisher)
            summary = clean_summary(_dedupe_summary(title, str(it.get("summary") or ""), publisher))
            if unglue is not None and summary and "nasdaq.com" in str(it.get("url") or ""):
                summary = unglue(title, summary)   # defensive: the collector already strips new items
            social = it.get("source_type") == "social"

            tags: list[str] = []
            for s in _as_list(it.get("tickers")):
                n = tickers.normalize_symbol(str(s))
                if n and n not in tags:
                    tags.append(n)
            t_spans = tickers.extract_spans(title, watch, social=social)
            s_syms = tickers.extract(summary, watch, social=social)
            tt, _ = title_subjects(title, tags, watch, social, t_spans)
            syms = list(dict.fromkeys(tt + tags + s_syms))[:MAX_TICKERS]

            given = [c for c in _as_list(it.get("categories")) if c in CATEGORIES]
            base = dict(it, title=title, summary=summary, tickers=syms, extra=dict(extra, title_tickers=tt))
            a = classify.analyze(base, acfg)
            scores = a["scores"]
            for c in given:
                if c not in scores:
                    scores[c] = CATEGORIES[c]["weight"] * 0.9
            cats = list(dict.fromkeys(given + a["categories"]))
            if any(c not in a["categories"] for c in given):
                a["importance"] = classify.importance(dict(base, sentiment=a["sentiment"]), acfg, scores, a["flags"])
            if a["flags"].get("junk"):
                cats = []
                extra["junk"] = True
            meta = derive_meta(title, summary, tags, cats, extra, watch, social, a["title_cats"], a["summary_cats"],
                               t_spans, given, s_syms)
            extra.update(meta)
            it["extra"] = extra
            it["tickers"] = syms
            it["categories"] = cats
            it["sentiment"] = a["sentiment"]
            it["sentiment_label"] = a["sentiment_label"]
            it["importance"] = a["importance"]
            analysed.append((it, title, dict(base, scores=scores, flags=a["flags"])))
        except Exception:  # noqa: BLE001 - one bad item must not break the batch
            log.exception("enrich failed for %r", str(it.get("title") or "")[:80])
            it.setdefault("tickers", [])
            it.setdefault("categories", [])
            it.setdefault("sentiment", 0.0)
            it.setdefault("sentiment_label", "neu")
            it.setdefault("importance", 0.0)

    if analysed and cfg.get("use_finbert") and ml.finbert_available():
        fb = ml.finbert_scores([t for _, t, _ in analysed])
        if fb:
            for (it, _, base), f in zip(analysed, fb):
                s = round(max(-1.0, min(1.0, 0.5 * it["sentiment"] + 0.5 * f)), 3)
                it["sentiment"] = s
                it["sentiment_label"] = classify.sentiment_label(s)
                it["importance"] = classify.importance(dict(base, sentiment=s), acfg, base["scores"], base["flags"])
    return items


__all__ = ["enrich", "strip_publisher", "clean_summary", "title_subjects", "derive_meta", "EVENT_CATEGORIES",
           "CATEGORIES", "classify", "tickers", "ml"]
