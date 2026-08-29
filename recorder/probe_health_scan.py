"""Per-column health of training_rows: nulls, zeros, distinct, min, max.

Runs over (A) the model population and (B) all of training_rows.
Writes probe_health.json.
"""
import os, sqlite3, json
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

info = list(con.execute("PRAGMA table_info(training_rows)"))
cols = [(r["name"], (r["type"] or "").upper()) for r in info]

MODEL_WHERE = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
  AND is_independent=1
  AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""


def exprs_for(name, typ):
    q = f'"{name}"'
    e = [
        f"SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END)",
        f"SUM(CASE WHEN {q} = 0 THEN 1 ELSE 0 END)",
        f"COUNT(DISTINCT {q})",
    ]
    if "BLOB" in typ:
        e += ["NULL", "NULL"]
    elif "TEXT" in typ or "CHAR" in typ:
        e += [f"substr(MIN({q}),1,40)", f"substr(MAX({q}),1,40)"]
    else:
        e += [f"MIN({q})", f"MAX({q})"]
    return e


def scan(where, label):
    out = {}
    total = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {where}").fetchone()["n"]
    CH = 30
    for i in range(0, len(cols), CH):
        chunk = cols[i:i + CH]
        sel = []
        for name, typ in chunk:
            sel.extend(exprs_for(name, typ))
        sql = "SELECT " + ", ".join(sel) + f" FROM training_rows WHERE {where}"
        row = con.execute(sql).fetchone()
        vals = list(row)
        for j, (name, typ) in enumerate(chunk):
            nulls, zeros, distinct, mn, mx = vals[j * 5:j * 5 + 5]
            out[name] = {
                "type": typ, "total": total, "nulls": nulls, "zeros": zeros,
                "distinct": distinct, "min": mn, "max": mx,
            }
        print(f"  [{label}] {i + len(chunk)}/{len(cols)} cols done", flush=True)
    return {"total": total, "cols": out}


res = {}
res["model"] = scan(MODEL_WHERE, "model")
res["all"] = scan("1=1", "all")

with open("probe_health.json", "w", encoding="utf-8") as f:
    json.dump(res, f, default=str, indent=0)
print("model total:", res["model"]["total"], " all total:", res["all"]["total"])
print("wrote probe_health.json")
