"""اختبارات الطبقة البطيئة: صلاحيات المِنت وقابليّة التعديل وحيازة المطوّر.

كل الأرقام في المُغلَّفات هنا مأخوذة من **ردود حيّة مقيسة** (2026-08-13) لا
مُختلقة: عملات pump.fun على `spl-token-2022` تعيد `creators: []` و
`updateAuthority: null` داخل امتداد `tokenMetadata`، وعملات `spl-token` القديمة
تعيد سلطتها في `authorities[]` من DAS. الاختبارات تحرس هذا الفرق بالذات لأنّه
موضع الخطأ الطبيعيّ: قراءة السلطة من مسار واحد تُصفّر نصف العملات.
"""
import os

import chain_layer
import config
import extract
import pytest
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


def _watch(db, token, *, network=SOL):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)


def _mint_reply(
    *, program="spl-token-2022", mint_authority=None, freeze_authority=None,
    supply="999673699453014", decimals=6, meta_update_authority=None,
    with_metadata_ext=True,
):
    info = {
        "decimals": decimals,
        "freezeAuthority": freeze_authority,
        "isInitialized": True,
        "mintAuthority": mint_authority,
        "supply": supply,
    }
    if with_metadata_ext:
        info["extensions"] = [
            {"extension": "metadataPointer",
             "state": {"authority": None, "metadataAddress": "m"}},
            {"extension": "tokenMetadata",
             "state": {"name": "T", "symbol": "T", "uri": "u",
                       "updateAuthority": meta_update_authority}},
        ]
    return {
        "context": {"slot": 439039056},
        "value": {"data": {"program": program, "parsed": {"info": info, "type": "mint"}},
                  "owner": "TokenzQdB", "space": 414},
    }


def _asset_reply(*, mutable=True, creators=(), authorities=()):
    return {
        "interface": "FungibleToken",
        "id": "mint1",
        "authorities": [{"address": a, "scopes": ["full"]} for a in authorities],
        "creators": [{"address": c, "share": 100, "verified": True} for c in creators],
        "mutable": mutable,
        "burnt": False,
        "ownership": {"frozen": False, "owner": ""},
        "token_info": {"supply": 999673699453014, "decimals": 6},
    }


def _owner_accounts(*amounts, decimals=6):
    return {
        "context": {"slot": 439039056},
        "value": [
            {"pubkey": f"ata{i}",
             "account": {"data": {"program": "spl-token", "parsed": {"info": {
                 "tokenAmount": {"amount": str(a), "decimals": decimals,
                                 "uiAmount": a / 10 ** decimals}}}}}}
            for i, a in enumerate(amounts)
        ],
    }


# ---------------------------------------------------------------------------
# المستخرِج
# ---------------------------------------------------------------------------
def test_extract_reads_revoked_authorities_as_null():
    """الحالة الشائعة (45 من 48): السكّ والتجميد مشطوبان."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(), "asset": _asset_reply(mutable=True)},
        "tok", SOL, NOW, NOW, "sig-1",
    )
    assert row["mint_authority"] is None
    assert row["freeze_authority"] is None
    assert row["is_mutable"] == 1
    assert row["token_program"] == "spl-token-2022"
    assert row["supply"] == pytest.approx(999673699.453014)
    assert row["decimals"] == 6
    assert row["entry_signal_id"] == "sig-1"


def test_extract_keeps_live_mint_authority():
    """3 من 48: باب الطبع مفتوح — العنوان يُحفَظ لا مجرّد راية."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(mint_authority="DevWa11et", freeze_authority="FrzAuth"),
         "asset": _asset_reply(mutable=False)},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["mint_authority"] == "DevWa11et"
    assert row["freeze_authority"] == "FrzAuth"
    assert row["is_mutable"] == 0


def test_extract_update_authority_from_token2022_extension():
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(meta_update_authority="UpdAuth22"),
         "asset": _asset_reply()},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["update_authority"] == "UpdAuth22"


