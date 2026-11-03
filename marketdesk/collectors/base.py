"""Collector contract. Every source module exposes COLLECTORS: list[Collector]."""
from __future__ import annotations


class Collector:
    key: str = ""             # unique, stable, snake_case (used as news.source)
    name: str = ""            # display name shown in UI ("CNBC", "SEC 8-K", ...)
    group: str = ""           # UI group: see GROUPS
    kind: str = "news"        # "news" | "calendar" | "market" | "social"
    source_type: str = "news"  # news.source_type: "news" | "press" | "filing" | "official" | "social" | "api"
    interval: int = 300       # seconds between runs
    requires: tuple[str, ...] = ()   # config keys that must be non-empty
    description: str = ""     # short Turkish description for the Sources page

    def missing_requirements(self, cfg: dict) -> list[str]:
        return [r for r in self.requires if not str(cfg.get(r) or "").strip()]

    def fetch(self, cfg: dict) -> dict:
        """Return {"news": [...], "events": [...], "snapshots": {key: data}} (any subset).
        Raise on failure; the scheduler records the error in source_status."""
        raise NotImplementedError

    def item(self, title: str, url: str, published_at: int | None, summary: str = "",
             tickers: list[str] | None = None, author: str = "", extra: dict | None = None) -> dict:
        return {
            "source": self.key,
            "source_name": self.name,
            "source_type": self.source_type,
            "title": (title or "").strip(),
            "url": url or "",
            "published_at": published_at,
            "summary": summary or "",
            "tickers": [t.upper() for t in (tickers or [])],
            "author": author or "",
            "extra": extra or {},
        }

    def info(self, cfg: dict) -> dict:
        return {
            "source": self.key, "source_name": self.name, "group": self.group, "kind": self.kind,
            "source_type": self.source_type, "interval": self.interval, "description": self.description,
            "requires": list(self.requires), "requires_missing": self.missing_requirements(cfg),
            "enabled": self.key not in (cfg.get("disabled_sources") or []),
        }


GROUPS = {
    "media": "Ana akım medya",
    "press": "Basın bültenleri (şirket açıklamaları)",
    "official": "Resmi kurumlar (SEC, Fed)",
    "aggregator": "Toplayıcılar (Google News vb.)",
    "broker": "Veri API'leri (Alpaca, Finnhub)",
    "social": "Sosyal medya (Reddit, StockTwits)",
    "data": "Piyasa verisi ve takvim",
}
