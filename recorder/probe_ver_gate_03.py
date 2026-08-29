import os, sqlite3, config, time
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

con.execute("""CREATE TEMP TABLE b AS
WITH mat AS (
  SELECT w.token_address ta, w.network_id nid, w.first_seen_at fsa, w.watch_until wu,
         w.is_control ic, w.design_version dv, w.admission_source asrc,
         CAST(strftime('%s',w.first_seen_at) AS INTEGER) e0,
         w.token_address||':'||w.network_id||':'||w.first_seen_at AS k
    FROM watch_windows w
   WHERE julianday('now') - julianday(w.first_seen_at) >= (48.0*3600.0+900.0)/86400.0
)
SELECT mat.* FROM mat
  LEFT JOIN bars_fetch_state s ON s.token_address=mat.ta AND s.network_id=mat.nid
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch' AND o.key=mat.k)
   AND NOT (s.token_address IS NOT NULL
            AND ((s.last_status='ok' AND s.last_fetch_at >= mat.wu)
                 OR (s.last_status='no_data' AND s.attempts>=3)))""")
print("blocked set size:", q("SELECT COUNT(*) n FROM b")[0]["n"])

t0 = time.time()
print("\n=== K. bars actually on disk for the blocked windows (res 5, full set) ===")
rows = q("""SELECT b.k, b.dv, b.ic, b.asrc, b.e0,
                   (SELECT COUNT(*) FROM token_bars tb
                     WHERE tb.token_address=b.ta AND tb.network_id=b.nid AND tb.resolution='5'
                       AND tb.ts>=b.e0 AND tb.ts<=b.e0+48*3600) nbars,
                   (SELECT MIN(tb.ts) FROM token_bars tb
                     WHERE tb.token_address=b.ta AND tb.network_id=b.nid AND tb.resolution='5'
                       AND tb.ts>=b.e0) firstbar
              FROM b""")
print(f"  ({time.time()-t0:.1f}s for {len(rows)} windows)")
buck = {}
entry_ok = 0; nobars = 0
for r in rows:
    n = r["nbars"] or 0
    cov = n/576.0
    key = ("e) none" if n == 0 else "d) <25%" if cov < .25 else "c) 25-50%" if cov < .5
           else "b) 50-90%" if cov < .9 else "a) >=90%")
    buck[key] = buck.get(key, 0) + 1
    if n == 0: nobars += 1
    if r["firstbar"] is not None and r["firstbar"] - r["e0"] <= 1800: entry_ok += 1
for k in sorted(buck): print(f"  coverage {k:10s} {buck[k]}")
print(f"  windows with an entry bar within {config.LABEL_ENTRY_MAX_LAG_SECONDS}s of t0 (=> not 'no_entry'): {entry_ok}/{len(rows)}")
print(f"  windows with ZERO bars in the 48h window: {nobars}")

print("\n=== L. for comparison: coverage of windows that WERE labelled ok ===")
for r in q("""SELECT AVG(candles_48h) avg_c, COUNT(*) n FROM outcomes
               WHERE kind='watch' AND status='ok'"""):
    print("  labelled-ok watch outcomes:", r["n"], "mean candles_48h:", round(r["avg_c"] or 0,1), "of 576")

print("\n=== M. does the phase-1 comparison population lose these? ===")
for r in q("""SELECT COUNT(*) n FROM watch_windows w
               WHERE w.design_version>=3 AND w.admission_source IN ('trending','verified')
                 AND julianday('now')-julianday(w.first_seen_at) >= (48.0*3600.0+900.0)/86400.0"""):
    tot = r["n"]
for r in q("""SELECT COUNT(*) n FROM b WHERE dv>=3 AND asrc IN ('trending','verified')"""):
    blk = r["n"]
print(f"  matured dv>=3 trending/verified windows: {tot}; blocked (never labelable): {blk} ({100.0*blk/tot:.2f}%)")
for r in q("""SELECT ic, COUNT(*) n FROM b WHERE dv>=3 AND asrc IN ('trending','verified') GROUP BY 1"""):
    print(f"    of those, is_control={r['ic']}: {r['n']}")
con.close()
