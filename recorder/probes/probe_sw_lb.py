import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, a=()):
    return con.execute(sql, a).fetchall()

print("=== 1. PRAGMA table_info(training_rows) vs features.ROW_COLUMNS ===")
live = [r["name"] for r in q("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("live cols:", len(live), " ROW_COLUMNS:", len(rc))
extra_in_db = [c for c in live if c not in rc]
missing_in_db = [c for c in rc if c not in live]
print("in LIVE table but NOT in ROW_COLUMNS (NULL forever):", extra_in_db)
print("in ROW_COLUMNS but NOT in live table:", missing_in_db)

print()
print("=== 2. leaderboard 'all' map empty -> match_count fabricated 0 ===")
r = q("""SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM signal_events
          WHERE top_trader_match_count_24h IS NOT NULL""")[0]
print("period-map era:", r["a"], "->", r["b"], "rows with 24h non-null:", r["n"])

era = r["a"]
r = q("""SELECT COUNT(*) n FROM signal_events WHERE ts >= ?""", (era,))[0]
print("signal_events in era:", r["n"])

sig = """SELECT COUNT(*) n FROM signal_events
          WHERE ts >= ?
            AND top_trader_match_count = 0
            AND top_trader_match_count_24h IS NULL
            AND top_trader_match_count_7d  IS NULL
            AND top_trader_match_count_30d IS NULL"""
print("SIGNATURE (all=0 but every period NULL):", q(sig, (era,))[0]["n"])

print("  breakdown of match_count in era:")
for r in q("""SELECT CASE WHEN top_trader_match_count IS NULL THEN 'NULL'
                        WHEN top_trader_match_count=0 THEN '0' ELSE '>0' END k,
                  COUNT(*) n FROM signal_events WHERE ts >= ? GROUP BY 1""", (era,)):
    print("   ", r["k"], r["n"])

print("  and how many of the SIGNATURE rows reached training_rows:")
print(q("""SELECT COUNT(*) n FROM training_rows t
             JOIN signal_events s ON s.id=t.key
            WHERE t.kind='signal' AND t.is_live=1
              AND s.top_trader_match_count = 0
              AND s.top_trader_match_count_24h IS NULL
              AND s.top_trader_match_count_7d IS NULL
              AND s.top_trader_match_count_30d IS NULL
              AND s.ts >= ?""", (era,))[0]["n"])

print()
print("=== 2b. distinct recorded_at minutes where ALL events have match_count=0 & periods NULL ===")
r = q("""SELECT COUNT(DISTINCT substr(recorded_at,1,16)) n FROM signal_events
          WHERE ts >= ? AND top_trader_match_count=0
            AND top_trader_match_count_24h IS NULL""", (era,))[0]
print("minutes affected:", r["n"])
r = q("""SELECT MIN(recorded_at) a, MAX(recorded_at) b FROM signal_events
          WHERE ts >= ? AND top_trader_match_count=0
            AND top_trader_match_count_24h IS NULL""", (era,))[0]
print("window:", r["a"], "->", r["b"])

print()
print("=== 3. cross-check: fomo tagged Top Trader but our match_count=0 ===")
r = q("""SELECT COUNT(*) n FROM signal_events
          WHERE is_top_trader_tagged=1 AND top_trader_match_count=0""")[0]
print("tagged=1 & match_count=0:", r["n"])
r = q("""SELECT COUNT(*) n FROM signal_events WHERE is_top_trader_tagged=1""")[0]
print("tagged=1 total:", r["n"])
con.close()
