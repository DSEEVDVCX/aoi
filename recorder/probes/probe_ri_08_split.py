import os, sqlite3, config, time, hashlib

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = int(con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])
print("feature_version:", FV)

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")
    return rows

print("=== SPLIT consistency ===")
q("coins in >1 split (outcomes)", """
SELECT COUNT(*) coins FROM (
  SELECT token_address, COUNT(DISTINCT split) s FROM outcomes WHERE split IS NOT NULL GROUP BY 1 HAVING s>1)""")
q("coins in >1 split (training_rows)", """
SELECT COUNT(*) coins FROM (
  SELECT token_address, COUNT(DISTINCT split) s FROM training_rows WHERE split IS NOT NULL GROUP BY 1 HAVING s>1)""")
q("coins in >1 split, case-insensitive (training_rows)", """
SELECT COUNT(*) coins FROM (
  SELECT lower(token_address) a, COUNT(DISTINCT split) s FROM training_rows WHERE split IS NOT NULL GROUP BY 1 HAVING s>1)""")
q("split NULL counts", """
SELECT 'outcomes' t, SUM(CASE WHEN split IS NULL THEN 1 ELSE 0 END) nulls, COUNT(*) n FROM outcomes
UNION ALL SELECT 'training_rows', SUM(CASE WHEN split IS NULL THEN 1 ELSE 0 END), COUNT(*) FROM training_rows""")
q("split distinct values", "SELECT split, COUNT(*) n FROM training_rows GROUP BY 1 ORDER BY 2 DESC")

q("split balance in model population", f"""
SELECT split, COUNT(*) rows, COUNT(DISTINCT token_address) coins FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
   AND feature_version={FV}
 GROUP BY 1 ORDER BY 2 DESC""")

q("training_rows.split disagrees with outcomes.split for same (kind,key)", """
SELECT COUNT(*) n FROM training_rows t JOIN outcomes o ON o.kind=t.kind AND o.key=t.key
 WHERE COALESCE(t.split,'~') <> COALESCE(o.split,'~')""")

print()
print("=== is_independent / is_live funnel (one filter at a time) ===")
steps = [
  ("all training_rows", "1=1"),
  ("kind='signal'", "kind='signal'"),
  ("+ is_live=1", "kind='signal' AND is_live=1"),
  ("+ asset_class='meme'", "kind='signal' AND is_live=1 AND asset_class='meme'"),
  ("+ status='ok'", "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'"),
  ("+ is_independent=1", "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1"),
  (f"+ feature_version={FV}", f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1 AND feature_version={FV}"),
]
prev = None
for label, w in steps:
    r = con.execute(f"SELECT COUNT(*) n, COUNT(DISTINCT token_address) c FROM training_rows WHERE {w}").fetchone()
    drop = "" if prev is None else f"  (-{prev-r['n']})"
    print(f"  {label:32s} rows={r['n']:>7} coins={r['c']:>5}{drop}")
    prev = r["n"]

q("is_independent value inventory (kind=signal)", "SELECT is_independent, COUNT(*) n FROM training_rows WHERE kind='signal' GROUP BY 1")
q("is_independent for kind=watch (schema says must be NULL)", "SELECT kind, is_independent, COUNT(*) n FROM training_rows WHERE kind<>'signal' GROUP BY 1,2")
q("outcomes is_independent for kind=watch (schema: NULL for watch)", "SELECT kind, is_independent, COUNT(*) n FROM outcomes GROUP BY 1,2 ORDER BY 1")
q("feature_version inventory", "SELECT feature_version, COUNT(*) n, MIN(built_at) mn, MAX(built_at) mx FROM training_rows GROUP BY 1 ORDER BY 1")

print()
print("=== python re-check: does stored split match assign_split(lower(addr)) ? ===")
def assign_split(a):
    d = hashlib.sha1(a.strip().lower().encode()).hexdigest()
    b = int(d[:8],16) % 10
    return "train" if b<=6 else ("val" if b==7 else "test")
bad = 0; tot = 0; badsample=[]
for r in con.execute("SELECT DISTINCT token_address, split FROM training_rows WHERE split IS NOT NULL"):
    tot += 1
    if assign_split(r["token_address"]) != r["split"]:
        bad += 1
        if len(badsample)<5: badsample.append((r["token_address"], r["split"], assign_split(r["token_address"])))
print(f"  distinct (addr,split) pairs checked: {tot}, mismatching assign_split: {bad}")
for s in badsample: print("   ", s)

con.close()
