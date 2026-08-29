import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

con.execute("""CREATE TEMP TABLE g AS
SELECT w.token_address ta, w.network_id nid, w.first_seen_at fsa, w.watch_until wu,
       w.admission_source asrc, w.is_control ic,
       w.token_address||':'||w.network_id||':'||w.first_seen_at k,
       CASE WHEN EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
                          AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at)
            THEN 'labelled' ELSE 'unlabelled' END lab
  FROM watch_windows w
 WHERE w.design_version>=3 AND w.is_control=0 AND w.admission_source='trending'
   AND julianday('now')-julianday(w.first_seen_at) >= (48.0*3600.0+900.0)/86400.0""")
print("dv3 trending signal windows matured:", q("SELECT COUNT(*) n FROM g")[0]["n"])

print("\n=== S. selection test: repeat-signal intensity, blocked vs labelled ===")
for r in q("""SELECT lab, COUNT(*) n, ROUND(AVG(sigs),2) mean_signals_per_token,
                     ROUND(AVG(sigs>=5),3) frac_token_with_5plus_signals
                FROM (SELECT g.lab, (SELECT COUNT(*) FROM signal_events s
                        WHERE s.token_address=g.ta AND s.network_id=g.nid) sigs FROM g)
               GROUP BY lab"""):
    print(f"  {r['lab']:11s} n={r['n']:6d} mean signal_events on that token={r['mean_signals_per_token']}"
          f"  frac(>=5 signals)={r['frac_token_with_5plus_signals']}")

print("\n=== T. outcome distribution of the LABELLED dv3 trending arm, split by whether the")
print("       window opened mid-watch (the same condition that blocks the others) ===")
for r in q("""SELECT CASE WHEN wl.first_seen_at IS NULL THEN 'no watchlist row'
                     WHEN g.fsa > wl.first_seen_at THEN 'opened mid-watch'
                     ELSE 'opened with the watchlist entry' END c,
                     COUNT(*) n, ROUND(AVG(o.final_return_48h),4) mean_ret,
                     ROUND(AVG(o.is_rug),4) rug_rate, ROUND(AVG(o.max_gain_24h),4) mean_maxgain
                FROM g JOIN outcomes o ON o.kind='watch' AND o.key=g.k
                LEFT JOIN watchlist wl ON wl.token_address=g.ta AND wl.network_id=g.nid
               WHERE o.status='ok' GROUP BY 1 ORDER BY n DESC"""):
    print(f"  {r['c']:32s} n={r['n']:6d} mean final_return_48h={r['mean_ret']} rug={r['rug_rate']} mean_max_gain_24h={r['mean_maxgain']}")
con.close()
