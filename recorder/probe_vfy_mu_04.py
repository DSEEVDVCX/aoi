"""Part 4: is multi_user_* rarity stable upstream, and is areTopTraders a real
measured false (with topTraders present) rather than an extractor coercion bug?"""
import os, sqlite3, collections, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=" * 78)
print("Q12 per-week: multi_user_* share of all signal_events")
print("-" * 78)
for r in con.execute("""
SELECT substr(recorded_at,1,7) AS month,
       COUNT(*) AS n_all,
       SUM(signal_type LIKE 'multi_user%') AS n_mu,
       MIN(recorded_at) AS first_seen, MAX(recorded_at) AS last_seen
  FROM signal_events GROUP BY 1 ORDER BY 1
"""):
    print(dict(r))
print()

print("=" * 78)
print("Q13 raw areTopTraders values + topTraders length in multi_user payloads")
print("-" * 78)
vals = collections.Counter()
tt_len = collections.Counter()
for r in con.execute("SELECT raw_json, are_top_traders FROM signal_events "
                     "WHERE signal_type LIKE 'multi_user%' AND raw_json IS NOT NULL"):
    obj = dbmod.decode_raw(r["raw_json"])
    b = obj.get("body") if isinstance(obj, dict) and isinstance(obj.get("body"), dict) else obj
    raw = b.get("areTopTraders")
    vals[(repr(raw), r["are_top_traders"])] += 1
    tt = b.get("topTraders")
    tt_len[len(tt) if isinstance(tt, list) else "not-a-list"] += 1
print("  (raw areTopTraders, stored are_top_traders) -> count:", dict(vals))
print("  len(topTraders) -> count:", dict(tt_len))
print()

print("=" * 78)
print("Q14 last multi_user_* event, and days since")
print("-" * 78)
for r in con.execute("""
SELECT signal_type, MAX(ts) AS last_ts, MAX(recorded_at) AS last_rec, COUNT(*) n
  FROM signal_events WHERE signal_type LIKE 'multi_user%' GROUP BY 1
"""):
    print(dict(r))
print()

print("=" * 78)
print("Q15 training_rows: are these 7 cols in features.ROW_COLUMNS?")
print("-" * 78)
import features
live = [r[1] for r in con.execute("PRAGMA table_info(training_rows)")]
seven = ["minutes", "are_top_traders", "unique_traders", "num_trades",
         "price_change_pct", "total_volume", "volume_per_trader"]
for c in seven:
    print(f"  {c:24s} in_live_table={c in live}  in_ROW_COLUMNS={c in features.ROW_COLUMNS}")
print()
print("  live-table cols missing from ROW_COLUMNS:",
      [c for c in live if c not in set(features.ROW_COLUMNS) | {"kind", "key"}])
con.close()
