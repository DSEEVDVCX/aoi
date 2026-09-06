"""Verify the age hole is closed: every watched coin should carry a creation date."""
from __future__ import annotations

import os
import sqlite3

import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=30)
con.row_factory = sqlite3.Row

SHAPE = """
SELECT COUNT(*) tot,
       SUM(CASE WHEN t.token_address IS NULL THEN 1 ELSE 0 END) no_row,
       SUM(CASE WHEN t.token_address IS NOT NULL
                 AND (t.token_created_at IS NULL OR t.token_created_at='')
                THEN 1 ELSE 0 END) no_age
  FROM ({src}) w
  LEFT JOIN token_static t
    ON t.token_address=w.token_address AND t.network_id=w.network_id
"""

for label, src in (
    ("ACTIVE WATCHES", "SELECT token_address, network_id FROM watchlist WHERE active=1"),
    ("EVER WATCHED  ", "SELECT DISTINCT token_address, network_id FROM watch_windows"),
):
    r = con.execute(SHAPE.format(src=src)).fetchone()
    print(f"{label} : {r['tot']:5}   no static row: {r['no_row']:4}   "
          f"row but no age: {r['no_age']:4}")

print()
for k in ("last_cycle_stats", "recorder_last_run_at", "last_error_filter"):
    row = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    print(f"{k}: {row[0][:260] if row else None}")
con.close()
