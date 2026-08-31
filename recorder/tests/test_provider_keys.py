import json
import time

import key_file
import provider_keys


def test_read_keys_supports_plural_and_legacy_file_keys(monkeypatch, tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({
        "helius_api_keys": [" h1 ", "h2", "h1"],
        "nodereal_api_key": "n1",
    }))
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(path))

    assert provider_keys.read_keys("helius_api_keys", "helius_api_key") == ["h1", "h2"]
    assert provider_keys.read_keys("nodereal_api_keys", "nodereal_api_key") == ["n1"]


def test_read_keys_supports_comma_separated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_PROVIDER_KEYS", "a, b,,a")
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(tmp_path / "missing"))

    assert provider_keys.read_keys("unused", "unused", "TEST_PROVIDER_KEYS") == ["a", "b"]


def test_read_keys_skips_keys_the_dashboard_paused(monkeypatch, tmp_path):
    """A temporary pause: it stays in the file for the dashboard to restore,
    and never enters the pool, so it is never called."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"helius_api_keys": [
        {"key": "live-one", "label": "main"},
        {"key": "paused-one", "enabled": False},
        {"key": "live-two"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(path))

    assert provider_keys.read_keys("helius_api_keys", "helius_api_key") == ["live-one", "live-two"]


def test_what_the_dashboard_writes_is_exactly_what_the_pool_reads(monkeypatch, tmp_path):
    """The writer and the reader are tied together in one test — otherwise the
    failure is a full outage.

    The old shape kept only strings (`isinstance(value, str)`), so the first
    dashboard write onto a process running that code would have returned
    **zero keys**: not one key failing but the whole layer stopping. This
    test goes through the real writer (`key_file.save_entries`), not a file
    written by hand in the test, so any drift between the two shapes falls
    here, not in production.
    """
    path = tmp_path / "keys.json"
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(path))

    key_file.save_entries(str(path), "helius_api_keys", "helius_api_key", [
        {"key": "dashboard-added-1", "label": "added by the dashboard"},
        {"key": "dashboard-paused", "label": "paused", "enabled": False},
    ])

    assert provider_keys.read_keys("helius_api_keys", "helius_api_key") == ["dashboard-added-1"]


def test_read_keys_reflects_an_edit_made_while_the_process_runs(monkeypatch, tmp_path):
    """A key added from the dashboard must work without a restart: read from
    disk on every call."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"helius_api_keys": ["first-key"]}), encoding="utf-8")
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(path))
    assert provider_keys.read_keys("helius_api_keys", "helius_api_key") == ["first-key"]

    path.write_text(json.dumps({"helius_api_keys": ["first-key", "added-later"]}), encoding="utf-8")
    assert provider_keys.read_keys("helius_api_keys", "helius_api_key") == [
        "first-key", "added-later",
    ]


def test_key_pool_rotates_and_cools_down_current_key(monkeypatch):
    pool = provider_keys.KeyPool(["a", "b"], cooldown_seconds=60)

    assert pool.current() == "a"
    assert pool.rotate(block_current=True) == "b"
    assert pool.rotate(block_current=True) == "a"


def test_stats_counts_blocked_keys_and_rotations():
    """The dashboard needs "how many keys are available right now", not "how
    many are in the file"."""
    pool = provider_keys.KeyPool(["a", "b", "c"], cooldown_seconds=60)

    fresh = pool.stats()
    assert fresh["keys"] == 3
    assert (fresh["blocked"], fresh["available"], fresh["rotations"]) == (0, 3, 0)

    pool.rotate(block_current=True)
    after = pool.stats()
    assert (after["blocked"], after["available"], after["rotations"]) == (1, 2, 1)


def test_blocked_count_forgets_keys_whose_cooldown_expired():
    """The cooldown is temporary: a dashboard that counts "cooled once" would
    stay red after healing."""
    pool = provider_keys.KeyPool(["a", "b"], cooldown_seconds=60)
    pool.rotate(block_current=True)

    assert pool.blocked_count() == 1
    # One hour later on the same monotonic clock ⇒ nothing is cooling down.
    assert pool.blocked_count(now=time.monotonic() + 3600) == 0


def test_stats_says_which_key_is_cooled_not_just_how_many():
    """"One of three rejected" does not say which one ⇒ three red dots and the
    healthy one gets blamed."""
    pool = provider_keys.KeyPool(["a", "b", "c"], cooldown_seconds=60)
    pool.rotate(block_current=True)   # cools "a" and moves to "b"

    stats = pool.stats()
    assert stats["blocked_index"] == [0]
    assert stats["index"] == 1
    # The count and the list are two faces of one truth, so they must not
    # contradict each other in the display.
    assert stats["blocked"] == len(stats["blocked_index"])
    assert pool.stats(now=time.monotonic() + 3600)["blocked_index"] == []


def test_blocked_index_positions_match_the_order_the_dashboard_shows():
    """The position matches a dashboard row: the list order is the order of
    the enabled ones in the file."""
    pool = provider_keys.KeyPool(["a", "b", "c"], cooldown_seconds=60)
    pool.rotate(block_current=True)          # a is cooling, the pointer is on b
    pool.rotate(block_current=True)          # b is cooling too, the pointer is on c

    assert pool.stats()["blocked_index"] == [0, 1]
    assert pool.current() == "c"


def test_stats_never_leaks_a_key_value_or_a_fragment_of_one():
    """FR-013: the report is counts and indexes. No value, no fragment of
    one, no fingerprint."""
    secret = "sk-live-abcdef123456"
    pool = provider_keys.KeyPool([secret, "second-secret"])
    pool.rotate(block_current=True)

    blob = provider_keys.pool_report(
        {"helius": pool.stats()}, at="2026-08-17T00:00:00+00:00", owner="chain",
    )

    assert secret not in blob
    for size in range(4, len(secret) + 1):
        assert secret[:size] not in blob
    assert json.loads(blob)["pools"]["helius"]["keys"] == 2


def test_pool_report_names_its_owner_so_two_processes_do_not_overwrite():
    """One provider in two processes with two different states; a single row
    for both is a lie.

    No provider is shared today — the replay path is keyless since GoldRush
    was removed — but the owner field is what stops the newer one from
    wiping the other's row the day a provider is added to both sides.
    """
    report = json.loads(provider_keys.pool_report(
        {"nodereal": {"keys": 1}}, at="2026-08-17T00:00:00+00:00", owner="replay",
    ))

    assert report["owner"] == "replay"
    assert report["at"] == "2026-08-17T00:00:00+00:00"


def test_write_pool_report_stamps_one_meta_row_per_owner():
    class _DB:
        def __init__(self):
            self.meta = {}

        def note_error(self, key, value):
            self.meta[key] = value
            return True

    db = _DB()
    assert provider_keys.write_pool_report(db, "chain", {"helius": {"keys": 2}}, "t0")
    assert list(db.meta) == ["provider_keys_chain"]
    assert json.loads(db.meta["provider_keys_chain"])["pools"]["helius"]["keys"] == 2


def test_write_pool_report_returns_false_on_a_locked_database():
    """A locked database loses the report but does not take down the cycle —
    it is a report, not a measurement."""
    class _Locked:
        def note_error(self, _key, _value):
            return False

    assert provider_keys.write_pool_report(_Locked(), "chain", {}, "t0") is False


def test_write_pool_report_tolerates_a_database_without_the_helper():
    """A fake database from an old test does not take down a caller that only
    writes a report."""
    assert provider_keys.write_pool_report(object(), "chain", {}, "t0") is False

