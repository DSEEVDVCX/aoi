import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
c = con.cursor()

FVN = c.execute("SELECT CAST(value AS INTEGER) v FROM meta WHERE key='current_feature_version'").fetchone()['v']
print("feature_version =", FVN)
POP = f"""t.kind='signal' AND t.is_live=1 AND t.asset_class='meme' AND t.status='ok'
          AND t.is_independent=1 AND t.feature_version={FVN}"""

print("\n=== D1: the 21 no-tick rows on 08-19 -- WHICH HOUR (vs the 14:54Z block) ===")
for r in c.execute(f"""
SELECT strftime('%Y-%m-%dT%H', t.entry_ts,'unixepoch') h,
       COUNT(*) n_pop,
       SUM(CASE WHEN t.tick_age_min IS NULL THEN 1 ELSE 0 END) no_tick,
       MIN(s.recorded_at) rec_min,
       ROUND(AVG((CAST(strftime('%s',s.recorded_at) AS INTEGER)-t.entry_ts)/60.0),1) lag_min
  FROM training_rows t LEFT JOIN signal_events s ON s.id=t.key
 WHERE {POP} AND t.entry_ts>=strftime('%s','2026-08-19')
   AND t.entry_ts<strftime('%s','2026-08-20')
 GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['h']}  pop={r['n_pop']:3d}  no_tick={r['no_tick']:3d}  first_rec={str(r['rec_min'])[:19]}  avg_lag={r['lag_min']}m")

print("\n=== D2: market_ticks rows per hour, 08-18..08-20 (the tick collector's own footprint) ===")
prev = None
for r in c.execute("""
SELECT substr(recorded_at,1,13) h, COUNT(*) n, COUNT(DISTINCT token_address) toks
  FROM market_ticks
 WHERE recorded_at>='2026-08-18' AND recorded_at<'2026-08-21'
 GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['h']}  ticks={r['n']:6d}  tokens={r['toks']:5d}")

print("\n=== D3: recent-regime comparison -- no_tick pct, 08-11..08-20 only ===")
for r in c.execute(f"""
SELECT strftime('%Y-%m-%d', t.entry_ts,'unixepoch') d, COUNT(*) n,
       SUM(CASE WHEN t.tick_age_min IS NULL THEN 1 ELSE 0 END) nt,
       ROUND(100.0*SUM(CASE WHEN t.tick_age_min IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) pct
  FROM training_rows t WHERE {POP} AND t.entry_ts>=strftime('%s','2026-08-11')
 GROUP BY 1 ORDER BY 4 DESC"""):
    print(f"  {r['d']}  n={r['n']:4d}  no_tick={r['nt']:3d}  pct={r['pct']}")

print("\n=== D4: token_static family NULL rate per day (their 4th family) ===")
for r in c.execute(f"""
SELECT strftime('%Y-%m-%d', t.entry_ts,'unixepoch') d, COUNT(*) n,
       SUM(CASE WHEN t.token_created_at IS NULL THEN 1 ELSE 0 END) no_created,
       ROUND(100.0*SUM(CASE WHEN t.token_created_at IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) pct
  FROM training_rows t WHERE {POP} AND t.entry_ts>=strftime('%s','2026-08-11')
 GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['d']}  n={r['n']:4d}  token_created_at NULL={r['no_created']:3d} ({r['pct']}%)")

print("\n=== D5: is 08-20's population truncated by pipeline lag (labeler not caught up)? ===")
for r in c.execute("""
SELECT substr(ts,1,10) d, COUNT(*) feed,
       SUM(CASE WHEN EXISTS(SELECT 1 FROM training_rows t WHERE t.key=s.id AND t.kind='signal')
                THEN 1 ELSE 0 END) has_row
  FROM signal_events s WHERE ts>='2026-08-18' AND ts<'2026-08-22'
 GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['d']}  feed_events={r['feed']:6d}  with_training_row={r['has_row']:6d}")

con.close()
