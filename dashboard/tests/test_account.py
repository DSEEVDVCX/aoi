"""تبديلُ حساب fomo من اللوحة: ما يُرفض قبل الكتابة، وما يبقى قابلاً للرجوع.

هذا مسارُ الكتابة الثاني في اللوحة، وهدفُه ملفٌّ واحد: `api/.privy_state.json`.
والاختباراتُ هنا تحرس ثلاثةَ أشياء لا يحرسها شيءٌ آخر:

1. **لا قيمةَ توكنٍ تخرج.** الاستجابةُ تحمل بصمةَ الهويّة وآخرَ أربعة أحرف، ولا
   تحمل التوكن — وفحصُ التسريب يبحث عن السلسلة كاملةً في الجواب، فلو أُضيف
   حقلٌ لاحقاً يعيدها سقط الاختبار.
2. **لا كتابةَ بلا رجوع.** نسخةٌ تُحفظ قبل كلّ مساس، والاستعادةُ تعمل — لأنّ
   لصقةً خاطئة تُسكِت الجمعَ كلَّه، والحسابُ القديم ليس محفوظاً في مكانٍ آخر.
3. **الجلسةُ المجهولة تُرفض.** Privy يكتب `privy:token` قبل أيّ دخول، فلصقُ
   مخزنٍ قبل الدخول كان يكتب هويّةً لا تملك شيئاً ويقول «تمّ».

وكلُّ اختبارٍ يلمس الملفَّ يعتمد على `isolate_live_state` (تلقائيّة في
`conftest.py`) التي تحوّل `PRIVY_STATE_PATH` إلى المؤقّت. بلا ذلك تكتب
الاختباراتُ فوق اعتماد التشغيل الحقيقيّ على هذا الجهاز.
"""
import base64
import json
import re

import account
import app as dashboard_app
import config
import pytest
from fastapi.testclient import TestClient

HOST = {"Host": "127.0.0.1:8090"}


def jwt(sub: str, exp: int = 4102444800, iat: int | None = None) -> str:
    """توكنٌ مزيّفٌ مقروءُ الحِمل. التوقيعُ نصٌّ حرفيّ — `account` لا يتحقّق منه
    ولا يجوز أن يتحقّق: توكنُ خدمةٍ أخرى ولا نملك مفتاحَها.

    و`iat` يُحذف حين لا يُطلَب لا يُصفَّر: توكناً بلا `iat` يجب أن يسقط إلى
    مقارنةِ `exp`، وصفرٌ صريحٌ يجعله «أقدمَ من كلّ شيء» فيخفي ذلك السقوط."""
    def part(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    claims: dict = {"sub": sub, "exp": exp}
    if iat is not None:
        claims["iat"] = iat
    return f"{part({'alg': 'ES256'})}.{part(claims)}.SiGnAtUrE"


# و`iat` في ALICE مقصود: توكنُ Privy الحقيقيّ يحمله دائماً (قِيس على توكنٍ حيّ
# 2026-08-20)، وحسابُ الملفّ هو ما يُقارَن به كلُّ لصقةٍ لاحقة. ولو تُرك بلا
# `iat` لصار سقوطُ المقارنة إلى `exp` هو المسارَ المُختبَر في كلّ حالة، وبقي
# المسارُ الحقيقيّ — المقارنةُ على `iat` — بلا اختبارٍ واحد.
ALICE = jwt("did:privy:alice00000000000000000", iat=1_700_000_000)
BOB = jwt("did:privy:bob000000000000000000000", iat=1_700_000_000)
ANON = jwt(account._ANON_DID)

APP_ID = "cmt0phbhk00080dla83dtghph"


def dump(token: str, *, refresh: str = "rt-bob-9999", pat: str = "pat-bob-9999") -> dict:
    """مخزنُ متصفّحٍ كما ينسخه المستخدم — بعلامات التنصيص التي يضعها Privy."""
    return {
        "privy:token": f'"{token}"',
        "privy:refresh_token": f'"{refresh}"',
        "privy:pat": f'"{pat}"',
        "privy:ca_id": '"ca-1234"',
        f"privy:{APP_ID}:state": '"{\\"client\\":\\"client-WYabc123456\\"}"',
    }


@pytest.fixture(autouse=True)
def forget_probes():
    """`_LAST_PROBE` ذاكرةُ عمليّة، فتُصفّى بين الاختبارات مثل `keystore._PROBES`."""
    account._LAST_PROBE.clear()
    yield
    account._LAST_PROBE.clear()


@pytest.fixture
def state(monkeypatch, tmp_path):
    """ملفُّ اعتمادٍ قائمٌ لحساب ALICE، ومسارُه مؤقّت."""
    path = tmp_path / "privy" / ".privy_state.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "access_token": ALICE,
        "refresh_token": "rt-alice-0001",
        "pat": "pat-alice-0001",
        "app_id": APP_ID,
        "client_id": "client-WYalice0001",
        "ca_id": "ca-alice",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "PRIVY_STATE_PATH", str(path))
    return path


