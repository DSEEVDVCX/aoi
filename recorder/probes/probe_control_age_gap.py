"""Does the control arm admit coins the signal arm now rejects?"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

import config

sys.stdout.reconfigure(encoding="utf-8")
URI = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(URI, uri=True, timeout=180)
con.row_factory = sqlite3.Row

rows = con.execute(
    """SELECT w.is_control ic, w.first_seen_at fs, t.token_created_at cr
         FROM watch_windows w
         LEFT JOIN token_static t
           ON t.token_address=w.token_address AND t.network_id=w.network_id
        WHERE w.first_seen_at >= '2026-08-22T00:00:00'"""
).fetchall()


def age_days(cr, fs):
    if cr in (None, ""):
        return None
    try:
        c = int(float(cr))
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    a = (int(datetime.fromisoformat(fs).timestamp()) - c) / 86400.0
    return a if a >= 0 else None


for arm, label in ((0, "SIGNAL arm (gated)"), (1, "CONTROL arm (ungated)")):
    sub = [r for r in rows if r["ic"] == arm]
    young = old = unk = 0
    for r in sub:
        a = age_days(r["cr"], r["fs"])
        if a is None:
            unk += 1
        elif a < config.MIN_TOKEN_AGE_DAYS:
            young += 1
        else:
            old += 1
    n = len(sub) or 1
    print(f"\n{label} — windows opened since 2026-08-22: {len(sub)}")
    print(f"  age >= {config.MIN_TOKEN_AGE_DAYS}d : {old:5}  ({100.0*old/n:.1f}%)")
    print(f"  age <  {config.MIN_TOKEN_AGE_DAYS}d : {young:5}  ({100.0*young/n:.1f}%)  <-- gate target")
    print(f"  age unknown  : {unk:5}  ({100.0*unk/n:.1f}%)")

r = con.execute(
    "SELECT COUNT(*) n FROM watchlist WHERE active=1 AND is_control=1"
).fetchone()
print(f"\nactive control watches right now: {r['n']}")
con.close()
