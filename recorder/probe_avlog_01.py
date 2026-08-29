import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = con.execute("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v").fetchone()["v"]
print("current_feature_version =", FV)

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1 AND feature_version=?"""

n = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP}", (FV,)).fetchone()["c"]
print("model population rows =", n)

# --- my own formulation: bucket by (sign of size_usd) x (log null?) ----------
print("\n=== A) bucket: sign(size_usd) x log_size_usd IS NULL ===")
q = f"""
SELECT CASE
         WHEN size_usd IS NULL THEN 'size NULL'
         WHEN size_usd > 0     THEN 'size > 0'
         WHEN size_usd = 0.0   THEN 'size == 0.0'
         ELSE                       'size < 0'
       END AS bucket,
       CASE WHEN log_size_usd IS NULL THEN 'log NULL' ELSE 'log set' END AS logst,
       COUNT(*) AS n,
       MIN(size_usd) AS mn, MAX(size_usd) AS mx,
       MIN(log_size_usd) AS lmn
  FROM training_rows
 WHERE {POP}
 GROUP BY bucket, logst
 ORDER BY bucket, logst
"""
tot_lognull = 0
for r in con.execute(q, (FV,)):
    print(f"  {r['bucket']:<12} | {r['logst']:<8} | n={r['n']:>6} | size[{r['mn']}, {r['mx']}] | min_log={r['lmn']}")
    if r["logst"] == "log NULL":
        tot_lognull += r["n"]
print("  total log_size_usd NULL =", tot_lognull,
      f"({100.0*tot_lognull/n:.2f}% of {n})")

# --- B) is the zero really preserved elsewhere in the SAME row? -------------
print("\n=== B) same rows: what other derivations of size survive? ===")
q2 = f"""
SELECT COUNT(*) AS z,
       SUM(CASE WHEN size_to_mcap IS NULL THEN 1 ELSE 0 END) AS s2m_null,
       SUM(CASE WHEN size_to_mcap = 0.0  THEN 1 ELSE 0 END) AS s2m_zero,
       SUM(CASE WHEN market_cap IS NULL OR market_cap = 0 THEN 1 ELSE 0 END) AS mc_bad,
       SUM(CASE WHEN log_market_cap IS NULL THEN 1 ELSE 0 END) AS logmc_null
  FROM training_rows
 WHERE {POP} AND size_usd = 0.0
"""
r = con.execute(q2, (FV,)).fetchone()
print(dict(r))

# --- C) per signal_type, my own grouping ------------------------------------
print("\n=== C) per signal_type ===")
q3 = f"""
SELECT signal_type,
       COUNT(*) AS n,
       SUM(size_usd IS NULL)                     AS sz_null,
       SUM(size_usd = 0.0)                       AS sz_zero,
       SUM(size_usd < 0)                         AS sz_neg,
       SUM(log_size_usd IS NULL)                 AS log_null,
       ROUND(100.0*SUM(log_size_usd IS NULL)/COUNT(*),2) AS pct_log_null
  FROM training_rows
 WHERE {POP}
 GROUP BY signal_type ORDER BY n DESC
"""
for r in con.execute(q3, (FV,)):
    print("  ", dict(r))

# --- D) population-inflation control: whole table & other slices ------------
print("\n=== D) population control (is the % inflated by the wrong slice?) ===")
for label, extra, args in [
    ("ALL training_rows",                    "1=1", ()),
    ("kind='signal' only",                   "kind='signal'", ()),
    ("signal + is_live=1",                   "kind='signal' AND is_live=1", ()),
    ("signal + is_live=0 (retro)",           "kind='signal' AND is_live=0", ()),
    ("model pop, current fv",                POP.replace("?", str(FV)), ()),
    ("signal live meme ok indep, ANY fv",    "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1", ()),
]:
    r = con.execute(f"""SELECT COUNT(*) n,
                        SUM(log_size_usd IS NULL) ln,
                        SUM(size_usd = 0.0) z,
                        SUM(size_usd < 0) neg
                        FROM training_rows WHERE {extra}""", args).fetchone()
    pct = (100.0*r["ln"]/r["n"]) if r["n"] else 0
    print(f"  {label:<38} n={r['n']:>7} log_null={r['ln']:>6} ({pct:5.2f}%) zero={r['z']:>6} neg={r['neg']:>5}")

con.close()
