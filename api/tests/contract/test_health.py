from fomo_api.auth.credential_store import CredentialStore, StoredCredentials
from fomo_api.config import settings


async def test_health_reports_missing_credentials_as_degraded(app_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "credential_state_file", str(tmp_path / "missing.json"))
    async with app_client as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "redis": "ok",
        "credentials": "unavailable",
    }


async def test_health_reports_unattended_upstream_credentials_ready(
    app_client, tmp_path, monkeypatch
):
    path = tmp_path / "credentials.json"
    CredentialStore(str(path)).save(
        StoredCredentials(
            access_token="access",
            refresh_token="refresh",
            pat="pat",
            app_id="app",
        )
    )
    monkeypatch.setattr(settings, "credential_state_file", str(path))
    async with app_client as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "redis": "ok",
        "credentials": "ready",
    }
