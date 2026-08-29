import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1
     AND feature_version = CAST(COALESCE((SELECT value FROM meta
         WHERE key='current_feature_version'),'0') AS INTEGER)"""

# rows with thesis_counted=0 whose token was never paged into token_thesis
sel = f"""SELECT key, token_address a, network_id nid, entry_ts,
                 thesis_counted tc, thesis_authors_before tab,
                 social_thesis_total stt, social_thesis_authors sta,
                 social_snapshot_age_min age
            FROM training_rows tr
           WHERE {POP} AND thesis_counted=0
             AND NOT EXISTS (SELECT 1 FROM token_thesis t
                              WHERE LOWER(t.token_address)=LOWER(tr.token_address))"""

n_never = con.execute(f"SELECT COUNT(*) FROM ({sel})").fetchone()[0]
print("never-paged zero rows:", n_never)

# how many of those have a pre-t0 social snapshot with >=1 sampled item
q = f"""
WITH z AS ({sel})
SELECT COUNT(*) rows_total,
       SUM(CASE WHEN s.sampled IS NULL THEN 1 ELSE 0 END) no_snapshot,
       SUM(CASE WHEN s.sampled=0 THEN 1 ELSE 0 END) snap_empty,
       SUM(CASE WHEN s.sampled>0 THEN 1 ELSE 0 END) snap_has_items,
       MAX(s.sampled) max_sampled
  FROM z LEFT JOIN (
       SELECT token_address, network_id,
              CAST(strftime('%s', recorded_at) AS INTEGER) e, thesis_sampled sampled
         FROM token_social) s
    ON s.token_address = z.a AND s.network_id = z.nid AND s.e <= z.entry_ts
"""
# that join multiplies rows; do it per-row with a correlated subquery instead
q2 = f"""
WITH z AS ({sel})
SELECT COUNT(*) rows_total,
       SUM(CASE WHEN sampled IS NULL THEN 1 ELSE 0 END) no_snapshot,
       SUM(CASE WHEN sampled=0 THEN 1 ELSE 0 END) snap_empty,
       SUM(CASE WHEN sampled>0 THEN 1 ELSE 0 END) snap_has_items
  FROM (SELECT z.*, (SELECT thesis_sampled FROM token_social s
                      WHERE s.token_address=z.a AND s.network_id=z.nid
                        AND CAST(strftime('%s', s.recorded_at) AS INTEGER) <= z.entry_ts
                      ORDER BY s.recorded_at DESC LIMIT 1) sampled
          FROM z)
"""
print("\n== pre-t0 social snapshot presence on never-paged zero rows ==")
print(dict(con.execute(q2).fetchone()))

# decisive: decode the pre-t0 envelope and count items whose createdAt <= t0
print("\n== decode envelope: items written at/before t0 on never-paged zero rows ==")
sample = con.execute(f"""
  WITH z AS ({sel})
  SELECT z.*, (SELECT recorded_at FROM token_social s
                WHERE s.token_address=z.a AND s.network_id=z.nid
                  AND CAST(strftime('%s', s.recorded_at) AS INTEGER) <= z.entry_ts
                ORDER BY s.recorded_at DESC LIMIT 1) snap_at
    FROM z
   ORDER BY z.stt DESC LIMIT 25""").fetchall()

import datetime as dt
def ep(iso):
    if not iso: return None
    s = iso.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None: d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())

bad = 0
for r in sample:
    if not r["snap_at"]:
        print("no snapshot", r["key"]); continue
    raw = con.execute(
        "SELECT raw_json FROM token_social WHERE token_address=? AND network_id=? "
        "AND recorded_at=?", (r["a"], r["nid"], r["snap_at"])).fetchone()["raw_json"]
    env = dbmod.decode_raw(raw)
    ro = env.get("responseObject") if isinstance(env, dict) else None
    items = ro.get("items") if isinstance(ro, dict) else []
    items = items if isinstance(items, list) else []
    before = [i for i in items
              if (e := ep(i.get("createdAt"))) is not None and e <= r["entry_ts"]]
    if before: bad += 1
    print(f"{r['a'][:10]}.. net={r['nid']} t0={r['entry_ts']} tc={r['tc']} "
          f"stt={r['stt']} env_items={len(items)} items_created_before_t0={len(before)} "
          f"oldest_before={min((i.get('createdAt') for i in before), default=None)}")
print(f"\nsampled {len(sample)} rows; {bad} had >=1 envelope item written at/before t0 "
      f"while thesis_counted=0")
con.close()
