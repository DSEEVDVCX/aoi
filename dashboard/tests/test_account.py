"""Switching the fomo account from the dashboard: what gets rejected before writing, and what stays reversible.

This is the dashboard's second write path, and its target is a single file:
`api/.privy_state.json`. The tests here guard three things nothing else
guards:

1. **No token value leaves.** The response carries the identity's fingerprint
   and the last four characters, not the token — and the leak check searches
   for the full string in the response, so if a field is added later that
   returns it, the test falls over.
2. **No write without a way back.** A backup is saved before any touch, and
   restore works — because a wrong paste silences the whole collection, and
   the old account is saved nowhere else.
3. **The anonymous session is rejected.** Privy writes `privy:token` before
   any sign-in, so pasting a pre-sign-in store used to write an identity that
   owns nothing and say "done".

And every test that touches the file relies on `isolate_live_state`
(automatic in `conftest.py`), which points `PRIVY_STATE_PATH` at a temporary
one. Without it the tests would write over this machine's real running
credential.
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
    """A fake token with a readable payload. The signature is literal text —
    `account` doesn't verify it and must not: it's another service's token
    and we don't hold its key.

    And `iat` is omitted when not asked for rather than zeroed: a token with
    no `iat` must fall back to comparing `exp`, and an explicit zero would
    make it "older than everything", hiding that fallback."""
    def part(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    claims: dict = {"sub": sub, "exp": exp}
    if iat is not None:
        claims["iat"] = iat
    return f"{part({'alg': 'ES256'})}.{part(claims)}.SiGnAtUrE"


# And `iat` in ALICE is deliberate: a real Privy token always carries it
# (measured on a live token 2026-08-20), and the file's account is what every
# later paste is compared against. If it were left without `iat`, falling back
# to the `exp` comparison would become the tested path in every case, and the
# real path — comparing on `iat` — would have no test at all.
ALICE = jwt("did:privy:alice00000000000000000", iat=1_700_000_000)
BOB = jwt("did:privy:bob000000000000000000000", iat=1_700_000_000)
ANON = jwt(account._ANON_DID)

APP_ID = "cmt0phbhk00080dla83dtghph"


def dump(token: str, *, refresh: str = "rt-bob-9999", pat: str = "pat-bob-9999") -> dict:
    """A browser store as the user copies it — with the quotation marks Privy puts in."""
    return {
        "privy:token": f'"{token}"',
        "privy:refresh_token": f'"{refresh}"',
        "privy:pat": f'"{pat}"',
        "privy:ca_id": '"ca-1234"',
        f"privy:{APP_ID}:state": '"{\\"client\\":\\"client-WYabc123456\\"}"',
    }


@pytest.fixture(autouse=True)
def forget_probes():
    """`_LAST_PROBE` is process memory, so it's cleared between tests like `keystore._PROBES`."""
    account._LAST_PROBE.clear()
    yield
    account._LAST_PROBE.clear()


@pytest.fixture
def state(monkeypatch, tmp_path):
    """An existing credential file for the ALICE account, on a temporary path."""
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
    """A client carrying the token, Host, and Origin — any request that changes state needs all three."""
    client = TestClient(dashboard_app.app)
    page = client.get("/", headers=HOST)
    token = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page.text).group(1)
    client.headers.update(
        {**HOST, "Origin": "http://127.0.0.1:8090", "X-Dashboard-Token": token},
    )
    return client


# --- what leaves the server: a fingerprint, not a value ---

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
    """Absence is a valid state: not signed in yet. And a 500 here used to empty the dashboard."""
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
    """The real file on this machine has no `client_id` and still renews its
    token every minute (measured 2026-08-20), because `api/config.py` puts
    the constant identifier in as a fallback. Listing it among the blocking
    gaps would turn the dashboard red on a healthy system — and a false
    alarm teaches the user to ignore red."""
    raw = json.loads(state.read_text(encoding="utf-8"))
    del raw["client_id"]
    del raw["ca_id"]
    state.write_text(json.dumps(raw), encoding="utf-8")
    body = signed.get("/api/fomo-account").json()
    assert set(body["missing"]) == {"client_id", "ca_id"}     # the truth stays displayed
    assert body["missing_required"] == []                      # and no alarm
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
    assert body["refreshable"] is True     # expired but renewable ≠ broken


# --- switching: what gets rejected before writing ---

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
    # what `_clean` strips: no quotation marks reach the file, or the upstream
    # answers 401 to a correct token.
    assert not written["access_token"].startswith('"')
    assert written["app_id"] == APP_ID
    assert written["ca_id"] == "ca-1234"

    # the backup carries the old account — it's the only way back.
    backup = json.loads((state.parent / out["backup"]).read_text(encoding="utf-8"))
    assert backup["access_token"] == ALICE


