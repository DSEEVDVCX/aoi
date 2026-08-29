import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120); con.row_factory = sqlite3.Row
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")
r = con.execute(f"""SELECT
   SUM(CASE WHEN size_usd<0 THEN 1 ELSE 0 END) neg,
   SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) zero,
   SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) nul,
   MIN(size_usd) mn
   FROM training_rows WHERE {POP}""").fetchone()
print("size_usd < 0 ->", r["neg"], " = 0 ->", r["zero"], " NULL ->", r["nul"], " min =", r["mn"])
con.close()
