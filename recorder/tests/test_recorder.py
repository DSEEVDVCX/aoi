"""اختبارات تدوير التوكن في المسجّل (بلا شبكة).

يثبت أنّ المسجّل يلتقط التوكن المتجدّد على القرص كل دورة ويعيد بناء العميل عند
تغيّره — وهو الإصلاح الدائم لانتهاء الجلسة كل ساعة (60 دقيقة عمر التوكن) بلا أي
تسجيل دخول يدوي. لا شبكة، لا قيمة توكن مطبوعة.
"""
import asyncio
import sqlite3
from typing import ClassVar

import pytest

import recorder


class _FakeClient:
    """عميل وهمي يسجّل التوكن الذي بُني به وما إذا أُغلق."""

    instances: ClassVar[list["_FakeClient"]] = []

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
        self.recoveries = 0

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

    def recover_connection(self):
        self.recoveries += 1
        return "ok"

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
    # والتسجيلُ وحده لم يكن يكفي: اتّصالٌ عَلِق يُنهي كلّ دورةٍ تالية كما أنهى
    # هذه. 2026-08-19: ثلاثة عشر دورةً متطابقةَ الانهيار، و22 دقيقة و40 ثانية
    # بلا صفٍّ واحد، حتى إعادةِ تشغيلٍ يدويّة.
    assert db.recoveries == 3         # إنقاذٌ لكلّ انهيار، لا واحدٌ للحلقة


# --- قاطعُ الحجب: مقيسٌ على أرقام 2026-08-19T14:53→16:07 الحقيقيّة ---
# عدّاداتُ الدورة 20 (سويّة) و21 (تحوّل) و22 (جزئيّة) و23 (محجوبة) منقولةٌ من
# `recorder.log` كما هي، فالحدُّ يُختبَر على ما وقع فعلاً لا على مثالٍ مُصطنع.
_LIVE_OK = {"signals": 5, "ticks": 122, "bars_rows": 6328, "social_items": 21134,
            "holders_details": 13, "flow_rows": 13, "filter_ticks": 109,
            "traders_rows": 4, "errors": 0}
_LIVE_MIXED = {"signals": 2, "ticks": 124, "bars_rows": 6250, "filter_ticks": 107,
               "holders_details": 13, "traders_rows": 4, "errors": 3}
_LIVE_PARTIAL = {"holders_details": 1, "holders_top": 2, "flow_rows": 1,
                 "filter_requested": 190, "errors": 49}
_LIVE_BLOCKED = {"filter_requested": 190, "errors": 52}


def test_a_dead_cycle_is_errors_with_no_row_written():
    """`filter_requested` ليست ثمرة: هي الرقم الوحيد الذي **ارتفع** وقت الحجب.

    108→190 لأنّ الفلتر يُطلب لكلّ عملةٍ لم تصلها لقطة؛ فلو عُدّ ثمرةً لبقي
    القاطعُ مغلقاً في الحجب الذي وُضع له تحديداً.
    """
    assert recorder._cycle_is_dead(_LIVE_BLOCKED) is True
    assert recorder._cycle_is_dead(_LIVE_OK) is False        # لا أخطاء أصلاً
    assert recorder._cycle_is_dead(_LIVE_MIXED) is False     # أخطاءٌ مع ثمرة
    assert recorder._cycle_is_dead(_LIVE_PARTIAL) is False   # صفٌّ واحد يكفي


def test_the_wait_doubles_to_a_ceiling_and_never_overflows(monkeypatch):
    monkeypatch.setattr(recorder.config, "CYCLE_SECONDS", 60)
    monkeypatch.setattr(recorder.config, "UPSTREAM_BREAKER_AFTER", 3)
    monkeypatch.setattr(recorder.config, "UPSTREAM_BREAKER_MAX_SECONDS", 900)

    waits = [recorder._breaker_wait(k) for k in range(3, 9)]

    assert waits == [60, 120, 240, 480, 900, 900]
    assert recorder._breaker_wait(10_000) == 900   # أُسٌّ لا يرفع OverflowError


