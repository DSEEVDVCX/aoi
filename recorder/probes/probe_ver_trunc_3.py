import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = con.execute
W = 48*3600

MODEL = """
  r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok'
  AND r.is_independent=1
  AND r.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
"""

# FULL population: of the rows flagged truncated, how many NOW have a 5m bar
# inside the last hour of their own 48h window (i.e. the flag is stale)?
row = q(f"""SELECT COUNT(*) tot,
  SUM(CASE WHEN EXISTS (SELECT 1 FROM token_bars b
        WHERE b.token_address=r.token_address AND b.network_id=COALESCE(r.network_id,'')
          AND b.resolution='5' AND b.ts > r.entry_ts + {W} - 3600 AND b.ts <= r.entry_ts + {W})
      THEN 1 ELSE 0 END) now_complete
  FROM training_rows r, outcomes o
  WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} AND o.bars_truncated=1""").fetchone()
print("flagged truncated:", row["tot"])
print("  ... that NOW have a 5m bar in the final hour of the window:", row["now_complete"],
      "(%.1f%%)" % (100.0*row["now_complete"]/row["tot"]))

# sanity mirror: of the NOT-truncated rows, same probe (should be ~all complete)
row2 = q(f"""SELECT COUNT(*) tot,
  SUM(CASE WHEN EXISTS (SELECT 1 FROM token_bars b
        WHERE b.token_address=r.token_address AND b.network_id=COALESCE(r.network_id,'')
          AND b.resolution='5' AND b.ts > r.entry_ts + {W} - 3600 AND b.ts <= r.entry_ts + {W})
      THEN 1 ELSE 0 END) now_complete
  FROM training_rows r, outcomes o
  WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} AND o.bars_truncated=0""").fetchone()
print("flagged NOT truncated:", row2["tot"], " complete now:", row2["now_complete"],
      "(%.1f%%)" % (100.0*row2["now_complete"]/row2["tot"]))

# was labeling simply too early? compare labeled_at against the fetch stamp of the
# bars that now fill the tail, on a sample.
print("\n-- labeled_at vs fetched_at of the tail bars (sample of 60 stale-flag rows) --")
sample = q(f"""SELECT r.key, r.token_address t, COALESCE(r.network_id,'') n, r.entry_ts e,
                      o.labeled_at, o.last_bar_lag_h lag, o.final_return_48h fr, o.entry_px
               FROM training_rows r, outcomes o
               WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} AND o.last_bar_lag_h > 6
               ORDER BY RANDOM() LIMIT 60""").fetchall()
later = 0; checked = 0; big_diff = 0; diffs = []
for r in sample:
    end = r["e"] + W
    tail = q("""SELECT MIN(fetched_at) mn, MAX(fetched_at) mx, COUNT(*) n
                  FROM token_bars WHERE token_address=? AND network_id=? AND resolution='5'
                   AND ts > ? AND ts <= ?""",
             (r["t"], r["n"], end - 3600, end)).fetchone()
    if not tail["n"]:
        continue
    checked += 1
    if tail["mn"] > r["labeled_at"]:
        later += 1
    # recompute the true 48h final return from the last non-suspect close in the window
    last = q("""SELECT c FROM token_bars WHERE token_address=? AND network_id=? AND resolution='5'
                 AND ts > ? AND ts <= ? AND c_suspect=0 ORDER BY ts DESC LIMIT 1""",
             (r["t"], r["n"], r["e"], end)).fetchone()
    if last and r["entry_px"] and r["fr"] is not None:
        true_fr = last["c"]/r["entry_px"] - 1
        d = true_fr - r["fr"]
        diffs.append(d)
        if abs(d) > 0.10:
            big_diff += 1
print("  rows whose tail bars exist:", checked,
      " tail fetched AFTER labeled_at:", later)
if diffs:
    diffs.sort()
    print("  stored final_return_48h vs recomputed-from-current-bars:")
    print("    n=%d  median diff %+.4f  p10 %+.4f  p90 %+.4f  |diff|>10pp in %d rows"
          % (len(diffs), diffs[len(diffs)//2], diffs[int(len(diffs)*0.1)],
             diffs[int(len(diffs)*0.9)], big_diff))
con.close()
