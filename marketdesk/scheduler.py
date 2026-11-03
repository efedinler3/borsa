"""Background loop: runs collectors on their intervals, enriches/stores results, clusters, scores
opportunities, writes scheduled briefs and does maintenance. Analysis/briefs modules are imported lazily
so a missing or broken module only disables its own step."""
from __future__ import annotations

import importlib
import inspect
import logging
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from . import db
from .config import load_config
from .util import _PY_ERRORS, HTTP_LABELS, ET, friendly_error, is_trading_day, last_close_ts, market_session, now_ts, to_ts  # noqa: F401 - re-exported

log = logging.getLogger("marketdesk.scheduler")

TICK = 5                    # loop period (s)
WORKERS = 6                 # concurrent collector runs
STARTUP_SPREAD = 20         # first collector runs are staggered over this many seconds
CLUSTER_DEBOUNCE = 60       # min seconds between clustering runs (only after new news)
OPPS_EVERY = 120            # opportunity scoring period
BRIEF_CHECK_EVERY = 60      # due-brief check period
CLAUDE_IMPORT_EVERY = 60    # Claude Code output import period
MOVERS_WHY_EVERY = 120      # catalyst ("Neden?") lookup for the movers tables
MOVERS_WHY_HOURS = 20       # catalyst news window: at least this, back to the close before the listed session
MOVER_LISTS = ("gainers", "losers", "actives", "premarket", "postmarket", "small_caps")
SIGNALS_EVERY = 600         # full opportunity history row set (bot backtests) while a session is on ...
SIGNALS_EVERY_CLOSED = 3600  # ... hourly while the market is closed
SIGNAL_SCORE_STEP = 8       # plus an immediate row when a ticker's score moves this much or its direction flips
PRUNE_EVERY = 86400
REANALYZE_EVERY = 600       # re-derive analysis fields of stored news whose analysis_version is stale
REANALYZE_BATCH = 500       # rows per enrich batch (at most REANALYZE_MAX_BATCHES per pass)
REANALYZE_MAX_BATCHES = 20
RETAIN_DAYS = 21            # news retention (db.prune default): the full re-cluster window
SYMBOLS_EVERY = 86400       # refresh_symbols itself only downloads when the cache is > 7 days old
SLOW_RUN_WARN = 180         # log a warning when a collector run exceeds this
BACKOFF_CAP = 1800          # max delay after repeated failures (s), unless the interval is longer
WARMUP_MIN, WARMUP_MAX = 60, 300  # scheduled briefs wait for the first collection round (bounded)
CRYPTO_EVERY = 60           # crypto paper bots (24/7): stops / targets, the 00:05 UTC daily pass
PAPER_EVERY = 15            # paper trading engine step (fills, stops, bot); own thread so analysis cannot delay it
SCORECARD_EVERY = 1800      # signal scorecard: evaluates signals whose entry day has closed (Yahoo 5-minute bars)
LAB_EVERY = 120             # test lab: daily data update after the close, referee trades at the open


# ---------------------------------------------------------------- helpers

def requirement_hint(missing: list[str]) -> str:
    """Turkish hint for a collector whose required config keys are empty."""
    if not missing:
        return ""
    if all(k == "sec_user_agent" for k in missing):
        return "Ayarlar'da SEC e-posta adresi gerekli"
    return "Ayarlar'da API anahtarı gerekli"


def _lazy(modname: str):
    """Import marketdesk.<modname> or return None (other modules may not exist yet)."""
    try:
        return importlib.import_module(f"{__package__}.{modname}")
    except Exception as e:  # noqa: BLE001 - absent or broken module just disables a step
        log.debug("module %s unavailable: %s", modname, e)
        return None


def _snapshot_size(data) -> int:
    if isinstance(data, dict):
        n = 0
        for k in ("items", "gainers", "losers", "actives", "apewisdom", "stocktwits"):
            v = data.get(k)
            if isinstance(v, (list, dict)):
                n += len(v)
        return n or 1
    if isinstance(data, list):
        return len(data)
    return 1


def _clean_news(it: dict, c) -> dict | None:
    """Coerce a collector news dict into something db.upsert_news cannot choke on."""
    if not isinstance(it, dict):
        return None
    title = it.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    out = dict(it)
    out["title"] = " ".join(title.split())
    out["source"] = str(out.get("source") or c.key)
    out["source_name"] = str(out.get("source_name") or c.name or c.key)
    out["source_type"] = str(out.get("source_type") or c.source_type or "news")
    out["published_at"] = to_ts(out.get("published_at"))
    tk = out.get("tickers") or []
    out["tickers"] = [str(t).upper().strip() for t in (tk if isinstance(tk, (list, tuple, set)) else [tk]) if str(t).strip()]
    if not isinstance(out.get("extra"), dict):
        out["extra"] = {}
    for k in ("summary", "url", "author"):
        if out.get(k) is not None and not isinstance(out[k], str):
            out[k] = str(out[k])
    return out


def _valid_event(e) -> bool:
    if not isinstance(e, dict) or not e.get("title"):
        return False
    try:
        int(e.get("ts"))
    except (TypeError, ValueError):
        return False
    return True


