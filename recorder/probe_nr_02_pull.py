import os, sqlite3, pickle, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FC = list(features.FEATURE_COLUMNS)
MC = list(features.META_COLUMNS)
LC = list(features.LABEL_COLUMNS)
cols = MC + FC + LC

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
 AND is_independent=1 AND feature_version=CAST(COALESCE(
 (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""

sql = f"SELECT {','.join(cols)} FROM training_rows WHERE {POP}"
rows = [dict(r) for r in con.execute(sql)]
print("rows:", len(rows))
with open("probe_nr_rows.pkl", "wb") as f:
    pickle.dump({"rows": rows, "cols": cols}, f)
print("saved probe_nr_rows.pkl")
