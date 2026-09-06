import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== 1) the 659: is the snapshot they missed a BACKDATED replay row? ===")
snaps = {}
for r in q("""SELECT network_id n, token_address a,
                     CAST(strftime('%s', recorded_at) AS INTEGER) e,
                     top1_pct, COALESCE(is_replay,0) rep
                FROM chain_concentration ORDER BY n, a, e"""):
    snaps.setdefault((r["n"], r["a"]), []).append(r)

def latest_at(key, t):
    lst = snaps.get(key)
    if not lst: return None
    lo, hi, best = 0, len(lst)-1, None
    while lo <= hi:
        mid = (lo+hi)//2
        if lst[mid]["e"] <= t: best = lst[mid]; lo = mid+1
        else: hi = mid-1
    return best

rows = q(f"""SELECT token_address a, network_id n, entry_ts t, onchain_top1_pct p, built_at
               FROM training_rows WHERE {POP}""")
by_rep = {0:0, 1:0}
built = {}
for r in rows:
    if r["p"] is not None: continue
    cur = latest_at((r["n"], r["a"]), r["t"])
    if cur is None or cur["top1_pct"] is None: continue
    by_rep[cur["rep"]] += 1
    built[(r["built_at"] or "")[:10]] = built.get((r["built_at"] or "")[:10], 0) + 1
print("   defect rows whose missed snapshot is is_replay=1 (backdated):", by_rep[1])
print("   defect rows whose missed snapshot is is_replay=0 (live)     :", by_rep[0])
print("   built_at day histogram of the defect rows:", dict(sorted(built.items())))

print()
print("=== 2) onchain family coverage in the model population ===")
cols = ["onchain_top1_pct","onchain_top5_pct","onchain_top10_pct","onchain_top20_pct",
        "onchain_top_accounts","onchain_holder_count","onchain_top1_delta_5m",
        "onchain_has_mint_authority","onchain_dev_holding_pct","onchain_code_size",
        "onchain_owner_renounced","onchain_has_mint_fn"]
sel = ", ".join(f"SUM(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END) \"{c}\"" for c in cols)
r = q(f"SELECT COUNT(*) tot, {sel} FROM training_rows WHERE {POP}")[0]
tot = r["tot"]
for c in cols:
    print(f"   {c:32s} non-null {r[c]:>6} / {tot}  ({100.0*r[c]/tot:5.1f}%)")

print()
print("=== 3) token_holders / hodlers_top: platform_holders_listed NULL vs platform_holders ===")
r = q("""SELECT COUNT(*) tot,
                SUM(CASE WHEN platform_holders_listed IS NULL THEN 1 ELSE 0 END) listed_null,
                SUM(CASE WHEN platform_holders_listed IS NULL AND platform_holders IS NOT NULL
                         THEN 1 ELSE 0 END) listed_null_but_total,
                SUM(CASE WHEN platform_holders=0 THEN 1 ELSE 0 END) total_zero,
                SUM(CASE WHEN platform_holders_listed=0 THEN 1 ELSE 0 END) listed_zero
           FROM token_holders WHERE source='hodlers_top'""")[0]
print("   hodlers_top rows:", r["tot"])
print("   platform_holders_listed IS NULL:", r["listed_null"])
print("   ... of those, platform_holders IS NOT NULL (row proves measurement happened):",
      r["listed_null_but_total"])
print("   platform_holders = 0:", r["total_zero"], "  platform_holders_listed = 0:", r["listed_zero"])

print()
print("=== 4) token_social: thesis_replies / thesis_likes fabricated-sum check ===")
r = q("""SELECT COUNT(*) tot,
                SUM(CASE WHEN thesis_replies=0 THEN 1 ELSE 0 END) rep0,
                SUM(CASE WHEN thesis_replies IS NULL THEN 1 ELSE 0 END) repN,
                MAX(thesis_replies) repmax,
                SUM(CASE WHEN thesis_likes=0 THEN 1 ELSE 0 END) lik0,
                MAX(thesis_likes) likmax,
                SUM(CASE WHEN thesis_sampled>0 THEN 1 ELSE 0 END) sampled_gt0
           FROM token_social""")[0]
print(f"   token_social rows={r['tot']} thesis_replies=0 -> {r['rep0']} NULL -> {r['repN']} max={r['repmax']}")
print(f"   thesis_likes=0 -> {r['lik0']} max={r['likmax']}   rows with thesis_sampled>0 -> {r['sampled_gt0']}")
r2 = q(f"""SELECT COUNT(*) tot,
                  SUM(CASE WHEN social_replies=0 THEN 1 ELSE 0 END) z,
                  SUM(CASE WHEN social_replies IS NULL THEN 1 ELSE 0 END) n,
                  MAX(social_replies) mx
             FROM training_rows WHERE {POP}""")[0]
print(f"   feature social_replies: =0 -> {r2['z']}  NULL -> {r2['n']}  max={r2['mx']}  of {r2['tot']}")

print()
print("=== 5) thesis_counted=0 while the snapshot says theses exist (fabricated zero) ===")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN thesis_counted=0 THEN 1 ELSE 0 END) tc0,
                 SUM(CASE WHEN thesis_counted IS NULL THEN 1 ELSE 0 END) tcN,
                 SUM(CASE WHEN thesis_counted=0 AND social_thesis_total>0 THEN 1 ELSE 0 END) contra,
                 SUM(CASE WHEN thesis_counted=0 AND social_thesis_total IS NULL THEN 1 ELSE 0 END) tc0_nosnap
            FROM training_rows WHERE {POP}""")[0]
print(f"   model rows={r['tot']} thesis_counted=0 -> {r['tc0']} NULL -> {r['tcN']}")
print(f"   thesis_counted=0 AND social_thesis_total>0 (PROOF the 0 is fabricated): {r['contra']}")
print(f"   thesis_counted=0 AND no social snapshot at all: {r['tc0_nosnap']}")
r = q(f"""SELECT COUNT(*) c, MIN(social_thesis_total) mn, MAX(social_thesis_total) mx,
                 AVG(social_thesis_total) av
            FROM training_rows WHERE {POP} AND thesis_counted=0 AND social_thesis_total>0""")[0]
print(f"   in those contradicting rows social_thesis_total: n={r['c']} min={r['mn']} max={r['mx']} avg={r['av']}")

con.close()
