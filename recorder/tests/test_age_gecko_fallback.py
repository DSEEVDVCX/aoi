"""اختبارات fallback عمر GeckoTerminal (pool_created_at) داخل resolve_ages."""
import asyncio
import os

import pytest
from db import RecorderDB

import recorder

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)


class _FomoNoAgeClient:
    """عميل fomo يجيب بلا أي عنصر — كأن filterTokens لم يجد العملة."""

    async def _post(self, *a, **k):
        return {"success": True, "responseObject": []}

    async def aclose(self):
        pass


class _FakeGecko:
    """زبون GeckoTerminal وهمي يعيد pool_created_at لأول عملة فقط."""

    def __init__(self):
        self.asked: list[str] = []

    async def pool_created_at(self, address: str, network_id: str) -> str | None:
        self.asked.append(address)
        if address.startswith("SOLV"):
            return "2026-08-20T10:00:00Z"   # 5 أيام قبل NOW
        return None                          # غير موجود عند GT أيضًا


NOW = "2026-08-25T15:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    recorder._evm_admission_paused_runtime = False
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()
    recorder._evm_admission_paused_runtime = False


def _seed_static(db: RecorderDB) -> None:
    import json

    raw = json.dumps({"token": {"address": "SOLVtoken1"}})
    db._conn.execute(
        """INSERT OR IGNORE INTO token_static(
               token_address, network_id, recorded_at, name, symbol, raw_json)
           VALUES(?,?,?,?,?,?)""",
        ("SOLVtoken1", "1399811149", NOW, "solvable", "SOLV", raw),
    )
    raw2 = json.dumps({"token": {"address": "SOLXtoken2"}})
    db._conn.execute(
        """INSERT OR IGNORE INTO token_static(
               token_address, network_id, recorded_at, name, symbol, raw_json)
           VALUES(?,?,?,?,?,?)""",
        ("SOLXtoken2", "1399811149", NOW, "unsolvable", "SOLX", raw2),
    )
    db._commit()


def test_gecko_fallback_fills_missing_age(db, monkeypatch):
    _seed_static(db)
    monkeypatch.setattr(recorder.config, "AGE_GATE_ENABLED_AT",
                        "2026-08-01T00:00:00+00:00")
    gecko = _FakeGecko()
    stats = {"age_resolved": 0, "age_lookup_failed": 0}
    asyncio.run(recorder.resolve_ages(
        _FomoNoAgeClient(), db,
        [("SOLVtoken1", "1399811149"), ("SOLXtoken2", "1399811149")],
        NOW, stats, gecko_fallback=gecko,
    ))
    # العملة الأولى حُلَّت عبر GT
    assert gecko.asked == ["SOLVtoken1", "SOLXtoken2"]
    assert stats["age_resolved"] == 1
    assert stats.get("age_gecko_resolved") == 1
    created, observed = recorder.stored_age(db, "SOLVtoken1", "1399811149")
    # القيمة تُخزَّن كما أرجعها GT (صيغة Z) — البوابة تقرؤها عبر parser الموحّد
    assert created == "2026-08-20T10:00:00Z"
    assert observed == NOW          # ختم الملاحظة = لحظة الجلب (provenance)


def test_gecko_fallback_off_by_default(db, monkeypatch):
    """بلا gecko_fallback: السلوك القديم تمامًا — missing لا يملؤها أحد."""
    _seed_static(db)
    monkeypatch.setattr(recorder.config, "AGE_GATE_ENABLED_AT",
                        "2026-08-01T00:00:00+00:00")
    stats = {"age_resolved": 0, "age_lookup_failed": 0}
    asyncio.run(recorder.resolve_ages(
        _FomoNoAgeClient(), db,
        [("SOLVtoken1", "1399811149")], NOW, stats,
    ))
    assert stats["age_resolved"] == 0
    assert stats.get("age_gecko_resolved", 0) == 0
    created, _ = recorder.stored_age(db, "SOLVtoken1", "1399811149")
    assert created is None


def test_gecko_fallback_invalid_age_rejected(db, monkeypatch):
    """GT يعيد مستقبلًا (بلا مدفوع) → يُرفض، لا يكتب عمرًا فاسدًا."""
    db.upsert_static({
        "token_address": "FUTUREtok", "network_id": "1399811149",
        "symbol": "FUT", "name": "future",
        "recorded_at": NOW,
        "token_created_at": None, "token_created_at_observed_at": None,
    })
    monkeypatch.setattr(recorder.config, "AGE_GATE_ENABLED_AT",
                        "2026-08-01T00:00:00+00:00")

    class _FutureGecko:
        async def pool_created_at(self, address, network_id):
            return "2027-01-01T00:00:00Z"

    stats = {"age_resolved": 0, "age_lookup_failed": 0}
    asyncio.run(recorder.resolve_ages(
        _FomoNoAgeClient(), db, [("FUTUREtok", "1399811149")],
        NOW, stats, gecko_fallback=_FutureGecko(),
    ))
    assert stats["age_resolved"] == 0
    created, _ = recorder.stored_age(db, "FUTUREtok", "1399811149")
    assert created is None
