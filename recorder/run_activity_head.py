"""Scheduled head backfill for global trading activity.

The upstream activity endpoint exposes a current head plus ``lastId`` paging.
This worker periodically walks from the head until it overlaps local history,
without touching the older historical cursor used by ``backfill_activity.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import backfill_activity as activity  # noqa: E402
import config  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402


def _load_access_token() -> str | None:
    """Reads the current Privy token from disk without printing it or keeping a stale copy."""
    from fomo_api.auth.credential_store import CredentialStore

    creds = CredentialStore(config.credential_state_path()).load()
    return creds.access_token if creds and creds.access_token else None


def _build_client(access_token: str):
    """Builds a client from a known token; separating construction makes rotation testable."""
    from fomo_api.clients.fomo_client import FomoClient

    return FomoClient(session_token=access_token)


async def _maybe_rotate_client(client, current_token: str):
    """Picks up the refreshed on-disk token; a change closes the old client and rebuilds at once."""
    try:
        fresh_token = _load_access_token()
    except Exception:  # noqa: BLE001 — a transient read failed; keep the current client
        return client, current_token
    if not fresh_token or fresh_token == current_token:
        return client, current_token
    fresh_client = _build_client(fresh_token)
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 — cleaning up the old client must not waste the fresh one
        pass
    _log("token rotated → client rebuilt")
    return fresh_client, fresh_token


def _log(message: str) -> None:
    """Write a bounded diagnostic line without exposing credentials."""
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{utcnow_iso()} activity-head: {message}\n")
    except OSError:
        pass


async def main_loop(cycles: int | None = None) -> None:
    current_token = _load_access_token()
    if not current_token:
        raise RuntimeError("No valid credential — start the api service first.")
    client = _build_client(current_token)
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        count = 0
        while cycles is None or count < cycles:
            started = time.monotonic()
            try:
                # The API server refreshes Privy on disk; the long-lived
                # worker picks up the change before every cycle instead of
                # holding the boot token until it gets a 401.
                client, current_token = await _maybe_rotate_client(
                    client, current_token
                )
                stats = await activity.walk_head(
                    client,
                    db,
                    max_pages=getattr(config, "ACTIVITY_HEAD_MAX_PAGES", 3),
                )
                db.note_error("activity_head_last_run_at", utcnow_iso())
                db.note_error("activity_head_last_stats", str(stats))
                if stats["added"]:
                    _log(str(stats))
            except Exception as exc:  # noqa: BLE001 - next cycle retries
                db.note_error(
                    "last_error_activity_head",
                    f"{utcnow_iso()}: {type(exc).__name__}: {exc}"[:400],
                )
            count += 1
            if cycles is not None and count >= cycles:
                break
            remaining = getattr(config, "ACTIVITY_HEAD_INTERVAL_SECONDS", 300) - (
                time.monotonic() - started
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        await client.aclose()
        db.close()


def main() -> None:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(main_loop(cycles))


if __name__ == "__main__":
    main()
