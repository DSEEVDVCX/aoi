import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

tabs = ["watchlist","market_ticks","token_bars","token_static","token_holders","token_social",
        "token_thesis","token_flow","chain_concentration","chain_authority","evm_contract",
        "evm_balances","traders","activity_events","snapshots","token_class","watch_windows",
        "signal_events","evm_block_time","evm_block_cursor",
        "bars_fetch_state","holders_fetch_state","social_fetch_state","chain_fetch_state",
        "traders_fetch_state","chain_auth_state","evm_contract_state","evm_backfill_state",
        "evm_replay_state","activity_bars_state","historical_bars_state"]

for t in tabs:
    try:
        cols = con.execute(f"PRAGMA table_info(\"{t}\")").fetchall()
        print(f"\n=== {t} ({len(cols)} cols)")
        print("   " + ", ".join(f"{c['name']}:{c['type']}" for c in cols))
    except Exception as e:
        print(f"\n=== {t} ERR {e}")
