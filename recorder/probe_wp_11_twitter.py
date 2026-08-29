import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")
r = con.execute(f"""SELECT COUNT(*) tot,
        SUM(CASE WHEN has_twitter=0 THEN 1 ELSE 0 END) t0,
        SUM(CASE WHEN has_twitter=1 THEN 1 ELSE 0 END) t1,
        SUM(CASE WHEN has_twitter IS NULL THEN 1 ELSE 0 END) tN,
        SUM(CASE WHEN socials_count=0 THEN 1 ELSE 0 END) s0,
        SUM(CASE WHEN socials_count IS NULL THEN 1 ELSE 0 END) sN,
        SUM(CASE WHEN log_size_usd IS NULL THEN 1 ELSE 0 END) lsN
        FROM training_rows WHERE {POP}""").fetchone()
print(f"model rows={r['tot']}")
print(f"  has_twitter: 0 -> {r['t0']}  1 -> {r['t1']}  NULL -> {r['tN']}")
print(f"  socials_count: 0 -> {r['s0']}  NULL -> {r['sN']}")
print(f"  log_size_usd NULL -> {r['lsN']}")
con.close()