def test_extract_update_authority_from_das_for_legacy_token():
    """21 من 48 على `spl-token` القديم: لا امتدادات، والسلطة من DAS وحده."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(program="spl-token", with_metadata_ext=False),
         "asset": _asset_reply(authorities=["MetaplexAuth"])},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["token_program"] == "spl-token"
    assert row["update_authority"] == "MetaplexAuth"


def test_extract_mutable_stays_null_when_das_failed():
    """فشل DAS وحده: «لم نقس» ليست «غير قابلة للتعديل» (FR-007)."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(), "asset": None, "asset_error": "getAsset: JSON-RPC -32000"},
        "tok", SOL, NOW, NOW, None,
    )
    assert row is not None
    assert row["is_mutable"] is None
    assert row["creator_count"] is None          # لا صفر: العدد غير معلوم
    assert row["mint_authority"] is None         # وهذا مقيس فعلاً
    assert row["supply"] == pytest.approx(999673699.453014)


def test_extract_empty_creators_is_measured_zero():
    """Token-2022 يعيد `creators: []` فعلاً ⇒ صفر **مقيس** حين ردّ DAS."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(), "asset": _asset_reply(creators=())},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["creator_count"] == 0
    assert row["creator_address"] is None


def test_extract_rejects_reply_without_mint_account():
    """بلا حساب مِنت لا صفّ: صفٌّ كلّه NULL يُقرأ «بلا صلاحيات» وهي أخطر قراءة."""
    for env in (None, {}, {"mint": {"value": None}},
                {"mint": {"value": {"data": {"program": "x"}}}}):
        assert extract.extract_chain_authority(env, "tok", SOL, NOW, NOW, None) is None


def test_extract_dev_holding_percentage():
    """حيازة المطوّر = مجموع حساباته ÷ المعروض، لا أكبر حساب."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(supply="1000000", decimals=6),
         "asset": _asset_reply(creators=["Dev1"]),
         "dev_owner": "Dev1",
         "owner_accounts": _owner_accounts(30_000, 20_000)},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["dev_owner"] == "Dev1"
    assert row["dev_holding_pct"] == pytest.approx(5.0)   # 50,000 من مليون


