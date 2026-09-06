import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

print("current_feature_version =",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

# --- 1. my own shape: GROUP BY the flag x whether ANY bar-derived field exists
print("\n=== 1. model population: flag x bar-evidence (my own crosstab) ===")
rows = con.execute(f"""
  SELECT CASE WHEN ath_history_complete IS NULL THEN 'NULL'
              ELSE CAST(ath_history_complete AS TEXT) END AS flag,
         CASE WHEN bars_history_h IS NULL AND bars_count_24h IS NULL
                   AND bar_vol_24h IS NULL AND dist_from_ath IS NULL
              THEN 'no-bars-at-all' ELSE 'has-bar-evidence' END AS ev,
         COUNT(*) n
    FROM training_rows WHERE {POP}
   GROUP BY flag, ev ORDER BY flag, ev""").fetchall()
tot = 0
for r in rows:
    print(f"  flag={r['flag']:>4}  {r['ev']:<16} n={r['n']}")
    tot += r["n"]
print("  population total =", tot)

# --- 2. finer: which bar fields are NULL among flag=0 rows
print("\n=== 2. flag=0 breakdown by each bar field ===")
r = con.execute(f"""
  SELECT COUNT(*) c0,
         SUM(bars_history_h IS NULL) bh_null,
         SUM(bars_count_24h IS NULL) bc_null,
         SUM(dist_from_ath IS NULL) dist_null,
         SUM(ath_history_days IS NULL) days_null,
         SUM(bars_history_h IS NULL AND dist_from_ath IS NULL) both_null
    FROM training_rows WHERE {POP} AND ath_history_complete=0""").fetchone()
print(dict(r))

# --- 3. flag=1 sanity: should always have bars + days
print("\n=== 3. flag=1 sanity ===")
r = con.execute(f"""
  SELECT COUNT(*) c1, SUM(bars_history_h IS NULL) bh_null,
         SUM(ath_history_days IS NULL) days_null, SUM(dist_from_ath IS NULL) dist_null
    FROM training_rows WHERE {POP} AND ath_history_complete=1""").fetchone()
print(dict(r))

# --- 4. their exact SQL, for agreement check
print("\n=== 4. their exact SQL ===")
r = con.execute(f"""
SELECT SUM(CASE WHEN ath_history_complete=0 THEN 1 ELSE 0 END) c0,
       SUM(CASE WHEN ath_history_complete=0 AND bars_history_h IS NULL THEN 1 ELSE 0 END) c0_nobars,
       SUM(CASE WHEN ath_history_complete=0 AND bars_history_h IS NOT NULL THEN 1 ELSE 0 END) c0_hasbars,
       SUM(CASE WHEN ath_history_complete=1 THEN 1 ELSE 0 END) c1,
       SUM(CASE WHEN ath_history_complete IS NULL THEN 1 ELSE 0 END) cnull
  FROM training_rows WHERE {POP}""").fetchone()
print(dict(r))

# --- 5. the no-bars rows: identify them
print("\n=== 5. the flag=0 & bars_history_h IS NULL rows ===")
rows = con.execute(f"""
  SELECT key, token_address, network_id, ts, feature_version, dist_from_ath,
         ath_history_days, bars_count_24h, bar_vol_24h, ret_24h_before, created_at
    FROM training_rows WHERE {POP} AND ath_history_complete=0
     AND bars_history_h IS NULL""").fetchall()
for r in rows:
    print(" ", dict(r))
print("  count =", len(rows))

# --- 6. do those tokens actually HAVE any bars? (is 0 a FALSE claim?)
print("\n=== 6. for each no-bars row: what bars exist upstream ===")
for r in rows:
    tok, net, t0 = r["token_address"], r["network_id"], r["ts"]
    a = con.execute(
        """SELECT resolution, COUNT(*) n, SUM(c_suspect) csus, SUM(h_suspect) hsus,
                  MIN(ts) mn, MAX(ts) mx
             FROM token_bars WHERE token_address=? AND network_id=?
            GROUP BY resolution""", (tok, net)).fetchall()
    st = con.execute(
        """SELECT resolution, last_status, candles, oldest_ts
             FROM historical_bars_state WHERE token_address=? AND network_id=?""",
        (tok, net)).fetchall()
    # would the 1D branch have fired?
    d = con.execute(
        """SELECT MIN(b.ts) first_ts, MAX(b.h) ath, COUNT(*) n
             FROM token_bars b JOIN historical_bars_state s
               ON s.token_address=b.token_address AND s.network_id=b.network_id
              AND s.resolution=b.resolution AND s.last_status='ok'
            WHERE b.token_address=? AND b.network_id=? AND b.resolution='1D'
              AND b.ts + 86400 <= ? AND b.h_suspect=0""", (tok, net, t0)).fetchone()
    print(f"  {tok[:14]}.. net={net} ts={t0}")
    print(f"     token_bars: {[dict(x) for x in a]}")
    print(f"     hist_state: {[dict(x) for x in st]}")
    print(f"     1D branch would give: {dict(d)}")

# --- 7. population-hypothesis test: same crosstab on OTHER populations
print("\n=== 7. other populations (was the population wrong?) ===")
for name, w in (
    ("all training_rows", "1=1"),
    ("kind=signal only", "kind='signal'"),
    ("is_live=0", "kind='signal' AND is_live=0"),
    ("stale feature_version", f"kind='signal' AND is_live=1 AND feature_version <> {FV}"),
):
    r = con.execute(f"""SELECT COUNT(*) n, SUM(ath_history_complete IS NULL) cnull,
                               SUM(ath_history_complete=0) c0,
                               SUM(ath_history_complete=0 AND bars_history_h IS NULL) c0nb
                          FROM training_rows WHERE {w}""").fetchone()
    print(f"  {name:<24} {dict(r)}")

# --- 8. is the flag actually reachable as 0-with-no-bars at all, or only via
#        the early return? count rows where flag=0 and EVERY family-D col NULL
print("\n=== 8. flag=0 rows with the entire family-D NULL ===")
cols = ["ret_1h_before","ret_4h_before","ret_24h_before","ret_7d_before",
        "vol_24h_before","flat_ratio_24h","up_candle_ratio_24h","dist_from_ath",
        "ath_history_days","bars_history_h","bars_count_24h","bar_vol_1h",
        "bar_vol_24h","vol_surge_1h"]
cond = " AND ".join(f"{c} IS NULL" for c in cols)
r = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {POP} AND ath_history_complete=0 AND {cond}").fetchone()
print("  flag=0 with all 14 family-D cols NULL:", r["n"])
r = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {POP} AND {cond}").fetchone()
print("  population rows with all 14 family-D cols NULL (any flag):", r["n"])
r = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {POP} AND {cond} AND ath_history_complete IS NULL").fetchone()
print("     ...of which flag IS NULL:", r["n"])

con.close()
