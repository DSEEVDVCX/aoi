import os, sys, io, sqlite3, config
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== THEIR query 1 verbatim ===")
r = con.execute("""SELECT COUNT(*) total, SUM(token_created_at IS NULL) no_created_at,
 SUM(launchpad_name IS NULL OR launchpad_name='') no_launchpad, SUM(creator_address IS NULL) no_creator,
 SUM(is_scam IS NULL) no_is_scam FROM token_static""").fetchone()
print("  " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
print("  (they reported 1203 / 12 / 463 / 931 / 1192)")
print()

print("=== THEIR query 2 verbatim ===")
rows = con.execute("""SELECT s.token_address, s.network_id, s.symbol, s.token_created_at,
 MIN(w.first_seen_at) fw,
 ROUND((CAST(s.token_created_at AS INTEGER) - CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER))/3600.0,2) hours_after
 FROM watch_windows w JOIN token_static s ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE s.token_created_at IS NOT NULL GROUP BY 1,2
 HAVING CAST(s.token_created_at AS INTEGER) > CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)""").fetchall()
print("  rows:", len(rows))
for x in rows:
    print(f"    {x['symbol']}/net{x['network_id']} created={x['token_created_at']} fw={x['fw']} hours_after={x['hours_after']}")
con.close()