def test_extract_dev_holding_zero_when_owner_has_no_accounts():
    """عنوان بلا حساب رمز = باع كلّ شيء ⇒ صفر **مقيس** لا NULL."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(supply="1000000"), "asset": _asset_reply(creators=["Dev1"]),
         "dev_owner": "Dev1", "owner_accounts": _owner_accounts()},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["dev_holding_pct"] == 0.0


def test_extract_dev_holding_null_when_second_call_failed():
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(), "asset": _asset_reply(creators=["Dev1"]),
         "dev_owner": "Dev1", "owner_accounts": None, "dev_error": "boom"},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["dev_owner"] == "Dev1"            # نعرف لِمن كنّا نقيس
    assert row["dev_holding_pct"] is None


def test_extract_dev_holding_null_without_owner():
    """لا عنوان ⇒ لم نسأل أصلاً ⇒ NULL لا صفر."""
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(), "asset": _asset_reply()}, "tok", SOL, NOW, NOW, None,
    )
    assert row["dev_owner"] is None
    assert row["dev_holding_pct"] is None


def test_extract_keeps_precision_on_large_supply():
    supply = 10 ** 27 + 1
    row = extract.extract_chain_authority(
        {"mint": _mint_reply(supply=str(supply), decimals=0),
         "asset": _asset_reply(creators=["Dev1"]), "dev_owner": "Dev1",
         "owner_accounts": _owner_accounts(supply)},
        "tok", SOL, NOW, NOW, None,
    )
    assert row["dev_holding_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# اختيار عنوان المطوّر
# ---------------------------------------------------------------------------
def test_pick_dev_owner_prefers_creator():
    owner = extract.pick_dev_owner({
        "mint": _mint_reply(meta_update_authority="UpdAuth"),
        "asset": _asset_reply(creators=["Creator1"], authorities=["DasAuth"]),
    })
    assert owner == "Creator1"


def test_pick_dev_owner_falls_back_to_update_authority():
    assert extract.pick_dev_owner({
        "mint": _mint_reply(meta_update_authority="UpdAuth"),
        "asset": _asset_reply(),
    }) == "UpdAuth"
    assert extract.pick_dev_owner({
        "mint": _mint_reply(program="spl-token", with_metadata_ext=False),
        "asset": _asset_reply(authorities=["DasAuth"]),
    }) == "DasAuth"


def test_pick_dev_owner_none_when_nothing_known():
    """الحالة الغالبة على pump.fun: لا منشئ ولا سلطة ⇒ لا نداء ثانٍ."""
    assert extract.pick_dev_owner({"mint": _mint_reply(), "asset": _asset_reply()}) is None
    assert extract.pick_dev_owner(None) is None


# ---------------------------------------------------------------------------
# الدورة
# ---------------------------------------------------------------------------
class _AuthRPC:
    """عميل مزيّف للطبقة البطيئة. يعدّ النداءين ليُكشف نداءٌ لا لزوم له."""

    def __init__(self, replies=None, fail=None, dev_fail=(), key_missing=False,
                 balances=None):
        self.calls = []
        self.owner_calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._dev_fail = set(dev_fail)
        self._key_missing = key_missing
        self._balances = balances or {}

    async def fetch_authority_raw(self, mint):
        self.calls.append(mint)
        if self._key_missing:
            raise solana_rpc.ChainKeyMissing("ملف مفاتيح السلسلة غائب")
        if mint in self._fail:
            raise solana_rpc.ChainRPCError("getAccountInfo: JSON-RPC -32603: boom")
        return self._replies.get(
            mint, {"mint": _mint_reply(), "asset": _asset_reply()}
        )

    async def fetch_owner_token_balance_raw(self, owner, mint):
        self.owner_calls.append((owner, mint))
        if mint in self._dev_fail:
            raise solana_rpc.ChainRPCError("getTokenAccountsByOwner: JSON-RPC -32603")
        return self._balances.get(mint, _owner_accounts())


async def _noop(_seconds):
    pass


async def test_auth_cycle_writes_row_and_state(db):
    _watch(db, "mint1")
    rpc = _AuthRPC(replies={"mint1": {
        "mint": _mint_reply(mint_authority="Dev1"), "asset": _asset_reply(mutable=True),
    }})

    stats = await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert stats == {"auth_due": 1, "auth_rows": 1, "auth_empty": 0,
                     "auth_errors": 0, "auth_dev": 0}
    row = db._conn.execute(
        "SELECT mint_authority, is_mutable, token_program, creator_count "
        "FROM chain_authority"
    ).fetchone()
    assert row["mint_authority"] == "Dev1"
    assert row["is_mutable"] == 1
    assert row["token_program"] == "spl-token-2022"
    assert row["creator_count"] == 0
    state = db._conn.execute(
        "SELECT last_status, attempts FROM chain_auth_state"
    ).fetchone()
    assert state["last_status"] == "ok"
    assert state["attempts"] == 1


async def test_auth_cycle_skips_second_call_without_owner(db):
    """الحالة الغالبة: لا منشئ ولا سلطة ⇒ نداء واحد لا نداءان."""
    _watch(db, "mint1")
    rpc = _AuthRPC()

    stats = await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.owner_calls == []
    assert stats["auth_dev"] == 0
    assert stats["auth_rows"] == 1


async def test_auth_cycle_measures_dev_holding_when_creator_known(db):
    _watch(db, "mint1")
    rpc = _AuthRPC(
        replies={"mint1": {"mint": _mint_reply(supply="1000000"),
                           "asset": _asset_reply(creators=["Dev1"])}},
        balances={"mint1": _owner_accounts(250_000)},
    )

    stats = await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.owner_calls == [("Dev1", "mint1")]
    assert stats["auth_dev"] == 1
    row = db._conn.execute(
        "SELECT dev_owner, dev_holding_pct FROM chain_authority"
    ).fetchone()
    assert row["dev_owner"] == "Dev1"
    assert row["dev_holding_pct"] == pytest.approx(25.0)


async def test_auth_cycle_survives_dev_call_failure(db):
    """سقوط النداء الثاني يُسقط عموداً لا صفّاً."""
    _watch(db, "mint1")
    rpc = _AuthRPC(
        replies={"mint1": {"mint": _mint_reply(), "asset": _asset_reply(creators=["Dev1"])}},
        dev_fail={"mint1"},
    )

    stats = await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert stats == {"auth_due": 1, "auth_rows": 1, "auth_empty": 0,
                     "auth_errors": 0, "auth_dev": 0}
    row = db._conn.execute(
        "SELECT dev_owner, dev_holding_pct, is_mutable FROM chain_authority"
    ).fetchone()
    assert row["dev_owner"] == "Dev1"
    assert row["dev_holding_pct"] is None
    assert row["is_mutable"] == 1
    raw = decode_raw(
        db._conn.execute("SELECT raw_json FROM chain_authority").fetchone()["raw_json"]
    )
    assert "dev_error" in raw                    # سبب الغياب مؤرشف لا مكتوم


async def test_auth_cycle_failure_marks_error_and_records_meta(db):
    _watch(db, "boom")
    rpc = _AuthRPC(fail={"boom"})

    stats = await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["auth_errors"] == 1
    assert stats["auth_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM chain_authority").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT last_status FROM chain_auth_state"
    ).fetchone()["last_status"] == "error"
    err = db.get_meta("last_error_chain_auth")
    assert err is not None and "boom" in err


async def test_auth_cycle_empty_reply_is_not_an_error(db):
    _watch(db, "nothing")
    rpc = _AuthRPC(replies={"nothing": {"mint": {"value": None}, "asset": None}})

    stats = await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["auth_empty"] == 1
    assert stats["auth_errors"] == 0
    assert db._conn.execute(
        "SELECT last_status FROM chain_auth_state"
    ).fetchone()["last_status"] == "empty"


async def test_auth_cycle_skips_non_solana_networks(db):
    _watch(db, "bsc", network="56")
    _watch(db, "sol", network=SOL)
    rpc = _AuthRPC()

    await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == ["sol"]


async def test_auth_missing_key_propagates_without_marking_tokens(db):
    _watch(db, "mint1")
    rpc = _AuthRPC(key_missing=True)

    with pytest.raises(solana_rpc.ChainKeyMissing):
        await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert db._conn.execute("SELECT COUNT(*) FROM chain_auth_state").fetchone()[0] == 0


async def test_auth_refresh_window_is_hourly_not_five_minutes(db):
    """الفرق الجوهريّ عن الطبقة السريعة: لا سؤال ثانٍ بعد خمس دقائق."""
    from datetime import datetime, timedelta

    _watch(db, "mint1")
    rpc = _AuthRPC()
    await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.calls) == 1

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=600)).isoformat()
    await chain_layer.run_chain_auth_cycle(rpc, db, soon, sleep=_noop)
    assert len(rpc.calls) == 1                   # عشر دقائق ليست نافذة تحديث

    later = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.CHAIN_AUTH_REFRESH_SECONDS + 1)
    ).isoformat()
    await chain_layer.run_chain_auth_cycle(rpc, db, later, sleep=_noop)
    assert len(rpc.calls) == 2


async def test_auth_error_retries_faster_than_refresh(db):
    from datetime import datetime, timedelta

    _watch(db, "boom")
    rpc = _AuthRPC(fail={"boom"})
    await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert config.CHAIN_AUTH_ERROR_RETRY_SECONDS < config.CHAIN_AUTH_REFRESH_SECONDS
    retry = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.CHAIN_AUTH_ERROR_RETRY_SECONDS + 1)
    ).isoformat()
    await chain_layer.run_chain_auth_cycle(rpc, db, retry, sleep=_noop)
    assert len(rpc.calls) == 2


async def test_auth_queue_is_independent_of_concentration_queue(db):
    """طابوران منفصلان: تحديث السريعة لا يزعم أنّ البطيئة تحدّثت."""
    _watch(db, "mint1")
    rpc = _AuthRPC()
    await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    assert db._conn.execute("SELECT COUNT(*) FROM chain_auth_state").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM chain_fetch_state").fetchone()[0] == 0


async def test_auth_cycle_archives_raw_envelope(db):
    _watch(db, "mint1")
    rpc = _AuthRPC()

    await chain_layer.run_chain_auth_cycle(rpc, db, NOW, sleep=_noop)

    raw = decode_raw(
        db._conn.execute("SELECT raw_json FROM chain_authority").fetchone()["raw_json"]
    )
    # ما أُسقط من الأعمدة لأنّه قِيس ثابتاً يبقى محفوظاً هنا.
    assert raw["asset"]["burnt"] is False
    assert raw["mint"]["value"]["data"]["program"] == "spl-token-2022"