@pytest.fixture
def signed():
    """عميلٌ يحمل الرمز وHost وOrigin — أي طلبٍ يغيّر الحالة يحتاج الثلاثة."""
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    token = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)
    client.headers.update(
        {**HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": token},
    )
    return client


# --- ما يخرج من الخادم: بصمةٌ لا قيمة ---

def test_status_returns_the_identity_and_never_the_token(state, signed):
    response = signed.get("/api/fomo-account")
    body = response.json()
    assert body["exists"] is True
    assert body["did"] == "did:privy:alice00000000000000000"
    assert body["token_tail"] == ALICE[-4:]
    assert body["refreshable"] is True
    assert body["missing"] == []
    assert ALICE not in response.text
    assert "rt-alice-0001" not in response.text
    assert "pat-alice-0001" not in response.text


def test_status_on_a_missing_file_says_so_instead_of_failing(signed):
    """الغيابُ حالةٌ صالحة: لم يُسجَّل دخولٌ بعد. و500 هنا كان يُفرغ اللوحة."""
    body = signed.get("/api/fomo-account").json()
    assert body["exists"] is False
    assert body["did"] == ""
    assert body["backups"] == []


def test_status_names_what_is_missing_when_the_file_is_partial(state, signed):
    state.write_text(json.dumps({"access_token": ALICE}), encoding="utf-8")
    body = signed.get("/api/fomo-account").json()
    assert body["refreshable"] is False
    assert set(body["missing"]) == {"refresh_token", "pat", "app_id", "client_id", "ca_id"}


def test_a_field_that_has_a_fallback_is_not_reported_as_a_blocking_gap(state, signed):
    """الملفُّ الحقيقيّ على هذا الجهاز بلا `client_id` ويجدّد توكنَه كلَّ دقيقة
    (قِيس 2026-08-20)، لأنّ `api/config.py` يضع المعرّفَ الثابت بديلاً. فإدراجُه
    في النقص المُعطِّل يُشعل اللوحةَ حمراءَ على نظامٍ سليم — وإنذارٌ كاذبٌ
    يُعلّم المستخدمَ تجاهلَ الأحمر."""
    raw = json.loads(state.read_text(encoding="utf-8"))
    del raw["client_id"]
    del raw["ca_id"]
    state.write_text(json.dumps(raw), encoding="utf-8")
    body = signed.get("/api/fomo-account").json()
    assert set(body["missing"]) == {"client_id", "ca_id"}     # الحقيقةُ تبقى معروضة
    assert body["missing_required"] == []                      # ولا إنذار
    assert body["refreshable"] is True


def test_a_field_with_no_fallback_is_reported_as_a_blocking_gap(state, signed):
    raw = json.loads(state.read_text(encoding="utf-8"))
    del raw["pat"]
    state.write_text(json.dumps(raw), encoding="utf-8")
    body = signed.get("/api/fomo-account").json()
    assert body["missing_required"] == ["pat"]
    assert body["refreshable"] is False


def test_an_expired_token_is_reported_as_expired_not_as_absent(state, signed):
    state.write_text(json.dumps({
        "access_token": jwt("did:privy:alice00000000000000000", exp=1600000000),
        "refresh_token": "rt", "pat": "pat", "app_id": APP_ID,
    }), encoding="utf-8")
    body = signed.get("/api/fomo-account").json()
    assert body["expired"] is True
    assert body["seconds_left"] < 0
    assert body["refreshable"] is True     # منتهٍ لكن قابلٌ للتجديد ≠ معطوب


# --- التبديل: ما يُرفض قبل الكتابة ---

def test_switch_writes_the_new_identity_and_keeps_a_backup(state, signed):
    response = signed.post("/api/fomo-account/switch", json=dump(BOB))
    assert response.status_code == 200
    out = response.json()
    assert out["did"] == "did:privy:bob000000000000000000000"
    assert out["backup"].startswith(account._BAK_PREFIX)

    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["access_token"] == BOB
    assert written["refresh_token"] == "rt-bob-9999"
    assert written["pat"] == "pat-bob-9999"
    # ما يقشّره `_clean`: لا علاماتَ تنصيصٍ تصل الملفّ، وإلّا ردّ المصدرُ 401 على
    # توكنٍ صحيح.
    assert not written["access_token"].startswith('"')
    assert written["app_id"] == APP_ID
    assert written["ca_id"] == "ca-1234"

    # النسخةُ تحمل الحسابَ القديم — وهي طريقُ الرجوع الوحيد.
    backup = json.loads((state.parent / out["backup"]).read_text(encoding="utf-8"))
    assert backup["access_token"] == ALICE


def test_switch_never_echoes_the_pasted_token_back(state, signed):
    text = signed.post("/api/fomo-account/switch", json=dump(BOB)).text
    assert BOB not in text
    assert "rt-bob-9999" not in text
    assert "pat-bob-9999" not in text
    assert BOB[-4:] in text          # الذيلُ وحده، وهو استثناء FR-013 المطلوب


def test_switch_refuses_the_anonymous_privy_session(state, signed):
    """Privy يكتب `privy:token` عند إقلاع الـSDK قبل أيّ دخول (قِيس 2026-08-20).

    وهذا أخطرُ رفضٍ في الملفّ: بلا هذا الحرس يقرأ المستخدمُ «تمّ التبديل» ثمّ
    يجد الجمعَ ميتاً، لأنّ الهويّةَ المكتوبة لا تملك شيئاً.
    """
    response = signed.post("/api/fomo-account/switch", json=dump(ANON))
    assert response.status_code == 400
    assert "جلسةٌ مجهولة" in response.json()["error"]
    # ولا يُلمس الملفّ: الرفضُ قبل النسخِ والكتابة.
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE
    assert not list(state.parent.glob(account._BAK_PREFIX + "*"))


def test_switch_refuses_the_same_identity(state, signed):
    response = signed.post("/api/fomo-account/switch", json=dump(ALICE))
    assert response.status_code == 409
    assert not list(state.parent.glob(account._BAK_PREFIX + "*"))


def test_status_reports_how_long_ago_the_file_was_written(state, signed):
    """عمرُ الكتابة يفرّق بين «لا أحدَ يجدّد» و«التجديدُ يعمل ويُرفَض»، وهما
    تشخيصان متناقضان كانت اللوحةُ تعطيهما نصّاً واحداً."""
    body = signed.get("/api/fomo-account").json()
    assert isinstance(body["written_seconds_ago"], int)
    assert body["written_seconds_ago"] <= 5           # كُتب في المُهيّئ الآن


def test_status_reports_no_write_age_when_there_is_no_file(signed):
    """لا ملفَّ ⇒ لا عمرَ. وصفرٌ هنا كان سيعني «كُتب الآن» فيقلب الحكم."""
    assert signed.get("/api/fomo-account").json()["written_seconds_ago"] is None


def test_the_same_identity_with_a_newer_token_is_a_refresh_not_a_repeat(state, signed):
    """لصقُ دخولٍ جديد لنفس الحساب هو طريقُ النجاة حين يتعطّل تجديدُ Privy.

    وكان يُرفض 409 «لا شيء ليُبدَّل»، فمن تعطّلت جلستُه وسجّل دخولاً جديداً
    بنفس حسابه وجد البابَ مغلقاً — والحالةُ ليست نظريّة: حدثت 2026-08-20.
    """
    fresh = jwt("did:privy:alice00000000000000000", iat=1_800_000_000)
    response = signed.post("/api/fomo-account/switch", json=dump(fresh))
    assert response.status_code == 200
    body = response.json()
    assert body["refreshed"] is True
    assert body["did"] == "did:privy:alice00000000000000000"
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == fresh
    assert fresh not in response.text          # البصمةُ تخرج، لا القيمة
    backups = list(state.parent.glob(account._BAK_PREFIX + "*"))
    assert len(backups) == 1 and backups[0].name.endswith("-refresh")


def test_a_refresh_of_the_same_identity_still_keeps_the_old_file(state, signed):
    """التجديدُ يمسّ نفسَ الملفّ، فنسخةُ الرجوع فيه ألزمُ لا أخفّ: لصقةٌ من
    نافذةٍ خاطئة تُسكِت الجمعَ كلَّه ولا نسخةَ للاعتماد القديم في مكانٍ آخر."""
    signed.post(
        "/api/fomo-account/switch",
        json=dump(jwt("did:privy:alice00000000000000000", iat=1_800_000_000)),
    )
    backup = next(iter(state.parent.glob(account._BAK_PREFIX + "*")))
    assert json.loads(backup.read_text(encoding="utf-8"))["access_token"] == ALICE
    assert json.loads(backup.read_text(encoding="utf-8"))["refresh_token"] == "rt-alice-0001"


def test_an_older_token_of_the_same_identity_cannot_overwrite_the_newer_one(state, signed):
    """نافذةٌ قديمةٌ نُسيت مفتوحةً تحمل توكناً منتهياً — ولصقُها كان سيطمس العامل."""
    state.write_text(json.dumps({
        "access_token": jwt("did:privy:alice00000000000000000", iat=1_800_000_000),
        "refresh_token": "rt-alice-0002",
        "pat": "pat-alice-0002",
        "app_id": APP_ID,
        "client_id": "client-WYalice0001",
        "ca_id": "ca-alice",
    }), encoding="utf-8")
    stale = jwt("did:privy:alice00000000000000000", iat=1_700_000_000)
    response = signed.post("/api/fomo-account/switch", json=dump(stale))
    assert response.status_code == 409
    assert "ليس أحدث" in response.json()["error"]
    assert json.loads(state.read_text(encoding="utf-8"))["refresh_token"] == "rt-alice-0002"
    assert not list(state.parent.glob(account._BAK_PREFIX + "*"))


def test_the_same_identity_falls_back_to_exp_when_no_iat_is_issued(state, signed):
    """لا كلَّ من يصدر الـJWT يضع `iat`. فحين يغيب تبقى المقارنةُ على `exp`
    بدل أن تسقط إلى «ليس أحدث» وتردّ 409 على تجديدٍ صحيح."""
    no_iat = jwt("did:privy:alice00000000000000000", exp=4102444800)
    state.write_text(json.dumps({
        "access_token": no_iat,
        "refresh_token": "rt-alice-0001",
        "pat": "pat-alice-0001",
        "app_id": APP_ID,
        "client_id": "client-WYalice0001",
        "ca_id": "ca-alice",
    }), encoding="utf-8")
    fresh = jwt("did:privy:alice00000000000000000", exp=4102444900)
    assert signed.post("/api/fomo-account/switch", json=dump(fresh)).status_code == 200
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == fresh


def test_switch_refuses_a_partial_store_by_name(state, signed):
    partial = dump(BOB)
    del partial["privy:refresh_token"]
    del partial["privy:pat"]
    response = signed.post("/api/fomo-account/switch", json=partial)
    assert response.status_code == 400
    error = response.json()["error"]
    assert "refresh_token" in error and "pat" in error
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE


def test_switch_refuses_a_store_with_no_app_id(state, signed):
    """`app_id` يُستخرج من اسم مفتاحٍ في المخزن، ومن نسخ سطراً واحداً فقده —
    وبلا `app_id` لا تجديدَ: يموت الحساب بعد ساعة بلا سببٍ ظاهر."""
    bare = {k: v for k, v in dump(BOB).items() if ":state" not in k}
    response = signed.post("/api/fomo-account/switch", json=bare)
    assert response.status_code == 400
    assert "app_id" in response.json()["error"]


def test_switch_refuses_an_unreadable_token(state, signed):
    response = signed.post("/api/fomo-account/switch", json=dump("not-a-jwt-at-all"))
    assert response.status_code == 400
    assert "JWT" in response.json()["error"]


def test_switch_accepts_the_full_local_storage_wrapper(state, signed):
    """الشكلُ الذي يفهمه `credential_store` أصلاً — المستخدمُ لا يعرف أيّهما بيده."""
    response = signed.post(
        "/api/fomo-account/switch", json={"_full_localStorage": dump(BOB)},
    )
    assert response.status_code == 200
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == BOB


def test_switch_keeps_old_fields_the_paste_does_not_carry(state, signed):
    """`client_id` لا يظهر في كلّ مخزن، وهو **شرطٌ** لتجديد Privy (بدونه 400).

    فالدمجُ لا الاستبدال: ما لم تحمله اللصقةُ يبقى من الملفّ القديم، لأنّ
    `client_id` معرّفُ تطبيقٍ ثابت لا سرَّ حساب.
    """
    without_state = {k: v for k, v in dump(BOB).items() if ":state" not in k}
    without_state[f"privy:{APP_ID}:x"] = '"{}"'
    signed.post("/api/fomo-account/switch", json=without_state)
    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["access_token"] == BOB
    assert written["client_id"] == "client-WYalice0001"


def test_switch_refuses_a_body_that_is_not_a_store(state, signed):
    assert signed.post("/api/fomo-account/switch", json={}).status_code == 400
    assert signed.post("/api/fomo-account/switch", json=[1, 2]).status_code == 400
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE


# --- الرجوع ---

def test_restore_brings_the_previous_account_back(state, signed):
    name = signed.post("/api/fomo-account/switch", json=dump(BOB)).json()["backup"]
    response = signed.post("/api/fomo-account/restore", json={"name": name})
    assert response.status_code == 200
    assert response.json()["did"] == "did:privy:alice00000000000000000"
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE
    assert ALICE not in response.text


def test_restore_backs_up_first_so_the_switch_is_not_lost(state, signed):
    """الاستعادةُ نفسُها كتابة، ومن استعاد بالخطأ يحتاج طريقَ رجوعٍ أيضاً."""
    first = signed.post("/api/fomo-account/switch", json=dump(BOB)).json()["backup"]
    signed.post("/api/fomo-account/restore", json={"name": first})
    saved = [b["did"] for b in signed.get("/api/fomo-account").json()["backups"]]
    assert "did:privy:bob000000000000000000000" in saved


def test_restore_refuses_a_path_outside_the_backup_set(state, signed):
    """`..` واسمٌ بلا البادئة: لا تُقرأ ملفّاتٌ أخرى ولا تُكتب فوق الاعتماد."""
    for name in ("../../../etc/passwd", ".privy_state.json", "", "bak-2026"):
        response = signed.post("/api/fomo-account/restore", json={"name": name})
        assert response.status_code == 404, name
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE


def test_restore_refuses_a_backup_with_no_token(state, signed):
    empty = state.parent / (account._BAK_PREFIX + "20260101T000000Z-x")
    empty.write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
    response = signed.post("/api/fomo-account/restore", json={"name": empty.name})
    assert response.status_code == 400
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE


def test_backups_are_pruned_to_the_keep_limit(state, signed):
    """النسخُ لا تتراكم بلا حدّ، وأقدمُها يُحذف أوّلاً."""
    for i in range(account._BAK_KEEP + 3):
        token = jwt(f"did:privy:acct{i:020d}")
        signed.post(
            "/api/fomo-account/switch", json=dump(token, refresh=f"rt{i}", pat=f"pat{i}"),
        )
    assert len(list(state.parent.glob(account._BAK_PREFIX + "*"))) <= account._BAK_KEEP


# --- المِجَسّ: يفصل «الهويّةُ محجوبة» عن «المصدرُ متعثّر» ---

@pytest.mark.parametrize(("codes", "level", "needle"), [
    ([200, 200, 200], "good", "يعمل"),
    ([403, 403, 403], "bad", "محجوب"),
    ([401, 200, 200], "bad", "401"),
    ([403, 200, 200], "bad", "جزئيّ"),
    ([503, 502, 500], "warn", "متعثّر"),
    ([None, None, None], "warn", "لا جواب"),
    ([200, 404, 500], "warn", "مختلط"),
])
def test_the_verdict_separates_a_blocked_identity_from_a_sick_upstream(codes, level, needle):
    """قِيس 2026-08-19: الحجبُ 403 على كلّ مسار، والمسجّل كتبه «غيرُ متاح»
    فطُوردت الشبكةُ ساعةً وهي سليمة. فالرمزُ هو الحكم لا نصُّ الخطأ."""
    got_level, detail = account._verdict(codes)
    assert got_level == level
    assert needle in detail


def test_probe_reports_each_path_and_caches_the_result(state, signed, monkeypatch):
    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers") or {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def _reply(self, url):
            assert url.startswith(config.FOMO_UPSTREAM_BASE)
            assert self.headers["authorization"] == f"Bearer {ALICE}"
            return type("R", (), {"status_code": 403})()

        get = _reply

        def post(self, url, json=None):
            return self._reply(url)

    monkeypatch.setattr("curl_cffi.requests.Session", FakeSession)
    response = signed.post("/api/fomo-account/probe", json={})
    body = response.json()
    assert body["level"] == "bad"
    assert [r["status"] for r in body["rows"]] == [403, 403, 403]
    assert ALICE not in response.text
    # ويُخزَّن في الذاكرة كي تعرضه البطاقةُ بعد التحديث بلا نداءٍ ثانٍ.
    assert signed.get("/api/fomo-account").json()["last_probe"]["level"] == "bad"


def test_probe_reads_a_transport_failure_as_no_answer_not_as_a_block(state, signed, monkeypatch):
    """انقطاعُ الشبكة ليس حجباً — والخلطُ يدفع المستخدمَ إلى تبديل حسابٍ سليم."""
    class Dead:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            raise OSError("no route to host")

        def post(self, url, json=None):
            raise OSError("no route to host")

    monkeypatch.setattr("curl_cffi.requests.Session", Dead)
    body = signed.post("/api/fomo-account/probe", json={}).json()
    assert body["level"] == "warn"
    assert "لا جواب" in body["detail"]
    assert all(r["status"] is None for r in body["rows"])


def test_probe_without_a_token_says_so_instead_of_calling_out(signed):
    response = signed.post("/api/fomo-account/probe", json={})
    assert response.status_code == 400
    assert "لا توكن" in response.json()["error"]


def test_switch_clears_a_probe_verdict_from_the_previous_account(state, signed):
    """نتيجةُ الحساب السابق لا تصف الجديد، وبقاؤها معروضةً بعد التبديل كان
    يقول «محجوب» عن حسابٍ لم يُفحص بعد."""
    account._LAST_PROBE.update({"level": "bad", "detail": "محجوب", "rows": []})
    signed.post("/api/fomo-account/switch", json=dump(BOB))
    assert signed.get("/api/fomo-account").json()["last_probe"] is None


# --- الحراس: نفسُ حرسِ المفاتيح، وأشدُّ حاجةً إليه ---

def test_every_write_path_needs_the_csrf_token(state):
    """بلا رمز: 403 قبل التوجيه. صفحةٌ خارجيّة لا تبدّل حسابَ مصدر البيانات."""
    client = TestClient(dashboard_app.app)
    for path in ("switch", "restore", "probe"):
        response = client.post(f"/api/fomo-account/{path}", json={}, headers=HOST)
        assert response.status_code == 403
        assert "رمز حماية اللوحة" in response.json()["error"]
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE


def test_the_paste_path_accepts_a_body_larger_than_the_default_cap(state, signed):
    """مخزنُ متصفّحٍ حقيقيّ يتجاوز 8KB بسهولة — وكان يُرفض بـ«الطلب كبير جداً»
    فيقرأ المستخدمُ رفضاً لا يفهم سببه. والسقفُ لهذا المسار وحده."""
    padded = dump(BOB)
    padded["privy:noise"] = '"' + "x" * 20000 + '"'
    assert signed.post("/api/fomo-account/switch", json=padded).status_code == 200
    # وليس مرفوعاً عن غيره:
    fat = {"provider": "helius", "key": "k" * 9000, "label": "x"}
    assert signed.post("/api/provider-keys/add", json=fat).status_code == 413


def test_a_body_that_is_not_json_is_refused_without_echoing_it(state, signed):
    response = signed.post(
        "/api/fomo-account/switch",
        content=b"privy:token=secret-value-here",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "secret-value-here" not in response.text
