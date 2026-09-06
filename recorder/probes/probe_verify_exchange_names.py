import os, sqlite3, json, collections, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("== exchange NAMES actually stored (exchanges_json is plain TEXT, not a BLOB)")
cnt = collections.Counter()
tok_with = 0
for r in con.execute("SELECT exchanges_json FROM token_static WHERE exchanges_json IS NOT NULL"):
    try:
        names = json.loads(r["exchanges_json"])
    except Exception as e:
        print("  parse fail:", e); continue
    if names:
        tok_with += 1
    for n in names:
        cnt[n] += 1
print(f"  tokens with >=1 exchange name: {tok_with}")
print("  top 25 names by token count:")
for n, c in cnt.most_common(25):
    print(f"    {c:5d}  {n}")
print(f"  distinct names total: {len(cnt)}")

print("\n== sibling has_cmc_id: the OTHER external-legitimacy flag, model population")
FV = ("(SELECT CAST(COALESCE((SELECT value FROM meta "
      "WHERE key='current_feature_version'),'0') AS INTEGER))")
POP = (f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version = {FV}")
for r in con.execute(f"""SELECT COALESCE(CAST(has_cmc_id AS TEXT),'NULL') v, COUNT(*) rows
                           FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY rows DESC"""):
    print(f"   has_cmc_id={r['v']:>4}  rows={r['rows']}")

print("\n== does exchanges_count=1 always mean the SAME single venue?")
for r in con.execute("""SELECT exchanges_json, COUNT(*) rows FROM token_static
                         WHERE exchanges_count=1 GROUP BY 1 ORDER BY rows DESC LIMIT 10"""):
    print(f"   {r['rows']:5d}  {r['exchanges_json']}")
con.close()
