import os, sqlite3, config, labeler

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

WIN = config.LABEL_WINDOW_HOURS * 3600

rows = q("""SELECT o.key, o.token_address, COALESCE(o.network_id,'') AS net, o.entry_ts,
                   o.signal_type,
                   (SELECT s.price_usd FROM signal_events s WHERE s.id = o.key) AS sig_px,
                   (SELECT w.admission_price_usd FROM watch_windows w
                     WHERE w.token_address=o.token_address AND w.network_id=o.network_id
                       AND CAST(strftime('%s', w.first_seen_at) AS INTEGER) <= o.entry_ts
                     ORDER BY w.first_seen_at DESC LIMIT 1) AS watch_px
              FROM outcomes o
             WHERE o.kind='signal' AND o.status='no_entry'
               AND o.entry_ts >= ? AND o.is_independent=1""", (config.LIVE_START_TS,))
print("population re-selected:", len(rows))

def bars_for(tok, net, a, b):
    r = con.execute(
        """SELECT ts,o,h,l,c,h_suspect,l_suspect,c_suspect FROM token_bars
            WHERE token_address=? AND network_id=? AND resolution='5'
              AND ts>=? AND ts<=? ORDER BY ts""", (tok, net, a, b)).fetchall()
    return [dict(x) for x in r if x["h"] is not None and x["l"] is not None and x["c"] is not None]

from collections import Counter
strict = Counter(); withfb = Counter(); nopx = 0
bars_at_all = 0; bars_in_win = 0; zero_bar_rows = 0
recoverable_ok = []
for r in rows:
    e = r["entry_ts"]
    bars = bars_for(r["token_address"], r["net"], e, e + WIN)
    anyb = con.execute("SELECT 1 FROM token_bars WHERE token_address=? AND network_id=? AND resolution='5' LIMIT 1",
                       (r["token_address"], r["net"])).fetchone()
    if anyb: bars_at_all += 1
    if bars: bars_in_win += 1
    else: zero_bar_rows += 1
    s = labeler.compute_labels(bars, e)
    strict[s["status"]] += 1
    px = r["sig_px"]
    if px is None:
        nopx += 1
        withfb["NO_PRICE_available"] += 1
        continue
    f = labeler.compute_labels(bars, e, admission_price_usd=px)
    withfb[f["status"]] += 1
    if f["status"] == "ok":
        recoverable_ok.append(r["key"])

print("\n=== recompute of the 925 with the CURRENT strict code (sanity: must be all no_entry) ===")
print(dict(strict))
print("\n=== the same rows recomputed WITH the admission-price fallback (signal_events.price_usd) ===")
print(dict(withfb))
print("rows whose signal_events.price_usd is NULL (fallback impossible):", nopx)
print("\n=== bar availability ===")
print("has ANY 5m bar row for that token/net, ever :", bars_at_all, f"({100*bars_at_all/len(rows):.1f}%)")
print("has >=1 usable bar inside [entry, entry+48h] :", bars_in_win, f"({100*bars_in_win/len(rows):.1f}%)")
print("has ZERO usable bars in the window          :", zero_bar_rows, f"({100*zero_bar_rows/len(rows):.1f}%)")
print("\n=> rows that the claimed fix would turn into a LABELLED ok row:", len(recoverable_ok))

print("\n=== watch arm: no_entry rate split by whether an admission price existed ===")
for r in q("""SELECT (w.admission_price_usd IS NOT NULL) AS has_px,
                     COUNT(*) AS n,
                     SUM(o.status='no_entry') AS no_entry,
                     SUM(o.status='ok') AS ok,
                     SUM(o.status='no_bars') AS no_bars,
                     SUM(o.status='incomplete') AS incomplete
                FROM outcomes o
                LEFT JOIN watch_windows w
                  ON w.token_address=o.token_address AND w.network_id=o.network_id
                 AND CAST(strftime('%s', w.first_seen_at) AS INTEGER)=o.entry_ts
               WHERE o.kind='watch' GROUP BY 1"""):
    print(dict(r))
con.close()
