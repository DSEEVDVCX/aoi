import os, sqlite3, config, datetime as dt, sys, collections

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
def P(*a): print(*a); sys.stdout.flush()
CUT = (NOW - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

def pp(s): return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))

def gap_intervals(sql, thresh=10.0, add_tail=True):
    ts = [pp(r[0]) for r in con.execute(sql)]
    out = []
    for a, b in zip(ts, ts[1:]):
        if (b-a).total_seconds()/60.0 > thresh:
            out.append((a, b))
    if add_tail and ts and (NOW - ts[-1]).total_seconds()/60.0 > thresh:
        out.append((ts[-1], NOW))
    span = (ts[-1]-ts[0]).total_seconds()/60.0 if len(ts) > 1 else 0.0
    return out, span, (ts[0] if ts else None)

base, base_span, base_start = gap_intervals(
    f"SELECT DISTINCT recorded_at FROM market_ticks WHERE recorded_at >= '{CUT}' ORDER BY 1", 10.0)
P(f"BASELINE market_ticks: span={base_span:.0f}min gaps={len(base)} total={sum((b-a).total_seconds()/60 for a,b in base):.0f}min")

def minus(iv, subs):
    """minutes of iv not covered by any interval in subs"""
    a, b = iv
    segs = [(a, b)]
    for s0, s1 in subs:
        new = []
        for x0, x1 in segs:
            if s1 <= x0 or s0 >= x1:
                new.append((x0, x1)); continue
            if s0 > x0: new.append((x0, s0))
            if s1 < x1: new.append((s1, x1))
        segs = new
    return sum((y-x).total_seconds()/60.0 for x, y in segs), segs

TARGETS = [
 ("chain_concentration Solana", f"SELECT DISTINCT recorded_at FROM chain_concentration WHERE network_id='1399811149' AND recorded_at >= '{CUT}' ORDER BY 1", 10.0),
 ("chain_authority (Solana)",   f"SELECT DISTINCT recorded_at FROM chain_authority WHERE recorded_at >= '{CUT}' ORDER BY 1", 30.0),
 ("chain_concentration BSC56",  f"SELECT DISTINCT recorded_at FROM chain_concentration WHERE network_id='56' AND recorded_at >= '{CUT}' ORDER BY 1", 30.0),
 ("evm_contract Base8453",      f"SELECT DISTINCT recorded_at FROM evm_contract WHERE network_id='8453' AND recorded_at >= '{CUT}' ORDER BY 1", 120.0),
 ("evm_balances 4663",          f"SELECT DISTINCT updated_at FROM evm_balances WHERE network_id='4663' AND updated_at >= '{CUT}' ORDER BY 1", 30.0),
 ("evm_balances 8453",          f"SELECT DISTINCT updated_at FROM evm_balances WHERE network_id='8453' AND updated_at >= '{CUT}' ORDER BY 1", 30.0),
 ("token_holders",              f"SELECT DISTINCT recorded_at FROM token_holders WHERE recorded_at >= '{CUT}' ORDER BY 1", 30.0),
 ("token_social",               f"SELECT DISTINCT recorded_at FROM token_social WHERE recorded_at >= '{CUT}' ORDER BY 1", 30.0),
]
for label, sql, th in TARGETS:
    iv, span, first = gap_intervals(sql, th)
    tot = sum((b-a).total_seconds()/60 for a, b in iv)
    extra_tot = 0.0
    details = []
    for g in iv:
        e, segs = minus(g, base)
        if e > th:
            extra_tot += e
            details.append((round(e,1), segs[0][0].isoformat(), segs[-1][1].isoformat()))
    P(f"\n{label}: gaps>{th}m n={len(iv)} total={tot:.0f}min; NOT explained by machine downtime = {extra_tot:.0f}min ({extra_tot/60:.1f}h)")
    details.sort(reverse=True)
    for d in details[:6]:
        P(f"     own-outage {d[0]:>8.1f} min  {d[1]} -> {d[2]}")

P("\n=== the 80 watched coins with ZERO market_ticks: do they have bars/holders/social? ===")
mt = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT DISTINCT token_address, network_id FROM market_ticks")}
bars = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT DISTINCT token_address, network_id FROM token_bars")}
soc = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT DISTINCT token_address, network_id FROM token_social")}
hol = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT DISTINCT token_address, network_id FROM token_holders")}
tr  = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT DISTINCT token_address, network_id FROM training_rows")}
wl = [dict(r) for r in con.execute("SELECT token_address, network_id, active, is_control, first_seen_at, source FROM watchlist")]
for w in wl: w["k"] = (str(w["token_address"]).lower(), str(w["network_id"] or ""))
nomt = [w for w in wl if w["k"] not in mt]
P(f"  n={len(nomt)}  with bars={sum(1 for w in nomt if w['k'] in bars)}  with social={sum(1 for w in nomt if w['k'] in soc)}  with holders={sum(1 for w in nomt if w['k'] in hol)}  with training_rows={sum(1 for w in nomt if w['k'] in tr)}")
P(f"  of the {len(nomt)}, ALL THREE of ticks/bars/holders absent: {sum(1 for w in nomt if w['k'] not in bars and w['k'] not in hol)}")

P("\n=== watch revival: how many windows per coin, and how many active watches are revivals ===")
ww = collections.Counter()
for r in con.execute("SELECT token_address, network_id, COUNT(*) n FROM watch_windows GROUP BY token_address, network_id"):
    ww[(str(r[0]).lower(), str(r[1] or ''))] = r[2]
act = [w for w in wl if w["active"]==1 and w["is_control"]==0]
rev = [w for w in act if ww.get(w["k"], 0) > 1]
P(f"  watch_windows rows={sum(ww.values())} over {len(ww)} coins; mean windows/coin={sum(ww.values())/max(1,len(ww)):.1f}")
P(f"  active-op={len(act)}; with >1 lifetime window (revivals)={len(rev)} ({100*len(rev)/max(1,len(act)):.0f}%)")
P(f"  WATCHLIST_CAP={config.WATCHLIST_CAP}; active-op over cap by {len(act)-config.WATCHLIST_CAP}")
P(f"  active-op with exactly 1 window (genuinely new)={len(act)-len(rev)}")
