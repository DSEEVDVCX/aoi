"""Source-table checks for the constant / all-zero columns."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

def stat(tbl, col):
    q = f"""SELECT COUNT(*) n, SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END) nulls,
                   SUM(CASE WHEN "{col}"=0 THEN 1 ELSE 0 END) zeros,
                   COUNT(DISTINCT "{col}") dist, MIN("{col}") mn, MAX("{col}") mx
              FROM {tbl}"""
    try:
        r = con.execute(q).fetchone()
        print(f"   {tbl}.{col:22s} n={r['n']:8d} nulls={r['nulls']:8d} zeros={r['zeros']:8d} dist={r['dist']} rng=[{r['mn']}..{r['mx']}]")
    except Exception as e:
        print(f"   {tbl}.{col}: FAILED {e}")

print("-- token_social (source of social_replies) --")
print("   cols:", [x["name"] for x in con.execute("PRAGMA table_info(token_social)")])
for c in ["thesis_replies", "thesis_total", "thesis_authors", "holder_authors"]:
    stat("token_social", c)

print("\n-- token_static (source of is_scam / exchanges_count) --")
for c in ["is_scam", "exchanges_count", "cmc_id", "mintable", "freezable", "graduation_percent"]:
    stat("token_static", c)

print("\n-- signal_events (source of are_top_traders / minutes / top_trader_match_count) --")
for c in ["are_top_traders", "minutes", "num_trades", "unique_traders",
          "top_trader_match_count", "buyers_best_rank", "top_trader_ids_json"]:
    stat("signal_events", c)

print("\n-- signal_type distribution: which types carry the multi_user_buy block? --")
for r in con.execute("""SELECT signal_type, COUNT(*) n,
                               SUM(CASE WHEN are_top_traders IS NOT NULL THEN 1 ELSE 0 END) atc_nonnull,
                               SUM(CASE WHEN minutes IS NOT NULL THEN 1 ELSE 0 END) min_nonnull
                          FROM signal_events GROUP BY 1 ORDER BY n DESC"""):
    print("   ", dict(r))

print("\n-- chain_authority (source of onchain_has_freeze_authority) --")
print("   cols:", [x["name"] for x in con.execute("PRAGMA table_info(chain_authority)")])
for c in ["has_freeze_authority", "has_mint_authority", "is_mutable", "is_token2022"]:
    stat("chain_authority", c)

print("\n-- market_ticks.top10_holders_pct (documented dead) --")
stat("market_ticks", "top10_holders_pct")

print("\n-- token_holders: source of onchain_holders_delta_5m? --")
print("   cols:", [x["name"] for x in con.execute("PRAGMA table_info(chain_concentration)")])
for c in ["holder_count", "top1_pct", "top10_pct"]:
    stat("chain_concentration", c)
