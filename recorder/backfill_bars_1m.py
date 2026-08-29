"""أرشفة شموع 1 دقيقة لنوافذ القرار — لكل عملات المجموعة الرئيسية.

المالك (2026-08-28): «نفّذ لكل العملات لا الجديدة فقط — أعمل مع AI آخر
لاستخراج أفضل طريقة تدريب ولا أريد انتظار الضابطة». الأرشفة تفتح للتحليل
المحيط الأولي الخارجي: دقة الدقيقة لكل من `time_to_plus20` والمحاكاة،
والدقائق الـ15 الأولى بعد الإشارة (نافذة إعلان الانفجار المبكر).

القياس الحي قبل البناء (2026-08-28):
- `resolution=1` يعمل على /proxy/getBarsNew (200 شمعة/نداء حي).
- **الأرشيف متاح**: عملة عمرها 34 يومًا أعادت 500 شمعة من نافذة إشارتها.
- 704 عملات المجموعة الرئيسية × ~6 نداءات (500/نداء × نافذة 48س) =
  4,224 نداءً للكامل.

التصميم: قابل للاستئناف (حالة historical_bars_state بمفتاح resolution='1'،
حفظ الموقع في cursor_to)، بلا SQL يدوي (كتابة عبر db.insert_bars)،
وتوقيت خلفي محترم للحد (نداء كل PACING ثانية). العملات التي مصدرها لا
يعيدها (no_data) لا تُعاد كل تشغيل.

الاستعمال:
    python backfill_bars_1m.py              # تشخيص
    python backfill_bars_1m.py --apply      # تنفيذ (استئناف تلقائي)
    python backfill_bars_1m.py --apply --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
from db import RecorderDB  # noqa: E402

RES = "1"
PAGE = 500                    # مقيس: أقصى ما يعيده المصدر للدقيقة الواحدة
PACING = 1.0                  # ثانية بين النداءات — حذر مع رصيد fomo
WINDOW_H = 48                 # نافذة القرار


async def _load_client():
    """زبين fomo حي: توكن الخادم المحلي من القرص (نفس مسار المسجّل)."""
    import recorder

    return recorder._build_client(recorder._load_access_token())


def _targets(db: RecorderDB) -> list[dict]:
    """كل عملات المجموعة الرئيسية: أقدم قرار لكل عملة — نافذته هي المدى.

    الضابطة والنشطة كذلك: المالك طلب «كل العملات» — نضمّن المراقَبات
    (kind=watch) فنافذتها نفسها قرار قابل للتحليل، والإشارات وحدها كذلك.
    """
    rows = db._conn.execute(
        """SELECT o.token_address, o.network_id, MIN(o.entry_ts) AS entry_ts
             FROM outcomes o
             JOIN training_rows t ON t.kind = o.kind AND t.key = o.key
            WHERE t.is_live = 1 AND t.is_independent = 1
              AND t.asset_class = 'meme' AND t.status = 'ok'
              AND o.status = 'ok'
            GROUP BY o.token_address, o.network_id
            ORDER BY entry_ts""",
    ).fetchall()
    return [dict(r) for r in rows]


def _state(db: RecorderDB, token: str, net: str) -> dict | None:
    row = db._conn.execute(
        """SELECT cursor_to, oldest_ts, last_status, candles, calls, attempts
             FROM historical_bars_state
            WHERE token_address=? AND network_id=? AND resolution=?""",
        (token, net, RES),
    ).fetchone()
    return dict(row) if row else None


def _save_state(db: RecorderDB, token: str, net: str, *, cursor_to: int | None,
                oldest_ts: int | None, status: str, candles: int, calls: int) -> None:
    db._conn.execute(
        """INSERT INTO historical_bars_state(
               token_address, network_id, resolution, cursor_to, oldest_ts,
               last_status, candles, calls, attempts, updated_at)
           VALUES(?,?,?,?,?,?,?,?,
                  COALESCE((SELECT attempts FROM historical_bars_state
                            WHERE token_address=? AND network_id=? AND resolution=?),0)+1,?)
           ON CONFLICT(token_address, network_id, resolution) DO UPDATE SET
               cursor_to=excluded.cursor_to, oldest_ts=excluded.oldest_ts,
               last_status=excluded.last_status, candles=excluded.candles,
               calls=excluded.calls, attempts=excluded.attempts,
               updated_at=excluded.updated_at""",
        (token, net, RES, cursor_to, oldest_ts, status, candles, calls,
         token, net, RES, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
    )
    db._commit()


def _extract_bars(raw: dict) -> list[dict]:
    """مغلّف getBarsNew → صفوف شموع (نفس شكل مستخرج المسجّل)."""
    ro = raw.get("responseObject") if isinstance(raw, dict) else None
    if not isinstance(ro, dict):
        return []
    ts = ro.get("t") or []
    out = []
    for i, t in enumerate(ts):
        try:
            out.append({
                "token_address": None,  # يملؤه المستدعي
                "network_id": None,
                "resolution": RES,
                "ts": int(t),
                "o": ro["o"][i], "h": ro["h"][i],
                "l": ro["l"][i], "c": ro["c"][i],
                "v": ro["v"][i] if ro.get("v") else 0,
                "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
                "fetched_at": None,
            })
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


async def archive_token(client, db: RecorderDB, t: dict, stats: dict) -> None:
    """أرشفة نافذة 48س (بدء决策 - سياق) لعملة واحدة — استئناف من cursor_to."""
    token, net = t["token_address"], str(t["network_id"])
    entry = int(t["entry_ts"])
    start = entry - 3600                      # ساعة سياق قبل القرار
    end = entry + WINDOW_H * 3600
    st = _state(db, token, net)
    if st and st["last_status"] == "ok":
        stats["already_done"] += 1
        return
    cursor = st["cursor_to"] if st and st["cursor_to"] else end
    candles = st["candles"] if st else 0
    calls = st["calls"] if st else 0
    from recorder import _fetch_bars_raw

    while cursor > start:
        try:
            raw = await _fetch_bars_raw(client, token, net, start, cursor,
                                        resolution=RES)
        except Exception:  # noqa: BLE001 — عطب عابر: تمهّل ثم أعد المحاولة
            # قياس 2026-08-28: موجة 404 عابرة أوقفت 456 عملة في دقيقة
            # (عملاتها تعمل عند إعادة المحاولة) — فالصواب تمهّل قصير مرتين
            # قبل الاستسلام، والحالة partial تجعل الاستئناف اللاحق آمنًا.
            retried = False
            for wait_s in (5, 15):
                await asyncio.sleep(wait_s)
                try:
                    raw = await _fetch_bars_raw(client, token, net, start,
                                                cursor, resolution=RES)
                    retried = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not retried:
                _save_state(db, token, net, cursor_to=cursor,
                            oldest_ts=None, status="partial",
                            candles=candles, calls=calls)
                stats["errors"] += 1
                return
        calls += 1
        bars = _extract_bars(raw)
        if not bars:
            # لا بيانات في هذا المقطع: إما بداية السلسلة أو فجوة.
            _save_state(db, token, net, cursor_to=start, oldest_ts=cursor,
                        status="ok", candles=candles, calls=calls)
            stats["done"] += 1
            return
        for b in bars:
            b["token_address"] = token
            b["network_id"] = net
            b["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                            time.gmtime())
        wrote = db.insert_bars(bars)
        candles += len(bars)
        oldest = min(int(b["ts"]) for b in bars)
        cursor = min(cursor, oldest) - 60     # ننزل تحت أقدم شمعة بمقدار دقيقة
        stats["rows"] += wrote
        _save_state(db, token, net, cursor_to=cursor, oldest_ts=oldest,
                    status="partial", candles=candles, calls=calls)
        await asyncio.sleep(PACING)
    _save_state(db, token, net, cursor_to=start, oldest_ts=start,
                status="ok", candles=candles, calls=calls)
    stats["done"] += 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        targets = _targets(db)
        if args.limit:
            targets = targets[: args.limit]
        done_prior = sum(
            1 for t in targets
            if (s := _state(db, t["token_address"], str(t["network_id"])))
            and s["last_status"] == "ok"
        )
        print(f"أهداف المجموعة الرئيسية: {len(targets):,} "
              f"(منجز سابقًا: {done_prior:,})")
        if not args.apply:
            print("\nتشخيص فقط — مرّر --apply للتنفيذ.")
            return 0

        stats = {"done": 0, "rows": 0, "errors": 0, "already_done": 0}
        client = await _load_client()
        try:
            for i, t in enumerate(targets):
                await archive_token(client, db, t, stats)
                if (i + 1) % 10 == 0:
                    print(f"  progress: {i+1}/{len(targets)} · "
                          f"rows={stats['rows']:,} · done={stats['done']} · "
                          f"err={stats['errors']}", flush=True)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        print(f"اكتملت الجولة: done={stats['done']} · rows={stats['rows']:,} · "
              f"err={stats['errors']} · skip={stats['already_done']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
