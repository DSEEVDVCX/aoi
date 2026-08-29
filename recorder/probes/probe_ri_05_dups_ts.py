import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, cap=40):
    t = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:cap]: print("   ", dict(r))
    if len(rows) > cap: print(f"    ... {len(rows)} rows")

print("========== 4. DUPLICATES ==========")
q("duplicate signal_events for same (token,network,ts,signal_type)", """
SELECT COUNT(*) dup_groups, SUM(n) rows_in_dup_groups, SUM(n-1) redundant
  FROM (SELECT token_address, COALESCE(network_id,'~') net, ts, signal_type, COUNT(*) n
          FROM signal_events GROUP BY 1,2,3,4 HAVING n>1)
""")
q("worst duplicate groups", """
SELECT token_address, COALESCE(network_id,'~') net, ts, signal_type, COUNT(*) n
  FROM signal_events GROUP BY 1,2,3,4 HAVING n>1 ORDER BY n DESC LIMIT 10
""")
q("signal_events with NULL ts (cannot be deduped by the view)", """
SELECT COUNT(*) null_ts, (SELECT COUNT(*) FROM signal_events) total FROM signal_events WHERE ts IS NULL
""")
q("duplicate watch_windows: same token+network with >1 first_seen_at within 60 min", """
SELECT COUNT(*) near_dup_pairs FROM watch_windows a JOIN watch_windows b
  ON a.token_address=b.token_address AND a.network_id=b.network_id
 AND a.first_seen_at < b.first_seen_at
 AND (julianday(b.first_seen_at)-julianday(a.first_seen_at))*1440.0 < 60
""")
q("watch_windows per token distribution", """
SELECT windows_per_token, COUNT(*) tokens FROM (
  SELECT token_address, network_id, COUNT(*) windows_per_token
    FROM watch_windows GROUP BY 1,2) GROUP BY 1 ORDER BY 1
""", cap=60)

print("\n========== 5. TIMESTAMPS ==========")
q("signal_events recorded_at in the future / weird", """
SELECT SUM(recorded_at > '2026-08-23') future,
       SUM(recorded_at IS NULL) nulls,
       MIN(recorded_at) mn, MAX(recorded_at) mx FROM signal_events
""")
q("tz-aware vs naive ISO mix per column", """
SELECT 'signal_events.recorded_at' c,
       SUM(recorded_at LIKE '%+00:00') aware, SUM(recorded_at LIKE '%Z') zulu,
       SUM(recorded_at NOT LIKE '%+00:00' AND recorded_at NOT LIKE '%Z') naive FROM signal_events
UNION ALL SELECT 'signal_events.ts',
       SUM(ts LIKE '%+00:00'), SUM(ts LIKE '%Z'),
       SUM(ts NOT LIKE '%+00:00' AND ts NOT LIKE '%Z') FROM signal_events
UNION ALL SELECT 'watch_windows.first_seen_at',
       SUM(first_seen_at LIKE '%+00:00'), SUM(first_seen_at LIKE '%Z'),
       SUM(first_seen_at NOT LIKE '%+00:00' AND first_seen_at NOT LIKE '%Z') FROM watch_windows
UNION ALL SELECT 'watch_windows.watch_until',
       SUM(watch_until LIKE '%+00:00'), SUM(watch_until LIKE '%Z'),
       SUM(watch_until NOT LIKE '%+00:00' AND watch_until NOT LIKE '%Z') FROM watch_windows
UNION ALL SELECT 'watchlist.first_seen_at',
       SUM(first_seen_at LIKE '%+00:00'), SUM(first_seen_at LIKE '%Z'),
       SUM(first_seen_at NOT LIKE '%+00:00' AND first_seen_at NOT LIKE '%Z') FROM watchlist
UNION ALL SELECT 'token_static.recorded_at',
       SUM(recorded_at LIKE '%+00:00'), SUM(recorded_at LIKE '%Z'),
       SUM(recorded_at NOT LIKE '%+00:00' AND recorded_at NOT LIKE '%Z') FROM token_static
UNION ALL SELECT 'token_static.token_created_at',
       SUM(token_created_at LIKE '%+00:00'), SUM(token_created_at LIKE '%Z'),
       SUM(token_created_at IS NOT NULL AND token_created_at NOT LIKE '%+00:00' AND token_created_at NOT LIKE '%Z') FROM token_static
UNION ALL SELECT 'outcomes.labeled_at',
       SUM(labeled_at LIKE '%+00:00'), SUM(labeled_at LIKE '%Z'),
       SUM(labeled_at NOT LIKE '%+00:00' AND labeled_at NOT LIKE '%Z') FROM outcomes
UNION ALL SELECT 'training_rows.built_at',
       SUM(built_at LIKE '%+00:00'), SUM(built_at LIKE '%Z'),
       SUM(built_at NOT LIKE '%+00:00' AND built_at NOT LIKE '%Z') FROM training_rows
""")
q("entry_ts sanity (epoch seconds expected ~1.78e9)", """
SELECT 'outcomes' t, MIN(entry_ts) mn, MAX(entry_ts) mx,
       SUM(entry_ts<=0) le0, SUM(entry_ts>100000000000) looks_ms,
       SUM(entry_ts > strftime('%s','now')) future FROM outcomes
UNION ALL SELECT 'training_rows', MIN(entry_ts), MAX(entry_ts),
       SUM(entry_ts<=0), SUM(entry_ts>100000000000),
       SUM(entry_ts > strftime('%s','now')) FROM training_rows
""")
q("token_static.token_created_at format families", """
SELECT CASE WHEN token_created_at IS NULL THEN 'NULL'
            WHEN token_created_at GLOB '[0-9]*' AND token_created_at NOT GLOB '*-*' THEN 'numeric-epoch'
            WHEN token_created_at LIKE '%T%' THEN 'iso'
            ELSE 'other' END fam, COUNT(*) n, MIN(token_created_at) mn, MAX(token_created_at) mx
  FROM token_static GROUP BY fam
""")
q("outcomes.entry_ts vs signal_events.ts drift (signal kind)", """
SELECT COUNT(*) n_compared,
       SUM(ABS(o.entry_ts - strftime('%s', e.ts)) > 120) drift_gt_2min
  FROM outcomes o JOIN signal_events e ON e.id=o.key AND o.kind='signal'
 WHERE e.ts IS NOT NULL
""")

