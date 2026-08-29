"""Sample the recorder's age-gate counters once per cycle for ten minutes."""
from __future__ import annotations

import ast
import os
import sqlite3
import time

import config

URI = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"


def snap():
    con = sqlite3.connect(URI, uri=True, timeout=30)
    try:
        rows = dict(con.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('last_cycle_at','last_cycle_stats','age_rejected_total',"
            "'age_lookup_last_ok_at','age_lookup_failed_streak',"
            "'last_error_age_lookup')"
        ).fetchall())
    finally:
        con.close()
    return rows


seen = ""
deadline = time.time() + 600
while time.time() < deadline:
    m = snap()
    at = m.get("last_cycle_at", "")
    if at and at != seen:
        seen = at
        s = ast.literal_eval(m.get("last_cycle_stats") or "{}")
        print(
            f"{at[11:19]}  signals={s.get('signals'):>2} "
            f"watch_added={s.get('watch_added'):>2} "
            f"rejected={s.get('age_rejected')} unknown={s.get('age_unknown')} "
            f"resolved={s.get('age_resolved')} "
            f"lookup_failed={s.get('age_lookup_failed')} "
            f"| total_rejected={m.get('age_rejected_total')} "
            f"streak={m.get('age_lookup_failed_streak')}",
            flush=True,
        )
    time.sleep(5)
err = snap().get("last_error_age_lookup")
print(f"last_error_age_lookup = {err}")
