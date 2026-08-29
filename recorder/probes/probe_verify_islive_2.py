import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== Is the watch asset_class actually LOST, or held in token_class? ===")
q = """
SELECT COALESCE(tc.asset_class,'<no token_class row>') AS ac, COUNT(*) n
  FROM training_rows tr
  LEFT JOIN token_class tc
    ON tc.token_address = tr.token_address
   AND tc.network_id = tr.network_id
 WHERE tr.kind='watch'
 GROUP BY ac ORDER BY n DESC
"""
tot = 0
for r in con.execute(q):
    print(f"  token_class.asset_class={r['ac']:<22} n={r['n']}")
    tot += r["n"]
print("  total watch rows:", tot)

print("\n=== Control arm: classification recoverable for the 307 control rows? ===")
q2 = """
SELECT COALESCE(tc.asset_class,'<none>') ac, COUNT(*) n
  FROM training_rows tr
  JOIN outcomes o ON o.kind=tr.kind AND o.key=tr.key AND o.is_control=1
  LEFT JOIN token_class tc ON tc.token_address=tr.token_address AND tc.network_id=tr.network_id
 WHERE tr.kind='watch' GROUP BY ac ORDER BY n DESC
"""
for r in con.execute(q2):
    print(f"  {r['ac']:<12} n={r['n']}")
con.close()
