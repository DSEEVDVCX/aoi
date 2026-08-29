import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== A. my own form: GROUP BY category, not SUM(CASE) ===")
sql_a = """
SELECT CASE WHEN thesis_sampled IS NULL THEN 'sampled NULL'
            WHEN thesis_sampled = 0 THEN 'sampled=0'
            ELSE 'sampled>0' END  s_cat,
       CASE WHEN thesis_total IS NULL THEN 'total NULL'
            WHEN thesis_total = 0 THEN 'total=0'
            ELSE 'total>0' END    t_cat,
       COUNT(*)                   n,
       MIN(thesis_total)          t_min,
       MAX(thesis_total)          t_max,
       MIN(thesis_likes)          lk_min, MAX(thesis_likes)  lk_max,
       MIN(thesis_authors)        au_min, MAX(thesis_authors) au_max,
       MIN(thesis_replies)        rp_min, MAX(thesis_replies) rp_max,
       MIN(holder_authors)        ho_min, MAX(holder_authors) ho_max,
       SUM(thesis_likes IS NULL)  lk_null
  FROM token_social
 GROUP BY s_cat, t_cat
 ORDER BY s_cat, t_cat
"""
for r in con.execute(sql_a):
    print(dict(r))

print("\n=== B. total rows ===")
print(dict(con.execute("SELECT COUNT(*) n FROM token_social").fetchone()))

print("\n=== C. their exact SQL, for agreement check ===")
sql_c = """
SELECT COUNT(*) n,
  SUM(CASE WHEN thesis_sampled=0 THEN 1 ELSE 0 END) sampled0,
  SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 THEN 1 ELSE 0 END) sampled0_total_pos,
  SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 AND thesis_likes=0 THEN 1 ELSE 0 END) likes_fab,
  SUM(CASE WHEN thesis_sampled=0 AND thesis_total>0 AND thesis_authors=0 THEN 1 ELSE 0 END) auth_fab,
  MAX(CASE WHEN thesis_sampled=0 THEN thesis_total END) worst_total
  FROM token_social
"""
print(dict(con.execute(sql_c).fetchone()))

print("\n=== D. the sampled=0 & total>0 rows themselves ===")
rows = con.execute("""
SELECT token_address, network_id, recorded_at, thesis_total, thesis_sampled,
       thesis_count, has_next_page, thesis_likes, thesis_replies,
       thesis_authors, holder_authors, newest_thesis_at, raw_json
  FROM token_social
 WHERE thesis_sampled = 0 AND thesis_total > 0
 ORDER BY recorded_at
""").fetchall()
print("n rows =", len(rows))
for r in rows[:6]:
    print({k: r[k] for k in r.keys() if k != "raw_json"})

print("\n=== E. decode raw for every one of them: what did upstream actually send? ===")
import json
shapes = {}
for r in rows:
    raw = dbmod.decode_raw(r["raw_json"])
    obj = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    ro = obj.get("responseObject") if isinstance(obj, dict) else None
    if isinstance(ro, dict):
        keys = tuple(sorted(ro.keys()))
        items = ro.get("items")
        desc = (
            "ro=dict keys=%s count=%r hasNext=%r items_type=%s items_len=%s"
            % (keys, ro.get("count"), ro.get("hasNextPage"),
               type(items).__name__,
               len(items) if isinstance(items, list) else "-")
        )
    elif isinstance(ro, list):
        desc = "ro=list len=%d" % len(ro)
    else:
        desc = "ro=%s" % type(ro).__name__
    top = tuple(sorted(obj.keys())) if isinstance(obj, dict) else type(obj).__name__
    shapes.setdefault((top, desc), 0)
    shapes[(top, desc)] += 1
for k, v in sorted(shapes.items(), key=lambda x: -x[1]):
    print(v, "x", k)

print("\n=== F. for contrast: shape of 5 rows with sampled=0 AND total=0 ===")
for r in con.execute("""SELECT raw_json FROM token_social
                         WHERE thesis_sampled=0 AND thesis_total=0 LIMIT 5"""):
    raw = dbmod.decode_raw(r["raw_json"])
    obj = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    ro = obj.get("responseObject") if isinstance(obj, dict) else None
    print(type(ro).__name__,
          {k: ro.get(k) for k in ("count", "hasNextPage")} if isinstance(ro, dict) else ro,
          "items_len=", len(ro.get("items")) if isinstance(ro, dict) and isinstance(ro.get("items"), list) else "-")
con.close()
