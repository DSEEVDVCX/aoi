import os
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

print("chain_concentration cols:",
      [r["name"] for r in con.execute("PRAGMA table_info(chain_concentration)")])
print("evm_contract cols:",
      [r["name"] for r in con.execute("PRAGMA table_info(evm_contract)")])

with open(os.path.join(HERE, "probe_ef_rows.pkl"), "rb") as fh:
    blob = pickle.load(fh)
with open(os.path.join(HERE, "probe_ef_fam.pkl"), "rb") as fh:
    FAM = pickle.load(fh)
with open(os.path.join(HERE, "probe_ef_src.pkl"), "rb") as fh:
    SRC = pickle.load(fh)
cols, rows = blob["cols"], blob["rows"]
ix = {c: i for i, c in enumerate(cols)}
FIDX = {k: [ix[c] for c in v] for k, v in FAM.items()}
pop = [r for r in rows if r[ix["is_live"]] == 1 and r[ix["is_independent"]] == 1]
NET = {"1399811149": "solana", "8453": "base", "4663": "robinhood", "56": "bsc"}


def day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# --- per-network entry/built ranges ---
print("\n=== per-network entry_ts and built_at range (model population) ===")
agg = defaultdict(lambda: [None, None, set(), 0])
for r in pop:
    k = NET.get(str(r[ix["network_id"]]), str(r[ix["network_id"]]))
    a = agg[k]
    d = day(r[ix["entry_ts"]])
    a[0] = d if a[0] is None or d < a[0] else a[0]
    a[1] = d if a[1] is None or d > a[1] else a[1]
    a[2].add(str(r[ix["built_at"]])[:10])
    a[3] += 1
for k, v in sorted(agg.items(), key=lambda x: -x[1][3]):
    print("  %-10s n=%5d entry %s..%s  built_days=%s" % (
        k, v[3], v[0], v[1], ",".join(sorted(v[2]))))

# --- the 659 reachable-but-NULL onchain_conc rows ---
ex = SRC["onchain_conc"]["exact"]
bad = []
for r in pop:
    if all(r[i] is None for i in FIDX["onchain_conc"]):
        e = ex.get((r[ix["token_address"]], str(r[ix["network_id"]])))
        if e is not None and e <= r[ix["entry_ts"]]:
            bad.append(r)
print("\n=== %d rows: onchain_conc entirely NULL but chain_concentration reachable ===" % len(bad))
print("  entry days:", dict(sorted(Counter(day(r[ix["entry_ts"]]) for r in bad).items())))
print("  built days:", dict(sorted(Counter(str(r[ix["built_at"]])[:10] for r in bad).items())))
print("  networks  :", dict(Counter(NET.get(str(r[ix["network_id"]])) for r in bad)))
print("  distinct tokens:", len({r[ix["token_address"]] for r in bad}))

# --- live re-run of the exact features.py query for 5 of them ---
q = """SELECT top1_pct, top5_pct, top10_pct, top20_pct, top_accounts, holder_count,
              CAST(strftime('%s', recorded_at) AS INTEGER) e, recorded_at
         FROM chain_concentration
        WHERE token_address=? AND network_id=?
          AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
        ORDER BY e DESC LIMIT 1"""
print("\n  re-running features.onchain_features' exact query for 5 of them:")
for r in bad[:5]:
    got = con.execute(q, (r[ix["token_address"]], str(r[ix["network_id"]]),
                          r[ix["entry_ts"]])).fetchone()
    print("   key=%s built=%s entry=%s -> %s" % (
        r[ix["key"]][:20], str(r[ix["built_at"]])[:19], day(r[ix["entry_ts"]]),
        dict(got) if got else None))

# --- Base rows vs evm_contract ---
base = [r for r in pop if str(r[ix["network_id"]]) == "8453"]
ec_addr = {a for (a, n) in SRC["onchain_contract_evm"]["exact"]}
ec_min = SRC["onchain_contract_evm"]["exact"]
print("\n=== BASE (8453) population rows vs evm_contract ===")
print("  base rows:", len(base))
print("  base rows whose token_address appears in evm_contract AT ALL:",
      sum(1 for r in base if r[ix["token_address"]] in ec_addr))
print("  base rows whose token_address appears (case-insensitive):",
      sum(1 for r in base if str(r[ix["token_address"]]).lower() in
          {a.lower() for a in ec_addr}))
ecs = con.execute("SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) m FROM evm_contract").fetchone()["m"]
print("  evm_contract earliest recorded_at epoch:", ecs, day(ecs))
print("  base rows with entry_ts >= that:", sum(1 for r in base if r[ix["entry_ts"]] >= ecs))
print("  base entry days:", dict(sorted(Counter(day(r[ix["entry_ts"]]) for r in base).items())))
print("  base built days:", dict(sorted(Counter(str(r[ix["built_at"]])[:10] for r in base).items())))
# do evm_contract addresses intersect the WHOLE table's base tokens?
allbase = {r[0] for r in con.execute(
    "SELECT DISTINCT token_address FROM training_rows WHERE network_id='8453'")}
print("  distinct base addresses in training_rows:", len(allbase))
print("  of evm_contract's 74 addresses, how many are in training_rows(8453):",
      len(ec_addr & allbase))
con.close()
