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
