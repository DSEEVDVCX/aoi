import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

print("== case profile per table (EVM-looking 0x addresses) ==")
for t in ("signal_events", "outcomes", "training_rows", "watchlist",
          "chain_concentration", "chain_authority", "evm_contract",
          "token_flow", "token_holders", "market_ticks", "token_static"):
    try:
        r = q(f"""SELECT COUNT(*) n,
                         SUM(CASE WHEN token_address = lower(token_address) THEN 1 ELSE 0 END) low,
                         SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixed
                    FROM {t} WHERE token_address LIKE '0x%'""")[0]
        print(f"{t:24s} n={r['n']:>8} lower={r['low']:>8} mixed={r['mixed']:>8}")
    except Exception as e:
        print(f"{t:24s} ERR {e}")

print()
print("== distinct token_address case forms overlapping? ==")
r = q("""SELECT COUNT(*) FROM (
           SELECT DISTINCT token_address a FROM signal_events WHERE token_address LIKE '0x%')
         WHERE a <> lower(a)""")
print("signal_events distinct mixed-case addrs:", r[0][0])

print()
print("== network_id storage type check ==")
for t in ("signal_events", "outcomes", "training_rows", "chain_concentration",
          "watchlist", "token_static", "token_flow", "token_holders", "evm_contract"):
    try:
        r = q(f"SELECT typeof(network_id) t, COUNT(*) n FROM {t} GROUP BY 1 ORDER BY n DESC")
        print(t, [(x['t'], x['n']) for x in r])
    except Exception as e:
        print(t, "ERR", e)
con.close()
