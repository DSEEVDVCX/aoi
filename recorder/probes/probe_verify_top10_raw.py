import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

# Does the upstream list payload EVER carry the key? Sample raw_json per source.
for src in ("trending", "verified", "filter", "most_held"):
    rows = con.execute(
        """SELECT raw_json FROM market_ticks WHERE source=?
             ORDER BY recorded_at DESC LIMIT 60""", (src,)).fetchall()
    hits = 0
    keyseen = 0
    holderkey = 0
    for r in rows:
        try:
            obj = dbmod.decode_raw(r["raw_json"])
        except Exception as e:
            print(src, "decode fail", e)
            break
        s = str(obj)
        if "top10HoldersPercent" in s:
            keyseen += 1
            # is it present with a value?
            if isinstance(obj, dict) and obj.get("top10HoldersPercent") is not None:
                hits += 1
        if "holders" in s:
            holderkey += 1
    print(f"{src:10s} sampled={len(rows):3d} key_present={keyseen:3d} "
          f"key_with_value={hits:3d} 'holders'_substr={holderkey:3d}")

# Show the actual top-level key set of one payload per source, for proof.
print("\n-- top-level keys of one 'trending' payload --")
r = con.execute("""SELECT raw_json FROM market_ticks WHERE source='trending'
                    ORDER BY recorded_at DESC LIMIT 1""").fetchone()
obj = dbmod.decode_raw(r["raw_json"])
if isinstance(obj, dict):
    ks = sorted(obj.keys())
    print(len(ks), "keys:", ks)
    print("has top10HoldersPercent?", "top10HoldersPercent" in obj)
    print("holders value:", obj.get("holders"))
con.close()
