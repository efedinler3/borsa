"""Crypto history and daily updates, from free public sources only (no account, no API key):

  Binance spot REST (api.binance.com)        daily candles of the USDT pairs trading now
  Binance public archive (data.binance.vision) monthly candle files, also of DELISTED pairs (so the training does not
                                              only see the coins that survived)
  alternative.me                              Crypto Fear & Greed index, daily since February 2018

Stablecoins (USDC, FDUSD, ...), fiat pairs (EUR, TRY, ...), gold tokens and Binance's leveraged tokens (BTCUP,
ETHBULL, ...) are left out: they are not coins a bot should buy.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..util import friendly_error, http_get, http_json, now_ts
from . import cstore

log = logging.getLogger("marketdesk.crypto")

REST = "https://api.binance.com/api/v3"
ARCHIVE = "https://data.binance.vision/"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FNG = "https://api.alternative.me/fng/"
START_MS = 1500000000000          # 2017-07-14: before Binance's first candles
NOT_COINS = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDS", "USDSB", "SUSD", "UST", "USTC", "AEUR", "EURI",
    "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "BIDR", "IDRT", "BVND", "BKRW", "PAXG", "XAUT",
    "XUSD", "USD1", "BFUSD", "RLUSD", "USDE", "USDQ", "USDD", "PYUSD", "EURC", "GUSD", "LUSD", "FRAX", "TRIBE",
    "BULL", "BEAR",                # the old 3x Bitcoin tokens
}
LEVER = ("UP", "DOWN", "BULL", "BEAR")
WORKERS = 6

STATUS: dict = {"state": "idle", "message": "", "done": 0, "total": 0, "started_at": None, "finished_at": None}
_lock = threading.Lock()


def _day(ms: int) -> str:
    if ms > 10 ** 14:                  # the archive switched to microseconds in 2025
        ms //= 1000
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def coin_pairs(symbols: set[str]) -> dict[str, str]:
    """USDT pairs of real coins: {symbol: base}."""
    bases = {s[:-4] for s in symbols if s.endswith("USDT") and len(s) > 4}
    out = {}
    for s in symbols:
        if not s.endswith("USDT") or len(s) <= 4:
            continue
        b = s[:-4]
        if b in NOT_COINS or not re.fullmatch(r"[A-Z0-9]+", b):
            continue
        if any(b.endswith(x) and b[:-len(x)] in bases for x in LEVER):    # BTCUP, ETHBULL ... (JUP stays)
            continue
        out[s] = b
    return out


def _rows_from_klines(sym: str, kl: list, now_ms: int) -> list[tuple]:
    rows = []
    for k in kl:
        try:
            open_ms, close_ms = int(k[0]), int(k[6])
        except (TypeError, ValueError):
            continue                   # a header line
        if close_ms > 10 ** 14:
            close_ms //= 1000
        if close_ms > now_ms:          # today's candle is not finished
            continue
        rows.append((sym, _day(open_ms), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]),
                     float(k[7]), float(k[9]), int(float(k[8]))))
    return rows


def _store_bars(rows: list[tuple]) -> None:
    if not rows:
        return
    with cstore.tx() as c:
        c.executemany("INSERT OR REPLACE INTO bars(symbol,d,o,h,l,c,v,qv,tb,n) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _store_symbol(sym: str, base: str, status: str) -> None:
    c = cstore.conn()
    r = c.execute("SELECT MIN(d), MAX(d) FROM bars WHERE symbol=?", (sym,)).fetchone()
    with cstore.tx() as c:
        c.execute("INSERT OR REPLACE INTO symbols(symbol, base, status, first_d, last_d, updated) VALUES (?,?,?,?,?,?)",
                  (sym, base, status, r[0], r[1], now_ts()))


def live_pairs() -> dict[str, str]:
    info = http_json(f"{REST}/exchangeInfo", params={"permissions": "SPOT"}, timeout=30, total_timeout=90)
    trading = {s["symbol"] for s in info.get("symbols", []) if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"}
    return coin_pairs(trading)


def archive_pairs() -> dict[str, str]:
    """Every USDT pair the public archive has daily files for (listed and delisted)."""
    found, marker = set(), ""
    for _ in range(100):
        r = http_get(LISTING, params={"delimiter": "/", "prefix": "data/spot/monthly/klines/", "marker": marker},
                     timeout=30, total_timeout=90)
        txt = r.text
        found.update(re.findall(r"<Prefix>data/spot/monthly/klines/([A-Z0-9]+)/</Prefix>", txt))
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", txt)
        if "<IsTruncated>true</IsTruncated>" not in txt or not m:
            break
        marker = m.group(1)
    return coin_pairs(found)


def _rest_history(sym: str, since_ms: int = START_MS) -> list[tuple]:
    rows, start, now_ms = [], since_ms, now_ts() * 1000
    for _ in range(20):
        kl = http_json(f"{REST}/klines", params={"symbol": sym, "interval": "1d", "startTime": start, "limit": 1000},
                       timeout=20, total_timeout=60)
        if not kl:
            break
        rows += _rows_from_klines(sym, kl, now_ms)
        if len(kl) < 1000:
            break
        start = int(kl[-1][0]) + 86_400_000
    return rows


def _archive_history(sym: str) -> list[tuple]:
    r = http_get(LISTING, params={"prefix": f"data/spot/monthly/klines/{sym}/1d/"}, timeout=30, total_timeout=90)
    keys = [k for k in re.findall(r"<Key>([^<]+)</Key>", r.text) if k.endswith(".zip")]
    rows, now_ms = [], now_ts() * 1000
    for k in sorted(keys):
        z = http_get(ARCHIVE + k, timeout=30, total_timeout=90)
        with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
            for name in zf.namelist():
                with zf.open(name) as f:
                    rows += _rows_from_klines(sym, list(csv.reader(io.TextIOWrapper(f, "utf-8"))), now_ms)
    return rows


def fetch_fng() -> int:
    d = http_json(FNG, params={"limit": 0, "format": "json"}, timeout=30, total_timeout=90).get("data") or []
    rows = [(_day(int(x["timestamp"]) * 1000), int(x["value"])) for x in d if x.get("value") not in (None, "")]
    with cstore.tx() as c:
        c.executemany("INSERT OR REPLACE INTO fng(d, v) VALUES (?, ?)", rows)
    return len(rows)


def _set(state: str | None = None, message: str | None = None, done: int | None = None, total: int | None = None) -> None:
    with _lock:
        if state is not None:
            STATUS["state"] = state
            if state == "running":
                STATUS.update(started_at=now_ts(), finished_at=None)
            elif state in ("done", "error"):
                STATUS["finished_at"] = now_ts()
        if message is not None:
            STATUS["message"] = message
        if done is not None:
            STATUS["done"] = done
        if total is not None:
            STATUS["total"] = total


def download_all() -> dict:
    """Full history (resumable: finished pairs are skipped). Live pairs from the REST API, delisted ones from the
    archive."""
    cstore.init()
    _set("running", "Kripto: coin listesi alınıyor…", 0, 0)
    try:
        live = live_pairs()
        allp = archive_pairs()
        gone = {s: b for s, b in allp.items() if s not in live}
        todo_done = cstore.done_keys("history")
        jobs = [(s, b, "live") for s, b in sorted(live.items())] + [(s, b, "gone") for s, b in sorted(gone.items())]
        jobs = [j for j in jobs if j[0] not in todo_done]
        total = len(jobs)
        _set(message=f"Kripto geçmişi: {len(live)} işlem gören + {len(gone)} borsadan çıkmış coin", done=0, total=total)
        n_fng = fetch_fng()
        done = [0]
        errors: list[str] = []

        def one(job):
            sym, base, status = job
            try:
                rows = _rest_history(sym) if status == "live" else _archive_history(sym)
                _store_bars(rows)
                _store_symbol(sym, base, status)
                cstore.mark("history", sym, {"bars": len(rows), "status": status})
            except Exception as e:  # noqa: BLE001 - one pair must not stop the rest
                errors.append(f"{sym}: {friendly_error(e)}")
                log.warning("crypto history %s: %s", sym, e)
            with _lock:
                done[0] += 1
            if done[0] % 10 == 0 or done[0] == total:
                _set(message=f"Kripto geçmişi indiriliyor: {done[0]}/{total} coin ({sym})", done=done[0])

        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, jobs))
        cnt = cstore.counts()
        msg = (f"Kripto geçmişi hazır: {cnt['symbols']} coin, {cnt['bars']:,} günlük mum ({cnt['first']} → {cnt['last']}), "
               f"korku endeksi {n_fng} gün" + (f" · {len(errors)} coin alınamadı" if errors else ""))
        _set("done", msg, total, total)
        cstore.kv_set("crypto_download", {"at": now_ts(), "counts": cnt, "errors": errors[:50]})
        return {"ok": True, "counts": cnt, "errors": errors}
    except Exception as e:  # noqa: BLE001
        log.exception("crypto download failed")
        _set("error", f"Kripto indirme hatası: {friendly_error(e)}")
        return {"ok": False, "error": str(e)}


def update_recent(days: int = 4) -> dict:
    """Daily refresh: the last few finished candles of every pair trading now (new listings get their full history),
    pairs that left the market are marked gone, and the latest Fear & Greed values."""
    cstore.init()
    live = live_pairs()
    c = cstore.conn()
    known = {r[0]: r[1] for r in c.execute("SELECT symbol, status FROM symbols")}
    last_d = {r[0]: r[1] for r in c.execute("SELECT symbol, MAX(d) FROM bars GROUP BY symbol")}
    recent = (now_ts() - days * 86400) * 1000

    def since_of(sym: str) -> int:
        d = last_d.get(sym)
        if sym not in known or not d:
            return START_MS
        own = int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()) * 1000 - 2 * 86_400_000
        return min(recent, own)

    def one(item):
        sym, base = item
        try:
            rows = _rest_history(sym, since_of(sym))
            _store_bars(rows)
            _store_symbol(sym, base, "live")
            return 0
        except Exception as e:  # noqa: BLE001
            log.warning("crypto update %s: %s", sym, e)
            return 1

    with ThreadPoolExecutor(WORKERS) as ex:
        failed = sum(ex.map(one, sorted(live.items())))
    left = [s for s, st in known.items() if st == "live" and s not in live]
    if left:
        with cstore.tx() as c:
            c.executemany("UPDATE symbols SET status='gone', updated=? WHERE symbol=?", [(now_ts(), s) for s in left])
    try:
        fetch_fng()
    except Exception as e:  # noqa: BLE001
        log.warning("fng update: %s", e)
    out = {"pairs": len(live), "failed": failed, "delisted": left, "at": now_ts()}
    cstore.kv_set("crypto_update", out)
    return out


def prices() -> dict[str, float]:
    """Last trade price of every USDT pair (one public call)."""
    return {x["symbol"]: float(x["price"]) for x in http_json(f"{REST}/ticker/price", timeout=15, total_timeout=30)
            if x.get("symbol", "").endswith("USDT")}


if __name__ == "__main__":       # py -m marketdesk.crypto.data
    logging.basicConfig(level=logging.INFO)
    t0 = time.time()
    th = threading.Thread(target=download_all, daemon=True)
    th.start()
    while th.is_alive():
        th.join(30)
        print(f"[{time.time() - t0:6.0f}s] {STATUS['message']}", flush=True)
    print(STATUS)
