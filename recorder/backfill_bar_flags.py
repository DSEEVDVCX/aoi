"""Recompute impossible-wick flags for stored bars (no network).

`h_suspect`/`l_suspect` have been computed at fetch time since 2026-07-30;
older bars were inserted without flags (all defaulting to 0). This script
recomputes them from the stored `o/h/l/c` — the raw data is enough, so not a
single call goes to fomo.

Why: fomo sometimes returns an impossible wick (observed h = 2,626,092 on a
bar whose close was 0.0219 = ×119 million, confirmed by a live re-fetch ⇒ a
persistent distortion upstream). Its effect: `max_gain` reached +3.78 billion%
in 4 rows, and the dashboard displayed +62,570,743,609%.

The raw values are **never touched**: we only flag the wick so it is excluded
from the high/low computation.

Usage:
    py backfill_bar_flags.py --dry-run     # report without writing
    py backfill_bar_flags.py               # flag the offending bars
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB  # noqa: E402
from extract import bar_context_flags  # noqa: E402  (the one reference defining distortion)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def scan(db: RecorderDB) -> list[tuple[int, int, int, str, str, str, int]]:
    """Returns the required changes: (h, l, c, token, network, resolution, ts).

    The verdict comes from the series, not the isolated bar (`bar_context_flags`)
    — the same function live fetching uses, so the two definitions cannot drift.
    We group by token/network/resolution because the neighbors are the reference.
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
        ratio = (config.DAILY_BAR_WICK_MAX_RATIO if k["resolution"] == "1D"
                 else config.BAR_WICK_MAX_RATIO)
        for b, (h_bad, l_bad, c_bad) in zip(
            series, bar_context_flags(series, max_ratio=ratio), strict=True
        ):
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
        print(f"bars scanned: {total}")
        print(f"impossible high: {h_bad} · impossible low: {l_bad} · distorted close: {c_bad} "
              f"· rows needing an update: {len(changes)}")
        if dry:
            for c in changes[:15]:
                print(f"  {c[3][:14]}… ts={c[6]} h={c[0]} l={c[1]} c={c[2]}")
            print("(dry-run — no writes)")
            return
        if changes:
            with db.batch():
                db._conn.executemany(
                    "UPDATE token_bars SET h_suspect=?, l_suspect=?, c_suspect=? "
                    "WHERE token_address=? AND network_id=? AND resolution=? AND ts=?",
                    changes,
                )
            print(f"flagged {len(changes)} bars. Raw values untouched.")
        else:
            print("no changes.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
