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
now = int(time.time()); mb = now - 48*3600 - 900
FV = "feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND " + FV)

# ---- exact effect of the consumers' `WHERE suspect_bars = 0` filter
print("MODEL total                        =", q(f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL}")[0]["c"])
print("MODEL suspect_bars = 0  (what train_pipeline/backtest/pattern_analysis get) =",
      q(f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND suspect_bars = 0")[0]["c"])
print("MODEL suspect_bars IS NULL (silently dropped by `= 0`) =",
      q(f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL")[0]["c"])
print("MODEL suspect_bars > 0 (the rows the filter MEANT to drop) =",
      q(f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND suspect_bars > 0")[0]["c"])
show("MODEL suspect_bars IS NULL: date span of entry_ts", q(
  f"SELECT datetime(MIN(entry_ts),'unixepoch') first_entry, datetime(MAX(entry_ts),'unixepoch') last_entry,"
  f" COUNT(*) n FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL"))
show("MODEL suspect_bars=0: date span of entry_ts", q(
  f"SELECT datetime(MIN(entry_ts),'unixepoch') first_entry, datetime(MAX(entry_ts),'unixepoch') last_entry,"
  f" COUNT(*) n FROM training_rows WHERE {MODEL} AND suspect_bars = 0"))
show("split x suspect_bars NULL (does the drop skew the split?)", q(
  f"SELECT split, SUM(suspect_bars IS NULL) dropped, COUNT(*) total FROM training_rows WHERE {MODEL} GROUP BY 1"))

# ---- is the blocked-window backlog transient or permanent?
gate_ok = """EXISTS (SELECT 1 FROM bars_fetch_state s WHERE s.token_address=w.token_address
        AND s.network_id=w.network_id
        AND ((s.last_status='ok' AND s.last_fetch_at >= w.watch_until)
             OR (s.last_status='no_data' AND s.attempts>=3)))"""
BLOCKED = f"""FROM watch_windows w
 WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER) <= {mb}
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
   AND NOT {gate_ok}"""
print("\nblocked matured windows total =", q(f"SELECT COUNT(*) c {BLOCKED}")[0]["c"])
show("blocked windows: is the token still ACTIVE in watchlist? (active=1 => may still resolve)", q(
  f"""SELECT COALESCE(wl.active,-1) active, COUNT(*) n {BLOCKED}
      AND 1=1 GROUP BY 1""".replace("FROM watch_windows w",
      "FROM watch_windows w LEFT JOIN watchlist wl ON wl.token_address=w.token_address AND wl.network_id=w.network_id")))
show("blocked windows: watch_until already in the past? (past => fetching has stopped)", q(
  f"SELECT CASE WHEN CAST(strftime('%s',w.watch_until) AS INTEGER) < {now} THEN 'watch_until PAST'"
  f" ELSE 'still open' END b, COUNT(*) n {BLOCKED} GROUP BY 1"))
show("blocked windows: bars_fetch_state last_status x attempts", q(
  f"""SELECT COALESCE(s.last_status,'NO_ROW') st, CASE WHEN s.attempts IS NULL THEN 'n/a'
        WHEN s.attempts>=3 THEN '>=3' ELSE '<3' END att, COUNT(*) n {BLOCKED} GROUP BY 1,2 ORDER BY n DESC"""
  .replace("FROM watch_windows w",
      "FROM watch_windows w LEFT JOIN bars_fetch_state s ON s.token_address=w.token_address AND s.network_id=w.network_id")))
# do these tokens have bars covering the window anyway? (label is computable, just gated)
show("blocked windows: do 5m bars exist covering >=90% of the 48h window?", q(
  f"""SELECT CASE WHEN cnt IS NULL OR cnt=0 THEN 'no bars at all' WHEN cnt>=518 THEN 'bars cover >=90%'
        WHEN cnt>=288 THEN 'bars cover 50-90%' ELSE 'bars cover <50%' END b, COUNT(*) n FROM (
      SELECT (SELECT COUNT(*) FROM token_bars b WHERE b.token_address=w.token_address
                AND b.network_id=w.network_id AND b.resolution='5'
                AND b.ts >= CAST(strftime('%s',w.first_seen_at) AS INTEGER)
                AND b.ts <= CAST(strftime('%s',w.first_seen_at) AS INTEGER)+48*3600) cnt
      {BLOCKED} LIMIT 400) GROUP BY 1 ORDER BY n DESC"""))

# ---- no_entry: labels lost outright
show("outcomes status='no_entry' by kind (label never produced; build skips them)", q(
  "SELECT kind, COUNT(*) n FROM outcomes WHERE status='no_entry' GROUP BY 1"))
print("\nsignal outcomes total =", q("SELECT COUNT(*) c FROM outcomes WHERE kind='signal'")[0]["c"])
show("no_entry signals: live & independent (would-be model rows lost)", q(
  "SELECT COUNT(*) n FROM outcomes WHERE kind='signal' AND status='no_entry' AND entry_ts >= ? "
  "AND is_independent=1", (config.LIVE_START_TS,)))
show("no_entry signals by network", q(
  "SELECT COALESCE(network_id,'') net, COUNT(*) n FROM outcomes WHERE kind='signal' "
  "AND status='no_entry' GROUP BY 1 ORDER BY n DESC"))
con.close()
