import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = ("(SELECT CAST(COALESCE((SELECT value FROM meta "
      "WHERE key='current_feature_version'),'0') AS INTEGER))")
POP = (f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version = {FV}")

def show(title, sql, args=()):
    print("\n== " + title)
    print("   SQL: " + " ".join(sql.split()))
    for r in con.execute(sql, args).fetchall():
        print("   " + " | ".join(f"{k}={r[k]}" for k in r.keys()))

print("current_feature_version =",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

# ---- 1. MY OWN framing: group by the actual value, not MIN/MAX/DISTINCT ----
show("A1 all training_rows, value histogram (my framing, not theirs)",
     """SELECT CASE WHEN listed_on_exchange IS NULL THEN 'NULL'
                    ELSE CAST(listed_on_exchange AS TEXT) END AS val,
               COUNT(*) AS rows
          FROM training_rows GROUP BY 1 ORDER BY rows DESC""")

show("A2 MODEL POPULATION only, value histogram",
     f"""SELECT CASE WHEN listed_on_exchange IS NULL THEN 'NULL'
                     ELSE CAST(listed_on_exchange AS TEXT) END AS val,
                COUNT(*) AS rows
           FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY rows DESC""")

show("A3 model population total + is_live/feature_version split of ALL rows",
     f"""SELECT (SELECT COUNT(*) FROM training_rows) AS all_rows,
                (SELECT COUNT(*) FROM training_rows WHERE is_live=1) AS live_rows,
                (SELECT COUNT(*) FROM training_rows
                   WHERE feature_version = {FV}) AS cur_fv_rows,
                (SELECT COUNT(*) FROM training_rows WHERE {POP}) AS model_pop""")

# ---- 2. Is the constancy an artifact of scanning stale/retro rows? ----
show("A4 per-feature_version, per-is_live distinct values of listed_on_exchange",
     """SELECT feature_version, is_live, kind,
               COUNT(*) rows,
               SUM(listed_on_exchange IS NULL) nulls,
               SUM(listed_on_exchange=0) zeros,
               SUM(listed_on_exchange=1) ones,
               COUNT(DISTINCT listed_on_exchange) d
          FROM training_rows GROUP BY 1,2,3 ORDER BY rows DESC""")

# ---- 3. Does the NULL pattern match the SOURCE being absent (measured absence)? ----
show("A5 cross-tab listed_on_exchange x exchanges_count-nullness, model population",
     f"""SELECT CASE WHEN listed_on_exchange IS NULL THEN 'loe:NULL'
                     ELSE 'loe:'||CAST(listed_on_exchange AS TEXT) END AS loe,
                CASE WHEN exchanges_count IS NULL THEN 'exc:NULL'
                     ELSE 'exc:'||CAST(exchanges_count AS TEXT) END AS exc,
                COUNT(*) rows
           FROM training_rows WHERE {POP} GROUP BY 1,2 ORDER BY rows DESC""")

# ---- 4. Does the sibling column exchanges_count carry variance? (is the FAMILY dead?)
show("A6 exchanges_count variance in the model population",
     f"""SELECT COUNT(*) n, SUM(exchanges_count IS NULL) nulls,
                COUNT(DISTINCT exchanges_count) d,
                MIN(exchanges_count) mn, MAX(exchanges_count) mx,
                AVG(exchanges_count*1.0) avg
           FROM training_rows WHERE {POP}""")

show("A7 exchanges_count histogram, model population",
     f"""SELECT COALESCE(CAST(exchanges_count AS TEXT),'NULL') v, COUNT(*) rows
           FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY rows DESC""")

# ---- 5. The SOURCE table: is exchanges_count ever 0 or NULL there? ----
show("A8 token_static shape",
     """SELECT COUNT(*) rows, COUNT(DISTINCT token_address||'|'||network_id) tokens
          FROM token_static""")

show("A9 token_static exchanges_count histogram (all rows, not distinct tokens)",
     """SELECT COALESCE(CAST(exchanges_count AS TEXT),'NULL') v, COUNT(*) rows
          FROM token_static GROUP BY 1 ORDER BY CAST(v AS INTEGER)""")

show("A10 token_static: exchanges_json='[]' vs exchanges_count (the absent-vs-zero branch)",
     """SELECT CASE WHEN exchanges_json IS NULL THEN 'json:NULL'
                    WHEN exchanges_json='[]' THEN 'json:[]'
                    ELSE 'json:nonempty' END AS j,
               COALESCE(CAST(exchanges_count AS TEXT),'NULL') AS c,
               COUNT(*) rows
          FROM token_static GROUP BY 1,2 ORDER BY rows DESC""")

# ---- 6. per-network: is this a one-network artifact? ----
show("A11 model population: listed_on_exchange by network",
     f"""SELECT network_id,
                COUNT(*) rows, SUM(listed_on_exchange IS NULL) nulls,
                SUM(listed_on_exchange=1) ones, SUM(listed_on_exchange=0) zeros,
                COUNT(DISTINCT exchanges_count) exc_d
           FROM training_rows WHERE {POP}
          GROUP BY 1 ORDER BY rows DESC""")

# ---- 7. sanity: is 1 fabricated anywhere (loe=1 while source NULL)? ----
show("A12 contradiction check: loe non-null while exchanges_count IS NULL",
     """SELECT COUNT(*) bad FROM training_rows
         WHERE listed_on_exchange IS NOT NULL AND exchanges_count IS NULL""")

show("A13 the schema's OWN precedent: has_image constancy in token_static",
     """SELECT COALESCE(CAST(has_image AS TEXT),'NULL') v, COUNT(*) rows
          FROM token_static GROUP BY 1 ORDER BY rows DESC""")

# ---- 8. run THEIR exact SQL for agreement ----
print("\n== B THEIR exact SQL, verbatim")
for r in con.execute("""SELECT COUNT(*) n, SUM(listed_on_exchange IS NULL) nulls,
       SUM(listed_on_exchange=0) zeros, COUNT(DISTINCT listed_on_exchange) d,
       MIN(listed_on_exchange) mn, MAX(listed_on_exchange) mx FROM training_rows"""):
    print("   training_rows: " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
for r in con.execute("""SELECT COUNT(*) n, SUM(exchanges_count IS NULL) nulls,
       SUM(exchanges_count=0) zeros, COUNT(DISTINCT exchanges_count) d,
       MIN(exchanges_count) mn, MAX(exchanges_count) mx FROM token_static"""):
    print("   token_static: " + " | ".join(f"{k}={r[k]}" for k in r.keys()))

con.close()
