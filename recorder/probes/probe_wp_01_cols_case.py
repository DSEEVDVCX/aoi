import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

print("=== 1) PRAGMA table_info(training_rows) vs features.ROW_COLUMNS ===")
live = [r["name"] for r in q("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("live cols:", len(live), " ROW_COLUMNS:", len(rc))
extra_live = [c for c in live if c not in rc]
missing_live = [c for c in rc if c not in live]
print("in LIVE table but NOT in ROW_COLUMNS (would be NULL forever):", extra_live)
print("in ROW_COLUMNS but NOT in live table:", missing_live)

fv = q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v")[0]["v"]
print("current_feature_version =", fv)
print("features.FEATURE_VERSION =", features.FEATURE_VERSION)

POP = """FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
   AND status='ok' AND is_independent=1 AND feature_version=?"""
n = q("SELECT COUNT(*) c " + POP, (fv,))[0]["c"]
print("model population rows =", n)

print()
print("=== 2) address case: is any token_address mixed-case anywhere? ===")
for tbl in ("signal_events", "outcomes", "training_rows", "chain_concentration",
            "evm_contract", "token_static", "token_holders", "token_flow",
            "watchlist", "token_social", "token_thesis"):
    try:
        r = q(f"""SELECT COUNT(*) tot,
                         SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixed,
                         COUNT(DISTINCT token_address) dist
                    FROM {tbl}""")[0]
        print(f"  {tbl:22s} tot={r['tot']:<9} mixed_case_rows={r['mixed']:<9} distinct={r['dist']}")
    except Exception as e:
        print(f"  {tbl:22s} ERR {e}")

print()
print("=== 3) EVM: model rows with onchain family all NULL, but a lowercase chain_concentration row exists ===")
r = q("""SELECT COUNT(*) c FROM training_rows t
          WHERE t.kind='signal' AND t.is_live=1 AND t.asset_class='meme' AND t.status='ok'
            AND t.is_independent=1 AND t.feature_version=?
            AND t.network_id <> '1399811149'
            AND t.onchain_top1_pct IS NULL
            AND EXISTS (SELECT 1 FROM chain_concentration c
                         WHERE lower(c.token_address)=lower(t.token_address)
                           AND c.network_id=t.network_id
                           AND CAST(strftime('%s', c.recorded_at) AS INTEGER) <= t.entry_ts)""", (fv,))[0]["c"]
print("  EVM model rows: onchain_top1_pct NULL but case-insensitive match exists before t0 =", r)

r2 = q("""SELECT COUNT(*) c FROM training_rows t
          WHERE t.kind='signal' AND t.is_live=1 AND t.asset_class='meme' AND t.status='ok'
            AND t.is_independent=1 AND t.feature_version=?
            AND t.network_id <> '1399811149'
            AND t.onchain_code_size IS NULL
            AND EXISTS (SELECT 1 FROM evm_contract c
                         WHERE lower(c.token_address)=lower(t.token_address)
                           AND c.network_id=t.network_id
                           AND CAST(strftime('%s', c.recorded_at) AS INTEGER) <= t.entry_ts)""", (fv,))[0]["c"]
print("  EVM model rows: onchain_code_size NULL but case-insensitive evm_contract match exists =", r2)

con.close()
