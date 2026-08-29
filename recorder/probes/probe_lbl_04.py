import os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()
def show(t, rows):
    print("\n== " + t)
    for r in rows: print("   ", dict(r))
now = int(time.time())
MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND feature_version = CAST(COALESCE("
         "(SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)")
print("LIVE_START_TS =", config.LIVE_START_TS)

# ---- A. flat / degenerate labels
print("\nMODEL rows fully flat (max_gain_48h=0 AND max_drawdown_48h=0) =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_48h=0 AND max_drawdown_48h=0")[0]["c"])
print("MODEL rows fully flat AND final_return_48h=0 =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_48h=0 AND max_drawdown_48h=0 "
  "AND final_return_48h=0")[0]["c"])
show("those flat rows: candles_48h from outcomes", q(
  f"SELECT o.candles_48h, COUNT(*) n FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key "
  f"WHERE {MODEL.replace('kind=','r.kind=').replace('is_live','r.is_live').replace('asset_class','r.asset_class').replace('status=','r.status=').replace('is_independent','r.is_independent').replace('feature_version','r.feature_version')} "
  "AND r.max_gain_48h=0 AND r.max_drawdown_48h=0 GROUP BY 1 ORDER BY n DESC LIMIT 10"))

# ---- B. suspect_bars NULL: is it legacy or current?
show("outcomes suspect_bars IS NULL: labeled_at range", q(
  "SELECT MIN(labeled_at) mn, MAX(labeled_at) mx, COUNT(*) n FROM outcomes WHERE suspect_bars IS NULL"))
show("outcomes suspect_bars NOT NULL: labeled_at range", q(
  "SELECT MIN(labeled_at) mn, MAX(labeled_at) mx, COUNT(*) n FROM outcomes WHERE suspect_bars IS NOT NULL"))
show("MODEL rows suspect_bars IS NULL: entry_ts range", q(
  f"SELECT MIN(entry_ts) mn, MAX(entry_ts) mx, COUNT(*) n FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL"))
show("outcomes NULL suspect_bars by month labeled", q(
  "SELECT substr(labeled_at,1,7) m, COUNT(*) n FROM outcomes WHERE suspect_bars IS NULL GROUP BY 1 ORDER BY 1"))

# ---- C. feature_version staleness by network + model population by network
show("training_rows feature_version x network (signal)", q(
  "SELECT feature_version, COALESCE(network_id,'') net, COUNT(*) n FROM training_rows "
  "WHERE kind='signal' GROUP BY 1,2 ORDER BY 1,n DESC"))
show("MODEL population by network", q(
  f"SELECT COALESCE(network_id,'') net, COUNT(*) n FROM training_rows WHERE {MODEL} GROUP BY 1 ORDER BY n DESC"))
show("outcomes kind=signal status=ok by network (the labelled universe)", q(
  "SELECT COALESCE(network_id,'') net, COUNT(*) n FROM outcomes WHERE kind='signal' AND status='ok' "
  "GROUP BY 1 ORDER BY n DESC"))
show("outcomes signal ok, entry_ts>=LIVE_START, is_independent=1 by network (label supply)", q(
  "SELECT COALESCE(network_id,'') net, COUNT(*) n FROM outcomes WHERE kind='signal' AND status='ok' "
  "AND entry_ts >= ? AND is_independent=1 GROUP BY 1 ORDER BY n DESC", (config.LIVE_START_TS,)))

# ---- D. blocked matured watch windows: why
gate_ok = """EXISTS (SELECT 1 FROM bars_fetch_state s WHERE s.token_address=w.token_address
        AND s.network_id=w.network_id
        AND ((s.last_status='ok' AND s.last_fetch_at >= w.watch_until)
             OR (s.last_status='no_data' AND s.attempts>=3)))"""
