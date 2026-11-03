"""Collector registry: gathers COLLECTORS from every source module."""
from __future__ import annotations

import importlib
import logging

from .base import GROUPS, Collector

log = logging.getLogger("marketdesk.collectors")

MODULES = ("rss", "sec", "calendar", "alpaca", "finnhub", "market", "social")

_cache: list[Collector] | None = None


def all_collectors() -> list[Collector]:
    global _cache
    if _cache is None:
        out: list[Collector] = []
        seen: set[str] = set()
        for m in MODULES:
            try:
                mod = importlib.import_module(f".{m}", __name__)
            except Exception:
                log.exception("collector module %s failed to import", m)
                continue
            for c in getattr(mod, "COLLECTORS", []):
                if c.key in seen:
                    log.warning("duplicate collector key %s in %s", c.key, m)
                    continue
                seen.add(c.key)
                out.append(c)
        _cache = out
    return _cache


def get_collector(key: str) -> Collector | None:
    return next((c for c in all_collectors() if c.key == key), None)


__all__ = ["Collector", "GROUPS", "all_collectors", "get_collector"]
