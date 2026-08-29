"""Adversarial verification, step 1: meta state + table sizes. READ-ONLY."""
import os, sqlite3, json, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("== meta keys of interest ==")
rows = con.execute(
    "SELECT key, value FROM meta WHERE key LIKE '%evm%' OR key LIKE '%feature_version%' "
    "OR key LIKE '%training%' ORDER BY key"
).fetchall()
for r in rows:
    v = r["value"]
    if v is not None and len(str(v)) > 200:
        v = str(v)[:200] + f"...(len={len(str(v))})"
    print(f"  {r['key']:<44} = {v}")

print("\n== sizes ==")
for t in ("outcomes", "training_rows", "signal_events"):
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t:<16} {n:>9,}")

print("\n== training_rows: feature_version x is_live ==")
for r in con.execute(
    "SELECT feature_version fv, is_live, COUNT(*) n, "
    " date(MIN(entry_ts),'unixepoch') d0, date(MAX(entry_ts),'unixepoch') d1 "
    "FROM training_rows GROUP BY 1,2 ORDER BY 1,2"
):
    print(f"  fv={r['fv']:<4} is_live={r['is_live']}  n={r['n']:>8,}  {r['d0']} .. {r['d1']}")
con.close()
