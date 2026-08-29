"""Nail the date wall: model-row entry_ts vs first enrichment snapshot, per network."""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
def d(e):
    return None if e is None else dt.datetime.fromtimestamp(int(e), dt.timezone.utc).isoformat()

MW = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
        AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
print("-- model population entry_ts range per network --")
for r in con.execute(f"SELECT network_id, COUNT(*) n, MIN(entry_ts) mn, MAX(entry_ts) mx FROM training_rows WHERE {MW} GROUP BY 1 ORDER BY n DESC"):
    print(f"   net={r['network_id']:12s} n={r['n']:6d}  {d(r['mn'])} -> {d(r['mx'])}")

print("\n-- first snapshot per source table per network --")
for tbl in ["token_flow", "chain_concentration", "evm_contract", "token_holders"]:
    print(f"   {tbl}:")
    for r in con.execute(f"SELECT network_id, MIN(recorded_at) mn, MAX(recorded_at) mx, COUNT(*) n FROM {tbl} GROUP BY 1 ORDER BY 1"):
        print(f"      net={r['network_id']:12s} n={r['n']:8d} {r['mn']} -> {r['mx']}")

print("\n-- model rows with ZERO enrichment, per network --")
q = f"""SELECT network_id, COUNT(*) n FROM training_rows tr WHERE {MW}
   AND flow_buy_volume_5m IS NULL AND flow_net_volume_1h IS NULL
   AND chain_holder_count IS NULL AND platform_holders IS NULL
   AND chain_top10_pct IS NULL
   AND onchain_top10_pct IS NULL AND onchain_has_mint_authority IS NULL
   AND onchain_holder_count IS NULL AND onchain_code_size IS NULL
   GROUP BY 1 ORDER BY n DESC"""
tot = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {MW}").fetchone()["n"]
s = 0
for r in con.execute(q):
    print(f"   net={r['network_id']:12s} n={r['n']}")
    s += r["n"]
print(f"   TOTAL zero-enrichment model rows: {s} of {tot} = {100*s/tot:.2f}%")