def test_switch_never_echoes_the_pasted_token_back(state, signed):
    text = signed.post("/api/fomo-account/switch", json=dump(BOB)).text
    assert BOB not in text
    assert "rt-bob-9999" not in text
    assert "pat-bob-9999" not in text
    assert BOB[-4:] in text          # the tail alone — the FR-013 exception, on purpose


def test_switch_refuses_the_anonymous_privy_session(state, signed):
    """Privy writes `privy:token` when the SDK boots, before any sign-in (measured 2026-08-20).

    And this is the most dangerous rejection in the file: without this guard
    the user reads "switched" then finds the collection dead, because the
    written identity owns nothing.
    """
    response = signed.post("/api/fomo-account/switch", json=dump(ANON))
    assert response.status_code == 400
    assert "anonymous session" in response.json()["error"]
    # and the file is untouched: the rejection happens before the backup and the write.
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE
    assert not list(state.parent.glob(account._BAK_PREFIX + "*"))


def test_switch_refuses_the_same_identity(state, signed):
    response = signed.post("/api/fomo-account/switch", json=dump(ALICE))
    assert response.status_code == 409
    assert not list(state.parent.glob(account._BAK_PREFIX + "*"))


def test_status_reports_how_long_ago_the_file_was_written(state, signed):
    """The write's age tells "nobody is renewing" from "renewal runs and is
    being rejected" — two contradictory diagnoses the dashboard used to give
    with one text."""
    body = signed.get("/api/fomo-account").json()
    assert isinstance(body["written_seconds_ago"], int)
    assert body["written_seconds_ago"] <= 5           # written in the fixture just now


def test_status_reports_no_write_age_when_there_is_no_file(signed):
    """No file ⇒ no age. And a zero here would have meant "written now", flipping the verdict."""
    assert signed.get("/api/fomo-account").json()["written_seconds_ago"] is None


def test_the_same_identity_with_a_newer_token_is_a_refresh_not_a_repeat(state, signed):
    """Pasting a fresh sign-in for the same account is the way out when
    Privy's renewal breaks.

    It used to be rejected with 409 "nothing to switch", so whoever's session
    broke, signed in again with the same account, and pasted their store
    found a closed door — and the case isn't theoretical: it happened
    2026-08-20.
    """
    fresh = jwt("did:privy:alice00000000000000000", iat=1_800_000_000)
    response = signed.post("/api/fomo-account/switch", json=dump(fresh))
    assert response.status_code == 200
    body = response.json()
    assert body["refreshed"] is True
    assert body["did"] == "did:privy:alice00000000000000000"
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == fresh
    assert fresh not in response.text          # the fingerprint leaves, not the value
    backups = list(state.parent.glob(account._BAK_PREFIX + "*"))
    assert len(backups) == 1 and backups[0].name.endswith("-refresh")


def test_a_refresh_of_the_same_identity_still_keeps_the_old_file(state, signed):
    """The renewal touches the same file, so a way-back backup there is more
    required, not less: a paste from a wrong window silences the whole
    collection and there's no copy of the old credential anywhere else."""
    signed.post(
        "/api/fomo-account/switch",
        json=dump(jwt("did:privy:alice00000000000000000", iat=1_800_000_000)),
    )
    backup = next(iter(state.parent.glob(account._BAK_PREFIX + "*")))
    assert json.loads(backup.read_text(encoding="utf-8"))["access_token"] == ALICE
    assert json.loads(backup.read_text(encoding="utf-8"))["refresh_token"] == "rt-alice-0001"


def test_an_older_token_of_the_same_identity_cannot_overwrite_the_newer_one(state, signed):
    """An old window left open carries an expired token — and pasting it would have wiped out the worker."""
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
    assert "not newer" in response.json()["error"]
    assert json.loads(state.read_text(encoding="utf-8"))["refresh_token"] == "rt-alice-0002"
    assert not list(state.parent.glob(account._BAK_PREFIX + "*"))


def test_the_same_identity_falls_back_to_exp_when_no_iat_is_issued(state, signed):
    """Not everyone issuing a JWT puts in `iat`. When it's absent the
    comparison stays on `exp` instead of falling to "not newer" and
    answering 409 to a correct renewal."""
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
    """`app_id` is extracted from a key's name in the store, and whoever
    copies a single line loses it — and without `app_id` there's no renewal:
    the account dies after an hour with no visible cause."""
    bare = {k: v for k, v in dump(BOB).items() if ":state" not in k}
    response = signed.post("/api/fomo-account/switch", json=bare)
    assert response.status_code == 400
    assert "app_id" in response.json()["error"]


def test_switch_refuses_an_unreadable_token(state, signed):
    response = signed.post("/api/fomo-account/switch", json=dump("not-a-jwt-at-all"))
    assert response.status_code == 400
    assert "JWT" in response.json()["error"]


def test_switch_accepts_the_full_local_storage_wrapper(state, signed):
    """The shape `credential_store` understands in the first place — the user doesn't know which one they're holding."""
    response = signed.post(
        "/api/fomo-account/switch", json={"_full_localStorage": dump(BOB)},
    )
    assert response.status_code == 200
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == BOB


