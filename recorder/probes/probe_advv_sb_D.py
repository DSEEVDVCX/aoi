import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

def show(t, rows):
    print("==", t)
    for r in rows:
        print("   ", dict(r))
    print()

FV = 12
MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND feature_version=%d" % FV)

# Recount, from token_bars AS IT IS TODAY (post backfill_bar_flags), how many
# suspect-wick bars actually sit inside each legacy row's 48h window.
SQL = """
WITH pop AS (
  SELECT key, token_address, network_id, entry_ts FROM training_rows
   WHERE {model} AND {bucket}
), per AS (
  SELECT p.key,
         COUNT(b.ts) AS nbars,
         COALESCE(SUM(CASE WHEN b.h_suspect=1 OR b.l_suspect=1 OR b.c_suspect=1
                           THEN 1 ELSE 0 END),0) AS nsus
    FROM pop p
    LEFT JOIN token_bars b
           ON b.token_address = p.token_address
          AND b.network_id    = p.network_id
          AND b.resolution    = '5'
          AND b.ts >  p.entry_ts
          AND b.ts <= p.entry_ts + 172800
   GROUP BY p.key
)
SELECT COUNT(*) rows_n,
       SUM(nbars = 0)  AS rows_with_no_bars,
       SUM(nsus = 0)   AS rows_truly_clean,
       SUM(nsus > 0)   AS rows_with_suspect_bars,
       MAX(nsus)       AS max_suspect_in_a_window,
       ROUND(AVG(nbars),1) AS avg_bars
  FROM per
"""

show("LEGACY bucket (suspect_bars IS NULL) recounted against today's flags",
     q(SQL.format(model=MODEL, bucket="suspect_bars IS NULL")))

show("CONTROL: the 711 earliest KEPT rows (suspect_bars=0) recounted the same way",
     q(SQL.format(model=MODEL,
                  bucket=("suspect_bars = 0 AND entry_ts <= (SELECT MIN(entry_ts) FROM ("
                          "SELECT entry_ts FROM training_rows WHERE %s AND suspect_bars=0 "
                          "ORDER BY entry_ts LIMIT 711 OFFSET 710))" % MODEL))))

# case-sensitivity control: does a NOCASE join find more bars for the legacy rows?
show("case-sensitivity control on the legacy join", q(f"""
WITH pop AS (
  SELECT key, token_address, network_id, entry_ts FROM training_rows
   WHERE {MODEL} AND suspect_bars IS NULL
)
SELECT COUNT(*) rows_n,
       SUM(exact_n = 0) no_bars_exact,
       SUM(nocase_n = 0) no_bars_nocase
  FROM (SELECT p.key,
          (SELECT COUNT(*) FROM token_bars b WHERE b.token_address=p.token_address
             AND b.network_id=p.network_id AND b.resolution='5'
             AND b.ts>p.entry_ts AND b.ts<=p.entry_ts+172800) exact_n,
          (SELECT COUNT(*) FROM token_bars b WHERE b.token_address=p.token_address COLLATE NOCASE
             AND b.network_id=p.network_id AND b.resolution='5'
             AND b.ts>p.entry_ts AND b.ts<=p.entry_ts+172800) nocase_n
        FROM pop p)"""))
con.close()
