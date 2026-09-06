import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

print("config.EVM_NETWORKS       =", getattr(config, "EVM_NETWORKS", "MISSING"))
print("config.EVM_REPLAY_NETWORKS=", getattr(config, "EVM_REPLAY_NETWORKS", "MISSING"))
print("config.MIN_TOKEN_AGE_DAYS =", getattr(config, "MIN_TOKEN_AGE_DAYS", "MISSING"))
print("config.LIVE_START_TS      =", getattr(config, "LIVE_START_TS", "MISSING"))

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

print("\n=== meta keys mentioning evm / rebuild / feature ===")
for r in con.execute("""SELECT key, substr(value,1,90) v FROM meta
                         WHERE key LIKE '%evm%' OR key LIKE '%rebuild%'
                            OR key LIKE '%feature%' OR key LIKE '%replay%'
                         ORDER BY key"""):
    print("  %-46s = %s" % (r["key"], r["v"]))

print("\n=== outcomes: rows per network/day that COULD build (status ok/no_bars) ===")
for r in con.execute("""
    SELECT network_id, COUNT(*) n,
           date(MIN(entry_ts),'unixepoch') a, date(MAX(entry_ts),'unixepoch') b
      FROM outcomes WHERE kind='signal' AND status IN ('ok','no_bars')
     GROUP BY 1 ORDER BY n DESC"""):
    print("  ", dict(r))

print("\n=== outcomes(signal, ok/no_bars) WITHOUT a fv12 training row, by network ===")
for r in con.execute("""
    SELECT o.network_id, COUNT(*) n,
           date(MIN(o.entry_ts),'unixepoch') a, date(MAX(o.entry_ts),'unixepoch') b
      FROM outcomes o
     WHERE o.kind='signal' AND o.status IN ('ok','no_bars')
       AND NOT EXISTS (SELECT 1 FROM training_rows r
                        WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
     GROUP BY 1 ORDER BY n DESC"""):
    print("  ", dict(r))

print("\n=== of those, is_independent=1 (i.e. would be model candidates) ===")
for r in con.execute("""
    SELECT o.network_id, COUNT(*) n,
           date(MIN(o.entry_ts),'unixepoch') a, date(MAX(o.entry_ts),'unixepoch') b
      FROM outcomes o
     WHERE o.kind='signal' AND o.status IN ('ok','no_bars') AND o.is_independent=1
       AND NOT EXISTS (SELECT 1 FROM training_rows r
                        WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
     GROUP BY 1 ORDER BY n DESC"""):
    print("  ", dict(r))
con.close()
