import os
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

with open(os.path.join(HERE, "probe_ef_rows.pkl"), "rb") as fh:
    blob = pickle.load(fh)
with open(os.path.join(HERE, "probe_ef_fam.pkl"), "rb") as fh:
    FAM = pickle.load(fh)
cols, rows = blob["cols"], blob["rows"]
ix = {c: i for i, c in enumerate(cols)}
FIDX = {k: [ix[c] for c in v] for k, v in FAM.items()}
pop = [r for r in rows if r[ix["is_live"]] == 1 and r[ix["is_independent"]] == 1]
N = len(pop)
NET = {"1399811149": "solana", "8453": "base", "4663": "robinhood", "56": "bsc"}

FEAT = [c for c in cols if c not in
        ("kind", "key", "token_address", "network_id", "entry_ts", "asset_class",
         "split", "is_independent", "is_live", "status", "suspect_bars",
         "feature_version", "built_at") and c not in FAM["labels"]]
FEATIDX = [ix[c] for c in FEAT]
print("feature columns counted:", len(FEAT))


def day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---- how many feature CELLS are non-NULL per row ----
filled = [sum(1 for i in FEATIDX if r[i] is not None) for r in pop]
filled.sort()
print("\n=== NON-NULL feature cells per row (out of %d) ===" % len(FEAT))
import statistics
print("  min=%d p05=%d p25=%d median=%d p75=%d max=%d mean=%.1f" % (
    filled[0], filled[int(.05 * N)], filled[int(.25 * N)], filled[N // 2],
    filled[int(.75 * N)], filled[-1], statistics.mean(filled)))
for thr in (40, 50, 60, 70, 80):
    n = sum(1 for x in filled if x <= thr)
    print("  rows with <= %3d of %d feature cells filled: %5d (%4.1f%%)" % (
        thr, len(FEAT), n, 100.0 * n / N))

# ---- strict usability ----
NETFREE = ["token_static", "market_snapshot", "social_thesis", "price_path",
           "regime", "density", "chain_ownership", "flow", "onchain_conc"]
NETLIMITED = ["onchain_auth_sol", "onchain_contract_evm"]
TIMEDOC = ["toptrader_periods"]

strict = [r for r in pop
          if not any(all(r[i] is None for i in FIDX[f]) for f in NETFREE)]
print("\n=== STRICT USABLE: no entirely-NULL family among the %d network-agnostic families ===" % len(NETFREE))
print("  %d / %d rows (%.1f%%)" % (len(strict), N, 100.0 * len(strict) / N))
print("  by network:", dict(Counter(NET.get(str(r[ix["network_id"]]), str(r[ix["network_id"]])) for r in strict)))
print("  entry day range:", min(day(r[ix["entry_ts"]]) for r in strict), "..",
      max(day(r[ix["entry_ts"]]) for r in strict))
print("  by entry day:", dict(sorted(Counter(day(r[ix["entry_ts"]]) for r in strict).items())))

strict2 = [r for r in strict
           if not all(r[i] is None for i in FIDX["toptrader_periods"])]
print("\n  ...and additionally with toptrader_periods present: %d (%.1f%%)" % (
    len(strict2), 100.0 * len(strict2) / N))

# ---- toptrader_periods switch-on date ----
tp = [(day(r[ix["entry_ts"]]), not all(r[i] is None for i in FIDX["toptrader_periods"]))
      for r in pop]
byd = defaultdict(lambda: [0, 0])
for d, ok in tp:
    byd[d][0] += 1
    byd[d][1] += 1 if ok else 0
print("\n=== toptrader_periods presence by entry day ===")
for d in sorted(byd):
    t, o = byd[d]
    print("  %s  %4d rows, present in %4d (%5.1f%%)" % (d, t, o, 100.0 * o / t))

# ---- lag between entry and build ----
print("\n=== build lag (built_at - entry_ts), hours ===")
lags = []
for r in pop:
    try:
        b = datetime.fromisoformat(str(r[ix["built_at"]]).replace("Z", "+00:00"))
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        lags.append((b.timestamp() - r[ix["entry_ts"]]) / 3600)
    except Exception:
        pass
lags.sort()
print("  n=%d  min=%.1fh p25=%.1fh median=%.1fh p75=%.1fh max=%.1fh" % (
    len(lags), lags[0], lags[len(lags) // 4], lags[len(lags) // 2],
    lags[3 * len(lags) // 4], lags[-1]))
