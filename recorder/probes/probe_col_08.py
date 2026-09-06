import os, sqlite3, config, datetime, statistics

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

NOW = datetime.datetime.now(datetime.timezone.utc)

def parse(s):
    if s is None:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d
    except Exception:
        return None

COLL = [
    ("market_ticks", "recorded_at"),
    ("token_bars", "fetched_at"),
    ("token_holders", "recorded_at"),
    ("token_social", "recorded_at"),
    ("token_flow", "recorded_at"),
    ("chain_concentration", "recorded_at"),
    ("chain_authority", "recorded_at"),
    ("evm_contract", "recorded_at"),
]

print("NOW:", NOW.isoformat())
active = con.execute("SELECT token_address, network_id FROM watchlist WHERE active=1 AND is_control=0").fetchall()
print("active operational:", len(active))

for t, ts in COLL:
    rows = con.execute(f"""
        SELECT w.token_address, w.network_id, (SELECT MAX(x.{ts}) FROM {t} x
             WHERE x.token_address=w.token_address AND x.network_id=w.network_id) mx
        FROM watchlist w WHERE w.active=1 AND w.is_control=0
    """).fetchall()
    ages = []
    missing = 0
    per_net = {}
    worst = []
    for r in rows:
        d = parse(r["mx"])
        if d is None:
            missing += 1
            continue
        a = (NOW - d).total_seconds() / 60.0
        ages.append(a)
        per_net.setdefault(r["network_id"], []).append(a)
        worst.append((a, r["token_address"], r["network_id"]))
    ages.sort()
    worst.sort(reverse=True)
    def pct(p):
        if not ages:
            return float("nan")
        return ages[min(len(ages) - 1, int(p / 100.0 * len(ages)))]
    over = {
        ">30m": sum(1 for a in ages if a > 30),
        ">60m": sum(1 for a in ages if a > 60),
        ">3h": sum(1 for a in ages if a > 180),
        ">12h": sum(1 for a in ages if a > 720),
        ">24h": sum(1 for a in ages if a > 1440),
    }
    print(f"\n--- {t} --- coins={len(ages)} no-rows={missing}")
    print(f"    age min: p50={pct(50):.1f} p90={pct(90):.1f} p99={pct(99):.1f} max={ages[-1] if ages else float('nan'):.1f}")
    print(f"    over: {over}")
    print("    per-net median age min: " + ", ".join(
        f"{n}={statistics.median(v):.1f}(n={len(v)})" for n, v in sorted(per_net.items(), key=lambda kv: -len(kv[1]))))
    print("    worst 5: " + "; ".join(f"{a:.0f}m {tok[:10]}..@{net}" for a, tok, net in worst[:5]))
