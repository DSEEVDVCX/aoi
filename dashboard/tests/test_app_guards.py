"""حراس HTTP في اللوحة: Host guard وCSRF، وحدودُ قسم إدارة المفاتيح.

اللوحة تقرأ القاعدة بـ`mode=ro` ولا تكتب فيها أبداً؛ كتابتُها الوحيدة ملفُّ
`recorder/chain_keys.json` عبر `keystore`. والحارسان يحرسان تلك المسارات: الأوّل
يمنع DNS rebinding عن كل مسار، والثاني يرفض أي طلب يغيّر الحالة بلا رمز. نختبر
CSRF على مسار غير موجود أيضاً — الوسيط يسبق التوجيه، فلو رجع 404 بدل 403 فقد سقط
الحارس عن كلّ مسارٍ يُضاف لاحقاً.

**كلُّ اختبارٍ يلمس المفاتيح يستعمل `keys_file`**: بلا ذلك تكتب الاختبارات في
ملفّ الأسرار الحقيقيّ على هذا الجهاز وتحذف مفاتيحَ تعمل.
"""
import hashlib
import json
import re
import secrets

import pytest
from fastapi.testclient import TestClient

import app as dashboard_app
import config
import keystore

HOST = {"Host": "127.0.0.1:8090"}

# مفتاحٌ مزيّف طويلٌ بما يكفي لظهور ذيله (12 حرفاً على الأقلّ)، ومميَّزٌ كي يمسك
# فحصُ التسريب أيَّ ظهورٍ له في الاستجابة.
FAKE = "AAAABBBBCCCCDDDDwxyz"


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    """يحوّل كلّ قراءةٍ وكتابةٍ إلى ملفٍّ مؤقّت، ويصفّي ذاكرةَ الفحوص."""
    path = tmp_path / "chain_keys.json"
    monkeypatch.setattr(config, "CHAIN_KEYS_PATH", str(path))
    monkeypatch.setattr(keystore.config, "CHAIN_KEYS_PATH", str(path))
    for name in ("HELIUS_API_KEY", "NODEREAL_API_KEY", "GOLDRUSH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    keystore._PROBES.clear()
    return path


@pytest.fixture
def signed(monkeypatch):
    """عميلٌ يحمل الرمز وHost وOrigin — أي طلبٍ يغيّر الحالة يحتاج الثلاثة."""
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    token = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)
    client.headers.update(
        {**HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": token},
    )
    return client


def test_host_guard_rejects_dns_rebinding_name():
    client = TestClient(dashboard_app.app)
    response = client.get("/api/counts", headers={"Host": "evil.example:8090"})
    assert response.status_code == 403


def test_index_serves_a_fresh_csrf_token():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', response.text)


def test_index_refreshes_a_stale_dashboard_token_before_retrying_mutations():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)

    assert response.status_code == 200
    assert "refreshDashboardToken" in response.text
    assert "retried" in response.text
    assert "token_refresh=" in response.text


def test_a_restarted_dashboard_hands_an_open_page_a_token_that_works(monkeypatch):
    """الميزةُ تُقاس بعملها، لا بوجود اسمها في الصفحة.

    الاختبارُ أعلاه يتحقّق أنّ `refreshDashboardToken` و`token_refresh=` مكتوبةٌ
    في الصفحة — وهو يمرّ حتى لو لم تعمل الميزةُ أصلاً. وهي تتعلّق بعقدين صامتين
    ينكسران بلا صوت: أنّ نصَّ خطأ الحارس يحتوي «رمز حماية اللوحة» بعينه (فهذا
    شرطُ إعادة المحاولة في الصفحة، وتغييرُ كلمةٍ في الرسالة يُبطله)، وأنّ الصفحةَ
    المُعادة تُقرأ بنمطِ الاستخراج نفسه. فنمشي المسارَ كما تمشيه الصفحة.

    والرمزُ يُولَد مرّةً عند الاستيراد، فالحالةُ الوحيدة التي تحتاج التحديثَ هي
    إقلاعُ اللوحة من جديد — وهي نفسُ حالةِ [`_ID_SALT`] في `keystore`.
    """
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    stale = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)

    monkeypatch.setattr(dashboard_app, "_DASHBOARD_TOKEN", secrets.token_hex(32))

    refused = client.post("/api/anything", json={}, headers={
        **HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": stale,
    })
    assert refused.status_code == 403
    assert "رمز حماية اللوحة" in refused.json()["error"]

    # المسارُ الذي تجلبه `refreshDashboardToken`، والنمطُ الذي تستخرج به.
    refreshed = client.get("/?token_refresh=1", headers=HOST)
    fresh = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', refreshed.text).group(1)
    assert fresh != stale

    retried = client.post("/api/anything", json={}, headers={
        **HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": fresh,
    })
    assert retried.status_code == 404      # مرّ الحارسَ: 404 توجيهٍ لا 403 حرس


