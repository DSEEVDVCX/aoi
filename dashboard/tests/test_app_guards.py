"""حراس HTTP في اللوحة: Host guard وCSRF.

اللوحة صارت قراءةً محضة بعد إزالة إدارة مفاتيح Helius، لكنّ الحارسين يبقيان:
الأوّل يمنع DNS rebinding عن كل مسار، والثاني يرفض أي طلب يغيّر الحالة. نختبر
CSRF على مسار غير موجود عن قصد — الوسيط يسبق التوجيه، فلو رجع 404 بدل 403 فقد
سقط الحارس ولن ننتبه إلا بعد إضافة أوّل مسار كتابة.
"""
import re

from fastapi.testclient import TestClient

import app as dashboard_app

HOST = {"Host": "127.0.0.1:8090"}


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


def test_provider_key_panel_is_read_only_and_reports_counts_only():
    """قسمُ المفاتيح عرضٌ لا إدارة (FR-013).

    إدارةُ مفاتيح Helius أُزيلت من اللوحة عن قصد (انظر مقدّمة هذا الملف)، وهذا
    القسم لا يعيدها: مسارُ قراءةٍ واحد، وحقولُه أعدادٌ ومؤشّرات بأسماء معروفة
    مسبقاً — فلو أضاف أحدهم لاحقاً بصمةً أو مقطعاً من مفتاح سقط هذا الاختبار.
    """
    client = TestClient(dashboard_app.app)
    allowed = {
        "owner", "provider", "keys", "blocked", "available", "index",
        "rotations", "cooldown_seconds", "disabled", "at", "age_seconds",
        "stale", "level",
    }

    response = client.get("/api/provider-keys", headers=HOST)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"pools", "min_keys"}
    for row in body["pools"]:
        assert set(row) <= allowed
        # كل قيمةٍ عددٌ أو علمٌ أو اسمٌ قصير معروف — لا سلسلةٌ طويلة تشبه مفتاحاً.
        for field in ("keys", "blocked", "available", "index", "rotations"):
            assert isinstance(row[field], int)

    # ولا سبيل للكتابة عبره: الحارس يرفض قبل التوجيه.
    assert client.post("/api/provider-keys", headers=HOST, json={}).status_code == 403


def test_health_view_shows_the_provider_key_panel():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)

    assert response.status_code == 200
    assert 'id="provider-keys"' in response.text
    assert "مفاتيح المزوّدين الخارجيّين" in response.text
    # التصريح معروضٌ للقارئ لا في التعليقات وحدها.
    assert "لا تُعرض قيمة مفتاح ولا جزء منها" in response.text
