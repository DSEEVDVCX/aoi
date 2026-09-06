import os, sqlite3, json, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== J) raw payload: is currentSizeUsd PRESENT and 0, or absent? ===")
rows = con.execute("""SELECT id, size_usd, raw_json FROM signal_events
                       WHERE signal_type='large_sell' AND size_usd = 0.0
                       AND raw_json IS NOT NULL LIMIT 12""").fetchall()
present = absent = 0
seen = []
for r in rows:
    try:
        d = dbmod.decode_raw(r["raw_json"])
    except Exception as e:
        print("  decode fail:", e); continue
    if isinstance(d, (bytes, str)):
        d = json.loads(d)
    # find the key anywhere shallow
    def find(o, depth=0):
        out = {}
        if isinstance(o, dict):
            for k, v in o.items():
                if "size" in k.lower() or "SizeUsd" in k:
                    out[k] = v
                if isinstance(v, (dict, list)) and depth < 3:
                    out.update(find(v, depth+1))
        elif isinstance(o, list):
            for v in o[:3]:
                out.update(find(v, depth+1))
        return out
    hits = find(d)
    seen.append(hits)
    if hits:
        present += 1
    else:
        absent += 1
print("  sampled", len(rows), "-> key present:", present, " key absent:", absent)
for h in seen[:8]:
    print("   ", h)

print("\n=== K) contrast: same probe on a large_sell with size_usd > 0 ===")
rows = con.execute("""SELECT id, size_usd, raw_json FROM signal_events
                       WHERE signal_type='large_sell' AND size_usd > 100
                       AND raw_json IS NOT NULL LIMIT 4""").fetchall()
for r in rows:
    d = dbmod.decode_raw(r["raw_json"])
    if isinstance(d, (bytes, str)):
        d = json.loads(d)
    def find(o, depth=0):
        out = {}
        if isinstance(o, dict):
            for k, v in o.items():
                if "size" in k.lower():
                    out[k] = v
                if isinstance(v, (dict, list)) and depth < 3:
                    out.update(find(v, depth+1))
        elif isinstance(o, list):
            for v in o[:3]:
                out.update(find(v, depth+1))
        return out
    print(f"   db size_usd={r['size_usd']:.2f} raw={find(d)}")

print("\n=== L) their exact SQL, verbatim ===")
q = """SELECT SUM(CASE WHEN log_size_usd IS NULL THEN 1 ELSE 0 END) log_null,
       SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) sz_null,
       SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) sz_zero,
       SUM(CASE WHEN size_usd<0 THEN 1 ELSE 0 END) sz_neg,
       SUM(CASE WHEN size_usd>0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) pos_lognull,
       SUM(CASE WHEN size_usd=0 AND size_to_mcap=0 THEN 1 ELSE 0 END) s2m_zero_kept
  FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
   AND status='ok' AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
print("  ", dict(con.execute(q).fetchone()))
con.close()
