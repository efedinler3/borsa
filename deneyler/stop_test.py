"""Stop-width experiment on crypto history (read-only: writes nothing to the app's databases).

For every live crypto bot the entries stay exactly as the bot makes them (same signals, same ranking, same holding
period, same position cap); only the stop changes, over every option of evolve.STOP_SET (target = stop x the bot's own
ratio; "none" = no stop, no target, plain time exit). Results on the training coins (groups 0-3), on the unseen exam
coins (group 4) and on the last 365 days. A baseline with random entries shows whether stops help crypto at all.
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\Users\sefed\OneDrive\Masaüstü\trade")
from marketdesk.crypto import cpool, cstore, ctree  # noqa: E402
from marketdesk.lab import evolve, learn, neuro, tree  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop_test_result.json")
t0 = time.time()


def say(m):
    print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)


con = cstore.conn()
bots = []
for bid, br, ni, rk, g in con.execute("SELECT id, branch, niche, rank, genome FROM cfleet_bots WHERE status='live' ORDER BY branch, niche, rank"):
    bots.append({"id": bid, "branch": br, "niche": ni, "rank": rk, "g": json.loads(g)})
say(f"{len(bots)} live bots")

pool = cpool.CryptoPool(progress=lambda m, i, n: None)
ctx = ctree._context(pool)
nd = ctx["nd"]
say(f"pool: {pool.n} rows, {nd} days, {pool.first_date} -> {pool.last_date}")
order = np.argsort(pool.dayidx, kind="stable")
X_cache = {}

STOPS = list(evolve.STOP_SET)
LAST = nd - 365


def stats(arr, lo=-1):
    s = neuro._agg_stats(tree._rows(arr, lo))
    return {k: s.get(k) for k in ("n", "days", "daily_pct", "avg_pct", "t", "win_rate", "pos_years", "years")}


def run_idx(parts, s_i, r_i, hold):
    out = {}
    for name, idx in parts.items():
        out[name] = tree._trades(ctx, idx, s_i, r_i, hold)
    return {"train": stats(out["train"]), "exam": stats(out["exam"]), "last365": stats(out["train"], LAST),
            "last365_exam": stats(out["exam"], LAST)}


def rule_parts(g, niche):
    m = learn._mask(pool, g)
    if ctree.NICHES[niche]["fear"]:
        m &= ctx["fear_row"]
    parts = {}
    rk = g.get("order") or "rsi2"
    sign, _t = ctree.RANKS[rk]
    for name in ("train", "exam"):
        idx = np.nonzero(m & ctx[name])[0]
        score = np.nan_to_num(-sign * pool.F[rk][idx], nan=-1e9)
        parts[name] = tree._picked(ctx, idx, score)
    return parts


def nn_parts(g, niche):
    key = json.dumps(g["norm"], sort_keys=True)
    if key not in X_cache:
        X_cache.clear()
        X_cache[key] = ctree.matrix(pool, order, g["norm"])
    X = X_cache[key]
    sc = ctree.forward(np.asarray(g["w"], dtype=np.float32), X)
    m = sc > 0
    if ctree.NICHES[niche]["fear"]:
        m &= ctx["fear_row"][order]
    parts = {}
    for name in ("train", "exam"):
        mm = m & ctx[name][order]
        parts[name] = tree._picked(ctx, order[mm], sc[mm])
    return parts


results = []
seen = {}
for b in bots:
    g, br, ni = b["g"], b["branch"], b["niche"]
    hold = int(g.get("hold") or g.get("hold_d") or 1)
    ratio = float(g["ratio"])
    r_i = evolve.RATIO_SET.index(min(evolve.RATIO_SET, key=lambda r: abs(r - ratio)))
    own = tuple(g["stop"])
    sig = json.dumps({k: g.get(k) for k in ("conds", "order", "w", "hold", "niche")}, sort_keys=True) + br + ni
    if sig in seen:                      # identical entries (fill clones): only the stop/ratio may differ
        parts = seen[sig]
    else:
        parts = rule_parts(g, ni) if br == "kural" else nn_parts(g, ni)
        seen[sig] = parts
    row = {"id": b["id"], "branch": br, "niche": ni, "rank": b["rank"], "hold": hold, "ratio": evolve.RATIO_SET[r_i],
           "own_stop": list(own), "entries_sig": hash(sig) % 10**8, "by_stop": {}}
    for s_i, s in enumerate(STOPS):
        row["by_stop"][f"{s[0]}:{s[1]:g}"] = run_idx(parts, s_i, r_i, hold)
    results.append(row)
    say(f"{br} {ni} #{b['rank']} hold {hold} own {own} done")

# baseline: random entries from the day's universe, 5 a day
rng = np.random.default_rng(11)
base = []
for hold in (1, 2, 7, 30):
    parts = {}
    for name in ("train", "exam"):
        idx = np.nonzero(ctx[name])[0]
        parts[name] = tree._picked(ctx, idx, rng.random(len(idx)))
    for r_i in (0, 2):
        row = {"hold": hold, "ratio": evolve.RATIO_SET[r_i], "by_stop": {}}
        for s_i, s in enumerate(STOPS):
            row["by_stop"][f"{s[0]}:{s[1]:g}"] = run_idx(parts, s_i, r_i, hold)
        base.append(row)
    say(f"baseline hold {hold} done")

# typical stop size of each ATR option in this universe
atr = pool.F["atr"].astype(np.float64)
atr = atr[np.isfinite(atr)]
atr_q = {q: float(np.quantile(atr, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}

json.dump({"range": [pool.first_date, pool.last_date], "days": int(nd), "rows": int(pool.n), "atr_quantiles": atr_q,
           "stops": [f"{s[0]}:{s[1]:g}" for s in STOPS], "bots": results, "baseline": base,
           "seconds": round(time.time() - t0, 1)}, open(OUT, "w"), default=lambda o: o.item() if hasattr(o, "item") else str(o))
say(f"saved {OUT}")
