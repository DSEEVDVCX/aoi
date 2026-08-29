import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120); con.row_factory = sqlite3.Row
print("=== evm_backfill_state on 143 ===")
for r in con.execute(
    "SELECT * FROM evm_backfill_state WHERE network_id='143'"):
    d = dict(r)
    print("  " + " | ".join(f"{k}={d[k]}" for k in d))
print("\n=== backfill status per network ===")
for r in con.execute(
    "SELECT network_id AS net, status, COUNT(*) n FROM evm_backfill_state "
    "GROUP BY network_id, status ORDER BY net, n DESC"):
    print(f"  net {r['net']:>6}  {r['status']:>8}  {r['n']:>5}")
print("\n=== active Monad watch ===")
for r in con.execute(
    "SELECT token_address, first_seen_at, source, is_control, active "
    "FROM watchlist WHERE network_id='143' ORDER BY active DESC, first_seen_at"):
    print("  " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
print("\n=== EVM_LOG_RANGE_HINT / batch ===")
print("  range hint:", config.EVM_LOG_RANGE_HINT)
print("  addr batch:", config.EVM_ADDRESS_BATCH)
con.close()
