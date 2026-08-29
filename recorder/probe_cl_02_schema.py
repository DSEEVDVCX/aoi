import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

TABLES = ["watchlist","market_ticks","token_bars","token_static","token_holders",
          "token_social","token_thesis","token_flow","chain_concentration",
          "chain_authority","evm_contract","evm_balances","evm_block_time",
          "traders","activity_events","signal_events","snapshots","watch_windows",
          "token_class","outcomes",
          "bars_fetch_state","chain_fetch_state","chain_auth_state","holders_fetch_state",
          "social_fetch_state","traders_fetch_state","activity_bars_state",
          "historical_bars_state","evm_contract_state","evm_replay_state",
          "evm_backfill_state","evm_block_cursor"]

for t in TABLES:
    cols = con.execute(f"PRAGMA table_info({t})").fetchall()
    print(f"--- {t} ({len(cols)} cols)")
    print("    " + ", ".join(f"{c['name']}:{c['type']}" + ("*PK" if c["pk"] else "") for c in cols))
    idx = con.execute(f"PRAGMA index_list({t})").fetchall()
    for i in idx:
        ic = con.execute(f"PRAGMA index_info({i['name']})").fetchall()
        print(f"    IDX {i['name']} unique={i['unique']} on {[x['name'] for x in ic]}")
    print()
