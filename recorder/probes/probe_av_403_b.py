import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
c = con.cursor()

FV = "(SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER))"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

print("=== B1: hour-by-hour 08-18..08-20: raw feed events (by feed ts) vs population rows ===")
raw = {}
for r in c.execute("""
SELECT substr(ts,1,13) h, COUNT(*) n, MIN(recorded_at) rmin, MAX(recorded_at) rmax
  FROM signal_events
 WHERE ts >= '2026-08-18' AND ts < '2026-08-21'
 GROUP BY 1"""):
    raw[r['h']] = (r['n'], r['rmin'], r['rmax'])

pop = {}
for r in c.execute(f"""
SELECT strftime('%Y-%m-%dT%H', entry_ts,'unixepoch') h, COUNT(*) n
  FROM training_rows
 WHERE {POP} AND entry_ts >= strftime('%s','2026-08-18')
   AND entry_ts < strftime('%s','2026-08-21')
 GROUP BY 1"""):
    pop[r['h']] = r['n']

# all signal-kind training rows regardless of admission filters
allsig = {}
for r in c.execute("""
SELECT strftime('%Y-%m-%dT%H', entry_ts,'unixepoch') h, COUNT(*) n
  FROM training_rows
 WHERE kind='signal' AND entry_ts >= strftime('%s','2026-08-18')
   AND entry_ts < strftime('%s','2026-08-21')
 GROUP BY 1"""):
    allsig[r['h']] = r['n']

import datetime
d0 = datetime.datetime(2026, 8, 18, tzinfo=datetime.timezone.utc)
print(f"{'hour(UTC)':16s} {'feed_ev':>8s} {'all_tr':>7s} {'pop':>5s}  recorded_at range")
for i in range(72):
    t = d0 + datetime.timedelta(hours=i)
    h = t.strftime('%Y-%m-%dT%H')
    hh = h.replace('T', 'T')
    n, rmin, rmax = raw.get(hh, (0, '', ''))
    print(f"{h:16s} {n:8d} {allsig.get(h,0):7d} {pop.get(h,0):5d}  {rmin[:19]} .. {rmax[:19]}")

con.close()