print("\n========== 3. token_static COVERAGE ==========")
q("token_static NULL coverage (all 1203 coins ever)", """
SELECT COUNT(*) total,
       SUM(token_created_at IS NULL) no_created_at,
       SUM(symbol IS NULL OR symbol='') no_symbol,
       SUM(name IS NULL OR name='') no_name,
       SUM(decimals IS NULL) no_decimals,
       SUM(launchpad_name IS NULL OR launchpad_name='') no_launchpad,
       SUM(creator_address IS NULL) no_creator,
       SUM(is_scam IS NULL) no_is_scam,
       SUM(exchanges_count IS NULL) no_exch
  FROM token_static
""")
q("token_static coverage per network", """
SELECT network_id, COUNT(*) n,
       SUM(token_created_at IS NULL) no_created,
       SUM(decimals IS NULL) no_dec,
       SUM(launchpad_name IS NULL) no_lp,
       SUM(symbol IS NULL) no_sym
  FROM token_static GROUP BY network_id ORDER BY n DESC
""")
q("ACTIVE watches missing token_created_at", """
SELECT COUNT(*) active_watches,
       SUM(s.token_created_at IS NULL) missing_created
  FROM watchlist wl JOIN token_static s
    ON s.token_address=wl.token_address AND s.network_id=wl.network_id
 WHERE wl.active=1
""")
q("every coin EVER watched (watch_windows) missing token_created_at", """
SELECT COUNT(DISTINCT w.token_address||'|'||w.network_id) coins_ever,
       COUNT(DISTINCT CASE WHEN s.token_created_at IS NULL
             THEN w.token_address||'|'||w.network_id END) coins_missing_created
  FROM watch_windows w LEFT JOIN token_static s
    ON s.token_address=w.token_address AND s.network_id=w.network_id
""")
q("the token_static token under 2 networks", """
SELECT token_address, network_id, symbol, token_created_at FROM token_static
 WHERE token_address IN (SELECT token_address FROM token_static GROUP BY token_address
       HAVING COUNT(DISTINCT COALESCE(network_id,'~'))>1)
""")
con.close()
