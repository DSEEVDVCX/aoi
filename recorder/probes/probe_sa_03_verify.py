import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

# --- 1. token_created_at format distribution (recorder.token_age_days input) --
print("=== 1. token_static.token_created_at formats ===")
r = con.execute("""
  SELECT COUNT(*) total,
    SUM(CASE WHEN token_created_at IS NULL OR token_created_at='' THEN 1 ELSE 0 END) missing,
    SUM(CASE WHEN token_created_at GLOB '[0-9]*' AND token_created_at NOT GLOB '*[^0-9]*'
             THEN 1 ELSE 0 END) pure_digits,
    SUM(CASE WHEN token_created_at GLOB '*[^0-9]*' THEN 1 ELSE 0 END) has_nondigit,
    SUM(CASE WHEN token_created_at GLOB '*T*' THEN 1 ELSE 0 END) looks_iso,
    SUM(CASE WHEN token_created_at NOT GLOB '*[^0-9]*' AND token_created_at<>''
              AND CAST(token_created_at AS INTEGER) > 100000000000 THEN 1 ELSE 0 END) ms_magnitude
  FROM token_static""").fetchone()
print(dict(r))
print("  samples:", [x[0] for x in con.execute(
    "SELECT DISTINCT token_created_at FROM token_static WHERE token_created_at IS NOT NULL LIMIT 6")])
print("  non-digit samples:", [x[0] for x in con.execute(
    "SELECT DISTINCT token_created_at FROM token_static WHERE token_created_at GLOB '*[^0-9]*' LIMIT 6")])

# --- 2. do thesis_counted=0 tokens have ANY token_thesis rows at all? ---------
print("\n=== 2. thesis_counted=0: is it 'never collected' or 'all after t0'? ===")
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = (f"FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' "
       f"AND status='ok' AND is_independent=1 AND feature_version = {FV}")
r = con.execute(f"""
  WITH pop AS (SELECT token_address, network_id, thesis_counted, social_thesis_total {POP})
  SELECT COUNT(*) n,
     SUM(CASE WHEN thesis_counted=0 THEN 1 ELSE 0 END) tc0,
     SUM(CASE WHEN thesis_counted=0 AND NOT EXISTS (
            SELECT 1 FROM token_thesis t WHERE t.token_address=pop.token_address
              AND t.network_id=pop.network_id) THEN 1 ELSE 0 END) tc0_no_rows_at_all,
     SUM(CASE WHEN thesis_counted=0 AND EXISTS (
            SELECT 1 FROM token_thesis t WHERE t.token_address=pop.token_address
              AND t.network_id=pop.network_id) THEN 1 ELSE 0 END) tc0_rows_exist_after_t0
  FROM pop""").fetchone()
print(dict(r))
print("  token_thesis total rows:", con.execute("SELECT COUNT(*) c FROM token_thesis").fetchone()["c"])
print("  distinct tokens in token_thesis:", con.execute(
    "SELECT COUNT(*) c FROM (SELECT DISTINCT token_address, network_id FROM token_thesis)").fetchone()["c"])
print("  distinct tokens in model pop  :", con.execute(
    f"SELECT COUNT(*) c FROM (SELECT DISTINCT token_address, network_id {POP})").fetchone()["c"])

# --- 3. top_trader_ids_json: is it literally "[]" ? ---------------------------
print("\n=== 3. signal_events.top_trader_ids_json shape ===")
r = con.execute("""SELECT COUNT(*) total,
     SUM(CASE WHEN top_trader_ids_json IS NULL THEN 1 ELSE 0 END) isnull,
     SUM(CASE WHEN top_trader_ids_json='[]' THEN 1 ELSE 0 END) empty_list,
     SUM(CASE WHEN top_trader_ids_json NOT IN ('[]') AND top_trader_ids_json IS NOT NULL
              THEN 1 ELSE 0 END) nonempty
   FROM signal_events""").fetchone()
print(dict(r))

# --- 4. token_static raw_json: does the `info` / `socialLinks` block exist? ---
print("\n=== 4. is description_len=0 / has_banner=0 an ABSENT info block? ===")
rows = con.execute("""SELECT token_address, network_id, description, description_len,
                             has_banner, has_image, cmc_id, twitter, telegram,
                             website, discord, raw_json
                        FROM token_static
                       WHERE description IS NULL AND has_banner=0 AND has_image=0
                         AND cmc_id IS NULL
                       LIMIT 250""").fetchall()
no_info = no_social = has_info_empty = 0
for r in rows:
    try:
        obj = dbmod.decode_raw(r["raw_json"])
    except Exception:
        continue
    tok = obj.get("token") if isinstance(obj, dict) else None
    tok = tok if isinstance(tok, dict) else {}
    if "info" not in tok or tok.get("info") is None:
        no_info += 1
    elif not tok.get("info"):
        has_info_empty += 1
    sl = tok.get("socialLinks")
    if "socialLinks" not in tok or sl is None:
        no_social += 1
print(f"  sampled {len(rows)} token_static rows that look 'all absent'")
print(f"  token.info key MISSING or null : {no_info}")
print(f"  token.info present but empty {{}}: {has_info_empty}")
print(f"  token.socialLinks MISSING/null : {no_social}")

# 4b. rows WITH socials, to show socialLinks does exist when populated
rows2 = con.execute("""SELECT raw_json FROM token_static
                        WHERE twitter IS NOT NULL LIMIT 60""").fetchall()
sl_present = 0
for r in rows2:
    try:
        obj = dbmod.decode_raw(r["raw_json"])
    except Exception:
        continue
    tok = obj.get("token") if isinstance(obj, dict) else {}
    if isinstance(tok, dict) and isinstance(tok.get("socialLinks"), dict):
        sl_present += 1
print(f"  control: of {len(rows2)} rows WITH a twitter handle, socialLinks dict present in {sl_present}")

# --- 5. socials_count=0 in the model pop vs the static row's actual columns ---
print("\n=== 5. token_static: all four social cols NULL (=> socials_count fabricated 0) ===")
r = con.execute("""SELECT COUNT(*) total,
    SUM(CASE WHEN twitter IS NULL AND telegram IS NULL AND website IS NULL
              AND discord IS NULL THEN 1 ELSE 0 END) all4_null,
    SUM(CASE WHEN twitter IS NULL AND telegram IS NULL AND website IS NULL
              AND discord IS NULL AND description IS NULL AND cmc_id IS NULL
              AND has_banner=0 AND has_image=0 THEN 1 ELSE 0 END) whole_block_absent
  FROM token_static""").fetchone()
print(dict(r))
con.close()
