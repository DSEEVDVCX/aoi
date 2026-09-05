"""The dashboard's HTTP guards: the Host guard and CSRF, and the limits of the key-management section.

The dashboard reads the database with `mode=ro` and never writes to it; its
only write is the `recorder/chain_keys.json` file via `keystore`. The two
guards protect those paths: the first blocks DNS rebinding on every route,
and the second refuses any state-changing request without a token. We also
test CSRF on a nonexistent path — the middleware runs before routing, so if
it answered 404 instead of 403, the guard would have been lost on every path
added later.

**Every test that touches the keys uses `keys_file`**: without it the tests
would write into this machine's real secrets file and delete working keys.
"""
import hashlib
import json
import re
import secrets
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as dashboard_app
import config
import keystore

HOST = {"Host": "127.0.0.1:8090"}

# A fake key long enough for its tail to show (at least 12 characters), and
# distinctive enough that the leak check catches any appearance of it in the
# response.
FAKE = "AAAABBBBCCCCDDDDwxyz"


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    """Points every read and write at a temporary file, and clears the probe memory."""
    path = tmp_path / "chain_keys.json"
    monkeypatch.setattr(config, "CHAIN_KEYS_PATH", str(path))
    monkeypatch.setattr(keystore.config, "CHAIN_KEYS_PATH", str(path))
    for name in ("HELIUS_API_KEY", "NODEREAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    keystore._PROBES.clear()
    return path


@pytest.fixture
def signed(monkeypatch):
    """A client carrying the token, Host, and Origin — any request that changes state needs all three."""
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    token = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)
    client.headers.update(
        {**HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": token},
    )
    return client


def test_health_endpoint_returns_secret_free_aggregate(tmp_path, monkeypatch):
    path = tmp_path / "health.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))

    response = TestClient(dashboard_app.app).get("/api/health", headers=HOST)

    assert response.status_code == 503
    body = response.json()
    assert body["level"] == "bad"
    assert "services" in body
    assert "token" not in response.text.lower()
    assert "authorization" not in response.text.lower()



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
    """The feature is measured by its work, not by its name appearing in the page.

    The test above checks that `refreshDashboardToken` and `token_refresh=`
    are written in the page — and it passes even if the feature never worked
    at all. And the feature hangs on two silent contracts that break without
    a sound: that the guard's error text contains "dashboard token" exactly
    (it's the page's retry condition, and changing one word in the message
    breaks it), and that the re-served page is read with the same extraction
    pattern. So we walk the path the way the page walks it.

    And the token is generated once at import, so the only state that needs
    a refresh is the dashboard booting again — the same case as
    [`_ID_SALT`] in `keystore`.
    """
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    stale = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)

    monkeypatch.setattr(dashboard_app, "_DASHBOARD_TOKEN", secrets.token_hex(32))

    refused = client.post("/api/anything", json={}, headers={
        **HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": stale,
    })
    assert refused.status_code == 403
    assert "dashboard token" in refused.json()["error"]

    # the path `refreshDashboardToken` fetches, and the pattern it extracts with.
    refreshed = client.get("/?token_refresh=1", headers=HOST)
    fresh = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', refreshed.text).group(1)
    assert fresh != stale

    retried = client.post("/api/anything", json={}, headers={
        **HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": fresh,
    })
    assert retried.status_code == 404      # passed the guard: a routing 404, not a guard 403


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
    assert '"No watches"' in response.text


def test_activity_head_failures_are_monitored_with_their_own_success_stamp():
    """The activity worker is independent: the recorder's success must not
    heal its silent failure.

    The live incident 2026-08-29: the task stayed Running while writing
    UnauthorizedError every five minutes, but the dashboard never showed the
    source at all. Its success stamp is independent for the same reason.
    """
    assert "activity_head" in config.RECORDER_SOURCES
    assert config.SOURCE_OK_STAMPS["activity_head"] == (
        "activity_head_last_run_at",
    )


def test_activity_head_success_stamp_heals_its_older_error(tmp_path):
    """Adding the source to config isn't enough: the DAO layer actually uses its independent stamp."""
    from dao import recorder_errors

    db_path = tmp_path / "errors.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [
            ("last_error_activity_head", "2026-08-29T10:00:00+00:00: UnauthorizedError"),
            ("activity_head_last_run_at", "2026-08-29T10:05:00+00:00"),
        ],
    )
    conn.commit()
    conn.row_factory = sqlite3.Row

    row = next(
        error for error in recorder_errors(
            conn,
            config.RECORDER_SOURCES,
            ok_stamps=config.SOURCE_OK_STAMPS,
        ) if error["source"] == "activity_head"
    )

    assert row["stale"] is True
    assert row["ok_at"] == "2026-08-29T10:05:00+00:00"
    conn.close()


