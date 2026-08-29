import os, sqlite3, config, sys
from collections import Counter
def p(*a):
    print(*a); sys.stdout.flush()

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
rows = lambda s: con.execute(s).fetchall()

EVM_NETS = ('56','4663','8453','143')

p("=== per-network casing of token_address, per table ===")
TABLES = ["watchlist","watch_windows","signal_events","token_static","token_class",
          "outcomes","training_rows","token_holders","token_flow","chain_concentration",
          "evm_contract","evm_balances","token_social","token_bars","evm_replay_state",
          "evm_backfill_state","chain_fetch_state","holders_fetch_state"]
for t in TABLES:
    try:
        r = rows(f"""SELECT network_id,
                       COUNT(DISTINCT token_address) d,
                       SUM(CASE WHEN token_address=LOWER(token_address) THEN 1 ELSE 0 END) lower_rows,
                       SUM(CASE WHEN token_address<>LOWER(token_address) THEN 1 ELSE 0 END) mixed_rows
                     FROM {t} GROUP BY network_id ORDER BY network_id""")
        p(f"-- {t}")
        for x in r:
            tag = "EVM" if str(x[0]) in EVM_NETS else ("SOL" if str(x[0])=='1399811149' else "?")
            p(f"     net={x[0]:12s} {tag} distinct={x[1]:5d} lower_rows={x[2]:8d} mixed_rows={x[3]:8d}")
    except Exception as e:
        p(f"-- {t} FAILED {e}")

p("\n=== CROSS-TABLE: same coin under two casings across tables (EVM nets only) ===")
def distinct(t):
    return set(rows(f"SELECT DISTINCT token_address, network_id FROM {t} WHERE network_id IN ('56','4663','8453','143')"))
sets = {}
for t in ["watchlist","watch_windows","signal_events","token_static","outcomes",
          "training_rows","token_holders","token_flow","chain_concentration",
          "evm_contract","evm_balances","token_bars","evm_replay_state","token_class"]:
    try:
        sets[t] = distinct(t)
    except Exception as e:
        p(f"  {t} FAILED {e}")
p("EVM distinct (addr,net) per table:")
for t,s in sets.items():
    nmixed = sum(1 for a,n in s if a != a.lower())
    p(f"   {t:22s} {len(s):5d}  of which mixed-case addr = {nmixed}")

base = sets.get("watchlist", set())
p("\nFor each table: EVM coins present but under a DIFFERENT casing than watchlist:")
wl_by_lower = {(a.lower(),n): a for a,n in base}
for t,s in sets.items():
    if t == "watchlist": continue
    diffcase = [(a,n) for a,n in s if (a.lower(),n) in wl_by_lower and wl_by_lower[(a.lower(),n)] != a]
    exact_missing = [(a,n) for a,n in s if (a,n) not in base and (a.lower(),n) in wl_by_lower]
    p(f"   {t:22s} casing differs from watchlist: {len(diffcase):5d} | exact-miss but lower-match: {len(exact_missing)}")
    if diffcase[:2]:
        p(f"        e.g. {diffcase[0]}  vs watchlist {wl_by_lower[(diffcase[0][0].lower(),diffcase[0][1])]}")

p("\n=== the one address under >1 network_id ===")
for r in rows("""SELECT token_address, GROUP_CONCAT(DISTINCT network_id) FROM watchlist
                 GROUP BY LOWER(token_address) HAVING COUNT(DISTINCT network_id)>1"""):
    p("   watchlist:", r)
for r in rows("""SELECT token_address, network_id, COUNT(*) FROM token_bars
                 WHERE LOWER(token_address) IN (
                   SELECT LOWER(token_address) FROM token_bars GROUP BY LOWER(token_address)
                   HAVING COUNT(DISTINCT network_id)>1)
                 GROUP BY token_address, network_id"""):
    p("   token_bars:", r)
con.close()
