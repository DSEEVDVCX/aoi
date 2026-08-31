"""Retro backfill: build a control group covering **the same period as the signals**.

Why: the live control group enters from the moment it is activated, while
signal tokens entered over the course of two days — so any comparison between
them mixes "the effect of the signal" with "the effect of the entry moment".
This script removes the mixing: it picks tokens from **the snapshot archive
itself** and gives each one an entry stamp from the moment it actually
appeared in the archive.

Possible in the first place because `snapshots` stores full trending/verified
lists for every cycle, and because `getBarsNew` returns price history
stretching back months — so any token's price in the past is available.

Comparison validity conditions (same as the live control group):
- **Never signalled** (neither in `signal_events` nor in `watchlist`).
- **Random selection** from the token universe, not by list position.
- **Entry stamp from a random snapshot** in which it appeared — not the first
  appearance (otherwise selection is biased toward old tokens) and not now
  (otherwise the temporal mixing returns).
- **Blind to performance**: no price or outcome is consulted in selection at all.

Labeled `source='control_retro'` to distinguish them from the live control
group (`'control'`). Bars reach them automatically through the recorder's
normal cycle.

Usage:
    Stop-ScheduledTask -TaskName FomoRecorder
    py seed_control_retro.py --dry-run
    py seed_control_retro.py --count 60
    Start-ScheduledTask -TaskName FomoRecorder
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, decode_raw  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def _arg(name: str, default: int) -> int:
    if name in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def _recorder_is_running() -> bool:
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*run_recorder.py*' }).ProcessId"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — check failed — the caller decides
        return False
    return bool(out.stdout.strip())


def build_appearance_map(db: RecorderDB) -> dict[tuple[str, str], list[str]]:
    """(address, network) → stamps of the snapshots in which the token appeared."""
    seen: dict[tuple[str, str], list[str]] = {}
    rows = db._conn.execute(
        "SELECT recorded_at, raw_json FROM snapshots "
        "WHERE source IN ('trending','verified') ORDER BY id"
    )
    for row in rows:
        try:
            items = extract.unwrap_token_list(decode_raw(row["raw_json"]))
        except Exception:  # noqa: BLE001 — a corrupt snapshot is skipped, it does not fail the build
            continue  # a corrupt snapshot is skipped, it does not fail the build
        for it in items:
            addr = extract._token_address(it)
            if not addr:
                continue
            tok = it.get("token") if isinstance(it.get("token"), dict) else {}
            net = str(tok.get("networkId") or it.get("networkId") or "")
            seen.setdefault((addr, net), []).append(row["recorded_at"])
    return seen


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    want = _arg("--count", 60)
    seed = _arg("--seed", 20260727)

    if not dry_run and _recorder_is_running():
        raise SystemExit(
            "The FomoRecorder task is running right now. Stop it first:\n"
            "  Stop-ScheduledTask -TaskName FomoRecorder"
        )

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        print("Reading the snapshot archive…", flush=True)
        appearances = build_appearance_map(db)
        print(f"  tokens that appeared in the archive: {len(appearances)}")

        signalled = db.signalled_tokens()
        known = db.known_tokens()
        pool = sorted(
            key for key in appearances
            if key[0] not in signalled and key not in known
        )
        print(f"  of those, never signalled and never watched: {len(pool)}")
        if not pool:
            print("No candidates.")
            return

        rng = random.Random(seed)
        picks = rng.sample(pool, min(want, len(pool)))
        now = datetime.now(tz=datetime.now().astimezone().tzinfo)

        added = 0
        for addr, net in picks:
            stamps = appearances[(addr, net)]
            # Entry stamp from a random snapshot it appeared in — not the
            # first, and not now
            entry = rng.choice(stamps)
            if dry_run:
                added += 1
                continue
            if db.admit_control(
                addr, net, config.CONTROL_WATCH_HOURS, entry, source="control_retro"
            ):
                added += 1
        if not dry_run:
            db._conn.commit()
            # A 48-hour window may already have ended for some of them; apply
            # the same rule the recorder applies
            expired = db.deactivate_expired(now.isoformat())
            print(f"  whose window already expired (archive older than 48h): {expired}")

        print(f"\n{'[preview] ' if dry_run else ''}retro control tokens: {added}")
        if not dry_run:
            print(f"  total active control: {db.active_watch_count(is_control=1)}")
            print(f"  active signalled:     {db.active_watch_count(is_control=0)}")
            print("\nBars will reach them automatically through the recorder "
                  "cycle (oldest pulls first).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
