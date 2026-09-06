import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = (f"FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' "
       f"AND status='ok' AND is_independent=1 AND feature_version = {FV}")

# rows per token in the model population
pop = con.execute(f"""SELECT token_address, network_id, COUNT(*) n,
                             SUM(CASE WHEN socials_count=0 THEN 1 ELSE 0 END) sc0
                        {POP} GROUP BY token_address, network_id""").fetchall()
print("distinct model tokens:", len(pop), " model rows:", sum(r["n"] for r in pop))

# one static row per token?
d = con.execute("""SELECT COUNT(*) rows, COUNT(DISTINCT token_address||'|'||network_id) keys
                     FROM token_static""").fetchone()
print("token_static rows / distinct keys:", dict(d))

missing_sl = set(); allnull_sl = set(); has_sl = set(); nostatic = set()
for r in pop:
    st = con.execute("""SELECT raw_json, twitter, telegram, website, discord
                          FROM token_static WHERE token_address=? AND network_id=?""",
                     (r["token_address"], r["network_id"])).fetchone()
    key = (r["token_address"], r["network_id"])
    if st is None:
        nostatic.add(key); continue
    try:
        obj = dbmod.decode_raw(st["raw_json"])
    except Exception:
        continue
    tok = obj.get("token") if isinstance(obj, dict) else {}
    tok = tok if isinstance(tok, dict) else {}
    sl = tok.get("socialLinks", "__MISSING__")
    if sl == "__MISSING__" or sl is None:
        missing_sl.add(key)
    elif isinstance(sl, dict) and not any(
            sl.get(k) for k in ("twitter", "telegram", "website", "discord")):
        allnull_sl.add(key)
    else:
        has_sl.add(key)

def rows_for(s):
    return sum(r["n"] for r in pop if (r["token_address"], r["network_id"]) in s)

print("\n=== socials_count=0 provenance, exact, over the model population ===")
print(f"  socialLinks block ABSENT from envelope : {len(missing_sl):4d} tokens -> {rows_for(missing_sl):6d} rows"
      "   (socials_count=0 is FABRICATED)")
print(f"  socialLinks present, all links null    : {len(allnull_sl):4d} tokens -> {rows_for(allnull_sl):6d} rows"
      "   (socials_count=0 is MEASURED)")
print(f"  socialLinks present with >=1 link      : {len(has_sl):4d} tokens -> {rows_for(has_sl):6d} rows")
print(f"  no token_static row at all             : {len(nostatic):4d} tokens -> {rows_for(nostatic):6d} rows"
      "   (socials_count NULL, correct)")

# --- was the lost socialLinks recoverable from another endpoint's envelope? ---
print("\n=== recoverability: does tokenDetails carry socialLinks for those tokens? ===")
checked = rec = 0
for key in list(missing_sl)[:40]:
    row = con.execute("""SELECT raw_json FROM token_holders
                          WHERE token_address=? AND network_id=? AND source='token_details'
                          ORDER BY recorded_at DESC LIMIT 1""", key).fetchone()
    if row is None:
        continue
    checked += 1
    try:
        obj = dbmod.decode_raw(row["raw_json"])
    except Exception:
        continue
    txt = str(obj)
    if "socialLinks" in txt or "twitter" in txt.lower():
        rec += 1
print(f"  of {checked} tokens with a tokenDetails envelope, "
      f"{rec} mention socialLinks/twitter in it")

# --- thesis coverage, exact ---
print("\n=== thesis collector coverage over model tokens ===")
covered = 0
for r in pop:
    c = con.execute("""SELECT 1 FROM token_thesis WHERE token_address=? AND network_id=? LIMIT 1""",
                    (r["token_address"], r["network_id"])).fetchone()
    if c: covered += 1
print(f"  model tokens with >=1 token_thesis row: {covered} of {len(pop)}"
      f" ({100.0*covered/len(pop):.1f}%)")
con.close()
