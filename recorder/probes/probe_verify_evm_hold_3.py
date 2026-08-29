import os, sqlite3, sys, json, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("--- signal_events schema ---")
print([c[1] for c in con.execute("PRAGMA table_info(signal_events)")])

print("\n--- signal_events per network per day (last 8 days) ---")
q = """SELECT network_id AS net, substr(ts,1,10) AS d, COUNT(*) n
         FROM signal_events
        WHERE ts >= '2026-08-15'
        GROUP BY 1,2 ORDER BY 2,1"""
try:
    for r in con.execute(q):
        print(f"  {r['d']}  net={str(r['net']):>12}  {r['n']:>6}")
except Exception as e:
    print("  FAILED:", e)

print("\n--- outcomes(ok/no_bars, kind=signal) per network per day (entry_ts, last 8 days) ---")
for r in con.execute("""SELECT network_id net, date(entry_ts,'unixepoch') d, COUNT(*) n
                          FROM outcomes WHERE kind='signal' AND status IN ('ok','no_bars')
                            AND entry_ts >= strftime('%s','2026-08-15')
                          GROUP BY 1,2 ORDER BY 2,1"""):
    print(f"  {r['d']}  net={str(r['net']):>12}  {r['n']:>6}")

print("\n--- repair cohort ---")
raw = con.execute("SELECT value FROM meta WHERE key='evm_repair_cohort'").fetchone()[0]
c = json.loads(raw)
print("  captured_at =", c["captured_at"], " networks =", c["networks"])
print("  active_backfills =", len(c["active_backfills"]), " replay_windows =", len(c["replay_windows"]))

print("\n--- how many of the held fv=8 EVM rows would be model-population eligible? ---")
r = con.execute("""SELECT COUNT(*) FROM training_rows
                    WHERE feature_version=8 AND kind='signal' AND is_live=1
                      AND asset_class='meme' AND status='ok' AND is_independent=1""").fetchone()[0]
print("  fv=8 rows passing all model filters:", r)
r2 = con.execute("SELECT COUNT(*) FROM training_rows WHERE feature_version=8").fetchone()[0]
print("  fv=8 rows total:", r2)

print("\n--- kind split of the 31.6k held outcomes on excluded EVM nets ---")
for r in con.execute("""SELECT kind, COUNT(*) n FROM outcomes o
                         WHERE o.status IN ('ok','no_bars') AND o.network_id IN ('143','4663','8453')
                           AND NOT EXISTS (SELECT 1 FROM training_rows r
                                WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
                         GROUP BY 1 ORDER BY n DESC"""):
    print(f"  kind={r['kind']:<9} {r['n']:>7}")

print("\n--- of the held kind=signal EVM outcomes, how many are model-eligible by outcome-side flags? ---")
for r in con.execute("""SELECT o.network_id net, COUNT(*) n
                          FROM outcomes o
                         WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
                           AND o.entry_ts >= ? AND o.network_id IN ('143','4663','8453')
                           AND NOT EXISTS (SELECT 1 FROM training_rows r
                                WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
                         GROUP BY 1 ORDER BY n DESC""", (config.LIVE_START_TS,)):
    print(f"  net={str(r['net']):>12} {r['n']:>7}")
con.close()
