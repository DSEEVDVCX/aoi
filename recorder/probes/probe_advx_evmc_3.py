import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

# B) Are Base signals still arriving? per-day signal_events by network.
qb = """SELECT substr(recorded_at,1,10) d,
               SUM(CASE WHEN network_id='8453' THEN 1 ELSE 0 END) base,
               COUNT(*) all_nets
          FROM signal_events
         WHERE recorded_at >= '2026-08-05'
         GROUP BY 1 ORDER BY 1"""
print("[B] signal_events per day (Base vs all):")
for r in con.execute(qb):
    print(f"    {r['d']}  base={r['base']:>4d}  all={r['all_nets']:>5d}")

# C) reachability of the 111 population Base rows
qc = """SELECT COUNT(*) n,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM evm_contract c
                     WHERE lower(c.token_address)=lower(t.token_address)) THEN 1 ELSE 0 END) addr_known,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM evm_contract c
                     WHERE lower(c.token_address)=lower(t.token_address)
                       AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts) THEN 1 ELSE 0 END) reachable_pit,
               SUM(CASE WHEN t.token_address = lower(t.token_address) THEN 1 ELSE 0 END) tr_lower
          FROM training_rows t
         WHERE t.network_id='8453' AND t.kind='signal' AND t.is_live=1
           AND t.asset_class='meme' AND t.status='ok' AND t.is_independent=1
           AND t.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
r = con.execute(qc).fetchone()
print(f"\n[C] population Base rows n={r['n']} addr_in_evm_contract={r['addr_known']} "
      f"pit_reachable={r['reachable_pit']} lowercase_in_training={r['tr_lower']}")

# C2) same for ALL Base training rows (any fv/live) -- the fv=8 ones reach 08-10
qc2 = """SELECT COUNT(*) n,
               SUM(CASE WHEN EXISTS(SELECT 1 FROM evm_contract c
                     WHERE lower(c.token_address)=lower(t.token_address)
                       AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts) THEN 1 ELSE 0 END) reachable_pit
          FROM training_rows t WHERE t.network_id='8453'"""
r = con.execute(qc2).fetchone()
print(f"[C2] ALL Base training_rows n={r['n']} pit_reachable={r['reachable_pit']}")

# D) Solana sibling authority family -- proves the same join pattern works
qd = """SELECT COUNT(*) n, COUNT(onchain_has_mint_authority) nn_auth,
               COUNT(onchain_dev_holding_pct) nn_dev, COUNT(onchain_auth_age_min) nn_age
          FROM training_rows
         WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
           AND is_independent=1 AND network_id='1399811149'
           AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
r = con.execute(qd).fetchone()
print(f"[D] Solana chain_authority family: n={r['n']} mint_authority nn={r['nn_auth']} "
      f"dev_holding nn={r['nn_dev']} auth_age nn={r['nn_age']}")

# E) newest population entry_ts overall
qe = """SELECT MAX(date(entry_ts,'unixepoch')) mxd FROM training_rows
         WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
           AND is_independent=1
           AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
print("[E] population newest entry_ts =", con.execute(qe).fetchone()["mxd"])

# F) watchlist: are Base coins still being watched? (evm_contract keeps recording)
try:
    qf = """SELECT network_id, COUNT(*) n FROM watchlist GROUP BY 1 ORDER BY n DESC"""
    print("\n[F] watchlist by network:")
    for r in con.execute(qf):
        print(f"    net={r['network_id']} n={r['n']}")
except Exception as e:
    print("\n[F] watchlist query failed:", e)
con.close()
