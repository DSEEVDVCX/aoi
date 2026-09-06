import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = con.execute

MODEL = """
  r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok'
  AND r.is_independent=1
  AND r.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
"""

# Sample of deeply-truncated model rows: verify from token_bars DIRECTLY,
# not from outcomes, and test whether bars exist AFTER the 48h window end.
rows = q(f"""SELECT r.key, r.token_address t, COALESCE(r.network_id,'') n, r.entry_ts e,
                    o.last_bar_lag_h lag, o.candles_48h c48, o.final_return_48h fr,
                    o.is_rug
             FROM training_rows r, outcomes o
             WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} AND o.last_bar_lag_h > 6.0
             ORDER BY r.entry_ts DESC LIMIT 400""").fetchall()
print("sampled deeply-truncated (lag>6h) model rows:", len(rows))

agree = 0; disagree = 0; have_bars_after = 0; after_gap = []
recomputed_lag = []
for r in rows:
    end = r["e"] + 48*3600
    b = q("""SELECT MAX(CASE WHEN ts>? AND ts<=? THEN ts END) last_in,
                    MIN(CASE WHEN ts>? THEN ts END) first_after,
                    MAX(ts) max_ts
               FROM token_bars WHERE token_address=? AND network_id=? AND resolution='5'""",
          (r["e"], end, end, r["t"], r["n"])).fetchone()
    if b["last_in"] is None:
        continue
    my_lag = (end - b["last_in"]) / 3600.0
    recomputed_lag.append(my_lag)
    if abs(my_lag - r["lag"]) <= 0.05:
        agree += 1
    else:
        disagree += 1
        if disagree <= 5:
            print("  MISMATCH", r["key"], "stored lag", round(r["lag"],2), "mine", round(my_lag,2))
    if b["first_after"] is not None:
        have_bars_after += 1
        after_gap.append((b["first_after"] - b["last_in"]) / 3600.0)

print("recomputed lag agrees with outcomes.last_bar_lag_h:", agree, " disagrees:", disagree)
if recomputed_lag:
    print("my mean lag on this sample: %.2f h  max %.2f h" % (
        sum(recomputed_lag)/len(recomputed_lag), max(recomputed_lag)))
print("of those, tokens WITH 5m bars after window_end:", have_bars_after,
      "(=> series continued, so the gap is a hole, not death)")
if after_gap:
    after_gap.sort()
    print("  gap last_in_window -> first_after_window (h): median %.2f  min %.2f  max %.2f"
          % (after_gap[len(after_gap)//2], after_gap[0], after_gap[-1]))

# Does the truncated tail look like death? compare volume of the last 6 bars in window
print("\n-- is_rug / return by truncation, over the FULL model population --")
for x in q(f"""SELECT o.bars_truncated bt, COUNT(*) n,
                      ROUND(AVG(o.final_return_48h),4) mean_fr,
                      ROUND(AVG(o.is_rug)*100,2) pct_rug,
                      ROUND(AVG(o.max_drawdown_48h),4) mean_dd
               FROM training_rows r, outcomes o
               WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} GROUP BY 1"""):
    print("  ", dict(x))

# how many model rows would a 'exclude truncated' policy drop, by split?
for x in q(f"""SELECT r.split, COUNT(*) n, SUM(o.bars_truncated) trunc
               FROM training_rows r, outcomes o
               WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} GROUP BY 1"""):
    print("  split", dict(x))
con.close()
