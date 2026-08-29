import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v"
).fetchone()["v"]
print("current_feature_version =", FV)

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1 AND feature_version=?"""


def q(label, sql, args=()):
    print("\n=== " + label + " ===")
    for r in con.execute(sql, args).fetchall():
        print("  ", dict(r))


# --- 1. MY OWN shape: GROUP BY a bucket expression, not SUM(CASE) --------
q("model population: creator_prior_tokens buckets (GROUP BY, not SUM CASE)", f"""
SELECT CASE WHEN creator_prior_tokens IS NULL THEN 'null'
            WHEN creator_prior_tokens=0    THEN 'zero'
            ELSE 'pos_' || creator_prior_tokens END AS bucket,
       COUNT(*) n
  FROM training_rows WHERE {POP}
 GROUP BY bucket ORDER BY n DESC""", (FV,))

q("model population total + COUNT(col) form", f"""
SELECT COUNT(*) total, COUNT(creator_prior_tokens) non_null,
       COUNT(*)-COUNT(creator_prior_tokens) nulls,
       MAX(creator_prior_tokens) mx,
       COUNT(DISTINCT token_address) distinct_tokens
  FROM training_rows WHERE {POP}""", (FV,))

# --- 2. is the 0 fabricated? join model rows to token_static -------------
q("model rows x token_static creator: does 0 only occur when creator KNOWN?", f"""
SELECT CASE WHEN tr.creator_prior_tokens IS NULL THEN 'cpt_null'
            WHEN tr.creator_prior_tokens=0 THEN 'cpt_zero' ELSE 'cpt_pos' END feat,
       CASE WHEN ts.token_address IS NULL THEN 'no_static_row'
            WHEN ts.creator_address IS NULL OR ts.creator_address='' THEN 'creator_null'
            ELSE 'creator_known' END src,
       COUNT(*) n
  FROM training_rows tr
  LEFT JOIN token_static ts
         ON ts.token_address = tr.token_address AND ts.network_id = tr.network_id
 WHERE {POP}
 GROUP BY feat, src ORDER BY feat, n DESC""", (FV,))

# --- 3. token_static universe, measured my own way ----------------------
q("token_static universe", """
SELECT COUNT(*) rows_,
       COUNT(DISTINCT token_address || '|' || network_id) distinct_token_net,
       COUNT(DISTINCT token_address) distinct_token,
       COUNT(creator_address) creator_present,
       COUNT(DISTINCT creator_address) distinct_creator,
       COUNT(DISTINCT lower(creator_address)) distinct_creator_lower
  FROM token_static""")

q("token_static: creator multiplicity histogram", """
SELECT ntok, COUNT(*) n_creators FROM (
  SELECT creator_address, COUNT(*) ntok FROM token_static
   WHERE creator_address IS NOT NULL AND creator_address<>''
   GROUP BY creator_address) GROUP BY ntok ORDER BY ntok""")

# --- 4. the two hidden narrowings in features.py:354-360 ----------------
q("creators whose tokens span >1 network (network_id=? filter would miss them)", """
SELECT COUNT(*) creators_multinet FROM (
  SELECT creator_address FROM token_static
   WHERE creator_address IS NOT NULL AND creator_address<>''
   GROUP BY creator_address HAVING COUNT(DISTINCT network_id)>1)""")

q("creator_address casing: would case-sensitive '=' lose matches?", """
SELECT SUM(CASE WHEN creator_address<>lower(creator_address) THEN 1 ELSE 0 END) mixed_case,
       COUNT(creator_address) present
  FROM token_static""")

q("collision only under lower(): distinct_lower vs distinct exact for multi-token", """
SELECT COUNT(*) creators_multi_under_lower FROM (
  SELECT lower(creator_address) c FROM token_static
   WHERE creator_address IS NOT NULL AND creator_address<>''
   GROUP BY c HAVING COUNT(*)>1)""")

# --- 5. creator coverage by network (is it a one-network-by-design gap?) -
q("token_static creator coverage per network", """
SELECT network_id, COUNT(*) rows_, COUNT(creator_address) with_creator,
       ROUND(100.0*COUNT(creator_address)/COUNT(*),1) pct
  FROM token_static GROUP BY network_id ORDER BY rows_ DESC""")

# --- 6. does the model population's token universe exceed token_static? --
q("model tokens missing a token_static row entirely", f"""
SELECT COUNT(*) model_tokens_no_static FROM (
  SELECT DISTINCT tr.token_address, tr.network_id
    FROM training_rows tr WHERE {POP}
     AND NOT EXISTS (SELECT 1 FROM token_static ts
                      WHERE ts.token_address=tr.token_address
                        AND ts.network_id=tr.network_id))""", (FV,))

# --- 7. second creator source features.py never reads --------------------
q("chain_authority creator coverage (an unused 2nd source)", """
SELECT COUNT(*) rows_, COUNT(creator_address) with_creator,
       COUNT(DISTINCT token_address) tokens,
       COUNT(DISTINCT creator_address) creators
  FROM chain_authority""")

con.close()
