import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

# collector table -> (ts column, joins on network too?)
COLL = [
    ("market_ticks", "recorded_at", True),
    ("token_bars", "fetched_at", True),
    ("token_static", "recorded_at", True),
    ("token_holders", "recorded_at", True),
    ("token_social", "recorded_at", True),
    ("token_flow", "recorded_at", True),
    ("chain_concentration", "recorded_at", True),
    ("chain_authority", "recorded_at", True),
    ("evm_contract", "recorded_at", True),
    ("token_class", "classified_at", True),
]

for label, cond in (("ACTIVE OPERATIONAL (active=1,is_control=0) n=169", "active=1 AND is_control=0"),
                    ("ACTIVE CONTROL (active=1,is_control=1) n=35", "active=1 AND is_control=1"),
                    ("EVER WATCHED (all rows)", "1=1")):
    tot = con.execute(f"SELECT COUNT(*) n FROM watchlist WHERE {cond}").fetchone()["n"]
    print(f"=== {label} — total={tot} ===")
    print(f"{'collector':22s} {'with rows':>10s} {'zero rows':>10s} {'pct cov':>8s}")
    for t, ts, _ in COLL:
        q = f"""SELECT COUNT(*) n FROM watchlist w WHERE {cond}
                AND EXISTS (SELECT 1 FROM {t} x
                            WHERE x.token_address=w.token_address AND x.network_id=w.network_id)"""
        have = con.execute(q).fetchone()["n"]
        pct = 100.0 * have / tot if tot else 0
        print(f"{t:22s} {have:>10d} {tot-have:>10d} {pct:>7.1f}%")
    print()
