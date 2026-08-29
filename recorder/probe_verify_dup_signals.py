import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print("FAILED:", e)
        return None
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")
    return rows

# 0. total signal_events
q("A0 total signal_events", "SELECT COUNT(*) AS n FROM signal_events")

# 1. MY OWN measurement: self-join on the exact 4-tuple, counting the
#    "redundant" partner directly (a row that has a strictly-smaller-id twin).
q("A1 redundant rows via self-join (row has an earlier twin id)", """
SELECT COUNT(*) AS redundant_rows
  FROM signal_events cur
 WHERE EXISTS (
   SELECT 1 FROM signal_events ear
    WHERE ear.token_address = cur.token_address
      AND COALESCE(ear.network_id,'') = COALESCE(cur.network_id,'')
      AND ear.ts = cur.ts
      AND ear.signal_type = cur.signal_type
      AND ear.id < cur.id )
""")

# 2. distinct 4-tuples that are duplicated (my own DISTINCT-based count,
#    not their GROUP BY HAVING)
q("A2 distinct duplicated 4-tuples + total rows involved", """
SELECT COUNT(DISTINCT cur.token_address || '|' || COALESCE(cur.network_id,'~')
                      || '|' || cur.ts || '|' || cur.signal_type) AS dup_tuples
  FROM signal_events cur
 WHERE EXISTS (
   SELECT 1 FROM signal_events o
    WHERE o.token_address = cur.token_address
      AND COALESCE(o.network_id,'') = COALESCE(cur.network_id,'')
      AND o.ts = cur.ts
      AND o.signal_type = cur.signal_type
      AND o.id <> cur.id )
""")

# 3. now THEIR query, verbatim, to see if we agree
q("A3 THEIR query verbatim", """
SELECT COUNT(*) dup_groups, SUM(n) rows_in_dup_groups, SUM(n-1) redundant
  FROM (SELECT token_address, COALESCE(network_id,'~') net, ts, signal_type, COUNT(*) n
          FROM signal_events GROUP BY 1,2,3,4 HAVING n>1)
""")

# 4. the actual duplicate rows, fully listed
q("A4 the duplicate rows themselves", """
SELECT cur.id, cur.token_address, cur.network_id, cur.ts, cur.signal_type,
       cur.recorded_at, cur.price_usd, cur.total_volume, cur.num_trades,
       cur.unique_traders
  FROM signal_events cur
 WHERE EXISTS (
   SELECT 1 FROM signal_events o
    WHERE o.token_address = cur.token_address
      AND COALESCE(o.network_id,'') = COALESCE(cur.network_id,'')
      AND o.ts = cur.ts
      AND o.signal_type = cur.signal_type
      AND o.id <> cur.id )
 ORDER BY cur.token_address, cur.ts, cur.id
""")

# 5. NULL ts check, my own phrasing
q("A5 ts NULL / empty in signal_events", """
SELECT SUM(CASE WHEN ts IS NULL THEN 1 ELSE 0 END) AS ts_null,
       SUM(CASE WHEN ts = '' THEN 1 ELSE 0 END) AS ts_empty,
       COUNT(*) AS total
  FROM signal_events
""")

# 6. case-sensitivity trap: same claim but case-insensitive on token_address
q("A6 case-insensitive duplicate 4-tuples (EVM mixed case trap)", """
SELECT COUNT(*) dup_groups, SUM(n) rows_in_groups, SUM(n-1) redundant
  FROM (SELECT LOWER(token_address) t, COALESCE(network_id,'~') net, ts,
               signal_type, COUNT(*) n
          FROM signal_events GROUP BY 1,2,3,4 HAVING n>1)
""")

# 7. DO THEY EVEN REACH THE MODEL POPULATION? this is the real question.
fv = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
q("A7 duplicate signal ids present in training_rows at all (any state)", f"""
SELECT COUNT(*) AS n_rows,
       SUM(CASE WHEN kind='signal' THEN 1 ELSE 0 END) AS kind_signal,
       SUM(CASE WHEN is_live=1 THEN 1 ELSE 0 END) AS live,
       SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
       SUM(CASE WHEN is_independent=1 THEN 1 ELSE 0 END) AS indep,
       SUM(CASE WHEN feature_version = {fv} THEN 1 ELSE 0 END) AS cur_fv
  FROM training_rows tr
 WHERE tr.kind='signal' AND tr.key IN (
   SELECT cur.id FROM signal_events cur
    WHERE EXISTS (SELECT 1 FROM signal_events o
                   WHERE o.token_address = cur.token_address
                     AND COALESCE(o.network_id,'') = COALESCE(cur.network_id,'')
                     AND o.ts = cur.ts AND o.signal_type = cur.signal_type
                     AND o.id <> cur.id))
""")

q("A8 duplicate ids inside THE MODEL POPULATION (cheap filters)", f"""
SELECT COUNT(*) AS n_in_model_pop
  FROM training_rows tr
 WHERE tr.kind='signal' AND tr.is_live=1 AND tr.asset_class='meme'
   AND tr.status='ok' AND tr.is_independent=1
   AND tr.feature_version = {fv}
   AND tr.key IN (
   SELECT cur.id FROM signal_events cur
    WHERE EXISTS (SELECT 1 FROM signal_events o
                   WHERE o.token_address = cur.token_address
                     AND COALESCE(o.network_id,'') = COALESCE(cur.network_id,'')
                     AND o.ts = cur.ts AND o.signal_type = cur.signal_type
                     AND o.id <> cur.id))
""")

q("A9 size of the model population for reference", f"""
SELECT COUNT(*) AS model_pop
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version = {fv}
""")

# 10. context: how common are near-duplicate signals in general (the thing
#     is_independent exists for). If 69% are <5min apart, an exact-ts pair is
#     the same phenomenon, not a distinct bug.
q("A10 is_independent distribution over live signal training rows", """
SELECT is_independent, COUNT(*) n
  FROM training_rows WHERE kind='signal' AND is_live=1
 GROUP BY 1
""")

# 11. Are the duplicate pairs actually IDENTICAL payloads, or genuinely
#     different upstream events that merely share a timestamp?
q("A11 do the duplicate pairs differ in payload?", """
SELECT a.token_address, a.ts, a.signal_type,
       a.id AS id_a, b.id AS id_b,
       a.price_usd AS px_a, b.price_usd AS px_b,
       a.total_volume AS vol_a, b.total_volume AS vol_b,
       a.buyer_user_id AS buyer_a, b.buyer_user_id AS buyer_b,
       a.usd_amount AS usd_a, b.usd_amount AS usd_b,
       a.recorded_at AS rec_a, b.recorded_at AS rec_b
  FROM signal_events a JOIN signal_events b
    ON a.token_address = b.token_address
   AND COALESCE(a.network_id,'') = COALESCE(b.network_id,'')
   AND a.ts = b.ts AND a.signal_type = b.signal_type AND a.id < b.id
""")

con.close()
