import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1
     AND feature_version = CAST(COALESCE((SELECT value FROM meta
         WHERE key='current_feature_version'),'0') AS INTEGER)"""

cols = [r[1] for r in con.execute("PRAGMA table_info(training_rows)")]
print("identity-ish cols:", [c for c in cols if c in
      ("kind","key","token_address","network_id","signal_ts","t0","ts","recorded_at",
       "signal_at","feature_version")])

# --- P4 fixed: mechanism split (token ever paged?) ---
print("\n== P4 thesis_counted=0 split by token_thesis presence (case-insensitive) ==")
for r in con.execute(f"""
  WITH pop AS (SELECT token_address a, network_id nid, thesis_counted tc
                 FROM training_rows WHERE {POP})
  SELECT tc=0 AS tc_zero,
         EXISTS(SELECT 1 FROM token_thesis t
                 WHERE LOWER(t.token_address)=LOWER(pop.a)) AS token_ever_paged,
         COUNT(*) n
    FROM pop GROUP BY 1,2 ORDER BY 3 DESC"""):
    print(dict(r))

# --- is social_thesis_total really per-token? ---
print("\n== S1 token_social: total vs sampled on the same snapshot ==")
for r in con.execute("""
  SELECT CASE WHEN thesis_total >= 1000 THEN '>=1000'
              WHEN thesis_total >= 100 THEN '100..999'
              WHEN thesis_total > 0 THEN '1..99' ELSE '0/null' END bucket,
         COUNT(*) snaps, COUNT(DISTINCT token_address) toks,
         MIN(thesis_sampled) smin, MAX(thesis_sampled) smax,
         AVG(thesis_sampled) savg, SUM(has_next_page) nextpg,
         MAX(thesis_total) tmax
    FROM token_social GROUP BY 1 ORDER BY tmax"""):
    print(dict(r))

print("\n== S2 do different tokens share the same thesis_total in one cycle? ==")
for r in con.execute("""
  SELECT recorded_at, COUNT(*) toks, COUNT(DISTINCT thesis_total) distinct_totals,
         MIN(thesis_total) mn, MAX(thesis_total) mx
    FROM token_social GROUP BY recorded_at ORDER BY recorded_at DESC LIMIT 5"""):
    print(dict(r))

print("\n== S3 top thesis_total snapshots, with sampled + raw envelope check ==")
rows = con.execute("""
  SELECT token_address, network_id, recorded_at, thesis_total, thesis_sampled,
         has_next_page, thesis_authors, newest_thesis_at, raw_json
    FROM token_social ORDER BY thesis_total DESC LIMIT 4""").fetchall()
for r in rows:
    raw = dbmod.decode_raw(r["raw_json"])
    ro = (raw or {}).get("responseObject") if isinstance(raw, dict) else None
    items = ro.get("items") if isinstance(ro, dict) else None
    keys = list(ro.keys()) if isinstance(ro, dict) else None
    print({k: r[k] for k in ("token_address","network_id","recorded_at","thesis_total",
                             "thesis_sampled","has_next_page","thesis_authors")},
          "| ro keys:", keys, "| n_items:", (len(items) if isinstance(items, list) else None),
          "| count:", (ro.get("count") if isinstance(ro, dict) else None))

print("\n== S4 per-token spread of thesis_total (is it token-specific?) ==")
for r in con.execute("""
  SELECT token_address, network_id, COUNT(*) snaps, MIN(thesis_total) mn,
         MAX(thesis_total) mx, MAX(thesis_sampled) smax
    FROM token_social GROUP BY 1,2 ORDER BY mx DESC LIMIT 8"""):
    print(dict(r))

con.close()
