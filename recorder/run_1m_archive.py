"""مشغّل مهمة أرشفة شموع 1m — منفصل عن باني صفوف التدريب.

الفصل مقصود: كلاهما كاتب SQLite؛ عملية واحدة/حلقة asyncio تمنع database locked.
المهمة تعمل كل ساعة، وتستأنف الحالات partial وتلتقط العملات الجديدة الناضجة.
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main() -> None:
    import backfill_bars_1m

    raise SystemExit(asyncio.run(backfill_bars_1m.main()))


if __name__ == "__main__":
    main()