def test_overview_tile_values_are_html_escaped():
    """The EVM state comes from meta; no value is injected into innerHTML without escaping."""
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)

    assert response.status_code == 200
    assert '<div class="v">${esc(i.v)}</div>' in response.text


def test_state_changing_request_without_token_is_refused():
    client = TestClient(dashboard_app.app)
    response = client.post("/api/anything", headers=HOST, json={})
    assert response.status_code == 403
    assert "dashboard token" in response.json()["error"]


def test_state_changing_request_with_token_passes_the_guard():
    """With the correct token the request reaches routing (404, not 403) — the guard isn't a permanent stone."""
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
    """The allowed limit: the account name and the last four characters — and not a fifth.

    Showing the tail is a deliberate FR-013 concession the user asked for to
    tell two keys apart, and this test is its boundary: it proves the tail
    appears, and that nothing before it appears at any length.
    """
    keys_file.write_text(json.dumps({"helius_api_keys": [
        {"key": FAKE, "label": "Main account"},
    ]}), encoding="utf-8")
    client = TestClient(dashboard_app.app)

    response = client.get("/api/provider-keys", headers=HOST)
    assert response.status_code == 200
    raw = response.text
    body = response.json()

    row = next(r for r in body["keys"] if r["provider"] == "helius")
    assert row["tail"] == "wxyz"          # the allowed tail
    assert row["label"] == "Main account"  # the account name it was fetched with
    assert "key" not in row

    # not the value, not any prefix of it, and not any tail longer than four.
    assert FAKE not in raw
    for size in range(4, len(FAKE)):
        assert FAKE[:size] not in raw
    for size in range(5, len(FAKE)):
        assert FAKE[-size:] not in raw


def test_pool_rows_stay_counts_and_indices_only(keys_file):
    """The pool rows are read from `meta` and are fit for the log: counts and indices, no exceptions."""
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
    """These three routes are the most dangerous thing in the dashboard now: without the token, they aren't touched."""
    client = TestClient(dashboard_app.app)
    for path in ("add", "toggle", "delete", "test"):
        response = client.post(f"/api/provider-keys/{path}", headers=HOST, json={})
        assert response.status_code == 403, path
    assert not keys_file.exists()


def test_adding_a_key_writes_the_file_and_hides_the_value(keys_file, signed):
    """The addition is stored in the file, and the answer returns only the tail."""
    response = signed.post("/api/provider-keys/add", json={
        "provider": "helius", "key": FAKE, "label": "Main",
    })
    assert response.status_code == 200
    assert response.json()["tail"] == "wxyz"
    assert FAKE not in response.text

    stored = json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]
    assert stored == [{"key": FAKE, "label": "Main", "enabled": True}]


def test_adding_a_duplicate_or_a_malformed_key_is_refused(keys_file, signed):
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE, "label": ""})

    again = signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    assert again.status_code == 409

    short = signed.post("/api/provider-keys/add", json={"provider": "helius", "key": "abc"})
    assert short.status_code == 400

    # a whole line pasted by mistake (it has a space) doesn't become a dead key in the pool.
    pasted = signed.post("/api/provider-keys/add", json={
        "provider": "helius", "key": "HELIUS_API_KEY = abcdef123456",
    })
    assert pasted.status_code == 400

    unknown = signed.post("/api/provider-keys/add", json={"provider": "nope", "key": FAKE})
    assert unknown.status_code == 404


def test_pausing_a_key_keeps_it_on_disk_but_out_of_the_pool(keys_file, signed):
    """Pausing doesn't lose the value — otherwise it wouldn't be a pause."""
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    off = signed.post("/api/provider-keys/toggle", json={
        "provider": "helius", "slot": 0, "tail": "wxyz", "enabled": False,
    })
    assert off.status_code == 200
    # the last enabled key ⇒ an explicit warning: the layer will stop.
    assert "No enabled key left" in off.json()["warning"]

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
    """Between painting and clicking the file may have changed; the position
    alone would have deleted the healthy one.

    We match the tail the row was painted with: if it changed ⇒ 409, not a
    silent deletion of a neighbor.
    """
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": "OTHERKEYVALUE9999"})

    wrong = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 0, "tail": "9999",
    })
    assert wrong.status_code == 409
    assert "reload the page" in wrong.json()["error"]

    missing = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 7, "tail": "wxyz",
    })
    assert missing.status_code == 409
    stored = json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]
    assert len(stored) == 2


def test_an_environment_variable_beats_the_file_and_the_panel_says_so(keys_file, signed, monkeypatch):
    """Editing the file with no effect is the worst failure: it looks successful and does nothing."""
    monkeypatch.setenv("HELIUS_API_KEY", "from-environment")

    refused = signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    assert refused.status_code == 409
    assert "HELIUS_API_KEY" in refused.json()["error"]

    provider = next(p for p in signed.get("/api/provider-keys").json()["providers"]
                    if p["provider"] == "helius")
    assert provider["env"] is True


