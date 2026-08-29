import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def scan(where, label, limit=400):
    rows = con.execute(f"""SELECT raw_json FROM token_static WHERE {where} LIMIT {limit}""").fetchall()
    n = len(rows)
    info_missing = info_empty = info_dict = 0
    sl_missing = sl_empty = sl_dict_allnull = sl_dict_some = 0
    desc_key_present = banner_key_present = cmc_key_present = 0
    for r in rows:
        try:
            obj = dbmod.decode_raw(r["raw_json"])
        except Exception:
            continue
        tok = obj.get("token") if isinstance(obj, dict) else None
        tok = tok if isinstance(tok, dict) else {}
        info = tok.get("info", "__MISSING__")
        if info == "__MISSING__" or info is None:
            info_missing += 1
        elif isinstance(info, dict) and not info:
            info_empty += 1
        elif isinstance(info, dict):
            info_dict += 1
            if "description" in info: desc_key_present += 1
            if "imageBannerUrl" in info: banner_key_present += 1
            if "cmcId" in info: cmc_key_present += 1
        sl = tok.get("socialLinks", "__MISSING__")
        if sl == "__MISSING__" or sl is None:
            sl_missing += 1
        elif isinstance(sl, dict) and not sl:
            sl_empty += 1
        elif isinstance(sl, dict):
            if any(sl.get(k) for k in ("twitter", "telegram", "website", "discord")):
                sl_dict_some += 1
            else:
                sl_dict_allnull += 1
    print(f"\n--- {label}  (n={n}) ---")
    print(f"  token.info : missing/null={info_missing}  empty-dict={info_empty}  populated-dict={info_dict}")
    if info_dict:
        print(f"     of the populated dicts: 'description' key present={desc_key_present}"
              f"  'imageBannerUrl'={banner_key_present}  'cmcId'={cmc_key_present}")
    print(f"  token.socialLinks : missing/null={sl_missing}  empty-dict={sl_empty}"
          f"  dict-all-null={sl_dict_allnull}  dict-with-a-link={sl_dict_some}")

scan("twitter IS NULL AND telegram IS NULL AND website IS NULL AND discord IS NULL",
     "A) all four social cols NULL  => features.socials_count returns 0")
scan("description IS NULL AND has_banner=0",
     "B) description NULL and has_banner=0 => extract writes description_len=0")
scan("cmc_id IS NULL", "C) cmc_id NULL => features.has_cmc_id returns 0")
scan("twitter IS NOT NULL", "D) control: twitter present")

# has_image distribution for the 'looks absent' cohort
print("\n=== has_image where all-four-social-NULL ===")
r = con.execute("""SELECT COUNT(*) n,
   SUM(CASE WHEN has_image=1 THEN 1 ELSE 0 END) img1,
   SUM(CASE WHEN has_image=0 THEN 1 ELSE 0 END) img0,
   SUM(CASE WHEN description IS NOT NULL THEN 1 ELSE 0 END) has_desc
  FROM token_static
 WHERE twitter IS NULL AND telegram IS NULL AND website IS NULL AND discord IS NULL""").fetchone()
print(dict(r))
con.close()
