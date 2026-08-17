import json
import time

import provider_keys


def test_read_keys_supports_plural_and_legacy_file_keys(monkeypatch, tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({
        "helius_api_keys": [" h1 ", "h2", "h1"],
        "goldrush_api_key": "g1",
    }))
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(path))

    assert provider_keys.read_keys("helius_api_keys", "helius_api_key") == ["h1", "h2"]
    assert provider_keys.read_keys("goldrush_api_keys", "goldrush_api_key") == ["g1"]


def test_read_keys_supports_comma_separated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_PROVIDER_KEYS", "a, b,,a")
    monkeypatch.setattr(provider_keys.config, "chain_keys_path", lambda: str(tmp_path / "missing"))

    assert provider_keys.read_keys("unused", "unused", "TEST_PROVIDER_KEYS") == ["a", "b"]


def test_key_pool_rotates_and_cools_down_current_key(monkeypatch):
    pool = provider_keys.KeyPool(["a", "b"], cooldown_seconds=60)

    assert pool.current() == "a"
    assert pool.rotate(block_current=True) == "b"
    assert pool.rotate(block_current=True) == "a"


def test_stats_counts_blocked_keys_and_rotations():
    """اللوحة تحتاج «كم مفتاحاً متاحاً الآن» لا «كم مفتاحاً في الملف»."""
    pool = provider_keys.KeyPool(["a", "b", "c"], cooldown_seconds=60)

    fresh = pool.stats()
    assert fresh["keys"] == 3
    assert (fresh["blocked"], fresh["available"], fresh["rotations"]) == (0, 3, 0)

    pool.rotate(block_current=True)
    after = pool.stats()
    assert (after["blocked"], after["available"], after["rotations"]) == (1, 2, 1)


def test_blocked_count_forgets_keys_whose_cooldown_expired():
    """التبريد مؤقّت: لوحةٌ تعدّ «بُرِّد يوماً» تبقى حمراء بعد الشفاء."""
    pool = provider_keys.KeyPool(["a", "b"], cooldown_seconds=60)
    pool.rotate(block_current=True)

    assert pool.blocked_count() == 1
    # ساعةٌ لاحقاً على نفس الساعة الرتيبة ⇒ لا شيء مبرَّد.
    assert pool.blocked_count(now=time.monotonic() + 3600) == 0


def test_stats_never_leaks_a_key_value_or_a_fragment_of_one():
    """FR-013: التقرير أعدادٌ ومؤشّرات. لا قيمة، ولا كسرٌ منها، ولا بصمة."""
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
    """حوض GoldRush يوجد في عمليّتين بحالتين مختلفتين؛ صفٌّ واحد لهما كذبة."""
    report = json.loads(provider_keys.pool_report(
        {"goldrush": {"keys": 1}}, at="2026-08-17T00:00:00+00:00", owner="replay",
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
    """القاعدة المقفلة تُفقد التقرير ولا تُسقط الدورة — تقريرٌ لا قياس."""
    class _Locked:
        def note_error(self, _key, _value):
            return False

    assert provider_keys.write_pool_report(_Locked(), "chain", {}, "t0") is False


def test_write_pool_report_tolerates_a_database_without_the_helper():
    """قاعدةٌ وهميّة في اختبارٍ قديم لا تُسقط منادياً يكتب تقريراً فحسب."""
    assert provider_keys.write_pool_report(object(), "chain", {}, "t0") is False