def test_the_probe_answers_false_when_the_source_refuses():
    """الحجبُ يرفع استثناءً؛ والمِجَسُّ يترجمه جواباً لا انهياراً."""
    class _Blocked:
        async def _get(self, path, params=None):
            raise RuntimeError("403 you have been blocked")

    class _Open:
        async def _get(self, path, params=None):
            return None            # 404 جوابٌ من الأصل ⇒ الطريقُ مفتوح

    assert asyncio.run(recorder._upstream_alive(_Blocked())) is False
    assert asyncio.run(recorder._upstream_alive(_Open())) is True

    # والسببُ يخرج نصّاً: قرارُ الانتظار واحد، والعلاجُ يفترق عليه.
    why: list[str] = []
    assert asyncio.run(recorder._upstream_alive(_Blocked(), why)) is False
    assert "403" in why[0]
    assert asyncio.run(recorder._upstream_alive(_Open(), why)) is True
    assert len(why) == 1                   # النجاحُ لا يكتب سبباً


def test_the_probe_sends_the_required_feed_types_and_reads_400_as_alive():
    """قِيسَ 2026-08-20T00:24Z بحسابٍ سليم: `/feed?limit=1` وحده يردّ 400.

    `feedTypes` شرطٌ لازمٌ في `/feed`، و`_get` يترجم كلَّ ≥400 إلى «غير متاح»،
    فكان المِجَسُّ يقرأ طريقاً حيّاً ميتاً — أي أنّ القاطعَ يُغلق ولا يُفتح ولو
    رُفع الحجب. وشهد السجلّ: أحدَ عشرَ إغلاقاً بلا استئنافٍ واحد إلّا بإعادة
    تشغيل. فحُكمان: النداءُ يحمل شرطَه، وجوابُ التطبيق بذاته حياة.
    """
    from fomo_api.api.errors import UpstreamUnavailableError

    seen: list[dict] = []

    class _Recording:
        async def _get(self, path, params=None):
            seen.append(dict(params or {}))
            return {"responseObject": {"items": []}}

    assert asyncio.run(recorder._upstream_alive(_Recording())) is True
    assert seen[0].get("feedTypes") == list(recorder.config.FEED_TYPES)
    assert seen[0].get("limit") == 1        # حالةُ الطريق لا حمولتُه

    # 400 = وصل الطلبُ وفُحص ⇒ حياة. ولو أضاف المصدرُ شرطاً غداً لم يَقتُل الجمعَ.
    class _Status:
        def __init__(self, code): self.code = code
        async def _get(self, path, params=None):
            raise UpstreamUnavailableError(details={"upstream_status": self.code})

    why: list[str] = []
    assert asyncio.run(recorder._upstream_alive(_Status(400), why)) is True
    assert asyncio.run(recorder._upstream_alive(_Status(422), why)) is True
    assert why == []                        # الحياةُ لا تكتب سبباً

    # و403 حجبُ هويّة، و5xx انقطاع — لا يُصلحهما تشغيلُ الدورة.
    assert asyncio.run(recorder._upstream_alive(_Status(403), why)) is False
    assert asyncio.run(recorder._upstream_alive(_Status(503), why)) is False
    assert len(why) == 2
    assert "403" in why[0]


def test_the_note_carries_the_status_code_not_the_default_message():
    """قِيسَ 2026-08-19: 403 على الهويّة كُتب «unreachable»، فطُورد DNS ساعةً.

    رسالةُ `UpstreamUnavailableError` الافتراضيّة واحدةٌ للحجب وللانقطاع، وما
    يفرّق بينهما يسكن `details` — فإن سقطت، سقط الفرقُ من اللوحة كلّها.
    """
    from fomo_api.api.errors import UpstreamUnavailableError

    blocked = UpstreamUnavailableError(details={"upstream_status": 403})
    note = recorder._exc_note(blocked)
    assert "403" in note                       # الرمزُ حاضر
    assert "UpstreamUnavailableError" in note   # والاسمُ لم يُفقَد

    # ونصُّ النقل الطويل يُقَصّ: الملاحظةُ صفٌّ في meta لا سجلُّ تنقيب.
    long = recorder._exc_note(UpstreamUnavailableError(details={"reason": "x" * 400}))
    assert len(long) < 260

    # وخطأٌ بلا details يبقى كما كان — لا قوسَ فارغاً.
    assert recorder._exc_note(RuntimeError("boom")) == "RuntimeError: boom"


