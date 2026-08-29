import os, sqlite3, config
from collections import Counter

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

# token coverage: population tokens per network vs chain_concentration tokens
print("distinct chain_concentration tokens per network:")
for r in con.execute("SELECT network_id, COUNT(DISTINCT token_address) t FROM chain_concentration GROUP BY network_id"):
    print("  ", r["network_id"], r["t"])
print("\ndistinct population tokens per network:")
for r in con.execute(f"SELECT network_id, COUNT(DISTINCT token_address) t FROM training_rows WHERE {POP} GROUP BY network_id"):
    print("  ", r["network_id"], r["t"])

Q = """SELECT top1_pct, top5_pct, top10_pct, top20_pct, top_accounts, holder_count,
              is_replay, recorded_at,
              CAST(strftime('%s', recorded_at) AS INTEGER) e
         FROM chain_concentration
        WHERE token_address=? AND network_id=?
          AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
        ORDER BY e DESC LIMIT 1"""

rows = con.execute(f"""SELECT key, token_address, network_id, entry_ts, built_at
                        FROM training_rows WHERE {POP} AND onchain_age_min IS NULL""").fetchall()
print("\nall-NULL population rows:", len(rows))

filled_counter = Counter()   # how many of the 11 would fill
per_col = Counter()
n_recover = 0
replay_src = Counter()
sample = None
for r in rows:
    cur = con.execute(Q, (r["token_address"], r["network_id"], r["entry_ts"])).fetchone()
    if cur is None:
        continue
    n_recover += 1
    replay_src[cur["is_replay"]] += 1
    got = {}
    for name, src in (("onchain_top1_pct","top1_pct"),("onchain_top5_pct","top5_pct"),
                      ("onchain_top10_pct","top10_pct"),("onchain_top20_pct","top20_pct"),
                      ("onchain_top_accounts","top_accounts"),("onchain_holder_count","holder_count")):
        got[name] = cur[src]
    got["onchain_age_min"] = (r["entry_ts"] - cur["e"]) / 60
    got["onchain_top1_delta_5m"] = None
    got["onchain_top10_delta_5m"] = None
    got["onchain_holders_delta_5m"] = None
    got["onchain_delta_span_min"] = None
    prev = con.execute(Q, (r["token_address"], r["network_id"], cur["e"] - 240)).fetchone()
    if prev is not None:
        span = (cur["e"] - prev["e"]) / 60
        if span <= 15:
            got["onchain_delta_span_min"] = span
            for col, src in (("onchain_top1_delta_5m","top1_pct"),
                             ("onchain_top10_delta_5m","top10_pct"),
                             ("onchain_holders_delta_5m","holder_count")):
                if cur[src] is not None and prev[src] is not None:
                    got[col] = cur[src] - prev[src]
    k = sum(1 for v in got.values() if v is not None)
    filled_counter[k] += 1
    for c, v in got.items():
        if v is not None:
            per_col[c] += 1
    if sample is None:
        sample = (r["key"], r["built_at"], r["entry_ts"], cur["recorded_at"], cur["is_replay"], got)

print("recoverable rows (features query returns a row):", n_recover)
print("source is_replay distribution:", dict(replay_src))
print("how many of the 11 columns would fill:", dict(sorted(filled_counter.items())))
print("\nper-column recoverable count (out of %d):" % n_recover)
for c, k in sorted(per_col.items(), key=lambda kv: -kv[1]):
    print(f"   {c:<28} {k}")
print("\ntotal cells that would become non-NULL:", sum(per_col.values()))
print("\nsample:", sample)
