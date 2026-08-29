import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, cap=40):
    t = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:cap]: print("   ", dict(r))
    if len(rows) > cap: print(f"    ... {len(rows)} rows")

print("LIVE_START_TS =", getattr(config, "LIVE_START_TS", None))
print("MIN_TOKEN_AGE_DAYS =", getattr(config, "MIN_TOKEN_AGE_DAYS", None))
print("EVM_NETWORKS =", getattr(config, "EVM_NETWORKS", None))
print("EVM_REPLAY_NETWORKS =", getattr(config, "EVM_REPLAY_NETWORKS", None))

q("meta evm rebuild flags", """
SELECT key, CAST(value AS TEXT) v FROM meta
 WHERE key LIKE '%evm%rebuild%' OR key LIKE '%ledger%' ORDER BY key
""")

q("network breakdown: signal outcomes ok WITHOUT any training row", """
SELECT COALESCE(o.network_id,'<NULL>') net, COUNT(*) missing
  FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok'
   AND NOT EXISTS (SELECT 1 FROM training_rows tr WHERE tr.kind='signal' AND tr.key=o.key)
 GROUP BY net ORDER BY missing DESC
""")

q("network breakdown: training_rows by feature_version (signal)", """
SELECT COALESCE(network_id,'<NULL>') net, feature_version, COUNT(*) n
  FROM training_rows WHERE kind='signal'
 GROUP BY net, feature_version ORDER BY net, feature_version
""")

q("network breakdown: ALL signal outcomes ok (denominator)", """
SELECT COALESCE(network_id,'<NULL>') net, COUNT(*) total
  FROM outcomes WHERE kind='signal' AND status='ok' GROUP BY net ORDER BY total DESC
""")

# ---------- NORMALIZATION ----------
print("\n\n========== NORMALIZATION ==========")
for tbl in ("signal_events","watchlist","watch_windows","token_static","outcomes","training_rows"):
    q(f"{tbl}: typeof(network_id) distribution", f"""
    SELECT typeof(network_id) ty, COUNT(*) n,
           COUNT(DISTINCT CAST(network_id AS TEXT)) distinct_vals
      FROM {tbl} GROUP BY ty ORDER BY n DESC""")

for tbl in ("signal_events","watchlist","watch_windows","token_static","outcomes","training_rows"):
    q(f"{tbl}: addresses differing only by case", f"""
    SELECT COUNT(*) coins_with_2plus_casings FROM (
      SELECT lower(token_address) la
        FROM {tbl} GROUP BY la HAVING COUNT(DISTINCT token_address) > 1
    )""")

q("mixed-case (non-lowercase) EVM addresses per table", """
SELECT 'signal_events' t, COUNT(*) rows_notlower, COUNT(DISTINCT token_address) toks
  FROM signal_events WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
UNION ALL SELECT 'watchlist', COUNT(*), COUNT(DISTINCT token_address) FROM watchlist
  WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
UNION ALL SELECT 'watch_windows', COUNT(*), COUNT(DISTINCT token_address) FROM watch_windows
  WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
UNION ALL SELECT 'token_static', COUNT(*), COUNT(DISTINCT token_address) FROM token_static
  WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
UNION ALL SELECT 'outcomes', COUNT(*), COUNT(DISTINCT token_address) FROM outcomes
  WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
UNION ALL SELECT 'training_rows', COUNT(*), COUNT(DISTINCT token_address) FROM training_rows
  WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
""")

q("network_id distinct values across tables (spot empty-string vs NULL)", """
SELECT 'signal_events' t, COALESCE(network_id,'<NULL>') net, COUNT(*) n FROM signal_events GROUP BY net
UNION ALL SELECT 'watch_windows', COALESCE(network_id,'<NULL>'), COUNT(*) FROM watch_windows GROUP BY 2
UNION ALL SELECT 'token_static', COALESCE(network_id,'<NULL>'), COUNT(*) FROM token_static GROUP BY 2
UNION ALL SELECT 'outcomes', COALESCE(network_id,'<NULL>'), COUNT(*) FROM outcomes GROUP BY 2
UNION ALL SELECT 'training_rows', COALESCE(network_id,'<NULL>'), COUNT(*) FROM training_rows GROUP BY 2
ORDER BY 1,3 DESC
""", cap=80)

q("same token_address under MORE THAN ONE network_id (any table)", """
SELECT 'signal_events' t, COUNT(*) toks FROM (
  SELECT token_address FROM signal_events GROUP BY token_address
   HAVING COUNT(DISTINCT COALESCE(network_id,'~'))>1)
UNION ALL SELECT 'outcomes', COUNT(*) FROM (
  SELECT token_address FROM outcomes GROUP BY token_address
   HAVING COUNT(DISTINCT COALESCE(network_id,'~'))>1)
UNION ALL SELECT 'training_rows', COUNT(*) FROM (
  SELECT token_address FROM training_rows GROUP BY token_address
   HAVING COUNT(DISTINCT COALESCE(network_id,'~'))>1)
UNION ALL SELECT 'token_static', COUNT(*) FROM (
  SELECT token_address FROM token_static GROUP BY token_address
   HAVING COUNT(DISTINCT COALESCE(network_id,'~'))>1)
""")

con.close()
