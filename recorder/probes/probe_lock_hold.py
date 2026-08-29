"""من يحتجز قفلَ الكتابة، وكم؟ — لا يكتب شيئاً.

`BEGIN IMMEDIATE` يطلب قفل الكتابة ثمّ `ROLLBACK` فوراً: صفرُ بايت مكتوب،
ومعه زمنُ الانتظار الحقيقيّ الذي يواجهه `chain_layer` كلّ دورة.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

import config

path = config.DB_PATH
print(f"db: {path}")
con = sqlite3.connect(path, timeout=1, isolation_level=None)
print("journal_mode:", con.execute("PRAGMA journal_mode").fetchone()[0])
print("busy_timeout:", con.execute("PRAGMA busy_timeout").fetchone()[0])
print("wal_autocheckpoint:", con.execute("PRAGMA wal_autocheckpoint").fetchone()[0])

print("\n=== محاولاتُ أخذ قفل الكتابة (12 محاولة، مهلة 1ث) ===")
held = 0
for i in range(12):
    t0 = time.monotonic()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        print(f"  {i:>2}  free   {(time.monotonic()-t0)*1000:7.0f}ms")
    except sqlite3.OperationalError as exc:
        held += 1
        print(f"  {i:>2}  BUSY   {(time.monotonic()-t0)*1000:7.0f}ms  {exc}")
    time.sleep(2.0)
print(f"\n  محتجَز في {held} من 12 عيّنة على مدى ~36 ثانية")

now = datetime.now(timezone.utc)
ro = sqlite3.connect(f"file:{path.replace(os.sep,'/')}?mode=ro", uri=True, timeout=60)
ro.row_factory = sqlite3.Row
print("\n=== نبضاتُ السلسلة ===")
for key in ("chain_last_run_at", "chain_last_ok_at", "evm_replay_last_run_at",
            "last_cycle_at", "labeler_last_run_at", "build_rows_last_run_at"):
    r = ro.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not r:
        print(f"  {key:<26} —")
        continue
    try:
        mins = (now - datetime.fromisoformat(str(r["value"]))).total_seconds() / 60
        print(f"  {key:<26} {mins:8.1f}m   {r['value']}")
    except Exception:
        print(f"  {key:<26} ?         {r['value']}")
ro.close()
con.close()
