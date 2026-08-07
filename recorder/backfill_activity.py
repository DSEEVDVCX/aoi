"""استرجاع رجعيّ: أحداث tradingActivity التاريخية (مشيّاط lastId رجوعاً في الزمن).

الـ /feed الأماميّ «أحدث فقط» (خمسة معاملات ترقيم مُتجاهِلة — مُثبت حيّاً
2026-07-28)، بينما /feed/tradingActivity يصفّح رجوعاً بـ lastId. يجمع:
- أحداث multi_user_buy/sell **بنفس معرّفات /feed** (تطابق 32/42 حيّاً) —
  تمديد رجعيّ مباشر لمجموعة الإشارات.
- أحداث swap_buy/swap_sell فردية بـ usdAmount **لا يعرضها /feed إطلاقاً**
  (0/42 تطابق) — كون جديد: شراء/بيع كبير بعتبة نختارها نحن.

ما لا يُسترجع: رتبة المتصدّر وقت الحدث (لا تاريخ للصدارة — تُؤرشَف ساعيّاً منذ
2026-07-28 للمستقبل)، وإعجابات لحظة الحدث. لا نفبركها (FR-007).

نقطة الاستئناف في meta (activity_backfill_last_id/oldest_at): الانقطاعات
العابرة متكرّرة في fomo، فيُعاد كل صفحة حتى 3 مرّات ثمّ يُحفَظ الموضع ويخرج
بأمان — إعادة التشغيل تكمل من حيث توقّف، والإدراج OR IGNORE يجعل التكرار
بلا أثر.

الاستعمال:
    py backfill_activity.py --pages 20              # ~1000 حدث رجوعاً
    py backfill_activity.py --until 2026-06-01      # حتى بلوغ تاريخ
    py backfill_activity.py --pages 40 --dry-run    # قياس العمق بلا كتابة
    py backfill_activity.py --reset                 # مسح نقطة الاستئناف والبدء من جديد
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

_PACING = 1.5          # فاصل اللُّطف بين الصفحات (ثوانٍ)
_PAGE_LIMIT = 50       # حجم الصفحة (الأقصى المؤكَّد)
_RETRIES = 3           # محاولات لكل صفحة قبل حفظ الموضع والخروج
_RETRY_BACKOFF = (5, 15, 30)

META_LAST_ID = "activity_backfill_last_id"
META_OLDEST = "activity_backfill_oldest_at"


def _arg_value(name: str) -> str | None:
    if name in sys.argv:
        try:
            return sys.argv[sys.argv.index(name) + 1]
        except IndexError:
            return None
    return None


def _arg_int(name: str, default: int) -> int:
    v = _arg_value(name)
    if v is not None:
        try:
            return int(v)
        except ValueError:
            pass
    return default


async def _fetch_page(client: Any, last_id: str | None) -> Any:
    """صفحة واحدة مع إعادة محاولة عند العابر. يرفع بعد نفاد المحاولات."""
    from fomo_api.config import settings

    params: dict = {"limit": _PAGE_LIMIT, "threshold": 0}
    if last_id:
        params["lastId"] = last_id
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            return await client._get(settings.upstream_alerts_path, params)
        except Exception as exc:  # noqa: BLE001 — العابر يُعاد، القاتل يصعد
            last_exc = exc
            if attempt + 1 < _RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
    raise last_exc  # type: ignore[misc]


async def walk(
    client: Any,
    db: RecorderDB,
    *,
    max_pages: int,
    until: str | None = None,
    dry_run: bool = False,
    sleep=asyncio.sleep,
) -> dict:
    """يمشي رجوعاً في الزمن من نقطة الاستئناف (أو من الأحدث). يعيد إحصاءً.

    يتوقّف عند: صفحة فارغة (نهاية التاريخ)، بلوغ max_pages، أو بلوغ --until.
    """
    last_id = db.get_meta(META_LAST_ID)
    oldest_seen = db.get_meta(META_OLDEST)
    stats = {"pages": 0, "events": 0, "added": 0, "types": {}, "oldest": oldest_seen,
             "resumed_from": last_id, "dry_run": dry_run, "stopped": None}
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        fetched_at = utcnow_iso()
        raw = await _fetch_page(client, last_id)
        items, has_next = extract.activity_page(raw)
        rows = [extract.extract_activity_event(e, fetched_at) for e in items]
        rows = [r for r in rows if r and r["id"] not in seen]
        if not rows:
            stats["stopped"] = "empty_page"
            break
        seen.update(r["id"] for r in rows)

        added = len(rows) if dry_run else db.insert_activity_events(rows)
        stats["pages"] = page
        stats["events"] += len(rows)
        stats["added"] += added
        for r in rows:
            stats["types"][r["event_type"]] = stats["types"].get(r["event_type"], 0) + 1
        ts_min = min((r["ts"] for r in rows if r["ts"]), default=None)
        if ts_min and (oldest_seen is None or ts_min < oldest_seen):
            oldest_seen = ts_min
        stats["oldest"] = oldest_seen
        last_id = rows[-1]["id"]

        if not dry_run:
            db.set_meta(META_LAST_ID, last_id)
            if oldest_seen:
                db.set_meta(META_OLDEST, oldest_seen)

        if until and ts_min and ts_min <= until:
            stats["stopped"] = f"until:{until}"
            break
        if not has_next:
            stats["stopped"] = "has_next_page_false"
            break
        await sleep(_PACING)
    else:
        stats["stopped"] = "max_pages"
    return stats


def _load_client():
    from fomo_api.auth.credential_store import CredentialStore
    from fomo_api.clients.fomo_client import FomoClient

    creds = CredentialStore(config.credential_state_path()).load()
    if creds is None or not creds.access_token:
        raise RuntimeError("لا يوجد اعتماد صالح — شغّل خدمة الـ api أولاً.")
    return FomoClient(session_token=creds.access_token)


async def main() -> None:
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    if "--reset" in sys.argv:
        db._conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?)", (META_LAST_ID, META_OLDEST)
        )
        db._conn.commit()
        print("مُسحت نقطة الاستئناف — يبدأ من الأحدث.")
    client = _load_client()
    try:
        stats = await walk(
            client, db,
            max_pages=_arg_int("--pages", 20),
            until=_arg_value("--until"),
            dry_run="--dry-run" in sys.argv,
        )
    finally:
        await client.aclose()
    tag = " (dry-run — بلا كتابة)" if stats["dry_run"] else ""
    print(f"صفحات: {stats['pages']} · أحداث: {stats['events']} · أُدرج: {stats['added']}{tag}")
    print(f"أقدم حدث: {stats['oldest']} · توقّف: {stats['stopped']}")
    print("الأنواع:", dict(sorted(stats["types"].items(), key=lambda kv: -kv[1])))
    if not stats["dry_run"]:
        print(f"إجمالي activity_events الآن: {db.activity_count()}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
