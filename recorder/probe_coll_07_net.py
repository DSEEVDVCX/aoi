import os, sqlite3, config, datetime as dt

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
print("now:", NOW.isoformat())

print("\n=== A. per-network newest row + rows in last 3h ===")
for t, ts in [("chain_concentration","recorded_at"),("chain_authority","recorded_at"),
              ("evm_contract","recorded_at"),("evm_balances","updated_at"),
              ("market_ticks","recorded_at"),("token_holders","recorded_at"),
              ("token_flow","recorded_at"),("token_social","recorded_at"),
              ("token_bars","fetched_at"),("token_static","recorded_at")]:
    print(f"\n-- {t}")
    for r in con.execute(f"""SELECT network_id, COUNT(*) n,
                COUNT(DISTINCT lower(token_address)) coins,
                MAX({ts}) mx,
                SUM(CASE WHEN {ts} >= datetime('now','-3 hours') THEN 1 ELSE 0 END) last3h,
                SUM(CASE WHEN {ts} >= datetime('now','-30 minutes') THEN 1 ELSE 0 END) last30m
              FROM "{t}" GROUP BY network_id ORDER BY n DESC"""):
        print(f"   net={str(r['network_id']):12s} n={r['n']:>9,} coins={r['coins']:>5,} newest={r['mx']} last3h={r['last3h']:>7,} last30m={r['last30m']:>6,}")

print("\n=== A2. chain_concentration: is_replay split + newest per network ===")
for r in con.execute("""SELECT network_id, is_replay, COUNT(*) n, MAX(recorded_at) mx
                        FROM chain_concentration GROUP BY network_id, is_replay ORDER BY network_id, is_replay"""):
    print(dict(zip(r.keys(), tuple(r))))

print("\n=== B. thesis: coins where the LIVE social collector saw theses but detail table is empty ===")
r = con.execute("""SELECT COUNT(*) coins FROM (
      SELECT lower(token_address) a, network_id n, MAX(COALESCE(thesis_total,0)) tt
        FROM token_social GROUP BY 1,2) s
     WHERE s.tt > 0""").fetchone()
print("coins with thesis_total>0 ever (token_social):", r["coins"])
r = con.execute("""SELECT COUNT(*) coins FROM (
      SELECT lower(token_address) a, network_id n, MAX(COALESCE(thesis_total,0)) tt
        FROM token_social GROUP BY 1,2) s
     WHERE s.tt > 0
       AND NOT EXISTS (SELECT 1 FROM token_thesis th
                        WHERE lower(th.token_address)=s.a AND th.network_id=s.n)""").fetchone()
print("  of those, ZERO rows in token_thesis:", r["coins"])
print("\n  newest token_thesis.fetched_at:", con.execute("SELECT MAX(fetched_at) FROM token_thesis").fetchone()[0])
print("  newest token_social.recorded_at:", con.execute("SELECT MAX(recorded_at) FROM token_social").fetchone()[0])

print("\n=== B2. active operational watches: thesis_total>0 but no token_thesis rows ===")
rows = con.execute("""SELECT w.token_address, w.network_id, w.first_seen_at,
             (SELECT MAX(COALESCE(thesis_total,0)) FROM token_social s
               WHERE lower(s.token_address)=lower(w.token_address) AND s.network_id=w.network_id) tt,
             (SELECT COUNT(*) FROM token_thesis th
               WHERE lower(th.token_address)=lower(w.token_address) AND th.network_id=w.network_id) th
        FROM watchlist w WHERE w.active=1 AND w.is_control=0""").fetchall()
gap = [r for r in rows if (r["tt"] or 0) > 0 and (r["th"] or 0) == 0]
print(f"active-op watches: {len(rows)}; thesis_total>0 and zero detail rows: {len(gap)}")
for r in gap[:8]:
    print("   ", r["token_address"], r["network_id"], "thesis_total=", r["tt"], "first_seen=", r["first_seen_at"])

print("\n=== C. 1D bars coverage over active op watches ===")
r = con.execute("""SELECT COUNT(*) n FROM watchlist w WHERE w.active=1 AND w.is_control=0
    AND NOT EXISTS (SELECT 1 FROM token_bars b WHERE lower(b.token_address)=lower(w.token_address)
       AND b.network_id=w.network_id AND b.resolution='1D')""").fetchone()
print("active-op with NO 1D bars:", r["n"], "of 169")
r = con.execute("""SELECT COUNT(*) n FROM watchlist w WHERE w.active=1 AND w.is_control=0
    AND EXISTS (SELECT 1 FROM historical_bars_state s WHERE lower(s.token_address)=lower(w.token_address)
       AND s.network_id=w.network_id AND s.last_status='ok' AND s.resolution='1D')""").fetchone()
print("active-op with historical_bars_state ok(1D):", r["n"])
print("newest historical_bars_state.updated_at:", con.execute("SELECT MAX(updated_at) FROM historical_bars_state").fetchone()[0])
for r in con.execute("SELECT resolution, COUNT(*) n, MAX(updated_at) mx FROM historical_bars_state GROUP BY resolution"):
    print("  hbs resolution", dict(zip(r.keys(), tuple(r))))

print("\n=== F. coins ever watched with ZERO market_ticks: when were they first seen? ===")
rows = con.execute("""SELECT w.token_address, w.network_id, w.first_seen_at, w.is_control, w.active, w.source
        FROM watchlist w
       WHERE NOT EXISTS (SELECT 1 FROM market_ticks m
             WHERE lower(m.token_address)=lower(w.token_address) AND m.network_id=w.network_id)""").fetchall()
print("count:", len(rows))
import collections
c = collections.Counter((r["first_seen_at"] or "")[:7] for r in rows)
print("by month:", dict(c))
c2 = collections.Counter((r["is_control"], r["active"]) for r in rows)
print("by (is_control,active):", dict(c2))
c3 = collections.Counter(r["network_id"] for r in rows)
print("by network:", dict(c3))
for r in rows[:6]:
    print("   ", tuple(r))
