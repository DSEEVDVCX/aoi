"""اختبارات طبقة السلسلة: المستخرِج والدورة وشطب المفتاح (بلا شبكة)."""
import os
from datetime import datetime, timedelta

import pytest

import chain_layer
import config
import extract
import solana_rpc
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-08-13T12:00:00+00:00"
SOL = config.SOLANA_NETWORK_ID


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token, *, control=False, network=SOL):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)
    if control:
        db._conn.execute(
            "UPDATE watchlist SET is_control=1, entry_signal_id=NULL WHERE token_address=?",
            (token,),
        )
        db._conn.commit()


def _envelope(amounts, supply, decimals=6, ui_supply=None, ui_amounts=None):
    """يحاكي دفعة `getTokenSupply` + `getTokenLargestAccounts`.

    `ui_supply`/`ui_amounts` تُمرَّر **خاطئة** في بعض الاختبارات عمداً: القياس
    يجب أن يأتي من `amount` الصحيح لا من العشريّ الجاهز.
    """
    unit = 10 ** decimals
    return {
        "supply": {
            "context": {"slot": 400},
            "value": {
                "amount": str(supply),
                "decimals": decimals,
                "uiAmount": ui_supply if ui_supply is not None else supply / unit,
                "uiAmountString": str(supply / unit),
            },
        },
        "largest": {
            "context": {"slot": 400},
            "value": [
                {
                    "address": f"acc{i}",
                    "amount": str(a),
                    "decimals": decimals,
                    "uiAmount": (
                        ui_amounts[i] if ui_amounts is not None else a / unit
                    ),
                }
                for i, a in enumerate(amounts)
            ],
        },
    }


# ---------------------------------------------------------------------------
# المستخرِج
# ---------------------------------------------------------------------------
def test_extract_computes_four_tiers():
    """20 حساباً متساوية بـ2% لكلٍّ ⇒ 2/10/20/40%."""
    amounts = [2_000_000] * 20                      # كلٌّ 2% من 100 مليون
    row = extract.extract_chain_concentration(
        _envelope(amounts, 100_000_000), "tok", SOL, NOW, NOW, "sig-1",
    )
    assert row is not None
    assert row["top1_pct"] == pytest.approx(2.0)
    assert row["top5_pct"] == pytest.approx(10.0)
    assert row["top10_pct"] == pytest.approx(20.0)
    assert row["top20_pct"] == pytest.approx(40.0)
    assert row["top_accounts"] == 20
    assert row["supply"] == pytest.approx(100.0)     # 6 منازل عشرية
    assert row["decimals"] == 6
    assert row["is_control"] == 0
    assert row["entry_signal_id"] == "sig-1"


