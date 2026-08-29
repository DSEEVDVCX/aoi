import os, sqlite3, config, time, datetime as dt

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = int(con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])
LIVE = int(config.LIVE_START_TS)

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:45]:
        print("   ", dict(r))
    if len(rows) > 45:
        print(f"    ... {len(rows)} rows total")
    return rows

q("EVM build freeze: entry_ts date range of missing model-eligible outcomes", f"""
SELECT o.network_id, date(o.entry_ts,'unixepoch') d, COUNT(*) n
  FROM outcomes o
 WHERE o.kind='signal' AND o.is_independent=1 AND o.status='ok' AND o.entry_ts>={LIVE}
   AND COALESCE(o.network_id,'') IN ('4663','8453','143')
   AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind='signal' AND r.key=o.key AND r.feature_version={FV})
 GROUP BY 1,2 ORDER BY 2 DESC LIMIT 20""")

q("model population TODAY by network (what the model actually sees)", f"""
SELECT network_id, COUNT(*) rows, COUNT(DISTINCT token_address) coins, MAX(date(entry_ts,'unixepoch')) newest
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
   AND feature_version={FV}
 GROUP BY 1 ORDER BY 2 DESC""")

q("what the model population WOULD be per network if EVM were built (outcomes side)", f"""
SELECT o.network_id, COUNT(*) eligible_outcomes
  FROM outcomes o
 WHERE o.kind='signal' AND o.is_independent=1 AND o.status='ok' AND o.entry_ts>={LIVE}
 GROUP BY 1 ORDER BY 2 DESC""")

print()
print("=== TIMESTAMP SANITY ===")
now = int(time.time())
print("now epoch:", now, dt.datetime.utcfromtimestamp(now).isoformat())
q("entry_ts sanity", f"""
SELECT 'outcomes' t, SUM(CASE WHEN entry_ts IS NULL THEN 1 ELSE 0 END) nulls,
       SUM(CASE WHEN entry_ts<=0 THEN 1 ELSE 0 END) le0,
       SUM(CASE WHEN entry_ts>{now}+300 THEN 1 ELSE 0 END) future,
       SUM(CASE WHEN entry_ts>100000000000 THEN 1 ELSE 0 END) looks_like_ms,
       MIN(entry_ts) mn, MAX(entry_ts) mx FROM outcomes
UNION ALL
SELECT 'training_rows', SUM(CASE WHEN entry_ts IS NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN entry_ts<=0 THEN 1 ELSE 0 END),
       SUM(CASE WHEN entry_ts>{now}+300 THEN 1 ELSE 0 END),
       SUM(CASE WHEN entry_ts>100000000000 THEN 1 ELSE 0 END),
       MIN(entry_ts), MAX(entry_ts) FROM training_rows""")

q("recorded_at in the future", f"""
SELECT 'signal_events' t, COUNT(*) n FROM signal_events WHERE recorded_at > strftime('%Y-%m-%dT%H:%M:%S','now','+5 minutes')
UNION ALL SELECT 'token_static', COUNT(*) FROM token_static WHERE recorded_at > strftime('%Y-%m-%dT%H:%M:%S','now','+5 minutes')
UNION ALL SELECT 'market_ticks', COUNT(*) FROM market_ticks WHERE recorded_at > strftime('%Y-%m-%dT%H:%M:%S','now','+5 minutes')""")

q("signal_events.ts vs recorded_at: ts AFTER recorded_at (feed clock ahead)", """
SELECT COUNT(*) n, MIN(ts) mn, MAX(ts) mx FROM signal_events
 WHERE ts IS NOT NULL
   AND CAST(strftime('%s',ts) AS INTEGER) > CAST(strftime('%s',recorded_at) AS INTEGER) + 60""")

q("outcomes.entry_ts vs signal_events.recorded_at mismatch > 1h (kind=signal)", """
SELECT COUNT(*) n FROM outcomes o JOIN signal_events s ON s.id=o.key
 WHERE o.kind='signal'
   AND ABS(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) > 3600""")

q("training_rows.entry_ts vs outcomes.entry_ts mismatch", """
SELECT COUNT(*) n FROM training_rows t JOIN outcomes o ON o.kind=t.kind AND o.key=t.key
 WHERE t.entry_ts <> o.entry_ts""")

q("watch_windows: watch_until <= first_seen_at (bad window)", """
SELECT COUNT(*) n FROM watch_windows WHERE watch_until <= first_seen_at""")

q("watchlist active=1 but watch_until already past", """
SELECT COUNT(*) n FROM watchlist WHERE active=1 AND watch_until < strftime('%Y-%m-%dT%H:%M:%S','now')""")

q("token_created_at in the future (coin created after now)", f"""
SELECT COUNT(*) n, MAX(CAST(token_created_at AS INTEGER)) mx FROM token_static
 WHERE token_created_at IS NOT NULL AND CAST(token_created_at AS INTEGER) > {now}""")

q("token_created_at implausibly old / ms-scale", f"""
SELECT SUM(CASE WHEN CAST(token_created_at AS INTEGER) < 1400000000 THEN 1 ELSE 0 END) before_2014,
       SUM(CASE WHEN CAST(token_created_at AS INTEGER) > 100000000000 THEN 1 ELSE 0 END) ms_scale,
       COUNT(*) n FROM token_static WHERE token_created_at IS NOT NULL""")

con.close()
