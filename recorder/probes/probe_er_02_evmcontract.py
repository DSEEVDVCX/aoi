"""Why is onchain_evm_contract 100% NULL? join evm_contract to the population."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== evm_contract schema ===")
for r in con.execute("PRAGMA table_info(evm_contract)"):
    print("  ", r["name"], r["type"])
print()
print("=== evm_contract by network, recorded_at range ===")
for r in con.execute("""SELECT network_id, COUNT(*) n, COUNT(DISTINCT token_address) toks,
                               MIN(recorded_at) mn, MAX(recorded_at) mx
                          FROM evm_contract GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={r['network_id']!r:14} n={r['n']:6d} toks={r['toks']:5d}  {r['mn']} .. {r['mx']}")
print()
print("=== sample evm_contract rows ===")
for r in con.execute("SELECT token_address, network_id, recorded_at, code_size, function_count FROM evm_contract LIMIT 5"):
    print("  ", dict(r))
print()

print("=== typeof(network_id) in evm_contract vs training_rows ===")
for r in con.execute("SELECT typeof(network_id) t, COUNT(*) n FROM evm_contract GROUP BY 1"):
    print("  evm_contract:", r["t"], r["n"])
for r in con.execute("SELECT typeof(network_id) t, COUNT(*) n FROM training_rows GROUP BY 1"):
    print("  training_rows:", r["t"], r["n"])
print()

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
      AND feature_version = {FV}"""

print("=== population by network_id ===")
for r in con.execute(f"SELECT network_id, COUNT(*) n FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 2 DESC"):
    print(f"  net={r['network_id']!r:14} n={r['n']}")
print()

print("=== population rows with an evm_contract row EXACT (t.token_address=e.token_address, same net) ===")
q1 = f"""SELECT COUNT(*) n FROM training_rows t
 WHERE {POP} AND EXISTS (SELECT 1 FROM evm_contract e
   WHERE e.token_address=t.token_address AND e.network_id=t.network_id
     AND CAST(strftime('%s', e.recorded_at) AS INTEGER) <= t.entry_ts)"""
print("  exact, recorded_at<=entry_ts :", con.execute(q1).fetchone()["n"])

q2 = f"""SELECT COUNT(*) n FROM training_rows t
 WHERE {POP} AND EXISTS (SELECT 1 FROM evm_contract e
   WHERE e.token_address=t.token_address AND e.network_id=t.network_id)"""
print("  exact, any time              :", con.execute(q2).fetchone()["n"])

q3 = f"""SELECT COUNT(*) n FROM training_rows t
 WHERE {POP} AND EXISTS (SELECT 1 FROM evm_contract e
   WHERE lower(e.token_address)=lower(t.token_address) AND CAST(e.network_id AS TEXT)=CAST(t.network_id AS TEXT))"""
print("  case-insensitive, any time   :", con.execute(q3).fetchone()["n"])

q4 = f"""SELECT COUNT(*) n FROM training_rows t
 WHERE {POP} AND EXISTS (SELECT 1 FROM evm_contract e
   WHERE lower(e.token_address)=lower(t.token_address) AND CAST(e.network_id AS TEXT)=CAST(t.network_id AS TEXT)
     AND CAST(strftime('%s', e.recorded_at) AS INTEGER) <= t.entry_ts)"""
print("  case-insens, recorded<=entry :", con.execute(q4).fetchone()["n"])
print()

print("=== strftime on a sample recorded_at (is it parseable?) ===")
for r in con.execute("""SELECT recorded_at, CAST(strftime('%s', recorded_at) AS INTEGER) e,
                               typeof(recorded_at) t FROM evm_contract LIMIT 5"""):
    print("  ", r["recorded_at"], "->", r["e"], r["t"])
print()

print("=== earliest/latest population entry_ts vs evm_contract epoch range ===")
r = con.execute(f"SELECT MIN(entry_ts) a, MAX(entry_ts) b FROM training_rows WHERE {POP}").fetchone()
print("  population entry_ts:", r["a"], r["b"])
r = con.execute("SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) a, MAX(CAST(strftime('%s',recorded_at) AS INTEGER)) b FROM evm_contract").fetchone()
print("  evm_contract epoch :", r["a"], r["b"])
print()

print("=== case profile of token_address ===")
for tbl in ("evm_contract", "training_rows"):
    r = con.execute(f"SELECT SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixed, COUNT(*) n FROM {tbl}").fetchone()
    print(f"  {tbl}: mixed-case={r['mixed']} of {r['n']}")
print()

print("=== whole training_rows: any row with onchain_code_size NOT NULL? ===")
for r in con.execute("""SELECT kind, feature_version, COUNT(*) n,
       SUM(CASE WHEN onchain_code_size IS NOT NULL THEN 1 ELSE 0 END) has_code,
       SUM(CASE WHEN onchain_contract_age_min IS NOT NULL THEN 1 ELSE 0 END) has_age
   FROM training_rows GROUP BY 1,2 ORDER BY 1,2"""):
    print("  ", dict(r))
