"""إعادة حساب أعلام الذيول المستحيلة للشموع المخزّنة (بلا شبكة).

`h_suspect`/`l_suspect` تُحسب عند السحب منذ 2026-07-30، والشموع الأقدم أُدرجت
بلا أعلام (كلّها 0 افتراضياً). هذا السكربت يعيد حسابها من `o/h/l/c` المخزّنة —
الخام كافٍ، فلا نداء واحد على fomo.

السبب: fomo تعيد أحياناً ذيلاً مستحيلاً (شوهد h = 2,626,092 لشمعة إغلاقها
0.0219 = ×119 مليون، ومؤكَّد بإعادة سحب حيّة ⇒ تشوّه دائم في المنبع). أثره:
`max_gain` وصل +3.78 مليار% في 4 صفوف، واللوحة عرضت +62,570,743,609%.

القيم الخام **لا تُلمس**: نعلّم الذيل فقط ليُستبعد من حساب القمّة/القاع.

الاستعمال:
    py backfill_bar_flags.py --dry-run     # تقرير بلا كتابة
    py backfill_bar_flags.py               # وسم الشموع المخالفة
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB  # noqa: E402
from extract import bar_context_flags  # noqa: E402  (المرجع الوحيد لتعريف التشوّه)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def scan(db: RecorderDB) -> list[tuple[int, int, int, str, str, str, int]]:
    """يعيد التغييرات المطلوبة: (h, l, c, token, network, resolution, ts).

    الحكم بالسلسلة لا بالشمعة المعزولة (`bar_context_flags`) — نفس الدالة التي
    يستعملها السحب الحيّ، فلا ينجرف تعريفان. نجمّع بالعملة/الشبكة/الدقّة لأنّ
    الجار هو المرجع.
    """
    series_keys = db._conn.execute(
        "SELECT DISTINCT token_address, network_id, resolution FROM token_bars"
    ).fetchall()
    out: list[tuple[int, int, int, str, str, str, int]] = []
    for k in series_keys:
        rows = db._conn.execute(
            "SELECT ts, o, h, l, c, h_suspect, l_suspect, c_suspect FROM token_bars "
            "WHERE token_address=? AND network_id=? AND resolution=? ORDER BY ts",
            (k["token_address"], k["network_id"], k["resolution"]),
        ).fetchall()
        series = [dict(r) for r in rows]
        for b, (h_bad, l_bad, c_bad) in zip(series, bar_context_flags(series)):
            if (h_bad, l_bad, c_bad) != (b["h_suspect"], b["l_suspect"], b["c_suspect"]):
                out.append((h_bad, l_bad, c_bad, k["token_address"],
                            k["network_id"], k["resolution"], b["ts"]))
    return out


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        total = db._conn.execute("SELECT COUNT(*) FROM token_bars").fetchone()[0]
        changes = scan(db)
        h_bad = sum(1 for c in changes if c[0])
        l_bad = sum(1 for c in changes if c[1])
        c_bad = sum(1 for c in changes if c[2])
        print(f"شموع مفحوصة: {total}")
        print(f"قمّة مستحيلة: {h_bad} · قاع مستحيل: {l_bad} · إغلاق مشوّه: {c_bad} "
              f"· صفوف تحتاج تحديثاً: {len(changes)}")
        if dry:
            for c in changes[:15]:
                print(f"  {c[3][:14]}… ts={c[6]} h={c[0]} l={c[1]} c={c[2]}")
            print("(dry-run — بلا كتابة)")
            return
        if changes:
            with db.batch():
                db._conn.executemany(
                    "UPDATE token_bars SET h_suspect=?, l_suspect=?, c_suspect=? "
                    "WHERE token_address=? AND network_id=? AND resolution=? AND ts=?",
                    changes,
                )
            print(f"وُسمت {len(changes)} شمعة. القيم الخام لم تُلمس.")
        else:
            print("لا تغيير.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