def test_the_test_button_reports_a_rejected_key_without_echoing_it(keys_file, signed, monkeypatch):
    """A 401 from the provider often returns the link with the key in it — it's struck before display."""
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

    # the result stays displayed after a repaint — otherwise it's lost after 10 seconds.
    row = next(r for r in signed.get("/api/provider-keys").json()["keys"]
               if r["provider"] == "helius")
    assert row["level"] == "bad" and row["probe"]["status"] == 401


def test_a_broken_provider_is_not_blamed_on_the_key(keys_file, signed, monkeypatch):
    """A 502 is not "a bad key": a red dot here pushes the user to delete a working key."""
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
    assert "not the key" in result["detail"]


def test_a_jsonrpc_error_under_http_200_is_not_called_healthy(keys_file, signed, monkeypatch):
    """JSON-RPC answers 200 with an `error` inside; "200 ⇒ healthy" would have green-lit a rejected key."""
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


def test_error_false_under_http_200_is_healthy(keys_file, signed, monkeypatch):
    """`error: false` is not an error: the probe rejects anything that isn't `None` or `False`.

    The rule came from Covalent/GoldRush, which used to answer `error: false`
    on success, and it stayed after the deletion because it isn't
    provider-specific: any JSON-RPC may answer the field as false, and a
    "healthy" misread here gets blamed on a working key.
    """
    class _Response:
        status_code = 200

        def json(self):
            return {"error": False, "result": "0x1"}

    class _Client:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def request(self, *_a, **_k): return _Response()

    monkeypatch.setattr(keystore.httpx, "Client", _Client)
    signed.post("/api/provider-keys/add", json={
        "provider": "nodereal", "key": FAKE,
    })

    result = signed.post("/api/provider-keys/test", json={
        "provider": "nodereal", "slot": 0, "tail": "wxyz",
    }).json()

    assert result["level"] == "good"
    assert result["detail"] == "healthy"


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
    """The rule: no value, no fragment, no **fingerprint**. And this guard pins it.

    `sha256(key)` does the same distinguishing job, so the temptation is
    real — and it breaks the rule: it works as an oracle by which whoever
    holds a list of candidate keys confirms which one is in use here, and as
    a link matching the same key across two systems. A random salt cuts
    both, but it's a line that's easy to "simplify" after a year — so we pin
    it with a test.
    """
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    row = next(item for item in signed.get("/api/provider-keys").json()["keys"]
               if item["provider"] == "helius")

    forbidden = {
        hashlib.sha256(FAKE.encode()).hexdigest(),
        hashlib.sha256(FAKE.strip().encode()).hexdigest(),
        hashlib.md5(FAKE.encode()).hexdigest(),  # noqa: S324 — we forbid it, we don't use it
        hashlib.sha1(FAKE.encode()).hexdigest(),  # noqa: S324 — we forbid it, we don't use it
    }
    assert row["key_id"]
    assert not any(row["key_id"] == digest or row["key_id"] == digest[:32]
                   for digest in forbidden)
    # and no fragment: the tail alone is allowed, and the identity carries none of it.
    assert FAKE[:8] not in row["key_id"]


def test_key_ids_die_with_the_process_so_a_stale_page_is_refused(keys_file, signed):
    """The salt lives in memory: after a reboot the old identities are refused with 409, not deleted by mistake."""
    signed.post("/api/provider-keys/add", json={"provider": "helius", "key": FAKE})
    row = next(item for item in signed.get("/api/provider-keys").json()["keys"]
               if item["provider"] == "helius")
    stale_id = row["key_id"]

    keystore._ID_SALT = secrets.token_bytes(32)      # as if the dashboard had booted again
    response = signed.post("/api/provider-keys/delete", json={
        "provider": "helius", "slot": 0, "tail": "wxyz", "key_id": stale_id,
    })

    assert response.status_code == 409
    assert json.loads(keys_file.read_text(encoding="utf-8"))["helius_api_keys"]


def test_stale_pool_report_does_not_mark_a_key_as_currently_cooled(keys_file):
    """The momentary falls with staleness, the configurational stays.

    A slot's cooling and use end with the cycle, so a stale report isn't a
    witness to either. But disabling the provider, and who owns it, is a
    configuration description — dropping it would have said "healthy" about
    a provider that's switched off, which is the opposite lie.
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
    # the form is written in HTML, not generated by JS: a repaint every 10s
    # would have wiped a half-pasted key.
    assert 'id="key-add"' in response.text
    assert 'id="key-value" type="password"' in response.text
    assert "External provider keys" in response.text
    # the declaration is shown to the reader, not only in the comments, and it matches what actually happens.
    assert "the value is neither displayed nor logged" in response.text
