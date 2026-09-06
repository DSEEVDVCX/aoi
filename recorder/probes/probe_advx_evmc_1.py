import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FAM = ["onchain_code_size","onchain_function_count","onchain_is_proxy",
       "onchain_owner_renounced","onchain_has_mint_fn","onchain_has_pause_fn",
       "onchain_has_blacklist_fn","onchain_has_fee_setter","onchain_has_limit_setter",
       "onchain_has_trading_switch","onchain_contract_age_min"]

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version = CAST(COALESCE("
       "(SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)")

fv = con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()
print("current_feature_version =", fv["value"] if fv else None)

# 1) my own per-column non-null count (NOT their all-null-at-once form)
sel = ", ".join(f"COUNT({c}) nn_{i}" for i, c in enumerate(FAM))
q1 = f"SELECT COUNT(*) total, {sel} FROM training_rows WHERE {POP}"
r = con.execute(q1).fetchone()
print("\n[1] population total =", r["total"])
for i, c in enumerate(FAM):
    print(f"    {c:32s} non-null={r[f'nn_{i}']}")

# 2) population per-network, and per-network non-null of code_size
q2 = f"""SELECT network_id, COUNT(*) n, COUNT(onchain_code_size) nn_code,
                MIN(date(entry_ts,'unixepoch')) first_e,
                MAX(date(entry_ts,'unixepoch')) last_e
           FROM training_rows WHERE {POP} GROUP BY network_id ORDER BY n DESC"""
print("\n[2] population by network:")
for row in con.execute(q2):
    print(f"    net={row['network_id']:>12s} n={row['n']:>6d} nn_code={row['nn_code']:>5d} "
          f"entry_ts {row['first_e']} .. {row['last_e']}")

# 3) is the family EVER non-null anywhere in training_rows (any fv, any is_live)?
q3 = f"""SELECT feature_version fv, is_live, network_id, COUNT(*) n,
                COUNT(onchain_code_size) nn
           FROM training_rows
          WHERE onchain_code_size IS NOT NULL OR onchain_function_count IS NOT NULL
             OR onchain_is_proxy IS NOT NULL OR onchain_has_mint_fn IS NOT NULL
             OR onchain_contract_age_min IS NOT NULL
          GROUP BY 1,2,3 ORDER BY n DESC LIMIT 20"""
print("\n[3] ANY training_rows row with the family populated (any fv/is_live):")
rows = list(con.execute(q3))
if not rows:
    print("    NONE — family is empty in the entire table, not just the population")
for row in rows:
    print(f"    fv={row['fv']} is_live={row['is_live']} net={row['network_id']} n={row['n']} nn_code={row['nn']}")

# 4) evm_contract source table shape, my own grouping
q4 = """SELECT network_id, COUNT(*) rows_, COUNT(DISTINCT token_address) addrs,
               MIN(recorded_at) first_rec, MAX(recorded_at) last_rec,
               SUM(CASE WHEN token_address = lower(token_address) THEN 1 ELSE 0 END) lower_rows,
               COUNT(code_size) nn_code, MIN(code_size) mn, MAX(code_size) mx
          FROM evm_contract GROUP BY 1"""
print("\n[4] evm_contract source table:")
for row in con.execute(q4):
    print(f"    net={row['network_id']} rows={row['rows_']} addrs={row['addrs']} "
          f"{row['first_rec']} .. {row['last_rec']} lower={row['lower_rows']} "
          f"nn_code={row['nn_code']} code_size {row['mn']}..{row['mx']}")

con.close()
