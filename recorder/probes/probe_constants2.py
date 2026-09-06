"""Part 2: Base signal supply, constant columns, and their sources."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

print("-- signal_events on Base(8453) per day (recorded_at) --")
for r in con.execute("""SELECT substr(recorded_at,1,10) day, COUNT(*) n FROM signal_events
                         WHERE network_id='8453' GROUP BY 1 ORDER BY 1 DESC LIMIT 15"""):
    print("   ", dict(r))
print("-- signal_events ALL per day (last 12) --")
for r in con.execute("""SELECT substr(recorded_at,1,10) day, COUNT(*) n,
                               SUM(CASE WHEN network_id='8453' THEN 1 ELSE 0 END) base
                          FROM signal_events GROUP BY 1 ORDER BY 1 DESC LIMIT 12"""):
    print("   ", dict(r))

print("\n-- always-constant columns over the WHOLE training_rows table --")
for c in ["social_replies", "listed_on_exchange", "are_top_traders", "is_scam",
          "rank_le_50", "rank_le_10", "minutes", "top_traders_listed",
          "top_trader_match_count", "onchain_has_freeze_authority", "suspect_bars",
          "onchain_holders_delta_5m", "top10_holders_pct"]:
    q = f"""SELECT COUNT(*) n, SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) nulls,
                   SUM(CASE WHEN "{c}"=0 THEN 1 ELSE 0 END) zeros,
                   COUNT(DISTINCT "{c}") dist, MIN("{c}") mn, MAX("{c}") mx
              FROM training_rows"""
    r = con.execute(q).fetchone()
    print(f"   {c:30s} n={r['n']} nulls={r['nulls']} zeros={r['zeros']} dist={r['dist']} rng=[{r['mn']}..{r['mx']}]")

print("\n-- signal_events.num_replies (source of social_replies?) --")
r = con.execute("""SELECT COUNT(*) n, SUM(CASE WHEN num_replies IS NULL THEN 1 ELSE 0 END) nulls,
                          COUNT(DISTINCT num_replies) d, MIN(num_replies) mn, MAX(num_replies) mx
                     FROM signal_events""").fetchone()
print("   signal_events.num_replies:", dict(r))

print("\n-- thesis/social source table for replies --")
tabs = [x["name"] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("   tables:", tabs)
