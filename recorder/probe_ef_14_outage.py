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
FEATFAM_NOTRIG = [k for k in FAM if k not in ("labels", "trigger")]


def hh(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H")


def es(r):
    return {k for k in FEATFAM_NOTRIG if all(r[i] is None for i in FIDX[k])}


# hourly emptiness 08-16 .. 08-21
print("=== HOURLY: rows and mean empty families, 2026-08-16..2026-08-21 ===")
byh = defaultdict(list)
for r in pop:
    h = hh(r[ix["entry_ts"]])
    if "2026-08-16" <= h[:10] <= "2026-08-21":
        byh[h].append(r)
print("%-14s %5s %7s  %s" % ("hour utc", "rows", "mean_e", "families empty in >50% of the hour"))
for h in sorted(byh):
    rr = byh[h]
    m = sum(len(es(r)) for r in rr) / len(rr)
    hot = [f for f in FEATFAM_NOTRIG
           if sum(1 for r in rr if all(r[i] is None for i in FIDX[f])) > len(rr) / 2]
    hot = [f for f in hot if f not in ("onchain_contract_evm", "onchain_auth_sol")]
    if m > 2.0 or len(rr) < 5:
        print("%-14s %5d %7.2f  %s" % (h, len(rr), m, ",".join(hot)))

print("\n=== 2026-08-19 detail: per-family NULL%% vs 2026-08-18 and 08-20 ===")
sets = {}
for d in ("2026-08-18", "2026-08-19", "2026-08-20"):
    sets[d] = [r for r in pop
               if datetime.fromtimestamp(r[ix["entry_ts"]], tz=timezone.utc)
               .strftime("%Y-%m-%d") == d]
print("%-24s" + "")
hdr = "%-24s" % "family" + "".join("%14s" % d[5:] for d in sets)
print(hdr)
for f in FEATFAM_NOTRIG:
    line = "%-24s" % f
    for d, rr in sets.items():
        n = sum(1 for r in rr if all(r[i] is None for i in FIDX[f]))
        line += "%13.1f%%" % (100.0 * n / len(rr))
    print(line)
print("row counts:", {d: len(v) for d, v in sets.items()})

# how many hours in 08-13..08-20 have zero population rows (gap detection)
print("\n=== HOURS WITH ZERO population rows, 2026-08-13..2026-08-20 ===")
have = {hh(r[ix["entry_ts"]]) for r in pop}
start = datetime(2026, 8, 13, tzinfo=timezone.utc)
gaps = []
for k in range(8 * 24):
    t = start.timestamp() + k * 3600
    lab = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H")
    if lab not in have:
        gaps.append(lab)
print("  %d of 192 hours have no row. Runs:" % len(gaps))
run = []
for g in gaps:
    if run and (datetime.strptime(g, "%Y-%m-%d %H") -
                datetime.strptime(run[-1], "%Y-%m-%d %H")).total_seconds() == 3600:
        run.append(g)
    else:
        if len(run) >= 2:
            print("    %s .. %s  (%dh)" % (run[0], run[-1], len(run)))
        run = [g]
if len(run) >= 2:
    print("    %s .. %s  (%dh)" % (run[0], run[-1], len(run)))

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row
print("\n=== meta: shutout / downtime / 403 related ===")
for r in con.execute("""SELECT key, substr(value,1,110) v FROM meta
     WHERE key LIKE '%shutout%' OR key LIKE '%403%' OR key LIKE '%streak%'
        OR key LIKE '%downtime%' OR key LIKE '%last_ok%' OR key LIKE '%block%'
     ORDER BY key"""):
    print("  %-42s = %s" % (r["key"], r["v"]))
con.close()
