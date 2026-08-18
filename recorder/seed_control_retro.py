"""استرجاع رجعيّ: بناء مجموعة ضابطة تغطّي **نفس فترة الإشارات**.

لماذا: المجموعة الضابطة الحيّة تدخل من لحظة تفعيلها، بينما عملات الإشارة دخلت
على مدى يومين — فأي مقارنة بينهما تخلط «أثر الإشارة» بـ«أثر لحظة الدخول».
هذا السكربت يلغي الخلط: يختار عملات من **أرشيف اللقطات نفسه**، ويمنح كلّاً منها
ختم دخول من لحظة ظهورها الفعليّ في الأرشيف.

ممكن أصلاً لأنّ `snapshots` تحفظ قوائم trending/verified كاملةً لكل دورة، ولأنّ
`getBarsNew` تعيد تاريخاً سعرياً يمتدّ أشهراً — فسعر أي عملة في الماضي متاح.

شروط صلاحية المقارنة (نفس شروط الضابطة الحيّة):
- **لم يُشَر إليها قطّ** (لا في `signal_events` ولا في `watchlist`).
- **اختيار عشوائيّ** من كون العملات لا حسب ترتيبها في القائمة.
- **ختم الدخول من لقطة عشوائية** ظهرت فيها — لا أوّل ظهور (وإلّا انحاز
  الاختيار إلى العملات القديمة) ولا الآن (وإلّا عاد الخلط الزمني).
- **بلا نظر إلى الأداء**: لا يُستشار سعر ولا نتيجة في الاختيار إطلاقاً.

تُوسم `source='control_retro'` لتمييزها عن الضابطة الحيّة (`'control'`).
الشموع تصلها تلقائياً عبر دورة المسجّل العادية.

الاستعمال:
    Stop-ScheduledTask -TaskName FomoRecorder
    py seed_control_retro.py --dry-run
    py seed_control_retro.py --count 60
    Start-ScheduledTask -TaskName FomoRecorder
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, decode_raw  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def _arg(name: str, default: int) -> int:
    if name in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def _recorder_is_running() -> bool:
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*run_recorder.py*' }).ProcessId"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — تعذّر الفحص — القرار للمشغّل
        return False
    return bool(out.stdout.strip())


def build_appearance_map(db: RecorderDB) -> dict[tuple[str, str], list[str]]:
    """(عنوان، شبكة) → أختام اللقطات التي ظهرت فيها العملة."""
    seen: dict[tuple[str, str], list[str]] = {}
    rows = db._conn.execute(
        "SELECT recorded_at, raw_json FROM snapshots "
        "WHERE source IN ('trending','verified') ORDER BY id"
    )
    for row in rows:
        try:
            items = extract.unwrap_token_list(decode_raw(row["raw_json"]))
        except Exception:  # noqa: BLE001 — لقطة تالفة تُتخطّى ولا تُسقط البناء
            continue  # لقطة تالفة تُتخطّى ولا تُسقط البناء
        for it in items:
            addr = extract._token_address(it)
            if not addr:
                continue
            tok = it.get("token") if isinstance(it.get("token"), dict) else {}
            net = str(tok.get("networkId") or it.get("networkId") or "")
            seen.setdefault((addr, net), []).append(row["recorded_at"])
    return seen


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    want = _arg("--count", 60)
    seed = _arg("--seed", 20260727)

    if not dry_run and _recorder_is_running():
        raise SystemExit(
            "مهمّة FomoRecorder تعمل الآن. أوقفها أوّلاً:\n"
            "  Stop-ScheduledTask -TaskName FomoRecorder"
        )

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        print("قراءة أرشيف اللقطات…", flush=True)
        appearances = build_appearance_map(db)
        print(f"  عملات ظهرت في الأرشيف: {len(appearances)}")

        signalled = db.signalled_tokens()
        known = db.known_tokens()
        pool = sorted(
            key for key in appearances
            if key[0] not in signalled and key not in known
        )
        print(f"  منها بلا إشارة ولم تدخل المراقبة: {len(pool)}")
        if not pool:
            print("لا مرشّحين.")
            return

        rng = random.Random(seed)
        picks = rng.sample(pool, min(want, len(pool)))
        now = datetime.now(tz=datetime.now().astimezone().tzinfo)

        added = 0
        for addr, net in picks:
            stamps = appearances[(addr, net)]
            # ختم دخول من لقطة عشوائية ظهرت فيها — لا الأولى ولا الآن
            entry = rng.choice(stamps)
            if dry_run:
                added += 1
                continue
            if db.admit_control(
                addr, net, config.CONTROL_WATCH_HOURS, entry, source="control_retro"
            ):
                added += 1
        if not dry_run:
            db._conn.commit()
            # نافذة 48 ساعة قد تكون انتهت لبعضها؛ نطبّق نفس قاعدة المسجّل
            expired = db.deactivate_expired(now.isoformat())
            print(f"  انتهت نافذتها فوراً (أرشيف أقدم من 48س): {expired}")

        print(f"\n{'[معاينة] ' if dry_run else ''}عملات ضابطة رجعية: {added}")
        if not dry_run:
            print(f"  ضابطة نشطة إجمالاً: {db.active_watch_count(is_control=1)}")
            print(f"  مُشار إليها نشطة:   {db.active_watch_count(is_control=0)}")
            print("\nالشموع ستصلها تلقائياً عبر دورة المسجّل (الأقدم سحباً أوّلاً).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
