import os, sqlite3, config, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== J. the 12 NULL-created_at tokens: when were their windows opened? ===")
q = """
SELECT w.first_seen_at, w.active, w.is_control, w.source,
       substr(ts.token_address,1,12) AS addr, ts.network_id
FROM token_static ts JOIN watchlist w
  ON w.token_address=ts.token_address AND w.network_id=ts.network_id
WHERE ts.token_created_at IS NULL OR ts.token_created_at=''
ORDER BY w.first_seen_at
"""
for r in con.execute(q):
    print("   ", dict(r))

print()
print("=== K. gate live-window boundary: windows opened before/after the gate ===")
q = """
SELECT CASE WHEN first_seen_at >= '2026-08-22' THEN 'post_gate_day' ELSE 'pre_gate' END AS era,
       is_control, COUNT(*) n, MIN(first_seen_at) lo, MAX(first_seen_at) hi
FROM watchlist GROUP BY 1,2 ORDER BY 1,2
"""
for r in con.execute(q):
    print("   ", dict(r))

print()
print("=== L. do ANY token_static rows created post-gate lack created_at? ===")
q = """
SELECT COUNT(*) n FROM token_static ts JOIN watchlist w
  ON w.token_address=ts.token_address AND w.network_id=ts.network_id
WHERE (ts.token_created_at IS NULL OR ts.token_created_at='')
  AND w.first_seen_at >= '2026-08-22'
"""
print("   post-gate windows whose token has NULL created_at:", dict(con.execute(q).fetchone()))
con.close()
