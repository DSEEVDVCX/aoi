import os, sqlite3, config, time, hashlib

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, cap=40):
    t = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return None
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:cap]: print("   ", dict(r))
    if len(rows) > cap: print(f"    ... {len(rows)} rows")
    return rows

print("========== 6. SPLIT / IS_LIVE / IS_INDEPENDENT ==========")
q("coins appearing in MORE THAN ONE split", """
SELECT 'outcomes' t, COUNT(*) coins_multi_split FROM (
  SELECT token_address FROM outcomes WHERE split IS NOT NULL
   GROUP BY token_address HAVING COUNT(DISTINCT split)>1)
UNION ALL SELECT 'training_rows', COUNT(*) FROM (
  SELECT token_address FROM training_rows WHERE split IS NOT NULL
   GROUP BY token_address HAVING COUNT(DISTINCT split)>1)
""")

# verify stored split == recomputed hash split
def assign_split(a):
    d = hashlib.sha1(a.strip().lower().encode()).hexdigest()
    b = int(d[:8],16)%10
    return "train" if b<=6 else ("val" if b==7 else "test")

rows = con.execute("""SELECT token_address, split, COUNT(*) n FROM outcomes
                       WHERE split IS NOT NULL GROUP BY token_address, split""").fetchall()
bad = [(r["token_address"], r["split"], assign_split(r["token_address"]), r["n"])
       for r in rows if assign_split(r["token_address"]) != r["split"]]
print(f"\n### stored split vs recomputed sha1 split: {len(bad)} mismatching (token,split) groups "
      f"of {len(rows)}; rows affected = {sum(b[3] for b in bad)}")
for b in bad[:15]: print("   ", b)

q("split balance: model population (base table + cheap filters)", """
SELECT split, COUNT(*) rows, COUNT(DISTINCT token_address) coins FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
 GROUP BY split ORDER BY rows DESC
""")

print("\n--- FUNNEL, one filter at a time (cumulative) ---")
steps = [
 ("training_rows total", "1=1"),
 ("+ kind='signal'", "kind='signal'"),
 ("+ is_live=1", "kind='signal' AND is_live=1"),
 ("+ asset_class='meme'", "kind='signal' AND is_live=1 AND asset_class='meme'"),
 ("+ status='ok'", "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'"),
 ("+ is_independent=1", "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1"),
 ("+ feature_version=current", "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1 AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"),
]
for name, w in steps:
    r = con.execute(f"SELECT COUNT(*) n, COUNT(DISTINCT token_address) c FROM training_rows WHERE {w}").fetchone()
    print(f"  {name:32s} rows={r['n']:>7,}  coins={r['c']:>5,}")

q("asset_class distribution of kind=signal is_live=1", """
SELECT COALESCE(asset_class,'<NULL>') ac, COUNT(*) n FROM training_rows
 WHERE kind='signal' AND is_live=1 GROUP BY ac ORDER BY n DESC
""")
q("is_live distribution", """
SELECT kind, is_live, COUNT(*) n, MIN(entry_ts) mn, MAX(entry_ts) mx
 FROM training_rows GROUP BY kind, is_live
""")
q("is_live vs LIVE_START_TS consistency (is_live must be entry_ts>=LIVE_START_TS)", f"""
SELECT SUM(is_live=1 AND entry_ts < {config.LIVE_START_TS}) live_but_early,
       SUM(is_live=0 AND entry_ts >= {config.LIVE_START_TS}) notlive_but_late,
       COUNT(*) total FROM training_rows
""")
q("training_rows.split vs outcomes.split disagreement", """
SELECT COUNT(*) n FROM training_rows tr JOIN outcomes o
   ON o.kind=tr.kind AND o.key=tr.key
 WHERE COALESCE(tr.split,'~') <> COALESCE(o.split,'~')
""")
q("training_rows.is_independent vs outcomes.is_independent disagreement", """
SELECT COUNT(*) n FROM training_rows tr JOIN outcomes o
   ON o.kind=tr.kind AND o.key=tr.key
 WHERE COALESCE(tr.is_independent,-1) <> COALESCE(o.is_independent,-1)
""")

print("\n========== WATCH WINDOW OVERLAP ==========")
q("watch_windows opened while the token's PREVIOUS window was still open", """
SELECT COUNT(*) overlapping_windows FROM watch_windows w
 WHERE EXISTS (SELECT 1 FROM watch_windows p
                WHERE p.token_address=w.token_address AND p.network_id=w.network_id
                  AND p.first_seen_at < w.first_seen_at
                  AND p.watch_until > w.first_seen_at)
""")
q("overlapping windows by source/is_control", """
SELECT w.source, w.is_control, COUNT(*) n FROM watch_windows w
 WHERE EXISTS (SELECT 1 FROM watch_windows p
                WHERE p.token_address=w.token_address AND p.network_id=w.network_id
                  AND p.first_seen_at < w.first_seen_at
                  AND p.watch_until > w.first_seen_at)
 GROUP BY w.source, w.is_control ORDER BY n DESC
""")
q("watch_windows: watch_until - first_seen_at hours distribution", """
SELECT ROUND((julianday(watch_until)-julianday(first_seen_at))*24.0,1) hours, COUNT(*) n
  FROM watch_windows GROUP BY hours ORDER BY n DESC LIMIT 10
""")
q("max windows for one token", """
SELECT token_address, network_id, COUNT(*) windows, MIN(first_seen_at) f, MAX(first_seen_at) l
  FROM watch_windows GROUP BY 1,2 ORDER BY windows DESC LIMIT 5
""")
q("design_version distribution of watch_windows", """
SELECT design_version, is_control, COUNT(*) n FROM watch_windows GROUP BY 1,2 ORDER BY 1,2
""")

print("\n========== DRIFT + FUTURE created_at ==========")
q("signal entry_ts (=recorded_at) minus feed ts: drift buckets", """
SELECT CASE WHEN d < 0 THEN 'negative (recorded BEFORE feed ts)'
            WHEN d < 60 THEN '0-60s' WHEN d < 120 THEN '60-120s'
            WHEN d < 300 THEN '2-5min' WHEN d < 3600 THEN '5-60min'
            ELSE '>1h' END bucket, COUNT(*) n
  FROM (SELECT o.entry_ts - CAST(strftime('%s', e.ts) AS INTEGER) d
          FROM outcomes o JOIN signal_events e ON e.id=o.key AND o.kind='signal')
 GROUP BY bucket ORDER BY n DESC
""")
q("token_static.token_created_at in the FUTURE", f"""
SELECT COUNT(*) future_rows, MAX(CAST(token_created_at AS INTEGER)) mx,
       strftime('%Y-%m-%dT%H:%M:%SZ', MAX(CAST(token_created_at AS INTEGER)), 'unixepoch') mx_iso,
       strftime('%Y-%m-%dT%H:%M:%SZ','now') now_iso
  FROM token_static
 WHERE token_created_at IS NOT NULL
   AND CAST(token_created_at AS INTEGER) > strftime('%s','now')
""")
q("negative token_age_h in training_rows", """
SELECT SUM(token_age_h < 0) negative_age, SUM(token_age_h IS NULL) null_age, COUNT(*) total
  FROM training_rows
""")
q("token_created_at AFTER the coin's own first watch window (impossible)", """
SELECT COUNT(*) coins FROM watch_windows w JOIN token_static s
   ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE s.token_created_at IS NOT NULL
   AND CAST(s.token_created_at AS INTEGER) > CAST(strftime('%s', w.first_seen_at) AS INTEGER)
""")
con.close()
