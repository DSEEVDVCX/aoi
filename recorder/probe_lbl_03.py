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
LBL = ["final_return_48h","max_gain_1h","max_gain_4h","max_gain_24h","max_gain_48h",
       "max_drawdown_48h","time_to_peak_h","is_rug"]
MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND feature_version = CAST(COALESCE("
         "(SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)")

# ---------- 3a. NULL labels where status='ok'  (outcomes)
sel = ", ".join(f"SUM({c} IS NULL) AS n_{c}" for c in LBL)
show("outcomes status='ok': NULL label counts (all kinds)",
     q(f"SELECT COUNT(*) total, {sel} FROM outcomes WHERE status='ok'"))
show("outcomes status='ok' kind='signal': NULL label counts",
     q(f"SELECT COUNT(*) total, {sel} FROM outcomes WHERE status='ok' AND kind='signal'"))
show("outcomes status='ok': also entry_px/candles/bars_truncated NULL",
     q("SELECT COUNT(*) total, SUM(entry_px IS NULL) n_entry_px, SUM(candles_48h IS NULL) n_candles,"
       " SUM(suspect_bars IS NULL) n_suspect, SUM(bars_truncated IS NULL) n_trunc,"
       " SUM(split IS NULL) n_split FROM outcomes WHERE status='ok'"))

# ---------- 3b. NULL labels where status='ok' (training_rows) + model population
show("training_rows status='ok': NULL label counts",
     q(f"SELECT COUNT(*) total, {sel} FROM training_rows WHERE status='ok'"))
show("MODEL POPULATION: NULL label counts",
     q(f"SELECT COUNT(*) total, {sel} FROM training_rows WHERE {MODEL}"))
show("training_rows status='no_bars': NULL label counts",
     q(f"SELECT COUNT(*) total, {sel} FROM training_rows WHERE status='no_bars'"))

# ---------- 4. suspect_bars
show("outcomes suspect_bars buckets", q(
  "SELECT CASE WHEN suspect_bars IS NULL THEN 'NULL' WHEN suspect_bars=0 THEN '0' "
  "WHEN suspect_bars<=2 THEN '1-2' WHEN suspect_bars<=10 THEN '3-10' ELSE '>10' END b, "
  "COUNT(*) n FROM outcomes GROUP BY 1 ORDER BY n DESC"))
show("MODEL POPULATION suspect_bars buckets", q(
  f"SELECT CASE WHEN suspect_bars IS NULL THEN 'NULL' WHEN suspect_bars=0 THEN '0' "
  "WHEN suspect_bars<=2 THEN '1-2' WHEN suspect_bars<=10 THEN '3-10' ELSE '>10' END b, "
  f"COUNT(*) n FROM training_rows WHERE {MODEL} GROUP BY 1 ORDER BY n DESC"))
print("\nMODEL rows with suspect_bars>0 =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND suspect_bars>0")[0]["c"])

# ---------- 5. label value sanity on the MODEL population
show("is_rug distribution (MODEL)", q(
  f"SELECT is_rug, COUNT(*) n FROM training_rows WHERE {MODEL} GROUP BY 1"))
show("is_rug distribution (outcomes kind=signal status=ok)", q(
  "SELECT is_rug, COUNT(*) n FROM outcomes WHERE kind='signal' AND status='ok' GROUP BY 1"))
show("final_return_48h stats (MODEL)", q(
  f"SELECT MIN(final_return_48h) mn, MAX(final_return_48h) mx, AVG(final_return_48h) avg, COUNT(*) n "
  f"FROM training_rows WHERE {MODEL}"))
med = q(f"SELECT final_return_48h v FROM training_rows WHERE {MODEL} AND final_return_48h IS NOT NULL "
        f"ORDER BY final_return_48h LIMIT 1 OFFSET (SELECT COUNT(*)/2 FROM training_rows WHERE {MODEL} "
        f"AND final_return_48h IS NOT NULL)")
