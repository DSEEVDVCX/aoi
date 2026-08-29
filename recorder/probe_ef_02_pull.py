import os
import pickle
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import features  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

cols = list(features.ROW_COLUMNS)
sel = ", ".join(cols)

sql = f"""
SELECT {sel} FROM training_rows
 WHERE kind='signal' AND asset_class='meme' AND status='ok'
   AND feature_version = CAST(COALESCE(
       (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
"""
rows = [tuple(r) for r in con.execute(sql)]
print("pulled (fv12, signal, meme, ok, any is_live/is_independent):", len(rows))

with open(os.path.join(HERE, "probe_ef_rows.pkl"), "wb") as fh:
    pickle.dump({"cols": cols, "rows": rows}, fh)

# breakdown of the wider cohort
for q, lbl in [
    ("SELECT is_live, is_independent, COUNT(*) n FROM training_rows WHERE kind='signal' AND asset_class='meme' AND status='ok' AND feature_version=12 GROUP BY 1,2", "fv12 live/indep"),
    ("SELECT is_live, COUNT(*) n FROM training_rows GROUP BY 1", "whole table is_live"),
    ("SELECT feature_version, COUNT(*) n FROM training_rows WHERE kind='signal' GROUP BY 1 ORDER BY 1", "fv spread (signal)"),
    ("SELECT status, COUNT(*) n FROM training_rows WHERE kind='signal' AND feature_version=12 GROUP BY 1", "status fv12"),
]:
    print("--", lbl)
    for r in con.execute(q):
        print("   ", tuple(r))
con.close()
