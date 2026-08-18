"""ترحيل لمرّة واحدة: ضغط أعمدة raw_json الموجودة (نصّ → zlib BLOB).

سبب الترحيل: المسجّل كان يكتب الخام نصّاً، فبلغت `recorder.db` نحو 720 MB خلال
20 ساعة (جدول `snapshots` وحده 577 MB) بنموّ ~1.3 GB يومياً. الضغط بلا خسارة
(نسبة ~4.7x) يخفض ذلك إلى ~280 MB يومياً دون فقد بايت واحد من الأرشيف.

خصائص أمان الترحيل:
- **بلا خسارة ومتحقَّق منه**: كل صفّ يُفكّ ضغطه ويُقارَن بالأصل قبل الكتابة؛
  أي عدم تطابق يُجهض الترحيل كلّه.
- **قابل للاستئناف (idempotent)**: يختار الصفوف بـ `typeof(raw_json)='text'`
  فقط، فإعادة التشغيل بعد انقطاع تُكمل من حيث توقّفت ولا تلمس المضغوط.
- **آمن مع المسجّل**: يرفض العمل إن كانت المهمّة المجدولة `FomoRecorder`
  تعمل — الكاتب يجب أن يكون متوقّفاً.

الاستعمال:
    Stop-ScheduledTask -TaskName FomoRecorder
    py migrate_compress.py            # ترحيل + VACUUM
    py migrate_compress.py --dry-run  # تقدير المكسب فقط، بلا كتابة
    Start-ScheduledTask -TaskName FomoRecorder
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import decode_raw, encode_raw  # noqa: E402

# الجداول التي تحمل raw_json، ومفتاح كل منها للتحديث الدقيق.
_TARGETS = (
    ("snapshots", "id"),
    ("market_ticks", "rowid"),
    ("signal_events", "rowid"),
    ("token_static", "rowid"),
)
_BATCH = 200   # صفوف لكل معاملة — يحدّ من ذاكرة العملية على اللقطات الكبيرة
_SAMPLE = 50   # صفوف العيّنة في وضع المعاينة

# طرفيّة Windows قد تكون cp1256 فتعجز عن العربية وعن الأسهم — نفرض UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - يعتمد على الطرفيّة
        pass


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


def _pending_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE typeof(raw_json)='text'"
    ).fetchone()[0]


def estimate_table(conn: sqlite3.Connection, table: str, pending: int) -> tuple[int, int]:
    """معاينة: يقيس عيّنة ويستقرئ على كامل الجدول. يعيد (بايت قبل، بايت بعد)."""
    sample = conn.execute(
        f"SELECT raw_json FROM {table} WHERE typeof(raw_json)='text' "
        f"LIMIT {_SAMPLE}"
    ).fetchall()
    if not sample:
        return 0, 0
    before = sum(len(t.encode("utf-8")) for (t,) in sample)
    after = sum(len(encode_raw(t)) for (t,) in sample)
    scale = pending / len(sample)
    return int(before * scale), int(after * scale)


def migrate_table(conn: sqlite3.Connection, table: str, key: str) -> tuple[int, int, int]:
    """يضغط صفوف جدول واحد. يعيد (عدد الصفوف، بايت قبل، بايت بعد)."""
    rows_done = before = after = 0
    while True:
        rows = conn.execute(
            f"SELECT {key} AS k, raw_json FROM {table} "
            f"WHERE typeof(raw_json)='text' LIMIT {_BATCH}"
        ).fetchall()
        if not rows:
            break
        updates = []
        for k, text in rows:
            blob = encode_raw(text)
            # تحقّق بلا خسارة: لا نكتب إلّا إذا عاد الأصل حرفياً.
            if decode_raw(blob) != decode_raw(text):
                raise SystemExit(
                    f"أُجهض الترحيل: عدم تطابق بعد الضغط في {table} {key}={k}. لم تُكتب هذه الدفعة."
                )
            before += len(text.encode("utf-8"))
            after += len(blob)
            updates.append((blob, k))
        conn.executemany(f"UPDATE {table} SET raw_json=? WHERE {key}=?", updates)
        conn.commit()
        rows_done += len(updates)
        print(f"  {table}: {rows_done} صفّاً ({before/1e6:.0f} -> {after/1e6:.0f} MB)", flush=True)
    return rows_done, before, after


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    db_path = config.DB_PATH
    if not os.path.isfile(db_path):
        raise SystemExit(f"لا توجد قاعدة بيانات في {db_path}")

    if not dry_run and _recorder_is_running():
        raise SystemExit(
            "مهمّة FomoRecorder تعمل الآن. أوقفها أوّلاً:\n"
            "  Stop-ScheduledTask -TaskName FomoRecorder"
        )

    size_before = os.path.getsize(db_path)
    print(f"قاعدة البيانات: {db_path} ({size_before/1e6:.0f} MB)")
    print("وضع المعاينة (بلا كتابة)\n" if dry_run else "")

    conn = sqlite3.connect(db_path)
    t0 = time.perf_counter()
    total_rows = total_before = total_after = 0
    try:
        for table, key in _TARGETS:
            pending = _pending_count(conn, table)
            if pending == 0:
                print(f"{table}: مضغوط بالفعل - تخطٍّ")
                continue
            print(f"{table}: {pending} صفّاً غير مضغوط")
            if dry_run:
                before, after = estimate_table(conn, table, pending)
                rows = pending
            else:
                rows, before, after = migrate_table(conn, table, key)
            total_rows += rows
            total_before += before
            total_after += after

        if total_before:
            label = "تقدير" if dry_run else "الخام"
            print(
                f"\n{label}: {total_before/1e6:.0f} MB -> {total_after/1e6:.0f} MB "
                f"({total_before/max(total_after,1):.1f}x) عبر {total_rows} صفّاً "
                f"في {time.perf_counter()-t0:.0f}s"
            )
        else:
            print("\nلا شيء للترحيل.")

        if not dry_run and total_rows:
            print("VACUUM لاستعادة المساحة (قد يستغرق دقائق)...", flush=True)
            conn.execute("VACUUM")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('raw_encoding','zlib') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.commit()
    finally:
        conn.close()

    if not dry_run:
        size_after = os.path.getsize(db_path)
        print(
            f"حجم الملفّ: {size_before/1e6:.0f} MB -> {size_after/1e6:.0f} MB "
            f"(وُفِّر {(size_before-size_after)/1e6:.0f} MB)"
        )


if __name__ == "__main__":
    main()
