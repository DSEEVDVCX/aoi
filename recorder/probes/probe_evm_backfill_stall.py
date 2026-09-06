"""Why are 114 EVM backfills still 'partial', and are they progressing?"""
from __future__ import annotations

import os
import sqlite3
import sys

import config

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect(
    f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro", uri=True, timeout=180
)
con.row_factory = sqlite3.Row

print("=== evm_backfill_state columns ===")
cols = [r["name"] for r in con.execute("PRAGMA table_info(evm_backfill_state)")]
print(" ", ", ".join(cols))

print("\n=== status x network, with progress and last activity ===")
for r in con.execute(
    """SELECT network_id net, COALESCE(status,'(null)') st, COUNT(*) n,
              MIN(last_try_at) oldest, MAX(last_try_at) newest
         FROM evm_backfill_state GROUP BY 1,2 ORDER BY 1,3 DESC"""
):
    print(f"  net {r['net']:>6} {r['st']:<9} {r['n']:>4}   "
          f"updated {str(r['oldest'])[:16]} .. {str(r['newest'])[:16]}")

print("\n=== are the partials moving? block cursor vs target ===")
for r in con.execute(
    """SELECT network_id net, token_address tok, status st,
              from_block fb, to_block tb, next_block nb,
              COALESCE(transfers,0) tr, last_try_at up, last_error err
         FROM evm_backfill_state
        WHERE COALESCE(status,'') IN ('partial','retry')
        ORDER BY last_try_at DESC LIMIT 12"""
    if "next_block" in cols else
    """SELECT network_id net, token_address tok, status st,
              from_block fb, to_block tb, COALESCE(transfers,0) tr,
              last_try_at up, last_error err
         FROM evm_backfill_state
        WHERE COALESCE(status,'') IN ('partial','retry')
        ORDER BY last_try_at DESC LIMIT 12"""
):
    d = dict(r)
    nb = d.get("nb")
    span = (d["tb"] - d["fb"]) if (d["tb"] and d["fb"]) else None
    done = (nb - d["fb"]) if (nb and d["fb"]) else None
    pct = f"{100.0*done/span:5.1f}%" if (span and done is not None and span > 0) else "  n/a"
    print(f"  net {d['net']:>5} {str(d['tok'])[:14]:<14} {d['st']:<8} "
          f"{pct}  transfers {d['tr']:>7}  up {str(d['up'])[:16]}"
          + (f"  ERR {str(d['err'])[:60]}" if d.get("err") else ""))

print("\n=== errors among partial/retry ===")
for r in con.execute(
    """SELECT COALESCE(substr(last_error,1,70),'(none)') e, COUNT(*) n
         FROM evm_backfill_state
        WHERE COALESCE(status,'') IN ('partial','retry')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""
):
    print(f"  {r['n']:>4}  {r['e']}")

print("\n=== replay keys still unfinished ===")
tabs = {r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
if "evm_replay_state" in tabs:
    for r in con.execute(
        """SELECT network_id net, COALESCE(status,'(null)') st, COUNT(*) n,
                  MAX(last_try_at) newest
             FROM evm_replay_state GROUP BY 1,2 ORDER BY 1,3 DESC"""
    ):
        print(f"  net {r['net']:>6} {r['st']:<12} {r['n']:>4}  newest {str(r['newest'])[:16]}")

print("\n=== task heartbeats and errors ===")
for r in con.execute(
    """SELECT key, value FROM meta
        WHERE key LIKE '%evm%' AND (key LIKE '%last%' OR key LIKE '%stat%'
              OR key LIKE '%error%' OR key LIKE '%rebuild%')
        ORDER BY key"""
):
    print(f"  {r['key']:38} {str(r['value'])[:110]}")
con.close()
