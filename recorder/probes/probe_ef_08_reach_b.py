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
with open(os.path.join(HERE, "probe_ef_src.pkl"), "rb") as fh:
    SRC = pickle.load(fh)

cols, rows = blob["cols"], blob["rows"]
ix = {c: i for i, c in enumerate(cols)}
FIDX = {k: [ix[c] for c in v] for k, v in FAM.items()}
pop = [r for r in rows if r[ix["is_live"]] == 1 and r[ix["is_independent"]] == 1]
NET = {"1399811149": "solana", "8453": "base", "4663": "robinhood", "56": "bsc"}


def nm(n):
    return NET.get(str(n), "net" + str(n))


print("distinct tokens in model population:",
      len({(r[ix["token_address"]], str(r[ix["network_id"]]))for r in pop}))

print("\n=== REACHABILITY: family NULL, but source has a row at/<= entry_ts ===")
print("%-22s %-20s %7s %9s %9s %9s %9s" % (
    "family", "source", "NULLrows", "reach_ex", "reach_low", "onlyLOW", "unreach"))
detail = {}
for fam, info in SRC.items():
    ex, lo = info["exact"], info["lower"]
    nulls = [r for r in pop if all(r[i] is None for i in FIDX[fam])]
    re_ex = re_lo = only_lo = 0
    per_net = Counter()
    for r in nulls:
        a, net, t0 = r[ix["token_address"]], str(r[ix["network_id"]]), r[ix["entry_ts"]]
        e = ex.get((a, net))
        l = lo.get((str(a).lower(), net))
        ok_e = e is not None and e <= t0
        ok_l = l is not None and l <= t0
        if ok_e:
            re_ex += 1
            per_net[nm(net)] += 1
        if ok_l:
            re_lo += 1
        if ok_l and not ok_e:
            only_lo += 1
    print("%-22s %-20s %7d %9d %9d %9d %9d" % (
        fam, info["table"], len(nulls), re_ex, re_lo, only_lo, len(nulls) - re_lo))
    detail[fam] = per_net

print("\n=== of the reachable-but-NULL, breakdown by network ===")
for fam, c in detail.items():
    if sum(c.values()):
        print(" ", fam, dict(c))

# ---- case-sensitivity audit on the address itself ----
print("\n=== ADDRESS CASE in population vs sources ===")
mixed = sum(1 for r in pop if r[ix["token_address"]] != str(r[ix["token_address"]]).lower())
print("population rows with a NON-lowercase token_address: %d / %d" % (mixed, len(pop)))
for fam, info in SRC.items():
    m = sum(1 for (a, n) in info["exact"] if a != str(a).lower())
    print("  %-20s groups with non-lowercase address: %d / %d" % (
        info["table"], m, len(info["exact"])))
