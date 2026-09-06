import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("config.EVM_NETWORKS       =", config.EVM_NETWORKS)
print("config.EVM_REPLAY_NETWORKS=", config.EVM_REPLAY_NETWORKS)
print("config.EVM_CONTRACT_NETWORKS=", config.EVM_CONTRACT_NETWORKS)
print()
print("--- meta flags that gate the builder ---")
for k in ("evm_ledger_rebuild_required", "evm_training_rebuild_started",
          "current_feature_version"):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    print(f"  {k} = {r['value'] if r else '<absent>'}")
print()
print("--- all evm_* meta keys ---")
for r in con.execute("SELECT key, substr(value,1,80) v FROM meta WHERE key LIKE 'evm%' ORDER BY key"):
    print("  ", dict(r))
print()
print("--- pending EVM outcomes the gate is holding back ---")
sql = ("SELECT network_id, COUNT(*) held FROM outcomes o "
       "WHERE o.status IN ('ok','no_bars') AND NOT EXISTS "
       "(SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key "
       "AND r.feature_version=12) GROUP BY 1 ORDER BY 2 DESC")
print("SQL:", sql)
for r in con.execute(sql):
    print("  ", dict(r))
print()
print("--- of the held Base outcomes, how many have entry_ts >= collector start")
sql2 = ("SELECT COUNT(*) eligible_now FROM outcomes o WHERE o.network_id='8453' "
        "AND o.status IN ('ok','no_bars') AND o.entry_ts >= "
        "(SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) FROM evm_contract) "
        "AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind "
        "AND r.key=o.key AND r.feature_version=12)")
print("SQL:", sql2)
print("  ", dict(con.execute(sql2).fetchone()))
print()
print("--- would those rows actually find a snapshot at/before t0? (exact features.py predicate)")
sql3 = ("SELECT COUNT(*) would_fill FROM outcomes o WHERE o.network_id='8453' "
        "AND o.status IN ('ok','no_bars') AND EXISTS (SELECT 1 FROM evm_contract c "
        "WHERE c.token_address=o.token_address AND c.network_id=o.network_id "
        "AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= o.entry_ts)")
print("SQL:", sql3)
print("  ", dict(con.execute(sql3).fetchone()))
con.close()
