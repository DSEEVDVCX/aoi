import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

print("config.EVM_NETWORKS        =", config.EVM_NETWORKS)
print("config.EVM_REPLAY_NETWORKS =", getattr(config, "EVM_REPLAY_NETWORKS", None))
excluded = sorted({*(str(n) for n in config.EVM_NETWORKS),
                   *(str(n) for n in getattr(config, "EVM_REPLAY_NETWORKS", ()))})
print("=> networks currently EXCLUDED from pending_outcomes:", excluded)

print()
print("=== newest built training row per network (build is gated for EVM right now) ===")
for r in q("""SELECT network_id, COUNT(*) c, MAX(built_at) newest_built,
                     MAX(datetime(entry_ts,'unixepoch')) newest_t0
                FROM training_rows GROUP BY 1 ORDER BY c DESC"""):
    flag = "  <== EXCLUDED" if str(r["network_id"]) in excluded else ""
    print(f"   net={r['network_id']:<12} rows={r['c']:<7} newest built_at={str(r['newest_built'])[:19]}"
          f"  newest entry_ts={r['newest_t0']}{flag}")

print()
print("=== outcomes waiting with no training row at feature_version 12, by network ===")
for r in q("""SELECT COALESCE(o.network_id,'') net, COUNT(*) c,
                     MIN(datetime(o.entry_ts,'unixepoch')) oldest,
                     MAX(datetime(o.entry_ts,'unixepoch')) newest
                FROM outcomes o
               WHERE o.status IN ('ok','no_bars') AND o.kind='signal' AND o.is_independent=1
                 AND o.entry_ts >= ?
                 AND NOT EXISTS (SELECT 1 FROM training_rows r
                                  WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
               GROUP BY 1 ORDER BY c DESC""", (config.LIVE_START_TS,)):
    flag = "  <== EXCLUDED by the rebuild gate" if str(r["net"]) in excluded else ""
    print(f"   net={r['net']:<12} pending={r['c']:<7} {r['oldest']} .. {r['newest']}{flag}")

con.close()
