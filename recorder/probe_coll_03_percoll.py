import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

# collector table -> (timestamp column, has network_id, per-token)
COLL = [
    ("market_ticks", "recorded_at"),
    ("token_bars", "fetched_at"),
    ("token_static", "recorded_at"),
    ("token_holders", "recorded_at"),
    ("token_social", "recorded_at"),
    ("token_thesis", "fetched_at"),
    ("token_flow", "recorded_at"),
    ("chain_concentration", "recorded_at"),
    ("chain_authority", "recorded_at"),
    ("evm_contract", "recorded_at"),
    ("evm_balances", "updated_at"),
    ("activity_events", "recorded_at"),
]

print("=== per-collector: rows, distinct coins, min/max ts ===")
for t, ts in COLL:
    r = q(f"""SELECT COUNT(*) n,
                     COUNT(DISTINCT lower(token_address)||':'||network_id) coins,
                     MIN({ts}) mn, MAX({ts}) mx
              FROM "{t}" """)[0]
    print(f"{t:22s} rows={r['n']:>10,} coins={r['coins']:>6,} min={r['mn']} max={r['mx']}")

print("\n=== traders (no token) ===")
r = q("SELECT COUNT(*) n, COUNT(DISTINCT trader_id) k, MIN(recorded_at) mn, MAX(recorded_at) mx FROM traders")[0]
print(dict(zip(r.keys(), tuple(r))))
r = q("SELECT COUNT(*) n, COUNT(DISTINCT trader_id) k, MIN(last_fetch_at) mn, MAX(last_fetch_at) mx FROM traders_fetch_state")[0]
print("fetch_state:", dict(zip(r.keys(), tuple(r))))

print("\n=== snapshots (macro / raw feed) ===")
for r in q("SELECT source, COUNT(*) n, MIN(recorded_at) mn, MAX(recorded_at) mx FROM snapshots GROUP BY source ORDER BY n DESC"):
    print(dict(zip(r.keys(), tuple(r))))

print("\n=== token_bars by resolution ===")
for r in q("SELECT resolution, COUNT(*) n, COUNT(DISTINCT lower(token_address)||':'||network_id) coins, MIN(ts) mnts, MAX(ts) mxts, MIN(fetched_at) mnf, MAX(fetched_at) mxf FROM token_bars GROUP BY resolution ORDER BY n DESC"):
    print(dict(zip(r.keys(), tuple(r))))
