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
mature_before = now - config.LABEL_WINDOW_HOURS*3600 - config.LABEL_MARGIN_SECONDS
print("now epoch", now, "mature_before", mature_before, "LABEL_WINDOW_HOURS", config.LABEL_WINDOW_HOURS)

# ---------- 1. the 168 orphan watch outcomes
show("orphan watch outcomes sample", q(
    "SELECT o.key, o.token_address, o.network_id, o.entry_ts, o.design_version, o.status, o.labeled_at "
    "FROM outcomes o WHERE o.kind='watch' AND NOT EXISTS ("
    " SELECT 1 FROM watch_windows w WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key) "
    "LIMIT 8"))
show("orphan watch outcomes by design_version", q(
    "SELECT o.design_version, o.status, COUNT(*) n FROM outcomes o WHERE o.kind='watch' AND NOT EXISTS ("
    " SELECT 1 FROM watch_windows w WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key) "
    "GROUP BY 1,2"))
# does a case-insensitive / trimmed match rescue them?
print("\norphans rescued by NOCASE match =", q(
    "SELECT COUNT(*) c FROM outcomes o WHERE o.kind='watch' AND NOT EXISTS ("
    " SELECT 1 FROM watch_windows w WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key) "
    "AND EXISTS (SELECT 1 FROM watch_windows w2 WHERE "
    " w2.token_address||':'||w2.network_id||':'||w2.first_seen_at = o.key COLLATE NOCASE)")[0]["c"])

# ---------- 2. closed watch_windows with NO outcome
tot_closed = q("SELECT COUNT(*) c FROM watch_windows w WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?",
               (mature_before,))[0]["c"]
print("\nwatch_windows matured (first_seen+48h+15m passed) =", tot_closed)
no_out = q("""SELECT COUNT(*) c FROM watch_windows w
   WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
     AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
        AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)""", (mature_before,))[0]["c"]
print("  ... of which NO outcome row =", no_out)
# split that by the bars_fetch_state gate the labeler requires
gate = """EXISTS (SELECT 1 FROM bars_fetch_state s WHERE s.token_address=w.token_address
        AND s.network_id=w.network_id
        AND ((s.last_status='ok' AND s.last_fetch_at >= w.watch_until)
             OR (s.last_status='no_data' AND s.attempts>=3)))"""
print("  ... unlabeled AND bars-gate SATISFIED (real labeler backlog) =", q(f"""
  SELECT COUNT(*) c FROM watch_windows w
   WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
     AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
        AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
     AND {gate}""", (mature_before,))[0]["c"])
print("  ... unlabeled AND bars-gate NOT satisfied (blocked forever?) =", q(f"""
  SELECT COUNT(*) c FROM watch_windows w
   WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
     AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
        AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
     AND NOT {gate}""", (mature_before,))[0]["c"])
show("unlabeled matured windows by month", q("""
  SELECT substr(w.first_seen_at,1,7) m, w.is_control, COUNT(*) n FROM watch_windows w
   WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
     AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
        AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   GROUP BY 1,2 ORDER BY 1""", (mature_before,)))
show("unlabeled matured windows: bars_fetch_state status", q(f"""
  SELECT COALESCE(s.last_status,'NO_ROW') st, COUNT(*) n FROM watch_windows w
   LEFT JOIN bars_fetch_state s ON s.token_address=w.token_address AND s.network_id=w.network_id
   WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= ?
     AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
        AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   GROUP BY 1 ORDER BY n DESC""", (mature_before,)))

# ---------- 3. matured signals with no outcome
print("\nsignal_events matured (recorded_at <= mature_before) =", q(
  "SELECT COUNT(*) c FROM signal_events s WHERE s.recorded_at IS NOT NULL "
  "AND CAST(strftime('%s',s.recorded_at) AS INTEGER) <= ?", (mature_before,))[0]["c"])
print("  ... with NO outcome row =", q(
  "SELECT COUNT(*) c FROM signal_events s WHERE s.recorded_at IS NOT NULL "
  "AND CAST(strftime('%s',s.recorded_at) AS INTEGER) <= ? "
  "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=s.id)", (mature_before,))[0]["c"])
print("  signal_events with recorded_at IS NULL =", q(
  "SELECT COUNT(*) c FROM signal_events WHERE recorded_at IS NULL")[0]["c"])

# ---------- 4. outcomes with no training row / stale feature_version
show("training_rows feature_version dist", q(
  "SELECT feature_version, kind, COUNT(*) n FROM training_rows GROUP BY 1,2 ORDER BY 1,2"))
print("\noutcomes(status ok|no_bars) with NO training row at all =", q(
  "SELECT COUNT(*) c FROM outcomes o WHERE o.status IN ('ok','no_bars') AND NOT EXISTS "
  "(SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key)")[0]["c"])
show("that gap by kind + network", q(
  "SELECT o.kind, COALESCE(o.network_id,'') net, COUNT(*) n FROM outcomes o "
  "WHERE o.status IN ('ok','no_bars') AND NOT EXISTS "
  "(SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key) "
  "GROUP BY 1,2 ORDER BY n DESC LIMIT 15"))
print("\nmeta evm_ledger_rebuild_required =", q("SELECT value FROM meta WHERE key='evm_ledger_rebuild_required'").__len__() and
      [dict(r) for r in q("SELECT key,value FROM meta WHERE key IN ('evm_ledger_rebuild_required','evm_training_rebuild_started')")])
con.close()
