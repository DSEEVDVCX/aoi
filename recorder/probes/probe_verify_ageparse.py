import os, sqlite3, config, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features
import recorder as rec

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== A. my own classification: typeof + length, NOT glob ===")
q = """
SELECT typeof(token_created_at) AS ty,
       CASE WHEN token_created_at IS NULL THEN -1
            ELSE length(CAST(token_created_at AS TEXT)) END AS len,
       COUNT(*) n,
       MIN(CAST(token_created_at AS TEXT)) AS sample_min,
       MAX(CAST(token_created_at AS TEXT)) AS sample_max
FROM token_static
GROUP BY ty, len ORDER BY n DESC
"""
for r in con.execute(q):
    print(dict(r))

print()
print("=== B. total rows in token_static ===")
print(dict(con.execute("SELECT COUNT(*) total FROM token_static").fetchone()))

print()
print("=== C. run BOTH parsers over EVERY distinct stored value (ground truth) ===")
NOW = "2026-08-22T12:00:00+00:00"
rows = con.execute(
    "SELECT token_created_at AS c, COUNT(*) n FROM token_static GROUP BY 1"
).fetchall()
disagree = []
recorder_none = 0
recorder_none_rows = 0
feat_none = 0
tot_rows = 0
for r in rows:
    c = r["c"]
    n = r["n"]
    tot_rows += n
    a = rec.token_age_days(c, NOW)
    e = features.epoch_of(c)
    b = None
    if e is not None:
        b = (int(__import__("datetime").datetime.fromisoformat(NOW).timestamp()) - e) / 86400.0
        if b < 0:
            b = None
    if a is None:
        recorder_none += 1
        recorder_none_rows += n
    if b is None:
        feat_none += 1
    # disagreement = one yields an age, the other does not, or ages differ >1 day
    if (a is None) != (b is None) or (a is not None and b is not None and abs(a - b) > 1.0):
        disagree.append((repr(c)[:40], n, a, b))
print(f"distinct values={len(rows)} rows={tot_rows}")
print(f"recorder.token_age_days -> None : {recorder_none} distinct / {recorder_none_rows} rows")
print(f"features-epoch-derived  -> None : {feat_none} distinct")
print(f"DISAGREEMENTS (the actual defect surface): {len(disagree)}")
for d in disagree[:25]:
    print("   ", d)

print()
print("=== D. also run THEIR query verbatim for comparison ===")
their = """
SELECT COUNT(*) total,
  SUM(CASE WHEN token_created_at IS NULL OR token_created_at='' THEN 1 ELSE 0 END) missing,
  SUM(CASE WHEN token_created_at NOT GLOB '*[^0-9]*' AND token_created_at<>'' THEN 1 ELSE 0 END) pure_digits,
  SUM(CASE WHEN token_created_at GLOB '*[^0-9]*' THEN 1 ELSE 0 END) has_nondigit,
  SUM(CASE WHEN token_created_at GLOB '*T*' THEN 1 ELSE 0 END) looks_iso,
  SUM(CASE WHEN token_created_at NOT GLOB '*[^0-9]*' AND token_created_at<>''
            AND CAST(token_created_at AS INTEGER) > 100000000000 THEN 1 ELSE 0 END) ms_magnitude
  FROM token_static
"""
print(dict(con.execute(their).fetchone()))
con.close()
