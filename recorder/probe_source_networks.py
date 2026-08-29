"""Do the enrichment SOURCE tables cover network 4663 / 8453 at all?"""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

for tbl in ["token_flow", "chain_concentration", "chain_authority", "token_holders",
            "evm_balances", "evm_contract", "token_social", "token_static", "market_ticks"]:
    try:
        rows = con.execute(f"SELECT network_id, COUNT(*) n FROM {tbl} GROUP BY 1 ORDER BY n DESC").fetchall()
        print(f"{tbl:22s}", {r["network_id"]: r["n"] for r in rows})
    except Exception as e:
        print(f"{tbl:22s} FAILED {e}")

print("\n-- token_flow / chain_concentration presence for 4663 tokens in the model population --")
MW = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
        AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
for net in ("4663", "8453", "56", "1399811149"):
    q = f"""SELECT COUNT(*) n,
      SUM(EXISTS(SELECT 1 FROM token_flow f WHERE f.token_address=tr.token_address
                  AND f.network_id=tr.network_id)) has_flow_any_ts,
      SUM(EXISTS(SELECT 1 FROM token_flow f WHERE f.token_address=tr.token_address
                  AND f.network_id=tr.network_id
                  AND CAST(strftime('%s',f.recorded_at) AS INTEGER) <= tr.entry_ts)) has_flow_before_t0
      FROM training_rows tr WHERE {MW} AND network_id='{net}'"""
    r = con.execute(q).fetchone()
    print(f"   net={net:12s} rows={r['n']:6d} flow_any_ts={r['has_flow_any_ts']:6d} flow_before_t0={r['has_flow_before_t0']:6d}")
