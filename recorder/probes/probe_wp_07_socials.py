import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== A) socialLinks key presence in EVERY token_static raw, vs the stored columns ===")
rows = q("""SELECT token_address, twitter, telegram, website, discord, raw_json
              FROM token_static""")
buck = {}
for r in rows:
    try:
        raw = dbmod.decode_raw(r["raw_json"])
    except Exception:
        raw = None
    tok = raw.get("token") if isinstance(raw, dict) and isinstance(raw.get("token"), dict) else {}
    sl = tok.get("socialLinks", "__ABSENT__")
    key_state = ("absent" if sl == "__ABSENT__" else
                 "null" if sl is None else
                 "empty_dict" if isinstance(sl, dict) and not sl else
                 "populated")
    any_link = any(r[c] for c in ("twitter", "telegram", "website", "discord"))
    buck[(key_state, any_link)] = buck.get((key_state, any_link), 0) + 1
print("   (socialLinks key state, any stored link) -> token_static row count")
for k in sorted(buck): print("    ", k, "->", buck[k])
tot_rows = len(rows)
absent = sum(v for (ks, _), v in buck.items() if ks in ("absent", "null"))
print(f"   socialLinks key absent/null in {absent} of {tot_rows} token_static rows"
      f" ({100.0*absent/tot_rows:.1f}%) -> socials_count/has_twitter are 0 there, not NULL")
pop_empty = sum(v for (ks, al), v in buck.items() if ks in ("empty_dict","populated") and not al)
print(f"   socialLinks present but yielding no stored link (a genuine measured 0): {pop_empty}")

print()
print("=== B) does the SAME token ever have both a socialLinks-bearing and a socialLinks-less row? ===")
r = q("""SELECT COUNT(*) c FROM (SELECT token_address, network_id, COUNT(*) n
             FROM token_static GROUP BY 1,2 HAVING n>1)""")[0]["c"]
print("   token_static (token,network) groups with >1 row:", r)

print()
print("=== C) exchanges_count distribution — why is listed_on_exchange a constant 1? ===")
for x in q("""SELECT exchanges_count, COUNT(*) c FROM token_static
               GROUP BY 1 ORDER BY c DESC LIMIT 12"""):
    print(f"      exchanges_count={x['exchanges_count']} -> {x['c']} token_static rows")
for x in q(f"""SELECT exchanges_count, COUNT(*) c FROM training_rows WHERE {POP}
                GROUP BY 1 ORDER BY c DESC LIMIT 12"""):
    print(f"      model rows exchanges_count={x['exchanges_count']} -> {x['c']}")

print()
print("=== D) signal_type composition of the model population + the multi_user_buy-only family ===")
for x in q(f"""SELECT signal_type, COUNT(*) c,
                      SUM(CASE WHEN total_volume IS NOT NULL THEN 1 ELSE 0 END) tv,
                      SUM(CASE WHEN unique_traders IS NOT NULL THEN 1 ELSE 0 END) ut
                 FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY c DESC"""):
    print(f"      {x['signal_type']:<16} rows={x['c']:<6} total_volume non-null={x['tv']} unique_traders non-null={x['ut']}")

print()
print("=== E) size_usd = 0 -> log_size_usd NULL (log1p(0)=0.0 is well defined) ===")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) z,
                 SUM(CASE WHEN size_usd=0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) zn,
                 SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) sn,
                 SUM(CASE WHEN size_usd=0 AND size_to_mcap=0 THEN 1 ELSE 0 END) stm0
            FROM training_rows WHERE {POP}""")[0]
print(f"   model rows={r['tot']} size_usd=0 -> {r['z']} ({100.0*r['z']/r['tot']:.1f}%)"
      f"  size_usd IS NULL -> {r['sn']}")
print(f"   size_usd=0 AND log_size_usd IS NULL -> {r['zn']}  (measured zero destroyed)")
print(f"   size_usd=0 AND size_to_mcap=0 -> {r['stm0']}  (the sibling derivation keeps the zero)")
for x in q(f"""SELECT signal_type, COUNT(*) c FROM training_rows WHERE {POP} AND size_usd=0
                GROUP BY 1 ORDER BY c DESC"""):
    print(f"      size_usd=0 by signal_type: {x['signal_type']} -> {x['c']}")

con.close()
