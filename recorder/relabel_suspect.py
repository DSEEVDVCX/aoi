"""إعادة توسيم النتائج المسمومة بذيول مستحيلة (بلا شبكة).

التوسيم idempotent بالتصميم: الصفّ يُوسَم مرّة ولا يُراجَع. لكنّ الصفوف التي
حُسبت قبل إصلاح 2026-07-30 قد تحمل `max_gain` مبنيّاً على ذيل مستحيل من المنبع
(شوهد +3.78 مليار%). هذا السكربت يحذف تلك الصفوف **فقط** ليعيد الموسِّم حسابها
من الشموع نفسها بالأعلام الجديدة — لا فبركة ولا تعديل يدويّ لقيمة.

آمن: الليبل مشتقّ حتميّاً من `token_bars` (المصدر باقٍ)، فالحذف قابل للاسترجاع
بالكامل بدورة موسِّم واحدة. لا يمسّ صفّاً سليماً.

الاستعمال:
    py relabel_suspect.py --dry-run     # ماذا سيُحذف ويُعاد
    py relabel_suspect.py               # نفّذ
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

# صفّ مشبوه: نافذته تحوي شمعة معلَّمة، أو مقاييسه مستحيلة اقتصادياً
# (×100 = +10,000% داخل 48 ساعة على عملة ميم ليس مستحيلاً نظرياً، لكن مع
# ذيل معلَّم في النافذة يصير الاحتمال الراجح تشوّه المنبع لا سعراً).
_SUSPECT_SQL = """
SELECT o.kind, o.key, o.token_address, o.entry_ts, o.max_gain_48h,
       (SELECT COUNT(*) FROM token_bars b
         WHERE b.token_address = o.token_address
           AND b.network_id = o.network_id
           AND b.resolution = '5'
           AND b.ts > o.entry_ts
           AND b.ts <= o.entry_ts + ?
           AND (b.h_suspect = 1 OR b.l_suspect = 1 OR b.c_suspect = 1)) AS flagged
  FROM outcomes o
 WHERE o.status = 'ok'
"""


def find(db: RecorderDB) -> list[dict]:
    window = config.LABEL_WINDOW_HOURS * 3600
    rows = db._conn.execute(_SUSPECT_SQL, (window,)).fetchall()
    return [dict(r) for r in rows if r["flagged"]]


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        bad = find(db)
        print(f"صفوف نتائج نافذتها تحوي ذيلاً معلَّماً: {len(bad)}")
        for r in bad[:20]:
            print(f"  {r['kind']:<9} {r['token_address'][:14]}… "
                  f"max_gain_48h={r['max_gain_48h']} flagged_bars={r['flagged']}")
        if dry:
            print("(dry-run — بلا حذف)")
            return
        if not bad:
            print("لا شيء لإعادته.")
            return
        # نسخة نصّية للصفوف قبل حذفها: الليبل مشتقّ حتميّاً من الشموع فالحذف
        # قابل للاسترجاع، لكن الاحتفاظ بالقيم القديمة يسمح بمقارنة قبل/بعد.
        snap = os.path.join(HERE, "relabel_suspect_before.json")
        full = [
            dict(r) for r in db._conn.execute(
                "SELECT * FROM outcomes WHERE (kind, key) IN (%s)"
                % ", ".join(["(?, ?)"] * len(bad)),
                [v for r in bad for v in (r["kind"], r["key"])],
            ).fetchall()
        ]
        with open(snap, "w", encoding="utf-8") as fh:
            json.dump(full, fh, ensure_ascii=False, indent=1)
        print(f"نسخة القيم القديمة: {snap}")
        with db.batch():
            db._conn.executemany(
                "DELETE FROM outcomes WHERE kind=? AND key=?",
                [(r["kind"], r["key"]) for r in bad],
            )
        print(f"حُذفت {len(bad)} نتيجة — الموسِّم يعيد حسابها في دورته القادمة "
              f"(كل 15 دقيقة) أو شغّل: py run_labeler.py 1")
    finally:
        db.close()


if __name__ == "__main__":
    main()
