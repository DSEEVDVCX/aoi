import os
import pickle
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

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

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

# --- market_snapshot / token_static NULL: how long after entry does the first
#     source row arrive? (0 rows reachable => source starts AFTER entry_ts)
for fam, tab in (("market_snapshot", "market_ticks"), ("token_static", "token_static")):
    ex = SRC[fam]["exact"]
    nulls = [r for r in pop if all(r[i] is None for i in FIDX[fam])]
    gaps, never = [], 0
    for r in nulls:
        e = ex.get((r[ix["token_address"]], str(r[ix["network_id"]])))
        if e is None:
            never += 1
        else:
            gaps.append((e - r[ix["entry_ts"]]) / 60.0)
    gaps.sort()
    print("=== %s entirely NULL: %d rows ===" % (fam, len(nulls)))
    print("   token absent from %s entirely: %d" % (tab, never))
    if gaps:
        print("   first %s row arrives AFTER entry by (min): p10=%.1f p25=%.1f med=%.1f p75=%.1f p90=%.1f max=%.1f"
              % (tab, gaps[len(gaps) // 10], gaps[len(gaps) // 4], gaps[len(gaps) // 2],
                 gaps[3 * len(gaps) // 4], gaps[9 * len(gaps) // 10], gaps[-1]))
        print("   arrives within 60 min of entry: %d / %d (%.1f%%)" % (
            sum(1 for g in gaps if g <= 60), len(gaps),
            100.0 * sum(1 for g in gaps if g <= 60) / len(gaps)))

# --- the 659: are they replay rows / control rows? ---
ex = SRC["onchain_conc"]["exact"]
bad = [r for r in pop
       if all(r[i] is None for i in FIDX["onchain_conc"])
       and (lambda e: e is not None and e <= r[ix["entry_ts"]])(
           ex.get((r[ix["token_address"]], str(r[ix["network_id"]]))))]
print("\n=== the %d reachable-but-NULL onchain_conc rows: source row properties ===" % len(bad))
q = """SELECT is_replay, is_control, top1_pct IS NULL a, holder_count IS NULL b,
              recorded_at
         FROM chain_concentration
        WHERE token_address=? AND network_id=?
          AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
        ORDER BY CAST(strftime('%s', recorded_at) AS INTEGER) DESC LIMIT 1"""
c = Counter()
for r in bad:
    g = con.execute(q, (r[ix["token_address"]], str(r[ix["network_id"]]),
                        r[ix["entry_ts"]])).fetchone()
    c[(g["is_replay"], g["is_control"], g["a"], g["b"])] += 1
print("  (is_replay, is_control, top1_pct IS NULL, holder_count IS NULL) -> count")
for k, v in c.items():
    print("   ", k, "->", v)

# --- how many fv12 4663 rows exist at all vs fv8 ---
print("\n=== training_rows 4663/8453 by feature_version and is_independent ===")
for r in con.execute("""SELECT network_id, feature_version fv, is_independent ind,
                               COUNT(*) n, date(MIN(entry_ts),'unixepoch') a,
                               date(MAX(entry_ts),'unixepoch') b
                          FROM training_rows
                         WHERE kind='signal' AND network_id IN ('4663','8453')
                         GROUP BY 1,2,3 ORDER BY 1,2,3"""):
    print("  ", dict(r))

print("\n=== would-be model population if the EVM gate were lifted ===")
r = con.execute("""SELECT COUNT(*) n FROM outcomes o
   WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
     AND o.entry_ts >= ? AND COALESCE(o.network_id,'') IN ('4663','8453','143')
     AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind
                      AND r.key=o.key AND r.feature_version=12)""",
                (config.LIVE_START_TS,)).fetchone()
print("  additional EVM-network model candidates blocked:", r["n"])
con.close()
