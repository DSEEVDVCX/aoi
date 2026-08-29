import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v"
).fetchone()["v"]
POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1 AND feature_version=?"""


def q(label, sql, args=()):
    print("\n=== " + label + " ===")
    for r in con.execute(sql, args).fetchall():
        print("  ", dict(r))


# --- A. THEIR EXACT SQL, verbatim, for an agreement statement -------------
q("their exact SQL", f"""
SELECT SUM(CASE WHEN creator_prior_tokens IS NULL THEN 1 ELSE 0 END) nulls,
       SUM(CASE WHEN creator_prior_tokens=0 THEN 1 ELSE 0 END) zeros,
       SUM(CASE WHEN creator_prior_tokens>0 THEN 1 ELSE 0 END) pos
  FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
   AND status='ok' AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)""")

# --- B. population-inflation check: did they scan retro / stale fv? -------
q("same column over the WHOLE table (what a wrong population would give)", """
SELECT COUNT(*) all_rows,
       SUM(CASE WHEN creator_prior_tokens=0 THEN 1 ELSE 0 END) zeros_all,
       SUM(CASE WHEN creator_prior_tokens>0 THEN 1 ELSE 0 END) pos_all
  FROM training_rows""")
q("zeros/pos by is_live and feature_version (is the model pop distinctive?)", """
SELECT is_live, feature_version, COUNT(*) n,
       SUM(CASE WHEN creator_prior_tokens=0 THEN 1 ELSE 0 END) zeros,
       SUM(CASE WHEN creator_prior_tokens>0 THEN 1 ELSE 0 END) pos
  FROM training_rows WHERE kind='signal' GROUP BY is_live, feature_version
 ORDER BY n DESC LIMIT 12""")

# --- C. the 184 rows: creator known in token_static yet feature NULL -----
q("cpt NULL + creator known: was the static row even visible at t0?", f"""
SELECT SUM(CASE WHEN CAST(strftime('%s', ts.recorded_at) AS INTEGER) > tr.entry_ts
                THEN 1 ELSE 0 END) static_after_t0,
       SUM(CASE WHEN CAST(strftime('%s', ts.recorded_at) AS INTEGER) <= tr.entry_ts
                THEN 1 ELSE 0 END) static_at_or_before_t0,
       COUNT(*) n
  FROM training_rows tr JOIN token_static ts
    ON ts.token_address=tr.token_address AND ts.network_id=tr.network_id
 WHERE {POP} AND tr.creator_prior_tokens IS NULL
   AND ts.creator_address IS NOT NULL AND ts.creator_address<>''""", (FV,))
q("those same rows: are the OTHER static features NULL too (row is None path)?", f"""
SELECT COUNT(*) n,
       SUM(CASE WHEN tr.token_age_h IS NULL THEN 1 ELSE 0 END) age_null,
       SUM(CASE WHEN tr.socials_count IS NULL THEN 1 ELSE 0 END) socials_null,
       SUM(CASE WHEN tr.name_len IS NULL THEN 1 ELSE 0 END) namelen_null
  FROM training_rows tr JOIN token_static ts
    ON ts.token_address=tr.token_address AND ts.network_id=tr.network_id
 WHERE {POP} AND tr.creator_prior_tokens IS NULL
   AND ts.creator_address IS NOT NULL AND ts.creator_address<>''""", (FV,))

# --- D. is the NULL side doctrine-correct? -------------------------------
q("cpt NULL rows: how many have creator_address NULL (doctrine honored)", f"""
SELECT COUNT(*) cpt_null,
       SUM(CASE WHEN ts.creator_address IS NULL OR ts.creator_address='' THEN 1 ELSE 0 END) creator_unknown
  FROM training_rows tr JOIN token_static ts
    ON ts.token_address=tr.token_address AND ts.network_id=tr.network_id
 WHERE {POP} AND tr.creator_prior_tokens IS NULL""", (FV,))

# --- E. variance / ceiling: how many DISTINCT tokens can ever be >0 ------
q("token_static: tokens belonging to a multi-token creator (the whole >0 ceiling)", """
SELECT COUNT(*) tokens_with_sibling FROM token_static
 WHERE creator_address IN (SELECT creator_address FROM token_static
   WHERE creator_address IS NOT NULL AND creator_address<>''
   GROUP BY creator_address HAVING COUNT(*)>1)""")
q("model population: distinct tokens by cpt bucket", f"""
SELECT CASE WHEN creator_prior_tokens IS NULL THEN 'null'
            WHEN creator_prior_tokens=0 THEN 'zero' ELSE 'pos' END bucket,
       COUNT(DISTINCT token_address) tokens, COUNT(*) rows_
  FROM training_rows WHERE {POP} GROUP BY bucket""", (FV,))

# --- F. the unused 2nd source: would chain_authority widen coverage? -----
q("tokens where chain_authority knows a creator but token_static does NOT", """
SELECT COUNT(DISTINCT ca.token_address) tokens_only_chain_knows
  FROM chain_authority ca JOIN token_static ts
    ON ts.token_address=ca.token_address AND ts.network_id=ca.network_id
 WHERE ca.creator_address IS NOT NULL AND ca.creator_address<>''
   AND (ts.creator_address IS NULL OR ts.creator_address='')""")

con.close()
