"""Address-case audit: population addresses vs each enrichment table (read-only)."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
      AND feature_version = {FV}"""

pop = con.execute(f"""SELECT token_address a, network_id n, COUNT(*) c
   FROM training_rows WHERE {POP} GROUP BY 1,2""").fetchall()
pop_exact = {(r["a"], r["n"]): r["c"] for r in pop}
pop_lower = {}
for r in pop:
    pop_lower.setdefault((r["a"].lower(), r["n"]), 0)
    pop_lower[(r["a"].lower(), r["n"])] += r["c"]
print("population distinct (address,net):", len(pop_exact), " rows:", sum(pop_exact.values()))
mixed = {k: v for k, v in pop_exact.items() if k[0] != k[0].lower()}
print("  of those, MIXED-CASE address:", len(mixed), "tokens /", sum(mixed.values()), "rows")
print()

TABLES = [
    ("token_static", False),
    ("token_social", False),
    ("token_thesis", False),
    ("token_bars", False),
    ("market_ticks", False),
    ("token_holders", True),
    ("chain_concentration", True),
    ("chain_authority", True),
    ("token_flow", True),
    ("evm_contract", True),
    ("evm_balances", True),
    ("watchlist", False),
    ("signal_events", True),
]

for tbl, has_net in TABLES:
    cols = [c["name"] for c in con.execute(f"PRAGMA table_info({tbl})")]
    if "token_address" not in cols:
        print(f"{tbl}: no token_address column ({cols[:6]})")
        continue
    if has_net and "network_id" in cols:
        rows = con.execute(f"SELECT DISTINCT token_address a, network_id n FROM {tbl}").fetchall()
        s_exact = {(r["a"], r["n"]) for r in rows}
        s_lower = {(r["a"].lower(), r["n"]) for r in rows}
    else:
        rows = con.execute(f"SELECT DISTINCT token_address a FROM {tbl}").fetchall()
        s_exact = {(r["a"], None) for r in rows}
        s_lower = {(r["a"].lower(), None) for r in rows}
    if has_net and "network_id" in cols:
        hit_exact = sum(v for k, v in pop_exact.items() if k in s_exact)
        hit_lower = sum(v for k, v in pop_lower.items() if k in s_lower)
        tok_exact = sum(1 for k in pop_exact if k in s_exact)
        tok_lower = sum(1 for k in pop_lower if k in s_lower)
    else:
        se = {a for a, _ in s_exact}
        sl = {a for a, _ in s_lower}
        hit_exact = sum(v for k, v in pop_exact.items() if k[0] in se)
        hit_lower = sum(v for k, v in pop_lower.items() if k[0] in sl)
        tok_exact = sum(1 for k in pop_exact if k[0] in se)
        tok_lower = sum(1 for k in pop_lower if k[0] in sl)
    mixedcnt = sum(1 for a, _ in s_exact if a != a.lower())
    print(f"{tbl:22s} distinct={len(s_exact):6d} mixedcase_in_tbl={mixedcnt:6d} | "
          f"pop tokens matched exact={tok_exact:5d} ci={tok_lower:5d} | "
          f"pop ROWS covered exact={hit_exact:6d} ci={hit_lower:6d}"
          + ("  <-- CASE GAP" if hit_lower > hit_exact else ""))
