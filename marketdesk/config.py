"""User settings stored as JSON in DATA_DIR (outside OneDrive so API keys never sync)."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time

from .util import DATA_DIR

log = logging.getLogger("marketdesk.config")

CONFIG_PATH = DATA_DIR / "config.json"

SECRET_KEYS = ("alpaca_key_id", "alpaca_secret", "finnhub_key")

DEFAULTS: dict = {
    # API keys (optional; sources that need them stay idle until filled)
    "alpaca_key_id": "",
    "alpaca_secret": "",
    "finnhub_key": "",
    "alpaca_feed": "iex",        # "iex" (free) | "sip" (paid plan) | "delayed_sip"
    # SEC EDGAR requires "Name email@domain" in the User-Agent; SEC sources stay idle until set.
    "sec_user_agent": "",
    # Optional local AI
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "",          # e.g. "qwen2.5:7b"; empty = auto-pick first installed model
    "use_finbert": False,        # needs `pip install transformers torch`
    # Watchlist drives per-ticker news, alerts and opportunity boosts
    "watchlist": ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "AMD", "PLTR"],
    "disabled_sources": [],
    # Brief times in US Eastern time (TR time shifts automatically with DST)
    "brief_times_et": {"sabah": "08:30", "ogle": "12:30", "aksam": "16:30"},
    "alert_min_importance": 70,
    "port": 8765,
    # Paper trading risk/bot settings; missing keys fall back to paper.SETTINGS_DEFAULTS
    "paper": {},
    # $100 small-cap paper bot (micro_paper.py); written by the live learning, missing keys use micro_paper.DEFAULTS
    "micro": {},
    # Realistic $100 account (real_sim.py): mixed bots under real cash-account rules; missing keys use real_sim.DEFAULTS
    "real_sim": {},
    # Bot tree fleet (fleet.py): 100 bots with their own paper accounts; retire_old switches the old live learning off
    "fleet": {},
}

_lock = threading.RLock()
_cache: dict | None = None
# False when the last _read_file() could not get a trustworthy view of config.json (file present but
# unreadable, or corrupt and not moved aside). Defaults must then never be cached or written back.
_read_ok = True


def _read_file() -> dict:
    """Parse config.json. A corrupt file is moved aside (config.json.bad-<ts>) instead of being silently
    overwritten with defaults by the next save."""
    global _read_ok
    _read_ok = True
    if not CONFIG_PATH.exists():
        return {}
    text = None
    for attempt in range(3):
        try:
            text = CONFIG_PATH.read_text(encoding="utf-8-sig")
            break
        except OSError as e:  # e.g. antivirus holding the file for a moment
            if attempt == 2:
                _read_ok = False
                log.warning("config.json okunamadı (%s); varsayılan ayarlar kullanılıyor", e)
                return {}
            time.sleep(0.2)
    try:
        data = json.loads(text or "")
        if not isinstance(data, dict):
            raise ValueError("kök bir JSON nesnesi değil")
        return data
    except ValueError as e:
        bad = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bad-{int(time.time())}")
        try:
            os.replace(CONFIG_PATH, bad)
            log.warning("config.json bozuk (%s); %s olarak saklandı, varsayılan ayarlar kullanılıyor", e, bad.name)
        except OSError as e2:
            _read_ok = False
            log.warning("config.json bozuk (%s) ve kenara alınamadı (%s); varsayılan ayarlar kullanılıyor", e, e2)
        return {}


def _merged_from_disk() -> tuple[dict, bool]:
    """Defaults overlaid with config.json; the bool is False when the file could not be read."""
    data = _read_file()
    merged = copy.deepcopy(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged, _read_ok


def _write_file(data: dict) -> None:
    """Atomic write: temp file + fsync + os.replace (a crash never leaves an empty config.json)."""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(3):
        try:
            os.replace(tmp, CONFIG_PATH)
            return
        except PermissionError:  # Windows: another process (antivirus, indexer) has the file open
            time.sleep(0.1 * (attempt + 1))
    log.warning("config.json atomik olarak değiştirilemedi; doğrudan yazılıyor")
    CONFIG_PATH.write_text(text, encoding="utf-8")
    try:
        tmp.unlink()
    except OSError:
        pass


def load_config() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            merged, ok = _merged_from_disk()
            if not ok:
                return merged  # not cached: the next call retries the read
            _cache = merged
        return copy.deepcopy(_cache)


def save_config(updates: dict, fn=None) -> dict:
    """Merge known keys into the config and persist. Empty strings for secrets are ignored
    unless the caller passes the sentinel "__clear__". `fn(cfg)` may change the merged dict in place;
    the whole read-modify-write runs under one lock so concurrent saves never lose each other's changes."""
    global _cache
    with _lock:
        current = load_config()
        if not _read_ok:
            # The file exists but could not be read: retry once, and never write defaults over it.
            current, ok = _merged_from_disk()
            if not ok:
                raise RuntimeError("config.json okunamadığı için ayarlar kaydedilmedi; dosya kilitli olabilir, birkaç saniye sonra tekrar deneyin")
        for k, v in (updates or {}).items():
            if k not in DEFAULTS:
                continue
            if k in SECRET_KEYS:
                if v == "__clear__":
                    v = ""
                elif not v:
                    continue
            if k == "watchlist" and isinstance(v, str):
                v = [s.strip().upper().lstrip("$") for s in v.replace(";", ",").split(",") if s.strip()]
            if k == "watchlist" and isinstance(v, list):
                v = list(dict.fromkeys(str(s).strip().upper().lstrip("$") for s in v if str(s).strip()))
            current[k] = v
        if fn is not None:
            fn(current)
        _write_file(current)
        _cache = copy.deepcopy(current)
        return copy.deepcopy(current)


def set_source_enabled(key: str, enabled: bool) -> dict:
    """Enable/disable one source atomically (no lost updates when several toggles overlap)."""
    def apply(cfg: dict) -> None:
        disabled = [k for k in (cfg.get("disabled_sources") or []) if k != key]
        if not enabled:
            disabled.append(key)
        cfg["disabled_sources"] = disabled
    return save_config({}, fn=apply)


def public_config(cfg: dict | None = None) -> dict:
    """Config safe to send to the UI: secrets replaced by <key>_set booleans."""
    cfg = cfg or load_config()
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        out[f"{k}_set"] = bool(cfg.get(k))
    return out
