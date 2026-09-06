import os, sys, io, sqlite3, config
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from features import epoch_of

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = 12
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")

print("=== A the 1265: recompute the age NOW from the stored created_at ===")
rows = con.execute(f"""
SELECT t.token_address ta, t.network_id ni, t.entry_ts, t.built_at, t.token_age_h,
       s.token_created_at tca, s.symbol sym
  FROM training_rows t
  JOIN token_static s ON s.token_address=t.token_address AND s.network_id=t.network_id
 WHERE {POP} AND t.token_age_h IS NULL AND s.token_created_at IS NOT NULL""").fetchall()
print("  rows fetched:", len(rows))
neg = pos = zero = 0
coins_neg, coins_pos = {}, {}
worst = []
for r in rows:
    c = epoch_of(r["tca"])
    age = (r["entry_ts"] - c) / 3600.0
    if age < 0:
        neg += 1
        coins_neg[(r["sym"], r["ni"])] = coins_neg.get((r["sym"], r["ni"]), 0) + 1
        worst.append(age)
    elif age == 0:
        zero += 1
    else:
        pos += 1
        coins_pos[(r["sym"], r["ni"])] = coins_pos.get((r["sym"], r["ni"]), 0) + 1
print(f"  would be NEGATIVE now (genuine suppression): {neg}")
print(f"  would be ZERO now: {zero}")
print(f"  would be POSITIVE now (i.e. the row is STALE, built before created_at was filled): {pos}")
if worst:
    print(f"  most negative age_h: {round(min(worst),2)}")
print(f"  distinct coins in the negative set: {len(coins_neg)}  in the positive set: {len(coins_pos)}")
print("  top coins in the POSITIVE (stale) set:")
for k, v in sorted(coins_pos.items(), key=lambda x: -x[1])[:12]:
    print(f"    {k[0]}/net{k[1]}: {v}")
print("  top coins in the NEGATIVE set:")
for k, v in sorted(coins_neg.items(), key=lambda x: -x[1])[:12]:
    print(f"    {k[0]}/net{k[1]}: {v}")
print()

print("=== B built_at of the NULL-age rows vs the NOT-NULL-age rows ===")
for r in con.execute(f"""
SELECT CASE WHEN token_age_h IS NULL THEN 'age NULL' ELSE 'age present' END grp,
       COUNT(*) n, MIN(built_at) first_built, MAX(built_at) last_built
  FROM training_rows WHERE {POP} GROUP BY 1"""):
    print(f"    {r['grp']}: n={r['n']} built_at {r['first_built']} .. {r['last_built']}")
print()

print("=== C NULL-age share by built_at day (is it an epoch artefact?) ===")
for r in con.execute(f"""
SELECT substr(built_at,1,10) d, COUNT(*) n,
       SUM(token_age_h IS NULL) age_null,
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),1) pct
  FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1"""):
    print(f"    {r['d']}  n={r['n']:6d}  age_null={r['age_null']:5d}  ({r['pct']}%)")
print()

print("=== D NULL-age share by network (one-network artefact?) ===")
for r in con.execute(f"""
SELECT network_id, COUNT(*) n, SUM(token_age_h IS NULL) age_null,
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),1) pct
  FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY n DESC"""):
    print(f"    net={r['network_id']:>12}  n={r['n']:6d}  age_null={r['age_null']:5d}  ({r['pct']}%)")
print()

print("=== E meta keys mentioning repair / static / age ===")
for r in con.execute("""SELECT key, substr(value,1,90) v FROM meta
   WHERE key LIKE '%repair%' OR key LIKE '%static%' OR key LIKE '%age%'
   ORDER BY key"""):
    print(f"    {r['key']} = {r['v']}")
con.close()
