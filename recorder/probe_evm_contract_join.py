"""Replicate features.onchain_contract_features join per training row (Base only)."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

print("== evm_contract columns ==")
print([r["name"] for r in con.execute("PRAGMA table_info(evm_contract)")])

print("\n== training_rows on Base (8453) by kind/fv ==")
for r in con.execute("""SELECT kind, feature_version, COUNT(*) n,
                               MIN(entry_ts) mn, MAX(entry_ts) mx
                          FROM training_rows WHERE network_id='8453' GROUP BY 1,2"""):
    print("   ", dict(r))

print("\n== model population by network_id ==")
MW = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
        AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
for r in con.execute(f"SELECT network_id, COUNT(*) n FROM training_rows WHERE {MW} GROUP BY 1 ORDER BY n DESC"):
    print("   ", dict(r))

print("\n== EXACT features.py join incl. time filter: matches over ALL training_rows ==")
q = """SELECT COUNT(*) n FROM training_rows tr
        WHERE EXISTS (SELECT 1 FROM evm_contract c
                       WHERE c.token_address = tr.token_address
                         AND c.network_id = tr.network_id
                         AND CAST(strftime('%s', c.recorded_at) AS INTEGER) <= tr.entry_ts)"""
print("   ", con.execute(q).fetchone()["n"], " SQL:", " ".join(q.split()))

print("\n== same, restricted to the model population ==")
q2 = f"""SELECT COUNT(*) n FROM training_rows tr
        WHERE {MW} AND EXISTS (SELECT 1 FROM evm_contract c
                       WHERE c.token_address = tr.token_address
                         AND c.network_id = tr.network_id
                         AND CAST(strftime('%s', c.recorded_at) AS INTEGER) <= tr.entry_ts)"""
print("   ", con.execute(q2).fetchone()["n"])

print("\n== the 366 token+network matches: built_at vs first contract snapshot ==")
q3 = """SELECT tr.kind, tr.feature_version, COUNT(*) n,
               MIN(tr.built_at) min_built, MAX(tr.built_at) max_built,
               SUM(CASE WHEN CAST(strftime('%s', tr.built_at) AS INTEGER)
                        < (SELECT MIN(CAST(strftime('%s', c2.recorded_at) AS INTEGER))
                             FROM evm_contract c2
                            WHERE c2.token_address = tr.token_address) THEN 1 ELSE 0 END) built_before_snapshot
          FROM training_rows tr
         WHERE EXISTS (SELECT 1 FROM evm_contract c
                        WHERE c.token_address = tr.token_address
                          AND c.network_id = tr.network_id)
         GROUP BY 1,2"""
for r in con.execute(q3):
    print("   ", dict(r))

print("\n== is_ownership_renounced / has_* null rates in evm_contract itself ==")
cols = ["code_size", "function_count", "is_proxy", "is_ownership_renounced", "has_mint",
        "has_pause", "has_blacklist", "has_fee_setter", "has_limit_setter", "has_trading_switch"]
sel = ", ".join(f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) {c}_null, COUNT(DISTINCT {c}) {c}_d" for c in cols)
r = con.execute(f"SELECT COUNT(*) n, {sel} FROM evm_contract").fetchone()
print("   ", dict(r))
