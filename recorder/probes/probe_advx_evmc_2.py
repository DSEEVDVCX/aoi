import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

CUT = "2026-08-13T18:16:54"  # evm_contract first record

# A) ALL training_rows on Base regardless of status/fv/is_live, split by whether
#    entry_ts is after the collector switched on.
qa = """SELECT CASE WHEN entry_ts >= strftime('%s',?) THEN 'after' ELSE 'before' END era,
               status, is_live, feature_version fv, COUNT(*) n,
               COUNT(onchain_code_size) nn_code,
               MIN(date(entry_ts,'unixepoch')) e0, MAX(date(entry_ts,'unixepoch')) e1
          FROM training_rows
         WHERE network_id='8453'
         GROUP BY 1,2,3,4 ORDER BY era DESC, n DESC"""
print("[A] every Base training_row, by era/status/fv:")
for r in con.execute(qa, (CUT,)):
    print(f"    {r['era']:6s} status={r['status']:<10s} live={r['is_live']} fv={r['fv']:>3} "
          f"n={r['n']:>5d} nn_code={r['nn_code']:>4d} {r['e0']}..{r['e1']}")

# B) Are Base signals still arriving at all? per-day signal count by network.
qb = """SELECT substr(created_at,1,10) d,
               SUM(CASE WHEN network_id='8453' THEN 1 ELSE 0 END) base,
               COUNT(*) all_nets
          FROM signal_events
         WHERE created_at >= '2026-08-05'
         GROUP BY 1 ORDER BY 1"""
print("\n[B] signal_events per day (Base vs all):")
for r in con.execute(qb):
    print(f"    {r['d']}  base={r['base']:>4d}  all={r['all_nets']:>5d}")

# C) how many of the 111 population Base rows share an address with evm_contract
qc = """SELECT COUNT(*) n,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM evm_contract c
                     WHERE lower(c.token_address)=lower(t.token_address)) THEN 1 ELSE 0 END) addr_known,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM evm_contract c
                     WHERE lower(c.token_address)=lower(t.token_address)
                       AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts) THEN 1 ELSE 0 END) reachable_pit
          FROM training_rows t
         WHERE t.network_id='8453' AND t.kind='signal' AND t.is_live=1
           AND t.asset_class='meme' AND t.status='ok' AND t.is_independent=1
           AND t.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
r = con.execute(qc).fetchone()
print(f"\n[C] population Base rows n={r['n']} address-known-in-evm_contract={r['addr_known']} "
      f"point-in-time-reachable={r['reachable_pit']}")

# D) sanity: does the sibling Solana family (chain_authority) work? proves the
#    point-in-time join pattern itself is not broken.
qd = """SELECT COUNT(*) n, COUNT(onchain_has_mint_authority) nn_auth,
               COUNT(onchain_dev_holding_pct) nn_dev
          FROM training_rows
         WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
           AND is_independent=1 AND network_id='1399811149'
           AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
r = con.execute(qd).fetchone()
print(f"[D] Solana sibling family: n={r['n']} onchain_has_mint_authority non-null={r['nn_auth']} "
      f"dev_holding_pct non-null={r['nn_dev']}")

# E) latest population entry_ts overall vs Base
qe = """SELECT MAX(entry_ts) mx, MAX(date(entry_ts,'unixepoch')) mxd FROM training_rows
         WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
           AND is_independent=1
           AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
r = con.execute(qe).fetchone()
print(f"[E] population newest entry_ts overall = {r['mxd']}")
con.close()
