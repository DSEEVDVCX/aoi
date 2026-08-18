"""ترحيل رجعيّ: ملء حقول حجم الصفقة من `raw_json` المؤرشف.

سبب الترحيل: `extract_signal_event` لم تكن تستخرج `currentSizeUsd` ولا
`inHumanAmount` ولا أخواتها، فكانت صفقة بـ 1,000$ وأخرى بـ 141,000$ متطابقتين
تماماً في قاعدة البيانات — رغم أنّ وسيط ما يسمّيه fomo «شراء كبير» هو 3,448$
فقط. أثمن حقل تمييزيّ في الإشارة كان مُهدراً.

لا بيانات ضاعت: الحدث الخام محفوظ كاملاً في `raw_json`، فنُعيد الاستخراج منه.

خصائص أمان الترحيل:
- **قابل للاستئناف**: يختار الصفوف التي `size_usd IS NULL` فقط.
- **بلا شبكة**: يقرأ الأرشيف المحلّي وحده.
- **آمن مع المسجّل**: يكتب أعمدة مشتقّة فقط ولا يمسّ `raw_json`؛ ومع ذلك
  يرفض العمل والمسجّل يكتب، تفادياً لقفل الكتابة.

الاستعمال:
    Stop-ScheduledTask -TaskName FomoRecorder
    py backfill_sizes.py --dry-run
    py backfill_sizes.py
    Start-ScheduledTask -TaskName FomoRecorder
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import decode_raw  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - يعتمد على الطرفيّة
        pass

_FIELDS = (
    "size_usd", "in_amount", "in_token_address",
    "out_amount", "token_amount", "realized_pnl_usd",
)
_BATCH = 500


def _recorder_is_running() -> bool:
    """True إن كانت عملية المسجّل حيّة.

    نفحص **العملية** لا حالة المهمّة المجدولة: `Stop-ScheduledTask` تُرجع
    الحالة إلى Ready بينما تبقى عملية pythonw حيّة لحظات (أو أكثر)، فكان
    الفحص القديم يمرّ والمسجّل ما يزال يكتب.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*run_recorder.py*' }).ProcessId"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — تعذّر الفحص — القرار للمشغّل
        return False  # لا نستطيع الفحص — نترك القرار للمشغّل
    return bool(out.stdout.strip())


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if not dry_run and _recorder_is_running():
        raise SystemExit(
            "مهمّة FomoRecorder تعمل الآن. أوقفها أوّلاً:\n"
            "  Stop-ScheduledTask -TaskName FomoRecorder"
        )

    # نفتح عبر RecorderDB أوّلاً حتى يُطبَّق ترحيل الأعمدة، فيكون السكربت
    # مكتفياً بذاته ولا يشترط تشغيل المسجّل قبله.
    from db import RecorderDB

    RecorderDB(config.DB_PATH, config.SCHEMA_PATH).close()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(signal_events)")}
    missing = [f for f in _FIELDS if f not in cols]
    if missing:
        raise SystemExit(f"تعذّر ترحيل الأعمدة: {', '.join(missing)}")

    total = conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE size_usd IS NULL"
    ).fetchone()[0]
    print(f"صفوف بلا حقول حجم: {total}")
    if not total:
        print("لا شيء للترحيل.")
        return

    done = filled = 0
    cursor = 0
    while True:
        # التقدّم بمؤشّر rowid **لا** بشرط `size_usd IS NULL` وحده: أحداث
        # multi_user_buy لا تحمل حقول حجم أصلاً، فتبقى NULL بعد المعالجة
        # ويعيد الاستعلامُ نفسَ الصفوف إلى الأبد (حلقة لا نهائية حقيقية،
        # وقعت فعلاً على 21 صفّاً).
        rows = conn.execute(
            "SELECT rowid AS rid, id, raw_json FROM signal_events "
            "WHERE rowid > ? AND size_usd IS NULL ORDER BY rowid LIMIT ?",
            (cursor, _BATCH),
        ).fetchall()
        if not rows:
            break
        cursor = rows[-1]["rid"]
        updates = []
        for r in rows:
            try:
                event = decode_raw(r["raw_json"])
            except Exception:  # noqa: BLE001 — صفّ خام تالف يُتخطّى ولا يُسقط الترحيل
                continue  # صفّ خام تالف — يُتخطّى، لا يُسقط الترحيل
            # نعيد الاستخراج بالدالة نفسها التي يستعملها المسجّل: مصدر واحد
            # للحقيقة، فلا ينحرف المُرحَّل عن المُسجَّل حديثاً.
            new = extract.extract_signal_event(event, "backfill", None)
            if new is None:
                continue
            vals = [new.get(f) for f in _FIELDS]
            if any(v is not None for v in vals):
                filled += 1
            updates.append((*vals, r["id"]))
        if dry_run:
            done += len(rows)
            print(f"  [معاينة] {done}/{total} فُحصت، {filled} لها قيَم")
            continue
        conn.executemany(
            f"UPDATE signal_events SET {', '.join(f'{f}=?' for f in _FIELDS)} WHERE id=?",
            updates,
        )
        conn.commit()
        done += len(rows)
        print(f"  {done}/{total} صفّاً ({filled} منها لها قيَم حجم)", flush=True)

    if dry_run:
        print("\nوضع المعاينة — لم يُكتب شيء.")
    else:
        got = conn.execute(
            "SELECT COUNT(*) FROM signal_events WHERE size_usd IS NOT NULL"
        ).fetchone()[0]
        print(f"\nاكتمل. صفوف لها size_usd الآن: {got}")
        row = conn.execute(
            "SELECT MIN(size_usd) lo, MAX(size_usd) hi, COUNT(*) n "
            "FROM signal_events WHERE size_usd IS NOT NULL"
        ).fetchone()
        if row["n"]:
            print(f"مدى حجم المركز: ${row['lo']:,.0f} .. ${row['hi']:,.0f}")
    conn.close()


if __name__ == "__main__":
    main()
