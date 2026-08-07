"""اختبارات كاش الصدارة بعد تحويله إلى الخام (بلا شبكة).

يثبت العقد الذي تبنى عليه الأرشفة: الرتبة تُشتقّ من الموضع (الخام بلا حقل
rank)، والمغلّف الخام الكامل يُحتفَظ به في `last_raw` ليؤرشفه المسجّل في
snapshots كل ساعة — بلا ذلك يضيع مسار كل متصدّر إلى الأبد. الفشل يبقي
الخريطة والخام القديمين (لا يمسحهما).
"""
import pytest

import recorder
from leaderboard_cache import LeaderboardCache

RAW = {
    "success": True,
    "responseObject": {
        "leaderboard": [
            {"id": "trader_A", "userHandle": "alice", "totalPnL": 999.0},
            {"id": "trader_B", "userHandle": "bob"},
            {"userHandle": "no_id"},          # بلا id → لا يدخل الخريطة
            {"id": "trader_C", "userHandle": "carol"},
        ]
    },
}


class _RawClient:
    """عميل وهمي يعيد مغلّفاً خاماً مُعدّاً أو يرمي حسب الطلب."""

    def __init__(self, data=RAW, boom=False):
        self._data = data
        self._boom = boom
        self.calls = []

    async def _get(self, path, params=None):
        self.calls.append((path, params))
        if self._boom:
            raise RuntimeError("upstream boom")
        return self._data


async def test_refresh_builds_lookup_from_raw_position():
    lb = LeaderboardCache(_RawClient(), size=200, refresh_seconds=3600)
    assert await lb.refresh(now_mono=100.0) is True
    # الرتبة = الموضع 1-based (كما في _map_leaderboard): C رابعة رغم الثالث الفارغ
    assert lb.lookup == {"trader_A": 1, "trader_B": 2, "trader_C": 4}


async def test_refresh_keeps_full_raw_for_archival():
    lb = LeaderboardCache(_RawClient(), size=200, refresh_seconds=3600)
    await lb.refresh(now_mono=100.0)
    assert lb.last_raw is RAW   # المغلّف كاملاً لا قائمة مُعيَّنة


async def test_refresh_passes_configured_limit():
    client = _RawClient()
    lb = LeaderboardCache(client, size=200, refresh_seconds=3600)
    await lb.refresh(now_mono=100.0)
    assert client.calls[0][1] == {"limit": 200}


async def test_failure_keeps_old_lookup_and_raw():
    lb = LeaderboardCache(_RawClient(), size=200, refresh_seconds=3600)
    await lb.refresh(now_mono=100.0)
    old_lookup, old_raw = dict(lb.lookup), lb.last_raw

    lb.set_client(_RawClient(boom=True))
    assert await lb.refresh(now_mono=4000.0) is False
    assert lb.lookup == old_lookup        # الخريطة القديمة بقيت
    assert lb.last_raw is old_raw         # الخام القديم بقي


async def test_malformed_envelope_returns_false_and_keeps_state():
    lb = LeaderboardCache(_RawClient(data={"responseObject": {}}), 200, 3600)
    assert await lb.refresh(now_mono=100.0) is False
    assert lb.lookup == {}
    assert lb.last_raw is None


async def test_none_envelope_returns_false():
    lb = LeaderboardCache(_RawClient(data=None), 200, 3600)
    assert await lb.refresh(now_mono=100.0) is False


async def test_staleness_drives_maybe_refresh():
    client = _RawClient()
    lb = LeaderboardCache(client, size=200, refresh_seconds=3600)
    assert await lb.maybe_refresh(now_mono=100.0) is True    # أوّل مرّة: قديم دائماً
    assert await lb.maybe_refresh(now_mono=200.0) is False   # طازج — لا نداء
    assert len(client.calls) == 1
    assert await lb.maybe_refresh(now_mono=100.0 + 3601) is True  # حان التحديث
    assert len(client.calls) == 2


# --- refresh_leaderboard: أرشفة الخام + تسجيل الفشل (مستوى الدورة) ---
class _StubLB:
    """كاش وهمي بحالة مُعدّة سلفاً — بلا شبكة."""

    def __init__(self, stale, refreshed, raw=None):
        self._stale = stale
        self._refreshed = refreshed
        self.last_raw = raw

    def is_stale(self, now_mono):
        return self._stale

    async def maybe_refresh(self, now_mono):
        return self._refreshed


class _StubDB:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.snapshots: list[tuple[str, object]] = []

    def set_meta(self, k, v):
        self.meta[k] = v

    def insert_snapshot(self, source, raw, recorded_at):
        self.snapshots.append((source, raw))


async def test_refresh_leaderboard_archives_raw_on_success():
    db = _StubDB()
    lb = _StubLB(stale=True, refreshed=True, raw={"responseObject": {"leaderboard": []}})
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == [("leaderboard", {"responseObject": {"leaderboard": []}})]
    assert "last_error_leaderboard" not in db.meta


async def test_refresh_leaderboard_failure_is_recorded_not_silent():
    """تحديث مستحقّ فشل ⇒ الخريطة تبقى قديمة — يجب أن يُرى في meta لا أن يصمت."""
    db = _StubDB()
    lb = _StubLB(stale=True, refreshed=False)
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == []
    assert "last_error_leaderboard" in db.meta


async def test_refresh_leaderboard_fresh_cache_does_nothing():
    db = _StubDB()
    lb = _StubLB(stale=False, refreshed=False)
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == [] and db.meta == {}
