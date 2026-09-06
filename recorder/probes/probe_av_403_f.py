import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120); con.row_factory = sqlite3.Row
c = con.cursor()

print("=== F1: DECISIVE -- no-market-snapshot rows on 08-19 before vs after the 14:54Z block ===")
r = c.execute("""
SELECT SUM(CASE WHEN entry_ts <  strftime('%s','2026-08-19T14:54:00') THEN 1 ELSE 0 END) before_block,
       SUM(CASE WHEN entry_ts >= strftime('%s','2026-08-19T14:54:00') THEN 1 ELSE 0 END) after_block,
       COUNT(*) total
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version=12
   AND entry_ts>=strftime('%s','2026-08-19') AND entry_ts<strftime('%s','2026-08-20')
   AND tick_age_min IS NULL""").fetchone()
print(f"  no-tick rows on 08-19: total={r['total']}  before 14:54Z={r['before_block']}  at/after 14:54Z={r['after_block']}")

print("\n=== F2: last feed event before the block, first after ===")
for q, lbl in [("SELECT MAX(recorded_at) v FROM signal_events WHERE recorded_at<'2026-08-19T15:00'", "last recorded_at before 15:00"),
               ("SELECT MIN(recorded_at) v FROM signal_events WHERE recorded_at>='2026-08-19T15:00'", "first recorded_at after 15:00")]:
    print(" ", lbl, "=", c.execute(q).fetchone()['v'])

print("\n=== F3: all-history no-market-snapshot rate for the whole population ===")
r = c.execute("""
SELECT COUNT(*) n, SUM(CASE WHEN tick_age_min IS NULL THEN 1 ELSE 0 END) nt
  FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
   AND status='ok' AND is_independent=1 AND feature_version=12""").fetchone()
print(f"  population n={r['n']}  no_tick={r['nt']}  = {100.0*r['nt']/r['n']:.2f}% overall")
con.close()
