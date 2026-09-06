import os, sqlite3, config, datetime as dt, collections, sys

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
def P(*a): print(*a); sys.stdout.flush()
P("now:", NOW.isoformat())

wl = [dict(r) for r in con.execute("SELECT token_address, network_id, active, is_control, first_seen_at, watch_until, source FROM watchlist")]
for w in wl:
    w["k"] = (str(w["token_address"]).lower(), str(w["network_id"] or ""))
act = [w for w in wl if w["active"]==1 and w["is_control"]==0]
actsol = [w for w in act if w["network_id"]=="1399811149"]
P(f"active-op={len(act)} of which Solana={len(actsol)}")

P("\n=== Solana concentration hourly rows, last 30h ===")
for r in con.execute("""SELECT substr(recorded_at,1,13) h, COUNT(*) n, COUNT(DISTINCT token_address) coins
     FROM chain_concentration WHERE network_id='1399811149' AND recorded_at >= '2026-08-21T17'
     GROUP BY 1 ORDER BY 1"""):
    P(f"   {r['h']}  rows={r['n']:>5}  coins={r['coins']:>4}")

P("\n=== Solana chain_authority hourly rows, last 30h ===")
for r in con.execute("""SELECT substr(recorded_at,1,13) h, COUNT(*) n, COUNT(DISTINCT token_address) coins
     FROM chain_authority WHERE recorded_at >= '2026-08-21T17' GROUP BY 1 ORDER BY 1"""):
    P(f"   {r['h']}  rows={r['n']:>5}  coins={r['coins']:>4}")

P("\n=== active Solana watches: newest concentration age ===")
conc = {}
for r in con.execute("SELECT token_address, MAX(recorded_at) mx FROM chain_concentration WHERE network_id='1399811149' GROUP BY token_address"):
    conc[str(r["token_address"]).lower()] = r["mx"]
def age(s):
    if s is None: return None
    try: p = dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception: return None
    return (NOW-p).total_seconds()/60.0
ages = []
miss = 0
for w in actsol:
    a = age(conc.get(w["k"][0]))
    if a is None: miss += 1
    else: ages.append(a)
ages.sort()
P(f"  Solana active-op={len(actsol)} no-row={miss} with-row={len(ages)}")
if ages:
    P(f"  age min: p50={ages[len(ages)//2]:.0f} p90={ages[int(.9*len(ages))]:.0f} max={max(ages):.0f} min={min(ages):.0f}")
    P(f"  >60min: {sum(1 for a in ages if a>60)}   >180min: {sum(1 for a in ages if a>180)}")

P("\n=== thesis: fast, via grouped sets ===")
soc = {}
for r in con.execute("SELECT token_address, network_id, MAX(COALESCE(thesis_total,0)) tt, MAX(COALESCE(thesis_count,0)) tc FROM token_social GROUP BY token_address, network_id"):
    soc[(str(r["token_address"]).lower(), str(r["network_id"] or ""))] = (r["tt"], r["tc"])
th = set()
for r in con.execute("SELECT DISTINCT token_address, network_id FROM token_thesis"):
    th.add((str(r["token_address"]).lower(), str(r["network_id"] or "")))
P(f"  token_social coins={len(soc)}  token_thesis coins={len(th)}")
pos = [k for k,v in soc.items() if (v[0] or 0) > 0]
P(f"  coins where live social saw thesis_total>0: {len(pos)}")
P(f"    of those with ZERO token_thesis rows: {sum(1 for k in pos if k not in th)}")
actpos = [w for w in act if (soc.get(w['k'],(0,0))[0] or 0) > 0]
P(f"  active-op watches with thesis_total>0: {len(actpos)}; of those zero detail rows: {sum(1 for w in actpos if w['k'] not in th)}")
P(f"  active-op watches with ZERO token_thesis rows at all: {sum(1 for w in act if w['k'] not in th)} of {len(act)}")

P("\n=== 1D bars / historical backfill coverage ===")
d1 = set()
for r in con.execute("SELECT DISTINCT token_address, network_id FROM token_bars WHERE resolution='1D'"):
    d1.add((str(r["token_address"]).lower(), str(r["network_id"] or "")))
hbs = {}
for r in con.execute("SELECT token_address, network_id, resolution, last_status, updated_at FROM historical_bars_state"):
    hbs[(str(r["token_address"]).lower(), str(r["network_id"] or ""), r["resolution"])] = (r["last_status"], r["updated_at"])
P(f"  coins with 1D bars: {len(d1)}; historical_bars_state rows: {len(hbs)}")
P(f"  active-op with NO 1D bars: {sum(1 for w in act if w['k'] not in d1)} of {len(act)}")
P(f"  ever-watched with NO 1D bars: {sum(1 for w in wl if w['k'] not in d1)} of {len(wl)}")
okhbs = {(a,n) for (a,n,res),(st,_u) in hbs.items() if res=='1D' and st=='ok'}
P(f"  active-op with historical_bars_state 1D ok: {sum(1 for w in act if w['k'] in okhbs)} of {len(act)}")
# coins first seen after the backfill stopped
CUT = "2026-08-18T22:12"
late = [w for w in act if (w["first_seen_at"] or "") > CUT]
P(f"  active-op first_seen after backfill stopped ({CUT}): {len(late)}; of those with 1D ok: {sum(1 for w in late if w['k'] in okhbs)}")

P("\n=== token_class coverage ===")
tc = set()
for r in con.execute("SELECT token_address, network_id FROM token_class"):
    tc.add((str(r["token_address"]).lower(), str(r["network_id"] or "")))
P(f"  token_class coins={len(tc)}")
P(f"  active-op missing token_class: {sum(1 for w in act if w['k'] not in tc)} of {len(act)}")
P(f"  ever-watched missing token_class: {sum(1 for w in wl if w['k'] not in tc)} of {len(wl)}")

P("\n=== coins ever watched with ZERO market_ticks ===")
mt = set()
for r in con.execute("SELECT DISTINCT token_address, network_id FROM market_ticks"):
    mt.add((str(r["token_address"]).lower(), str(r["network_id"] or "")))
nomt = [w for w in wl if w["k"] not in mt]
P(f"  count={len(nomt)}")
P("  by month first_seen:", dict(collections.Counter((w['first_seen_at'] or '')[:7] for w in nomt)))
P("  by (is_control,active):", dict(collections.Counter((w['is_control'], w['active']) for w in nomt)))
P("  by network:", dict(collections.Counter(w['network_id'] for w in nomt)))
P("  by source:", dict(collections.Counter(w['source'] for w in nomt)))
for w in nomt[:6]:
    P("   ", w['token_address'], w['network_id'], w['first_seen_at'], w['source'], 'ctl=' + str(w['is_control']))
