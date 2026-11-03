"""Background jobs of the crypto desk: the history download and the tree build (walk -> 40 accounts).

The tree build waits while the stock tree is being built (both at once would strain the memory), then starts by
itself.
"""
from __future__ import annotations

import logging
import threading
import time

from ..util import friendly_error, now_ts
from . import cstore, ctree, data

log = logging.getLogger("marketdesk.crypto")

_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def running(name: str) -> bool:
    t = _threads.get(name)
    return bool(t and t.is_alive())


def _start(name: str, fn, *args) -> bool:
    with _lock:
        if running(name):
            return False
        t = threading.Thread(target=fn, args=args, name=f"crypto-{name}", daemon=True)
        _threads[name] = t
        t.start()
        return True


def _stock_tree_busy() -> bool:
    try:
        from ..lab import runner
        return runner._running("tree")
    except Exception:  # noqa: BLE001
        return False


def start_download() -> dict:
    if running("tree"):
        return {"ok": False, "message": "Kripto ağacı kurulurken veri indirilemez"}
    _start("download", data.download_all)
    return {"ok": True}


def _run_tree() -> None:
    from . import cfleet
    T = ctree.CTREE
    T.update(state="running", message="Başlıyor…", started_at=now_ts(), finished_at=None, done=0, total=0)
    try:
        while _stock_tree_busy():
            T.update(message="Hisse ağacının bitmesi bekleniyor (ikisi birden belleği zorlar); bitince kendiliğinden başlayacak")
            time.sleep(60)
        while running("download"):
            T.update(message="Kripto veri indirmesinin bitmesi bekleniyor…")
            time.sleep(15)
        if not cstore.counts().get("bars"):
            data.download_all()
        T.update(message="Son günlerin mumları alınıyor…")
        try:
            data.update_recent()
        except Exception as e:  # noqa: BLE001 - the history is enough to train
            log.warning("crypto update before tree: %s", e)
        rep = ctree.run(progress=lambda m, i, n: T.update(message=m, done=i, total=n))
        n = cfleet.seed_from_tree(rep)
        T.update(state="done", finished_at=now_ts(),
                 message=f"Kripto ağacı hazır: {n} bot kendi hesabıyla işleme başlıyor ({rep['seconds'] / 60:.0f} dk)")
    except Exception as e:  # noqa: BLE001
        log.exception("crypto tree failed")
        T.update(state="error", message=f"Hata: {friendly_error(e)}", finished_at=now_ts())


def start_tree() -> dict:
    if not _start("tree", _run_tree):
        return {"ok": False, "message": "Kripto ağacı zaten kuruluyor"}
    return {"ok": True}


def status() -> dict:
    return {"tree": dict(ctree.CTREE), "tree_running": running("tree"), "download": dict(data.STATUS),
            "download_running": running("download")}
