"""Backfill the daily price history of every collected token for a trustworthy ATH.

The ``getBarsNew`` endpoint truncates the response at roughly 900 bars. So we
fetch at 1D resolution and page backward using the oldest timestamp returned on
each page. Writes are idempotent, and the ``historical_bars_state`` table makes
runs resumable. The series only counts as valid for features once it reaches
``last_status='ok'``.

Usage::

    py backfill_bars.py --dry-run
    py backfill_bars.py --max-calls 200
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402
from features import epoch_of  # noqa: E402

from recorder import _fetch_bars_raw  # noqa: E402

RESOLUTION = "1D"
EARLIEST_EPOCH = 1_420_070_400  # 2015-01-01; before any token date we target
MAX_EMPTY_ATTEMPTS = 3
PACING_SECONDS = 1.50


def collected_tokens(db: RecorderDB) -> list[dict[str, Any]]:
    """Every token/network pair seen in any collection source, with creation date when known."""
    rows = db._conn.execute(
        """WITH u AS (
               SELECT token_address, network_id FROM signal_events
               UNION SELECT token_address, network_id FROM activity_events
               UNION SELECT token_address, network_id FROM watchlist
               UNION SELECT token_address, network_id FROM market_ticks
               UNION SELECT token_address, network_id FROM token_static
           )
           SELECT u.token_address, COALESCE(u.network_id, '') AS network_id,
                  s.token_created_at
             FROM u LEFT JOIN token_static s
               ON s.token_address=u.token_address AND s.network_id=u.network_id
            WHERE u.token_address IS NOT NULL AND trim(u.token_address) <> ''
            ORDER BY u.token_address, u.network_id"""
    ).fetchall()
    return [dict(row) for row in rows]


def lower_bound(token_created_at: Any) -> int:
    created = epoch_of(token_created_at)
    return max(EARLIEST_EPOCH, created or EARLIEST_EPOCH)


def _state(db: RecorderDB, token: str, network: str) -> Any:
    return db._conn.execute(
        """SELECT * FROM historical_bars_state
            WHERE token_address=? AND network_id=? AND resolution=?""",
        (token, network, RESOLUTION),
    ).fetchone()


def _save_state(
    db: RecorderDB, token: str, network: str, *, cursor_to: int | None,
    oldest_ts: int | None, status: str, added: int = 0, attempted: bool = True,
) -> None:
    db._conn.execute(
        """INSERT INTO historical_bars_state(
               token_address,network_id,resolution,cursor_to,oldest_ts,last_status,
               candles,calls,attempts,updated_at)
           VALUES(?,?,?,?,?,?,?, ?,?,?)
           ON CONFLICT(token_address,network_id,resolution) DO UPDATE SET
               cursor_to=excluded.cursor_to,
               oldest_ts=COALESCE(MIN(historical_bars_state.oldest_ts,
                                    excluded.oldest_ts), excluded.oldest_ts,
                                  historical_bars_state.oldest_ts),
               last_status=excluded.last_status,
               candles=historical_bars_state.candles + excluded.candles,
               calls=historical_bars_state.calls + excluded.calls,
               attempts=historical_bars_state.attempts + excluded.attempts,
               updated_at=excluded.updated_at""",
        (token, network, RESOLUTION, cursor_to, oldest_ts, status, added,
         1 if attempted else 0, 1 if attempted else 0, utcnow_iso()),
    )
    db._conn.commit()


async def run(
    client: Any,
    db: RecorderDB,
    *,
    max_calls: int,
    dry_run: bool = False,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "tokens": 0, "done": 0, "partial": 0, "no_data": 0, "errors": 0,
        "skipped_done": 0, "calls": 0, "retries": 0,
        "candles": 0, "dry_run": dry_run,
    }
    targets = collected_tokens(db)
    if dry_run:
        pending = [t for t in targets if not (_state(db, t["token_address"], t["network_id"])
                   and _state(db, t["token_address"], t["network_id"])["last_status"]
                   in ("ok", "no_data"))]
        stats.update(tokens=len(targets), partial=len(pending), calls=min(len(pending), max_calls))
        return stats

    now_epoch = int(time.time())
    consecutive_errors = 0
    for target in targets:
        token, network = target["token_address"], str(target["network_id"] or "")
        stats["tokens"] += 1
        state = _state(db, token, network)
        if state and state["last_status"] in ("ok", "no_data"):
            stats["skipped_done"] += 1
            continue
        lower = lower_bound(target.get("token_created_at"))
        cursor = int(state["cursor_to"]) if state and state["cursor_to"] else now_epoch
        total_before = int(state["candles"] or 0) if state else 0

        while cursor > lower:
            try:
                fetch_retry = 0
                while True:
                    if stats["calls"] >= max_calls:
                        stats["stopped_at"] = f"{token[:12]}…:{network}"
                        return stats
                    stats["calls"] += 1
                    if progress is not None and stats["calls"] % 25 == 0:
                        progress(
                            f"progress calls={stats['calls']} tokens={stats['tokens']} "
                            f"done={stats['done']} candles={stats['candles']} "
                            f"errors={stats['errors']} retries={stats['retries']}"
                        )
                    try:
                        raw = await _fetch_bars_raw(
                            client, token, network, lower, cursor, RESOLUTION
                        )
                        break
                    except Exception as fetch_exc:
                        if (type(fetch_exc).__name__ == "UpstreamUnavailableError"
                                and fetch_retry < 2):
                            fetch_retry += 1
                            stats["retries"] += 1
                            await sleep(2.0 * fetch_retry)
                            continue
                        raise
                consecutive_errors = 0
                rows = extract.extract_bars(raw, token, network, RESOLUTION, utcnow_iso())
                if not rows:
                    current = _state(db, token, network)
                    accumulated = int(current["candles"] or 0) if current else total_before
                    attempts = int(current["attempts"] or 0) + 1 if current else 1
                    if accumulated > 0:
                        _save_state(db, token, network, cursor_to=cursor,
                                    oldest_ts=None, status="ok", added=0)
                        db.recompute_bar_flags(token, network, RESOLUTION)
                        stats["done"] += 1
                    else:
                        terminal = attempts >= MAX_EMPTY_ATTEMPTS
                        _save_state(db, token, network, cursor_to=cursor,
                                    oldest_ts=None,
                                    status="no_data" if terminal else "empty_retry",
                                    added=0)
                        stats["no_data" if terminal else "partial"] += 1
                    break

                added = db.insert_bars(rows)
                oldest = min(int(row["ts"]) for row in rows)
                stats["candles"] += added
                next_cursor = oldest - 1
                complete = oldest <= lower + 2 * 86400
                _save_state(db, token, network, cursor_to=next_cursor,
                            oldest_ts=oldest, status="ok" if complete else "partial",
                            added=added)
                if complete:
                    db.recompute_bar_flags(token, network, RESOLUTION)
                    stats["done"] += 1
                    break
                if next_cursor >= cursor:  # progress guard against a broken upstream envelope
                    _save_state(db, token, network, cursor_to=cursor,
                                oldest_ts=oldest, status="error", attempted=False)
                    stats["errors"] += 1
                    break
                cursor = next_cursor
                await sleep(PACING_SECONDS)
            except Exception as exc:  # noqa: BLE001 — one token must not stop the whole run
                consecutive_errors += 1
                _save_state(db, token, network, cursor_to=cursor,
                            oldest_ts=None, status="error", added=0)
                db.set_meta(
                    "last_error_historical_bars",
                    f"{utcnow_iso()}: {token[:12]}…: {type(exc).__name__}: {exc}",
                )
                stats["errors"] += 1
                # The token refreshes on disk, but the current client keeps the
                # old one. Exit immediately so the next run reloads a fresh
                # credential; continuing would turn every remaining token into
                # spurious errors.
                if type(exc).__name__ == "UnauthorizedError":
                    stats["stopped_reason"] = "session_expired"
                    return stats
                if (type(exc).__name__ == "UpstreamUnavailableError"
                        and consecutive_errors >= 3):
                    stats["stopped_reason"] = "upstream_unavailable"
                    return stats
                break
        else:
            _save_state(db, token, network, cursor_to=cursor,
                        oldest_ts=None, status="ok", attempted=False)
            db.recompute_bar_flags(token, network, RESOLUTION)
            stats["done"] += 1
        await sleep(PACING_SECONDS)
    return stats


def _load_client() -> Any:
    from fomo_api.auth.credential_store import CredentialStore
    from fomo_api.clients.fomo_client import FomoClient

    creds = CredentialStore(config.credential_state_path()).load()
    if creds is None or not creds.access_token:
        raise RuntimeError("No valid fomo credential in the state file.")
    return FomoClient(session_token=creds.access_token)


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    max_calls = 10**9
    if "--max-calls" in sys.argv:
        try:
            max_calls = max(0, int(sys.argv[sys.argv.index("--max-calls") + 1]))
        except (IndexError, ValueError):
            raise SystemExit("--max-calls needs an integer") from None
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    client = None if dry_run else _load_client()
    try:
        stats = await run(
            client, db, max_calls=max_calls, dry_run=dry_run,
            progress=lambda message: print(message, flush=True),
        )
        print(stats)
    finally:
        if client is not None:
            await client.aclose()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
