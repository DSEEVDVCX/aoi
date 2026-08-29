import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def show(title, sql, args=()):
    try:
        rows = con.execute(sql, args).fetchall()
        print(f"--- {title}")
        for r in rows:
            print("   ", tuple(r))
    except Exception as e:
        print(f"--- {title} ERR {e}")

TABLES_TOKEN = [
    "watchlist", "watch_windows", "signal_events", "token_static", "token_class",
    "outcomes", "training_rows", "token_flow", "token_holders", "chain_concentration",
    "chain_authority", "evm_contract", "evm_balances", "token_social", "token_thesis",
    "bars_fetch_state", "token_bars",
]

print("===== typeof(network_id) per table =====")
for t in TABLES_TOKEN:
    show(t, f"SELECT typeof(network_id) tp, COUNT(*) c FROM \"{t}\" GROUP BY tp ORDER BY c DESC")

print()
print("===== typeof(token_address) per table =====")
for t in TABLES_TOKEN:
    show(t, f"SELECT typeof(token_address) tp, COUNT(*) c FROM \"{t}\" GROUP BY tp ORDER BY c DESC")

print()
print("===== network_id distinct values (small tables) =====")
for t in ["watchlist", "watch_windows", "token_static", "token_class", "outcomes", "training_rows", "signal_events"]:
    show(t, f"SELECT network_id, typeof(network_id) tp, COUNT(*) c FROM \"{t}\" GROUP BY network_id, tp ORDER BY c DESC LIMIT 20")

print()
print("===== mixed-case (non-lowercase) token_address counts =====")
for t in TABLES_TOKEN:
    show(t, f"""SELECT SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixed,
                       SUM(CASE WHEN token_address <> lower(token_address) AND token_address LIKE '0x%' THEN 1 ELSE 0 END) mixed_evm,
                       COUNT(*) total FROM "{t}" """)
