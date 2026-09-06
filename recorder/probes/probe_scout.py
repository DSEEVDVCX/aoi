"""Scout: table sizes, feature version, ROW_COLUMNS vs table columns, error meta."""
from __future__ import annotations

import os
import sqlite3

import config
import features

URI = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(URI, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print(f"DB size MB: {os.path.getsize(config.DB_PATH) / 1e6:.1f}\n")

print("== tables ==")
tables = [
    r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]
for t in tables:
    n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    print(f"  {t:34} {n:>9}")

fv = con.execute(
    "SELECT value FROM meta WHERE key='current_feature_version'"
).fetchone()
print(f"\ncurrent_feature_version: {fv[0] if fv else None}")

print("\n== ROW_COLUMNS vs training_rows table columns ==")
tab_cols = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
row_cols = list(features.ROW_COLUMNS)
print(f"  table columns : {len(tab_cols)}")
print(f"  ROW_COLUMNS   : {len(row_cols)}")
in_table_only = [c for c in tab_cols if c not in row_cols]
in_rowcols_only = [c for c in row_cols if c not in tab_cols]
print(f"  in table but NOT written by the builder (NULL forever): {in_table_only}")
print(f"  in ROW_COLUMNS but missing from the table            : {in_rowcols_only}")

print("\n== meta error / streak keys ==")
for r in con.execute(
    "SELECT key, value FROM meta WHERE key LIKE 'last_error%' "
    "OR key LIKE '%streak%' OR key LIKE '%_last_ok_at' "
    "OR key LIKE '%_last_run_at' ORDER BY key"
):
    val = str(r["value"])
    print(f"  {r['key']:44} {val[:110]}")

con.close()
