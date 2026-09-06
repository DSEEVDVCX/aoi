"""Adversarial verification: multi_user-only signal-shape columns."""
import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

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

# --- Q1: my own phrasing: coverage per signal_type, all six columns, using
#         COUNT(col) (not SUM(IS NOT NULL)) so it is a different formulation.
show("Q1 signal_events coverage per signal_type (COUNT(col) form)", """
SELECT signal_type,
       COUNT(*)                AS n,
       COUNT(minutes)          AS c_minutes,
       COUNT(are_top_traders)  AS c_atr,
       COUNT(unique_traders)   AS c_uniq,
       COUNT(num_trades)       AS c_ntr,
       COUNT(price_change_pct) AS c_pcp,
       COUNT(total_volume)     AS c_vol
  FROM signal_events
 GROUP BY signal_type
 ORDER BY n DESC
""")

show("Q2 signal_events grand totals + pct", """
SELECT COUNT(*) AS n_events,
       COUNT(minutes) AS c_minutes,
       ROUND(100.0*COUNT(minutes)/COUNT(*), 4) AS pct_minutes,
       COUNT(are_top_traders) AS c_atr,
       MIN(minutes) AS min_minutes, MAX(minutes) AS max_minutes,
       COUNT(DISTINCT minutes) AS d_minutes,
       MIN(are_top_traders) AS min_atr, MAX(are_top_traders) AS max_atr,
       COUNT(DISTINCT are_top_traders) AS d_atr
  FROM signal_events
""")

# --- Q3: is the non-null set EXACTLY the multi_user_* set? (contrapositive test)
show("Q3 cross-tab: is minutes-non-null <=> signal_type LIKE 'multi_user%'", """
SELECT CASE WHEN signal_type LIKE 'multi_user%' THEN 'multi_user' ELSE 'other' END AS grp,
       CASE WHEN minutes IS NULL THEN 'minutes_null' ELSE 'minutes_set' END AS m,
       COUNT(*) AS n
  FROM signal_events
 GROUP BY 1,2
 ORDER BY 1,2
""")

# --- Q4: what does the current feature version look like
show("Q4 meta current_feature_version", """
SELECT value FROM meta WHERE key='current_feature_version'
""")

con.close()
