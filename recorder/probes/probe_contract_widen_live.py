"""Did the widened contract scan start writing BSC and Robinhood rows?"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import config

sys.stdout.reconfigure(encoding="utf-8")
URI = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
START = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def snap():
    con = sqlite3.connect(URI, uri=True, timeout=120)
    try:
        rows = con.execute(
            """SELECT network_id net, COUNT(*) n, MAX(recorded_at) hi
                 FROM evm_contract GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        err = con.execute(
            "SELECT value FROM meta WHERE key='last_error_evm_contract'"
        ).fetchone()
    finally:
        con.close()
    return rows, (err[0] if err else None)


print(f"watching from {START}Z for new BSC (56) / Robinhood (4663) rows\n")
deadline = time.time() + 600
seen = ""
while time.time() < deadline:
    rows, err = snap()
    line = " | ".join(f"net {r[0]}: {r[1]} rows, newest {r[2][11:19]}" for r in rows)
    if line != seen:
        seen = line
        print(line, flush=True)
        if any(r[0] in ("56", "4663") and r[2] > START for r in rows):
            print("\n-> the widened networks are writing rows")
            break
    time.sleep(15)
else:
    print("\n-> no new 56/4663 row within 600s")
_, err = snap()
print(f"last_error_evm_contract = {err}")
