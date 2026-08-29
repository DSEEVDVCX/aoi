import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

# 1) case of Base addresses in each table (the classic mixed-case bug)
for tbl, tcol, where in (("signal_events","token_address","network_id='8453' AND recorded_at>='2026-08-14'"),
                         ("signal_events","token_address","network_id='8453'"),
                         ("watchlist","token_address","network_id='8453'"),
                         ("evm_contract","token_address","1=1")):
    q = f"""SELECT COUNT(*) n, SUM(CASE WHEN {tcol}=lower({tcol}) THEN 1 ELSE 0 END) low_
              FROM {tbl} WHERE {where}"""
    r = con.execute(q).fetchone()
    print(f"[case] {tbl:14s} where={where[:34]:34s} n={r['n']:>6d} lowercase={r['low_']}")

# 2) POSITIVE CONTROL: replay features.onchain_contract_features' exact query
#    (exact-match on token_address, no lower()) for every Base watchlist coin at t0=now.
now = con.execute("SELECT CAST(strftime('%s','now') AS INTEGER) t").fetchone()["t"]
wl = [r["token_address"] for r in con.execute(
    "SELECT token_address FROM watchlist WHERE network_id='8453'")]
hit = 0
sample = None
for a in wl:
    row = con.execute(
        """SELECT code_size, function_count, is_proxy, is_ownership_renounced,
                  has_mint, has_pause, has_blacklist, has_fee_setter,
                  has_limit_setter, has_trading_switch,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM evm_contract
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""", (a, "8453", now)).fetchone()
    if row is not None:
        hit += 1
        if sample is None:
            sample = (a, dict(row))
print(f"\n[pos-control] Base watchlist coins={len(wl)} exact-match feature lookup returns a row for {hit}")
if sample:
    print("   sample:", sample[0], sample[1])

# 3) variance of the family across the 74 collected addresses (is it informative?)
q3 = """SELECT COUNT(DISTINCT token_address) addrs, COUNT(DISTINCT code_size) d_size,
               COUNT(DISTINCT code_hash) d_hash, SUM(is_proxy) proxies,
               SUM(CASE WHEN owner_address IS NOT NULL THEN 1 ELSE 0 END) has_owner,
               SUM(has_mint) mint, SUM(has_pause) pause, SUM(has_blacklist) bl,
               SUM(has_fee_setter) fee, SUM(has_limit_setter) lim,
               SUM(has_trading_switch) tsw, COUNT(*) rows_
          FROM evm_contract"""
r = con.execute(q3).fetchone()
print("\n[variance in evm_contract]", dict(r))

# 4) latest-per-address view of variance
q4 = """SELECT COUNT(*) addrs, COUNT(DISTINCT code_size) d_size, SUM(is_proxy) proxies,
               SUM(has_mint) mint, SUM(has_limit_setter) lim, SUM(is_ownership_renounced) renounced,
               SUM(CASE WHEN is_ownership_renounced IS NULL THEN 1 ELSE 0 END) no_owner_fn
          FROM (SELECT c.* FROM evm_contract c
                 JOIN (SELECT token_address, MAX(recorded_at) m FROM evm_contract
                        GROUP BY 1) x
                   ON x.token_address=c.token_address AND x.m=c.recorded_at)"""
r = con.execute(q4).fetchone()
print("[variance latest-per-address]", dict(r))

# 5) why no Base training rows after 08-10?  all training_rows by network by week
q5 = """SELECT network_id, strftime('%Y-%W', entry_ts,'unixepoch') wk, COUNT(*) n
          FROM training_rows WHERE kind='signal'
         GROUP BY 1,2 ORDER BY wk, network_id"""
print("\n[all training_rows kind=signal by network/week]")
for r in con.execute(q5):
    print(f"    wk={r['wk']} net={r['network_id']:>12s} n={r['n']}")
con.close()
