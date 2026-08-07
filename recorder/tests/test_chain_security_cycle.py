"""اختبارات دمج فحص السلسلة مع الجدولة والتخزين (بلا شبكة)."""
import os

import pytest

import chain_security
import config
import recorder
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-08-07T15:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _watch(db, token="0x" + "a" * 40, network="56"):
    db.upsert_watch(token, network, "large_buy", "sig-1", 48, NOW)


def _scan_result(gate="review"):
    return {
        "chain_kind": "evm", "rpc_chain_id": "56", "gate_status": gate,
        "reason_codes": ["owner_authority_active"], "contract_exists": 1,
        "token_standard": "erc20", "program_or_implementation": None,
        "owner_authority": "0x" + "2" * 40, "owner_renounced": 0,
        "mint_authority": None, "freeze_authority": None, "paused": 0,
        "upgradeable": 0, "dangerous_capabilities": ["mint(address,uint256)"],
        "transfer_simulation_status": "success", "top1_account_pct": None,
        "top10_accounts_pct": None, "details": {"code_bytes": 100},
        "raw": {"rpc": [{"method": "eth_chainId", "result": "0x38"}]},
    }


async def test_cycle_persists_assessment_and_compressed_rpc(db, monkeypatch):
    _watch(db)

    async def fake_scan(*_args, **_kwargs):
        return _scan_result()

    monkeypatch.setattr(chain_security, "scan_chain_token", fake_scan)
    monkeypatch.setattr(config, "CHAIN_SECURITY_PER_CYCLE", 1)
    stats = await recorder.run_chain_security_cycle(db, NOW)

    assert stats["chain_tokens"] == 1
    assert stats["chain_review"] == 1
    row = db._conn.execute("SELECT * FROM token_chain_assessments").fetchone()
    assert row["gate_status"] == "review"
    assert row["owner_authority"] == "0x" + "2" * 40
    assert decode_raw(row["raw_json"])["rpc"][0]["method"] == "eth_chainId"
    state = db._conn.execute("SELECT last_status FROM chain_fetch_state").fetchone()
    assert state["last_status"] == "review"


async def test_latest_combined_gate_is_fail_closed(db, monkeypatch):
    _watch(db)

    async def fake_scan(*_args, **_kwargs):
        return _scan_result("blocked")

    monkeypatch.setattr(chain_security, "scan_chain_token", fake_scan)
    monkeypatch.setattr(config, "CHAIN_SECURITY_PER_CYCLE", 1)
    await recorder.run_chain_security_cycle(db, NOW)

    # قبل وصول فحص المزوّد: unknown لا يكتفي بنتيجة السلسلة وحدها.
    combined = db._conn.execute("SELECT combined_status FROM latest_token_safety").fetchone()
    assert combined["combined_status"] == "blocked"  # blocked يتغلب حتى مع غياب الآخر

    db.insert_risk_assessment({
        "token_address": "0x" + "a" * 40, "network_id": "56",
        "recorded_at": NOW, "watch_first_seen_at": NOW, "entry_signal_id": "sig-1",
        "is_control": 0, "disable_buying": 0, "disable_selling": 0,
        "warning_count": 0, "severe_count": 0, "high_count": 0,
        "gate_status": "pass", "warning_types_json": "[]", "warnings_json": "[]",
        "raw_json": "{}",
    })
    combined = db._conn.execute(
        "SELECT provider_status, chain_status, combined_status FROM latest_token_safety"
    ).fetchone()
    assert tuple(combined) == ("pass", "blocked", "blocked")


async def test_cycle_respects_disabled_switch(db, monkeypatch):
    _watch(db)
    monkeypatch.setattr(config, "CHAIN_SECURITY_ENABLED", False)
    stats = await recorder.run_chain_security_cycle(db, NOW)
    assert stats["chain_tokens"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM token_chain_assessments").fetchone()[0] == 0
