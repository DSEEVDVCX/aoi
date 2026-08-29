"""هل عملات «العمر المجهول» حديثةٌ فعلاً وقت أوّل استرجاع؟ — قياس مستقلّ.

المشكلة: 177 عملة لا صفَّ ثوابت لها إطلاقاً، فلا عمرَ عندنا ولا في أي خام
أرشفناه (خام الإشارة يحمل وقت الصفقة لا وقت العملة، وخام tokenDetails لا يحمل
تاريخ إنشاء أصلاً). فنقيس العمر من مصدر مستقلّ: **تاريخ الأسعار**.

المرساة **أوّل لحظة استرجعنا فيها بيانات العملة** لا الآن: نطلب شموعاً ساعيّة
تنتهي عند أوّل استرجاع وتبدأ قبله بخمسة أيّام. فإن وُجدت شمعة قبل أوّل استرجاع
بيومين أو أكثر ⇒ العملة كانت ≥ يومين **في تلك اللحظة**. سؤال «كم عمرها اليوم»
لا يُطرح إطلاقاً.

المعايرة أوّلاً على عملات عمرُها معروف: لو أعاد المنبع شموعاً عند حدّ النافذة
لعملة نعرف أنّها كانت قديمة، فغيابُ الشموع دليلُ حداثةٍ لا دليلُ نقصٍ في الأرشيف.

قراءة فقط من القاعدة (mode=ro)؛ نداءات getBarsNew وحدها تخرج للشبكة.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import config
import recorder

LOOKBACK_DAYS = 5
RESOLUTION = "60"          # ساعيّة: 5 أيّام = 120 شمعة، تحت سقف 900
YOUNG_DAYS = 2.0
PACING = 0.35


def ro_conn() -> sqlite3.Connection:
    uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


FIRST_RETRIEVAL = """
SELECT MIN(x.t) FROM (
  SELECT MIN(first_seen_at) t FROM watch_windows
   WHERE token_address=:tok AND network_id=:net
  UNION ALL SELECT MIN(recorded_at) FROM signal_events
   WHERE token_address=:tok AND network_id=:net
  UNION ALL SELECT MIN(recorded_at) FROM market_ticks
   WHERE token_address=:tok AND network_id=:net
) x
"""

UNKNOWN = """
SELECT w.token_address AS tok, w.network_id AS net
  FROM watch_windows w
  LEFT JOIN token_static t
    ON t.token_address=w.token_address AND t.network_id=w.network_id
 WHERE w.is_control=0 AND w.admission_source IS NULL AND t.token_address IS NULL
 GROUP BY 1,2
"""

KNOWN = """
SELECT w.token_address AS tok, w.network_id AS net,
       CAST(t.token_created_at AS INTEGER) AS created
  FROM watch_windows w
  JOIN token_static t
    ON t.token_address=w.token_address AND t.network_id=w.network_id
 WHERE w.is_control=0 AND w.admission_source IS NULL
   AND t.token_created_at IS NOT NULL AND t.token_created_at<>''
 GROUP BY 1,2
