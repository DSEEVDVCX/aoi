import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row


def q(label, sql, params=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        rows = con.execute(sql, params).fetchall()
    except Exception as e:
        print("  !! FAILED:", e)
        return []
    for r in rows:
        print("  ", dict(r))
    if not rows:
        print("   (no rows)")
    return rows


# --- 1. MY OWN formulation: EXISTS self-join, no GROUP_CONCAT ---
q("1a. outcomes: activity keys that also exist as a signal key (EXISTS join)", """
SELECT COUNT(*) AS n_activity_keys_also_signal
  FROM outcomes a
 WHERE a.kind = 'activity'
   AND EXISTS (SELECT 1 FROM outcomes s
                WHERE s.kind = 'signal' AND s.key = a.key)
""")

q("1b. outcomes: full kind pair matrix via self-join (a.kind < b.kind)", """
SELECT a.kind AS kind_a, b.kind AS kind_b, COUNT(*) AS n
  FROM outcomes a JOIN outcomes b ON a.key = b.key AND a.kind < b.kind
 GROUP BY 1,2
""")

q("1c. outcomes: rows per kind", """
SELECT kind, COUNT(*) n, SUM(status='ok') n_ok FROM outcomes GROUP BY kind
""")

q("1d. training_rows: activity keys that also exist as a signal key (EXISTS)", """
SELECT COUNT(*) AS n
  FROM training_rows a
 WHERE a.kind = 'activity'
   AND EXISTS (SELECT 1 FROM training_rows s
                WHERE s.kind = 'signal' AND s.key = a.key)
""")

q("1e. training_rows: kind pair matrix via self-join", """
SELECT a.kind AS kind_a, b.kind AS kind_b, COUNT(*) AS n
  FROM training_rows a JOIN training_rows b ON a.key = b.key AND a.kind < b.kind
 GROUP BY 1,2
""")

q("1f. training_rows: rows per kind", """
SELECT kind, COUNT(*) n FROM training_rows GROUP BY kind
""")

# --- 2. THEIR query, verbatim, for agreement check ---
q("2a. THEIR outcomes query verbatim", """
SELECT kinds, COUNT(*) n, MIN(key) sample_key FROM (
  SELECT key, GROUP_CONCAT(DISTINCT kind) kinds FROM outcomes
   GROUP BY key HAVING COUNT(DISTINCT kind) > 1) GROUP BY kinds
""")
q("2b. THEIR training_rows query verbatim", """
SELECT COUNT(*) FROM (SELECT key FROM training_rows GROUP BY key
 HAVING COUNT(DISTINCT kind) > 1)
""")

# --- 3. THE POPULATION QUESTION: is_live / feature_version of activity rows ---
q("3a. training_rows kind='activity': is_live x feature_version x status", """
SELECT is_live, feature_version, status, asset_class, COUNT(*) n
  FROM training_rows WHERE kind='activity'
 GROUP BY 1,2,3,4 ORDER BY n DESC
""")

q("3b. current_feature_version from meta", "SELECT value FROM meta WHERE key='current_feature_version'")

q("3c. does ANY activity row survive the model population filters?", """
SELECT COUNT(*) n FROM training_rows
 WHERE kind='activity' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
        WHERE key='current_feature_version'),'0') AS INTEGER)
""")

q("3d. model population size (kind='signal', cheap filters)", """
SELECT COUNT(*) n FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
        WHERE key='current_feature_version'),'0') AS INTEGER)
""")

q("3e. colliding SIGNAL-side rows: are they in the model population?", """
SELECT s.is_live, s.feature_version, s.status, s.asset_class, s.is_independent,
       COUNT(*) n
  FROM training_rows s
 WHERE s.kind='signal'
   AND EXISTS (SELECT 1 FROM training_rows a
                WHERE a.kind='activity' AND a.key = s.key)
 GROUP BY 1,2,3,4,5 ORDER BY n DESC
""")

con.close()
