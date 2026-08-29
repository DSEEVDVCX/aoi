import os, sqlite3, config, datetime as dt, json

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

NOW = dt.datetime.now(dt.timezone.utc)

def parse(s):
    if s is None:
        return None
    s = str(s)
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

COLL = [
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
]

# load watchlist
wl = [dict(r) for r in con.execute("SELECT token_address, network_id, active, is_control, first_seen_at, watch_until, source FROM watchlist")]
for w in wl:
    w["k"] = (str(w["token_address"]).lower(), str(w["network_id"] or ""))
    w["ke"] = (str(w["token_address"]), str(w["network_id"] or ""))

act = [w for w in wl if w["active"] == 1 and w["is_control"] == 0]
actc = [w for w in wl if w["active"] == 1 and w["is_control"] == 1]
print(f"active op={len(act)} active control={len(actc)} ever={len(wl)}")

data = {}
for t, ts in COLL:
    rows = con.execute(f'SELECT token_address, network_id, COUNT(*) n, MAX({ts}) mx FROM "{t}" GROUP BY token_address, network_id').fetchall()
    lo = {}
    ex = {}
    for r in rows:
        kl = (str(r["token_address"]).lower(), str(r["network_id"] or ""))
        ke = (str(r["token_address"]), str(r["network_id"] or ""))
        prev = lo.get(kl)
        mx = r["mx"]
        if prev is None or (mx or "") > (prev[1] or ""):
            lo[kl] = (r["n"], mx)
        else:
            lo[kl] = (prev[0] + r["n"], prev[1])
        ex[ke] = (r["n"], mx)
    data[t] = (lo, ex)
    print(f"loaded {t}: {len(rows)} groups, {len(lo)} lower-keys, {len(ex)} exact-keys")

def report(label, watches):
    print(f"\n################ COVERAGE: {label} (n={len(watches)}) ################")
    print(f"{'collector':22s} {'have':>5s} {'miss':>5s} {'missEXACT':>9s} {'p50min':>8s} {'p90min':>8s} {'max_min':>9s} {'>60m':>5s} {'>360m':>6s}")
    out = {}
    for t, _ts in COLL:
        lo, ex = data[t]
        have = [w for w in watches if w["k"] in lo]
        miss = [w for w in watches if w["k"] not in lo]
        misse = [w for w in watches if w["ke"] not in ex]
        ages = []
        for w in have:
            mx = parse(lo[w["k"]][1])
            if mx:
                ages.append((NOW - mx).total_seconds() / 60.0)
        ages.sort()
        def pct(p):
            if not ages: return float("nan")
            return ages[min(len(ages) - 1, int(p * len(ages)))]
        g60 = sum(1 for a in ages if a > 60)
        g360 = sum(1 for a in ages if a > 360)
        print(f"{t:22s} {len(have):>5d} {len(miss):>5d} {len(misse):>9d} {pct(0.5):>8.1f} {pct(0.9):>8.1f} {(max(ages) if ages else float('nan')):>9.1f} {g60:>5d} {g360:>6d}")
        out[t] = (have, miss, misse, ages)
    return out

res_op = report("active watchlist, is_control=0", act)
res_ct = report("active watchlist, is_control=1", actc)
res_all = report("every coin ever watched", wl)

# examples of misses for active op
print("\n=== examples: active op watches with ZERO rows per collector ===")
for t, _ in COLL:
    have, miss, misse, ages = res_op[t]
    if miss:
        ex = [(w["token_address"], w["network_id"], w["first_seen_at"], w["source"]) for w in miss[:6]]
        bynet = {}
        for w in miss:
            bynet[w["network_id"]] = bynet.get(w["network_id"], 0) + 1
        print(f"\n{t}: {len(miss)} missing; by network {bynet}")
        for e in ex:
            print("    ", e)

print("\n=== EXACT-case-only misses (case bug detector) on active op ===")
for t, _ in COLL:
    have, miss, misse, ages = res_op[t]
    delta = len(misse) - len(miss)
    if delta:
        print(f"{t}: lower-join misses {len(miss)}, exact-join misses {len(misse)} -> {delta} watches only match case-insensitively")
