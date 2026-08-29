import os, sqlite3, config, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features
import recorder as rec

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== E. does upstream send ISO under the SAME 'createdAt' key elsewhere? ===")
print("signal_events.ts (extract.py:204 = _str(event['createdAt'])):")
q = """
SELECT CASE WHEN ts GLOB '*T*' THEN 'iso_T'
            WHEN ts NOT GLOB '*[^0-9]*' THEN 'pure_digits'
            ELSE 'other' END AS shape,
       COUNT(*) n, MIN(ts) lo, MAX(ts) hi
FROM signal_events GROUP BY 1 ORDER BY n DESC
"""
for r in con.execute(q):
    print("   ", dict(r))

print()
print("=== F. what happens if recorder.token_age_days is fed signal_events.ts shape? ===")
sample = con.execute("SELECT ts FROM signal_events WHERE ts IS NOT NULL LIMIT 1").fetchone()
if sample:
    s = sample["ts"]
    NOW = "2026-08-22T23:00:00+00:00"
    print(f"    sample ts = {s!r}")
    print(f"    rec.token_age_days -> {rec.token_age_days(s, NOW)}")
    print(f"    features.epoch_of  -> {features.epoch_of(s)}")

print()
print("=== G. are the 12 NULL-created_at tokens actually gate subjects? ===")
q = """
SELECT
 (SELECT COUNT(*) FROM token_static WHERE token_created_at IS NULL OR token_created_at='') AS null_static,
 (SELECT COUNT(*) FROM token_static ts JOIN watchlist w
    ON w.token_address=ts.token_address AND w.network_id=ts.network_id
   WHERE ts.token_created_at IS NULL OR ts.token_created_at='') AS null_and_in_watchlist,
 (SELECT COUNT(*) FROM watchlist) AS watch_rows
"""
print(dict(con.execute(q).fetchone()))

print()
print("=== H. gate counters in meta (is the fail-closed audible?) ===")
q = """SELECT key, value FROM meta WHERE key LIKE '%age%' ORDER BY key"""
for r in con.execute(q):
    print("   ", r["key"], "=", r["value"])

print()
print("=== I. watch windows opened since the gate went live, by arm ===")
q = """
SELECT is_control, COUNT(*) n, MIN(added_at) lo, MAX(added_at) hi
FROM watchlist WHERE added_at >= '2026-08-22' GROUP BY is_control
"""
for r in con.execute(q):
    print("   ", dict(r))
con.close()
