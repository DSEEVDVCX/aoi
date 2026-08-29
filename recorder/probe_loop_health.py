"""نبضاتُ خطوط الأنابيب ونموُّ الجداول — قراءة فقط."""
import os
import sqlite3
from datetime import datetime, timezone

import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc)


def age(value):
    try:
        return f"{(now - datetime.fromisoformat(str(value))).total_seconds()/60:7.1f}m"
    except Exception:
        return "      ?"


print("=== heartbeats (age) ===")
for r in con.execute(
    "SELECT key, value FROM meta WHERE key LIKE '%last_run_at' "
    "OR key LIKE '%last_ok_at' OR key='last_cycle_at' ORDER BY key"
):
    print(f"  {age(r['value'])}  {r['key']:<40} {r['value']}")

print("\n=== error / streak keys ===")
for r in con.execute(
    "SELECT key, substr(value,1,170) v FROM meta WHERE key LIKE 'last_error%' "
    "OR key LIKE '%streak%' ORDER BY key"
):
    print(f"  {r['key']:<40} {r['v']}")

print("\n=== rows written in the last 30 minutes ===")
for tbl in ("market_ticks", "signal_events", "chain_concentration",
            "evm_contract", "token_static", "token_holders"):
    try:
        n = con.execute(
            f"SELECT COUNT(*) c FROM {tbl} "
            "WHERE recorded_at >= datetime('now','-30 minutes')"
        ).fetchone()["c"]
        print(f"  {tbl:<22} {n:>8}")
    except sqlite3.Error as exc:
        print(f"  {tbl:<22} {exc}")

print("\n=== model population ===")
fv = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta "
    "WHERE key='current_feature_version'),'0') AS INTEGER) v"
).fetchone()["v"]
n = con.execute(
    "SELECT COUNT(*) c FROM training_rows WHERE kind='signal' AND is_live=1 "
    "AND asset_class='meme' AND status='ok' AND is_independent=1 "
    "AND feature_version=?", (fv,)
).fetchone()["c"]
print(f"  feature_version {fv}  model rows {n}")
con.close()