print("median final_return_48h (MODEL) =", med[0]["v"] if med else None)
show("final_return_48h extreme buckets (MODEL)", q(
  f"SELECT CASE WHEN final_return_48h <= -1.0 THEN '<=-100%' WHEN final_return_48h = -1.0 THEN 'exactly -100%'"
  " WHEN final_return_48h < -0.999 THEN '(-100%,-99.9%]' WHEN final_return_48h > 100 THEN '>+10000%'"
  " WHEN final_return_48h > 10 THEN '+1000%..+10000%' ELSE 'normal' END b, COUNT(*) n "
  f"FROM training_rows WHERE {MODEL} GROUP BY 1 ORDER BY n DESC"))
print("\nMODEL final_return_48h exactly -1.0 =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND final_return_48h = -1.0")[0]["c"])
print("MODEL final_return_48h < -1.0 (impossible) =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND final_return_48h < -1.0")[0]["c"])
print("MODEL max_gain_48h < -1.0 (impossible) =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_48h < -1.0")[0]["c"])
print("MODEL max_drawdown_48h < -1.0 (impossible) =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_drawdown_48h < -1.0")[0]["c"])

show("max_gain_48h stats (MODEL)", q(
  f"SELECT MIN(max_gain_48h) mn, MAX(max_gain_48h) mx, SUM(max_gain_48h<0) neg, COUNT(*) n "
  f"FROM training_rows WHERE {MODEL}"))
show("max_drawdown_48h stats + sign (MODEL)", q(
  f"SELECT MIN(max_drawdown_48h) mn, MAX(max_drawdown_48h) mx, SUM(max_drawdown_48h>0) positive, "
  f"SUM(max_drawdown_48h=0) zero, SUM(max_drawdown_48h<0) negative FROM training_rows WHERE {MODEL}"))
show("time_to_peak_h range (MODEL)", q(
  f"SELECT MIN(time_to_peak_h) mn, MAX(time_to_peak_h) mx, SUM(time_to_peak_h<0) lt0, "
  f"SUM(time_to_peak_h>48) gt48, SUM(time_to_peak_h=0) eq0 FROM training_rows WHERE {MODEL}"))

# monotonicity of the gain ladder
print("\nMODEL max_gain_48h < final_return_48h =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_48h < final_return_48h")[0]["c"])
print("MODEL max_gain_24h < max_gain_1h =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_24h < max_gain_1h")[0]["c"])
print("MODEL max_gain_48h < max_gain_24h =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_48h < max_gain_24h")[0]["c"])
print("MODEL max_drawdown_48h > max_gain_48h =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_drawdown_48h > max_gain_48h")[0]["c"])
print("MODEL is_rug=1 but final_return_48h > -0.90 (threshold breach) =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND is_rug=1 AND final_return_48h > -0.90")[0]["c"])
print("MODEL is_rug=0 but final_return_48h <= -0.90 =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND is_rug=0 AND final_return_48h <= -0.90")[0]["c"])

# ---------- 6. leakage: labels on windows that have NOT closed
print("\n--- leakage the other way ---")
print("outcomes with entry_ts > now-48h (window not closed) =", q(
  "SELECT COUNT(*) c FROM outcomes WHERE entry_ts > ?", (now-48*3600,))[0]["c"])
print("outcomes with entry_ts > now-48h-900s (labeler maturity bar) =", q(
  "SELECT COUNT(*) c FROM outcomes WHERE entry_ts > ?", (now-48*3600-900,))[0]["c"])
print("outcomes with entry_ts > now (future entry) =", q(
  "SELECT COUNT(*) c FROM outcomes WHERE entry_ts > ?", (now,))[0]["c"])
print("training_rows with entry_ts > now-48h and any label NOT NULL =", q(
  "SELECT COUNT(*) c FROM training_rows WHERE entry_ts > ? AND ("
  + " OR ".join(f"{c} IS NOT NULL" for c in LBL) + ")", (now-48*3600,))[0]["c"])
print("outcomes labeled_at earlier than entry_ts+48h (labeled too early) =", q(
  "SELECT COUNT(*) c FROM outcomes WHERE CAST(strftime('%s',labeled_at) AS INTEGER) < entry_ts + 48*3600")[0]["c"])
show("that, by kind", q(
  "SELECT kind, COUNT(*) n, MIN(CAST(strftime('%s',labeled_at) AS INTEGER) - entry_ts) min_gap_s "
  "FROM outcomes WHERE CAST(strftime('%s',labeled_at) AS INTEGER) < entry_ts + 48*3600 GROUP BY 1"))
con.close()
