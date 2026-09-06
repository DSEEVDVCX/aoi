import os, sqlite3, config, labeler
from collections import Counter

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()
WIN = config.LABEL_WINDOW_HOURS * 3600

rows = q("""SELECT o.key, o.token_address, COALESCE(o.network_id,'') AS net, o.entry_ts,
                   (SELECT s.price_usd FROM signal_events s WHERE s.id=o.key) AS sig_px
              FROM outcomes o
             WHERE o.kind='signal' AND o.status='no_entry'
               AND o.entry_ts >= ? AND o.is_independent=1""", (config.LIVE_START_TS,))

def bars_for(tok, net, a, b):
    r = con.execute("""SELECT ts,o,h,l,c,h_suspect,l_suspect,c_suspect FROM token_bars
        WHERE token_address=? AND network_id=? AND resolution='5' AND ts>=? AND ts<=? ORDER BY ts""",
        (tok, net, a, b)).fetchall()
    return [dict(x) for x in r if x["h"] is not None and x["l"] is not None and x["c"] is not None]

candles = []; g1_null = 0; g4_null = 0
for r in rows:
    e = r["entry_ts"]
    f = labeler.compute_labels(bars_for(r["token_address"], r["net"], e, e+WIN), e,
                              admission_price_usd=r["sig_px"])
    if f["status"] == "ok":
        candles.append(f["candles_48h"])
        g1_null += f["max_gain_1h"] is None
        g4_null += f["max_gain_4h"] is None
candles.sort(); n = len(candles)
print("=== quality of the 'ok' rows the proposed fix would add (n=%d) ===" % n)
for lbl, i in (("min",0),("p25",n//4),("median",n//2),("p75",3*n//4),("max",n-1)):
    print(f"  candles_48h {lbl:<7}{candles[i]:6d}")
print("  max_gain_1h would be NULL in %d/%d (%.1f%%)" % (g1_null, n, 100*g1_null/n))
print("  max_gain_4h would be NULL in %d/%d (%.1f%%)" % (g4_null, n, 100*g4_null/n))

fv = q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v")[0]["v"]
r = q("""SELECT COUNT(*) n,
                MIN(candles_48h) mn, MAX(candles_48h) mx,
                AVG(candles_48h) avg,
                SUM(max_gain_1h IS NULL) g1null, SUM(max_gain_4h IS NULL) g4null
           FROM training_rows
          WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
            AND is_independent=1 AND feature_version=?""", (fv,))[0]
print("\n=== the existing 10,942-row model population, same metrics ===")
print("  n=%d candles_48h min=%s max=%s avg=%.1f  max_gain_1h NULL=%d  max_gain_4h NULL=%d"
      % (r["n"], r["mn"], r["mx"], r["avg"], r["g1null"], r["g4null"]))

print("\n=== is the decision unlabelled elsewhere too? nearest watch outcome for the same token/net ===")
hit = Counter()
for row in rows:
    w = con.execute("""SELECT status, ABS(entry_ts - ?) d FROM outcomes
                        WHERE kind='watch' AND token_address=? AND network_id=?
                        ORDER BY d LIMIT 1""",
                    (row["entry_ts"], row["token_address"], row["net"])).fetchone()
    if w is None:
        hit["no watch outcome at all"] += 1
    elif w["d"] <= 1800:
        hit["watch outcome within 30min: " + w["status"]] += 1
    elif w["d"] <= 48*3600:
        hit["watch outcome within 48h: " + w["status"]] += 1
    else:
        hit["watch outcome only >48h away"] += 1
for k, v in hit.most_common():
    print(f"  {k:<40}{v}")
con.close()