def test_switch_keeps_old_fields_the_paste_does_not_carry(state, signed):
    """`client_id` doesn't appear in every store, and it's a **requirement**
    for Privy's renewal (without it, 400).

    So merge, don't replace: what the paste doesn't carry stays from the old
    file, because `client_id` is a constant app identifier, not an account
    secret.
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


# --- the way back ---

def test_restore_brings_the_previous_account_back(state, signed):
    name = signed.post("/api/fomo-account/switch", json=dump(BOB)).json()["backup"]
    response = signed.post("/api/fomo-account/restore", json={"name": name})
    assert response.status_code == 200
    assert response.json()["did"] == "did:privy:alice00000000000000000"
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE
    assert ALICE not in response.text


def test_restore_backs_up_first_so_the_switch_is_not_lost(state, signed):
    """Restore is itself a write, and whoever restores by mistake needs a way back too."""
    first = signed.post("/api/fomo-account/switch", json=dump(BOB)).json()["backup"]
    signed.post("/api/fomo-account/restore", json={"name": first})
    saved = [b["did"] for b in signed.get("/api/fomo-account").json()["backups"]]
    assert "did:privy:bob000000000000000000000" in saved


def test_restore_refuses_a_path_outside_the_backup_set(state, signed):
    """`..` and a name without the prefix: no other files are read and nothing is written over the credential."""
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
    """Backups don't pile up without limit, and the oldest is deleted first."""
    for i in range(account._BAK_KEEP + 3):
        token = jwt(f"did:privy:acct{i:020d}")
        signed.post(
            "/api/fomo-account/switch", json=dump(token, refresh=f"rt{i}", pat=f"pat{i}"),
        )
    assert len(list(state.parent.glob(account._BAK_PREFIX + "*"))) <= account._BAK_KEEP


# --- the probe: separating "the identity is blocked" from "the upstream is struggling" ---

@pytest.mark.parametrize(("codes", "level", "needle"), [
    ([200, 200, 200], "good", "works"),
    ([403, 403, 403], "bad", "blocked"),
    ([401, 200, 200], "bad", "401"),
    ([403, 200, 200], "bad", "partial"),
    ([503, 502, 500], "warn", "struggling"),
    ([None, None, None], "warn", "no response"),
    ([200, 404, 500], "warn", "mixed"),
])
def test_the_verdict_separates_a_blocked_identity_from_a_sick_upstream(codes, level, needle):
    """Measured 2026-08-19: a block is 403 on every path, and the recorder
    logged it as "unreachable", so the network was hunted for an hour while
    it was fine. The code is the verdict, not the error text."""
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
    # and it's cached in memory so the card can show it after a refresh with no second call.
    assert signed.get("/api/fomo-account").json()["last_probe"]["level"] == "bad"


def test_probe_reads_a_transport_failure_as_no_answer_not_as_a_block(state, signed, monkeypatch):
    """A network outage is not a block — and conflating them pushes the user toward switching a healthy account."""
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
    assert "no response" in body["detail"]
    assert all(r["status"] is None for r in body["rows"])


def test_probe_without_a_token_says_so_instead_of_calling_out(signed):
    response = signed.post("/api/fomo-account/probe", json={})
    assert response.status_code == 400
    assert "No token" in response.json()["error"]


def test_switch_clears_a_probe_verdict_from_the_previous_account(state, signed):
    """The previous account's result doesn't describe the new one, and
    leaving it displayed after a switch used to say "blocked" about an
    account that hasn't been probed."""
    account._LAST_PROBE.update({"level": "bad", "detail": "blocked", "rows": []})
    signed.post("/api/fomo-account/switch", json=dump(BOB))
    assert signed.get("/api/fomo-account").json()["last_probe"] is None


# --- the guards: the key panel's own guards, and needed here even more ---

def test_every_write_path_needs_the_csrf_token(state):
    """Without the token: 403 before routing. An external page must not switch the data source's account."""
    client = TestClient(dashboard_app.app)
    for path in ("switch", "restore", "probe"):
        response = client.post(f"/api/fomo-account/{path}", json={}, headers=HOST)
        assert response.status_code == 403
        assert "dashboard token" in response.json()["error"]
    assert json.loads(state.read_text(encoding="utf-8"))["access_token"] == ALICE


def test_the_paste_path_accepts_a_body_larger_than_the_default_cap(state, signed):
    """A real browser store passes 8KB easily — and it used to be rejected
    with "request too large", so the user read a rejection whose cause they
    couldn't understand. And the cap is for this path alone."""
    padded = dump(BOB)
    padded["privy:noise"] = '"' + "x" * 20000 + '"'
    assert signed.post("/api/fomo-account/switch", json=padded).status_code == 200
    # and it's not raised for everyone else:
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
