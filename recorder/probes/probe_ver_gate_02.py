import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

con.execute("""CREATE TEMP TABLE b AS
WITH mat AS (
  SELECT w.token_address ta, w.network_id nid, w.first_seen_at fsa, w.watch_until wu,
         w.is_control ic, w.design_version dv, w.admission_source asrc,
         w.token_address||':'||w.network_id||':'||w.first_seen_at AS k
    FROM watch_windows w
   WHERE julianday('now') - julianday(w.first_seen_at) >= (48.0*3600.0+900.0)/86400.0
)
SELECT mat.*, s.last_status ls, s.attempts att, s.last_fetch_at lfa,
       wl.active wact, wl.watch_until wl_wu, wl.first_seen_at wl_fsa, wl.is_control wl_ic
  FROM mat
  LEFT JOIN bars_fetch_state s ON s.token_address=mat.ta AND s.network_id=mat.nid
  LEFT JOIN watchlist wl ON wl.token_address=mat.ta AND wl.network_id=mat.nid
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch' AND o.key=mat.k)
   AND NOT (s.token_address IS NOT NULL
            AND ((s.last_status='ok' AND s.last_fetch_at >= mat.wu)
                 OR (s.last_status='no_data' AND s.attempts>=3)))""")
n = q("SELECT COUNT(*) n FROM b")[0]["n"]
print("blocked-unlabelled matured windows =", n)

print("\n=== F. how far short does last_fetch_at fall of watch_until? (minutes) ===")
for r in q("""SELECT CASE WHEN g IS NULL THEN 'no state row'
                   WHEN g<=15 THEN 'a) <=15 min (one fetch cycle)'
                   WHEN g<=60 THEN 'b) 15-60 min'
                   WHEN g<=1440 THEN 'c) 1-24 h'
                   ELSE 'd) >24 h' END bucket, COUNT(*) n
              FROM (SELECT (julianday(wu)-julianday(lfa))*1440.0 g FROM b) GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['bucket']:32s} {r['n']}")
print("  median-ish gap minutes:", [round(x["g"],1) for x in q(
  "SELECT (julianday(wu)-julianday(lfa))*1440.0 g FROM b WHERE lfa IS NOT NULL ORDER BY g LIMIT 1")],
  [round(x["g"],1) for x in q("SELECT (julianday(wu)-julianday(lfa))*1440.0 g FROM b WHERE lfa IS NOT NULL ORDER BY g DESC LIMIT 1")])

print("\n=== G. PERMANENCE: age of last_fetch_at now (is fetching still happening?) ===")
for r in q("""SELECT CASE WHEN lfa IS NULL THEN 'no state'
                   WHEN a<=1 THEN 'a) <1h ago (STILL FETCHING)'
                   WHEN a<=24 THEN 'b) 1-24h ago'
                   WHEN a<=72 THEN 'c) 1-3 days ago'
                   ELSE 'd) >3 days ago (dead)' END bucket, COUNT(*) n, wact
              FROM (SELECT lfa, wact, (julianday('now')-julianday(lfa))*24.0 a FROM b)
             GROUP BY 1, wact ORDER BY 1"""):
    print(f"  {r['bucket']:30s} watchlist.active={r['wact']}  n={r['n']}")

print("\n=== H. MECHANISM: window watch_until vs the watchlist row that drives fetching ===")
for r in q("""SELECT CASE WHEN wl_wu IS NULL THEN 'no watchlist row'
                   WHEN wu > wl_wu THEN 'window ends AFTER watchlist stops fetching'
                   WHEN wu = wl_wu THEN 'equal'
                   ELSE 'window ends BEFORE watchlist stops' END c, COUNT(*) n
              FROM b GROUP BY 1 ORDER BY n DESC"""):
    print(f"  {r['c']:46s} {r['n']}")
print("  window fsa vs watchlist fsa (was the window added mid-watch?):")
for r in q("""SELECT CASE WHEN wl_fsa IS NULL THEN 'no watchlist row'
                   WHEN fsa > wl_fsa THEN 'window opened AFTER the watchlist entry'
                   WHEN fsa = wl_fsa THEN 'same instant'
                   ELSE 'window opened BEFORE' END c, COUNT(*) n FROM b GROUP BY 1 ORDER BY n DESC"""):
    print(f"    {r['c']:42s} {r['n']}")

print("\n=== I. per-arm loss RATE (blocked / all matured) ===")
for r in q("""SELECT dv, ic, COALESCE(asrc,'(null)') a,
                     COUNT(*) tot,
                     SUM(EXISTS(SELECT 1 FROM b WHERE b.k = w.token_address||':'||w.network_id||':'||w.first_seen_at)) blk
                FROM watch_windows w
               WHERE julianday('now')-julianday(w.first_seen_at) >= (48.0*3600.0+900.0)/86400.0
               GROUP BY 1,2,3 ORDER BY tot DESC""" .replace("dv","w.design_version").replace("ic","w.is_control").replace("asrc","w.admission_source")):
    pct = 100.0*r["blk"]/r["tot"] if r["tot"] else 0
    print(f"  dv={r['w.design_version'] if 'w.design_version' in r.keys() else r[0]} ic={r[1]} src={r[2]:10s} matured={r['tot']:6d} blocked={r['blk']:5d} ({pct:.2f}%)")

print("\n=== J. case-insensitive re-join: is the 'no state row' set a case bug? ===")
for r in q("""SELECT COUNT(*) n,
                SUM(EXISTS(SELECT 1 FROM bars_fetch_state s2
                     WHERE lower(s2.token_address)=lower(b.ta) AND s2.network_id=b.nid)) ci_state,
                SUM(EXISTS(SELECT 1 FROM watchlist w2
                     WHERE lower(w2.token_address)=lower(b.ta) AND w2.network_id=b.nid)) ci_watchlist
              FROM b WHERE lfa IS NULL"""):
    print("  no-state windows:", r["n"], "-> state row found case-insensitively:", r["ci_state"],
          "| watchlist row found case-insensitively:", r["ci_watchlist"])
con.close()
