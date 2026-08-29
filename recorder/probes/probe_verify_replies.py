import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def show(title, sql):
    print("\n== " + title + " ==")
    try:
        for r in con.execute(sql):
            print("   ", dict(r))
    except Exception as e:
        print("    FAILED:", e)

# 1. My own phrasing on training_rows: distribution via GROUP BY, not SUM(x=0)
show("A1 training_rows.social_replies value histogram (all rows)", """
SELECT CASE WHEN social_replies IS NULL THEN 'NULL' ELSE CAST(social_replies AS TEXT) END v,
       COUNT(*) c
FROM training_rows GROUP BY 1 ORDER BY c DESC LIMIT 15
""")

# 2. Model population, exact filters from the brief
show("A2 model population histogram", """
SELECT CASE WHEN social_replies IS NULL THEN 'NULL' ELSE CAST(social_replies AS TEXT) END v,
       COUNT(*) c
FROM training_rows
WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
  AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
GROUP BY 1 ORDER BY c DESC LIMIT 15
""")

# 3. Is the retro/stale population inflating it? split by is_live and feature_version
show("A3 training_rows social_replies by is_live", """
SELECT is_live, COUNT(*) n, SUM(social_replies IS NULL) nulls,
       COUNT(DISTINCT social_replies) d
FROM training_rows GROUP BY is_live
""")

# 4. token_social source histogram
show("B1 token_social.thesis_replies histogram", """
SELECT CASE WHEN thesis_replies IS NULL THEN 'NULL' ELSE CAST(thesis_replies AS TEXT) END v,
       COUNT(*) c
FROM token_social GROUP BY 1 ORDER BY c DESC LIMIT 15
""")

# 5. DECISIVE: token_thesis.num_replies is written WITHOUT `or 0`.
#    NULL => key absent upstream. 0 => key present with value 0.
show("C1 token_thesis.num_replies histogram (NULL=key absent, 0=present-and-zero)", """
SELECT CASE WHEN num_replies IS NULL THEN 'NULL' ELSE CAST(num_replies AS TEXT) END v,
       COUNT(*) c
FROM token_thesis GROUP BY 1 ORDER BY c DESC LIMIT 15
""")

# 6. Same decisive test for the sibling documented-dead field `equity` for contrast
show("C2 token_thesis.equity histogram (the other documented-dead field)", """
SELECT CASE WHEN equity IS NULL THEN 'NULL' ELSE CAST(equity AS TEXT) END v, COUNT(*) c
FROM token_thesis GROUP BY 1 ORDER BY c DESC LIMIT 10
""")

# 7. signal_events.num_replies -- schema says "zero in 238 rows, absent in the rest",
#    stored without `or 0`, so it should be mostly NULL. Tests the doctrine is applied
#    where the field really IS absent.
show("D1 signal_events.num_replies histogram", """
SELECT CASE WHEN num_replies IS NULL THEN 'NULL' ELSE CAST(num_replies AS TEXT) END v, COUNT(*) c
FROM signal_events GROUP BY 1 ORDER BY c DESC LIMIT 10
""")

# 8. sibling health in token_social to test the "field-specific" part of the claim
show("B2 token_social sibling distinct counts", """
SELECT COUNT(*) n,
       COUNT(DISTINCT thesis_total) d_total,
       COUNT(DISTINCT thesis_likes) d_likes,
       COUNT(DISTINCT thesis_authors) d_auth,
       COUNT(DISTINCT holder_authors) d_holder,
       COUNT(DISTINCT thesis_sampled) d_sampled
FROM token_social
""")

# 9. Do rows with a NONZERO likes sample still have replies=0? If the sample really
#    contains items, replies=0 is a measurement over present items, not a no-op.
show("B3 token_social: rows with sampled>0 -- are replies still 0?", """
SELECT COUNT(*) n_sampled_gt0,
       SUM(thesis_replies=0) replies_zero,
       SUM(thesis_likes>0) likes_pos,
       MAX(thesis_replies) max_replies
FROM token_social WHERE thesis_sampled > 0
""")

con.close()
