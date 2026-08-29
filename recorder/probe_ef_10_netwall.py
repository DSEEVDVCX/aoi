import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

print("signal_events cols:",
      [r["name"] for r in con.execute("PRAGMA table_info(signal_events)")])

print("\n=== signal_events: LAST ts per network ===")
for r in con.execute("""
    SELECT network_id, COUNT(*) n,
           datetime(MIN(ts),'unixepoch') first_ts,
           datetime(MAX(ts),'unixepoch') last_ts
      FROM signal_events GROUP BY 1 ORDER BY n DESC"""):
    print("  ", dict(r))

print("\n=== signal_events per network, last 8 days (by day) ===")
for r in con.execute("""
    SELECT date(ts,'unixepoch') d, network_id, COUNT(*) n
      FROM signal_events
     WHERE ts >= strftime('%s','now') - 8*86400
     GROUP BY 1,2 ORDER BY 1, n DESC"""):
    print("  ", tuple(r))

print("\n=== training_rows(kind=signal) LAST entry per network, ANY filter ===")
for r in con.execute("""
    SELECT network_id, COUNT(*) n, feature_version fv,
           date(MIN(entry_ts),'unixepoch') first_d,
           date(MAX(entry_ts),'unixepoch') last_d
      FROM training_rows WHERE kind='signal' GROUP BY network_id, feature_version
     ORDER BY network_id, feature_version"""):
    print("  ", dict(r))

print("\n=== robinhood(4663) signal_events after 2026-08-04, and their training_rows state ===")
r = con.execute("""SELECT COUNT(*) n FROM signal_events
                    WHERE network_id='4663' AND ts > strftime('%s','2026-08-05')""").fetchone()
print("  signal_events 4663 after 2026-08-05:", r["n"])
r = con.execute("""SELECT COUNT(*) n FROM signal_events
                    WHERE network_id='8453' AND ts > strftime('%s','2026-08-05')""").fetchone()
print("  signal_events 8453 after 2026-08-05:", r["n"])

print("\n=== watch_windows / watchlist by network ===")
for t in ("watch_windows", "watchlist"):
    ci = [x["name"] for x in con.execute(f"PRAGMA table_info({t})")]
    print(f"  {t} cols:", ci)
    if "network_id" in ci:
        for r in con.execute(f"SELECT network_id, COUNT(*) n FROM {t} GROUP BY 1 ORDER BY n DESC"):
            print("    ", tuple(r))
con.close()
