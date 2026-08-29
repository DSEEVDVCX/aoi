import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
c = con.cursor()

print("=== meta current_feature_version ===")
for r in c.execute("SELECT key,value FROM meta WHERE key IN ('current_feature_version','live_start_ts')"):
    print(dict(r))

FV = "(SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER))"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

print("\n=== A1: population rows per UTC day, whole live history ===")
q = f"""
SELECT strftime('%Y-%m-%d', entry_ts, 'unixepoch') d,
       COUNT(*) n,
       COUNT(DISTINCT strftime('%H', entry_ts,'unixepoch')) hrs_present
  FROM training_rows
 WHERE {POP}
 GROUP BY 1 ORDER BY 1
"""
rows = list(c.execute(q))
for r in rows:
    print(f"{r['d']}  n={r['n']:5d}  hours_with_rows={r['hrs_present']:2d}  empty_hours={24-r['hrs_present']:2d}")
print("total days:", len(rows), "total rows:", sum(r['n'] for r in rows))

print("\n=== A2: raw signal_events per UTC day (no population filter) ===")
q2 = """
SELECT strftime('%Y-%m-%d', ts, 'unixepoch') d, COUNT(*) n,
       COUNT(DISTINCT strftime('%H', ts,'unixepoch')) hrs
  FROM signal_events
 WHERE ts >= strftime('%s','2026-08-10')
 GROUP BY 1 ORDER BY 1
"""
try:
    for r in c.execute(q2):
        print(f"{r['d']}  signals={r['n']:6d}  hours={r['hrs']:2d}")
except Exception as e:
    print("FAILED A2:", e)
    print("signal_events schema:")
    for r in c.execute("PRAGMA table_info(signal_events)"):
        print("  ", r['name'], r['type'])

con.close()
