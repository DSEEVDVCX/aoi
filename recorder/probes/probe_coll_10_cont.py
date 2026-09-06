import os, sqlite3, config, datetime as dt, sys, collections

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
def P(*a): print(*a); sys.stdout.flush()

CUT = (NOW - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
P("now:", NOW.isoformat(), " 7d cutoff:", CUT)

def gaps(label, sql, thresh_min=10.0):
    ts = [r[0] for r in con.execute(sql)]
    P(f"\n--- {label}: {len(ts)} distinct timestamps")
    if len(ts) < 2:
        return
    def pp(s):
        return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    prev = pp(ts[0])
    g = []
    for s in ts[1:]:
        cur = pp(s)
        d = (cur - prev).total_seconds()/60.0
        if d > thresh_min:
            g.append((prev.isoformat(), cur.isoformat(), round(d,1)))
        prev = cur
    span = (pp(ts[-1]) - pp(ts[0])).total_seconds()/60.0
    tot = sum(x[2] for x in g)
    P(f"    span={span:.0f} min ({span/60:.1f} h)  gaps>{thresh_min}m: {len(g)}  total_gap={tot:.0f} min ({tot/60:.1f} h) = {100*tot/span:.2f}% of span")
    g.sort(key=lambda x: -x[2])
    for x in g[:12]:
        P(f"      {x[2]:>8.1f} min   {x[0]} -> {x[1]}")
    return g

# 1) market_ticks: the recorder's own 60s heartbeat (distinct recorded_at)
gaps("market_ticks distinct recorded_at (7d)",
     f"SELECT DISTINCT recorded_at FROM market_ticks WHERE recorded_at >= '{CUT}' ORDER BY 1")

# 2) token_bars fetched_at heartbeat
gaps("token_bars distinct fetched_at (7d)",
     f"SELECT DISTINCT fetched_at FROM token_bars WHERE fetched_at >= '{CUT}' ORDER BY 1")

# 3) snapshots feed heartbeat = the cycle itself
gaps("snapshots(source='feed') recorded_at (7d)",
     f"SELECT DISTINCT recorded_at FROM snapshots WHERE source='feed' AND recorded_at >= '{CUT}' ORDER BY 1")

# 4) Solana chain layer heartbeat
gaps("chain_concentration Solana recorded_at (7d)",
     f"SELECT DISTINCT recorded_at FROM chain_concentration WHERE network_id='1399811149' AND recorded_at >= '{CUT}' ORDER BY 1")

# 5) chain_authority heartbeat
gaps("chain_authority recorded_at (7d)",
     f"SELECT DISTINCT recorded_at FROM chain_authority WHERE recorded_at >= '{CUT}' ORDER BY 1", 30.0)

# 6) BSC concentration (nodereal) heartbeat
gaps("chain_concentration BSC(56) recorded_at (7d)",
     f"SELECT DISTINCT recorded_at FROM chain_concentration WHERE network_id='56' AND recorded_at >= '{CUT}' ORDER BY 1", 30.0)

# 7) evm_contract Base heartbeat
gaps("evm_contract Base(8453) recorded_at (7d)",
     f"SELECT DISTINCT recorded_at FROM evm_contract WHERE network_id='8453' AND recorded_at >= '{CUT}' ORDER BY 1", 120.0)
