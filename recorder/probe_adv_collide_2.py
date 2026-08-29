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


# --- 4. Are the 43 really the SAME upstream feed event? ---
q("4a. do the 43 keys exist in BOTH activity_events.id and signal_events.id?", """
SELECT SUM(EXISTS(SELECT 1 FROM activity_events ae WHERE ae.id = a.key)) AS in_activity_events,
       SUM(EXISTS(SELECT 1 FROM signal_events se WHERE se.id = a.key))   AS in_signal_events,
       COUNT(*) AS total
  FROM outcomes a
 WHERE a.kind='activity'
   AND EXISTS (SELECT 1 FROM outcomes s WHERE s.kind='signal' AND s.key=a.key)
""")

q("4b. GLOBAL id overlap between the two source tables (not just the 43)", """
SELECT (SELECT COUNT(*) FROM activity_events) AS n_activity_events,
       (SELECT COUNT(*) FROM signal_events)   AS n_signal_events,
       (SELECT COUNT(*) FROM activity_events ae
          WHERE EXISTS (SELECT 1 FROM signal_events se WHERE se.id = ae.id)) AS shared_ids
""")

q("4c. for the 43: are the two outcome rows field-identical?", """
SELECT COUNT(*) AS n,
       SUM(a.token_address = s.token_address) AS same_token,
       SUM(COALESCE(a.network_id,'') = COALESCE(s.network_id,'')) AS same_net,
       SUM(a.entry_ts = s.entry_ts) AS same_entry_ts,
       SUM(COALESCE(a.status,'')=COALESCE(s.status,'')) AS same_status,
       SUM(COALESCE(a.split,'')=COALESCE(s.split,'')) AS same_split,
       SUM(COALESCE(a.signal_type,'')=COALESCE(s.signal_type,'')) AS same_type,
       SUM(COALESCE(a.is_independent,-1)=COALESCE(s.is_independent,-1)) AS same_indep,
       SUM(COALESCE(a.max_gain_48h,-999)=COALESCE(s.max_gain_48h,-999)) AS same_gain48
  FROM outcomes a JOIN outcomes s ON s.key=a.key AND s.kind='signal'
 WHERE a.kind='activity'
""")

q("4d. signal_type values on the two sides for the 43", """
SELECT a.signal_type AS activity_type, s.signal_type AS signal_type, COUNT(*) n
  FROM outcomes a JOIN outcomes s ON s.key=a.key AND s.kind='signal'
 WHERE a.kind='activity' GROUP BY 1,2
""")

# --- 5. Is the activity population frozen / retro-only? ---
q("5a. is_live of ALL activity training_rows + entry_ts range", """
SELECT is_live, COUNT(*) n, MIN(entry_ts) min_ts, MAX(entry_ts) max_ts,
       datetime(MIN(entry_ts),'unixepoch') min_utc, datetime(MAX(entry_ts),'unixepoch') max_utc
  FROM training_rows WHERE kind='activity' GROUP BY is_live
""")

q("5b. LIVE_START_TS from config/meta", "SELECT key, value FROM meta WHERE key LIKE '%live%' OR key LIKE '%LIVE%'")

q("5c. activity_events: is the table frozen? row count + ts range", """
SELECT COUNT(*) n, MIN(ts) min_ts, MAX(ts) max_ts,
       datetime(MIN(ts),'unixepoch') min_utc, datetime(MAX(ts),'unixepoch') max_utc
  FROM activity_events
""")

q("5d. labeled_at range of activity outcomes (when were they written?)", """
SELECT MIN(labeled_at) first_labeled, MAX(labeled_at) last_labeled, COUNT(*) n
  FROM outcomes WHERE kind='activity'
""")

q("5e. labeled_at range of signal outcomes for comparison", """
SELECT MIN(labeled_at) first_labeled, MAX(labeled_at) last_labeled, COUNT(*) n
  FROM outcomes WHERE kind='signal'
""")

# --- 6. Does the collision cause any measurable harm? duplicate key in model pop? ---
q("6a. within kind='signal' alone: any duplicate key? (PK is (kind,key))", """
SELECT COUNT(*) AS dup_keys_within_signal FROM (
  SELECT key FROM training_rows WHERE kind='signal' GROUP BY key HAVING COUNT(*)>1)
""")

q("6b. within kind='activity' alone: any duplicate key?", """
SELECT COUNT(*) AS dup_keys_within_activity FROM (
  SELECT key FROM training_rows WHERE kind='activity' GROUP BY key HAVING COUNT(*)>1)
""")

q("6c. model population: any duplicate key at all?", """
SELECT COUNT(*) AS dup FROM (
  SELECT key FROM training_rows
   WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1
     AND feature_version = CAST(COALESCE((SELECT value FROM meta
          WHERE key='current_feature_version'),'0') AS INTEGER)
   GROUP BY key HAVING COUNT(*)>1)
""")

q("6d. outcomes: does any 'watch' key collide with signal/activity? (namespace check)", """
SELECT COUNT(*) AS watch_keys_colliding
  FROM outcomes w
 WHERE w.kind='watch'
   AND EXISTS (SELECT 1 FROM outcomes o WHERE o.key=w.key AND o.kind<>'watch')
""")

con.close()
