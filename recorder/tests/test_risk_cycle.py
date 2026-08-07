"""اختبارات بوابة مخاطر التداول الزمنية (بلا شبكة)."""
import os
from datetime import datetime, timedelta

import pytest

import config
import extract
import recorder
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-08-07T12:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _envelope(*, buy=False, sell=False, warnings=None):
    return {
        "responseObject": {
            "disableBuying": buy,
            "disableSelling": sell,
            "warnings": [] if warnings is None else warnings,
        }
    }


class _RiskClient:
    def __init__(self, replies=None, fail_on=None):
        self.calls = []
        self._replies = replies or {}
        self._fail_on = fail_on or set()

    async def _post(self, path, body):
        self.calls.append((path, body))
        addr = body["address"]
        if addr in self._fail_on:
            raise RuntimeError("upstream boom")
        return self._replies.get(addr, _envelope())


async def _noop(_seconds):
    pass


def _watch(db, token, *, control=False, network="56"):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)
    if control:
        db._conn.execute(
            "UPDATE watchlist SET is_control=1, entry_signal_id=NULL WHERE token_address=?",
            (token,),
        )
        db._conn.commit()


def test_extract_pass_preserves_explicit_false_and_provenance():
    row = extract.extract_risk_assessment(
        _envelope(), "tok", "56", NOW, "2026-08-07T11:59:00+00:00", "sig-1"
    )

    assert row is not None
    assert row["disable_buying"] == 0
    assert row["disable_selling"] == 0
    assert row["gate_status"] == "pass"
    assert row["watch_first_seen_at"] == "2026-08-07T11:59:00+00:00"
    assert row["entry_signal_id"] == "sig-1"


def test_extract_missing_sell_flag_is_unknown_not_safe():
    raw = {"responseObject": {"disableBuying": False, "warnings": []}}
    row = extract.extract_risk_assessment(raw, "tok", "56", NOW, NOW, None)

    assert row is not None
    assert row["disable_selling"] is None
    assert row["gate_status"] == "unknown"


def test_extract_severe_warning_requires_review_and_keeps_type():
    warning = {"type": "INSIDER_HOLDINGS", "severity": "SEVERE", "priority": 2}
    row = extract.extract_risk_assessment(
        _envelope(warnings=[warning]), "tok", "4663", NOW, NOW, "sig-1"
    )

    assert row is not None
    assert row["gate_status"] == "review"
    assert row["severe_count"] == 1
    assert row["warning_types_json"] == '["INSIDER_HOLDINGS"]'


def test_extract_moderate_owner_authority_still_requires_review():
    warning = {"type": "TOKEN_MINTABLE", "severity": "MODERATE", "priority": 3}
    row = extract.extract_risk_assessment(
        _envelope(warnings=[warning]), "tok", "1399811149", NOW, NOW, "sig-1"
    )

    assert row is not None
    assert row["gate_status"] == "review"
    assert row["severe_count"] == 0
    assert row["high_count"] == 0


def test_extract_provider_sell_block_is_hard_block():
    row = extract.extract_risk_assessment(
        _envelope(sell=True), "tok", "56", NOW, NOW, "sig-1"
    )

    assert row is not None
    assert row["disable_selling"] == 1
    assert row["gate_status"] == "blocked"


async def test_risk_cycle_archives_raw_and_updates_state(db):
    _watch(db, "safe")
    _watch(db, "insider", network="4663")
    warning = {"type": "INSIDER_HOLDINGS", "severity": "SEVERE", "priority": 2}
    client = _RiskClient({"insider": _envelope(warnings=[warning])})

    stats = await recorder.run_risk_cycle(client, db, NOW, sleep=_noop)

    assert stats == {
        "risk_tokens": 2, "risk_blocked": 0, "risk_review": 1,
        "risk_unknown": 0, "risk_warnings": 1, "risk_errors": 0,
    }
    rows = db._conn.execute(
        "SELECT token_address, gate_status, raw_json FROM token_risk_assessments "
        "ORDER BY token_address"
    ).fetchall()
    assert [r["gate_status"] for r in rows] == ["review", "pass"]
    assert decode_raw(rows[0]["raw_json"])["responseObject"]["warnings"][0]["type"] == "INSIDER_HOLDINGS"
    states = {r["token_address"]: r["last_status"]
              for r in db._conn.execute("SELECT * FROM risk_fetch_state")}
    assert states == {"safe": "ok", "insider": "review"}
    bodies = {call[1]["address"]: call[1] for call in client.calls}
    assert bodies["safe"]["networkId"] == 56
    assert bodies["insider"]["networkId"] == 4663


async def test_one_risk_failure_stays_error_and_does_not_stop_slice(db):
    _watch(db, "bad")
    _watch(db, "safe")
    client = _RiskClient(fail_on={"bad"})

    stats = await recorder.run_risk_cycle(client, db, NOW, sleep=_noop)

    assert stats["risk_errors"] == 1
    assert stats["risk_tokens"] == 1
    state = db._conn.execute(
        "SELECT last_status FROM risk_fetch_state WHERE token_address='bad'"
    ).fetchone()
    assert state["last_status"] == "error"
    assert db._conn.execute("SELECT COUNT(*) FROM token_risk_assessments").fetchone()[0] == 1


async def test_risk_refresh_and_error_retry_windows(db):
    _watch(db, "safe")
    client = _RiskClient()
    await recorder.run_risk_cycle(client, db, NOW, sleep=_noop)

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    await recorder.run_risk_cycle(client, db, soon, sleep=_noop)
    assert len(client.calls) == 1

    later = (
        datetime.fromisoformat(NOW) + timedelta(seconds=config.RISK_REFRESH_SECONDS + 1)
    ).isoformat()
    await recorder.run_risk_cycle(client, db, later, sleep=_noop)
    assert len(client.calls) == 2


async def test_unassessed_signal_precedes_unassessed_control_when_capped(db, monkeypatch):
    _watch(db, "control", control=True)
    _watch(db, "signal")
    monkeypatch.setattr(config, "RISK_PER_CYCLE", 1)
    client = _RiskClient()

    await recorder.run_risk_cycle(client, db, NOW, sleep=_noop)

    assert client.calls[0][1]["address"] == "signal"


def test_schema_startup_upgrades_prior_pass_with_warning_to_review(tmp_path):
    path = str(tmp_path / "t.db")
    first = RecorderDB(path, SCHEMA)
    row = extract.extract_risk_assessment(
        _envelope(), "tok", "56", NOW, NOW, "sig-1"
    )
    assert row is not None
    row["warning_count"] = 1
    row["warnings_json"] = '[{"type":"TOKEN_MINTABLE","severity":"MODERATE"}]'
    row["warning_types_json"] = '["TOKEN_MINTABLE"]'
    row["gate_status"] = "pass"  # يحاكي الصفوف المكتوبة قبل تشديد القاعدة
    first.insert_risk_assessment(row)
    first.set_risk_state("tok", "56", "ok", 1, NOW)
    first.close()

    reopened = RecorderDB(path, SCHEMA)
    gate = reopened._conn.execute(
        "SELECT gate_status FROM token_risk_assessments"
    ).fetchone()["gate_status"]
    state = reopened._conn.execute(
        "SELECT last_status FROM risk_fetch_state"
    ).fetchone()["last_status"]
    reopened.close()
    assert gate == "review"
    assert state == "review"
