"""Why is the whole onchain_* EVM-contract family NULL? Probe the source table."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

def show(title, sql, args=()):
    print(f"\n== {title} ==")
    print("   SQL:", " ".join(sql.split()))
    try:
        for r in con.execute(sql, args).fetchall()[:25]:
            print("   ", dict(r))
    except Exception as e:
        print("   FAILED:", e)

show("evm_contract exists?", "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%contract%'")
show("evm_contract rowcount", "SELECT COUNT(*) n FROM evm_contract")
show("evm_contract by network", "SELECT network_id, typeof(network_id) t, COUNT(*) n, MIN(recorded_at) mn, MAX(recorded_at) mx FROM evm_contract GROUP BY 1,2")
show("evm_contract sample", "SELECT token_address, network_id, recorded_at, typeof(recorded_at) tr, code_size, function_count, is_proxy, has_mint FROM evm_contract LIMIT 5")
show("strftime parse of recorded_at",
     "SELECT recorded_at, CAST(strftime('%s', recorded_at) AS INTEGER) e FROM evm_contract LIMIT 5")
show("how many recorded_at unparseable",
     "SELECT SUM(CASE WHEN strftime('%s', recorded_at) IS NULL THEN 1 ELSE 0 END) bad, COUNT(*) n FROM evm_contract")

# join-key comparison against the model population
show("distinct token_address case in evm_contract",
     "SELECT SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixedcase, COUNT(*) n FROM evm_contract")
show("training_rows network_id typeof",
     "SELECT network_id, typeof(network_id) t, COUNT(*) n FROM training_rows GROUP BY 1,2 ORDER BY n DESC")
show("training_rows token_address mixed case",
     "SELECT SUM(CASE WHEN token_address <> lower(token_address) THEN 1 ELSE 0 END) mixedcase, COUNT(*) n FROM training_rows")

# exact-match join as features.py does it
show("EXACT join (as features.py) -> matches",
     """SELECT COUNT(*) n FROM training_rows tr
        WHERE EXISTS (SELECT 1 FROM evm_contract c
                       WHERE c.token_address = tr.token_address
                         AND c.network_id = tr.network_id)""")
show("CASE-INSENSITIVE + cast join -> matches",
     """SELECT COUNT(*) n FROM training_rows tr
        WHERE EXISTS (SELECT 1 FROM evm_contract c
                       WHERE lower(c.token_address) = lower(tr.token_address)
                         AND CAST(c.network_id AS TEXT) = CAST(tr.network_id AS TEXT))""")
show("addr-only case-insensitive join -> matches",
     """SELECT COUNT(*) n FROM training_rows tr
        WHERE EXISTS (SELECT 1 FROM evm_contract c
                       WHERE lower(c.token_address) = lower(tr.token_address))""")
show("evm_contract distinct tokens vs training_rows EVM distinct tokens",
     """SELECT (SELECT COUNT(DISTINCT lower(token_address)) FROM evm_contract) c_tokens,
               (SELECT COUNT(DISTINCT lower(token_address)) FROM training_rows
                 WHERE network_id <> '1399811149') tr_evm_tokens""")
