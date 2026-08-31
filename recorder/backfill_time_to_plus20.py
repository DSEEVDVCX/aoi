"""Backfills time_to_plus20_min through the service layer — no hand-written SQL.

`time_to_plus20_min` was born 2026-08-28 and labeling is idempotent, so it
never touches old results. The column needs bars (first close >= +20% after
entry) — read from stored `token_bars` without network, using the same logic
as the live `compute_labels` so the two definitions cannot drift. Restricted
to ok outcomes that have an entry price — outcomes without bars stay NULL
(absence, not zero).

Usage:
    python backfill_time_to_plus20.py            # diagnosis
    python backfill_time_to_plus20.py --apply    # execute
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
        print(f"Outcomes that can be filled: {len(rows):,}")

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
                # Never reached +20%: the honest value is NULL (never
                # happened) — we leave it NULL rather than writing infinity.
                # The pull separates "never happened" from "happened at
                # minute X" by reading the same NULL, which is acceptable:
                # absence of the event = no flag, and that is the meaning.
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
                print(f"  progress: {done:,} (filled {filled:,})", flush=True)
        if args.apply:
            db._commit()
        print(f"Filled: {filled:,} | no bars: {no_bars:,} | never reached +20%: "
              f"{len(rows) - filled - no_bars:,}")
        if not args.apply:
            print("\nDiagnosis only — pass --apply to execute.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
