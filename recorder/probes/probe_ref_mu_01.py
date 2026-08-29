import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def show(title, sql, args=()):
    print("=" * 78)
    print(title)
    print("SQL:", " ".join(sql.split()))
    rows = con.execute(sql, args).fetchall()
    if not rows:
        print("  (no rows)")
        return
    print("  " + " | ".join(rows[0].keys()))
    for r in rows:
        print("  " + " | ".join("NULL" if v is None else str(v) for v in tuple(r)))

# --- 1) signal_events: my own formulation (COUNT(col), not SUM(col IS NOT NULL))
show("1) signal_events coverage of the 6 multi_user body fields, per signal_type", """
SELECT signal_type,
       COUNT(*)                  AS n,
       COUNT(minutes)            AS c_minutes,
       COUNT(are_top_traders)    AS c_are_top,
       COUNT(unique_traders)     AS c_uniq,
       COUNT(num_trades)         AS c_ntrades,
       COUNT(price_change_pct)   AS c_pcpct,
       COUNT(total_volume)       AS c_tvol,
       COUNT(top_trader_ids_json) AS c_ttids
  FROM signal_events
 GROUP BY signal_type
 ORDER BY n DESC
""")

show("1b) grand total signal_events + total non-null on `minutes`", """
SELECT COUNT(*) AS n_all,
       COUNT(minutes) AS c_minutes,
       ROUND(100.0*COUNT(minutes)/COUNT(*), 4) AS pct_minutes,
       COUNT(DISTINCT minutes) AS d_minutes, MIN(minutes) AS mn, MAX(minutes) AS mx,
       COUNT(DISTINCT are_top_traders) AS d_at, MIN(are_top_traders) AS at_mn,
       MAX(are_top_traders) AS at_mx
  FROM signal_events
""")

# --- 2) the model population, computed on the base table with the view's cheap filters
POP = """
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
                                          WHERE key='current_feature_version'),'0') AS INTEGER)
"""

show("2) model population size + non-null counts (COUNT(col) form)", """
SELECT COUNT(*) AS n_pop,
       COUNT(minutes) AS c_minutes, COUNT(are_top_traders) AS c_are_top,
       COUNT(unique_traders) AS c_uniq, COUNT(num_trades) AS c_ntrades,
       COUNT(price_change_pct) AS c_pcpct, COUNT(total_volume) AS c_tvol,
       COUNT(volume_per_trader) AS c_vpt
""" + POP)

show("2b) model population: distinct values / min / max for each of the 7", """
SELECT COUNT(DISTINCT minutes) AS d_min, MIN(minutes) AS min_mn, MAX(minutes) AS min_mx,
       COUNT(DISTINCT are_top_traders) AS d_at, MIN(are_top_traders) AS at_mn, MAX(are_top_traders) AS at_mx,
       COUNT(DISTINCT unique_traders) AS d_ut, MIN(unique_traders) AS ut_mn, MAX(unique_traders) AS ut_mx,
       COUNT(DISTINCT num_trades) AS d_nt, MIN(num_trades) AS nt_mn, MAX(num_trades) AS nt_mx,
       COUNT(DISTINCT price_change_pct) AS d_pc,
       COUNT(DISTINCT total_volume) AS d_tv,
       COUNT(DISTINCT volume_per_trader) AS d_vpt
""" + POP)

# --- 3) THE DECISIVE CROSS-TAB: is the NULL explained entirely by signal_type?
show("3) model population: coverage per signal_type", """
SELECT signal_type, COUNT(*) AS n,
       COUNT(minutes) AS c_minutes, COUNT(are_top_traders) AS c_are_top,
       COUNT(unique_traders) AS c_uniq, COUNT(volume_per_trader) AS c_vpt
""" + POP + " GROUP BY signal_type ORDER BY n DESC")

show("4) whole training_rows (no filters) for comparison with their query", """
SELECT COUNT(*) AS n_all, SUM(minutes IS NULL) AS min_nulls,
       COUNT(DISTINCT minutes) AS d_min, MIN(minutes) AS mn, MAX(minutes) AS mx,
       SUM(are_top_traders IS NULL) AS at_nulls, COUNT(DISTINCT are_top_traders) AS d_at
  FROM training_rows
""")

show("5) training_rows breakdown by kind/is_live/feature_version (population sanity)", """
SELECT kind, is_live, feature_version, COUNT(*) AS n, COUNT(minutes) AS c_minutes
  FROM training_rows GROUP BY 1,2,3 ORDER BY n DESC LIMIT 20
""")

show("6) current_feature_version", "SELECT key, value FROM meta WHERE key='current_feature_version'")

con.close()
