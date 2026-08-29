import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

print("LABEL_WINDOW_HOURS", config.LABEL_WINDOW_HOURS, "MARGIN", config.LABEL_MARGIN_SECONDS)
print("now(db) =", q("SELECT datetime('now') d")[0]["d"])

# --- my own formulation: LEFT JOIN, explicit CASE on the gate, one pass ---
sql = """
WITH mat AS (
  SELECT w.token_address ta, w.network_id nid, w.first_seen_at fsa, w.watch_until wu,
         w.is_control ic, w.design_version dv, w.source src, w.admission_source asrc,
         w.token_address||':'||w.network_id||':'||w.first_seen_at AS k
    FROM watch_windows w
   WHERE julianday('now') - julianday(w.first_seen_at) >= (48.0*3600.0+900.0)/86400.0
),
j AS (
  SELECT mat.*, o.status AS ostat, s.last_status ls, s.attempts att, s.last_fetch_at lfa,
         CASE WHEN s.token_address IS NULL THEN 'no_state'
              WHEN (s.last_status='ok'      AND s.last_fetch_at >= mat.wu) THEN 'gate_ok_branch'
              WHEN (s.last_status='no_data' AND s.attempts >= 3)           THEN 'gate_nodata_branch'
              ELSE 'blocked' END AS gate
    FROM mat
    LEFT JOIN outcomes o ON o.kind='watch' AND o.key = mat.k
    LEFT JOIN bars_fetch_state s
           ON s.token_address = mat.ta AND s.network_id = mat.nid
)
SELECT * FROM j
"""
con.execute("CREATE TEMP TABLE t AS " + sql)
print("\n=== A. matured windows, by outcome presence x gate ===")
for r in q("""SELECT CASE WHEN ostat IS NULL THEN 'NO OUTCOME' ELSE 'labelled('||ostat||')' END lab,
                     gate, COUNT(*) n FROM t GROUP BY 1,2 ORDER BY 1,3 DESC"""):
    print(f"  {r['lab']:24s} {r['gate']:20s} {r['n']}")
print("  TOTAL matured:", q("SELECT COUNT(*) n FROM t")[0]["n"])
print("  unlabelled   :", q("SELECT COUNT(*) n FROM t WHERE ostat IS NULL")[0]["n"])
print("  unlab+blocked:", q("SELECT COUNT(*) n FROM t WHERE ostat IS NULL AND gate IN ('blocked','no_state')")[0]["n"])

print("\n=== B. the blocked-unlabelled set: last_status x attempts bucket ===")
for r in q("""SELECT COALESCE(ls,'(no row)') ls, CASE WHEN att IS NULL THEN 'n/a' WHEN att>=3 THEN '>=3' ELSE '<3' END ab,
                     COUNT(*) n, MIN(fsa) mn, MAX(fsa) mx
                FROM t WHERE ostat IS NULL AND gate IN ('blocked','no_state') GROUP BY 1,2 ORDER BY n DESC"""):
    print(f"  ls={r['ls']:10s} att{r['ab']:4s} n={r['n']:6d}  {r['mn']} .. {r['mx']}")

print("\n=== C. design_version / is_control / admission_source of blocked-unlabelled ===")
for r in q("""SELECT dv, ic, COALESCE(asrc,'(null)') asrc, COUNT(*) n FROM t
               WHERE ostat IS NULL AND gate IN ('blocked','no_state') GROUP BY 1,2,3 ORDER BY n DESC"""):
    print(f"  design_version={r['dv']} is_control={r['ic']} admission_source={r['asrc']:10s} n={r['n']}")
print("  -- for contrast, ALL matured windows by design_version:")
for r in q("SELECT dv, COUNT(*) n, SUM(ostat IS NULL) unl FROM t GROUP BY 1 ORDER BY 1"):
    print(f"     dv={r['dv']}: {r['n']} matured, {r['unl']} unlabelled")

print("\n=== D. is the string comparison the bug? compare as epochs too ===")
for r in q("""SELECT COUNT(*) n,
                 SUM(lfa >= wu) str_ge,
                 SUM(CAST(strftime('%s',lfa) AS INT) >= CAST(strftime('%s',wu) AS INT)) epoch_ge
              FROM t WHERE ostat IS NULL AND gate='blocked' AND lfa IS NOT NULL"""):
    print("  blocked w/ state:", r["n"], "string lfa>=wu:", r["str_ge"], "epoch lfa>=wu:", r["epoch_ge"])
print("  sample lfa/wu formats:")
for r in q("""SELECT lfa, wu, ls, att FROM t WHERE ostat IS NULL AND gate='blocked' ORDER BY fsa DESC LIMIT 5"""):
    print("   ", dict(r))

print("\n=== E. HOW can active=0 coexist with lfa<wu? later re-admission per token ===")
for r in q("""SELECT COALESCE(wl.active,-1) act, COUNT(*) n,
                     SUM(EXISTS (SELECT 1 FROM watch_windows w2
                          WHERE w2.token_address=t.ta AND w2.network_id=t.nid
                            AND w2.first_seen_at > t.fsa)) has_later_window
                FROM t LEFT JOIN watchlist wl ON wl.token_address=t.ta AND wl.network_id=t.nid
               WHERE t.ostat IS NULL AND t.gate IN ('blocked','no_state') GROUP BY 1"""):
    print(f"  watchlist.active={r['act']}: n={r['n']}, of which token has a LATER window: {r['has_later_window']}")
print("  and: does the current bars_fetch_state.last_fetch_at fall inside a LATER window?")
for r in q("""SELECT COUNT(*) n FROM t WHERE ostat IS NULL AND gate='blocked' AND lfa IS NOT NULL
                AND EXISTS (SELECT 1 FROM watch_windows w2 WHERE w2.token_address=t.ta AND w2.network_id=t.nid
                              AND w2.first_seen_at > t.fsa AND t.lfa >= w2.first_seen_at)"""):
    print("   ", r["n"])
con.close()
