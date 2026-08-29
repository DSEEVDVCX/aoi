import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

print("=== chain_concentration latest per source ===")
for r in con.execute("""SELECT network_id, is_replay, COUNT(*) n,
      COUNT(DISTINCT token_address) coins, MIN(recorded_at) mn, MAX(recorded_at) mx
      FROM chain_concentration GROUP BY network_id, is_replay ORDER BY network_id, is_replay"""):
    print(dict(r))

print()
print("=== chain_authority latest ===")
for r in con.execute("""SELECT network_id, COUNT(*) n, COUNT(DISTINCT token_address) coins,
      MIN(recorded_at) mn, MAX(recorded_at) mx FROM chain_authority GROUP BY network_id"""):
    print(dict(r))

print()
print("=== chain_fetch_state (fast Solana layer) ===")
for r in con.execute("""SELECT last_status, COUNT(*) n, MIN(last_fetch_at) mn, MAX(last_fetch_at) mx
      FROM chain_fetch_state GROUP BY last_status ORDER BY n DESC"""):
    print(dict(r))

print()
print("=== chain_auth_state ===")
for r in con.execute("""SELECT last_status, COUNT(*) n, MIN(last_fetch_at) mn, MAX(last_fetch_at) mx
      FROM chain_auth_state GROUP BY last_status ORDER BY n DESC"""):
    print(dict(r))

print()
print("=== evm_contract_state ===")
for r in con.execute("""SELECT network_id, last_status, COUNT(*) n, MAX(last_fetch_at) mx
      FROM evm_contract_state GROUP BY network_id, last_status ORDER BY network_id, n DESC"""):
    print(dict(r))

print()
print("=== social_fetch_state ===")
for r in con.execute("""SELECT last_status, COUNT(*) n, MIN(last_fetch_at) mn, MAX(last_fetch_at) mx,
      SUM(items) items FROM social_fetch_state GROUP BY last_status ORDER BY n DESC"""):
    print(dict(r))

print()
print("=== holders_fetch_state ===")
for r in con.execute("""SELECT last_status, COUNT(*) n, MAX(last_fetch_at) mx
      FROM holders_fetch_state GROUP BY last_status ORDER BY n DESC"""):
    print(dict(r))

print()
print("=== bars_fetch_state ===")
for r in con.execute("""SELECT last_status, COUNT(*) n, MAX(last_fetch_at) mx
      FROM bars_fetch_state GROUP BY last_status ORDER BY n DESC"""):
    print(dict(r))

print()
print("=== traders_fetch_state ===")
for r in con.execute("""SELECT last_status, COUNT(*) n, MAX(last_fetch_at) mx
      FROM traders_fetch_state GROUP BY last_status ORDER BY n DESC"""):
    print(dict(r))

print()
print("=== evm_block_cursor ===")
for r in con.execute("SELECT * FROM evm_block_cursor"):
    print(dict(r))

print()
print("=== evm_backfill_state by status ===")
for r in con.execute("""SELECT network_id, status, COUNT(*) n, MAX(last_try_at) mx
      FROM evm_backfill_state GROUP BY network_id, status ORDER BY network_id, n DESC"""):
    print(dict(r))

print()
print("=== evm_replay_state by status ===")
for r in con.execute("""SELECT network_id, status, COUNT(*) n, MAX(last_try_at) mx
      FROM evm_replay_state GROUP BY network_id, status ORDER BY network_id, n DESC"""):
    print(dict(r))
