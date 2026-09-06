import os, sqlite3, config, datetime as dt, collections, sys

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
def P(*a):
    print(*a); sys.stdout.flush()
P("now:", NOW.isoformat())

def parse(s):
    if s is None: return None
    try: return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception: return None

def age(s):
    p = parse(s)
    return None if p is None else round((NOW-p).total_seconds()/60.0, 1)

P("\n=== A. per-network newest row (grouped, no now() scan) ===")
for t, ts in [("chain_concentration","recorded_at"),("chain_authority","recorded_at"),
              ("evm_contract","recorded_at"),("evm_balances","updated_at"),
              ("market_ticks","recorded_at"),("token_holders","recorded_at"),
              ("token_flow","recorded_at"),("token_social","recorded_at"),
              ("token_bars","fetched_at"),("token_static","recorded_at")]:
    P(f"-- {t}")
    for r in con.execute(f'SELECT network_id, COUNT(*) n, COUNT(DISTINCT token_address) coins, MAX({ts}) mx FROM "{t}" GROUP BY network_id ORDER BY n DESC'):
        P(f"   net={str(r['network_id']):12s} n={r['n']:>9,} coins={r['coins']:>5,} newest={r['mx']}  age_min={age(r['mx'])}")

P("\n=== A2. chain_concentration by network x is_replay ===")
for r in con.execute("SELECT network_id, is_replay, COUNT(*) n, MAX(recorded_at) mx FROM chain_concentration GROUP BY network_id, is_replay ORDER BY network_id, is_replay"):
    P(f"   net={str(r['network_id']):12s} is_replay={r['is_replay']} n={r['n']:>8,} newest={r['mx']} age_min={age(r['mx'])}")

P("\n=== A3. chain_concentration: newest LIVE (is_replay=0) Solana vs BSC ===")
for r in con.execute("""SELECT network_id, COUNT(*) n, MAX(recorded_at) mx FROM chain_concentration
                        WHERE is_replay=0 GROUP BY network_id"""):
    P(f"   net={str(r['network_id']):12s} n={r['n']:>8,} newest={r['mx']} age_min={age(r['mx'])}")
