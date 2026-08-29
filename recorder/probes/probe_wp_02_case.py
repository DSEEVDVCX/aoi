import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

FV = 12
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== A) chain_concentration: earliest snapshot per (net, lower(addr)) + exact-case set ===")
cc = q("""SELECT network_id net, token_address addr,
                 MIN(CAST(strftime('%s', recorded_at) AS INTEGER)) e0,
                 SUM(CASE WHEN top1_pct IS NOT NULL THEN 1 ELSE 0 END) with_pct
            FROM chain_concentration GROUP BY network_id, token_address""")
exact = {}          # (net, addr as stored) -> (e0, with_pct)
ci    = {}          # (net, addr.lower()) -> (min e0, with_pct sum)
for r in cc:
    exact[(r["net"], r["addr"])] = (r["e0"], r["with_pct"])
    k = (r["net"], r["addr"].lower())
    prev = ci.get(k)
    ci[k] = (min(prev[0], r["e0"]) if prev else r["e0"],
             (prev[1] if prev else 0) + r["with_pct"])
print("  distinct (net,addr) groups:", len(cc), " ci keys:", len(ci))
print("  groups whose stored addr is NOT lowercase:",
      sum(1 for r in cc if r["addr"] != r["addr"].lower()))

print()
print("=== B) EVM model rows: onchain_top1_pct NULL — split by exact vs case-insensitive match ===")
rows = q(f"""SELECT token_address a, network_id n, entry_ts t, onchain_top1_pct p
               FROM training_rows WHERE {POP} AND network_id <> '1399811149'""")
print("  EVM model rows total:", len(rows))
nulls = [r for r in rows if r["p"] is None]
print("  of which onchain_top1_pct IS NULL:", len(nulls),
      f"({100.0*len(nulls)/max(1,len(rows)):.1f}%)")

case_only = exact_hit = no_data = 0
case_only_tokens = set()
for r in nulls:
    ek = (r["n"], r["a"]); ck = (r["n"], r["a"].lower())
    e_ok = ek in exact and exact[ek][0] <= r["t"] and exact[ek][1] > 0
    c_ok = ck in ci and ci[ck][0] <= r["t"] and ci[ck][1] > 0
    if e_ok:
        exact_hit += 1
    elif c_ok:
        case_only += 1
        case_only_tokens.add((r["n"], r["a"]))
    else:
        no_data += 1
print("  NULL though an EXACT-case usable snapshot exists <= t0 :", exact_hit)
print("  NULL and ONLY a case-folded snapshot exists <= t0     :", case_only,
      f"  ({len(case_only_tokens)} distinct tokens)")
print("  NULL with no snapshot at all <= t0                    :", no_data)
if case_only_tokens:
    for n, a in sorted(case_only_tokens)[:6]:
        stored = [x["addr"] for x in cc if x["net"] == n and x["addr"].lower() == a.lower()]
        print(f"     net={n} training_rows='{a}'  chain_concentration={stored}")

print()
print("=== C) same test for the whole model population (both networks) ===")
allrows = q(f"SELECT token_address a, network_id n, entry_ts t, onchain_top1_pct p FROM training_rows WHERE {POP}")
c2 = sum(1 for r in allrows
         if r["p"] is None
         and not ((r["n"], r["a"]) in exact and exact[(r["n"], r["a"])][0] <= r["t"] and exact[(r["n"], r["a"])][1] > 0)
         and ((r["n"], r["a"].lower()) in ci and ci[(r["n"], r["a"].lower())][0] <= r["t"] and ci[(r["n"], r["a"].lower())][1] > 0))
print("  model rows losing onchain_* purely to address case:", c2, "of", len(allrows))

print()
print("=== D) is_replay rows inside chain_concentration (features.onchain_features does NOT filter them) ===")
try:
    r = q("""SELECT COALESCE(is_replay,0) rep, COUNT(*) c, MIN(recorded_at) lo, MAX(recorded_at) hi
               FROM chain_concentration GROUP BY 1""")
    for x in r: print(f"   is_replay={x['rep']} rows={x['c']} {x['lo']} .. {x['hi']}")
except Exception as e:
    print("   ERR", e)

con.close()