"""


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


async def first_candle(client, tok: str, net: str, anchor_ts: int) -> tuple[str, int | None]:
    """يعيد (الحالة، ختم أوّل شمعة) لنافذة تنتهي عند أوّل استرجاع."""
    from fomo_api.config import settings

    body = {
        "symbol": f"{tok}:{net}",
        "resolution": RESOLUTION,
        "from": anchor_ts - LOOKBACK_DAYS * 86400,
        "to": anchor_ts + 3600,
        "countBack": 900,
    }
    raw = await client._post(settings.upstream_get_bars_path, body)
    ro = (raw or {}).get("responseObject") or {}
    status = ro.get("s") or "no_data"
    ts = ro.get("t") if isinstance(ro.get("t"), list) else []
    ts = [int(v) for v in ts if isinstance(v, (int, float))]
    return status, (min(ts) if ts else None)


def classify(anchor_ts: int, first_ts: int | None, floor_ts: int) -> tuple[str, float | None]:
    """حكمٌ من عمر أوّل شمعة. `countBack` يعيد تاريخاً أعمق من النافذة أحياناً،
    فالعمر المقيس هو العمر الحقيقيّ لا حدَّ النافذة — والمعايرة تشهد (15.69 مقابل
    15.7 يوماً). فالحكم على العمر نفسه، ووسمُ `at_floor` للشفافيّة فقط."""
    if first_ts is None:
        return "no_history", None
    age_days = (anchor_ts - first_ts) / 86400.0
    return ("young_lt2d" if age_days < YOUNG_DAYS else "old_ge2d"), age_days


async def run(n_known: int, n_unknown: int) -> None:
    con = ro_conn()
    client = recorder._load_client()

    def anchor(tok: str, net: str) -> int | None:
        row = con.execute(FIRST_RETRIEVAL, {"tok": tok, "net": net}).fetchone()
        return epoch(row[0]) if row and row[0] else None

    async def measure(rows, label, expect_known=False):
        out = []
        print(f"\n=== {label} (n={len(rows)}) ===")
        for i, r in enumerate(rows):
            tok, net = r["tok"], r["net"]
            a = anchor(tok, net)
            if a is None:
                continue
            floor = a - LOOKBACK_DAYS * 86400
            try:
                status, first_ts = await first_candle(client, tok, net, a)
            except Exception as exc:  # noqa: BLE001
                print(f"  {tok[:12]:14} net={net:11} ERROR {type(exc).__name__}: {exc}")
                out.append({"tok": tok, "net": net, "verdict": "error"})
                await asyncio.sleep(PACING)
                continue
            verdict, age = classify(a, first_ts, floor)
            rec = {
                "tok": tok, "net": net, "anchor": a, "verdict": verdict,
                "bars_age_days": None if age is None else round(age, 2),
                "status": status,
            }
            if expect_known:
                true_age = (a - r["created"]) / 86400.0
                rec["true_age_days"] = round(true_age, 2)
                rec["true_bucket"] = "young_lt2d" if true_age < YOUNG_DAYS else "old_ge2d"
            out.append(rec)
            extra = f" true_age={rec.get('true_age_days')}" if expect_known else ""
            print(
                f"  {tok[:12]:14} net={net:11} anchor="
                f"{datetime.fromtimestamp(a, timezone.utc).strftime('%m-%d %H:%M')} "
                f"bars_age={rec['bars_age_days']!s:>7} -> {verdict}{extra}"
            )
            if i + 1 < len(rows):
                await asyncio.sleep(PACING)
        return out

    known = con.execute(KNOWN).fetchall()
    unknown = con.execute(UNKNOWN).fetchall()
    # عيّنة موزّعة على المدى الزمنيّ كلّه لا أوّلها (خطوة ثابتة، بلا عشوائيّة)
    def spread(rows, k):
        if k >= len(rows):
            return list(rows)
        step = len(rows) / k
        return [rows[int(i * step)] for i in range(k)]

    cal = await measure(spread(known, n_known), "CALIBRATION: age already known", True)
    tgt = await measure(spread(unknown, n_unknown), "TARGET: age unknown (no token_static)")

    print("\n=== calibration accuracy (bars verdict vs provider age) ===")
    scored = [r for r in cal if r.get("true_bucket") and r["verdict"] != "error"]
    agree = sum(1 for r in scored if r["verdict"] == r["true_bucket"])
    print(f"  agreement: {agree}/{len(scored)}")
    for v in ("young_lt2d", "old_ge2d", "no_history", "error"):
        for b in ("young_lt2d", "old_ge2d"):
            k = sum(1 for r in cal if r["verdict"] == v and r.get("true_bucket") == b)
            if k:
                mark = "  <-- MISMATCH" if v != b and v in ("young_lt2d", "old_ge2d") else ""
                print(f"    bars={v:12} provider={b:11} {k}{mark}")

    print("\n=== target verdicts (age unknown) ===")
    tot = len([r for r in tgt if r["verdict"] != "error"]) or 1
    for v in ("young_lt2d", "old_ge2d", "no_history", "error"):
        k = sum(1 for r in tgt if r["verdict"] == v)
        if k:
            print(f"  {v:12} {k:3}  ({100.0*k/tot:.1f}%)")

    # معدّل الحداثة الأساسيّ لكلّ العملات معروفة العمر — من القاعدة بلا شبكة،
    # والمرساة نفسها (أوّل استرجاع) لا الآن. هو المرجع الذي تُقاس عليه المجهولة.
    base_young = base_tot = 0
    for r in known:
        a = anchor(r["tok"], r["net"])
        if a is None or not r["created"]:
            continue
        base_tot += 1
        if (a - r["created"]) / 86400.0 < YOUNG_DAYS:
            base_young += 1
    print(
        f"\n=== base rate, ALL known-age coins (n={base_tot}) ===\n"
        f"  young_lt2d at first retrieval: {base_young} "
        f"({100.0*base_young/max(base_tot,1):.1f}%)"
    )

    with open("unknown_age_probe.json", "w", encoding="utf-8") as fh:
        json.dump({"calibration": cal, "target": tgt}, fh, indent=2)
    print("\nwrote unknown_age_probe.json")
    await client.close() if hasattr(client, "close") else None
    con.close()


if __name__ == "__main__":
    nk = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    nu = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    asyncio.run(run(nk, nu))
