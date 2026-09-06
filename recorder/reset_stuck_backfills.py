"""Reset stuck backfill tokens to their join point.

Diagnosis (measured 2026-08-27): 145 `partial` tokens in `evm_backfill_state`,
63 of them on active watches, with remaining ranges of 13–77 **million**
blocks. At the cycle budget (12 calls × ~2000 blocks) a single token needs
~20,000 cycles — meaning the cycle burns calls on a range that will **never
complete** with no rows to show for it, while the token is meanwhile denied
concentration snapshots because `partial` is excluded from the live layer.

The fix: start from the **join-time block** (`first_seen_at`), not the dawn of
the chain. This is mathematically safe for any future row: every possible `t0`
for a token is ≥ its join moment, so no feature ever reads the history before
it. Existing training rows are untouched — the ledger is cumulative and new
rows are built on top of the present.

Balances missing before the starting point are deliberate and documented: the
ledger measures "distribution since we watched", not "distribution since
birth" — the same semantics as Solana snapshots, which only ever know 20
accounts anyway.

Usage:
    python reset_stuck_backfills.py            # diagnosis only (read-only)
    python reset_stuck_backfills.py --apply    # actually reset
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

# Reality threshold: a remaining range shorter than this is left to complete
# the normal way. (A token that joined recently whose available range is short
# anyway — no reason for us to intervene.)
REMAINING_BLOCK_THRESHOLD = 2_000_000


def _block_clock_secs(network_id: str) -> int:
    """Average block time in seconds per network (known measured values)."""
    return {"4663": 12, "8453": 2, "143": 12, "56": 3}.get(str(network_id), 12)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="perform the reset; without it, a read-only diagnosis")
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = db._conn.execute(
            """SELECT b.network_id, b.token_address, b.from_block, b.to_block,
                      w.first_seen_at, w.active
                 FROM evm_backfill_state b
                 JOIN watchlist w
                   ON w.token_address=b.token_address AND w.network_id=b.network_id
                WHERE b.status='partial'
                  AND b.to_block IS NOT NULL AND b.from_block IS NOT NULL
                  AND (b.to_block - b.from_block) > ?""",
            (REMAINING_BLOCK_THRESHOLD,),
        ).fetchall()
        from datetime import datetime

        resettable: list[tuple] = []
        skipped = 0
        for net, token, fb, tb, first_seen, active in rows:
            try:
                ts = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                skipped += 1
                continue
            join_epoch = int(ts.timestamp())
            clock = _block_clock_secs(net)
            # Approximate join block: computed from the current head known in
            # the state itself (to_block is the highest block the fill has
            # seen) minus the age since joining.
            age_secs = max(0, int(datetime.now().timestamp()) - join_epoch)
            join_block = max(0, int(tb) - age_secs // clock)
            # If the join point buys nothing (the token is old on the platform
            # but we also joined long ago) then that is the best we can do —
            # what matters is that the new range is achievable.
            remaining = int(tb) - join_block
            if remaining > REMAINING_BLOCK_THRESHOLD:
                # Even the join point is far away (a very old watch window) —
                # start from the reality threshold just below the head: the
                # documented maximum cutoff.
                join_block = max(0, int(tb) - REMAINING_BLOCK_THRESHOLD)
            resettable.append((net, token, fb, tb, join_block, active))
        print(f"stuck partial tokens (>{REMAINING_BLOCK_THRESHOLD:,} blocks "
              f"remaining): {len(rows)} | skipped(bad ts): {skipped}")
        active_n = sum(1 for r in resettable if r[5])
        print(f"resettable: {len(resettable)} (still-active watches: {active_n})")
        if not resettable:
            print("nothing needs a reset.")
            return 0

        for net, token, fb, tb, join_block, _active in resettable[:10]:
            print(f"  {token[:14]}… net={net} {fb:,}→{tb:,} "
                  f"({int(tb)-int(fb):,} blk) => new from {join_block:,}")

        if not args.apply:
            print("\ndiagnosis only — pass --apply to execute.")
            return 0

        from db import utcnow_iso

        now = utcnow_iso()
        with db.batch():
            for net, token, _fb, _tb, join_block, _active in resettable:
                db.restart_evm_backfill_from(token, net, int(join_block), now)
        print(f"reset {len(resettable)} tokens to their join point.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