def test_index_exposes_separate_dashboard_views():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)

    assert response.status_code == 200
    for view in ("overview", "signals", "watchlist", "performance", "health", "networks"):
        assert f'data-view-link="{view}"' in response.text
        assert f'data-view-section="{view}"' in response.text


def test_network_view_distinguishes_no_active_watches_from_bad_data():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)

    assert response.status_code == 200
    assert 'total === 0' in response.text
    assert '"لا مراقبات نشطة"' in response.text


def test_state_changing_request_without_token_is_refused():
    client = TestClient(dashboard_app.app)
    response = client.post("/api/anything", headers=HOST, json={})
    assert response.status_code == 403
    assert "رمز حماية اللوحة" in response.json()["error"]


def test_state_changing_request_with_token_passes_the_guard():
    """بالرمز الصحيح يصل الطلب إلى التوجيه (404 لا 403) — الحارس ليس حجراً دائماً."""
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    token = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)
    response = client.post(
        "/api/anything",
        headers={**HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": token},
        json={},
    )
    assert response.status_code == 404


def test_provider_key_panel_reports_state_without_the_key_value(keys_file):
    """الحدُّ المسموح: اسمُ الحساب وآخرُ أربعة أحرف — ولا حرفاً خامساً.

    عرضُ الذيل خفضٌ مقصودٌ لِـ FR-013 طلبه المستخدم للتمييز بين مفتاحين، وهذا
    الاختبار هو حدُّه: يُثبت أنّ الذيل يظهر، وأنّ ما قبله لا يظهر بأيّ طول.
    """
    keys_file.write_text(json.dumps({"helius_api_keys": [
        {"key": FAKE, "label": "حسابي الرئيسي"},
    ]}), encoding="utf-8")
    client = TestClient(dashboard_app.app)

    response = client.get("/api/provider-keys", headers=HOST)
    assert response.status_code == 200
    raw = response.text
    body = response.json()

    row = next(r for r in body["keys"] if r["provider"] == "helius")
    assert row["tail"] == "wxyz"          # الذيل المسموح
    assert row["label"] == "حسابي الرئيسي"  # اسمُ الحساب الذي جُلب منه
    assert "key" not in row

    # لا القيمة ولا أيُّ بدايةٍ منها ولا أيُّ ذيلٍ أطول من أربعة.
    assert FAKE not in raw
    for size in range(4, len(FAKE)):
        assert FAKE[:size] not in raw
    for size in range(5, len(FAKE)):
        assert FAKE[-size:] not in raw


def test_pool_rows_stay_counts_and_indices_only(keys_file):
    """أسطرُ الأحواض تُقرأ من `meta` وتصلح للسجلّ: أعدادٌ ومؤشّرات بلا استثناء."""
    client = TestClient(dashboard_app.app)
    allowed = {
        "owner", "provider", "keys", "blocked", "available", "index", "blocked_index",
        "rotations", "cooldown_seconds", "disabled", "at", "age_seconds",
        "stale", "level",
    }

    body = client.get("/api/provider-keys", headers=HOST).json()
    assert set(body) == {"pools", "min_keys", "keys", "providers"}
    for row in body["pools"]:
        assert set(row) <= allowed
        for field in ("keys", "blocked", "available", "index", "rotations"):
            assert isinstance(row[field], int)
        assert all(isinstance(index, int) for index in row["blocked_index"])


def test_key_mutations_need_the_csrf_token(keys_file):
    """أخطرُ ما في اللوحة الآن هذه المسارات الثلاثة: بلا رمزٍ لا تُلمس."""
    client = TestClient(dashboard_app.app)
    for path in ("add", "toggle", "delete", "test"):
        response = client.post(f"/api/provider-keys/{path}", headers=HOST, json={})
        assert response.status_code == 403, path
    assert not keys_file.exists()


def test_adding_a_key_writes_the_file_and_hides_the_value(keys_file, signed):
    """الإضافة تُخزَّن في الملفّ، والجواب لا يعيد إلّا الذيل."""
    response = signed.post("/api/provider-keys/add", json={
        "provider": "helius", "key": FAKE, "label": "الرئيسي",
    })
    assert response.status_code == 200
    assert response.json()["tail"] == "wxyz"
    assert FAKE not in response.text

    stored = json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]
    assert stored == [{"key": FAKE, "label": "الرئيسي", "enabled": True}]


