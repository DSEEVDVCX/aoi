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

print("############ token_bars EVM casing split ############")
show("bars mixed-case evm rows/tokens", """
SELECT COUNT(*) rows_, COUNT(DISTINCT token_address) toks, COUNT(DISTINCT network_id) nets
  FROM token_bars WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
""")
show("bars mixed-case evm by network", """
SELECT network_id, COUNT(*) rows_, COUNT(DISTINCT token_address) toks
  FROM token_bars WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
 GROUP BY network_id
""")
show("bars: tokens present under BOTH casings (same lower(addr),net)", """
SELECT COUNT(*) FROM (
  SELECT lower(token_address) la, network_id
    FROM token_bars WHERE token_address LIKE '0x%'
   GROUP BY la, network_id
  HAVING COUNT(DISTINCT token_address) > 1
)
""")
show("bars: the both-casing tokens detail", """
SELECT lower(token_address) la, network_id,
       COUNT(DISTINCT token_address) casings, COUNT(*) rows_,
       GROUP_CONCAT(DISTINCT resolution) res
  FROM token_bars WHERE token_address LIKE '0x%'
 GROUP BY la, network_id
HAVING COUNT(DISTINCT token_address) > 1
 LIMIT 30
""")
show("bars mixed-case evm sample addrs + fetched_at range", """
SELECT token_address, network_id, resolution, COUNT(*) c,
       MIN(fetched_at), MAX(fetched_at), MIN(ts), MAX(ts)
  FROM token_bars WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)
 GROUP BY token_address, network_id, resolution LIMIT 30
""")

print()
print("############ do those mixed-case bar tokens join to watchlist? ############")
show("mixed-case bar token in watchlist exact", """
SELECT COUNT(*) FROM (
  SELECT DISTINCT token_address, network_id FROM token_bars
   WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)) b
 WHERE EXISTS (SELECT 1 FROM watchlist l WHERE l.token_address=b.token_address AND l.network_id=b.network_id)
""")
show("mixed-case bar token in watchlist lowered", """
SELECT COUNT(*) FROM (
  SELECT DISTINCT token_address, network_id FROM token_bars
   WHERE token_address LIKE '0x%' AND token_address <> lower(token_address)) b
 WHERE EXISTS (SELECT 1 FROM watchlist l WHERE lower(l.token_address)=lower(b.token_address) AND l.network_id=b.network_id)
""")

print()
print("############ token_class gap ############")
show("tr distinct tokens", "SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM training_rows)")
show("tr distinct tokens with no class row", """
SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM training_rows) t
 WHERE NOT EXISTS (SELECT 1 FROM token_class c WHERE c.token_address=t.token_address AND c.network_id=t.network_id)
""")
show("tr rows no class, by kind and asset_class", """
SELECT t.kind, t.asset_class, COUNT(*) c FROM training_rows t
 WHERE NOT EXISTS (SELECT 1 FROM token_class c WHERE c.token_address=t.token_address AND c.network_id=t.network_id)
 GROUP BY t.kind, t.asset_class ORDER BY c DESC
""")
show("tr rows no class, min/max built_at", """
SELECT MIN(built_at), MAX(built_at) FROM training_rows t
 WHERE NOT EXISTS (SELECT 1 FROM token_class c WHERE c.token_address=t.token_address AND c.network_id=t.network_id)
   AND t.kind='signal'
""")
show("tr signal rows no class in MODEL POPULATION", """
SELECT COUNT(*) FROM training_rows t
 WHERE t.kind='signal' AND t.is_live=1 AND t.asset_class='meme' AND t.status='ok' AND t.is_independent=1
   AND t.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
   AND NOT EXISTS (SELECT 1 FROM token_class c WHERE c.token_address=t.token_address AND c.network_id=t.network_id)
""")
show("token_class classified_at range", "SELECT MIN(classified_at), MAX(classified_at), COUNT(*) FROM token_class")
show("watchlist tokens with no class row", """
SELECT COUNT(*) FROM watchlist l
 WHERE NOT EXISTS (SELECT 1 FROM token_class c WHERE c.token_address=l.token_address AND c.network_id=l.network_id)
""")
