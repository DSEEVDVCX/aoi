import os, sqlite3, config, datetime as dt

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)

def parse(s):
    if s is None: return None
    try: return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception: return None

print("=== META (all keys) ===")
for r in con.execute("SELECT key, value FROM meta ORDER BY key"):
    v = str(r["value"])
    if len(v) > 160: v = v[:160] + "..."
    print(f"{r['key']:44s} = {v}")

STATES = [
 ("bars_fetch_state","last_fetch_at","last_status"),
 ("holders_fetch_state","last_fetch_at","last_status"),
 ("social_fetch_state","last_fetch_at","last_status"),
 ("chain_fetch_state","last_fetch_at","last_status"),
 ("chain_auth_state","last_fetch_at","last_status"),
 ("evm_contract_state","last_fetch_at","last_status"),
 ("activity_bars_state","last_fetch_at","last_status"),
 ("historical_bars_state","updated_at","last_status"),
 ("evm_backfill_state","last_try_at","status"),
 ("evm_replay_state","last_try_at","status"),
]

wl = [dict(r) for r in con.execute("SELECT token_address, network_id, active, is_control FROM watchlist")]
actkeys = {(str(w['token_address']).lower(), str(w['network_id'] or '')) for w in wl if w['active']==1 and w['is_control']==0}
actall  = {(str(w['token_address']).lower(), str(w['network_id'] or '')) for w in wl if w['active']==1}

for t, tsc, stc in STATES:
    print(f"\n=== {t} ===")
    for r in con.execute(f'SELECT "{stc}" st, COUNT(*) n, MIN("{tsc}") mn, MAX("{tsc}") mx FROM "{t}" GROUP BY "{stc}" ORDER BY n DESC'):
        print(f"   status={str(r['st']):18s} n={r['n']:>6,} min={r['mn']} max={r['mx']}")
    # staleness for active operational watches
    rows = con.execute(f'SELECT token_address, network_id, "{tsc}" ts, "{stc}" st FROM "{t}"').fetchall()
    m = {}
    for r in rows:
        k = (str(r['token_address']).lower(), str(r['network_id'] or ''))
        cur = m.get(k)
        if cur is None or (r['ts'] or '') > (cur[0] or ''):
            m[k] = (r['ts'], r['st'])
    have = [k for k in actkeys if k in m]
    print(f"   active-op coverage in state table: {len(have)}/{len(actkeys)}")
    ages = []
    stale = []
    for k in have:
        p = parse(m[k][0])
        if p:
            a = (NOW-p).total_seconds()/60.0
            ages.append(a)
            if a > 120: stale.append((round(a), m[k][1], k[0][:16], k[1]))
    ages.sort()
    if ages:
        print(f"   active-op last_fetch age min: p50={ages[len(ages)//2]:.1f} p90={ages[int(.9*len(ages))]:.1f} max={max(ages):.1f}  >120m={len(stale)}")
    stale.sort(reverse=True)
    for s in stale[:8]:
        print("      stale:", s)