def test_adding_a_duplicate_or_a_malformed_key_is_refused(keys_file, signed):
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE, "label": ""})

    again = signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    assert again.status_code == 409

    short = signed.post("/api/provider-keys/add", json={"provider": "helius", "key": "abc"})
    assert short.status_code == 400

    # سطرٌ كامل مُلصَق بالخطأ (فيه فراغ) لا يصير مفتاحاً ميّتاً في الحوض.
    pasted = signed.post("/api/provider-keys/add", json={
        "provider": "helius", "key": "HELIUS_API_KEY = abcdef123456",
    })
    assert pasted.status_code == 400

    unknown = signed.post("/api/provider-keys/add", json={"provider": "nope", "key": FAKE})
    assert unknown.status_code == 404


def test_pausing_a_key_keeps_it_on_disk_but_out_of_the_pool(keys_file, signed):
    """الإيقاف المؤقّت لا يفقد القيمة — وإلّا لم يكن مؤقّتاً."""
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    off = signed.post("/api/provider-keys/toggle", json={
        "provider": "helius", "slot": 0, "tail": "wxyz", "enabled": False,
    })
    assert off.status_code == 200
    # آخرُ مفتاحٍ مفعّل ⇒ تحذيرٌ صريح: الطبقة ستتوقّف.
    assert "لم يبقَ" in off.json()["warning"]

    stored = json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]
    assert stored[0]["key"] == FAKE and stored[0]["enabled"] is False

    row = next(r for r in signed.get("/api/provider-keys").json()["keys"]
               if r["provider"] == "helius")
    assert (row["enabled"], row["level"], row["pool_slot"]) == (False, "off", None)


def test_deleting_a_key_removes_it(keys_file, signed):
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    response = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 0, "tail": "wxyz",
    })
    assert response.status_code == 200
    assert json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"] == []


def test_a_stale_page_cannot_delete_the_wrong_key(keys_file, signed):
    """بين الرسم والضغط قد يتغيّر الملفّ؛ الموضعُ وحده كان سيحذف السليم.

    نطابق الذيلَ الذي رُسم به السطر: لو تغيّر ⇒ 409 لا حذفٌ صامتٌ لجارٍ.
    """
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": "OTHERKEYVALUE9999"})

    wrong = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 0, "tail": "9999",
    })
    assert wrong.status_code == 409
    assert "أعد تحميل" in wrong.json()["error"]

    missing = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 7, "tail": "wxyz",
    })
    assert missing.status_code == 409
    stored = json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]
    assert len(stored) == 2


def test_an_environment_variable_beats_the_file_and_the_panel_says_so(keys_file, signed, monkeypatch):
    """تحريرُ الملفّ بلا أثرٍ هو أسوأُ فشل: يبدو ناجحاً ولا يفعل شيئاً."""
    monkeypatch.setenv("HELIUS_API_KEY", "from-environment")

    refused = signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    assert refused.status_code == 409
    assert "HELIUS_API_KEY" in refused.json()["error"]

    provider = next(p for p in signed.get("/api/provider-keys").json()["providers"]
                    if p["provider"] == "helius")
    assert provider["env"] is True


