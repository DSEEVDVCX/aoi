import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")
    return rows

TS = [
    ("watchlist","first_seen_at"),("watchlist","watch_until"),
    ("watch_windows","first_seen_at"),("watch_windows","watch_until"),
    ("signal_events","recorded_at"),("signal_events","ts"),
    ("token_static","recorded_at"),("token_static","token_created_at"),
    ("outcomes","labeled_at"),
    ("training_rows","built_at"),
    ("bars_fetch_state","last_fetch_at"),
]
print("=== timezone suffix mix per timestamp column ===")
for t,c in TS:
    sql = f"""
    SELECT CASE
             WHEN {c} IS NULL THEN 'null'
             WHEN {c} LIKE '%+00:00' THEN 'aware_+00:00'
             WHEN {c} LIKE '%Z' THEN 'aware_Z'
             WHEN {c} GLOB '*[+-][0-9][0-9]:[0-9][0-9]' THEN 'aware_other_offset'
             WHEN typeof({c})<>'text' THEN 'nontext_'||typeof({c})
             ELSE 'naive'
           END form, COUNT(*) n, MIN({c}) mn, MAX({c}) mx
      FROM {t} GROUP BY 1 ORDER BY 2 DESC"""
    rows = q(f"{t}.{c}", sql)

print()
print("=== type affinity of network_id ===")
for t in ["watchlist","watch_windows","signal_events","token_static","outcomes","training_rows","market_ticks"]:
    q(f"typeof(network_id) {t}", f"SELECT typeof(network_id) ty, COUNT(*) n, COUNT(DISTINCT network_id) d FROM {t} GROUP BY 1 ORDER BY 2 DESC")

print()
print("=== network_id value inventory ===")
for t in ["watchlist","watch_windows","signal_events","token_static","outcomes","training_rows"]:
    q(f"network_id values {t}", f"SELECT COALESCE(CAST(network_id AS TEXT),'<NULL>') nid, typeof(network_id) ty, COUNT(*) n FROM {t} GROUP BY 1,2 ORDER BY 3 DESC")

con.close()
