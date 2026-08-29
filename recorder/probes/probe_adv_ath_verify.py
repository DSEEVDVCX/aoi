import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

print("current_feature_version =",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

# My own independent framing: group by the flag and by whether ANY price-history
# family value was measured, not just bars_history_h.
q = f"""
SELECT ath_history_complete AS flag,
       COUNT(*) AS n,
       SUM(bars_history_h IS NULL) AS no_bars_h,
       SUM(bars_count_24h IS NULL) AS no_bars_cnt,
       SUM(dist_from_ath IS NULL) AS no_dist,
       SUM(ath_history_days IS NULL) AS no_ath_days,
       SUM(bars_history_h IS NULL AND bars_count_24h IS NULL
           AND dist_from_ath IS NULL AND ret_1h_before IS NULL
           AND bar_vol_24h IS NULL) AS whole_family_null
  FROM training_rows
 WHERE {POP}
 GROUP BY ath_history_complete
 ORDER BY flag
"""
print("\n-- by flag value --")
tot = 0
for r in con.execute(q):
    tot += r["n"]
    print(dict(r))
print("total pop =", tot)

# Their exact SQL, for agreement check
q2 = f"""
SELECT SUM(CASE WHEN ath_history_complete=0 THEN 1 ELSE 0 END) c0,
       SUM(CASE WHEN ath_history_complete=0 AND bars_history_h IS NULL THEN 1 ELSE 0 END) c0_nobars,
       SUM(CASE WHEN ath_history_complete=0 AND bars_history_h IS NOT NULL THEN 1 ELSE 0 END) c0_hasbars,
       SUM(CASE WHEN ath_history_complete=1 THEN 1 ELSE 0 END) c1,
       SUM(CASE WHEN ath_history_complete IS NULL THEN 1 ELSE 0 END) cnull
  FROM training_rows WHERE {POP}
"""
print("\n-- theirs --")
print(dict(con.execute(q2).fetchone()))

# Is the flag NULL anywhere in the WHOLE table (any feature_version / retro)?
print("\n-- whole training_rows, no filters --")
print(dict(con.execute("""
SELECT COUNT(*) n, SUM(ath_history_complete IS NULL) cnull,
       SUM(ath_history_complete=0) c0, SUM(ath_history_complete=1) c1
  FROM training_rows""").fetchone()))

# control arm too
print("\n-- kind breakdown of flag=0 & no bars --")
for r in con.execute(f"""
SELECT kind, is_live, feature_version, COUNT(*) n
  FROM training_rows
 WHERE ath_history_complete=0 AND bars_history_h IS NULL
 GROUP BY kind, is_live, feature_version ORDER BY n DESC LIMIT 20"""):
    print(dict(r))

# For the 4 no-bars rows in the model population: identify them and check
# whether token_bars really has nothing for them at t0.
print("\n-- the no-bars model rows --")
rows = con.execute(f"""
SELECT key, token_address, network_id, ts, ath_history_days, dist_from_ath,
       bars_history_h, bars_count_24h, ret_1h_before, bar_vol_24h
  FROM training_rows
 WHERE {POP} AND ath_history_complete=0 AND bars_history_h IS NULL""").fetchall()
print("count =", len(rows))
for r in rows:
    d = dict(r)
    tb = con.execute("""SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx,
                               SUM(c_suspect=0) ok_close
                          FROM token_bars
                         WHERE token_address=? AND network_id=?
                           AND resolution=?""",
                     (r["token_address"], r["network_id"], config.BARS_RESOLUTION)).fetchone()
    d["bars_any_res5"] = dict(tb)
    tb1d = con.execute("""SELECT COUNT(*) n FROM token_bars
                           WHERE token_address=? AND network_id=? AND resolution='1D'""",
                       (r["token_address"], r["network_id"])).fetchone()
    d["bars_1D"] = tb1d["n"]
    print(d)
