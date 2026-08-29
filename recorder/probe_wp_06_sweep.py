import os, sqlite3, config, features
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== A) model population entry_ts range ===")
r = q(f"""SELECT COUNT(*) c, MIN(datetime(entry_ts,'unixepoch')) lo,
                 MAX(datetime(entry_ts,'unixepoch')) hi, MIN(built_at) b_lo, MAX(built_at) b_hi
            FROM training_rows WHERE {POP}""")[0]
print(f"   rows={r['c']}  entry_ts {r['lo']} .. {r['hi']}   built_at {r['b_lo'][:19]} .. {r['b_hi'][:19]}")

print()
print("=== B) every FEATURE_COLUMN: non-null count + distinct non-null values (model population) ===")
cols = list(features.FEATURE_COLUMNS)
parts = []
for c in cols:
    parts.append(f'SUM(CASE WHEN "{c}" IS NOT NULL THEN 1 ELSE 0 END) "nn_{c}"')
    parts.append(f'COUNT(DISTINCT "{c}") "dv_{c}"')
tot = q(f"SELECT COUNT(*) c FROM training_rows WHERE {POP}")[0]["c"]
row = q(f"SELECT {', '.join(parts)} FROM training_rows WHERE {POP}")[0]

dead, const, sparse = [], [], []
for c in cols:
    nn, dv = row[f"nn_{c}"], row[f"dv_{c}"]
    if nn == 0:
        dead.append(c)
    elif dv <= 1:
        const.append((c, nn, dv))
    elif nn / tot < 0.10:
        sparse.append((c, nn, 100.0*nn/tot, dv))

print(f"   total model rows = {tot}")
print(f"   -- {len(dead)} columns 100% NULL:")
for c in dead: print("        ", c)
print(f"   -- {len(const)} columns with a SINGLE distinct value (zero variance):")
for c, nn, dv in const:
    v = q(f'SELECT DISTINCT "{c}" v FROM training_rows WHERE {POP} AND "{c}" IS NOT NULL')[0]["v"]
    print(f"         {c:34s} non-null {nn}/{tot}  value={v!r}")
print(f"   -- {len(sparse)} columns non-null in <10% of rows:")
for c, nn, pct, dv in sorted(sparse, key=lambda x: x[1]):
    print(f"         {c:34s} non-null {nn:>6}/{tot} ({pct:4.1f}%) distinct={dv}")

print()
print("=== C) token_static raw payload: does the 'info'/'socialLinks' block actually exist? ===")
rows = q("""SELECT token_address, description, cmc_id, twitter, telegram, website, discord,
                   description_len, raw_json
              FROM token_static WHERE description IS NULL LIMIT 400""")
stat = {"info_missing":0, "info_present_no_desc":0, "social_missing":0,
        "social_present_empty":0, "no_raw":0, "n":0}
for r in rows:
    stat["n"] += 1
    try:
        raw = dbmod.decode_raw(r["raw_json"])
    except Exception:
        stat["no_raw"] += 1; continue
    if not isinstance(raw, dict):
        stat["no_raw"] += 1; continue
    tok = raw.get("token") if isinstance(raw.get("token"), dict) else {}
    if "info" not in tok or tok.get("info") is None:
        stat["info_missing"] += 1
    else:
        stat["info_present_no_desc"] += 1
    if "socialLinks" not in tok or tok.get("socialLinks") is None:
        stat["social_missing"] += 1
    else:
        stat["social_present_empty"] += 1
print("   sampled token_static rows with description IS NULL:", stat["n"])
print("   token.info key ABSENT/null  :", stat["info_missing"],
      "   present (so 'no description' is a measured fact):", stat["info_present_no_desc"])
print("   token.socialLinks ABSENT/null:", stat["social_missing"],
      "   present:", stat["social_present_empty"])
print("   undecodable raw:", stat["no_raw"])

con.close()
