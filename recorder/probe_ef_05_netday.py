import os
import pickle
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

with open(os.path.join(HERE, "probe_ef_rows.pkl"), "rb") as fh:
    blob = pickle.load(fh)
with open(os.path.join(HERE, "probe_ef_fam.pkl"), "rb") as fh:
    FAM = pickle.load(fh)
cols, rows = blob["cols"], blob["rows"]
ix = {c: i for i, c in enumerate(cols)}
FAMS = list(FAM.keys())
FIDX = {k: [ix[c] for c in v] for k, v in FAM.items()}
FEATFAM_NOTRIG = [k for k in FAMS if k not in ("labels", "trigger")]

pop = [r for r in rows if r[ix["is_live"]] == 1 and r[ix["is_independent"]] == 1]
N = len(pop)
NET = {"1399811149": "solana", "8453": "base", "4663": "robinhood",
       "56": "bsc", "1": "eth"}


def nm(n):
    return NET.get(str(n), "net" + str(n))


def emptyset(r):
    return {k for k in FEATFAM_NOTRIG if all(r[i] is None for i in FIDX[k])}


def day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---- network distribution + emptiness ----
print("=== NETWORK x empty-family count (model population n=%d) ===" % N)
bynet = defaultdict(list)
for r in pop:
    bynet[nm(r[ix["network_id"]])].append(len(emptyset(r)))
print("%-10s %7s %7s %7s %7s %7s" % ("net", "rows", "%pop", "mean_e", ">=6e", "pct>=6"))
for k in sorted(bynet, key=lambda x: -len(bynet[x])):
    v = bynet[k]
    hi = sum(1 for x in v if x >= 6)
    print("%-10s %7d %6.1f%% %7.2f %7d %6.1f%%" % (
        k, len(v), 100.0 * len(v) / N, sum(v) / len(v), hi, 100.0 * hi / len(v)))

# ---- per-family emptiness per network ----
print("\n=== per-family entirely-NULL %% by network ===")
nets = sorted(bynet, key=lambda x: -len(bynet[x]))
print("%-24s" % "family" + "".join("%12s" % k for k in nets))
rows_by_net = defaultdict(list)
for r in pop:
    rows_by_net[nm(r[ix["network_id"]])].append(r)
for f in FAMS:
    line = "%-24s" % f
    for k in nets:
        rr = rows_by_net[k]
        n = sum(1 for r in rr if all(r[i] is None for i in FIDX[f]))
        line += "%11.1f%%" % (100.0 * n / len(rr))
    print(line)

# ---- time: entry day ----
print("\n=== BY ENTRY DAY (utc) ===")
print("%-12s %6s %7s %7s %7s" % ("entry_day", "rows", "mean_e", ">=6e", "pct>=6"))
byday = defaultdict(list)
for r in pop:
    byday[day(r[ix["entry_ts"]])].append(len(emptyset(r)))
for d in sorted(byday):
    v = byday[d]
    hi = sum(1 for x in v if x >= 6)
    print("%-12s %6d %7.2f %7d %6.1f%%" % (d, len(v), sum(v) / len(v), hi, 100.0 * hi / len(v)))

# ---- time: built_at day ----
print("\n=== BY BUILT_AT DAY ===")
print("%-12s %6s %7s %7s %7s" % ("built_day", "rows", "mean_e", ">=6e", "pct>=6"))
byb = defaultdict(list)
for r in pop:
    byb[str(r[ix["built_at"]])[:10]].append(len(emptyset(r)))
for d in sorted(byb):
    v = byb[d]
    hi = sum(1 for x in v if x >= 6)
    print("%-12s %6d %7.2f %7d %6.1f%%" % (d, len(v), sum(v) / len(v), hi, 100.0 * hi / len(v)))
