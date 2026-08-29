import os, sqlite3, config, datetime as dt

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

print("now utc:", dt.datetime.now(dt.timezone.utc).isoformat())

print("\n=== watchlist breakdown ===")
for r in q("SELECT active, is_control, COUNT(*) n, COUNT(DISTINCT token_address||':'||network_id) k FROM watchlist GROUP BY active, is_control ORDER BY active DESC, is_control"):
    print(dict(r))

print("\n=== watchlist PK / dupes ===")
print(q("SELECT COUNT(*) tot, COUNT(DISTINCT token_address||':'||network_id) uniq, COUNT(DISTINCT lower(token_address)||':'||network_id) uniq_lower FROM watchlist")[0])

print("\n=== active operational watches per network ===")
for r in q("SELECT network_id, COUNT(*) n FROM watchlist WHERE active=1 AND is_control=0 GROUP BY network_id ORDER BY n DESC"):
    print(dict(r))

print("\n=== active control watches per network ===")
for r in q("SELECT network_id, COUNT(*) n FROM watchlist WHERE active=1 AND is_control=1 GROUP BY network_id ORDER BY n DESC"):
    print(dict(r))

print("\n=== watch_until vs now for active=1 ===")
for r in q("""SELECT is_control,
       SUM(CASE WHEN watch_until > strftime('%Y-%m-%dT%H:%M:%SZ','now') THEN 1 ELSE 0 END) future,
       SUM(CASE WHEN watch_until <= strftime('%Y-%m-%dT%H:%M:%SZ','now') THEN 1 ELSE 0 END) expired,
       SUM(CASE WHEN watch_until IS NULL THEN 1 ELSE 0 END) nullw,
       COUNT(*) n
       FROM watchlist WHERE active=1 GROUP BY is_control"""):
    print(dict(r))

print("\n=== case profile of token_address across tables ===")
for t in ["watchlist","market_ticks","token_holders","chain_concentration","evm_contract","token_flow","token_static","token_bars"]:
    r = q(f"SELECT COUNT(*) n, SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixed FROM \"{t}\"")[0]
    print(f"{t:22s} rows={r['n']:>10,} mixedcase={r['mixed']:>10,}")
