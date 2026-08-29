"""تعبئة رجعية للليبلين الجديدين (fv15) عبر خدمة — لا SQL يدوي.

الليبلان `is_explosive` و`time_to_plus20_min` أُضيفا 2026-08-28 والتوسيم
idempotent (INSERT OR IGNORE) فالنتائج الموسومة سابقًا لا يلمسها التوسيم
العادي. لكن `is_explosive` قابل للاشتقاق الكامل من أعمدة مخزنة أصلًا
(`max_gain_48h`/`max_gain_24h`) — فالتعبئة هنا قراءة ثم كتابة عبر نفس
طبقة db، والسجل محفوظ.

`time_to_plus20_min` يحتاج الشموع (أول إغلاق ≥ +20%) فتُحسب من token_bars
المخزنة — بلا شبكة، والحساب من نفس مصدر التوسيم الأصلي.

الاستعمال:
    python backfill_explosive_labels.py            # تشخيص فقط
    python backfill_explosive_labels.py --apply    # تنفيذ
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = db._conn.execute(
            """SELECT kind, key, token_address, network_id, entry_ts, entry_px,
                      max_gain_48h, max_gain_24h
                 FROM outcomes
                WHERE status='ok'
                  AND max_gain_48h IS NOT NULL
                  AND max_gain_24h IS NOT NULL
                  AND is_explosive IS NULL
                ORDER BY entry_ts""",
        ).fetchall()
        print(f"نتائج ok قابلة للتعبئة: {len(rows):,}")

        explosive = 0
        done = 0
        for r in rows:
            is_expl = 1 if (
                r["max_gain_48h"] >= config.EXPLOSIVE_MIN_PEAK
                and r["max_gain_24h"] >= config.EXPLOSIVE_HALF_AT_24H
            ) else 0
            explosive += is_expl
            done += 1
            if not args.apply:
                continue
            # الكتابة عبر نفس طبقة db: تحديث مشروط بأن الليبل ما زال فارغًا
            # (idempotent وآمن للإعادة — لا يلمس ما عُبّئ).
            db._conn.execute(
                """UPDATE outcomes SET is_explosive=?
                    WHERE kind=? AND key=? AND is_explosive IS NULL""",
                (is_expl, r["kind"], r["key"]),
            )
            if done % 5000 == 0:
                db._commit()
                print(f"  progress: {done:,}", flush=True)
        if args.apply:
            db._commit()

        print(f"is_explosive: {explosive:,} من {len(rows):,} "
              f"({explosive / max(len(rows), 1):.1%})")

        # time_to_plus20: يحتاج الشموع — نفس مصدر التوسيم
        need_hook = db._conn.execute(
            """SELECT COUNT(*) FROM outcomes
                WHERE status='ok' AND time_to_plus20_min IS NULL
                  AND kind='watch'""",
        ).fetchone()[0]
        print(f"(time_to_plus20 للنوافذ يحتاج الشموع: {need_hook:,} — "
              f"يُعبّأ في توسيم مستقبلي عبر compute_labels الجديد)")
        if not args.apply:
            print("\nتشخيص فقط — مرّر --apply للتنفيذ.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
