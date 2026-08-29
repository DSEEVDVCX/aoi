import os, sqlite3, config, labeler
from collections import Counter

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()
WIN = config.LABEL_WINDOW_HOURS * 3600

rows = q("""SELECT o.key, o.token_address, COALESCE(o.network_id,'') AS net, o.entry_ts,
                   (SELECT s.price_usd FROM signal_events s WHERE s.id=o.key) AS sig_px,
                   (SELECT s.ts FROM signal_events s WHERE s.id=o.key) AS src_ts,
                   (SELECT s.recorded_at FROM signal_events s WHERE s.id=o.key) AS rec_at
              FROM outcomes o
             WHERE o.kind='signal' AND o.status='no_entry'
               AND o.entry_ts >= ? AND o.is_independent=1""", (config.LIVE_START_TS,))

def bars_for(tok, net, a, b):
    r = con.execute("""SELECT ts,o,h,l,c,h_suspect,l_suspect,c_suspect FROM token_bars
        WHERE token_address=? AND network_id=? AND resolution='5' AND ts>=? AND ts<=? ORDER BY ts""",
        (tok, net, a, b)).fetchall()
    return [dict(x) for x in r if x["h"] is not None and x["l"] is not None and x["c"] is not None]

strict_ok = set(); fb_ok = set(); fb_status = {}
firstlag = []
for r in rows:
    e = r["entry_ts"]
    bars = bars_for(r["token_address"], r["net"], e, e + WIN)
    if labeler.compute_labels(bars, e)["status"] == "ok":
        strict_ok.add(r["key"])
    f = labeler.compute_labels(bars, e, admission_price_usd=r["sig_px"])
    fb_status[r["key"]] = f["status"]
    if f["status"] == "ok":
        fb_ok.add(r["key"])
        good = [b for b in bars if b["ts"] > e and not b.get("c_suspect")]
        if good:
            firstlag.append((good[0]["ts"] - e) / 60.0)

print("strict-code recompute now yields ok  :", len(strict_ok))
print("fallback recompute yields ok         :", len(fb_ok))
print("strict_ok NOT inside fb_ok           :", len(strict_ok - fb_ok))
print("INCREMENTAL rows the fallback adds beyond a plain re-label:", len(fb_ok - strict_ok))

firstlag.sort()
if firstlag:
    n = len(firstlag)
    print("\nfirst usable bar lag after entry_ts, minutes, for the fallback-ok rows (n=%d)" % n)
    for lbl, i in (("min",0),("p25",n//4),("median",n//2),("p75",3*n//4),("p95",int(n*.95)),("max",n-1)):
        print(f"  {lbl:<7}{firstlag[i]:10.1f}")
    print("  share with first bar > 30 min (i.e. the check the fallback would skip): %.1f%%"
          % (100*sum(1 for x in firstlag if x > 30)/n))

print("\n=== is the feed price contemporaneous with entry_ts? (signal ts -> recorded_at lag) ===")
lags = []
for r in rows:
    if r["src_ts"] and r["rec_at"]:
        try:
            a = labeler._epoch(r["src_ts"]); b = labeler._epoch(r["rec_at"])
            lags.append((b - a)/60.0)
        except Exception:
            pass
lags.sort(); n = len(lags)
print("n =", n)
for lbl, i in (("min",0),("p25",n//4),("median",n//2),("p75",3*n//4),("p95",int(n*.95)),("max",n-1)):
    print(f"  {lbl:<7}{lags[i]:10.1f} min")
print("  share where the feed price is >30 min older than entry_ts: %.1f%%"
      % (100*sum(1 for x in lags if x > 30)/n))

print("\n=== do 'no_bars' outcomes reach the model population at all? ===")
fv = q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v")[0]["v"]
for r in q("""SELECT status, COUNT(*) c FROM training_rows
               WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND is_independent=1
                 AND feature_version=? GROUP BY 1""", (fv,)):
    print("  training_rows status", r["status"], r["c"])
print("  -> the model-population filter requires status='ok', so a no_bars row is excluded anyway")

print("\n=== what would the 572 become? (fallback statuses) ===")
print(dict(Counter(fb_status.values())))
con.close()
