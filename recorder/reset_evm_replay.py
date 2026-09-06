"""Return stuck replay tokens to the queue — by selection, not wholesale
erasure.

`repair_evm_ledger.py --apply` is the tool for a defect in the ledger
itself: it wipes whole EVM networks (balances, cursors, every concentration
row) and requires the full network set in one operation, so it drops a
healthy network's snapshots to free another network's tokens. This tool is
for the opposite case: the ledger and the code are sound, but the token's
state was written by a **separate** path — so it stayed `error`/`negative`
with a call count behind it, and the final status keeps it out of the new
path.

Deletion is the return: `evm_replay_state` is fully derived from chain
logs, so deleting its row puts the token back in the queue from genesis.
The default is display only; nothing is written without `--apply`.

And a third case, more hidden than either: an **orphan** — replay
concentration rows with no state row. No state matches, so `--status`
cannot catch them. They are shown on every run **and never deleted**: their
source is a deliberate path (`db.admit` clears the state of a token
reactivated after a gap during which the ledger went unwatched, so it is
walked from genesis), and their rows are exactly what only a full walk
reproduces. Whoever wants to judge their correctness has a tool that asks
the chain itself: `audit_evm_ledger.py`. Orphanhood is news about the
state, not a verdict on the rows.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import repair_evm_ledger  # noqa: E402 — the network validator lives in one place, not a copy
from db import RecorderDB  # noqa: E402

# `done` is not a stuck status but finished work, and deleting it drops
# snapshots that only a full walk can reproduce. To redo finished work use
# `run_evm_replay.py --redo`; to wipe a network for a ledger defect use
# `repair_evm_ledger.py`.
REFUSED_STATUSES = ("done",)


def candidates(
    db: RecorderDB, networks: tuple[str, ...], statuses: tuple[str, ...],
    tokens: tuple[str, ...] = (),
) -> list[dict]:
    """Matching state rows, heaviest by call count first (the ones most
    worth inspecting before deletion)."""
    where = [
        f"network_id IN ({', '.join('?' for _ in networks)})",
        f"COALESCE(status, '') IN ({', '.join('?' for _ in statuses)})",
    ]
    params: list[str] = [*networks, *statuses]
    if tokens:
        where.append(f"token_address IN ({', '.join('?' for _ in tokens)})")
        params.extend(token.lower() for token in tokens)
    rows = db._conn.execute(
        f"""SELECT s.token_address, s.network_id, s.status, s.calls,
                   s.snapshots, s.transfers, s.from_block, s.last_error,
                   (SELECT COUNT(*) FROM chain_concentration c
                     WHERE c.token_address = s.token_address
                       AND c.network_id = s.network_id
                       AND c.is_replay = 1) AS rows_written
              FROM evm_replay_state s
             WHERE {' AND '.join(where)}
             ORDER BY COALESCE(s.calls, 0) DESC""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def orphans(
    db: RecorderDB, networks: tuple[str, ...], tokens: tuple[str, ...] = (),
) -> list[dict]:
    """Replay rows with no state row — written by a path that left no
    record to select by.

    `candidates` selects from `evm_replay_state`, so a token whose rows
    were written and whose state row then vanished is invisible to this
    whole tool: no state matches, so nothing does. What it lacks is a state
    row, not row deletion: its walk was lost, but its snapshots are
    measured exactly as written — `audit_evm_ledger.py` checked them on
    4663 and 8453 and their coverage came out 100%.

    That is why they are shown, never deleted. And the showing itself stays
    useful: once invalidating a verdict became an update instead of a
    delete (`db.stale_evm_replay_verdict`), orphanhood had only one
    deliberate path left, so a line appearing here became news to read
    rather than a routine status to ignore.

    The shape matches `candidates` so `reset` and `_describe` work without
    a second branch.
    """
    where = [
        f"c.network_id IN ({', '.join('?' for _ in networks)})",
        "c.is_replay = 1",
        "s.token_address IS NULL",
    ]
    params: list[str] = list(networks)
    if tokens:
        where.append(f"c.token_address IN ({', '.join('?' for _ in tokens)})")
        params.extend(token.lower() for token in tokens)
    rows = db._conn.execute(
        f"""SELECT c.token_address, c.network_id, NULL AS status, NULL AS calls,
                   NULL AS snapshots, NULL AS transfers, NULL AS from_block,
                   NULL AS last_error, COUNT(*) AS rows_written
              FROM chain_concentration c
              LEFT JOIN evm_replay_state s
                     ON s.token_address = c.token_address
                    AND s.network_id = c.network_id
             WHERE {' AND '.join(where)}
             GROUP BY c.token_address, c.network_id
             ORDER BY COUNT(*) DESC""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def reset(db: RecorderDB, rows: list[dict]) -> dict[str, int]:
    """Deletion token by token, one transaction per token.

    `reset_evm_replay_token` is the same one `--redo` uses, so there is no
    second deletion path to maintain. And token by token, not one batch:
    the live worker may be holding the database, so a small transaction
    waits less, and one failure does not undo what succeeded before it.
    """
    out = {"tokens": 0, "rows_deleted": 0, "calls_freed": 0}
    for row in rows:
        db.reset_evm_replay_token(row["token_address"], row["network_id"])
        out["tokens"] += 1
        out["rows_deleted"] += int(row["rows_written"] or 0)
        out["calls_freed"] += int(row["calls"] or 0)
    return out


def _describe(rows: list[dict]) -> str:
    by_status: dict[str, list[dict]] = {}
    for row in rows:
        by_status.setdefault(str(row["status"] or ""), []).append(row)
    lines = []
    for status, group in sorted(by_status.items()):
        written = sum(int(r["rows_written"] or 0) for r in group)
        calls = sum(int(r["calls"] or 0) for r in group)
        lines.append(
            f"  {status or '(no state)'}: {len(group)} tokens · {calls} "
            f"calls spent · {written} rows to delete"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--networks", nargs="+", required=True,
        help="the intended replay networks, e.g. 8453",
    )
    parser.add_argument(
        "--status", nargs="+", default=["error"],
        help="the statuses to return to the queue (default: error)",
    )
    parser.add_argument(
        "--token", nargs="*", default=[],
        help="specific addresses; default is everything matching the status",
    )
    parser.add_argument(
        "--max", type=int, default=0,
        help="cap on tokens returned in this run (0 = no cap)",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        networks = repair_evm_ledger.validated_networks(args.networks)
    except ValueError as exc:
        print(str(exc))
        return 2
    statuses = tuple(dict.fromkeys(str(s) for s in args.status))
    refused = sorted(set(statuses) & set(REFUSED_STATUSES))
    if refused:
        print(
            f"statuses not returned by this tool: {', '.join(refused)} — "
            "use `run_evm_replay.py --redo` or `repair_evm_ledger.py`"
        )
        return 2

    db = RecorderDB(config.DB_PATH, os.path.join(HERE, "schema.sql"))
    try:
        rows = candidates(db, networks, statuses, tuple(args.token))
        stray = orphans(db, networks, tuple(args.token))
        if stray:
            # News, not candidates: there is no `--orphans` and no deletion
            # path from here. The rows are measured, and what they lack is
            # a state row the next walk writes on its own.
            print(
                f"orphans (informational only, never deleted): "
                f"{len(stray)} tokens · "
                f"{sum(int(r['rows_written'] or 0) for r in stray)} rows — "
                "to judge their correctness: audit_evm_ledger.py"
            )
        if args.max > 0:
            rows = rows[: args.max]
        print(
            f"networks: {', '.join(networks)} · statuses: {', '.join(statuses)}"
            f" · matched: {len(rows)}"
        )
        if rows:
            print(_describe(rows))
        if not args.apply:
            # Display is the default because deletion spends calls that are
            # not refunded: returning a Base token means a full walk from
            # genesis in the coming cycles.
            print("display only. add --apply to execute.")
            return 0
        done = reset(db, rows)
        print(
            f"returned {done['tokens']} tokens · deleted "
            f"{done['rows_deleted']} concentration rows · "
            f"{done['calls_freed']} calls previously spent"
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
