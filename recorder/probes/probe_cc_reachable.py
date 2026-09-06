"""Is chain_concentration reachable at t0 for the 4663 model rows? (exact features.py predicate)"""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
MW = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
        AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""

for net in ("4663", "8453", "56", "1399811149"):
    q = f"""SELECT COUNT(*) n,
      SUM(EXISTS(SELECT 1 FROM chain_concentration c
                  WHERE c.token_address=tr.token_address AND c.network_id=tr.network_id)) any_ts,
      SUM(EXISTS(SELECT 1 FROM chain_concentration c
                  WHERE c.token_address=tr.token_address AND c.network_id=tr.network_id
                    AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= tr.entry_ts)) before_t0,
      SUM(onchain_top10_pct IS NOT NULL) actually_filled,
      MIN(tr.built_at) min_built, MAX(tr.built_at) max_built
      FROM training_rows tr WHERE {MW} AND network_id='{net}'"""
    r = con.execute(q).fetchone()
    print(f"net={net:12s} rows={r['n']:6d} cc_any={r['any_ts']:6d} cc_before_t0={r['before_t0']:6d} "
          f"filled={r['actually_filled']:6d} built {r['min_built'][:19]} -> {r['max_built'][:19]}")

print("\n-- chain_concentration is_replay split by network --")
for r in con.execute("SELECT network_id, is_replay, COUNT(*) n, MIN(recorded_at) mn, MAX(recorded_at) mx FROM chain_concentration GROUP BY 1,2 ORDER BY 1,2"):
    print("   ", dict(r))
