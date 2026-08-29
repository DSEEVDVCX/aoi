import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        for r in con.execute(sql, args).fetchall()[:60]:
            print("   ", dict(r))
    except Exception as e:
        print("    FAILED:", e)
    print()

# A. Are Base signals still arriving at all?
q("A1. signal_events per network: span + count",
  "SELECT network_id, COUNT(*) n, MIN(recorded_at) first, MAX(recorded_at) last "
  "FROM signal_events GROUP BY 1 ORDER BY n DESC")
q("A2. Base signals per day, last 14 days",
  "SELECT substr(recorded_at,1,10) d, COUNT(*) n FROM signal_events "
  "WHERE network_id='8453' AND recorded_at >= '2026-08-08' GROUP BY 1 ORDER BY 1")
q("A3. all networks per day since 2026-08-08 (pivot)",
  "SELECT substr(recorded_at,1,10) d, "
  "SUM(network_id='1399811149') sol, SUM(network_id='56') bsc, "
  "SUM(network_id='4663') rh, SUM(network_id='8453') base, COUNT(*) tot "
  "FROM signal_events WHERE recorded_at >= '2026-08-08' GROUP BY 1 ORDER BY 1")

# B. Are Base watch windows still being opened? (the evm_contract feed)
q("B1. watch_windows per network: span",
  "SELECT network_id, COUNT(*) n, MIN(first_seen_at) first, MAX(first_seen_at) last "
  "FROM watch_windows GROUP BY 1 ORDER BY n DESC")
q("B2. evm_contract watch_first_seen_at span + entry_signal_id presence",
  "SELECT COUNT(DISTINCT token_address) toks, MIN(watch_first_seen_at) first, "
  "MAX(watch_first_seen_at) last, SUM(entry_signal_id IS NOT NULL) with_sig, COUNT(*) n "
  "FROM evm_contract")
q("B3. the 74 Base tokens: when did their watch start (distinct tokens per month-day)",
  "SELECT substr(watch_first_seen_at,1,10) d, COUNT(DISTINCT token_address) toks "
  "FROM evm_contract GROUP BY 1 ORDER BY 1")

# C. Do the 18 shared tokens have a signal AFTER the collector start?
q("C1. Base signal_events for tokens that evm_contract covers, after collector start",
  "SELECT COUNT(*) n, MIN(recorded_at) first, MAX(recorded_at) last FROM signal_events s "
  "WHERE s.network_id='8453' AND lower(s.token_address) IN "
  "(SELECT DISTINCT lower(token_address) FROM evm_contract) "
  "AND recorded_at >= '2026-08-13T18:16:54'")
q("C2. ANY Base signal after collector start",
  "SELECT COUNT(*) n FROM signal_events WHERE network_id='8453' "
  "AND recorded_at >= '2026-08-13T18:16:54'")

# D. Training rows: last built per kind/network -- is row building itself stalled?
q("D1. training_rows newest entry per kind x network",
  "SELECT kind, network_id, COUNT(*) n, datetime(MAX(entry_ts),'unixepoch') newest "
  "FROM training_rows GROUP BY 1,2 ORDER BY 1,3 DESC")
q("D2. training_rows built_at column? (list cols containing 'at' or 'ts')",
  "SELECT 1")
print("training_rows cols:", [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")][:40])
print()

# E. Contrast: the Solana authority family, same t0 predicate, DOES fill
q("E1. Solana authority family coverage on Solana rows only",
  "SELECT COUNT(*) n, SUM(onchain_has_mint_authority IS NOT NULL) nn, "
  "datetime(MIN(entry_ts),'unixepoch') first, datetime(MAX(entry_ts),'unixepoch') last "
  "FROM training_rows WHERE network_id='1399811149'")
q("E2. chain_authority source table span (Solana counterpart of evm_contract)",
  "SELECT COUNT(*) n, COUNT(DISTINCT token_address) toks, MIN(recorded_at) first, MAX(recorded_at) last "
  "FROM chain_authority")

# F. If a Base row existed after collector start, would it match? simulate by
#    replaying the exact feature query for one covered token at a fake t0 = now.
q("F1. dry-run of features.onchain_contract_features query at t0=last snapshot, per token",
  "SELECT token_address, code_size, function_count, is_proxy, is_ownership_renounced, "
  "has_mint, has_pause, has_blacklist, has_fee_setter, has_limit_setter, has_trading_switch "
  "FROM evm_contract WHERE recorded_at=(SELECT MAX(recorded_at) FROM evm_contract e2 "
  "WHERE e2.token_address=evm_contract.token_address) LIMIT 8")
q("F2. variance in the source: distinct values per flag (is the signal informative?)",
  "SELECT COUNT(DISTINCT code_size) d_size, COUNT(DISTINCT function_count) d_fc, "
  "SUM(is_proxy) proxies, SUM(has_mint) mint, SUM(has_pause) pause, "
  "SUM(has_blacklist) black, SUM(has_fee_setter) fee, SUM(has_limit_setter) lim, "
  "SUM(has_trading_switch) sw, SUM(is_ownership_renounced) renounced, COUNT(*) n "
  "FROM evm_contract")

con.close()