def _loop_with(monkeypatch, cycle_stats, alive, cycles):
    """يشغّل main_loop بدورةٍ تُعيد عدّاداتٍ معطاة ومِجَسٍّ مُتحكَّم به.

    يعيد (عدد دورات الجمع، عدد نداءات المِجَسّ، القاعدة الوهميّة).
    """
    db = _FakeDB()
    ran: list[int] = []
    probes: list[int] = []

    async def _cycle(*_a, **_k):
        ran.append(1)
        return dict(cycle_stats[min(len(ran) - 1, len(cycle_stats) - 1)])

    async def _probe(_client, _reason=None):
        probes.append(1)
        return alive(len(probes))

    monkeypatch.setattr(recorder, "RecorderDB", lambda *a, **k: db)
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_A")
    monkeypatch.setattr(
        recorder, "LeaderboardCache", lambda client, **k: _FakeCache(client)
    )
    monkeypatch.setattr(recorder, "run_cycle", _cycle)
    monkeypatch.setattr(recorder, "_upstream_alive", _probe)
    monkeypatch.setattr(recorder.config, "CYCLE_SECONDS", 0)  # ⇒ انتظارٌ صفر
    monkeypatch.setattr(recorder.config, "UPSTREAM_BREAKER_AFTER", 3)

    asyncio.run(recorder.main_loop(cycles=cycles))
    return len(ran), len(probes), db


def test_the_breaker_stops_collecting_after_three_dead_cycles(patched, monkeypatch):
    """المقصودُ بعينه: 67 دورةً محجوبة رمت 190 طلباً في الدقيقة وكتبت صفراً.

    بعد الثالثة تصير الدورةُ مِجَسّاً واحداً — فالطَّرْقُ على حائطٍ لا يفتحه،
    وقواعدُ الحجب في Cloudflare تُمدَّد بالطَّرْق المتواصل.
    """
    ran, probes, db = _loop_with(
        monkeypatch, [_LIVE_BLOCKED], lambda _n: False, cycles=9
    )

    assert ran == 3                  # ثلاثٌ ثمّ لا دورةَ جمعٍ بعدها
    assert probes == 6               # وستُّ دوراتٍ صارت ستَّ نداءات
    assert "last_error_upstream_blocked" in db.meta   # والحجبُ مكتوبٌ لا مسكوت


def test_the_first_answering_probe_resumes_full_cycles(patched, monkeypatch):
    """وأوّلُ نجاحٍ يُعيد الجمعَ كاملاً: القاطعُ يُبطئ الطَّرْقَ لا الجمع."""
    ran, probes, _db = _loop_with(
        monkeypatch, [_LIVE_BLOCKED, _LIVE_BLOCKED, _LIVE_BLOCKED, _LIVE_OK],
        lambda n: n >= 3, cycles=8
    )

    assert probes == 3               # مِجَسّان يُرفَضان ثمّ ثالثٌ يُقبَل
    assert ran == 6                  # 3 ميتة + 3 بعد العودة، ولا شيء ضائع


def test_a_two_cycle_outage_never_opens_the_breaker(patched, monkeypatch):
    """سابقةُ سبعة أيام: عشرةُ انقطاعاتٍ كلّها ≤ دورتين. لا يجوز أن تُبطئ شيئاً."""
    ran, probes, _db = _loop_with(
        monkeypatch, [_LIVE_BLOCKED, _LIVE_BLOCKED, _LIVE_OK],
        lambda _n: False, cycles=6
    )

    assert probes == 0               # لم يُفتح القاطعُ أصلاً
    assert ran == 6                  # ولا دورةَ جمعٍ فُقدت
