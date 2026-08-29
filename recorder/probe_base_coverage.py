"""Are Base (8453) signals reaching training_rows at all? Per-network coverage."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

print("-- signals with a CLOSED 48h window (ts <= max(training entry_ts)) vs training_rows, per network --")
q = """WITH cut AS (SELECT MAX(entry_ts) c FROM training_rows)
SELECT s.network_id,
       COUNT(*) signals_closed,
       (SELECT COUNT(*) FROM training_rows t WHERE t.kind='signal' AND t.network_id=s.network_id) tr_signal_rows
  FROM signal_events s, cut
 WHERE s.ts <= cut.c
 GROUP BY s.network_id ORDER BY signals_closed DESC"""
for r in con.execute(q):
    print("   ", dict(r))

print("\n-- Base signals by day vs Base training rows by day --")
q2 = """SELECT substr(recorded_at,1,10) day,
               COUNT(*) signals,
               (SELECT COUNT(*) FROM training_rows t
                 WHERE t.kind='signal' AND t.network_id='8453'
                   AND t.key IN (SELECT id FROM signal_events s2
                                  WHERE s2.network_id='8453'
                                    AND substr(s2.recorded_at,1,10)=substr(signal_events.recorded_at,1,10))) tr_rows
          FROM signal_events WHERE network_id='8453'
         GROUP BY 1 ORDER BY 1 DESC LIMIT 14"""
for r in con.execute(q2):
    print("   ", dict(r))

print("\n-- watch_windows: do Base signals get a window? --")
print("   watch_windows cols:", [x["name"] for x in con.execute("PRAGMA table_info(watch_windows)")])
