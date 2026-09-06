import os, sqlite3, config, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

print("=== watchlist totals ===")
for r in con.execute("""
  SELECT active, is_control, COUNT(*) n,
         COUNT(DISTINCT token_address||'|'||network_id) coins,
         MIN(first_seen_at) mn, MAX(first_seen_at) mx
  FROM watchlist GROUP BY active, is_control ORDER BY active DESC, is_control
"""):
    print(dict(r))

print()
print("=== ACTIVE operational (active=1, is_control=0) by network ===")
for r in con.execute("""
  SELECT network_id, COUNT(*) n FROM watchlist
  WHERE active=1 AND is_control=0 GROUP BY network_id ORDER BY n DESC
"""):
    print(dict(r))

print()
print("=== ACTIVE control (active=1, is_control=1) by network ===")
for r in con.execute("""
  SELECT network_id, COUNT(*) n FROM watchlist
  WHERE active=1 AND is_control=1 GROUP BY network_id ORDER BY n DESC
"""):
    print(dict(r))

print()
print("=== ever watched by network (all rows) ===")
for r in con.execute("""
  SELECT network_id, COUNT(*) n, SUM(active) act FROM watchlist GROUP BY network_id ORDER BY n DESC
"""):
    print(dict(r))

print()
print("=== active rows whose watch_until already passed (should have been deactivated) ===")
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
r = con.execute("SELECT COUNT(*) n FROM watchlist WHERE active=1 AND watch_until < ?", (now,)).fetchone()
print("expired-but-active:", r["n"], " now=", now)
for r in con.execute("SELECT token_address, network_id, watch_until, is_control FROM watchlist WHERE active=1 AND watch_until < ? ORDER BY watch_until LIMIT 10", (now,)):
    print("  ", dict(r))
