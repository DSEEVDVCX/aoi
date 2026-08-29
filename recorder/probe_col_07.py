import os, sqlite3, config, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

COLL = [
    ("market_ticks", "recorded_at"),
    ("token_bars", "fetched_at"),
    ("token_static", "recorded_at"),
    ("token_holders", "recorded_at"),
    ("token_social", "recorded_at"),
    ("token_flow", "recorded_at"),
    ("chain_concentration", "recorded_at"),
    ("chain_authority", "recorded_at"),
    ("evm_contract", "recorded_at"),
]

nets = [r["network_id"] for r in con.execute(
    "SELECT network_id, COUNT(*) n FROM watchlist WHERE active=1 AND is_control=0 GROUP BY network_id ORDER BY n DESC")]
tot = {}
for n in nets:
    tot[n] = con.execute("SELECT COUNT(*) c FROM watchlist WHERE active=1 AND is_control=0 AND network_id=?", (n,)).fetchone()["c"]

print("ACTIVE OPERATIONAL per-network coverage (coins with >=1 row / total active on that net)")
hdr = f"{'collector':22s}" + "".join(f"{n:>13s}" for n in nets)
print(hdr)
print(" " * 22 + "".join(f"{'(n=%d)'%tot[n]:>13s}" for n in nets))
for t, ts in COLL:
    line = f"{t:22s}"
    for n in nets:
        have = con.execute(f"""SELECT COUNT(*) c FROM watchlist w
            WHERE w.active=1 AND w.is_control=0 AND w.network_id=?
            AND EXISTS(SELECT 1 FROM {t} x WHERE x.token_address=w.token_address AND x.network_id=w.network_id)""",
            (n,)).fetchone()["c"]
        line += f"{('%d/%d' % (have, tot[n])):>13s}"
    print(line)
