"""Retro backfill: thesis history for every watched token.

Every thesis carries `createdAt`, and the endpoint supports pagination via
`lastId` — so the **historical count** is recoverable: how many theses
existed at the moment of the signal. Measured: three pages (300 theses) went
back to before the recorder started, so a few pages cover the archive.

**What is not recoverable**: likes and replies **at the moment of the
signal**. The API only gives the current counter, with no historical log.
That is why `token_thesis.fetched_at` is recorded: it is when the likes were
measured, and it must not be read as if it were the value at write time.

Usage:
    py backfill_thesis.py --pages 5          # 500 theses per token at most
    py backfill_thesis.py --pages 5 --dry-run
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

_PACING = 1.2


def _arg(name: str, default: int) -> int:
    if name in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


async def fetch_history(client, addr: str, net: str, max_pages: int) -> list[dict]:
    """Pages backwards in time and returns every gathered thesis.

    Stops when: pages run out, a page returns nothing new (protection against
    an infinite loop if the server ignores `lastId`), or the limit is reached.
    """
    from fomo_api.config import settings

    fetched_at = utcnow_iso()
    out: list[dict] = []
    seen_ids: set[str] = set()
    last_id: str | None = None

    for _ in range(max_pages):
        params: dict = {
            "tokenAddress": addr,
            "networkId": int(net) if str(net).isdigit() else net,
            "threshold": config.SOCIAL_THRESHOLD,
        }
        if last_id:
            params["lastId"] = last_id
        raw = await client._get(settings.upstream_feed_token_thesis_path, params)
        rows = extract.extract_thesis_items(raw, addr, net, fetched_at)
        fresh = [r for r in rows if r["id"] not in seen_ids]
        if not fresh:
            break  # the server returns the same page — stop instead of looping
        seen_ids.update(r["id"] for r in fresh)
        out.extend(fresh)
        _total, has_next = extract.thesis_total(raw)
        if not has_next:
            break
        last_id = rows[-1]["id"]
        await asyncio.sleep(_PACING)
    return out


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    max_pages = _arg("--pages", 5)

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    targets = db._conn.execute(
        "SELECT token_address, network_id FROM watchlist "
        "GROUP BY token_address, network_id ORDER BY MIN(first_seen_at)"
    ).fetchall()
    print(f"Watched token pairs: {len(targets)} · page limit per token: {max_pages}")

    if dry_run:
        print("[preview] no network connection, no writes.")
        db.close()
        return

    from fomo_api.auth.credential_store import CredentialStore
    from fomo_api.clients.fomo_client import FomoClient

    credentials = CredentialStore(config.credential_state_path()).load()
    if credentials is None or not credentials.access_token:
        raise RuntimeError("No valid credentials — start the api service first.")
    token = credentials.access_token
    client = FomoClient(session_token=token)

    total_rows = total_added = failed = 0
    try:
        for i, t in enumerate(targets, 1):
            addr, net = t["token_address"], str(t["network_id"] or "")
            try:
                rows = await fetch_history(client, addr, net, max_pages)
            except Exception as exc:
                failed += 1
                print(f"  [{i}/{len(targets)}] {addr[:14]}… failed: {type(exc).__name__}")
                await asyncio.sleep(_PACING)
                continue
            total_rows += len(rows)
            if not dry_run:
                total_added += db.insert_thesis_items(rows)
            oldest = min((r["created_at"] for r in rows), default="—")
            print(
                f"  [{i}/{len(targets)}] {addr[:14]}… {len(rows):>4} theses "
                f"· oldest {str(oldest)[:19]}",
                flush=True,
            )
            await asyncio.sleep(_PACING)
    finally:
        await client.aclose()

    print(f"\n{'[preview] ' if dry_run else ''}theses gathered: {total_rows}")
    if not dry_run:
        print(f"Newly inserted: {total_added} (duplicates ignored)")
        n = db._conn.execute("SELECT COUNT(*) FROM token_thesis").fetchone()[0]
        rng = db._conn.execute(
            "SELECT MIN(created_at) lo, MAX(created_at) hi FROM token_thesis"
        ).fetchone()
        print(f"Total token_thesis: {n} · from {str(rng[0])[:19]} to {str(rng[1])[:19]}")
    if failed:
        print(f"Failed tokens: {failed}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
