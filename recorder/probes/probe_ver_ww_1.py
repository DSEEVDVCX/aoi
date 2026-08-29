import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, params=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        rows = con.execute(sql, params).fetchall()
    except Exception as e:
        print("FAILED:", e)
        return []
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print("   ... (%d rows total)" % len(rows))
    return rows

# 0. totals
q("A0 outcomes by kind", "SELECT kind, COUNT(*) n FROM outcomes GROUP BY kind")
q("A1 watch_windows total + by design_version+is_control",
  "SELECT design_version, is_control, COUNT(*) n FROM watch_windows GROUP BY 1,2 ORDER BY 1,2")
q("A2 watch outcomes by design_version, is_control, analysis_eligible, exclusion_reason",
  """SELECT design_version, is_control, analysis_eligible, exclusion_reason, COUNT(*) n
       FROM outcomes WHERE kind='watch' GROUP BY 1,2,3,4 ORDER BY 1,2,3""")

# 1. MY OWN measurement: build the window key set in python, anti-join in python.
wkeys = set()
for r in con.execute("SELECT token_address, network_id, first_seen_at FROM watch_windows"):
    wkeys.add(f"{r['token_address']}:{r['network_id']}:{r['first_seen_at']}")
print("=" * 70)
print("B0 distinct window keys built in python:", len(wkeys))

orphans = []
allwatch = 0
for r in con.execute(
    """SELECT key, token_address, network_id, is_control, design_version, status,
              entry_ts, labeled_at, analysis_eligible, exclusion_reason
         FROM outcomes WHERE kind='watch'"""):
    allwatch += 1
    if r["key"] not in wkeys:
        orphans.append(dict(r))
print("B1 total kind='watch' outcomes:", allwatch)
print("B2 orphans (python set anti-join):", len(orphans))

from collections import Counter
print("B3 orphan design_version:", Counter(o["design_version"] for o in orphans))
print("B4 orphan is_control:", Counter(o["is_control"] for o in orphans))
print("B5 orphan status:", Counter(o["status"] for o in orphans))
print("B6 orphan analysis_eligible/reason:",
      Counter((o["analysis_eligible"], o["exclusion_reason"]) for o in orphans))
print("B7 orphan entry_ts min/max:",
      min(o["entry_ts"] for o in orphans), max(o["entry_ts"] for o in orphans))
print("B8 orphan network_id:", Counter(str(o["network_id"]) for o in orphans))
print("B9 sample orphan keys:", [o["key"] for o in orphans[:5]])

# 2. how many design_version=1 watch outcomes exist at all -> what is the denominator?
q("C0 kind=watch design_version=1 count", "SELECT COUNT(*) n FROM outcomes WHERE kind='watch' AND design_version=1")

con.close()
