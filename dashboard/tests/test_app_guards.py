"""حراس HTTP في اللوحة: Host guard وCSRF، وحدودُ قسم إدارة المفاتيح.

اللوحة تقرأ القاعدة بـ`mode=ro` ولا تكتب فيها أبداً؛ كتابتُها الوحيدة ملفُّ
`recorder/chain_keys.json` عبر `keystore`. والحارسان يحرسان تلك المسارات: الأوّل
يمنع DNS rebinding عن كل مسار، والثاني يرفض أي طلب يغيّر الحالة بلا رمز. نختبر
CSRF على مسار غير موجود أيضاً — الوسيط يسبق التوجيه، فلو رجع 404 بدل 403 فقد سقط
الحارس عن كلّ مسارٍ يُضاف لاحقاً.

**كلُّ اختبارٍ يلمس المفاتيح يستعمل `keys_file`**: بلا ذلك تكتب الاختبارات في
ملفّ الأسرار الحقيقيّ على هذا الجهاز وتحذف مفاتيحَ تعمل.
"""
import json
import re

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

