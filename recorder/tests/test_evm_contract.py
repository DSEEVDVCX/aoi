"""اختبارات سلامة عقد EVM: keccak، المُعرّفات، الوكيل، والدورة (بلا شبكة).

keccak مكتوب بأيدينا (لا `pycryptodome` ولا `eth-hash` في البيئة، و
`hashlib.sha3_256` حشوه مختلف) ⇒ يُختبَر على متّجهات معروفة قبل أي شيء: مُعرّف
خاطئ يجعل كل أعمدة الخطر أصفاراً كاذبة بلا أثر ظاهر.
"""
import os

import pytest

import config
import evm_contract
from db import RecorderDB, decode_raw
from evm_rpc import EVMRateLimit

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
BASE = "8453"
TOK = "0xbbbb000000000000000000000000000000000002"
ZERO = "0x0000000000000000000000000000000000000000"
OWNER = "0x00000000000000000000000000000000000000ff"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token=TOK, *, network=BASE, when=NOW):
    db.upsert_watch(token, network, "large_buy", f"sig-{token[:8]}", 48, when)


def _code(*signatures, extra=b""):
    """بايت‑كود مصنوع: `PUSH4 <مُعرّف>` لكل توقيع، كما يفعل جدول التوزيع."""
    body = b""
    for sig in signatures:
        body += b"\x63" + bytes.fromhex(evm_contract.selector(sig)[2:])
    return "0x" + (body + extra).hex()


def _word(addr):
    return "0x" + "0" * 24 + addr[2:]


# ---------------------------------------------------------------------------
# keccak والمُعرّفات
# ---------------------------------------------------------------------------
def test_keccak_matches_known_vectors():
    assert evm_contract.keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert evm_contract.keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_keccak_spans_multiple_blocks():
    """أطول من 136 بايتاً (rate) ⇒ يمرّ على أكثر من دورة ضغط."""
    assert evm_contract.keccak256(b"a" * 200).hex() == evm_contract.keccak256(
        bytes(bytearray(b"a" * 200))
    ).hex()
    assert len(evm_contract.keccak256(b"a" * 200)) == 32


def test_selector_matches_known_signatures():
    assert evm_contract.selector("owner()") == "0x8da5cb5b"
    assert evm_contract.selector("transfer(address,uint256)") == "0xa9059cbb"
    assert evm_contract.selector("decimals()") == "0x313ce567"
    assert evm_contract.selector("balanceOf(address)") == "0x70a08231"


def test_transfer_topic_is_the_hashed_signature():
    """التوقيع المخزَّن في `evm_rpc` يجب أن يساوي حساب keccak لا نسخةً منقولة."""
    import evm_rpc

    computed = "0x" + evm_contract.keccak256(
        b"Transfer(address,address,uint256)"
    ).hex()
    assert computed == evm_rpc.TRANSFER_TOPIC


# ---------------------------------------------------------------------------
# قراءة البايت‑كود
# ---------------------------------------------------------------------------
def test_code_selectors_skips_push_immediates():
    """بايتة 0x63 **داخل** بيانات دفعٍ أخرى ليست تعليمة — قراءتها تخلق مُعرّفات
    وهميّة تجعل كل عقد يبدو حاملاً كل شيء."""
    # PUSH5 يحمل 0x63 وأربع بايتات بعدها: لو عُدّت تعليمةً لظهر مُعرّف وهميّ.
    body = "64" + "63aabbccdd"
    assert evm_contract._code_selectors("0x" + body) == set()


def test_code_selectors_finds_real_push4():
    sels = evm_contract._code_selectors(_code("owner()", "mint(uint256)"))
    assert evm_contract.selector("owner()") in sels
    assert len(sels) == 2


def test_analyze_flags_only_present_selectors():
    out = evm_contract.analyze_code(_code("mint(address,uint256)", "pause()"))
    assert out["has_mint"] == 1
    assert out["has_pause"] == 1
    assert out["has_blacklist"] == 0
    assert out["has_fee_setter"] == 0
    assert out["function_count"] == 2
    assert out["is_proxy"] == 0
    assert out["code_size"] == 10
    assert out["code_hash"].startswith("0x") and len(out["code_hash"]) == 66