def test_the_test_button_reports_a_rejected_key_without_echoing_it(keys_file, signed, monkeypatch):
    """401 من المزوّد كثيراً ما يعيد الرابطَ وفيه المفتاح — يُشطب قبل العرض."""
    class _Response:
        status_code = 401

        def json(self):
            return {"error": f"unauthorized for https://x/?api-key={FAKE}"}

    class _Client:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def request(self, *_a, **_k): return _Response()

    monkeypatch.setattr(keystore.httpx, "Client", _Client)
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})

    response = signed.post("/api/provider-keys/test", json={
        "provider": "helius", "slot": 0, "tail": "wxyz",
    })
    assert response.status_code == 200
    assert response.json()["level"] == "bad"
    assert FAKE not in response.text

    # النتيجة تبقى معروضة بعد إعادة الرسم — وإلّا ضاعت بعد 10 ثوانٍ.
    row = next(r for r in signed.get("/api/provider-keys").json()["keys"]
               if r["provider"] == "helius")
    assert row["level"] == "bad" and row["probe"]["status"] == 401


def test_a_broken_provider_is_not_blamed_on_the_key(keys_file, signed, monkeypatch):
    """502 ليست «مفتاحٌ فاسد»: نقطةٌ حمراء هنا تدفع المستخدم لحذف مفتاحٍ سليم."""
    class _Response:
        status_code = 502

        def json(self):
            return {}

    class _Client:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def request(self, *_a, **_k): return _Response()

    monkeypatch.setattr(keystore.httpx, "Client", _Client)
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})

    result = signed.post("/api/provider-keys/test", json={
        "provider": "helius", "slot": 0, "tail": "wxyz",
    }).json()
    assert result["level"] == "warn"
    assert "ليس المفتاح" in result["detail"]


def test_a_jsonrpc_error_under_http_200_is_not_called_healthy(keys_file, signed, monkeypatch):
    """JSON-RPC يردّ 200 وفيه `error`؛ «200 ⇒ سليم» كانت ستُخضِّر مفتاحاً مرفوضاً."""
    class _Response:
        status_code = 200

        def json(self):
            return {"error": {"code": -32600, "message": "invalid api key"}}

    class _Client:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def request(self, *_a, **_k): return _Response()

    monkeypatch.setattr(keystore.httpx, "Client", _Client)
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})

    result = signed.post("/api/provider-keys/test", json={
        "provider": "helius", "slot": 0, "tail": "wxyz",
    }).json()
    assert result["level"] == "bad"
    assert "invalid api key" in result["detail"]


def test_goldrush_error_false_under_http_200_is_healthy(keys_file, signed, monkeypatch):
    class _Response:
        status_code = 200

        def json(self):
            return {"error": False, "data": {"items": []}}

    class _Client:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def request(self, *_a, **_k): return _Response()

    monkeypatch.setattr(keystore.httpx, "Client", _Client)
    signed.post("/api/provider-keys/add", json={
        "provider": "goldrush", "key": FAKE,
    })

    result = signed.post("/api/provider-keys/test", json={
        "provider": "goldrush", "slot": 0, "tail": "wxyz",
    }).json()

    assert result["level"] == "good"
    assert result["detail"] == "سليم"


def test_probe_results_do_not_cross_keys_with_the_same_tail(keys_file, signed, monkeypatch):
    first = "AAAAAAAAAAAAwxyz"
    second = "BBBBBBBBBBBBwxyz"

    class _Response:
        status_code = 401

        def json(self):
            return {"error": "rejected"}

    class _Client:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def request(self, *_a, **_k): return _Response()

    monkeypatch.setattr(keystore.httpx, "Client", _Client)
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": first})
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": second})
    rows = [row for row in signed.get("/api/provider-keys").json()["keys"]
            if row["provider"] == "helius"]
    signed.post("/api/provider-keys/test", json={
        "provider": "helius", "slot": 0, "tail": "wxyz",
        "key_id": rows[0]["key_id"],
    })

    rows = [row for row in signed.get("/api/provider-keys").json()["keys"]
            if row["provider"] == "helius"]
    assert rows[0]["probe"]["status"] == 401
    assert rows[1]["probe"] is None


def test_opaque_key_id_disambiguates_mutations_with_the_same_tail(keys_file, signed):
    first = "AAAAAAAAAAAAwxyz"
    second = "BBBBBBBBBBBBwxyz"
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": first})
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": second})
    rows = [row for row in signed.get("/api/provider-keys").json()["keys"]
            if row["provider"] == "helius"]

    assert rows[0]["key_id"] != rows[1]["key_id"]
    assert first not in rows[0]["key_id"] and second not in rows[1]["key_id"]
    response = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 1, "tail": "wxyz",
        "key_id": rows[1]["key_id"],
    })

    assert response.status_code == 200
    stored = json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]
    assert [row["key"] for row in stored] == [first]


