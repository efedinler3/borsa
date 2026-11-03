"""Optional local ML: FinBERT sentiment (transformers) and Ollama text generation. Never raises if missing."""
from __future__ import annotations

import importlib.util
import logging
import threading
import time

import requests

log = logging.getLogger("marketdesk.analysis")

FINBERT_MODEL = "ProsusAI/finbert"

_fb_lock = threading.Lock()
_fb_pipe = None
_fb_failed = False
_fb_avail: bool | None = None

_ol_cache: dict[str, tuple[float, list[str] | None]] = {}
_OL_TTL = 60


# ---------------------------------------------------------------- FinBERT

def finbert_available() -> bool:
    """True when transformers + torch are importable (the model itself loads lazily on first use)."""
    global _fb_avail
    if _fb_avail is None:
        try:
            _fb_avail = bool(importlib.util.find_spec("transformers") and importlib.util.find_spec("torch"))
        except (ImportError, ValueError):
            _fb_avail = False
    return _fb_avail and not _fb_failed


def _pipeline():
    global _fb_pipe, _fb_failed
    if _fb_pipe is not None or _fb_failed:
        return _fb_pipe
    with _fb_lock:
        if _fb_pipe is None and not _fb_failed:
            try:
                from transformers import pipeline  # type: ignore[import-not-found]
                _fb_pipe = pipeline("text-classification", model=FINBERT_MODEL, top_k=None, truncation=True)
            except Exception as e:  # noqa: BLE001 - missing weights, no network, torch errors...
                log.warning("FinBERT unavailable: %s", e)
                _fb_failed = True
    return _fb_pipe


def finbert_scores(texts: list[str], batch_size: int = 32) -> list[float] | None:
    """p(positive) - p(negative) per text in -1..1, or None when FinBERT cannot run."""
    if not texts or not finbert_available():
        return None
    pipe = _pipeline()
    if pipe is None:
        return None
    try:
        with _fb_lock:
            raw = pipe([t[:512] or "." for t in texts], batch_size=batch_size)
    except Exception as e:  # noqa: BLE001
        log.warning("FinBERT inference failed: %s", e)
        return None
    out = []
    for res in raw:
        if isinstance(res, dict):
            res = [res]
        probs = {str(r.get("label", "")).lower(): float(r.get("score", 0.0)) for r in res}
        out.append(round(probs.get("positive", 0.0) - probs.get("negative", 0.0), 3))
    return out


# ---------------------------------------------------------------- Ollama

def _base(cfg: dict) -> str:
    return str((cfg or {}).get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")


def ollama_models(cfg: dict) -> list[str]:
    """Installed model names (cached for 60 s); [] when Ollama is not reachable."""
    base = _base(cfg)
    hit = _ol_cache.get(base)
    if hit and time.time() - hit[0] < _OL_TTL:
        return list(hit[1] or [])
    models: list[str] | None
    try:
        r = requests.get(base + "/api/tags", timeout=2)
        r.raise_for_status()
        models = [m.get("name") or m.get("model") for m in (r.json().get("models") or [])]
        models = [m for m in models if m]
    except (requests.RequestException, ValueError, AttributeError):
        models = None
    _ol_cache[base] = (time.time(), models)
    return list(models or [])


def ollama_available(cfg: dict) -> bool:
    ollama_models(cfg)
    hit = _ol_cache.get(_base(cfg))
    return bool(hit and hit[1])


def _pick_model(cfg: dict) -> str | None:
    want = str((cfg or {}).get("ollama_model") or "").strip()
    models = ollama_models(cfg)
    if want:
        return want
    return models[0] if models else None


def ollama_generate(prompt: str, cfg: dict, system: str | None = None, timeout: int = 120) -> str | None:
    """Single non-streaming completion; None on any failure."""
    model = _pick_model(cfg)
    if not model or not prompt:
        return None
    body = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.3, "num_ctx": 8192}}
    if system:
        body["system"] = system
    try:
        r = requests.post(_base(cfg) + "/api/generate", json=body, timeout=timeout)
        r.raise_for_status()
        text = (r.json().get("response") or "").strip()
        return text or None
    except (requests.RequestException, ValueError, AttributeError) as e:
        log.warning("ollama generate failed: %s", e)
        return None


__all__ = ["finbert_available", "finbert_scores", "ollama_available", "ollama_models", "ollama_generate"]
