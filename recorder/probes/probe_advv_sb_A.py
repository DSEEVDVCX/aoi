import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

def show(t, rows):
    print("==", t)
    for r in rows:
        print("   ", dict(r))
    print()

FV = q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v")[0]["v"]
print("current_feature_version =", FV)

MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND feature_version=%d" % FV)

# --- 1. my own formulation: GROUP BY a bucket expression (not SUM(cond))
show("model population bucketed by suspect_bars", q(f"""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'a_NULL'
              WHEN suspect_bars = 0     THEN 'b_zero'
              ELSE 'c_positive' END AS bucket,
         COUNT(*) AS n,
         MIN(datetime(entry_ts,'unixepoch')) AS first_entry,
         MAX(datetime(entry_ts,'unixepoch')) AS last_entry,
         MIN(built_at) AS min_built, MAX(built_at) AS max_built
    FROM training_rows WHERE {MODEL}
   GROUP BY bucket ORDER BY bucket"""))

show("total model rows (independent count)", q(f"SELECT COUNT(*) n FROM training_rows WHERE {MODEL}"))

# --- 2. what the `= 0` filter yields vs `IS NOT NULL OR ...` intent
show("filter comparison", q(f"""
  SELECT (SELECT COUNT(*) FROM training_rows WHERE {MODEL} AND suspect_bars = 0) AS eq0,
         (SELECT COUNT(*) FROM training_rows WHERE {MODEL} AND COALESCE(suspect_bars,0) = 0) AS coalesced,
         (SELECT COUNT(*) FROM training_rows WHERE {MODEL} AND NOT (suspect_bars > 0)) AS not_gt0"""))

# --- 3. time boundary: does the drop remove a contiguous head of history?
show("entry_ts boundary", q(f"""
  SELECT (SELECT datetime(MIN(entry_ts),'unixepoch') FROM training_rows WHERE {MODEL}) AS model_first,
         (SELECT datetime(MAX(entry_ts),'unixepoch') FROM training_rows WHERE {MODEL}) AS model_last,
         (SELECT datetime(MIN(entry_ts),'unixepoch') FROM training_rows WHERE {MODEL} AND suspect_bars=0) AS kept_first,
         (SELECT datetime(MAX(entry_ts),'unixepoch') FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL) AS null_last"""))

show("interleaving: non-NULL rows at/before the last NULL entry_ts", q(f"""
  SELECT COUNT(*) n FROM training_rows WHERE {MODEL} AND suspect_bars IS NOT NULL
    AND entry_ts <= (SELECT MAX(entry_ts) FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL)"""))

# --- 4. outcomes-level epoch check
show("outcomes: suspect_bars NULL vs NOT NULL labeled_at ranges", q("""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'NULL' ELSE 'set' END g,
         COUNT(*) n, MIN(labeled_at) mn, MAX(labeled_at) mx
    FROM outcomes GROUP BY g"""))

# --- 5. are the NULL rows' LABELS present at all?
show("NULL bucket: label completeness", q(f"""
  SELECT COUNT(*) n,
         SUM(final_return_48h IS NULL) fr_null,
         SUM(max_gain_48h IS NULL) mg48_null,
         SUM(max_gain_24h IS NULL) mg24_null,
         SUM(entry_px IS NULL) epx_null
    FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL"""))

# --- 6. label magnitude sanity: pre-flag era vs post-flag era
show("max_gain_48h extremes by bucket", q(f"""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'a_NULL' WHEN suspect_bars=0 THEN 'b_zero' ELSE 'c_pos' END bucket,
         COUNT(*) n, ROUND(MAX(max_gain_48h),2) mx_gain,
         SUM(max_gain_48h > 10) gt_1000pct,
         SUM(max_gain_48h > 100) gt_10000pct,
         ROUND(MIN(max_drawdown_48h),4) worst_dd,
         ROUND(MAX(final_return_48h),2) mx_final
    FROM training_rows WHERE {MODEL} GROUP BY bucket ORDER BY bucket"""))

con.close()