def _existing_ids(ids: list[str]) -> set[str]:
    found: set[str] = set()
    c = db.conn()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = c.execute(f"SELECT id FROM news WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        found.update(r[0] for r in rows)
    return found


def _float(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _match_story(stories: list[dict], hit: dict) -> tuple[dict, dict]:
    """(story, news item) behind a briefs._why hit. _why returns only {title, url, sources}, so the item is found
    again by url (then title) among the grouped stories; {} when nothing matches."""
    url, title = hit.get("url") or "", hit.get("title") or ""
    for s in stories:
        for it in s.get("items") or []:
            if (url and it.get("url") == url) or (not url and it.get("title") == title):
                return s, it
    for s in stories:
        if (url and s.get("url") == url) or (not url and s.get("title") == title):
            items = s.get("items") or []
            return s, max(items, key=lambda n: float(n.get("importance") or 0), default={})
    return {}, {}


def _movers_news_start(ts: int) -> int:
    """News window start for a mover's catalyst: the close before the regular session the screener lists
    describe (the latest one that has opened by `ts`; extended-hours moves come after that close anyway)."""
    d = datetime.fromtimestamp(ts, ET).date()
    open_ts = int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).timestamp())
    if is_trading_day(d) and ts >= open_ts:
        return last_close_ts(open_ts)
    return last_close_ts(last_close_ts(ts) - 60)


