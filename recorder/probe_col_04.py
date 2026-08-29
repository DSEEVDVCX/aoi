import os, sqlite3, config, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
print("NOW(utc iso) =", NOW)

# collector table -> (timestamp column)
specs = [
    ("market_ticks", "recorded_at"),
    ("token_bars", "fetched_at"),
    ("token_static", "recorded_at"),
    ("token_holders", "recorded_at"),
    ("token_social", "recorded_at"),
    ("token_thesis", "fetched_at"),
    ("token_flow", "recorded_at"),
    ("chain_concentration", "recorded_at"),
    ("chain_authority", "recorded_at"),
    ("evm_contract", "recorded_at"),
    ("evm_balances", "updated_at"),
    ("activity_events", "recorded_at"),
    ("signal_events", "recorded_at"),
    ("snapshots", "recorded_at"),
    ("token_class", "classified_at"),
    ("traders", "recorded_at"),
]
print(f"{'table':22s} {'rows':>10s} {'coins':>7s} {'earliest':>21s} {'latest':>21s}")
for t, ts in specs:
    has_tok = any(c["name"] == "token_address" for c in con.execute(f"PRAGMA table_info({t})"))
    coin = "COUNT(DISTINCT token_address||'|'||network_id)" if has_tok else "NULL"
    q = f"SELECT COUNT(*) n, {coin} c, MIN({ts}) mn, MAX({ts}) mx FROM {t}"
    r = con.execute(q).fetchone()
    print(f"{t:22s} {r['n']:>10d} {str(r['c']):>7s} {str(r['mn'])[:21]:>21s} {str(r['mx'])[:21]:>21s}")

print()
print("=== traders ===")
r = con.execute("SELECT COUNT(*) n, COUNT(DISTINCT trader_id) c, MIN(recorded_at) mn, MAX(recorded_at) mx FROM traders").fetchone()
print(dict(r))
