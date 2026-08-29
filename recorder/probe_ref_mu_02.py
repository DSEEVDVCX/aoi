import os, sqlite3, json, collections, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

MU = ("numTrades", "uniqueTraders", "minutes", "priceChangePercent",
      "totalVolume", "areTopTraders")

print("=" * 78)
print("A) body-key census from the RAW upstream payload, per signal_type")
print("   (raw_json is a zlib BLOB -> db.decode_raw, never json.loads)")
for st, lim in (("large_buy", 400), ("large_sell", 400),
                ("multi_user_buy", 74), ("multi_user_sell", 23)):
    sql = ("SELECT raw_json FROM signal_events WHERE signal_type=? "
           "ORDER BY recorded_at DESC LIMIT ?")
    print("  SQL:", sql, (st, lim))
    keys = collections.Counter()
    n = 0
    for r in con.execute(sql, (st, lim)):
        ev = dbmod.decode_raw(r["raw_json"])
        if isinstance(ev, (str, bytes)):
            ev = json.loads(ev)
        body = (ev or {}).get("body") or {}
        n += 1
        for k in body:
            keys[k] += 1
    print(f"  {st}: sampled n={n}")
    print(f"    body keys present: {sorted(keys)}")
    print("    multi_user field presence in this sample: "
          + ", ".join(f"{k}={keys.get(k,0)}/{n}" for k in MU))

print("=" * 78)
print("B) the 97 multi_user events: are the constants genuinely UPSTREAM constants?")
sql = ("SELECT minutes, are_top_traders, COUNT(*) n FROM signal_events "
       "WHERE signal_type LIKE 'multi_user%' GROUP BY 1,2")
print("  SQL:", sql)
for r in con.execute(sql):
    print("   ", dict(r))

sql2 = ("SELECT raw_json FROM signal_events WHERE signal_type LIKE 'multi_user%' "
        "ORDER BY recorded_at DESC LIMIT 5")
print("  SQL:", sql2)
for r in con.execute(sql2):
    ev = dbmod.decode_raw(r["raw_json"])
    if isinstance(ev, (str, bytes)):
        ev = json.loads(ev)
    b = (ev or {}).get("body") or {}
    print("    upstream body: minutes=%r areTopTraders=%r uniqueTraders=%r numTrades=%r"
          % (b.get("minutes"), b.get("areTopTraders"),
             b.get("uniqueTraders"), b.get("numTrades")))

print("=" * 78)
print("C) large_buy/large_sell ANALOG fields in the model population")
print("   (if these are populated, nothing is lost - the two event types")
print("    simply have disjoint body schemas, both fully mapped)")
POP = """
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
                                WHERE key='current_feature_version'),'0') AS INTEGER)
"""
sql3 = """
SELECT signal_type, COUNT(*) n,
       COUNT(num_swaps) c_num_swaps, COUNT(is_first_buy) c_is_first_buy,
       COUNT(buyer_pnl_pct) c_buyer_pnl, COUNT(avg_cost) c_avg_cost,
       COUNT(size_usd) c_size, COUNT(market_cap) c_mcap,
       COUNT(top_trader_match_count) c_ttmc, COUNT(buyers_best_rank) c_rank
""" + POP + " GROUP BY signal_type ORDER BY n DESC"
print("  SQL:", " ".join(sql3.split()))
rows = con.execute(sql3).fetchall()
print("  " + " | ".join(rows[0].keys()))
for r in rows:
    print("  " + " | ".join("NULL" if v is None else str(v) for v in tuple(r)))

print("=" * 78)
print("D) is the multi_user event type rare THROUGHOUT, or did it stop?")
sql4 = """
SELECT substr(recorded_at,1,7) AS month,
       SUM(signal_type LIKE 'multi_user%') AS mu,
       COUNT(*) AS all_ev
  FROM signal_events GROUP BY 1 ORDER BY 1
"""
print("  SQL:", " ".join(sql4.split()))
for r in con.execute(sql4):
    print("   ", dict(r))

con.close()
