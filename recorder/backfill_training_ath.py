"""ترقية صفوف التدريب القديمة إلى ميزة ATH اليومية من دون إعادة بناء كل الميزات.

الإصدار 3 غيّر عائلة ATH فقط. نستعمل ``dist_from_ath`` القديمة لاستعادة القمة
المحلية من آخر إغلاق، ثم ندمجها مع القمة اليومية المكتملة. هذا يطابق منطق
``features.price_history_features`` ويحفظ التقدم على دفعات.
"""
from __future__ import annotations

import bisect
import os
import sys
from collections import defaultdict
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import features  # noqa: E402
from db import RecorderDB  # noqa: E402


def daily_prefixes(db: RecorderDB) -> dict[tuple[str, str], tuple[list[int], list[float | None]]]:
    """أختمة إغلاق 1D وقمة تراكمية للشموع السليمة ذات التاريخ المكتمل."""
    grouped: dict[tuple[str, str], list[tuple[int, Any]]] = defaultdict(list)
    rows = db._conn.execute(
        """SELECT b.token_address,b.network_id,b.ts,b.h
             FROM token_bars b JOIN historical_bars_state s
               ON s.token_address=b.token_address
              AND s.network_id=b.network_id
              AND s.resolution=b.resolution
            WHERE b.resolution='1D' AND b.h_suspect=0 AND s.last_status='ok'
            ORDER BY b.token_address,b.network_id,b.ts"""
    )
    for row in rows:
        grouped[(row["token_address"], row["network_id"])].append(
            (int(row["ts"]) + 86400, row["h"])
        )
    out: dict[tuple[str, str], tuple[list[int], list[float | None]]] = {}
    for key, values in grouped.items():
        ends: list[int] = []
        peaks: list[float | None] = []
        peak: float | None = None
        for end_ts, high in values:
            if isinstance(high, (int, float)):
                peak = float(high) if peak is None else max(peak, float(high))
            ends.append(end_ts)
            peaks.append(peak)
        out[key] = (ends, peaks)
    return out


def intraday_prefixes(
    db: RecorderDB,
) -> dict[tuple[str, str], tuple[list[int], list[Any], list[float | None]]]:
    """إغلاقات 5د السليمة وقمتها التراكمية؛ نفس مرشحات مستخرج الميزات تماماً."""
    rows = db._conn.execute(
        """SELECT token_address,network_id,ts,c,h,h_suspect
             FROM token_bars
            WHERE resolution=? AND c_suspect=0
            ORDER BY token_address,network_id,ts""",
        (config.BARS_RESOLUTION,),
    )
    out: dict[tuple[str, str], tuple[list[int], list[Any], list[float | None]]] = {}
    current: tuple[str, str] | None = None
    ends: list[int] = []
    closes: list[Any] = []
    peaks: list[float | None] = []
    peak: float | None = None
    for row in rows:
        key = (row["token_address"], row["network_id"])
        if current is not None and key != current:
            out[current] = (ends, closes, peaks)
            ends, closes, peaks, peak = [], [], [], None
        current = key
        high = row["h"]
        if isinstance(high, (int, float)) and not row["h_suspect"]:
            peak = float(high) if peak is None else max(peak, float(high))
        ends.append(int(row["ts"]) + 300)
        closes.append(row["c"])
        peaks.append(peak)
    if current is not None:
        out[current] = (ends, closes, peaks)
    return out


def upgrade(db: RecorderDB, batch_size: int = 1000, force: bool = False) -> dict[str, int]:
    histories = daily_prefixes(db)
    intraday = intraday_prefixes(db)
    stats = {"updated": 0, "complete": 0, "without_daily_before_t0": 0}
    last_rowid = 0
    while True:
        version_filter = "" if force else "AND feature_version < ?"
        params = ((last_rowid, features.FEATURE_VERSION, batch_size) if not force
                  else (last_rowid, batch_size))
        rows = db._conn.execute(
            f"""SELECT rowid AS rid,kind,key,token_address,network_id,entry_ts
                  FROM training_rows WHERE rowid > ? {version_filter}
                 ORDER BY rowid LIMIT ?""",
            params,
        ).fetchall()
        if not rows:
            break
        updates = []
        for row in rows:
            token, network, t0 = row["token_address"], row["network_id"], int(row["entry_ts"])
            key = (token, network)
            local = intraday.get(key)
            local_ath = None
            last_c = None
            if local:
                local_ends, closes, local_peaks = local
                local_index = bisect.bisect_right(local_ends, t0) - 1
                if local_index >= 0:
                    last_c = closes[local_index]
                    local_ath = local_peaks[local_index]
            history = histories.get((token, network))
            complete = 0
            history_days = None
            daily_ath = None
            if history and local_ath is not None:
                ends, peaks = history
                index = bisect.bisect_right(ends, t0) - 1
                if index >= 0 and peaks[index] is not None:
                    complete = 1
                    daily_ath = peaks[index]
                    history_days = (t0 - (ends[0] - 86400)) / 86400

            peaks_to_merge = [p for p in (local_ath, daily_ath if complete else None)
                              if isinstance(p, (int, float))]
            new_dist = None
            if peaks_to_merge and isinstance(last_c, (int, float)) and last_c:
                new_dist = float(last_c) / max(peaks_to_merge) - 1.0
            if complete:
                stats["complete"] += 1
            else:
                stats["without_daily_before_t0"] += 1
            updates.append((new_dist, complete, history_days, features.FEATURE_VERSION,
                            row["kind"], row["key"]))
        with db.batch():
            db._conn.executemany(
                """UPDATE training_rows
                      SET dist_from_ath=?,ath_history_complete=?,ath_history_days=?,
                          feature_version=?
                    WHERE kind=? AND key=?""",
                updates,
            )
        stats["updated"] += len(updates)
        last_rowid = int(rows[-1]["rid"])
        print(f"progress updated={stats['updated']} complete={stats['complete']}", flush=True)
    return stats


def main() -> None:
    batch_size = 1000
    if "--batch-size" in sys.argv:
        try:
            batch_size = max(1, int(sys.argv[sys.argv.index("--batch-size") + 1]))
        except (IndexError, ValueError):
            raise SystemExit("--batch-size يحتاج عدداً صحيحاً") from None
    force = "--force" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        print(upgrade(db, batch_size, force=force))
    finally:
        db.close()


if __name__ == "__main__":
    main()
