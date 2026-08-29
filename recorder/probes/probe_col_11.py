import os, sqlite3, config, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

print("=== token_thesis coverage ===")
r = con.execute("""SELECT COUNT(*) n, COUNT(DISTINCT token_address||'|'||network_id) coins,
    MIN(fetched_at) mnf, MAX(fetched_at) mxf, MIN(created_at) mnc, MAX(created_at) mxc
    FROM token_thesis""").fetchone()
print(dict(r))

for label, cond in (("active operational", "active=1 AND is_control=0"),
                    ("active control", "active=1 AND is_control=1"),
                    ("ever watched", "1=1")):
    tot = con.execute(f"SELECT COUNT(*) n FROM watchlist WHERE {cond}").fetchone()["n"]
    have = con.execute(f"""SELECT COUNT(*) n FROM watchlist w WHERE {cond}
        AND EXISTS(SELECT 1 FROM token_thesis x WHERE x.token_address=w.token_address
                   AND x.network_id=w.network_id)""").fetchone()["n"]
    print(f"  {label:20s} {have}/{tot} = {100.0*have/tot:.1f}%")

print()
print("=== token_thesis rows by day (last 12 distinct days of fetched_at) ===")
for r in con.execute("""SELECT substr(fetched_at,1,10) d, COUNT(*) n,
      COUNT(DISTINCT token_address) coins FROM token_thesis
      GROUP BY d ORDER BY d DESC LIMIT 12"""):
    print("  ", dict(r))

print()
print("=== token_social rows by day (last 8) — the aggregate that IS still running ===")
for r in con.execute("""SELECT substr(recorded_at,1,10) d, COUNT(*) n,
      COUNT(DISTINCT token_address) coins FROM token_social
      GROUP BY d ORDER BY d DESC LIMIT 8"""):
    print("  ", dict(r))
