"""Adversarial verification part 2: model population side."""
import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
        WHERE key='current_feature_version'),'0') AS INTEGER)
"""

def show(title, sql, params=()):
    print("=" * 78)
    print(title)
    print("-" * 78)
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("NULL" if r[c] is None else str(r[c]) for c in cols))
    print()

show("Q5 model population size", "SELECT COUNT(*) AS n_model " + POP)

show("Q6 model population: signal_type breakdown", """
SELECT signal_type, COUNT(*) AS n,
       COUNT(minutes) AS c_minutes, COUNT(unique_traders) AS c_uniq,
       COUNT(volume_per_trader) AS c_vpt
""" + POP + " GROUP BY signal_type ORDER BY n DESC")

show("Q7 model population: coverage of the 7 columns (non-null counts + distinct)", """
SELECT COUNT(*) AS n,
       COUNT(minutes) AS c_min, COUNT(DISTINCT minutes) AS d_min,
       COUNT(are_top_traders) AS c_atr, COUNT(DISTINCT are_top_traders) AS d_atr,
       COUNT(unique_traders) AS c_uniq, COUNT(DISTINCT unique_traders) AS d_uniq,
       COUNT(num_trades) AS c_ntr, COUNT(DISTINCT num_trades) AS d_ntr,
       COUNT(price_change_pct) AS c_pcp, COUNT(DISTINCT price_change_pct) AS d_pcp,
       COUNT(total_volume) AS c_vol, COUNT(DISTINCT total_volume) AS d_vol,
       COUNT(volume_per_trader) AS c_vpt, COUNT(DISTINCT volume_per_trader) AS d_vpt
""" + POP)

show("Q8 whole training_rows table (the claim's own, unfiltered, population)", """
SELECT COUNT(*) AS n_all, COUNT(minutes) AS c_min,
       MIN(minutes) AS mn, MAX(minutes) AS mx,
       COUNT(are_top_traders) AS c_atr, MIN(are_top_traders) AS a_mn,
       MAX(are_top_traders) AS a_mx
  FROM training_rows
""")

show("Q9 the 5 non-null model rows, in full", """
SELECT key, signal_type, minutes, are_top_traders, unique_traders, num_trades,
       ROUND(price_change_pct,3) AS pcp, ROUND(total_volume,1) AS vol,
       ROUND(volume_per_trader,1) AS vpt
""" + POP + " AND minutes IS NOT NULL ORDER BY key")

show("Q10 does ANY model row have signal_type multi_user_* but NULL minutes?", """
SELECT COUNT(*) AS mismatched
""" + POP + " AND signal_type LIKE 'multi_user%' AND minutes IS NULL")

show("Q11 non-model kinds present in training_rows", """
SELECT kind, is_live, feature_version, COUNT(*) AS n
  FROM training_rows GROUP BY 1,2,3 ORDER BY n DESC LIMIT 20
""")

con.close()
