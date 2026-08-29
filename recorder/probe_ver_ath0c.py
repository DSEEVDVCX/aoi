import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

# ALL rows in the whole table where flag=0 and no intraday bars (the 28)
rows = con.execute("""
  SELECT kind, key, token_address, network_id, entry_ts, is_live, feature_version
    FROM training_rows
   WHERE ath_history_complete=0 AND bars_history_h IS NULL""").fetchall()
print("rows table-wide with flag=0 & bars_history_h NULL:", len(rows))

diverge = 0
same = 0
for r in rows:
    tok, net, t0 = r["token_address"], r["network_id"], r["entry_ts"]
    d = con.execute(
        """SELECT MAX(b.h) ath, COUNT(*) n
             FROM token_bars b JOIN historical_bars_state s
               ON s.token_address=b.token_address AND s.network_id=b.network_id
              AND s.resolution=b.resolution AND s.last_status='ok'
            WHERE b.token_address=? AND b.network_id=? AND b.resolution='1D'
              AND b.ts + 86400 <= ? AND b.h_suspect=0""", (tok, net, t0)).fetchone()
    would_be_1 = d is not None and isinstance(d["ath"], (int, float))
    if would_be_1:
        diverge += 1
        print(f"  DIVERGES: kind={r['kind']} live={r['is_live']} fv={r['feature_version']} "
              f"{tok} net={net} t0={t0} daily_n={d['n']} ath={d['ath']}")
    else:
        same += 1
print(f"  early-return value == measured value : {same}")
print(f"  early-return value != measured value : {diverge}")

# and how many rows table-wide have NO closed intraday bar before t0 at all
print("\n--- family-d completely NULL, table-wide, by kind/is_live ---")
cols = ["ret_1h_before","ret_4h_before","ret_24h_before","ret_7d_before",
        "vol_24h_before","flat_ratio_24h","up_candle_ratio_24h","dist_from_ath",
        "ath_history_days","bars_history_h","bars_count_24h","bar_vol_1h",
        "bar_vol_24h","vol_surge_1h"]
cond = " AND ".join(f"{c} IS NULL" for c in cols)
for r in con.execute(f"""SELECT kind, is_live, COUNT(*) n,
                                SUM(ath_history_complete=0) c0,
                                SUM(ath_history_complete IS NULL) cnull
                           FROM training_rows WHERE {cond}
                          GROUP BY kind, is_live""").fetchall():
    print("  ", dict(r))

# does any OTHER flag-style column in family-d default to a non-NULL?
print("\n--- distinct values of the family-d columns in the model population ---")
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""
for c in ["ath_history_complete","suspect_bars"]:
    r = con.execute(f"""SELECT COUNT(DISTINCT {c}) d, SUM({c} IS NULL) nn, COUNT(*) n
                          FROM training_rows WHERE {POP}""").fetchone()
    print(f"  {c}: distinct={r['d']} nulls={r['nn']} of {r['n']}")
con.close()
