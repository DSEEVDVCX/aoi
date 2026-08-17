"""اختبارات تدوير التوكن في المسجّل (بلا شبكة).

يثبت أنّ المسجّل يلتقط التوكن المتجدّد على القرص كل دورة ويعيد بناء العميل عند
تغيّره — وهو الإصلاح الدائم لانتهاء الجلسة كل ساعة (60 دقيقة عمر التوكن) بلا أي
تسجيل دخول يدوي. لا شبكة، لا قيمة توكن مطبوعة.
"""
import asyncio
import sqlite3

import pytest

import recorder


class _FakeClient:
    """عميل وهمي يسجّل التوكن الذي بُني به وما إذا أُغلق."""

    instances: list["_FakeClient"] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False
        _FakeClient.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


class _FakeCache:
    def __init__(self, client):
        self.client = client
        self.set_client_calls = 0

    def set_client(self, client):
        self.client = client
        self.set_client_calls += 1


class _FakeDB:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.closed = False

    def set_meta(self, k, v):
        self.meta[k] = v

    def note_error(self, k, v):
        """يحاكي `RecorderDB.note_error`: يكتب عبر set_meta ولا يرفع أبداً."""
        try:
            self.set_meta(k, v)
            return True
        except Exception:  # noqa: BLE001
            return False

    def bump_counter(self, k, n=1):
        pass

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_instances():
    _FakeClient.instances = []
    yield
    _FakeClient.instances = []


@pytest.fixture()
def patched(monkeypatch):
    """يبدّل _build_client بعميل وهمي ويكتم _log (لا نكتب ملف السجلّ)."""
    monkeypatch.setattr(recorder, "_build_client", lambda tok: _FakeClient(tok))
    monkeypatch.setattr(recorder, "_log", lambda msg: None)
    return monkeypatch


def _rotate(client, token, lb, db):
    return asyncio.run(recorder._maybe_rotate_client(client, token, lb, db))


def test_no_rotation_when_token_unchanged(patched, monkeypatch):
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_A")
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is c0                # نفس العميل
    assert token == "TOK_A"
    assert c0.closed is False           # لم يُغلق
    assert lb.set_client_calls == 0
    assert "last_token_refresh_at" not in db.meta


def test_rotation_rebuilds_client_and_closes_old(patched, monkeypatch):
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_B")
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is not c0             # عميل جديد
    assert client.token == "TOK_B"      # بالتوكن الجديد
    assert token == "TOK_B"
    assert c0.closed is True            # القديم أُغلق
    assert lb.set_client_calls == 1     # الكاش وُجّه للجديد
    assert lb.client is client
    assert "last_token_refresh_at" in db.meta


def test_disk_read_failure_keeps_current_client(patched, monkeypatch):
    def _boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(recorder, "_load_access_token", _boom)
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is c0                 # نُكمل بالحالي
    assert token == "TOK_A"
    assert c0.closed is False
    assert "last_error_token_reload" in db.meta   # سُجّل الخطأ (بلا قيمة سرّية)


def test_old_client_close_error_does_not_break_rotation(patched, monkeypatch):
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_B")

    class _BadClose(_FakeClient):
        async def aclose(self):
            raise RuntimeError("close failed")

    c0 = _BadClose("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client.token == "TOK_B"      # التدوير نجح رغم فشل الإغلاق
    assert token == "TOK_B"
    assert lb.client is client


def test_meta_key_never_contains_token_value(patched, monkeypatch):
    """درع أمني: قيمة meta لا تحوي التوكن — فقط ختماً زمنياً."""
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "SECRET_TOKEN_VALUE")
    c0 = _FakeClient("OLD")
    lb, db = _FakeCache(c0), _FakeDB()
    _rotate(c0, "OLD", lb, db)
    for v in db.meta.values():
        assert "SECRET_TOKEN_VALUE" not in v


# ---------------------------------------------------------------------------
# قاعدة مقفلة لا تُخرج العمليّة (قِيس 2026-08-17)
#
# `database is locked` في `insert_holders` رفع الاستثناء أيضاً من معالج الخطأ
# الذي يكتب وصفَ الفشل، ثم من `bump_counter("cycle_crashes")` في درع الحلقة
# نفسه — فخرج المسجّل بالرمز 1 وبقيت المهمّة `Ready` ثلاث ساعات صامتة. الدرع
# لا يجوز أن يموت بيده.
# ---------------------------------------------------------------------------
class _LockedDB(_FakeDB):
    """تقبل أوّل `accept` كتابةً ثم ترفض كلّ ما بعدها كقاعدةٍ مقفلة."""

    def __init__(self, *, accept: int = 0):
        super().__init__()
        self.attempts = 0
        self._accept = accept

    def set_meta(self, k, v):
        self.attempts += 1
        if self.attempts > self._accept:
            raise sqlite3.OperationalError("database is locked")
        super().set_meta(k, v)

    def bump_counter(self, k, n=1):
        raise sqlite3.OperationalError("database is locked")


def test_rotation_survives_a_locked_database(patched, monkeypatch):
    """الختم دفترٌ لا قياس: القفل لا يهدر عميلاً بُني فعلاً بالتوكن الجديد."""
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_B")
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _LockedDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client.token == "TOK_B"                  # التدوير تمّ رغم القفل
    assert token == "TOK_B"
    assert lb.client is client
    assert "last_token_refresh_at" not in db.meta   # الختم وحده فُقد


def test_token_reload_note_survives_a_locked_database(patched, monkeypatch):
    """معالج فشل القراءة كان يكتب في القاعدة عارياً — قفلُها كان يُخرج العمليّة."""
    def _boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(recorder, "_load_access_token", _boom)
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _LockedDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is c0                             # نُكمل بالحالي
    assert token == "TOK_A"


def test_locked_database_does_not_end_the_cycle_loop(patched, monkeypatch):
    """الانحدار بعينه: ثلاث دورات ساقطة تُكمل الحلقة ولا تُخرج العمليّة."""
    db = _LockedDB(accept=3)          # أختام الإقلاع الثلاثة تمرّ ثمّ يُقفل
    ran = []

    async def _crashing_cycle(*_a, **_k):
        ran.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder, "RecorderDB", lambda *a, **k: db)
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_A")
    monkeypatch.setattr(recorder, "LeaderboardCache", lambda client, **k: _FakeCache(client))
    monkeypatch.setattr(recorder, "run_cycle", _crashing_cycle)
    monkeypatch.setattr(recorder.config, "CYCLE_SECONDS", 0)

    asyncio.run(recorder.main_loop(cycles=3))

    assert len(ran) == 3              # الثلاث تمّت: الدرع نجا من قفل عدّاده
    assert db.closed is True          # وخرجت الحلقة بنظافة لا بانفجار
