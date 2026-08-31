"""Warm the dashboard cache at boot — the first visitor doesn't pay the cold cost.

The measured problem (2026-08-29): `serve_dashboard.py` boots with an empty
`cache.MEMO`, so whoever opens the page first waits for the cold computation of
the three heavy paths (3.4 seconds for networks, 1.7 for labeling, 1.0 for
counts — measured on the live database), because the page fires its fifteen
requests in parallel and waits for the slowest one before rendering anything.

The fix has been sitting in `cache.py` since the beginning: the `warm()` function
is documented "to be called from a background thread at boot" but nobody calls
it. This file is that call.

The warmup contract:
- **One daemon thread**, started after listening begins; it never blocks process exit.
- **Order by weight**: slowest first — whoever opens the page before warmup
  finishes waits on the longest path for nothing, so every second cut from the
  front of the thread is cut from their wait.
- **Failure never stops it**: a key that fails is logged and skipped — warmup is
  a courtesy, not a boot requirement, and the database may be busy with the
  recorder at boot time.
- **No writes, no touching the database**: `warm()` goes through the same
  `get()` — a read in mode=ro like everything else in the dashboard.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import cache
import config

# The heavy keys in order: heaviest first (times measured 2026-08-29).
# Add every new heavy `_cached` key here — `test_every_heavy_key_is_covered`
# in test_warmup.py checks that this list doesn't drift.
HEAVY_KEYS: tuple[tuple[str, float], ...] = (
    ("network_summary", config.NETWORK_SUMMARY_TTL_SECONDS),
    ("labeling", config.LABELING_TTL_SECONDS),
    ("table_counts", config.TABLE_COUNTS_TTL_SECONDS),
)

# `ticks_summary` is a public path that costs 1.5 seconds but the page doesn't
# call it today; deliberately not warmed — warming what isn't displayed wastes
# disk the recorder competes for.

_ComputeFor = Callable[[str], Callable[[], Any]]


def warm_heavy_keys(
    memo: cache.TTLMemo, compute_for: _ComputeFor
) -> dict[str, bool]:
    """Warms the heavy keys **in parallel** and returns a {key: succeeded?} map.

    `compute_for` turns a key name into a zero-argument callable that opens its
    own connection — the same contract as `cache.TTLMemo.get`. An exception from
    a key is caught and passed over (the False value in the map), so it never
    kills the warmup thread before the rest are done.

    And why parallel after the order used to be sequential? Measured
    (2026-08-29): whoever opens the page in the first seconds of boot waits for
    the **sum** of the keys ahead of theirs — `labeling` used to be answered
    after 3.4+1.5 seconds of networks and labeling combined. Readers in SQLite
    don't contend on locks (every connection is mode=ro), so parallelism gets
    each path ready within its own time, and the worst possible wait = the
    single slowest key.
    """
    done: dict[str, bool] = {}
    threads: list[threading.Thread] = []

    def _warm_one(key: str, ttl: float) -> None:
        done[key] = memo.warm(key, ttl, compute_for(key))

    for key, ttl in HEAVY_KEYS:
        thread = threading.Thread(
            target=_warm_one, args=(key, ttl), daemon=True,
            name=f"dashboard-warmup-{key}",
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    return done


def _dao_compute(key: str) -> Callable[[], Any]:
    """Builds the cold-compute function for a key — the same calls the paths in app.py make."""
    import dao

    def _compute() -> Any:
        conn = dao.connect_ro(config.DB_PATH)
        try:
            if key == "network_summary":
                return dao.network_summary(conn)
            if key == "labeling":
                return dao.labeling_outcomes(
                    conn,
                    config.LIVE_START_TS,
                    design_version=config.CONTROL_DESIGN_VERSION,
                    gate_targets=(
                        config.CONTROL_PRELIMINARY_TARGET,
                        config.CONTROL_DECISION_TARGET,
                    ),
                )
            if key == "table_counts":
                return dao.table_counts(conn)
            raise KeyError(f"unknown warmup key: {key}")
        finally:
            conn.close()

    return _compute


# Replaceable in tests (test_warmup_thread_is_daemon_and_runs_after_start)
def _spawn_background(run: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=run, daemon=True, name="dashboard-warmup")
    thread.start()
    return thread


def start_warmup_thread(memo: cache.TTLMemo | None = None) -> threading.Thread:
    """Starts the warmup thread and returns it. A warmup failure is logged, not raised."""
    target_memo = memo if memo is not None else cache.MEMO

    def _run() -> None:
        warm_heavy_keys(target_memo, _dao_compute)

    return _spawn_background(_run)
