"""إعادة ضبط عملات backfill العالقة إلى نقطة انضمامها.

التشخيص (مقيس 2026-08-27): 145 عملة `partial` في `evm_backfill_state`، 63 منها
على مراقبات نشطة، ومداها المتبقي 13–77 **مليون** كتلة. بميزانية الدورة
(12 نداءً × ~2000 كتلة) تحتاج العملة الواحدة ~20,000 دورة — أي أنّ الدورة
تستهلك نداءات **لن تكتمل أبدًا** بلا صفوف مقابلها، والعملة في الأثناء محرومة
من لقطات التركيز لأنّ `partial` مستثناة من التطبيق الحي.

الحل: البدء من **كتلة لحظة الانضمام** (`first_seen_at`) لا من فجر السلسلة.
هذا آمن رياضيّاً لأي صفٍّ مستقبليّ: كل `t0` ممكن للعملة ≥ لحظة انضمامها،
فالتاريخ قبلها لا تدخله أي ميزة. صفوف التدريب القائمة لا تُمسّ أصلاً —
قاعدة الدفتر تراكميّة والصفوف الجديدة تُبنى فوق الحاضر.

الأرصدة الناقصة قبل نقطة البداية مقصودة وموثّقة: الدفتر يقيس «التوزيع منذ
راقبنا» لا «التوزيع منذ الولادة» — نفس دلالة لقطات سولانا التي لا تعرف
غير 20 حساباً أصلاً.

الاستعمال:
    python reset_stuck_backfills.py            # تشخيص فقط (قراءة)
    python reset_stuck_backfills.py --apply    # إعادة الضبط فعلياً
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
from db import RecorderDB  # noqa: E402

# حدّ الواقعية: مدى أقصر من هذا يُترك يكتمل بالطريقة العادية.
# (عملة انضمت حديثاً ومداها المتاح أصلاً قصير — لا داعي لتدخّلنا.)
REMAINING_BLOCK_THRESHOLD = 2_000_000


def _block_clock_secs(network_id: str) -> int:
    """متوسط زمن الكتلة بالثواني حسب الشبكة (قيم مقيسة معروفة)."""
    return {"4663": 12, "8453": 2, "143": 12, "56": 3}.get(str(network_id), 12)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="نفّذ إعادة الضبط؛ بدونه تشخيص قراءة فقط")
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = db._conn.execute(
            """SELECT b.network_id, b.token_address, b.from_block, b.to_block,
                      w.first_seen_at, w.active
                 FROM evm_backfill_state b
                 JOIN watchlist w
                   ON w.token_address=b.token_address AND w.network_id=b.network_id
                WHERE b.status='partial'
                  AND b.to_block IS NOT NULL AND b.from_block IS NOT NULL
                  AND (b.to_block - b.from_block) > ?""",
            (REMAINING_BLOCK_THRESHOLD,),
        ).fetchall()
        from datetime import datetime

        resettable: list[tuple] = []
        skipped = 0
        for net, token, fb, tb, first_seen, active in rows:
            try:
                ts = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                skipped += 1
                continue
            join_epoch = int(ts.timestamp())
            clock = _block_clock_secs(net)
            # كتلة تقريبية للانضمام: نحسبها من الرأس الحالي المعلوم في الحالة
            # نفسها (to_block هو أعلى كتلة رأتها التعبئة) ناقص عمر الانضمام.
            age_secs = max(0, int(datetime.now().timestamp()) - join_epoch)
            join_block = max(0, int(tb) - age_secs // clock)
            # لو نقطة الانضمام لا توفّر شيئاً (العملة قديمة على المنصة لكن
            # انضمتنا قديمة أيضاً) فهذا هو أفضل ما نستطيع — المهم أنّ المدى
            # الجديد قابل للإنجاز.
            remaining = int(tb) - join_block
            if remaining > REMAINING_BLOCK_THRESHOLD:
                # حتى نقطة الانضمام بعيدة (نافذة مراقبة قديمة جداً) — نبدأ
                # من حدّ الواقعية قبل الرأس مباشرة: القصّ الأقصى الموثّق.
                join_block = max(0, int(tb) - REMAINING_BLOCK_THRESHOLD)
            resettable.append((net, token, fb, tb, join_block, active))
        print(f"stuck partial tokens (>{REMAINING_BLOCK_THRESHOLD:,} blocks "
              f"remaining): {len(rows)} | skipped(bad ts): {skipped}")
        active_n = sum(1 for r in resettable if r[5])
        print(f"resettable: {len(resettable)} (still-active watches: {active_n})")
        if not resettable:
            print("لا شيء يستدعي إعادة ضبط.")
            return 0

        for net, token, fb, tb, join_block, _active in resettable[:10]:
            print(f"  {token[:14]}… net={net} {fb:,}→{tb:,} "
                  f"({int(tb)-int(fb):,} blk) => new from {join_block:,}")

        if not args.apply:
            print("\nتشخيص فقط — مرّر --apply للتنفيذ.")
            return 0

        from db import utcnow_iso

        now = utcnow_iso()
        with db.batch():
            for net, token, _fb, _tb, join_block, _active in resettable:
                db.restart_evm_backfill_from(token, net, int(join_block), now)
        print(f"تمت إعادة ضبط {len(resettable)} عملة إلى نقطة انضمامها.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
