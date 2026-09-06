import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
c = con.cursor()

FV = "(SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER))"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

# tick_age_min is set UNCONDITIONALLY whenever any market_ticks row exists at/before t0
# (features.py:655). So tick_age_min IS NULL <=> zero tick rows == "no market snapshot".
print("=== C1: per-day 'no market snapshot' rate, WHOLE live history (my own measure) ===")
print(f"{'day':12s} {'n':>5s} {'no_tick':>7s} {'pct':>6s} | {'no_holders':>10s} {'pct':>6s} | {'no_flow':>7s} {'pct':>6s}")
tot = []
for r in c.execute(f"""
SELECT strftime('%Y-%m-%d', entry_ts,'unixepoch') d, COUNT(*) n,
       SUM(CASE WHEN tick_age_min IS NULL THEN 1 ELSE 0 END) no_tick,
       SUM(CASE WHEN holders IS NULL AND top10_holders_pct IS NULL THEN 1 ELSE 0 END) no_hold,
       SUM(CASE WHEN buy_count_24h IS NULL AND sell_count_24h IS NULL
                 AND unique_buys_24h IS NULL AND unique_sells_24h IS NULL THEN 1 ELSE 0 END) no_flow
  FROM training_rows WHERE {POP}
 GROUP BY 1 ORDER BY 1"""):
    p1 = 100.0*r['no_tick']/r['n']; p2 = 100.0*r['no_hold']/r['n']; p3 = 100.0*r['no_flow']/r['n']
    tot.append((r['d'], r['n'], r['no_tick'], p1))
    mark = "  <== 08-19" if r['d'] == '2026-08-19' else ""
    print(f"{r['d']:12s} {r['n']:5d} {r['no_tick']:7d} {p1:6.1f} | {r['no_hold']:10d} {p2:6.1f} | {r['no_flow']:7d} {p3:6.1f}{mark}")

full = [t for t in tot if t[1] >= 100]
print(f"\nno_tick pct across {len(full)} days with n>=100: min={min(t[3] for t in full):.1f} "
      f"max={max(t[3] for t in full):.1f} mean={sum(t[3] for t in full)/len(full):.2f}")
ranked = sorted(full, key=lambda t: -t[3])
print("top 6 days by no_tick pct:", [(t[0], round(t[3],1), t[1]) for t in ranked[:6]])

print("\n=== C2: does their 21-column AND-chain equal my tick_age_min IS NULL? ===")
r = c.execute(f"""
SELECT COUNT(*) n,
  SUM(CASE WHEN tick_age_min IS NULL THEN 1 ELSE 0 END) mine,
  SUM(CASE WHEN liquidity IS NULL AND holders IS NULL AND volume_24h IS NULL
        AND tick_age_min IS NULL AND tick_change_1h IS NULL AND tick_volume_1h IS NULL
        AND tick_txn_1h IS NULL AND top10_holders_pct IS NULL AND buy_count_24h IS NULL
        AND sell_count_24h IS NULL AND buy_sell_ratio_24h IS NULL AND unique_buys_24h IS NULL
        AND unique_sells_24h IS NULL AND tick_change_4h IS NULL AND tick_change_24h IS NULL
        AND tick_volume_4h IS NULL AND tick_txn_24h IS NULL AND volume_to_liquidity IS NULL
        AND liquidity_to_mcap IS NULL AND float_ratio IS NULL
        AND tick_rich_age_min IS NULL THEN 1 ELSE 0 END) theirs
  FROM training_rows WHERE {POP}""").fetchone()
print(f"population n={r['n']}  mine(tick_age_min NULL)={r['mine']}  theirs(21-col AND)={r['theirs']}")

print("\n=== C3: the 21 no-tick rows on 08-19 -- which hour, and ingestion lag ===")
for r in c.execute(f"""
SELECT strftime('%Y-%m-%dT%H', t.entry_ts,'unixepoch') h, COUNT(*) n,
       ROUND(AVG((CAST(strftime('%s', s.recorded_at) AS INTEGER) - t.entry_ts)/60.0),1) lag_min
  FROM training_rows t LEFT JOIN signal_events s ON s.id = t.key
 WHERE {POP.replace('kind','t.kind').replace('is_live','t.is_live').replace('asset_class','t.asset_class').replace('status','t.status').replace('is_independent','t.is_independent').replace('feature_version','t.feature_version')}
   AND t.entry_ts >= strftime('%s','2026-08-19') AND t.entry_ts < strftime('%s','2026-08-20')
   AND t.tick_age_min IS NULL
 GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['h']}  rows={r['n']:3d}  avg ingest lag={r['lag_min']} min")

print("\n=== C4: same for 08-18 and 08-20 (where the tick rows DO exist) ===")
for day in ('2026-08-18','2026-08-20'):
    for r in c.execute(f"""
    SELECT strftime('%Y-%m-%dT%H', entry_ts,'unixepoch') h, COUNT(*) n
      FROM training_rows WHERE {POP}
       AND entry_ts >= strftime('%s','{day}') AND entry_ts < strftime('%s','{day}','+1 day')
       AND tick_age_min IS NULL GROUP BY 1 ORDER BY 1"""):
        print(f"  {r['h']}  no_tick rows={r['n']}")

con.close()
