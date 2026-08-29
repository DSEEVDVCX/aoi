"""ما تنفقه Monad (143) في الطبقة الحيّة مقابل ما تعطيه — قراءة فقط.

`EVM_REPLAY_NETWORKS` أسقطت 143 في 08-19 لصفر عملة نشطة، لكنها باقية في
`EVM_NETWORKS` (طبقة التطبيق/التعبئة/اللقطة). هذا يقيس هل ما زالت تنفق.
"""
import os
import sqlite3

import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row


def q(sql, args=()):
    return con.execute(sql, args).fetchall()


print("=== watchlist per EVM network ===")
for r in q(
    "SELECT network_id, SUM(active=1) AS act, COUNT(*) AS total "
    "FROM watchlist WHERE network_id IN ('4663','8453','143','56') "
    "GROUP BY network_id ORDER BY act DESC"
):
    print(f"  net {r['network_id']:>6}  active {r['act']:>4}  ever {r['total']:>5}")

print("\n=== evm_block_cursor ===")
for r in q(
    "SELECT * FROM evm_block_cursor ORDER BY network_id"
):
    keys = r.keys()
    err = (r["last_error"] if "last_error" in keys else None) or ""
    upd = next((r[k] for k in ("updated_at", "recorded_at", "last_run_at")
                if k in keys), None)
    print(f"  net {r['network_id']:>6}  block {r['last_block']}  at {upd}")
    if err:
        print(f"      last_error: {err[:220]}")

print("\n=== rows written per network (ledger + snapshots) ===")
for table, col in (("evm_transfer_ledger", "network_id"),
                   ("chain_concentration", "network_id")):
    try:
        rows = q(
            f"SELECT {col} AS net, COUNT(*) AS n, MAX(recorded_at) AS newest "
            f"FROM {table} WHERE {col} IN ('4663','8453','143','56') "
            f"GROUP BY {col} ORDER BY n DESC"
        )
    except sqlite3.Error as exc:
        print(f"  {table}: {exc}")
        continue
    print(f"  {table}:")
    for r in rows:
        print(f"    net {r['net']:>6}  rows {r['n']:>8}  newest {r['newest']}")

print("\n=== meta: monad / 143 / rate-limit traces ===")
for r in q(
    "SELECT key, substr(value,1,300) AS v FROM meta "
    "WHERE key LIKE '%evm%' AND (key LIKE '%error%' OR key LIKE '%stats%') "
    "ORDER BY key"
):
    print(f"  {r['key']}\n      {r['v']}")

print("\n=== model rows on 143 ===")
fv = q("SELECT CAST(COALESCE((SELECT value FROM meta "
       "WHERE key='current_feature_version'),'0') AS INTEGER) AS v")[0]["v"]
for r in q(
    "SELECT network_id, COUNT(*) AS n FROM training_rows "
    "WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
    "AND is_independent=1 AND feature_version=? "
    "AND network_id IN ('4663','8453','143','56','1399811149') "
    "GROUP BY network_id ORDER BY n DESC", (fv,)
):
    print(f"  net {r['network_id']:>12}  model rows {r['n']:>6}")

con.close()
