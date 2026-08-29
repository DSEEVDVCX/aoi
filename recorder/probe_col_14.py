import os, sqlite3, config, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

def parse(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d

NOW = datetime.datetime.now(datetime.timezone.utc)

# 1) Solana concentration cycle stamps -> gaps
print("=== chain_concentration Solana (is_replay=0) distinct recorded_at gaps > 10 min, last 7d ===")
cut = (NOW - datetime.timedelta(days=7)).isoformat()
st = sorted(parse(r[0]) for r in con.execute(
    "SELECT DISTINCT recorded_at FROM chain_concentration WHERE network_id='1399811149' AND is_replay=0 AND recorded_at>=?", (cut,)))
print("stamps:", len(st), st[0].isoformat(), "->", st[-1].isoformat())
span = (st[-1] - st[0]).total_seconds() / 60
gl = [((b - a).total_seconds() / 60, a, b) for a, b in zip(st, st[1:]) if (b - a).total_seconds() > 600]
gl.sort(reverse=True)
tot = sum(g for g, _, _ in gl)
lead = (NOW - st[-1]).total_seconds() / 60
print(f"span={span:.1f}min gaps={len(gl)} total={tot:.1f}min = {100*tot/span:.2f}%  trailing lag={lead:.1f}min")
for g, a, b in gl[:12]:
    print(f"   {g:8.1f} min  {a.isoformat()[:19]} -> {b.isoformat()[:19]}")

# 2) chain_authority gaps
print()
print("=== chain_authority distinct recorded_at gaps > 30 min, last 7d ===")
st2 = sorted(parse(r[0]) for r in con.execute(
    "SELECT DISTINCT recorded_at FROM chain_authority WHERE recorded_at>=?", (cut,)))
print("stamps:", len(st2), st2[0].isoformat(), "->", st2[-1].isoformat())
span2 = (st2[-1] - st2[0]).total_seconds() / 60
gl2 = [((b - a).total_seconds() / 60, a, b) for a, b in zip(st2, st2[1:]) if (b - a).total_seconds() > 1800]
gl2.sort(reverse=True)
print(f"span={span2:.1f}min gaps>30m={len(gl2)} total={sum(g for g,_,_ in gl2):.1f}min "
      f"trailing lag={(NOW-st2[-1]).total_seconds()/60:.1f}min")
for g, a, b in gl2[:10]:
    print(f"   {g:8.1f} min  {a.isoformat()[:19]} -> {b.isoformat()[:19]}")

# 3) signals landing inside the two chain-layer crash-loop windows
print()
print("=== signal_events on Solana inside the two chain crash-loop windows ===")
for lo, hi, lbl in (("2026-08-20T16:24:00", "2026-08-21T02:15:00", "episode 1 (9h51m)"),
                    ("2026-08-22T19:25:03", "2026-08-23T00:00:00", "episode 2 (ongoing)")):
    r = con.execute("""SELECT COUNT(*) n, COUNT(DISTINCT token_address) coins FROM signal_events
        WHERE network_id='1399811149' AND signal_type IN ('multi_user_buy','large_buy')
          AND ts>=? AND ts<?""", (lo, hi)).fetchone()
    print(f"   {lbl}: solana trigger signals={r['n']} coins={r['coins']}")

# 4) how many active Solana watches are stale beyond the documented 300s target
print()
print("=== active Solana operational watches vs CHAIN_REFRESH_SECONDS=300 (5 min) ===")
rows = con.execute("""SELECT w.token_address, (SELECT MAX(recorded_at) FROM chain_concentration c
        WHERE c.token_address=w.token_address AND c.network_id=w.network_id AND c.is_replay=0) mx
        FROM watchlist w WHERE w.active=1 AND w.is_control=0 AND w.network_id='1399811149'""").fetchall()
ages = []
none_ct = 0
for r in rows:
    if r["mx"] is None:
        none_ct += 1
        continue
    ages.append((NOW - parse(r["mx"])).total_seconds() / 60)
ages.sort()
print(f"   active solana={len(rows)} with-no-row={none_ct} measured={len(ages)}")
print(f"   fresher than 5min: {sum(1 for a in ages if a<=5)}  >5min: {sum(1 for a in ages if a>5)}"
      f"  >60min: {sum(1 for a in ages if a>60)}  >3h: {sum(1 for a in ages if a>180)}")
print(f"   p50={ages[len(ages)//2]:.1f}min max={ages[-1]:.1f}min")
