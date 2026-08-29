import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1
     AND feature_version = CAST(COALESCE((SELECT value FROM meta
                                           WHERE key='current_feature_version'),'0') AS INTEGER)"""

print("=== G. how many distinct coins own the 47 rows, and their date span ===")
print(dict(con.execute("""
SELECT COUNT(*) n, COUNT(DISTINCT token_address||'|'||network_id) coins,
       MIN(recorded_at) first_at, MAX(recorded_at) last_at,
       COUNT(DISTINCT network_id) nets
  FROM token_social WHERE thesis_sampled=0 AND thesis_total>0""").fetchone()))
for r in con.execute("""
SELECT token_address, network_id, COUNT(*) n, MIN(recorded_at) f, MAX(recorded_at) l
  FROM token_social WHERE thesis_sampled=0 AND thesis_total>0
 GROUP BY token_address, network_id ORDER BY n DESC"""):
    print(dict(r))

print("\n=== H. fingerprint check: can a sampled>0 row ever have authors=0? ===")
print(dict(con.execute("""
SELECT SUM(CASE WHEN thesis_sampled>0 AND thesis_authors=0 THEN 1 ELSE 0 END) s_pos_auth0,
       SUM(CASE WHEN thesis_sampled IS NULL AND thesis_authors=0 THEN 1 ELSE 0 END) s_null_auth0,
       SUM(CASE WHEN thesis_total>0 AND thesis_authors=0 THEN 1 ELSE 0 END) tot_pos_auth0
  FROM token_social""").fetchone()))

print("\n=== I. downstream reach: model-population rows whose social snapshot is one of the 47 ===")
print("   (fingerprint: social_thesis_total>0 AND social_thesis_authors=0)")
print(dict(con.execute(f"""
SELECT COUNT(*) pop,
       SUM(CASE WHEN social_thesis_total IS NOT NULL THEN 1 ELSE 0 END) has_snap,
       SUM(CASE WHEN social_thesis_total>0 AND social_thesis_authors=0 THEN 1 ELSE 0 END) fingerprint,
       SUM(CASE WHEN social_thesis_total=1 AND social_thesis_authors=0
                 AND social_holder_authors=0 THEN 1 ELSE 0 END) exact_fp,
       SUM(CASE WHEN social_thesis_total>0 AND social_thesis_authors=0
                 AND social_holder_ratio IS NOT NULL THEN 1 ELSE 0 END) ratio_not_null
  FROM training_rows WHERE {POP}""").fetchone()))

print("\n=== J. are the 47 rows' coins in the model population at all? ===")
print(dict(con.execute(f"""
SELECT COUNT(*) rows_for_those_coins
  FROM training_rows t
 WHERE {POP}
   AND EXISTS (SELECT 1 FROM token_social s
                WHERE s.thesis_sampled=0 AND s.thesis_total>0
                  AND s.token_address = t.token_address
                  AND s.network_id = t.network_id)""").fetchone()))

print("\n=== K. thesis_replies really is dead upstream (so the 47 add nothing there) ===")
print(dict(con.execute("""
SELECT COUNT(DISTINCT thesis_replies) d, MAX(thesis_replies) mx,
       SUM(thesis_replies IS NULL) nulls, COUNT(*) n FROM token_social""").fetchone()))

print("\n=== L. legacy rows: sampled/total NULL but likes populated ===")
print(dict(con.execute("""
SELECT COUNT(*) n, MIN(recorded_at) f, MAX(recorded_at) l
  FROM token_social WHERE thesis_sampled IS NULL""").fetchone()))
con.close()
