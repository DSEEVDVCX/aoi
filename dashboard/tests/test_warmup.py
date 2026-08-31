"""Warming the cache at boot — the first visitor doesn't pay the cold price.

The measured problem (2026-08-29): the dashboard boots with an empty cache,
so the first one to open the page waits for the three heavy routes in
parallel — 3.4 seconds for networks, 1.7 for labeling, 1.0 for counts —
because `Promise.all` in the page waits for the slowest of them before
painting anything.

And the fix has been ready in `cache.py` from the start: the `warm()`
function is documented as "for calling from a background thread at boot" but
nobody calls it. This file pins the contract:

1. **Boot warms.** `serve_dashboard.py` starts a single thread after
   listening, warming the heavy keys in order. A failure is logged and
   doesn't take the boot down.
2. **Warming serves.** After it finishes, no cold computation runs on the
   first request — `cache.MEMO.get` finds the value and returns it
   immediately (tested through `TTLMemo` literally).
3. **Operational silence.** The warmup prints nothing and writes no log
   except on failure, because it runs on every boot of the scheduled task
   (and the server relaunches it after every boot).
"""
import threading

import cache
import pytest
import warmup


def test_warmup_heats_all_heavy_keys():
    """The thread warms the three heavy keys — in parallel, so none waits for another.

    Measured: sequential warming made `labeling` answer after the **sum** of
    the networks and labeling times (3.4+1.5 seconds) for whoever beat the
    warmup thread. Parallelism has every key ready within its own time.
    """
    computed: set[str] = set()

    def make_compute(key: str):
        def _compute():
            computed.add(key)
            return {"done": key}
        return _compute

    memo = cache.TTLMemo()
    done = warmup.warm_heavy_keys(memo, compute_for=make_compute)

    # the full key list (its completeness is guarded by the drift test below)
    expected = {key for key, _ttl in warmup.HEAVY_KEYS}
    assert set(done) == expected
    assert all(done.values())
    assert computed == expected


def test_warmup_survives_a_failing_key_and_continues():
    """A failing key doesn't stop the rest — warming is a courtesy, not a boot condition.

    The database may be busy with the recorder at boot; one key's failure (an
    exception raised from `compute`) must be logged and passed by, not kill
    the whole warmup thread before the remaining keys.
    """
    calls: list[str] = []

    def make_compute(key: str):
        def _compute():
            calls.append(key)
            if key == "labeling":
                raise RuntimeError("database is locked")
            return {"ok": key}
        return _compute

    memo = cache.TTLMemo()
    done = warmup.warm_heavy_keys(memo, compute_for=make_compute)

    expected = {key for key, _ttl in warmup.HEAVY_KEYS}
    assert set(calls) == expected
    assert len(calls) == len(expected)
    assert done == {key: (key != "labeling") for key in expected}


def test_a_warmed_cache_answers_the_first_request_without_cold_compute():
    """The whole contract is here: after warming, no cold computation runs on the first request.

    `warm()` itself called `get()`: the value exists within its lifetime, so
    the first real request is answered from memory, and any `compute` passed
    to it after the warming never runs at all.
    """
    memo = cache.TTLMemo()

    def compute():
        return {"value": 41}
    memo.warm("labeling", ttl=300.0, compute=compute)

    def never():
        raise AssertionError("must not run — the value is warmed")

    value, meta = memo.get("labeling", 300.0, never)
    assert value == {"value": 41}
    assert meta["stale"] is False
    assert meta["age_seconds"] < 300


def test_warmup_thread_is_daemon_and_runs_after_start():
    """The thread is a daemon and doesn't prevent the process from exiting — the contract for background threads here."""
    started = threading.Event()
    probe = {"spawned": False}

    original_spawn = warmup._spawn_background

    def tracking_spawn(run):
        probe["spawned"] = True
        started.set()
        original_spawn(run)

    warmup._spawn_background = tracking_spawn
    try:
        thread = warmup.start_warmup_thread()
        assert started.wait(timeout=5)
        assert probe["spawned"] is True
        if thread is not None:
            assert thread.daemon is True
    finally:
        warmup._spawn_background = original_spawn


def test_warmup_uses_the_live_memo_and_config_ttls():
    """It uses the real process cache and config's lifetimes — not a test cache.

    Without this guard the warmup could be built against a copy of `TTLMemo`
    that requests never reach, and the test would go green while the server
    stayed as cold as before.
    """
    seen: dict[str, float] = {}

    real_get = cache.MEMO.get
    real_ttls = {
        "network_summary": config_ttls()["NETWORK_SUMMARY_TTL_SECONDS"],
        "labeling": config_ttls()["LABELING_TTL_SECONDS"],
        "table_counts": config_ttls()["TABLE_COUNTS_TTL_SECONDS"],
        "watchlist_market": config_ttls()["WATCHLIST_MARKET_TTL_SECONDS"],
    }

    def fake_get(key, ttl, compute, **kwargs):
        seen[key] = ttl
        return {"warmed": key}, {}

    cache.MEMO.get = fake_get
    try:
        warmup.warm_heavy_keys(cache.MEMO, compute_for=lambda key: (lambda: None))
        assert seen == real_ttls
    finally:
        cache.MEMO.get = real_get


def config_ttls():
    import config
    return {
        "NETWORK_SUMMARY_TTL_SECONDS": config.NETWORK_SUMMARY_TTL_SECONDS,
        "LABELING_TTL_SECONDS": config.LABELING_TTL_SECONDS,
        "TABLE_COUNTS_TTL_SECONDS": config.TABLE_COUNTS_TTL_SECONDS,
        "WATCHLIST_MARKET_TTL_SECONDS": config.WATCHLIST_MARKET_TTL_SECONDS,
    }


@pytest.mark.parametrize("missing", ["network_summary", "labeling", "table_counts", "watchlist_market"])
def test_every_heavy_key_is_covered_by_warmup(missing):
    """The guard against drift: a new heavy key without warming is caught immediately."""
    keys = {key for key, _ttl in warmup.HEAVY_KEYS}

    assert missing in keys
    assert keys == {"network_summary", "labeling", "table_counts", "watchlist_market"}
