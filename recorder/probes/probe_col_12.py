import os, sqlite3, config, datetime, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=600)
con.row_factory = sqlite3.Row

NOW = datetime.datetime.now(datetime.timezone.utc)
CUT = (NOW - datetime.timedelta(days=7)).isoformat()
print("NOW:", NOW.isoformat(), " cutoff(7d):", CUT)

def parse(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d

def gaps(name, sql, params, thresh_min=10.0):
    t0 = time.time()
    stamps = [parse(r[0]) for r in con.execute(sql, params)]
    stamps.sort()
    print(f"\n--- {name} --- distinct stamps in window: {len(stamps)}  (query {time.time()-t0:.1f}s)")
    if not stamps:
        return
    print(f"    first={stamps[0].isoformat()}  last={stamps[-1].isoformat()}")
    period_min = (stamps[-1] - stamps[0]).total_seconds() / 60.0
    gl = []
    for a, b in zip(stamps, stamps[1:]):
        g = (b - a).total_seconds() / 60.0
        if g > thresh_min:
            gl.append((g, a, b))
    gl.sort(reverse=True)
    total = sum(g for g, _, _ in gl)
    # also the lead gap: from last stamp to now
    lead = (NOW - stamps[-1]).total_seconds() / 60.0
    print(f"    span={period_min:.1f} min ({period_min/60:.1f} h); gaps>{thresh_min}min: {len(gl)}"
          f"; total gap={total:.1f} min = {100.0*total/period_min:.2f}% of span")
    print(f"    trailing lag (last stamp -> now) = {lead:.1f} min")
    for g, a, b in gl[:15]:
        print(f"      {g:8.1f} min  {a.isoformat()[:19]} -> {b.isoformat()[:19]}")

gaps("market_ticks (recorded_at, 7d)",
     "SELECT DISTINCT recorded_at FROM market_ticks WHERE recorded_at >= ?", (CUT,))
gaps("token_bars (fetched_at, 7d)",
     "SELECT DISTINCT fetched_at FROM token_bars WHERE fetched_at >= ?", (CUT,))
gaps("snapshots (recorded_at, 7d) = raw cycle heartbeat",
     "SELECT DISTINCT recorded_at FROM snapshots WHERE recorded_at >= ?", (CUT,))
