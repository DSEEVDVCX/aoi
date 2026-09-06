import os, sqlite3, config, time
from collections import Counter, defaultdict

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")
    return rows

print("=== address casing per table (distinct coins) ===")
TABLES = ["watchlist","watch_windows","signal_events","token_static","outcomes","training_rows",
          "market_ticks","token_bars","token_holders","token_flow","chain_concentration",
          "evm_contract","bars_fetch_state","token_class","activity_events","token_social"]
for t in TABLES:
    try:
        rows = con.execute(f"""
          SELECT COUNT(DISTINCT token_address) d_exact,
                 COUNT(DISTINCT lower(token_address)) d_lower,
                 SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) rows_mixedcase,
                 SUM(CASE WHEN token_address LIKE '0x%' OR token_address LIKE '0X%' THEN 1 ELSE 0 END) rows_evm
            FROM {t}""").fetchone()
        print(f"  {t:22s} distinct_exact={rows['d_exact']:>7} distinct_lower={rows['d_lower']:>7} "
              f"split={rows['d_exact']-rows['d_lower']:>4} rows_with_uppercase_chars={rows['rows_mixedcase']:>8} evm_rows={rows['rows_evm']}")
    except Exception as e:
        print(f"  {t}: FAILED {e}")

print()
print("=== EVM rows only: how many have any uppercase hex char (mixed-case / checksummed) ===")
for t in TABLES:
    try:
        r = con.execute(f"""
          SELECT COUNT(*) n_evm,
                 SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) n_mixed,
                 COUNT(DISTINCT token_address) d, COUNT(DISTINCT lower(token_address)) dl
            FROM {t} WHERE substr(token_address,1,2) IN ('0x','0X')""").fetchone()
        print(f"  {t:22s} evm_rows={r['n_evm']:>8} mixedcase={r['n_mixed']:>8} distinct={r['d']:>6} distinct_lower={r['dl']:>6}")
    except Exception as e:
        print(f"  {t}: FAILED {e}")

print()
print("=== coins that appear under two different casings of the same address (cross-table union) ===")
seen = defaultdict(set)  # lower -> set of exact spellings
where = defaultdict(set)
for t in TABLES:
    try:
        for r in con.execute(f"SELECT DISTINCT token_address FROM {t}"):
            a = r["token_address"]
            if a is None: continue
            seen[a.lower()].add(a)
            where[a.lower()].add(t)
    except Exception as e:
        print(f"  {t}: FAILED {e}")
multi = {k:v for k,v in seen.items() if len(v) > 1}
print("  coins with >1 spelling across all tables:", len(multi), "of", len(seen))
for k,v in list(multi.items())[:15]:
    print("    ", k, sorted(v), sorted(where[k]))

print()
print("=== creator_address / in_token_address / out_token_address casing ===")
q("token_static.creator_address casing", """
SELECT SUM(CASE WHEN creator_address<>lower(creator_address) THEN 1 ELSE 0 END) mixed,
       COUNT(creator_address) n, COUNT(DISTINCT creator_address) d, COUNT(DISTINCT lower(creator_address)) dl
  FROM token_static""")
q("signal_events.in/out token casing", """
SELECT SUM(CASE WHEN in_token_address<>lower(in_token_address) THEN 1 ELSE 0 END) in_mixed,
       SUM(CASE WHEN out_token_address<>lower(out_token_address) THEN 1 ELSE 0 END) out_mixed,
       COUNT(*) n FROM signal_events""")

con.close()
