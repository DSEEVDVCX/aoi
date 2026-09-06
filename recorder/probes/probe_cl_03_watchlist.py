import os, sqlite3, sys, time, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    t0 = time.time()
    rows = con.execute(sql, args).fetchall()
    return rows, time.time() - t0

print("== NOW ==")
r, _ = q("SELECT datetime('now') AS n")
print("db datetime('now') =", r[0]["n"])

print("\n== WATCHLIST ==")
r, dt = q("""SELECT active, is_control, COUNT(*) c,
                    COUNT(DISTINCT token_address||'|'||network_id) tk
             FROM watchlist GROUP BY active, is_control ORDER BY active, is_control""")
for x in r:
    print(f"active={x['active']} is_control={x['is_control']}  rows={x['c']} tokens={x['tk']}")
print(f"({dt:.2f}s)")

print("\n== ACTIVE OPERATIONAL WATCHLIST (active=1, is_control=0) ==")
r, dt = q("""SELECT COUNT(*) c, MIN(first_seen_at) mn, MAX(first_seen_at) mx,
                    MIN(watch_until) wmn, MAX(watch_until) wmx
             FROM watchlist WHERE active=1 AND is_control=0""")
x = r[0]
print(dict(x), f"({dt:.2f}s)")

print("\n-- active watches by network")
r, dt = q("""SELECT network_id, COUNT(*) c FROM watchlist
             WHERE active=1 AND is_control=0 GROUP BY network_id ORDER BY c DESC""")
for x in r:
    print(f"  net={x['network_id']:>12}  {x['c']}")
print(f"({dt:.2f}s)")

print("\n-- active watches whose watch_until already passed (should the sweeper have closed them?)")
r, dt = q("""SELECT COUNT(*) c FROM watchlist
             WHERE active=1 AND is_control=0 AND watch_until < datetime('now')""")
print(dict(r[0]), f"({dt:.2f}s)")

print("\n-- source breakdown of active watches")
r, dt = q("""SELECT source, COUNT(*) c FROM watchlist WHERE active=1 AND is_control=0
             GROUP BY source ORDER BY c DESC""")
for x in r: print(f"  {x['source']}: {x['c']}")

print("\n== EVER WATCHED (all watchlist rows, non-control) ==")
r, dt = q("SELECT COUNT(*) c FROM watchlist WHERE is_control=0")
print(dict(r[0]))
r, dt = q("SELECT COUNT(*) c FROM watchlist")
print("all incl control:", dict(r[0]))

print("\n== watch_windows ==")
r, dt = q("""SELECT is_control, COUNT(*) c, COUNT(DISTINCT token_address||'|'||network_id) tk,
                    MIN(first_seen_at) mn, MAX(first_seen_at) mx
             FROM watch_windows GROUP BY is_control""")
for x in r: print(dict(x))
print(f"({dt:.2f}s)")