def test_the_published_key_id_is_not_derived_from_the_key(keys_file, signed):
    """القاعدة: لا قيمة، ولا شَظيّة، ولا **بصمة**. وهذا الحرسُ يُثبّتها.

    `sha256(key)` تؤدّي وظيفةَ التمييز نفسَها، فالإغراءُ حقيقيّ — وهي تخالف
    القاعدة: تصلح مِحكّاً يؤكّد به مَن يملك قائمةَ مفاتيحٍ مرشَّحة أيَّها
    المستعمل هنا، وتصلح رابطاً يُطابق نفسَ المفتاح بين نظامين. والملحُ العشوائيّ
    يقطع الاثنين، لكنّه سطرٌ يسهل «تبسيطُه» بعد سنة — فنُثبّته باختبار.
    """
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    row = next(item for item in signed.get("/api/provider-keys").json()["keys"]
               if item["provider"] == "helius")

    forbidden = {
        hashlib.sha256(FAKE.encode()).hexdigest(),
        hashlib.sha256(FAKE.strip().encode()).hexdigest(),
        hashlib.md5(FAKE.encode()).hexdigest(),  # noqa: S324 — نمنعه لا نستعمله
        hashlib.sha1(FAKE.encode()).hexdigest(),  # noqa: S324 — نمنعه لا نستعمله
    }
    assert row["key_id"]
    assert not any(row["key_id"] == digest or row["key_id"] == digest[:32]
                   for digest in forbidden)
    # ولا شَظيّة: الذيلُ وحده هو المسموح، والهويّةُ لا تحمل منه شيئاً.
    assert FAKE[:8] not in row["key_id"]


def test_key_ids_die_with_the_process_so_a_stale_page_is_refused(keys_file, signed):
    """الملحُ في الذاكرة: بعد الإقلاع تبطل الهويّاتُ القديمة بـ409 لا بحذفٍ خطأ."""
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    row = next(item for item in signed.get("/api/provider-keys").json()["keys"]
               if item["provider"] == "helius")
    stale_id = row["key_id"]

    keystore._ID_SALT = secrets.token_bytes(32)      # كما لو أُقلعت اللوحةُ من جديد
    response = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 0, "tail": "wxyz", "key_id": stale_id,
    })

    assert response.status_code == 409
    assert json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]


def test_stale_pool_report_does_not_mark_a_key_as_currently_cooled(keys_file):
    """اللحظيُّ يسقط بالتقادم، والإعداديُّ يبقى.

    تبريدُ موضعٍ واستعمالُه ينتهيان مع الدورة، فتقريرٌ بائتٌ لا يشهد بهما.
    أمّا تعطيلُ المزوّد ومَن يملكه فوصفُ إعدادٍ — إسقاطُه كان سيقول «سليم» عن
    مزوّدٍ مُطفأ، وهو الكذبُ المعاكس.
    """
    keys_file.write_text(json.dumps({"helius_api_keys": [FAKE]}), encoding="utf-8")
    view = keystore.rows([{
        "provider": "helius", "owner": "chain", "keys": 1,
        "blocked_index": [0], "index": 0, "disabled": True, "stale": True,
    }])

    row = next(item for item in view["keys"] if item["provider"] == "helius")
    assert row["cooled_by"] == []
    assert row["in_use"] is False
    assert row["provider_disabled"] is True
    provider = next(item for item in view["providers"] if item["provider"] == "helius")
    assert provider["disabled_by"] == ["chain"]
    assert provider["owners"] == ["chain"]


def test_health_view_shows_the_provider_key_panel():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)

    assert response.status_code == 200
    assert 'id="provider-keys"' in response.text
    # النموذج مكتوبٌ في HTML لا مولَّدٌ بـJS: إعادةُ الرسم كل 10ث كانت ستمحو
    # مفتاحاً نصفَ ملصوق.
    assert 'id="key-add"' in response.text
    assert 'id="key-value" type="password"' in response.text
    assert "مفاتيح المزوّدين الخارجيّين" in response.text
    # التصريح معروضٌ للقارئ لا في التعليقات وحدها، ومطابقٌ لما يحدث فعلاً.
    assert "القيمة لا تُعرض ولا تُسجَّل" in response.text
