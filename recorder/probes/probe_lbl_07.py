import os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
one = lambda s, p=(): con.execute(s, p).fetchone()[0]
now = int(time.time()); mb = now - 48*3600 - 900
FV = "feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
M = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1 AND " + FV)
MJ = ("r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok' AND r.is_independent=1 AND r." + FV)
gate = """EXISTS (SELECT 1 FROM bars_fetch_state s WHERE s.token_address=w.token_address
  AND s.network_id=w.network_id AND ((s.last_status='ok' AND s.last_fetch_at >= w.watch_until)
  OR (s.last_status='no_data' AND s.attempts>=3)))"""
print("SNAPSHOT", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
print("model_pop                 =", one(f"SELECT COUNT(*) FROM training_rows WHERE {M}"))
print("  suspect_bars = 0        =", one(f"SELECT COUNT(*) FROM training_rows WHERE {M} AND suspect_bars=0"))
print("  suspect_bars IS NULL    =", one(f"SELECT COUNT(*) FROM training_rows WHERE {M} AND suspect_bars IS NULL"))
print("  suspect_bars > 0        =", one(f"SELECT COUNT(*) FROM training_rows WHERE {M} AND suspect_bars>0"))
print("  bars_truncated=1        =", one(f"SELECT COUNT(*) FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key WHERE {MJ} AND o.bars_truncated=1"))
print("  last_bar_lag_h > 24     =", one(f"SELECT COUNT(*) FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key WHERE {MJ} AND o.last_bar_lag_h>24"))
print("  last_bar_lag_h > 40     =", one(f"SELECT COUNT(*) FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key WHERE {MJ} AND o.last_bar_lag_h>40"))
print("  candles_48h < 200       =", one(f"SELECT COUNT(*) FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key WHERE {MJ} AND o.candles_48h<200"))
print("model-eligible at fv != current =", one("SELECT COUNT(*) FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1 AND NOT ("+FV+")"))
print("labelled live indep signal outcomes w/o current-fv row =", one("SELECT COUNT(*) FROM outcomes o WHERE o.kind='signal' AND o.status='ok' AND o.entry_ts>=? AND o.is_independent=1 AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key AND r."+FV+")", (config.LIVE_START_TS,)))
print("outcomes ok|no_bars with no training row at all =", one("SELECT COUNT(*) FROM outcomes o WHERE o.status IN ('ok','no_bars') AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key)"))
print("matured watch_windows      =", one("SELECT COUNT(*) FROM watch_windows w WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER)<=?", (mb,)))
print("  no outcome row           =", one("SELECT COUNT(*) FROM watch_windows w WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER)<=? AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch' AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at)", (mb,)))
print("  no outcome AND gate unsatisfiable =", one(f"SELECT COUNT(*) FROM watch_windows w WHERE CAST(strftime('%s',w.first_seen_at) AS INTEGER)<=? AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch' AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at) AND NOT {gate}", (mb,)))
print("orphan watch outcomes      =", one("SELECT COUNT(*) FROM outcomes o WHERE o.kind='watch' AND NOT EXISTS (SELECT 1 FROM watch_windows w WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at=o.key)"))
print("no_entry signals live+indep =", one("SELECT COUNT(*) FROM outcomes WHERE kind='signal' AND status='no_entry' AND entry_ts>=? AND is_independent=1", (config.LIVE_START_TS,)))
print("training_rows total       =", one("SELECT COUNT(*) FROM training_rows"))
print("outcomes total            =", one("SELECT COUNT(*) FROM outcomes"))
print("watch_windows total       =", one("SELECT COUNT(*) FROM watch_windows"))
con.close()