def _rel_volumes() -> dict[str, float]:
    """symbol -> volume / average volume from the market snapshots (signal rows when the scorer gives none)."""
    out: dict[str, float] = {}

    def put(r) -> None:
        if not isinstance(r, dict) or not r.get("symbol"):
            return
        sym = str(r["symbol"]).upper()
        if sym in out:
            return
        rv = r.get("rel_volume")
        try:
            if rv is None and r.get("volume") and r.get("avg_volume"):
                rv = float(r["volume"]) / float(r["avg_volume"])
            if rv is not None:
                out[sym] = round(float(rv), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    for key in ("movers", "movers_alpaca"):
        snap = db.get_snapshot_data(key)
        if isinstance(snap, dict):
            for lst in MOVER_LISTS:
                for r in snap.get(lst) or []:
                    put(r)
    quotes = db.get_snapshot_data("quotes")
    for r in (quotes or {}).get("items") or [] if isinstance(quotes, dict) else []:
        put(r)
    return out


class _JobState:
    __slots__ = ("next_due", "running", "queued_at", "last_start", "last_end", "last_duration", "fails",
                 "forced", "fetched_last", "new_last", "last_error", "warning")

    def __init__(self, next_due: float) -> None:
        self.next_due = next_due
        self.running = False
        self.queued_at = 0.0
        self.last_start = 0
        self.last_end = 0
        self.last_duration: float | None = None
        self.fails = 0
        self.forced = False
        self.fetched_last = 0
        self.new_last = 0
        self.last_error: str | None = None
        self.warning: str | None = None     # partial failure of the last successful run (res["warnings"])

    def as_dict(self) -> dict:
        return {"next_due": int(self.next_due), "running": self.running, "last_start": self.last_start,
                "last_end": self.last_end, "last_duration": self.last_duration, "fails": self.fails,
                "forced": self.forced, "fetched_last": self.fetched_last, "new_last": self.new_last,
                "last_error": self.last_error, "warning": self.warning}


# ---------------------------------------------------------------- scheduler

class Scheduler:
    def __init__(self, workers: int = WORKERS) -> None:
        self.workers = workers
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._aux: ThreadPoolExecutor | None = None      # clustering, opportunities, claude import, prune
        self._briefs: ThreadPoolExecutor | None = None   # brief generation (Ollama can take minutes)
        self._paper: ThreadPoolExecutor | None = None    # paper trading ticks
        self._score: ThreadPoolExecutor | None = None    # signal scorecard (network-bound, up to a minute)
        self._lab: ThreadPoolExecutor | None = None      # test lab tick (starts its own download thread)
        self._crypto: ThreadPoolExecutor | None = None
        self._analysis_lock = threading.Lock()
        self._jobs: dict[str, _JobState] = {}
        self._collectors: list | None = None
        self._collectors_tried = 0.0
        self.started_at = 0
        self.ticks = 0
        self._news_dirty = False
        self._last = {"cluster": 0, "opportunities": 0, "brief_check": 0, "claude_import": 0, "prune": 0,
                      "symbols": 0, "movers_why": 0, "reanalyze": 0, "paper": 0, "scorecard": 0, "lab": 0, "crypto": 0}
        self._busy = {"cluster": False, "opportunities": False, "briefs": False, "claude_import": False,
                      "prune": False, "movers_why": False, "reanalyze": False, "paper": False, "scorecard": False, "lab": False,
                      "crypto": False}
        self._results = {"cluster": None, "opportunities": None, "claude_import": None, "signals": None,
                         "movers_why": None, "paper": None, "scorecard": None, "lab": None, "crypto": None}
        self._signal_state: dict[str, tuple[float, str]] = {}   # last stored (score, direction) per ticker
        self._signals_full_ts = 0
        self._signal_lock = threading.Lock()   # _record_signals runs on the aux and the briefs threads
        self._brief_runs: dict[str, int] | None = None
        self._brief_log: list[dict] = []
        self._missing_logged: set[str] = set()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        db.init_db()
        self._stop.clear()
        self.started_at = now_ts()
        self._pool = ThreadPoolExecutor(self.workers, thread_name_prefix="collector")
        self._aux = ThreadPoolExecutor(1, thread_name_prefix="analysis")
        self._briefs = ThreadPoolExecutor(1, thread_name_prefix="briefs")
        self._paper = ThreadPoolExecutor(1, thread_name_prefix="paper")
        self._score = ThreadPoolExecutor(1, thread_name_prefix="scorecard")
        self._last["scorecard"] = self.started_at - SCORECARD_EVERY + 180
        self._lab = ThreadPoolExecutor(1, thread_name_prefix="lab")
        self._crypto = ThreadPoolExecutor(1, thread_name_prefix="crypto")    # crypto bots: 24/7 minute checks
        self._last["lab"] = self.started_at - LAB_EVERY + 90
        # First prune 5 min after start; first opportunities pass ~30 s after start.
        self._last["prune"] = self.started_at - PRUNE_EVERY + 300
        self._last["opportunities"] = self.started_at - OPPS_EVERY + 30
        self._last["claude_import"] = self.started_at - CLAUDE_IMPORT_EVERY + 15
        self._last["movers_why"] = self.started_at - MOVERS_WHY_EVERY + 40
        self._last["reanalyze"] = self.started_at - REANALYZE_EVERY + 120
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started (%d workers)", self.workers)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        for ex in (self._pool, self._aux, self._briefs, self._paper, self._score, self._lab, self._crypto):
            if ex:
                ex.shutdown(wait=False, cancel_futures=True)
        log.info("scheduler stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------ public API

    def collectors(self) -> list:
        """Registered collectors ([] if the registry cannot be imported; retried every minute)."""
        if self._collectors is None and time.time() - self._collectors_tried > 60:
            self._collectors_tried = time.time()
            try:
                from .collectors import all_collectors
                cols = list(all_collectors())
                if cols:
                    self._collectors = cols
                return cols
            except Exception:
                log.exception("collector registry failed to load")
                return []
        return self._collectors or []

    def refresh_now(self, source: str | None = None, rerun_running: bool | None = None) -> list[str]:
        """Queue an immediate run of one source (or all runnable sources). Returns the queued keys.
        A source that is running right now (it fetched with the old settings) runs once more right after it when
        `rerun_running` (default: only for a single-source request, so "refresh all" does not hit
        rate-limited sources twice)."""
        if rerun_running is None:
            rerun_running = bool(source)
        cfg = load_config()
        disabled = set(cfg.get("disabled_sources") or [])
        queued: list[str] = []
        with self._lock:
            for c in self.collectors():
                if source and c.key != source:
                    continue
                if c.key in disabled or c.missing_requirements(cfg):
                    continue
                st = self._job(c.key)
                if not st.running or rerun_running:
                    st.forced = True  # _schedule_collectors skips running jobs, so this reruns after the current run
                queued.append(c.key)
        if queued:
            self._news_dirty = True
            self._last["opportunities"] = min(self._last["opportunities"], now_ts() - OPPS_EVERY + 45)
            self._wake.set()
        return queued

    def status(self) -> dict:
        with self._lock:
            jobs = {k: st.as_dict() for k, st in self._jobs.items()}
        return {
            "running": self.running, "started_at": self.started_at, "ticks": self.ticks, "workers": self.workers,
            "active": sorted(k for k, j in jobs.items() if j["running"]),
            "jobs": jobs,
            "last": dict(self._last), "busy": dict(self._busy), "results": dict(self._results),
            "brief_runs": dict(self._brief_runs or {}), "brief_log": list(self._brief_log[-10:]),
        }

    def job(self, key: str) -> dict | None:
        with self._lock:
            st = self._jobs.get(key)
            return st.as_dict() if st else None

    # ------------------------------------------------------------ loop

    def _loop(self) -> None:
        try:
            self._startup()
        except Exception:
            log.exception("scheduler startup step failed")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            self.ticks += 1
            self._wake.wait(TICK)
            self._wake.clear()

    def _startup(self) -> None:
        threading.Thread(target=self._refresh_symbols, name="symbols", daemon=True).start()
        self._last["symbols"] = now_ts()
        cols = self.collectors()
        # Market data first (the dashboard needs it), then everything else, spread over STARTUP_SPREAD s.
        order = sorted(cols, key=lambda c: (0 if c.kind == "market" else 1 if c.kind == "calendar" else 2))
        step = STARTUP_SPREAD / max(1, len(order))
        base = time.time()
        with self._lock:
            for i, c in enumerate(order):
                self._jobs.setdefault(c.key, _JobState(base + i * step))
        log.info("%d collectors registered", len(cols))

    def _job(self, key: str) -> _JobState:
        st = self._jobs.get(key)
        if st is None:
            st = self._jobs[key] = _JobState(time.time())
        return st

    def _tick(self) -> None:
        cfg = load_config()
        now = now_ts()
        self._schedule_collectors(cfg)
        self._schedule_aux(cfg, now)

    # ------------------------------------------------------------ collectors

    def _effective_interval(self, c, fails: int) -> float:
        base = max(15, int(getattr(c, "interval", 300) or 300))
        if fails <= 0:
            return base
        return min(base * (2 ** min(fails - 1, 4)), max(base, BACKOFF_CAP))

    def _schedule_collectors(self, cfg: dict) -> None:
        if self._pool is None:
            return
        disabled = set(cfg.get("disabled_sources") or [])
        now = time.time()
        with self._lock:
            due = []
            running = 0
            for c in self.collectors():
                st = self._job(c.key)
                if st.running:
                    running += 1
                    if st.last_start and now - st.last_start > SLOW_RUN_WARN and int(now - st.last_start) % 60 < TICK:
                        log.warning("collector %s still running after %d s", c.key, int(now - st.last_start))
                    continue
                if c.key in disabled:
                    st.forced = False
                    continue
                if c.missing_requirements(cfg):
                    st.forced = False
                    continue
                if st.forced or now >= st.next_due:
                    due.append((0 if st.forced else st.next_due, c))
            due.sort(key=lambda x: x[0])
            capacity = self.workers * 2 - running
            for _, c in due[:max(0, capacity)]:
                st = self._jobs[c.key]
                st.running = True
                st.queued_at = now
                forced = st.forced
                st.forced = False
                try:
                    self._pool.submit(self._run_collector, c, forced)
                except RuntimeError:  # pool shut down
                    st.running = False
                    return

    def _run_collector(self, c, forced: bool = False) -> None:
        st = self._jobs[c.key]
        start = time.time()
        st.last_start = int(start)
        cfg = load_config()
        ok = False
        try:
            res = c.fetch(cfg)
            if res is None:
                res = {}
            if not isinstance(res, dict):
                raise TypeError(f"fetch() sözlük yerine {type(res).__name__} döndürdü")
            fetched, new, n_events, n_snap = self._ingest(c, res, cfg)
            st.fetched_last, st.new_last = fetched, new
            st.last_error = None
            # Partial failure (some feeds/days of the source failed): stays "ok", but the UI shows "Kısmi hata".
            warns = res.get("warnings")
            if isinstance(warns, str):
                warns = [warns]
            st.warning = "; ".join(str(w) for w in (warns if isinstance(warns, (list, tuple)) else []) if w)[:400] or None
            db.update_source_status(c.key, c.name, True, items=new + n_events + n_snap, warning=st.warning)
            ok = True
            if new:
                log.info("%s: %d new / %d fetched", c.key, new, fetched)
        except Exception as e:  # noqa: BLE001 - every failure is recorded, nothing propagates
            msg = friendly_error(e)
            st.last_error = msg
            if isinstance(e, _PY_ERRORS):
                log.exception("collector %s crashed: %s", c.key, msg)
            else:
                log.warning("collector %s failed: %s", c.key, msg)
            try:
                db.update_source_status(c.key, c.name, False, error=msg)
            except Exception:
                log.exception("could not record status for %s", c.key)
        finally:
            end = time.time()
            with self._lock:
                st.fails = 0 if ok else st.fails + 1
                st.last_end = int(end)
                st.last_duration = round(end - start, 2)
                st.next_due = start + self._effective_interval(c, st.fails)
                st.running = False

    def _ingest(self, c, res: dict, cfg: dict) -> tuple[int, int, int, int]:
        """Store one fetch result. Returns (news fetched, news new, events stored, snapshot entries)."""
        raw = res.get("news") or []
        news: list[dict] = []
        seen: set[str] = set()
        for it in raw if isinstance(raw, list) else []:
            try:
                item = _clean_news(it, c)
                if item is None:
                    continue
                nid = item.get("id") or db.news_id(item)
                if nid in seen:
                    continue
                seen.add(nid)
                item["id"] = nid
                news.append(item)
            except Exception:
                log.debug("%s: malformed news item skipped", c.key, exc_info=True)
        new_ids: list[str] = []
        if news:
            existing = _existing_ids([it["id"] for it in news])
            fresh = [it for it in news if it["id"] not in existing]
            if fresh:
                fresh = self._enrich(fresh, cfg)
                new_ids = self._upsert_news(fresh)
                if new_ids:
                    self._news_dirty = True

        n_events = 0
        events = [e for e in (res.get("events") or []) if _valid_event(e)]
        if events:
            try:
                n_events = db.upsert_events(events)
            except Exception:
                self._rollback()
                for e in events:
                    try:
                        n_events += db.upsert_events([e])
                    except Exception:
                        self._rollback()
                        log.debug("%s: bad event skipped", c.key, exc_info=True)
            self._apply_event_windows(c, res.get("event_windows"), events)

        n_snap = 0
        snaps = res.get("snapshots") or {}
        if isinstance(snaps, dict):
            for key, data in snaps.items():
                if not key or data is None:
                    continue
                try:
                    db.set_snapshot(str(key), data)
                    n_snap += _snapshot_size(data)
                except Exception:
                    self._rollback()
                    log.exception("%s: snapshot %s could not be stored", c.key, key)
        return len(news), len(new_ids), n_events, n_snap

    def _apply_event_windows(self, c, windows, events: list[dict]) -> None:
        """Remove this source's calendar rows that a successful re-fetch no longer contains (a moved earnings
        date, a withdrawn IPO). Collector contract: res["event_windows"] = [{"start": ts, "end": ts,
        "kind"?: str, "status"?: str, "keep_ids"?: [...]}], one per day/range it fetched successfully *with*
        rows. Rows of this source with start <= ts < end (and the given kind / extra.status) whose id is not
        kept are deleted; keep_ids defaults to the ids of all returned events. A window that contains none of
        the returned events is ignored (never delete on an empty or failed response)."""
        if not windows or not isinstance(windows, list):
            return
        all_ids = [db.event_id(e) for e in events]
        for w in windows:
            try:
                if not isinstance(w, dict) or (w.get("source") or c.key) != c.key:
                    continue
                start, end = int(w["start"]), int(w["end"])
                kind = w.get("kind") or None
                inside = [e for e in events if start <= int(e["ts"]) < end and (e.get("source") or c.key) == c.key
                          and (not kind or (e.get("kind") or "macro") == kind)]
                if end <= start or not inside:
                    continue
                keep = w.get("keep_ids")
                keep = [str(i) for i in keep] if isinstance(keep, (list, tuple, set)) else all_ids
                n = db.delete_events(c.key, start, end, keep, kind=kind, status=w.get("status") or None)
                if n:
                    log.info("%s: %d stale calendar row(s) removed", c.key, n)
            except Exception:
                self._rollback()
                log.warning("%s: event window %r could not be applied", c.key, w, exc_info=True)

    def _enrich(self, items: list[dict], cfg: dict) -> list[dict]:
        analysis = _lazy("analysis")
        if analysis is None or not hasattr(analysis, "enrich"):
            if "analysis" not in self._missing_logged:
                self._missing_logged.add("analysis")
                log.warning("analysis module unavailable; storing news un-enriched")
            return items
        try:
            out = analysis.enrich(items, cfg)
            return out if isinstance(out, list) and out else items
        except Exception:
            log.exception("analysis.enrich failed; storing %d items un-enriched", len(items))
            return items

    @staticmethod
    def _rollback() -> None:
        try:
            db.conn().rollback()
        except Exception:
            pass

    def _upsert_news(self, items: list[dict]) -> list[str]:
        try:
            return db.upsert_news(items)
        except Exception:
            log.exception("upsert_news batch failed; retrying item by item")
            self._rollback()
            out: list[str] = []
            for it in items:
                try:
                    out += db.upsert_news([it])
                except Exception:
                    self._rollback()
                    log.debug("bad news item skipped: %r", it.get("title"), exc_info=True)
            return out

    # ------------------------------------------------------------ analysis / briefs / maintenance

    def _submit_aux(self, name: str, fn, *args, pool: ThreadPoolExecutor | None = None) -> bool:
        """Run fn on the aux (or given) pool unless that step is already queued/running. True when submitted."""
        pool = pool or self._aux
        if pool is None or self._busy.get(name):
            return False
        self._busy[name] = True

        def run():
            try:
                fn(*args)
            except Exception:
                log.exception("%s step failed", name)
                self._rollback()  # never leave this long-lived thread's connection inside a write transaction
            finally:
                self._busy[name] = False
                self._last[name] = now_ts()

        try:
            pool.submit(run)
        except RuntimeError:
            self._busy[name] = False
            return False
        return True

    def _schedule_aux(self, cfg: dict, now: int) -> None:
        L = self._last
        if self._news_dirty and now - L["cluster"] >= CLUSTER_DEBOUNCE:
            # Cleared only when a pass was actually queued: news arriving while one is queued or running keeps
            # the flag, so it gets its own pass afterwards.
            if self._submit_aux("cluster", self._run_cluster):
                self._news_dirty = False
        if now - L["opportunities"] >= OPPS_EVERY:
            self._submit_aux("opportunities", self._run_opportunities, cfg)
        if now - L["movers_why"] >= MOVERS_WHY_EVERY:
            self._submit_aux("movers_why", self._run_movers_why)
        if now - L["reanalyze"] >= REANALYZE_EVERY:
            self._submit_aux("reanalyze", self._run_reanalyze, cfg)
        if now - L["claude_import"] >= CLAUDE_IMPORT_EVERY:
            self._submit_aux("claude_import", self._run_claude_import)
        if now - L["brief_check"] >= BRIEF_CHECK_EVERY and self._warmed_up(cfg, now):
            self._submit_aux("briefs", self._run_briefs, cfg, now, pool=self._briefs)
            L["brief_check"] = now
        if now - L["paper"] >= PAPER_EVERY:
            self._submit_aux("paper", self._run_paper, cfg, pool=self._paper)
        if now - L["scorecard"] >= SCORECARD_EVERY:
            self._submit_aux("scorecard", self._run_scorecard, pool=self._score)
        if now - L["crypto"] >= CRYPTO_EVERY and self._crypto is not None:
            self._submit_aux("crypto", self._run_crypto, cfg, pool=self._crypto)
        if now - L["lab"] >= LAB_EVERY:
            self._submit_aux("lab", self._run_lab, cfg, pool=self._lab)
        if now - L["prune"] >= PRUNE_EVERY:
            self._submit_aux("prune", self._run_prune)
        if now - L["symbols"] >= SYMBOLS_EVERY:
            L["symbols"] = now
            threading.Thread(target=self._refresh_symbols, name="symbols", daemon=True).start()

    def _warmed_up(self, cfg: dict, now: int) -> bool:
        """Briefs wait until the first collection round is done (an app started after 16:30 ET must not
        write the evening brief from an empty DB)."""
        age = now - self.started_at
        if age >= WARMUP_MAX:
            return True
        if age < WARMUP_MIN:
            return False
        disabled = set(cfg.get("disabled_sources") or [])
        with self._lock:
            for c in self.collectors():
                st = self._jobs.get(c.key)
                if st and not st.last_end and c.key not in disabled and not c.missing_requirements(cfg):
                    return False
        return True

    def _refresh_symbols(self) -> None:
        tickers = _lazy("analysis.tickers")
        if tickers is None or not hasattr(tickers, "refresh_symbols"):
            return
        try:
            n = tickers.refresh_symbols(load_config())
            log.info("symbol universe: %s symbols", n)
        except Exception as e:  # noqa: BLE001
            log.warning("symbol refresh failed: %s", e)

    def _run_cluster(self, full: bool = False) -> None:
        cluster = _lazy("analysis.cluster")
        if cluster is None or not hasattr(cluster, "assign_clusters"):
            return
        with self._analysis_lock:
            t = time.time()
            # full: the whole retained news window (news rows are pruned after 21 days), not only the last 48 h
            n = cluster.assign_clusters(since_hours=RETAIN_DAYS * 24) if full else cluster.assign_clusters()
            self._results["cluster"] = {"ts": now_ts(), "result": n, "seconds": round(time.time() - t, 2)}

    def _run_opportunities(self, cfg: dict) -> None:
        opps = _lazy("analysis.opportunities")
        if opps is None or not hasattr(opps, "compute"):
            return
        with self._analysis_lock:
            t = time.time()
            data = opps.compute(cfg)
            if isinstance(data, dict):
                db.set_snapshot("opportunities", data)
                self._results["opportunities"] = {"ts": now_ts(), "items": len(data.get("items") or []),
                                                  "seconds": round(time.time() - t, 2)}
        if isinstance(data, dict):
            try:
                self._record_signals(data)
            except Exception:
                self._rollback()
                log.exception("signal history could not be stored")

    def _record_signals(self, data: dict) -> None:
        with self._signal_lock:
            self._record_signals_locked(data)

    def _record_signals_locked(self, data: dict) -> None:
        """Opportunity history for the day-trade bot (`signals` table): every listed ticker each SIGNALS_EVERY s
        (SIGNALS_EVERY_CLOSED while the market is closed) and, in between, only tickers that are new, moved
        >= SIGNAL_SCORE_STEP points or flipped direction."""
        items = [it for it in data.get("items") or [] if isinstance(it, dict) and it.get("ticker")]
        if not items:
            return
        ts = int(data.get("ts") or now_ts())
        session = market_session(ts)
        every = SIGNALS_EVERY if session != "closed" else SIGNALS_EVERY_CLOSED
        full = ts - self._signals_full_ts >= every
        rows = []
        for it in items:
            sym = str(it["ticker"]).upper()
            try:
                score = float(it.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            direction = str(it.get("direction") or "mixed")
            prev = self._signal_state.get(sym)
            if full or prev is None or abs(score - prev[0]) >= SIGNAL_SCORE_STEP or direction != prev[1]:
                rows.append(it)
                self._signal_state[sym] = (score, direction)
        if full:
            live = {str(it["ticker"]).upper() for it in items}
            for sym in [k for k in self._signal_state if k not in live]:
                del self._signal_state[sym]  # re-entering the list later counts as new
        if rows:
            n = db.append_signals(rows, ts, session, rel_volumes=_rel_volumes())
            self._results["signals"] = {"ts": ts, "rows": n, "full": full}
        if full:
            self._signals_full_ts = ts

    def _run_movers_why(self) -> None:
        """Snapshot `movers_why` {"ts", "since", "items": {SYM: catalyst}} for the symbols in the movers tables, so
        /api/market can show a "Neden?" headline next to each mover without scanning news on every request.
        Matching is the briefs' own: briefs._group_stories + briefs._why (company name / tag check, and, in the
        direction-aware version, a headline that explains a move of that size and sign)."""
        briefs = _lazy("briefs")
        group = (getattr(briefs, "group_stories", None) or getattr(briefs, "_group_stories", None)) if briefs else None
        why = (getattr(briefs, "why_for", None) or getattr(briefs, "_why", None)) if briefs else None
        if group is None or why is None:
            return
        try:
            why_takes_chg = "chg" in inspect.signature(why).parameters
        except (TypeError, ValueError):
            why_takes_chg = False
        movers: dict[str, tuple[str, float | None]] = {}   # symbol -> (name, largest move of its rows)
        lists_ts = 0
        for key in ("movers", "movers_alpaca"):
            snap = db.get_snapshot(key)
            data = snap[0] if snap else None
            if not isinstance(data, dict):
                continue
            lists_ts = max(lists_ts, int(data.get("ts") or snap[1] or 0))
            for lst in MOVER_LISTS:
                for r in data.get(lst) or []:
                    if not isinstance(r, dict) or not r.get("symbol"):
                        continue
                    sym = str(r["symbol"]).upper()
                    name, best = movers.get(sym, ("", None))
                    for k in ("change_pct", "pre_change_pct", "post_change_pct"):
                        v = _float(r.get(k))
                        if v is not None and (best is None or abs(v) > abs(best)):
                            best = v
                    movers[sym] = (name or str(r.get("name") or ""), best)
        if not movers:
            return
        t = time.time()
        now = now_ts()
        since = min(now - MOVERS_WHY_HOURS * 3600, _movers_news_start(lists_ts or now))
        news = [n for n in db.recent_news(since, limit=20000)
                if (n.get("published_at") or 0) <= now + 300 and movers.keys() & set(n.get("tickers") or [])]
        with self._analysis_lock:  # not while clustering rewrites cluster_id
            stories = group(news)
        ticker_stories: dict[str, list[dict]] = {}
        for st in stories:
            for tk in st.get("tickers") or []:
                if tk in movers:
                    ticker_stories.setdefault(tk, []).append(st)
        for tk, lst in ticker_stories.items():  # same order as the briefs: media first, stories about tk first
            lst.sort(key=lambda st, tk=tk: (bool(st.get("social_only")), (st.get("ticker_share") or {}).get(tk, 0) < 0.5,
                                           -float(st.get("rank") or 0)))
        pack = {"ticker_stories": ticker_stories, "why_stories": ticker_stories}
        out: dict[str, dict] = {}
        for sym, (name, chg) in movers.items():
            if sym not in ticker_stories:
                continue
            st = why(pack, sym, name or None, chg) if why_takes_chg else why(pack, sym, name or None)
            if not st:
                continue
            story, lead = _match_story(ticker_stories.get(sym) or [], st)
            sources = st.get("sources") or []
            out[sym] = {"title": st.get("title") or "", "url": st.get("url") or "",
                        "source_name": sources[0] if sources else (lead.get("source_name") or ""),
                        "publisher": str((lead.get("extra") or {}).get("publisher") or ""),
                        "published_at": int(lead.get("published_at") or story.get("last_seen") or 0) or None,
                        "categories": [c for c in story.get("categories") or [] if c != "opinion"][:2],
                        "sentiment_label": lead.get("sentiment_label") or "neu",
                        "source_count": int(story.get("source_count") or len(sources) or 1),
                        "news_id": lead.get("id")}
        db.set_snapshot("movers_why", {"ts": now, "since": since, "items": out})
        self._results["movers_why"] = {"ts": now, "symbols": len(movers), "news": len(news), "found": len(out),
                                       "seconds": round(time.time() - t, 2)}

    def _run_claude_import(self) -> None:
        briefs = _lazy("briefs")
        if briefs is None or not hasattr(briefs, "import_claude_outputs"):
            return
        n = briefs.import_claude_outputs()
        self._results["claude_import"] = {"ts": now_ts(), "imported": n}
        if n:
            log.info("imported %d Claude brief(s)", n)

    def _run_paper(self, cfg: dict) -> None:
        paper = _lazy("paper")
        if paper is None or not hasattr(paper, "tick"):
            return
        try:
            self._results["paper"] = paper.tick(cfg)
        finally:   # the $100 small-cap account steps with the main engine even when the main tick failed
            fleet = _lazy("fleet")        # the bot tree's 100 accounts: exits every minute, equity snapshots
            if fleet is not None and hasattr(fleet, "tick"):
                try:
                    self._results["fleet"] = fleet.tick(cfg)
                except Exception:  # noqa: BLE001
                    log.exception("fleet tick failed")
            micro = _lazy("micro_paper")
            fl_cfg = cfg.get("fleet") if isinstance(cfg.get("fleet"), dict) else {}
            # retired: the $100 small-cap bot only steps while it still holds positions (exits), it opens nothing new
            micro_on = not fl_cfg.get("retire_old", True) or (micro is not None and hasattr(micro, "_positions") and bool(micro._positions()))
            if micro is not None and hasattr(micro, "check_and_trade_micro_cands") and micro_on:
                try:
                    self._results["micro"] = micro.check_and_trade_micro_cands()
                except Exception:  # noqa: BLE001
                    log.exception("micro paper tick failed")
            real = _lazy("real_sim")   # the realistic $100 account (cash-account rules, mixed bots)
            if real is not None and hasattr(real, "tick"):
                try:
                    self._results["real_sim"] = real.tick(cfg)
                except Exception:  # noqa: BLE001
                    log.exception("real_sim tick failed")

    def _run_scorecard(self) -> None:
        scorecard = _lazy("scorecard")
        if scorecard is None or not hasattr(scorecard, "evaluate"):
            return
        self._results["scorecard"] = scorecard.evaluate()

    def _run_crypto(self, cfg: dict) -> None:
        cfleet = _lazy("crypto.cfleet")
        if cfleet is None or not hasattr(cfleet, "cycle"):
            return
        self._results["crypto"] = cfleet.cycle(cfg)

    def _run_lab(self, cfg: dict) -> None:
        runner = _lazy("lab.runner")
        if runner is None or not hasattr(runner, "tick"):
            return
        self._results["lab"] = runner.tick(cfg)

    def _run_prune(self) -> None:
        db.prune()
        log.info("database pruned")

    def _run_reanalyze(self, cfg: dict) -> None:
        """Re-derive categories / tickers / sentiment / importance for stored news whose `extra.analysis_version`
        is not the current analysis.ANALYSIS_VERSION, so rule changes reach existing rows (the opportunity
        snapshot follows on its next pass). Does nothing while analysis does not define ANALYSIS_VERSION."""
        analysis = _lazy("analysis")
        version = getattr(analysis, "ANALYSIS_VERSION", None) if analysis is not None else None
        if version is None or not hasattr(analysis, "enrich"):
            return
        stamp = str(version)
        done = 0
        for _ in range(REANALYZE_MAX_BATCHES):
            rows = db.stale_news(stamp, REANALYZE_BATCH)
            if not rows:
                break
            try:
                out = analysis.enrich(rows, cfg)
            except Exception:  # noqa: BLE001 - keep the stored values, try again next pass
                log.exception("reanalyze: analysis.enrich failed")
                break
            by_id = {str(it.get("id")): it for it in out if isinstance(it, dict)} if isinstance(out, list) else {}
            updates = []
            for r in rows:
                it = by_id.get(str(r["id"])) or {}
                fields = {k: it[k] for k in ("tickers", "categories", "sentiment", "sentiment_label", "importance")
                          if k in it}
                extra = dict(r.get("extra") or {})
                if isinstance(it.get("extra"), dict):
                    extra.update(it["extra"])
                extra["analysis_version"] = stamp   # stamped even when enrich returned nothing: no endless retries
                fields["extra"] = extra
                updates.append((r["id"], fields))
            db.update_news_fields(updates)
            done += len(updates)
            if len(rows) < REANALYZE_BATCH:
                break
        if done:
            log.info("reanalyze: %d news row(s) re-derived (analysis version %s)", done, stamp)
            # After a version bump the clusters of older rows may be stale: re-cluster the full retained window
            # once, when no stale rows remain (a partial pass waits for the next one).
            if not db.stale_news(stamp, 1):
                self._run_cluster(full=True)

    def _load_brief_runs(self, briefs) -> dict[str, int]:
        if self._brief_runs is None:
            runs = db.get_snapshot_data("brief_runs")
            if not isinstance(runs, dict):
                runs = {}
                if hasattr(briefs, "last_runs_from_db"):
                    try:
                        runs = dict(briefs.last_runs_from_db())
                    except Exception:
                        log.exception("last_runs_from_db failed")
            self._brief_runs = {str(k): int(v or 0) for k, v in runs.items()}
        return self._brief_runs

    def _run_briefs(self, cfg: dict, now: int) -> None:
        briefs = _lazy("briefs")
        if briefs is None or not hasattr(briefs, "due_briefs"):
            return
        runs = self._load_brief_runs(briefs)
        due = briefs.due_briefs(cfg, now, dict(runs))
        if not due:
            return
        # Fresh analysis before writing (no-op when those modules are missing).
        for step in (self._run_cluster, lambda: self._run_opportunities(cfg)):
            try:
                step()
            except Exception:
                log.exception("pre-brief analysis failed")
        for kind in due:
            runs[kind] = now  # mark first: a failing brief must not retry every minute
            db.set_snapshot("brief_runs", runs)
            entry = {"kind": kind, "ts": now, "kural": False, "ollama": False, "claude_export": None}
            try:
                briefs.generate(kind, cfg)
                entry["kural"] = True
            except Exception:
                log.exception("rule-based brief %s failed", kind)
            if hasattr(briefs, "generate_ollama"):
                try:
                    entry["ollama"] = bool(briefs.generate_ollama(kind, cfg))
                except Exception:
                    log.exception("ollama brief %s failed", kind)
            if hasattr(briefs, "export_for_claude"):
                try:
                    entry["claude_export"] = str(briefs.export_for_claude(kind, cfg))
                except Exception:
                    log.exception("claude export %s failed", kind)
            self._brief_log.append(entry)
            del self._brief_log[:-20]
            log.info("brief %s done: %s", kind, entry)


__all__ = ["Scheduler", "friendly_error", "requirement_hint"]
