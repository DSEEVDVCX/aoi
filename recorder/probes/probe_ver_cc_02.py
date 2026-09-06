import os, sqlite3, config
from collections import Counter, defaultdict

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = int(con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
).fetchone()[0])
print("feature_version =", FV)

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=?")

n_pop = con.execute(f"SELECT COUNT(*) FROM training_rows WHERE {POP}", (FV,)).fetchone()[0]
print("population rows =", n_pop)

print("\npopulation by network:")
for r in con.execute(f"SELECT network_id, COUNT(*) n FROM training_rows WHERE {POP} "
                     "GROUP BY network_id ORDER BY n DESC", (FV,)):
    print("  ", r["network_id"], r["n"])

CONC = ["onchain_top1_pct","onchain_top5_pct","onchain_top10_pct","onchain_top20_pct",
        "onchain_top_accounts","onchain_age_min","onchain_top1_delta_5m",
        "onchain_top10_delta_5m","onchain_delta_span_min","onchain_holder_count",
        "onchain_holders_delta_5m"]

# how many are all-NULL, and does "all NULL" == "onchain_age_min IS NULL"?
allnull = " AND ".join(f"{c} IS NULL" for c in CONC)
n_all = con.execute(f"SELECT COUNT(*) FROM training_rows WHERE {POP} AND {allnull}", (FV,)).fetchone()[0]
n_age = con.execute(f"SELECT COUNT(*) FROM training_rows WHERE {POP} AND onchain_age_min IS NULL", (FV,)).fetchone()[0]
print(f"\nall-11-NULL = {n_all}   onchain_age_min IS NULL = {n_age}  (equal => age_min is the sentinel)")

print("\nall-11-NULL by network:")
for r in con.execute(f"SELECT network_id, COUNT(*) n FROM training_rows WHERE {POP} AND {allnull} "
                     "GROUP BY network_id ORDER BY n DESC", (FV,)):
    print("  ", r["network_id"], r["n"])

# ---- independent reachability: earliest snapshot epoch per (token, network), in Python
first = {}
for r in con.execute("""SELECT token_address, network_id,
                               MIN(CAST(strftime('%s', recorded_at) AS INTEGER)) fe,
                               COUNT(*) n
                          FROM chain_concentration GROUP BY token_address, network_id"""):
    first[(r["token_address"], r["network_id"])] = (r["fe"], r["n"])
print("\nchain_concentration distinct (token,network) =", len(first))

lower = defaultdict(list)
for (tok, net), v in first.items():
    lower[(tok.lower(), net)].append(v)

rows = con.execute(
    f"SELECT key, token_address, network_id, entry_ts, built_at FROM training_rows "
    f"WHERE {POP} AND {allnull}", (FV,)).fetchall()
print("fetched all-NULL rows:", len(rows))

reach_exact = []
reach_ci_only = 0
for r in rows:
    k = (r["token_address"], r["network_id"])
    v = first.get(k)
    if v is not None and v[0] is not None and v[0] <= r["entry_ts"]:
        reach_exact.append(r)
        continue
    cands = lower.get((r["token_address"].lower(), r["network_id"]), [])
    if any(fe is not None and fe <= r["entry_ts"] for fe, _ in cands):
        reach_ci_only += 1

print("\nREACHABLE (exact-case, earliest snapshot <= entry_ts):", len(reach_exact))
print("extra rows recovered only by case-insensitive match:", reach_ci_only)
print("by network:", Counter(r["network_id"] for r in reach_exact))
print("distinct tokens:", len({r["token_address"] for r in reach_exact}))
print("built_at days:", Counter((r["built_at"] or "")[:10] for r in reach_exact))
import datetime as dt
ents = sorted(r["entry_ts"] for r in reach_exact)
if ents:
    print("entry_ts range:",
          dt.datetime.utcfromtimestamp(ents[0]).date(), "..",
          dt.datetime.utcfromtimestamp(ents[-1]).date())