def test_analyze_empty_code_is_a_measurement_not_a_gap():
    """`0x` = ليس عقداً (محفظة أو عقد حُذف) — والصفر قياس فيُكتب."""
    out = evm_contract.analyze_code("0x")
    assert out["code_size"] == 0
    assert out["code_hash"] is None
    assert out["function_count"] is None
    assert "has_mint" not in out          # لم يُقَس ⇒ يبقى NULL لا صفر


def test_analyze_detects_eip1167_proxy_and_implementation():
    impl = "1234567890abcdef1234567890abcdef12345678"
    body = evm_contract._PROXY_PREFIX + impl + evm_contract._PROXY_SUFFIX
    out = evm_contract.analyze_code("0x" + body)
    assert len(body) // 2 == 45
    assert out["is_proxy"] == 1
    assert out["impl_address"] == "0x" + impl


def test_analyze_rejects_45_bytes_that_are_not_a_proxy():
    """المطابقة على الطرفين لا الطول: عقد بطول 45 وبايتات أخرى ليس وكيلاً."""
    out = evm_contract.analyze_code("0x" + "ab" * 45)
    assert out["is_proxy"] == 0
    assert out["impl_address"] is None


# ---------------------------------------------------------------------------
# الدورة
# ---------------------------------------------------------------------------
class _RPC:
    """عميل مزيّف: `code` بايت‑كود لكل عنوان، و`owner` جواب `eth_call`."""

    def __init__(self, code=None, owner=None, fail=(), throttle=()):
        self.code = code or {}
        self.owner = owner or {}
        self.fail = set(fail)
        self.throttle = set(throttle)
        self.code_calls = []
        self.eth_calls = []

    async def get_code(self, network_id, address):
        self.code_calls.append((str(network_id), address))
        if address in self.throttle:
            raise EVMRateLimit("eth_getCode [8453] HTTP 429")
        if address in self.fail:
            raise RuntimeError("eth_getCode HTTP 503")
        return self.code.get(address, "0x")

    async def eth_call(self, network_id, to, data, block="latest"):
        self.eth_calls.append((to, data))
        want = self.owner.get(to)
        if want is None:
            return None                   # كل صيغ الملكيّة ترتدّ
        if data != evm_contract.selector(want[0]):
            return None                   # هذه الصيغة غير موجودة في العقد
        return _word(want[1])


async def _noop(_seconds):
    pass


async def test_cycle_writes_row_and_state(db):
    _watch(db)
    rpc = _RPC(
        code={TOK: _code("mint(address,uint256)", "setMaxTxAmount(uint256)")},
        owner={TOK: ("owner()", OWNER)},
    )

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats == {
        "evm_contract_due": 1, "evm_contract_rows": 1,
        "evm_contract_errors": 0, "evm_contract_proxies": 0,
        "evm_contract_throttled": 0,
    }
    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["network_id"] == BASE
    assert row["owner_address"] == OWNER
    assert row["is_ownership_renounced"] == 0
    assert row["has_mint"] == 1
    assert row["has_limit_setter"] == 1
    assert row["has_pause"] == 0
    assert decode_raw(row["raw_json"])["owner"] == OWNER
    state = db._conn.execute(
        "SELECT last_status, attempts FROM evm_contract_state"
    ).fetchone()
    assert state["last_status"] == "ok"
    assert state["attempts"] == 1


async def test_zero_owner_is_renounced_not_missing(db):
    _watch(db)
    rpc = _RPC(code={TOK: _code("owner()")}, owner={TOK: ("owner()", ZERO)})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["owner_address"] == ZERO
    assert row["is_ownership_renounced"] == 1


async def test_no_owner_function_stays_null(db):
    """«لا دالّة ملكيّة» ≠ «الملكيّة متروكة» — دمجهما يجعل العمود كذباً."""
    _watch(db)
    rpc = _RPC(code={TOK: _code("transfer(address,uint256)")})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["owner_address"] is None
    assert row["is_ownership_renounced"] is None
    # وقد جُرِّبت كل الصيغ قبل الاستسلام.
    assert len(rpc.eth_calls) == len(evm_contract._OWNER_CALLS)


