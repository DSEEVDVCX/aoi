import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
SOC = ("twitter", "telegram", "website", "discord")

# Per-source shape of token.socialLinks across a broad market_ticks sample.
agg = {}
rows = con.execute("""SELECT source, raw_json FROM market_ticks
                       WHERE rowid % 97 = 0""").fetchall()
print("sampled ticks:", len(rows))
for r in rows:
    try:
        item = dbmod.decode_raw(r["raw_json"])
    except Exception:
        continue
    tok = item.get("token") if isinstance(item.get("token"), dict) else {}
    d = agg.setdefault(r["source"], {"n": 0, "key_absent": 0, "key_null": 0,
                                     "dict_all_null": 0, "dict_with_val": 0})
    d["n"] += 1
    if "socialLinks" not in tok:
        d["key_absent"] += 1
    else:
        sl = tok.get("socialLinks")
        if sl is None:
            d["key_null"] += 1
        elif isinstance(sl, dict) and any(sl.get(k) for k in SOC):
            d["dict_with_val"] += 1
        else:
            d["dict_all_null"] += 1

print("\ntoken.socialLinks shape by market_ticks source:")
for s, d in sorted(agg.items()):
    print(f"  {s:10s} n={d['n']:7d} key_absent={d['key_absent']:6d} "
          f"key_null={d['key_null']:6d} all_null_dict={d['dict_all_null']:6d} "
          f"with_value={d['dict_with_val']:6d}")

# same for the frozen token_static envelopes (all 1213)
st = {"n": 0, "key_absent": 0, "key_null": 0, "dict_all_null": 0, "dict_with_val": 0}
for r in con.execute("SELECT raw_json FROM token_static"):
    try:
        item = dbmod.decode_raw(r["raw_json"])
    except Exception:
        continue
    tok = item.get("token") if isinstance(item.get("token"), dict) else {}
    st["n"] += 1
    if "socialLinks" not in tok:
        st["key_absent"] += 1
    else:
        sl = tok.get("socialLinks")
        if sl is None:
            st["key_null"] += 1
        elif isinstance(sl, dict) and any(sl.get(k) for k in SOC):
            st["dict_with_val"] += 1
        else:
            st["dict_all_null"] += 1
print("\ntoken_static frozen envelopes:", st)
con.close()
