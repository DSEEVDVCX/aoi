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
cols = blob["cols"]
rows = blob["rows"]
ix = {c: i for i, c in enumerate(cols)}
FAMS = list(FAM.keys())
FIDX = {k: [ix[c] for c in v] for k, v in FAM.items()}

pop = [r for r in rows if r[ix["is_live"]] == 1 and r[ix["is_independent"]] == 1]
print("model population:", len(pop))

NET = {"1399811149": "solana", "8453": "base", "4663": "robinhood"}


def empties(r):
    return {k for k in FAMS if all(r[i] is None for i in FIDX[k])}


def day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---- 1) per-family coverage over the model population ----
print("\n=== FAMILY-LEVEL EMPTINESS (model population, n=%d) ===" % len(pop))
allempty = Counter()
for r in pop:
    for k in empties(r):
        allempty[k] += 1
for k in FAMS:
    n = allempty[k]
    print(f"  {k:24s} entirely NULL in {n:6d} / {len(pop)}  ({100.0*n/len(pop):5.1f}%)")

# ---- 2) distribution of empty-family count ----
FEATFAM = [k for k in FAMS if k != "labels"]
FEATFAM_NOTRIG = [k for k in FEATFAM if k != "trigger"]
dist = Counter()
for r in pop:
    e = empties(r)
    dist[len(e & set(FEATFAM))] += 1
print("\n=== DISTRIBUTION: # of entirely-NULL FEATURE families per row (13 families, labels excluded) ===")
for k in sorted(dist):
    print(f"  {k:2d} empty families : {dist[k]:6d} rows ({100.0*dist[k]/len(pop):5.1f}%)")

# ---- 3) rows where everything except trigger is NULL ----
ghosts = []
near = Counter()
for r in pop:
    e = empties(r)
    nt = e & set(FEATFAM_NOTRIG)
    near[len(nt)] += 1
    if len(nt) == len(FEATFAM_NOTRIG):
        ghosts.append(r)
print(f"\n=== TOTAL GHOSTS: all 12 non-trigger feature families NULL: {len(ghosts)} rows ===")
for r in ghosts[:10]:
    print("   key=%s net=%s(%s) entry=%s built=%s trigger_nulls=%d/35" % (
        r[ix["key"]], r[ix["network_id"]], NET.get(str(r[ix["network_id"]]), "?"),
        day(r[ix["entry_ts"]]), r[ix["built_at"]][:19],
        sum(1 for i in FIDX["trigger"] if r[i] is None)))

print("\n=== distribution of non-trigger empty families (12 max) ===")
for k in sorted(near):
    print(f"  {k:2d} : {near[k]:6d} rows ({100.0*near[k]/len(pop):5.1f}%)")

# ---- worst rows: most non-trigger empty families ----
worst = sorted(pop, key=lambda r: -len(empties(r) & set(FEATFAM_NOTRIG)))
print("\n=== 15 EMPTIEST ROWS ===")
for r in worst[:15]:
    e = empties(r) & set(FEATFAM_NOTRIG)
    print("  key=%-40s net=%-9s entry=%s built=%s  %2d empty: %s" % (
        r[ix["key"]][:40], NET.get(str(r[ix["network_id"]]), str(r[ix["network_id"]])),
        day(r[ix["entry_ts"]]), r[ix["built_at"]][:10], len(e), ",".join(sorted(e))))