async def test_owner_probes_are_paced_not_back_to_back(db):
    """ثلاث محاولات ملكيّة متلاصقة كتمت عقدة Base بـ429 في دورة حيّة.

    والكتم على `eth_call` لا يُبتلع بعد الإصلاح ⇒ الصفّ كلّه يُلغى ويُعاد. فبين
    كل صيغة وأختها إيقاعٌ، وتكلفته أجزاء ثانية من دورة ساعيّة.
    """
    _watch(db)
    rpc = _RPC(code={TOK: _code("transfer(address,uint256)")})
    slept = []

    async def _record(seconds):
        slept.append(seconds)

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_record)

    assert len(rpc.eth_calls) == len(evm_contract._OWNER_CALLS)
    # نداءٌ قبل الأوّل (بعد `eth_getCode`) وواحد بين كل صيغتين.
    assert len(slept) >= len(evm_contract._OWNER_CALLS)
    assert all(s == config.EVM_PACING_SECONDS for s in slept)


async def test_alternate_owner_signature_answers(db):
    """`getOwner()` صيغة شائعة على BSC؛ الارتداد الأوّل جوابٌ لا خطأ."""
    _watch(db)
    rpc = _RPC(code={TOK: _code("getOwner()")}, owner={TOK: ("getOwner()", OWNER)})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    row = db._conn.execute("SELECT owner_address FROM evm_contract").fetchone()
    assert row["owner_address"] == OWNER


async def test_non_contract_address_skips_owner_call(db):
    """`0x` ⇒ لا عقد فلا معنى لسؤال `owner()`؛ الصفّ يُكتب بالقياس (صفر حجم)."""
    _watch(db)
    rpc = _RPC()

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_rows"] == 1
    assert rpc.eth_calls == []
    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["code_size"] == 0
    assert row["has_mint"] is None            # لم يُقَس لا «غير موجود»
    assert row["function_count"] is None


async def test_proxy_is_counted(db):
    _watch(db)
    impl = "1234567890abcdef1234567890abcdef12345678"
    body = evm_contract._PROXY_PREFIX + impl + evm_contract._PROXY_SUFFIX
    rpc = _RPC(code={TOK: "0x" + body})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_proxies"] == 1
    row = db._conn.execute("SELECT is_proxy, impl_address FROM evm_contract").fetchone()
    assert row["is_proxy"] == 1
    assert row["impl_address"] == "0x" + impl


async def test_failure_marks_state_error_and_meta(db):
    _watch(db)
    rpc = _RPC(fail={TOK})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_errors"] == 1
    assert stats["evm_contract_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM evm_contract").fetchone()[0] == 0
    state = db._conn.execute(
        "SELECT last_status FROM evm_contract_state"
    ).fetchone()
    assert state["last_status"] == "error"
    assert "503" in (db.get_meta("last_error_evm_contract") or "")


async def test_only_configured_networks_are_scanned(db):
    """روبن‑هود وBSC مستثناتان بقياس: العمود هناك ثابت لا معلومة فيه."""
    _watch(db, "0xrh", network="4663")
    _watch(db, TOK, network=BASE)
    rpc = _RPC(code={TOK: _code("owner()")})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert [a for _, a in rpc.code_calls] == [TOK]


async def test_row_per_measurement_not_updated_in_place(db):
    """شطبُ الملكيّة **حدث** وسط النافذة؛ صفّ يُكتب فوق نفسه يمحو أنّه وقع."""
    from datetime import datetime, timedelta

    _watch(db)
    rpc = _RPC(code={TOK: _code("owner()")}, owner={TOK: ("owner()", OWNER)})
    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    later = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.EVM_CONTRACT_REFRESH_SECONDS + 1)
    ).isoformat()
    rpc.owner = {TOK: ("owner()", ZERO)}          # تُركت الملكيّة بين القياسين
    await evm_contract.run_evm_contract_cycle(rpc, db, later, sleep=_noop)

    rows = db._conn.execute(
        "SELECT recorded_at, is_ownership_renounced FROM evm_contract "
        "ORDER BY recorded_at"
    ).fetchall()
    assert [r["is_ownership_renounced"] for r in rows] == [0, 1]


