import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

print("=== N. THEIR exact SQL, verbatim ===")
print(q("""SELECT COUNT(*) n FROM watch_windows w
 WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= (strftime('%s','now')-48*3600-900)
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
                    AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at)
   AND NOT EXISTS (SELECT 1 FROM bars_fetch_state s
                    WHERE s.token_address=w.token_address AND s.network_id=w.network_id
                      AND ((s.last_status='ok' AND s.last_fetch_at >= w.watch_until)
                           OR (s.last_status='no_data' AND s.attempts>=3)))""")[0]["n"])

con.execute("""CREATE TEMP TABLE b AS
WITH mat AS (
  SELECT w.token_address ta, w.network_id nid, w.first_seen_at fsa, w.watch_until wu,
         w.is_control ic, w.design_version dv, w.admission_source asrc, w.entry_signal_id esid,
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

print("\n=== O. is the underlying event labelled elsewhere (kind='signal' outcome)? ===")
for r in q("""SELECT COUNT(*) n,
                SUM(esid IS NOT NULL) has_sig,
                SUM(EXISTS(SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=b.esid)) sig_labelled
              FROM b"""):
    print(f"  blocked={r['n']}  have entry_signal_id={r['has_sig']}  that signal HAS a kind='signal' outcome={r['sig_labelled']}")

print("\n=== P. does the same token+network already have another LABELLED dv3 window? ===")
for r in q("""SELECT COUNT(*) n,
                SUM(EXISTS(SELECT 1 FROM watch_windows w2
                     JOIN outcomes o ON o.kind='watch'
                          AND o.key=w2.token_address||':'||w2.network_id||':'||w2.first_seen_at
                    WHERE w2.token_address=b.ta AND w2.network_id=b.nid AND w2.design_version>=3)) other
              FROM b WHERE dv>=3"""):
    print(f"  blocked dv3={r['n']}, token already has SOME labelled dv3 window={r['other']}")

print("\n=== Q. network split of the blocked set ===")
for r in q("SELECT nid, ic, COUNT(*) n FROM b GROUP BY 1,2 ORDER BY n DESC"):
    print(f"  network={r['nid']:12s} is_control={r['ic']} n={r['n']}")

print("\n=== R. is the backlog growing? blocked windows by day of first_seen_at ===")
for r in q("SELECT substr(fsa,1,10) d, COUNT(*) n FROM b GROUP BY 1 ORDER BY 1"):
    print(f"  {r['d']} {r['n']}")
con.close()
