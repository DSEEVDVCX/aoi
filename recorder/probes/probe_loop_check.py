"""Loop check: control gate live? contract widening progressing? static hypothesis?"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys

import config

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect(
    f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro", uri=True, timeout=180
)
con.row_factory = sqlite3.Row
g = lambda k: (con.execute(
    "SELECT value FROM meta WHERE key=?", (k,)).fetchone() or [None])[0]

print("=== 1) control-arm age gate: is it live? ===")
print(f"  last_cycle_at              {g('last_cycle_at')}")
stats = ast.literal_eval(g("last_cycle_stats") or "{}")
print(f"  control_age_rejected in cycle stats: "
      f"{'YES' if 'control_age_rejected' in stats else 'NO — code not loaded'}"
      f"  (= {stats.get('control_age_rejected')})")
print(f"  control_age_rejected_total {g('control_age_rejected_total')}")
print(f"  control_added / age_rejected: {stats.get('control_added')} / "
      f"{stats.get('age_rejected')}")

print("\n=== 2) control vs signal arm age mix, windows opened since the gate ===")
CUT = "2026-08-22T22:10:00"
import datetime as dt
rows = con.execute(
    """SELECT w.is_control ic, w.first_seen_at fs, t.token_created_at cr
         FROM watch_windows w LEFT JOIN token_static t
           ON t.token_address=w.token_address AND t.network_id=w.network_id
        WHERE w.first_seen_at >= ?""", (CUT,)).fetchall()
for arm, label in ((0, "signal "), (1, "control")):
    sub = [r for r in rows if r["ic"] == arm]
    y = o = u = 0
    for r in sub:
        try:
            c = int(float(r["cr"]))
        except (TypeError, ValueError):
            u += 1
            continue
        a = (int(dt.datetime.fromisoformat(r["fs"]).timestamp()) - c) / 86400.0
        if a < 0:
            u += 1
        elif a < config.MIN_TOKEN_AGE_DAYS:
            y += 1
        else:
            o += 1
    n = len(sub) or 1
    print(f"  {label} windows {len(sub):>5}  >=2d {o:>5}  <2d {y:>4} "
          f"({100.0*y/n:.1f}%)  unknown {u}")

print("\n=== 3) EVM contract widening: rows per network ===")
for r in con.execute(
    """SELECT network_id net, COUNT(*) n, COUNT(DISTINCT token_address) toks,
              MAX(recorded_at) hi FROM evm_contract GROUP BY 1 ORDER BY 1"""):
    print(f"  net {r['net']:>6}  rows {r['n']:>6}  tokens {r['toks']:>4}  "
          f"newest {str(r['hi'])[:16]}")

print("\n=== 4) hypothesis: is the token_static family NULL because the static "
      "row is stamped LATER than entry_ts? ===")
FV = ("(SELECT CAST(value AS INTEGER) FROM meta "
      "WHERE key='current_feature_version')")
POP = (f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")
r = con.execute(f"""
    SELECT COUNT(*) missing,
           SUM(CASE WHEN s.token_address IS NOT NULL THEN 1 ELSE 0 END) has_row,
           SUM(CASE WHEN s.first_seen IS NOT NULL
                     AND s.first_seen > datetime(r.entry_ts,'unixepoch')
                    THEN 1 ELSE 0 END) row_is_later
      FROM training_rows r
      LEFT JOIN (SELECT token_address, network_id, MIN(recorded_at) first_seen
                   FROM token_static GROUP BY 1,2) s
        ON s.token_address=r.token_address AND s.network_id=r.network_id
     WHERE {POP} AND r.token_age_h IS NULL AND r.decimals IS NULL
""").fetchone()
print(f"  rows with token_static family NULL      : {r['missing']}")
print(f"    of which a token_static row DOES exist: {r['has_row']}")
print(f"    of which that row is stamped LATER    : {r['row_is_later']}")
con.close()