async def test_fresh_row_is_not_refetched(db):
    _watch(db)
    rpc = _RPC(code={TOK: _code("owner()")})
    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.code_calls) == 1

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.code_calls) == 1               # ما زال طازجاً


async def test_empty_networks_tuple_scans_nothing(db, monkeypatch):
    _watch(db)
    monkeypatch.setattr(config, "EVM_CONTRACT_NETWORKS", ())
    rpc = _RPC(code={TOK: _code("owner()")})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.code_calls == []
    assert stats["evm_contract_due"] == 0


# ---------------------------------------------------------------------------
# الكتم: نهاية حصّة لا فشل عملة
# ---------------------------------------------------------------------------
OLD = "2026-08-13T11:00:00+00:00"
TOK2 = "0xbbbb000000000000000000000000000000000009"


async def test_throttle_stops_the_step_and_writes_no_state(db, monkeypatch):
    """429 = «انتهت حصّتنا» لا «هذه العملة معطوبة».

    حصّة `mainnet.base.org` مقيسة بالعدد (تسعة نداءات) لا بالتباعد ⇒ ما بعد أوّل
    كتم مكتومٌ سلفاً. فالخطوة تُقطَع، ولا حالة تُكتب: `error` تدفع العملة خلف
    ربع ساعة بلا ذنب، وبلا حالة تبقى أوّل المستحقّين في الدورة القادمة.
    """
    monkeypatch.setattr(config, "EVM_CONTRACT_PER_CYCLE", 2)
    _watch(db, TOK, when=NOW)                     # الأحدث ⇒ أوّلاً
    _watch(db, TOK2, when=OLD)
    rpc = _RPC(code={TOK: _code("owner()")}, throttle={TOK2})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_due"] == 2
    assert stats["evm_contract_rows"] == 1
    assert stats["evm_contract_errors"] == 0
    assert stats["evm_contract_throttled"] == 1
    # المكتومة بلا صفّ حالة إطلاقاً — لا 'ok' ولا 'error'.
    rows = db._conn.execute(
        "SELECT token_address, last_status FROM evm_contract_state"
    ).fetchall()
    assert [(r["token_address"], r["last_status"]) for r in rows] == [(TOK, "ok")]
    # ولا رسالة خطأ: الكتم ليس خطأً فلا يُشوّش لوحة الأخطاء.
    assert db.get_meta("last_error_evm_contract") is None
    assert db.get_meta("evm_contract_last_throttle_at") == NOW


async def test_throttled_token_is_measured_next_cycle(db, monkeypatch):
    """التقدّم مضمون: المكتومة تعود مستحقّة، ولا تدور الدورة على نفسها."""
    monkeypatch.setattr(config, "EVM_CONTRACT_PER_CYCLE", 2)
    _watch(db, TOK, when=NOW)
    _watch(db, TOK2, when=OLD)
    rpc = _RPC(code={TOK: _code("owner()")}, throttle={TOK2})
    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    rpc.throttle = set()                          # امتلأت الحصّة من جديد
    rpc.code[TOK2] = _code("pause()")
    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_due"] == 1          # الأولى ما زالت طازجة
    assert stats["evm_contract_rows"] == 1
    assert stats["evm_contract_throttled"] == 0
    row = db._conn.execute(
        "SELECT token_address, has_pause FROM evm_contract "
        "WHERE token_address=?", (TOK2,)
    ).fetchone()
    assert row["has_pause"] == 1


async def test_throttle_on_owner_probe_cancels_the_row_entirely(db, monkeypatch):
    """الكتم وسط صيغ الملكيّة لا يُبتلع: «لا مالك» المزعومة قياسٌ كاذب يُخزَّن."""
    monkeypatch.setattr(config, "EVM_CONTRACT_PER_CYCLE", 2)
    _watch(db, TOK)

    class _Throttling(_RPC):
        async def eth_call(self, network_id, to, data, block="latest"):
            self.eth_calls.append((to, data))
            raise EVMRateLimit("eth_call [8453] HTTP 429")

    rpc = _Throttling(code={TOK: _code("owner()")})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_rows"] == 0
    assert stats["evm_contract_throttled"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM evm_contract").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM evm_contract_state"
    ).fetchone()[0] == 0
