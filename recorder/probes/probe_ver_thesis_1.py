import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1
     AND feature_version = CAST(COALESCE((SELECT value FROM meta
         WHERE key='current_feature_version'),'0') AS INTEGER)"""

print("feature_version:", con.execute(
    "SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

# --- P1 my own formulation: GROUP BY category, not SUM(CASE) ---
print("\n== P1 population + thesis_counted categories ==")
for col in ("thesis_counted", "thesis_authors_before", "thesis_1h", "thesis_24h",
            "thesis_counted_capped", "thesis_accel", "hours_since_last_thesis",
            "thesis_history_days"):
    rows = con.execute(f"""
        SELECT CASE WHEN {col} IS NULL THEN 'NULL'
                    WHEN {col}=0 THEN 'zero' ELSE 'positive' END cat,
               COUNT(*) n
          FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1""").fetchall()
    tot = sum(r["n"] for r in rows)
    print(f"{col:26s} total={tot:6d} " + "  ".join(f"{r['cat']}={r['n']}" for r in rows))

# --- P2 cross-tab zero vs social_thesis_total, my own GROUP BY form ---
print("\n== P2 rows with thesis_counted=0: social_thesis_total category ==")
for r in con.execute(f"""
    SELECT CASE WHEN social_thesis_total IS NULL THEN 'NULL'
                WHEN social_thesis_total=0 THEN 'zero' ELSE 'positive' END cat,
           COUNT(*) n, MIN(social_thesis_total) mn, MAX(social_thesis_total) mx,
           MIN(social_thesis_authors) amn, MAX(social_thesis_authors) amx
      FROM training_rows WHERE {POP} AND thesis_counted=0
     GROUP BY 1 ORDER BY 1"""):
    print(dict(r))

print("\n== P2b same for rows with thesis_counted>0 (control) ==")
for r in con.execute(f"""
    SELECT CASE WHEN social_thesis_total IS NULL THEN 'NULL'
                WHEN social_thesis_total=0 THEN 'zero' ELSE 'positive' END cat,
           COUNT(*) n, MAX(thesis_counted) max_tc, MAX(social_thesis_total) mx
      FROM training_rows WHERE {POP} AND thesis_counted>0
     GROUP BY 1 ORDER BY 1"""):
    print(dict(r))

print("\n== P2c authors: thesis_authors_before=0 vs social_thesis_authors ==")
for r in con.execute(f"""
    SELECT CASE WHEN social_thesis_authors IS NULL THEN 'NULL'
                WHEN social_thesis_authors=0 THEN 'zero' ELSE 'positive' END cat,
           COUNT(*) n, MAX(social_thesis_authors) mx
      FROM training_rows WHERE {POP} AND thesis_authors_before=0
     GROUP BY 1 ORDER BY 1"""):
    print(dict(r))

# --- P3 token level coverage, exact / case-insensitive / address-only ---
print("\n== P3 token-level token_thesis coverage ==")
r = con.execute(f"""
  WITH pop AS (SELECT DISTINCT token_address a, network_id nid
                 FROM training_rows WHERE {POP})
  SELECT COUNT(*) tokens,
         SUM(EXISTS(SELECT 1 FROM token_thesis t
                     WHERE t.token_address=pop.a AND t.network_id=pop.nid)) exact_hit,
         SUM(EXISTS(SELECT 1 FROM token_thesis t
                     WHERE LOWER(t.token_address)=LOWER(pop.a)
                       AND CAST(t.network_id AS TEXT)=CAST(pop.nid AS TEXT))) ci_hit,
         SUM(EXISTS(SELECT 1 FROM token_thesis t
                     WHERE LOWER(t.token_address)=LOWER(pop.a))) addr_only_hit
    FROM pop""").fetchone()
print(dict(r))

print("\n== P3b token_thesis table shape ==")
r = con.execute("""SELECT COUNT(*) rows, COUNT(DISTINCT token_address) toks,
                          COUNT(DISTINCT network_id) nets,
                          MIN(created_at) c_min, MAX(created_at) c_max,
                          MIN(fetched_at) f_min, MAX(fetched_at) f_max,
                          COUNT(DISTINCT fetched_at) f_distinct
                     FROM token_thesis""").fetchone()
print(dict(r))
print("network split:", [dict(x) for x in con.execute(
    "SELECT network_id, COUNT(*) n, COUNT(DISTINCT token_address) t"
    " FROM token_thesis GROUP BY 1 ORDER BY n DESC").fetchall()])

# --- P4 row-level mechanism split, my own phrasing (LEFT JOIN count) ---
print("\n== P4 rows thesis_counted=0 split by whether token has ANY thesis row ==")
for r in con.execute(f"""
  WITH pop AS (SELECT token_address a, network_id nid, thesis_counted tc, ts
                 FROM training_rows WHERE {POP})
  SELECT tc IS NULL AS tc_null, tc=0 AS tc_zero,
         EXISTS(SELECT 1 FROM token_thesis t
                 WHERE LOWER(t.token_address)=LOWER(pop.a)) AS token_ever_paged,
         COUNT(*) n
    FROM pop GROUP BY 1,2,3 ORDER BY 4 DESC"""):
    print(dict(r))

con.close()
