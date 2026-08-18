"""تصنيف الأصول: عملة ميم أم أصل كبير أم مستقرّة أم سهم/سلعة مرمّزة (بلا شبكة).

fomo منصّة **متعدّدة الأصول** لا سوق ميمات — مقيس 2026-07-30 في أرشيفنا:
BTC ($1.27T) · ETH · SOL (103 إشارة) · USDT · XRP · BNB · HYPE · ذهب PAXG ·
أسهم مرمّزة: AAPL ($339) · SNDK ($1013) · MU ($958) · MSTR · HOOD · INTC ·
META · DRAM · NET. خلط هذه بالميمات يفسد التدريب: سهم آبل لا يسلك سلوك عملة
عمرها ساعتان، والقيمة السوقية **لا تكشفها** (AAPL بـ$1.36M فقط لأنّ المرمَّز
جزء ضئيل من السهم) — السعر هو المميّز.

يجمع كل مشاهدات كل عملة من `signal_events` و`activity_events` و`market_ticks`
ثمّ يحكم بـ`extract.classify_asset` ويكتب `token_class`. مشتقّ بالكامل ⇒ يُعاد
بناؤه في أيّ وقت، ولا يمسّ صفّاً خامّاً.

الاستعمال:
    py classify_tokens.py --dry-run     # التوزيع وأمثلة غير الميمات
    py classify_tokens.py               # كتابة/تحديث token_class
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402
from extract import classify_asset  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

# مشاهدات السعر/القيمة/الاسم من كل المصادر. `symbol` من token_static أوّلاً
# (الأدقّ) ثمّ ticker الأحداث.
_OBS_SQL = """
WITH obs AS (
    SELECT token_address a, network_id n, ticker sym, price_usd px, market_cap mc
      FROM signal_events WHERE price_usd IS NOT NULL OR market_cap IS NOT NULL
    UNION ALL
    SELECT token_address, network_id, ticker, price_usd, market_cap
      FROM activity_events WHERE price_usd IS NOT NULL OR market_cap IS NOT NULL
    UNION ALL
    SELECT token_address, network_id, NULL, price_usd, market_cap
      FROM market_ticks WHERE price_usd IS NOT NULL OR market_cap IS NOT NULL
)
SELECT a AS token_address, n AS network_id,
       COUNT(*) AS observations,
       MIN(CASE WHEN px > 0 THEN px END) AS price_min,
       MAX(px) AS price_max,
       MAX(mc) AS market_cap_max,
       (SELECT s.symbol FROM token_static s
         WHERE s.token_address = obs.a AND s.network_id = obs.n LIMIT 1) AS static_sym,
       (SELECT o2.sym FROM obs o2
         WHERE o2.a = obs.a AND o2.n = obs.n AND o2.sym IS NOT NULL LIMIT 1) AS obs_sym
  FROM obs GROUP BY a, n
"""


def build(db: RecorderDB) -> list[dict]:
    now = utcnow_iso()
    out = []
    for r in db._conn.execute(_OBS_SQL).fetchall():
        sym = r["static_sym"] or r["obs_sym"]
        cls, reason = classify_asset(
            sym, r["price_min"], r["price_max"], r["market_cap_max"]
        )
        out.append({
            "token_address": r["token_address"], "network_id": r["network_id"],
            "asset_class": cls, "reason": reason, "symbol": sym,
            "price_min": r["price_min"], "price_max": r["price_max"],
            "market_cap_max": r["market_cap_max"],
            "observations": r["observations"], "classified_at": now,
        })
    return out


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = build(db)
        dist: dict[str, int] = {}
        for r in rows:
            dist[r["asset_class"]] = dist.get(r["asset_class"], 0) + 1
        print(f"عملات مصنَّفة: {len(rows)}")
        print("التوزيع:", dict(sorted(dist.items(), key=lambda kv: -kv[1])))
        print("\nغير الميمية (كلّها):")
        for r in sorted(
            (x for x in rows if x["asset_class"] != "meme"),
            key=lambda x: -(x["price_max"] or 0),
        ):
            print(f"  {r['asset_class']:<7} {r['symbol'] or '?'!s:<8} "
                  f"px_max={r['price_max']} mc_max={r['market_cap_max']} "
                  f"obs={r['observations']} — {r['reason']}")
        if dry:
            print("\n(dry-run — بلا كتابة)")
            return
        cols = ("token_address", "network_id", "asset_class", "reason", "symbol",
                "price_min", "price_max", "market_cap_max", "observations",
                "classified_at")
        with db.batch():
            db._conn.executemany(
                f"INSERT OR REPLACE INTO token_class({', '.join(cols)}) "
                f"VALUES({', '.join('?' * len(cols))})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        print(f"\nكُتبت {len(rows)} صفّاً في token_class.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