def test_extract_sorts_descending_before_slicing():
    """الترتيب هو كلّ المعنى في «أكبر واحد» — ردّ مبعثر لا يفسد top1."""
    row = extract.extract_chain_concentration(
        _envelope([100, 900, 300, 200], 2_000), "tok", SOL, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(45.0)    # 900 من 2000
    assert row["top5_pct"] == pytest.approx(75.0)    # الأربعة كلّها = 1500


def test_extract_top_accounts_exposes_short_lists():
    """عملة حائزوها ثلاثة: top20 = مجموع الكلّ لا «أكبر 20» — العمود يكشف ذلك."""
    row = extract.extract_chain_concentration(
        _envelope([500, 300, 200], 1_000), "tok", SOL, NOW, NOW, None,
    )
    assert row["top_accounts"] == 3
    assert row["top1_pct"] == pytest.approx(50.0)
    assert row["top5_pct"] == pytest.approx(100.0)
    assert row["top10_pct"] == pytest.approx(100.0)
    assert row["top20_pct"] == pytest.approx(100.0)


def test_extract_uses_base_units_not_ui_amount():
    """القياس من `amount` الصحيح لا من `uiAmount` — هنا العشريّ مدسوس خاطئاً."""
    row = extract.extract_chain_concentration(
        _envelope(
            [400, 100], 1_000,
            ui_supply=999999.0,                     # عشريّ عرضٍ خاطئ تماماً
            ui_amounts=[0.000001, 0.000001],        # وعشريّ كميّات خاطئ
        ),
        "tok", SOL, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(40.0)
    assert row["top5_pct"] == pytest.approx(50.0)
    assert row["supply"] == pytest.approx(0.001)     # 1000 ÷ 10^6 لا 999999


def test_extract_keeps_precision_at_eighteen_decimals():
    """معروض بـ18 منزلة يفقد أرقاماً بالعشريّات؛ الأعداد الصحيحة لا تفقد."""
    supply = 10 ** 27 + 1                            # عدد لا يمثّله float
    row = extract.extract_chain_concentration(
        _envelope([supply], supply, decimals=18), "tok", SOL, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(100.0)


def test_extract_rejects_envelope_without_measurement():
    assert extract.extract_chain_concentration(
        None, "tok", SOL, NOW, NOW, None
    ) is None
    assert extract.extract_chain_concentration(
        {}, "tok", SOL, NOW, NOW, None
    ) is None
    empty = {"supply": {"value": None}, "largest": {"value": []}}
    assert extract.extract_chain_concentration(
        empty, "tok", SOL, NOW, NOW, None
    ) is None


def test_extract_zero_supply_keeps_row_with_null_ratios():
    """حرق كامل: الصفر **قياس** فيبقى الصفّ، والنِّسب تبقى NULL (لا قسمة على صفر)."""
    row = extract.extract_chain_concentration(
        _envelope([0], 0), "tok", SOL, NOW, NOW, None,
    )
    assert row is not None
    assert row["supply"] == 0.0
    assert row["top1_pct"] is None
    assert row["top20_pct"] is None
    assert row["top_accounts"] == 1


def test_extract_missing_accounts_leaves_ratios_null_but_keeps_supply():
    row = extract.extract_chain_concentration(
        _envelope([], 1_000_000), "tok", SOL, NOW, NOW, None,
    )
    assert row is not None
    assert row["top_accounts"] == 0
    assert row["top1_pct"] is None
    assert row["supply"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# شطب المفتاح (FR-013)
# ---------------------------------------------------------------------------
def test_redact_removes_key_from_any_message():
    key = "778a585b-4825-4df1-80c1-964d45b475cf"
    msg = f"ConnectError: https://mainnet.helius-rpc.com/?api-key={key} فشل"
    out = solana_rpc._redact(msg, key)
    assert key not in out
    assert "api-key=<محجوب>" in out


def test_redact_scrubs_url_even_when_key_rotated():
    """لو دُوِّر المفتاح على القرص بين النداء والاستثناء لم يطابق نصّ الرابط."""
    out = solana_rpc._redact("HTTP 401: .../?api-key=OLDVALUE&x=1", "NEWVALUE")
    assert "OLDVALUE" not in out
    assert "&x=1" in out                              # الشطب لا يبتلع بقيّة النصّ


def test_read_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "env-secret")
    monkeypatch.setattr(config, "chain_keys_path", lambda: "missing.json")
    assert solana_rpc._read_key() == "env-secret"


async def test_concentration_rejects_materially_different_slots(monkeypatch):
    rpc = solana_rpc.SolanaRPC.__new__(solana_rpc.SolanaRPC)

    async def _post(_payload):
        supply = _envelope([1], 10)["supply"]
        largest = _envelope([1], 10)["largest"]
        largest["context"]["slot"] = 400 + config.CHAIN_MAX_SLOT_LAG + 1
        return [
            {"id": "supply", "result": supply},
            {"id": "largest", "result": largest},
        ]

    rpc._post = _post
    with pytest.raises(solana_rpc.ChainRPCError, match="غير متزامنتين"):
        await rpc.fetch_concentration_raw("mint")


# ---------------------------------------------------------------------------
# الدورة
# ---------------------------------------------------------------------------
class _RPC:
    """عميل سلسلة مزيّف. `replies` قواميس، و`fail`/`key_missing` استثناءات."""

    def __init__(self, replies=None, fail=None, key_missing=False):
        self.calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._key_missing = key_missing

    async def fetch_concentration_raw(self, mint):
        self.calls.append(mint)
        if self._key_missing:
            raise solana_rpc.ChainKeyMissing("ملف مفاتيح السلسلة غائب")
        if mint in self._fail:
            raise solana_rpc.ChainRPCError("getTokenSupply: JSON-RPC -32603: boom")
        return self._replies.get(mint, _envelope([], 0))


async def _noop(_seconds):
    pass


async def test_cycle_writes_row_and_marks_state_ok(db):
    _watch(db, "mint1")
    rpc = _RPC(replies={"mint1": _envelope([3_221_000, 1_000_000], 10_000_000)})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert stats == {
        "chain_due": 1, "chain_rows": 1, "chain_empty": 0,
        "chain_unsupported": 0, "chain_errors": 0,
    }
    row = db._conn.execute(
        "SELECT token_address, network_id, top1_pct, top20_pct, top_accounts "
        "FROM chain_concentration"
    ).fetchone()
    assert row["token_address"] == "mint1"
    assert row["network_id"] == SOL
    assert row["top1_pct"] == pytest.approx(32.21)
    assert row["top20_pct"] == pytest.approx(42.21)
    assert row["top_accounts"] == 2
    state = db._conn.execute(
        "SELECT last_status, top1_pct, attempts FROM chain_fetch_state"
    ).fetchone()
    assert state["last_status"] == "ok"
    assert state["top1_pct"] == pytest.approx(32.21)
    assert state["attempts"] == 1


async def test_cycle_skips_non_solana_networks(db):
    """EVM لا يُسأل إطلاقاً: معيار ERC-20 بلا قائمة حائزين على السلسلة."""
    _watch(db, "bsc", network="56")
    _watch(db, "robinhood", network="4663")
    _watch(db, "sol", network=SOL)
    rpc = _RPC(replies={"sol": _envelope([1], 10)})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == ["sol"]
    assert stats["chain_due"] == 1
    # ولا صفّ حالة للـEVM: لا نوسم بالفشل عملةً لم نسألها أصلاً.
    assert db._conn.execute("SELECT COUNT(*) FROM chain_fetch_state").fetchone()[0] == 1


async def test_cycle_failure_marks_error_and_records_meta(db):
    _watch(db, "boom")
    rpc = _RPC(fail={"boom"})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["chain_errors"] == 1
    assert stats["chain_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM chain_concentration").fetchone()[0] == 0
    state = db._conn.execute("SELECT last_status FROM chain_fetch_state").fetchone()
    assert state["last_status"] == "error"
    err = db.get_meta("last_error_chain")
    assert err is not None and "boom" in err


async def test_cycle_marks_known_unsupported_mint_without_rpc_or_error(db, monkeypatch):
    token = "So11111111111111111111111111111111111111112"
    _watch(db, token)
    monkeypatch.setattr(config, "CHAIN_UNSUPPORTED_TOKENS", frozenset({token.lower()}))
    rpc = _RPC()

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == []
    assert stats["chain_errors"] == 0
    assert stats["chain_unsupported"] == 1
    state = db._conn.execute(
        "SELECT last_status FROM chain_fetch_state WHERE token_address=?", (token,)
    ).fetchone()
    assert state["last_status"] == "unsupported"

    later = (datetime.fromisoformat(NOW) + timedelta(days=1)).isoformat()
    await chain_layer.run_chain_cycle(rpc, db, later, sleep=_noop)
    assert rpc.calls == []


async def test_cycle_empty_reply_is_not_an_error(db):
    """ردّ بلا قياس ≠ فشل: `empty` يعيد الجدولة بإيقاع عاديّ لا كل دقيقتين."""
    _watch(db, "nothing")
    rpc = _RPC(replies={"nothing": {"supply": {"value": None}, "largest": {"value": []}}})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["chain_empty"] == 1
    assert stats["chain_errors"] == 0
    state = db._conn.execute("SELECT last_status FROM chain_fetch_state").fetchone()
    assert state["last_status"] == "empty"


async def test_missing_key_propagates_without_marking_tokens(db):
    """عيب إعداد لا عيب عملة: الطابور لا يُوسَم `error` بسبب ملفّ غائب."""
    _watch(db, "mint1")
    rpc = _RPC(key_missing=True)

    with pytest.raises(solana_rpc.ChainKeyMissing):
        await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert db._conn.execute("SELECT COUNT(*) FROM chain_fetch_state").fetchone()[0] == 0


async def test_cycle_archives_raw_envelope(db):
    _watch(db, "mint1")
    rpc = _RPC(replies={"mint1": _envelope([7, 3], 100)})

    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    raw = decode_raw(
        db._conn.execute("SELECT raw_json FROM chain_concentration").fetchone()["raw_json"]
    )
    assert raw["supply"]["value"]["amount"] == "100"
    assert raw["largest"]["value"][0]["address"] == "acc0"


async def test_refresh_and_error_retry_windows(db):
    from datetime import datetime, timedelta

    _watch(db, "mint1")
    rpc = _RPC(replies={"mint1": _envelope([1], 10)})
    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.calls) == 1

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    await chain_layer.run_chain_cycle(rpc, db, soon, sleep=_noop)
    assert len(rpc.calls) == 1                      # ما زال طازجاً

    later = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.CHAIN_REFRESH_SECONDS + 1)
    ).isoformat()
    await chain_layer.run_chain_cycle(rpc, db, later, sleep=_noop)
    assert len(rpc.calls) == 2


async def test_errored_token_retries_faster_than_refresh(db):
    from datetime import datetime, timedelta

    _watch(db, "boom")
    rpc = _RPC(fail={"boom"})
    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.calls) == 1

    retry = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.CHAIN_ERROR_RETRY_SECONDS + 1)
    ).isoformat()
    assert config.CHAIN_ERROR_RETRY_SECONDS < config.CHAIN_REFRESH_SECONDS
    await chain_layer.run_chain_cycle(rpc, db, retry, sleep=_noop)
    assert len(rpc.calls) == 2                      # قبل نافذة الطزاجة


async def test_signal_token_precedes_control_when_capped(db, monkeypatch):
    _watch(db, "control", control=True)
    _watch(db, "signal")
    monkeypatch.setattr(config, "CHAIN_PER_CYCLE", 1)
    rpc = _RPC()

    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == ["signal"]


async def test_empty_networks_tuple_fetches_nothing(db, monkeypatch):
    """قائمة شبكات فارغة تعني «لا شيء» لا «الكلّ» — الصمت أصدق من مسح أعمى."""
    _watch(db, "mint1")
    monkeypatch.setattr(config, "CHAIN_NETWORKS", ())
    rpc = _RPC()

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == []
    assert stats["chain_due"] == 0
