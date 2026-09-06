import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

print("=== 5. the flag=0 & bars_history_h IS NULL rows ===")
rows = con.execute(f"""
  SELECT key, token_address, network_id, entry_ts, feature_version, built_at,
         dist_from_ath, ath_history_days, bars_count_24h, bar_vol_24h,
         ret_24h_before, suspect_bars, token_age_h
    FROM training_rows WHERE {POP} AND ath_history_complete=0
     AND bars_history_h IS NULL""").fetchall()
for r in rows:
    print(" ", dict(r))
print("  count =", len(rows))

print("\n=== 6. for each no-bars row: what bars actually exist ===")
for r in rows:
    tok, net, t0 = r["token_address"], r["network_id"], r["entry_ts"]
    a = con.execute(
        """SELECT resolution, COUNT(*) n, SUM(c_suspect) csus, SUM(h_suspect) hsus,
                  MIN(ts) mn, MAX(ts) mx
             FROM token_bars WHERE token_address=? AND network_id=?
            GROUP BY resolution""", (tok, net)).fetchall()
    st = con.execute(
        """SELECT resolution, last_status, candles, oldest_ts
             FROM historical_bars_state WHERE token_address=? AND network_id=?""",
        (tok, net)).fetchall()
    d = con.execute(
        """SELECT MIN(b.ts) first_ts, MAX(b.h) ath, COUNT(*) n
             FROM token_bars b JOIN historical_bars_state s
               ON s.token_address=b.token_address AND s.network_id=b.network_id
              AND s.resolution=b.resolution AND s.last_status='ok'
            WHERE b.token_address=? AND b.network_id=? AND b.resolution='1D'
              AND b.ts + 86400 <= ? AND b.h_suspect=0""", (tok, net, t0)).fetchone()
    # the exact intraday query features.py runs
    intr = con.execute(
        f"""SELECT COUNT(*) n FROM token_bars
             WHERE token_address=? AND network_id=? AND resolution=?
               AND ts + CAST(resolution AS INTEGER)*60 <= ? AND c_suspect=0""",
        (tok, net, config.BARS_RESOLUTION, t0)).fetchone()
    print(f"  {tok} net={net} entry_ts={t0}")
    print(f"     token_bars by res : {[dict(x) for x in a]}")
    print(f"     hist_bars_state   : {[dict(x) for x in st]}")
    print(f"     intraday query n  : {intr['n']}  (res={config.BARS_RESOLUTION})")
    print(f"     1D branch would   : {dict(d)}")

print("\n=== 7. other populations ===")
for name, w in (
    ("all training_rows", "1=1"),
    ("kind=signal only", "kind='signal'"),
    ("signal is_live=0", "kind='signal' AND is_live=0"),
    ("signal live stale fv", f"kind='signal' AND is_live=1 AND feature_version <> {FV}"),
):
    r = con.execute(f"""SELECT COUNT(*) n, SUM(ath_history_complete IS NULL) cnull,
                               SUM(ath_history_complete=0) c0,
                               SUM(ath_history_complete=0 AND bars_history_h IS NULL) c0nb,
                               SUM(ath_history_complete=1) c1
                          FROM training_rows WHERE {w}""").fetchone()
    print(f"  {name:<22} {dict(r)}")

print("\n=== 8. flag=0 rows with entire family-d NULL ===")
cols = ["ret_1h_before","ret_4h_before","ret_24h_before","ret_7d_before",
        "vol_24h_before","flat_ratio_24h","up_candle_ratio_24h","dist_from_ath",
        "ath_history_days","bars_history_h","bars_count_24h","bar_vol_1h",
        "bar_vol_24h","vol_surge_1h"]
cond = " AND ".join(f"{c} IS NULL" for c in cols)
for lbl, extra in (("flag=0", "AND ath_history_complete=0"),
                   ("any flag", ""),
                   ("flag NULL", "AND ath_history_complete IS NULL")):
    r = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {POP} {extra} AND {cond}").fetchone()
    print(f"  all-14-NULL & {lbl:<9}: {r['n']}")

print("\n=== 9. would-be NULL share: how many rows total lack intraday bars ===")
r = con.execute(f"""SELECT COUNT(*) n, SUM(bars_history_h IS NULL) nb
                      FROM training_rows WHERE {POP}""").fetchone()
print(dict(r), " => share with no intraday bars = %.4f%%" % (100.0*r["nb"]/r["n"]))

con.close()
