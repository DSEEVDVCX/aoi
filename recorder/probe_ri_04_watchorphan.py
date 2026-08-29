import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:30]:
        print("   ", dict(r))
    if len(rows) > 30:
        print(f"    ... {len(rows)} rows total")
    return rows

# build the set of valid watch keys once, in python, to avoid the slow correlated join
t0 = time.time()
valid = set()
valid_ci = set()
for r in con.execute("SELECT token_address, network_id, first_seen_at FROM watch_windows"):
    k = f"{r['token_address']}:{r['network_id']}:{r['first_seen_at']}"
    valid.add(k)
    valid_ci.add(k.lower())
print(f"watch_windows keys: {len(valid)} ({time.time()-t0:.1f}s)")

orph = []
for r in con.execute("SELECT kind,key,token_address,network_id,design_version,status,labeled_at,is_control,entry_ts FROM outcomes WHERE kind='watch'"):
    if r["key"] not in valid:
        orph.append(dict(r))
print("orphan watch outcomes:", len(orph))
ci_recoverable = [o for o in orph if o["key"].lower() in valid_ci]
print("  of which recoverable by case-insensitive match:", len(ci_recoverable))
from collections import Counter
print("  design_version:", Counter(o["design_version"] for o in orph))
print("  status:", Counter(o["status"] for o in orph))
print("  labeled_at day:", Counter(str(o["labeled_at"])[:10] for o in orph))
print("  network:", Counter(str(o["network_id"]) for o in orph))
print("  is_control:", Counter(o["is_control"] for o in orph))
for o in orph[:8]:
    print("   sample:", o["key"], "| net", o["network_id"], "| dv", o["design_version"], "| st", o["status"])

# does the coin still exist in watch_windows at all?
coins_with_windows = set()
for r in con.execute("SELECT DISTINCT token_address, network_id FROM watch_windows"):
    coins_with_windows.add((r["token_address"], str(r["network_id"])))
no_coin = [o for o in orph if (o["token_address"], str(o["network_id"])) not in coins_with_windows]
print("  orphans whose coin has NO watch_windows row at all:", len(no_coin))
print("  orphans whose coin HAS windows but this first_seen differs:", len(orph)-len(no_coin))

# for the ones whose coin has windows, show nearest window first_seen_at
sample = [o for o in orph if (o["token_address"], str(o["network_id"])) in coins_with_windows][:6]
for o in sample:
    rows = con.execute("SELECT first_seen_at, source, design_version FROM watch_windows WHERE token_address=? AND network_id=? ORDER BY first_seen_at", (o["token_address"], o["network_id"])).fetchall()
    print(f"   coin {o['token_address'][:14]} orphan_key_fs={o['key'].split(':')[-1]} windows={[r['first_seen_at'] for r in rows][:6]}")

# how many outcomes-watch rows are in the phase1 view but orphaned
n = 0
for o in orph:
    if o["design_version"] and o["design_version"] >= 3:
        n += 1
print("  orphans with design_version>=3 (phase1 view candidates):", n)

# also: watch_windows with no outcomes -- is watch_until in the past?
q("watch_windows with no outcomes, split by watch_until vs now", """
SELECT CASE WHEN w.watch_until < strftime('%Y-%m-%dT%H:%M:%S', 'now') THEN 'closed' ELSE 'still_open' END st,
       COUNT(*) n, MIN(w.first_seen_at) mn, MAX(w.first_seen_at) mx
  FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o
                    WHERE o.kind='watch'
                      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
 GROUP BY 1
""")

con.close()