mb = now - 48*3600 - 900
show("blocked windows: how far last_fetch_at sits BEFORE watch_until (hours)", q(f"""
 SELECT CASE WHEN s.last_fetch_at IS NULL THEN 'no state row'
   WHEN s.last_fetch_at >= w.watch_until THEN 'not behind'
   WHEN (CAST(strftime('%s',w.watch_until) AS INTEGER)-CAST(strftime('%s',s.last_fetch_at) AS INTEGER))/3600.0 < 24 THEN '<24h behind'
   WHEN (CAST(strftime('%s',w.watch_until) AS INTEGER)-CAST(strftime('%s',s.last_fetch_at) AS INTEGER))/3600.0 < 168 THEN '1-7d behind'
   ELSE '>7d behind' END b, COUNT(*) n
 FROM watch_windows w LEFT JOIN bars_fetch_state s
   ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   AND NOT {gate_ok}
 GROUP BY 1 ORDER BY n DESC""", (mb,)))
print("\nblocked windows that are the NEWEST window for their token =", q(f"""
 SELECT COUNT(*) c FROM watch_windows w
 WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   AND NOT {gate_ok}
   AND w.first_seen_at = (SELECT MAX(w2.first_seen_at) FROM watch_windows w2
        WHERE w2.token_address=w.token_address AND w2.network_id=w.network_id)""", (mb,))[0]["c"])
show("blocked windows by network", q(f"""
 SELECT w.network_id, w.is_control, COUNT(*) n FROM watch_windows w
 WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   AND NOT {gate_ok} GROUP BY 1,2 ORDER BY n DESC""", (mb,)))
show("blocked windows: oldest first_seen_at", q(f"""
 SELECT MIN(w.first_seen_at) oldest, MAX(w.first_seen_at) newest FROM watch_windows w
 WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   AND NOT {gate_ok}""", (mb,)))
# control-arm completeness
show("control arm: windows vs labelled", q("""
 SELECT COUNT(*) windows,
   SUM(CASE WHEN EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
     AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at) THEN 1 ELSE 0 END) labelled
 FROM watch_windows w WHERE w.is_control=1
   AND CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?""", (mb,)))
show("signal arm (design_version>=3): windows vs labelled", q("""
 SELECT COUNT(*) windows,
   SUM(CASE WHEN EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
     AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at) THEN 1 ELSE 0 END) labelled
 FROM watch_windows w WHERE w.is_control=0 AND w.design_version>=3
   AND CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?""", (mb,)))

# ---- E. orphan watch outcomes context
show("watch_windows first_seen_at range", q("SELECT MIN(first_seen_at) mn, MAX(first_seen_at) mx FROM watch_windows"))
show("orphan watch outcomes entry_ts range", q("""
 SELECT MIN(o.entry_ts) mn, MAX(o.entry_ts) mx, COUNT(*) n FROM outcomes o WHERE o.kind='watch'
 AND NOT EXISTS (SELECT 1 FROM watch_windows w
   WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key)"""))
print("\norphan watch outcomes that HAVE a training_rows row =", q("""
 SELECT COUNT(*) c FROM outcomes o JOIN training_rows r ON r.kind=o.kind AND r.key=o.key
 WHERE o.kind='watch' AND NOT EXISTS (SELECT 1 FROM watch_windows w
   WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key)""")[0]["c"])
print("orphan watch outcomes in phase1_watch_outcomes view =", q("""
 SELECT COUNT(*) c FROM phase1_watch_outcomes o WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
   WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key)""")[0]["c"])

# ---- F. the 49 unlabeled matured signals: how old
show("unlabeled matured signal_events age", q("""
 SELECT MIN(s.recorded_at) oldest, MAX(s.recorded_at) newest, COUNT(*) n FROM signal_events s
 WHERE s.recorded_at IS NOT NULL AND CAST(strftime('%s',s.recorded_at) AS INTEGER) <= ?
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=s.id)""", (mb,)))
show("unlabeled matured signal_events by month", q("""
 SELECT substr(s.recorded_at,1,7) m, COUNT(*) n FROM signal_events s
 WHERE s.recorded_at IS NOT NULL AND CAST(strftime('%s',s.recorded_at) AS INTEGER) <= ?
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=s.id)
 GROUP BY 1 ORDER BY 1""", (mb,)))
show("labeler heartbeat meta", q("SELECT key,value FROM meta WHERE key LIKE '%label%' ORDER BY key"))
con.close()
