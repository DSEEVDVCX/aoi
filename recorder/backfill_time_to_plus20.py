"""تعبئة رجعية لـ time_to_plus20_min عبر خدمة — لا SQL يدوي.

`time_to_plus20_min` وُلد 2026-08-28 والتوسيم idempotent فلا يلمس النتائج
القديمة. العمود يحتاج الشموع (أول إغلاق ≥ +20% بعد الدخول) — تُقرأ من
`token_bars` المخزنة بلا شبكة، بنفس منطق `compute_labels` الحيّ فلا ينجرف
تعريفان. يُقصر على النتائج ok ذات سعر دخول — والنتائج بلا شموع تبقى NULL
(غياب لا صفر).

الاستعمال:
    python backfill_time_to_plus20.py            # تشخيص
    python backfill_time_to_plus20.py --apply    # تنفيذ
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
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = db._conn.execute(
            """SELECT kind, key, token_address, network_id, entry_ts, entry_px
                 FROM outcomes
                WHERE status='ok' AND entry_px IS NOT NULL
                  AND time_to_plus20_min IS NULL
                ORDER BY entry_ts""",
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        print(f"نتائج قابلة للتعبئة: {len(rows):,}")

        filled = 0
        no_bars = 0
        done = 0
        for r in rows:
            done += 1
            bars = db.bars_for(
                r["token_address"], str(r["network_id"] or ""),
                int(r["entry_ts"]), int(r["entry_ts"]) + config.LABEL_WINDOW_HOURS * 3600,
            )
            if not bars:
                no_bars += 1
                continue
            threshold = float(r["entry_px"]) * (1.0 + config.PLUS20_THRESHOLD)
            hit = next(
                (b["ts"] for b in bars
                 if b["c"] is not None and float(b["c"]) >= threshold
                 and not b.get("c_suspect")),
                None,
            )
            if hit is None:
                # لم تبلغ +20% أبدًا: قيمة صادقة هي NULL (لم يحدث) —
                # نتركها NULL ولا نكتب ما لا نهاية. السحب يفصل "لم يحدث"
                # عن "حدث في الدقيقة X" بقراءة NULL نفسها، وهو مقبول:
                # غياب الحدث = لا سنارة، وهذا هو المعنى.
                continue
            filled += 1
            if args.apply:
                db._conn.execute(
                    """UPDATE outcomes SET time_to_plus20_min=?
                        WHERE kind=? AND key=? AND time_to_plus20_min IS NULL""",
                    ((hit - int(r["entry_ts"])) / 60, r["kind"], r["key"]),
                )
            if done % 20000 == 0:
                if args.apply:
                    db._commit()
                print(f"  progress: {done:,} (سُدّ {filled:,})", flush=True)
        if args.apply:
            db._commit()
        print(f"سُدّ: {filled:,} | بلا شموع: {no_bars:,} | لم تبلغ +20%: "
              f"{len(rows) - filled - no_bars:,}")
        if not args.apply:
            print("\nتشخيص فقط — مرّر --apply للتنفيذ.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
