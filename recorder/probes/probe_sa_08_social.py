import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== token_social: extract.py:767 accumulators (likes=replies=0) ===")
x = con.execute("""SELECT COUNT(*) n,
   SUM(CASE WHEN thesis_sampled=0 THEN 1 ELSE 0 END) sampled0,
   SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 THEN 1 ELSE 0 END) sampled0_total_pos,
   SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 AND thesis_likes=0 THEN 1 ELSE 0 END) likes_fab,
   SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 AND thesis_replies=0 THEN 1 ELSE 0 END) repl_fab,
   SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 AND thesis_authors=0 THEN 1 ELSE 0 END) auth_fab,
   SUM(CASE WHEN thesis_likes IS NULL THEN 1 ELSE 0 END) likes_null,
   MAX(CASE WHEN thesis_sampled=0 THEN thesis_total END) worst_total
  FROM token_social""").fetchone()
print("  ", dict(x))

print("\n=== token_social: thesis_sampled>0 but likes=0 (capped-page sample) ===")
x = con.execute("""SELECT COUNT(*) n,
   SUM(CASE WHEN thesis_sampled>0 AND thesis_likes=0 THEN 1 ELSE 0 END) s_pos_likes0,
   SUM(CASE WHEN has_next_page=1 THEN 1 ELSE 0 END) hasnext
  FROM token_social""").fetchone()
print("  ", dict(x))

print("\n=== creator_prior_tokens coverage ceiling ===")
print("  token_static rows (our whole creator universe):",
      con.execute("SELECT COUNT(*) c FROM token_static").fetchone()["c"])
print("  distinct creator_address:",
      con.execute("SELECT COUNT(DISTINCT creator_address) c FROM token_static "
                  "WHERE creator_address IS NOT NULL").fetchone()["c"])
print("  creators appearing on >1 token:",
      con.execute("SELECT COUNT(*) c FROM (SELECT creator_address FROM token_static "
                  "WHERE creator_address IS NOT NULL GROUP BY creator_address "
                  "HAVING COUNT(*)>1)").fetchone()["c"])
print("  token_static rows with creator_address NULL:",
      con.execute("SELECT COUNT(*) c FROM token_static WHERE creator_address IS NULL").fetchone()["c"])
con.close()
