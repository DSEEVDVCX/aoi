import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== 0) mixed-case token_address by network (is the case spread just Solana base58?) ===")
for r in q("""SELECT network_id, COUNT(*) tot,
                     SUM(CASE WHEN token_address<>lower(token_address) THEN 1 ELSE 0 END) mixed
                FROM training_rows GROUP BY network_id ORDER BY tot DESC"""):
    print(f"   net={r['network_id']:<12} rows={r['tot']:<7} mixed_case={r['mixed']}")

print()
print("=== 1) load all chain_concentration snapshots per token, ordered ===")
snaps = {}
for r in q("""SELECT network_id n, token_address a,
                     CAST(strftime('%s', recorded_at) AS INTEGER) e,
                     top1_pct, top_accounts, holder_count, supply,
                     COALESCE(is_replay,0) rep
                FROM chain_concentration ORDER BY n, a, e"""):
    snaps.setdefault((r["n"], r["a"]), []).append(r)
print("   token keys:", len(snaps))

import bisect
def latest_at(key, t):
    lst = snaps.get(key)
    if not lst: return None
    lo, hi, best = 0, len(lst)-1, None
    while lo <= hi:
        mid = (lo+hi)//2
        if lst[mid]["e"] <= t:
            best = lst[mid]; lo = mid+1
        else:
            hi = mid-1
    return best

print()
print("=== 2) model rows where onchain_top1_pct IS NULL but an EARLIER snapshot had a value ===")
rows = q(f"""SELECT token_address a, network_id n, entry_ts t, onchain_top1_pct p,
                    onchain_top_accounts ta, onchain_age_min am
               FROM training_rows WHERE {POP}""")
stale_null = latest_null_older_ok = 0
latest_has_val_but_row_null = 0
ex = []
for r in rows:
    if r["p"] is not None: continue
    key = (r["n"], r["a"])
    lst = snaps.get(key)
    if not lst: continue
    cur = latest_at(key, r["t"])
    if cur is None: continue
    if cur["top1_pct"] is not None:
        latest_has_val_but_row_null += 1
        continue
    # latest row has NULL top1_pct; did any earlier row <= t0 have a value?
    older = [x for x in lst if x["e"] <= r["t"] and x["top1_pct"] is not None]
    if older:
        stale_null += 1
        if len(ex) < 5:
            ex.append((r["n"], r["a"], r["t"], cur["e"], cur["top_accounts"],
                       cur["holder_count"], cur["supply"], cur["rep"],
                       older[-1]["top1_pct"], r["t"]-older[-1]["e"]))
print("   latest snapshot <=t0 HAS top1_pct but training row is NULL (real defect):",
      latest_has_val_but_row_null)
print("   latest snapshot <=t0 has NULL top1_pct while an EARLIER one <=t0 had a value:",
      stale_null)
for e in ex:
    print(f"     net={e[0]} {e[1][:16]}.. t0={e[2]} latest_e={e[3]} top_accounts={e[4]}"
          f" holder_count={e[5]} supply={e[6]} is_replay={e[7]}"
          f" -> earlier top1_pct={e[8]:.3f} ({e[9]}s older)")

print()
print("=== 3) chain_concentration rows with NULL tier percentages ===")
for r in q("""SELECT COALESCE(is_replay,0) rep,
                     COUNT(*) tot,
                     SUM(CASE WHEN top1_pct IS NULL THEN 1 ELSE 0 END) null_top1,
                     SUM(CASE WHEN top_accounts=0 THEN 1 ELSE 0 END) zero_accounts,
                     SUM(CASE WHEN supply=0 THEN 1 ELSE 0 END) zero_supply,
                     SUM(CASE WHEN holder_count=0 THEN 1 ELSE 0 END) zero_holders
                FROM chain_concentration GROUP BY 1"""):
    print(f"   is_replay={r['rep']} tot={r['tot']} null_top1={r['null_top1']}"
          f" top_accounts=0:{r['zero_accounts']} supply=0:{r['zero_supply']}"
          f" holder_count=0:{r['zero_holders']}")

print()
print("=== 4) onchain_top_accounts=0 in the model population (fabricated 'measured 0 accounts'?) ===")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN onchain_top_accounts=0 THEN 1 ELSE 0 END) z,
                 SUM(CASE WHEN onchain_top_accounts IS NULL THEN 1 ELSE 0 END) n
            FROM training_rows WHERE {POP}""")[0]
print(f"   model rows={r['tot']} onchain_top_accounts=0 -> {r['z']}  NULL -> {r['n']}")

con.close()
