"""اختبارات طبقة EVM: فكّ السجلّ، القسمة عند القصّ، الدفتر، والدورة (بلا شبكة).

لا نداء شبكة في أي اختبار: العميل مزيّف يعيد سجلّات مصنوعة، والدفتر يُقرأ من
قاعدة مؤقّتة. ما يُتحقَّق منه هو ما يُفسده الصمت: رصيد ناقص، مدًى يُطبَّق مرّتين،
لقطة تُبنى على دفتر نصف معبَّأ.
"""
import os
import sqlite3

import pytest

import config
import evm_layer
import evm_rpc
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
NET = "4663"
TOK = "0xaaaa000000000000000000000000000000000001"

A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"
C = "0x3333333333333333333333333333333333333333"
ZERO = "0x0000000000000000000000000000000000000000"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token=TOK, *, network=NET, control=False):
    db.upsert_watch(token, network, "large_buy", f"sig-{token[:8]}", 48, NOW)
    if control:
        db._conn.execute(
            "UPDATE watchlist SET is_control=1, entry_signal_id=NULL "
            "WHERE token_address=?",
            (token,),
        )
        db._conn.commit()


def _topic(addr):
    return "0x" + "0" * 24 + addr[2:]


def _log(frm, to, value, block, token=TOK):
    """سجلّ `Transfer` خام كما تعيده العقدة (قيم ستّ‑عشريّة نصّاً)."""
    return {
        "address": token,
        "topics": [evm_rpc.TRANSFER_TOPIC, _topic(frm), _topic(to)],
        "data": hex(value),
        "blockNumber": hex(block),
    }


# ---------------------------------------------------------------------------
# فكّ السجلّ
# ---------------------------------------------------------------------------
def test_decode_transfer_reads_value_from_data_not_topics():
    rec = evm_rpc.decode_transfer(_log(A, B, 12_345, 900))
    assert rec == {
        "token_address": TOK, "from": A, "to": B, "value": 12_345, "block": 900,
    }


def test_decode_transfer_keeps_uint256_exactly():
    """قيمة تتجاوز 64 بتّاً: الحساب في بايثون بلا حدّ، ولو مرّ على float فسد."""
    big = 2 ** 200 + 7
    rec = evm_rpc.decode_transfer(_log(A, B, big, 1))
    assert rec["value"] == big


def test_decode_transfer_rejects_non_standard_event():
    """توقيع مشترك بحقول مختلفة: يُهمَل ولا يُخمَّن."""
    bad = _log(A, B, 1, 1)
    bad["topics"] = bad["topics"][:2]
    assert evm_rpc.decode_transfer(bad) is None


def test_decode_transfer_rejects_wrong_signature_and_malformed_words():
    wrong = _log(A, B, 1, 1)
    wrong["topics"][0] = "0x" + "11" * 32
    assert evm_rpc.decode_transfer(wrong) is None

    short_topic = _log(A, B, 1, 1)
    short_topic["topics"][1] = _topic(A)[:-2]
    assert evm_rpc.decode_transfer(short_topic) is None

    oversized_value = _log(A, B, 1, 1)
    oversized_value["data"] = "0x1" + "00" * 32
    assert evm_rpc.decode_transfer(oversized_value) is None

    bad_token = _log(A, B, 1, 1)
    bad_token["address"] = "not-an-address"
    assert evm_rpc.decode_transfer(bad_token) is None


def test_deltas_aggregate_and_sign():
    """عنوان يتحرّك مرّتين ⇒ كتابة واحدة، والإشارة تفرّق المرسل من المستلم."""
    logs = [_log(A, B, 100, 10), _log(B, C, 30, 11)]
    out = evm_layer._deltas_by_token(logs)
    assert out[TOK][A] == (-100, 10, None)
    assert out[TOK][B] == (70, 11, 10)        # +100 ثمّ −30
    assert out[TOK][C] == (30, 11, 11)


def test_deltas_keep_burn_address():
    """رصيد عنوان الصفر معلومة (كم حُرق) — الاستثناء موضعه حساب النسب."""
    out = evm_layer._deltas_by_token([_log(A, ZERO, 50, 5)])
    assert ZERO in out[TOK]


# ---------------------------------------------------------------------------
# القسمة عند القصّ
# ---------------------------------------------------------------------------
class _PagingRPC(evm_rpc.EVMRPC):
    """يورّث العميل الحقيقيّ ويستبدل `get_logs` وحدها: المقسوم هو ما نفحصه."""

    def __init__(self, limit_above=100, per_block=None):
        self.ranges = []
        self._limit_above = limit_above
        self._per_block = per_block or {}

    async def get_logs(self, network_id, addresses, from_block, to_block, topics=None):
        self.ranges.append((from_block, to_block))
        if to_block - from_block > self._limit_above:
            raise evm_rpc.EVMLogLimit("exceeds limit of 10000")
        out = []
        for blk in range(from_block, to_block + 1):
            out.extend(self._per_block.get(blk, []))
        return out


async def _noop(_seconds):
    pass


async def test_paging_halves_range_until_accepted():
    rpc = _PagingRPC(limit_above=100, per_block={150: [_log(A, B, 5, 150)]})

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=50, sleep=_noop,
    )

    assert complete is True
    assert resume == 400
    assert calls == len(rpc.ranges) > 1
    assert len(logs) == 1
    # لا فجوة ولا تراكب: القسمة تغطّي المدى كلّه بالضبط مرّة واحدة.
    covered = sorted(r for r in rpc.ranges if r[1] - r[0] <= 100)
    merged = []
    for lo, hi in covered:
        if merged and lo == merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    assert merged == [(0, 400)]


async def test_paging_uses_configured_range_hint_after_first_rejection(monkeypatch):
    rpc = _PagingRPC(limit_above=100)
    monkeypatch.setitem(config.EVM_LOG_RANGE_HINT, NET, 100)

    _logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=10, sleep=_noop,
    )

    assert complete is True
    assert resume == 400
    assert calls == 6
    assert rpc.ranges == [
        (0, 400), (0, 99), (100, 199), (200, 299), (300, 399), (400, 400),
    ]


async def test_contract_creation_block_uses_historical_code_binary_search():
    class _HistoricalCodeRPC:
        def __init__(self):
            self.blocks = []

        async def get_code_at(self, network_id, address, block):
            self.blocks.append((network_id, address, block))
            return "0x6000" if block >= 700 else "0x"

        async def historical_block_number(self, _network_id):
            return 1_000

    rpc = _HistoricalCodeRPC()

    block = await evm_rpc.EVMRPC.contract_creation_block(rpc, NET, TOK, 1_000)

    assert block == 700
    assert len(rpc.blocks) < 15


async def test_contract_creation_block_returns_none_for_an_eoa():
    class _NoCodeRPC:
        async def get_code_at(self, _network_id, _address, _block):
            return "0x"

        async def historical_block_number(self, _network_id):
            return 1_000

    assert await evm_rpc.EVMRPC.contract_creation_block(
        _NoCodeRPC(), NET, TOK, 1_000,
    ) is None


async def test_paging_reads_oldest_first():
    """الترتيب الزمنيّ شرط صحّة الرصيد حين يُثبَّت عند صفر."""
    rpc = _PagingRPC(limit_above=100)
    await rpc.get_logs_paged(NET, [TOK], 0, 400, max_calls=50, sleep=_noop)
    accepted = [r for r in rpc.ranges if r[1] - r[0] <= 100]
    assert accepted == sorted(accepted)


async def test_paging_stops_at_call_cap_and_reports_resume():
    """بلوغ السقف ⇒ `complete=False` وكتلة استئناف، لا «اكتمل» كذباً."""
    rpc = _PagingRPC(limit_above=10)

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 1000, max_calls=3, sleep=_noop,
    )

    assert calls == 3
    assert complete is False
    # الثلاثة كلّها قُصَّت فلم يُقرأ شيء ⇒ الاستئناف من أوّل المدى: هذا صدقٌ لا
    # تعثّر — القسمة تتقدّم في الدورة التاليّة بسقف حقيقيّ (24 نداءً).
    assert resume == 0
    assert logs == []


async def test_paging_raises_when_single_block_exceeds_limit():
    """كتلة واحدة تفوق السقف: لا قسمة ممكنة ⇒ استثناء لا حلقة أبديّة."""
    rpc = _PagingRPC(limit_above=-1)
    with pytest.raises(evm_rpc.EVMLogLimit):
        await rpc.get_logs_paged(NET, [TOK], 7, 7, max_calls=5, sleep=_noop)


async def test_paging_stops_at_time_budget_not_only_call_count():
    """السقف الزمنيّ هو ما يحمي الفترة: نداء واحد قد يعلَق 25 ثانية.

    مقيس على الدورة الحيّة الثانية: 72 نداءً استهلكت 118 ثانية والفترة 60 —
    فعدد النداءات لا يقول شيئاً عن الزمن. والخروج هنا بنقطة استئناف لا بخسارة.
    """
    import time

    rpc = _PagingRPC(limit_above=10)

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 1000, max_calls=50, sleep=_noop,
        deadline=time.monotonic() - 1,          # الميزانية منتهية سلفاً
    )

    # نداء واحد دائماً ولو انتهت الميزانية: بلا هذا تدور العملة بلا تقدّم أبداً.
    assert calls == 1
    assert complete is False
    assert resume == 0
    assert logs == []


async def test_paging_ignores_a_deadline_that_never_comes():
    """ميزانية واسعة ⇒ السلوك كما هو بلا فرق (لا تقصير خفيّ)."""
    import time

    rpc = _PagingRPC(limit_above=100, per_block={150: [_log(A, B, 5, 150)]})

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=50, sleep=_noop,
        deadline=time.monotonic() + 300,
    )

    assert (complete, resume, len(logs)) == (True, 400, 1)


# ---------------------------------------------------------------------------
# تصنيف ردّ العقدة: مهلة تُقسَم، كتم يُنتظَر
#
# كلا الحالتين مقيسة على أوّل دورة حيّة (2026-08-13): روبن‑هود ردّ على تعبئة من
# الكتلة صفر بـ`-32000 log query timed out`، ثمّ كتم النداءين بعدها بـ429.
# ---------------------------------------------------------------------------
class _Resp:
    """ردّ HTTP مزيّف بأقلّ ما يقرأه `_call`: الحالة والنصّ وjson()."""

    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("ليس json")
        return self._payload


class _ScriptedHTTP:
    """عميل httpx مزيّف: الردّ يُحسب من المدى المطلوب ورقم النداء."""

    def __init__(self, fn):
        self._fn = fn
        self.ranges = []

    async def post(self, url, json=None):                       # noqa: A002
        params = ((json or {}).get("params") or [{}])[0]
        params = params if isinstance(params, dict) else {}
        lo = int(str(params.get("fromBlock", "0x0")), 16)
        hi = int(str(params.get("toBlock", "0x0")), 16)
        self.ranges.append((lo, hi))
        return self._fn(lo, hi, len(self.ranges))


def _fake_rpc(fn):
    """عميل حقيقيّ بعميل HTTP مزيّف — بلا `__init__` كي لا يُفتح مقبس أصلاً."""
    rpc = evm_rpc.EVMRPC.__new__(evm_rpc.EVMRPC)
    rpc._urls = {NET: "http://node.invalid"}
    rpc._timeout = 1.0
    rpc._client = _ScriptedHTTP(fn)
    return rpc


def _err(code, message):
    return _Resp(payload={"jsonrpc": "2.0", "error": {"code": code, "message": message}})


def _ok(result):
    return _Resp(payload={"jsonrpc": "2.0", "result": result})


async def test_query_timeout_is_a_range_to_split_not_a_dead_end():
    """«المدى أوسع من طاقتي» بصياغة ثانية ⇒ نفس العلاج: القسمة.

    بلا هذا التصنيف تموت تعبئة العملة كلّها من أوّل مهلة — ووقع فعلاً في أوّل
    دورة حيّة: 3 من 58 عملة خرجت من الدفتر.
    """
    def fn(lo, hi, n):
        if hi - lo > 50:
            return _err(-32000, "log query timed out")
        return _ok([_log(A, B, 5, lo)])

    rpc = _fake_rpc(fn)
    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 200, max_calls=20, sleep=_noop,
    )

    assert complete is True
    assert resume == 200
    assert len(logs) == 4                     # أربعة أرباع كلٌّ منها ≤50 كتلة
    assert calls > 4                          # والقسمة نفسها كلّفت نداءات


async def test_rate_limit_waits_and_repeats_the_same_range():
    """الكتم يُنتظَر ولا يُقسَم: نصفُ المدى يضاعف النداءات فيزيد الكتم."""
    def fn(lo, hi, n):
        if n == 1:
            return _Resp(status=429, text='{"error":{"code":429}}')
        return _ok([_log(A, B, 5, lo)])

    rpc = _fake_rpc(fn)
    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 100, max_calls=5, sleep=_noop,
    )

    assert complete is True
    assert rpc._client.ranges == [(0, 100), (0, 100)]      # نفس المدى لا نصفه
    assert len(logs) == 1


async def test_rate_limit_inside_a_200_body_is_also_classified():
    """بعض العقد تكتم بـ200 وكتلة `error` — التصنيف بالمعنى لا بحالة HTTP."""
    def fn(lo, hi, n):
        if n == 1:
            return _err(429, "Too Many Requests")
        return _ok([])

    rpc = _fake_rpc(fn)
    logs, calls, complete, _ = await rpc.get_logs_paged(
        NET, [TOK], 0, 10, max_calls=5, sleep=_noop,
    )

    assert complete is True
    assert rpc._client.ranges == [(0, 10), (0, 10)]


async def test_revert_is_an_answer_but_rate_limit_is_not():
    """`eth_call` يبتلع الارتداد («لا هذه الدالّة») ولا يبتلع الكتم.

    ابتلاع الكتم يكتب «لا مالك لهذا العقد» — معلومةٌ كاذبة تُخزَّن كأنّها مقيسة.
    """
    reverting = _fake_rpc(lambda lo, hi, n: _err(3, "execution reverted"))
    assert await reverting.eth_call(NET, TOK, "0x8da5cb5b") is None

    muted = _fake_rpc(lambda lo, hi, n: _Resp(status=429, text="Too Many Requests"))
    with pytest.raises(evm_rpc.EVMRateLimit):
        await muted.eth_call(NET, TOK, "0x8da5cb5b")


# ---------------------------------------------------------------------------
# الدفتر
# ---------------------------------------------------------------------------
def test_ledger_applies_signed_deltas_and_ranks_by_value(db):
    db.evm_apply_transfers(NET, TOK, {A: (300, 10), B: (100, 10)}, NOW)
    db.evm_apply_transfers(NET, TOK, {A: (-250, 11), C: (250, 11)}, NOW)

    top = db.evm_top_balances(NET, TOK, 10)
    assert top == [(C, 250), (B, 100), (A, 50)]
    assert db.evm_ledger_stats(NET, TOK) == {"holder_count": 3, "supply": 400}


def test_ledger_chunks_holder_lookup_below_sqlite_variable_limit(db):
    original_limit = db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
    holders = {
        f"0x{i:040x}": (1, 10)
        for i in range(40)
    }
    try:
        assert db.evm_apply_transfers(NET, TOK, holders, NOW) == 40
    finally:
        db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_limit)

    assert db.evm_ledger_stats(NET, TOK) == {"holder_count": 40, "supply": 40}


def test_ledger_ranks_beyond_64_bit(db):
    """الترتيب معجميّ على نصّ محشوّ ⇒ مطابق للعدديّ فوق حدّ SQLite."""
    small, huge = 2 ** 63 + 1, 2 ** 200
    db.evm_apply_transfers(NET, TOK, {A: (small, 1), B: (huge, 1)}, NOW)
    assert db.evm_top_balances(NET, TOK, 2) == [(B, huge), (A, small)]


def test_ledger_rejects_negative_regular_holder(db):
    """السالب لعنوان عادي دليل فقد/تكرار، وليس صفراً يجوز تخزينه."""
    with pytest.raises(Exception, match="سالب"):
        db.evm_apply_transfers(NET, TOK, {A: (-500, 9)}, NOW)
    assert db.evm_ledger_stats(NET, TOK)["holder_count"] == 0


def test_ledger_allows_negative_burn_source_without_storing_it(db):
    """عنوان الصفر يرسل عند السكّ، فسالبُه متوقع لكنه لا يصبح حائزاً."""
    db.evm_apply_transfers(
        NET, TOK, {ZERO: (-500, 9), A: (500, 9)}, NOW,
        allow_negative=(ZERO,),
    )
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 500)]


def test_ledger_first_seen_block_never_moves(db):
    """«حائز جديد» = أوّل دخول لا آخر حركة."""
    db.evm_apply_transfers(NET, TOK, {A: (10, 100)}, NOW)
    db.evm_apply_transfers(NET, TOK, {A: (10, 500)}, NOW)
    row = db._conn.execute(
        "SELECT first_seen_block, updated_block FROM evm_balances "
        "WHERE holder_address=?", (A,),
    ).fetchone()
    assert row["first_seen_block"] == 100
    assert row["updated_block"] == 500
    assert db.evm_new_holders_since(NET, TOK, 50) == 1
    assert db.evm_new_holders_since(NET, TOK, 200) == 0


def test_first_seen_uses_first_receipt_even_when_net_delta_is_zero(db):
    deltas = evm_layer._deltas_by_token([
        _log(ZERO, A, 100, 10), _log(A, B, 100, 11),
    ])
    db.evm_apply_transfers(
        NET, TOK, deltas[TOK], NOW, allow_negative=evm_rpc.BURN_ADDRESSES,
    )
    row = db._conn.execute(
        "SELECT balance_hex, first_seen_block FROM evm_balances "
        "WHERE holder_address=?", (A,),
    ).fetchone()
    assert int(row["balance_hex"], 16) == 0
    assert row["first_seen_block"] == 10


def test_ledger_excludes_burn_addresses_from_ratios(db):
    db.evm_apply_transfers(NET, TOK, {A: (100, 1), ZERO: (900, 1)}, NOW)
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 1000
    stats = db.evm_ledger_stats(NET, TOK, exclude=evm_rpc.BURN_ADDRESSES)
    assert stats == {"holder_count": 1, "supply": 100}


# ---------------------------------------------------------------------------
# صفّ اللقطة
# ---------------------------------------------------------------------------
def test_concentration_row_computes_tiers_from_live_supply():
    top = [(f"0x{i:040x}", 100) for i in range(20)]
    row = evm_layer.build_evm_concentration_row(
        {"supply": 2_000, "holder_count": 40}, top, TOK, NET, NOW, NOW, "sig-1",
    )
    assert row["top1_pct"] == pytest.approx(5.0)
    assert row["top5_pct"] == pytest.approx(25.0)
    assert row["top20_pct"] == pytest.approx(100.0)
    assert row["holder_count"] == 40
    assert row["top_accounts"] == 20
    assert row["raw_json"]["supply_base"] == "2000"


def test_concentration_row_sorts_before_slicing():
    row = evm_layer.build_evm_concentration_row(
        {"supply": 1_000, "holder_count": 4},
        [(A, 100), (B, 700), (C, 200)], TOK, NET, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(70.0)


def test_concentration_row_keeps_precision_at_eighteen_decimals():
    huge = 10 ** 27 + 1
    row = evm_layer.build_evm_concentration_row(
        {"supply": huge, "holder_count": 1}, [(A, huge)], TOK, NET, NOW, NOW,
        None, decimals=18,
    )
    assert row["top1_pct"] == pytest.approx(100.0)


def test_concentration_row_none_when_ledger_empty():
    """دفتر فارغ ≠ عملة بلا حائزين: لا صفّ أصفار (FR-007)."""
    assert evm_layer.build_evm_concentration_row(
        {"supply": 0, "holder_count": 0}, [], TOK, NET, NOW, NOW, None,
    ) is None


# ---------------------------------------------------------------------------
# الدورة
# ---------------------------------------------------------------------------
class _CycleRPC:
    """عميل EVM مزيّف: سجلّات ثابتة تُرشَّح بالمدى والعنوان كما تفعل العقدة."""

    def __init__(self, head=1_000, logs=(), fail=()):
        self.head = head
        self._logs = list(logs)
        self._fail = set(fail)
        self.ranges = []
        self.heads = []

    async def block_number(self, network_id):
        self.heads.append(str(network_id))
        if str(network_id) in self._fail:
            raise evm_rpc.EVMRPCError(f"eth_blockNumber [{network_id}] HTTP 503")
        return self.head

    async def get_logs_paged(
        self, network_id, addresses, from_block, to_block, topics=None,
        max_calls=None, sleep=None, deadline=None,
    ):
        self.ranges.append((str(network_id), tuple(addresses), from_block, to_block))
        want = {a.lower() for a in addresses}
        out = [
            log for log in self._logs
            if log["address"].lower() in want
            and from_block <= int(log["blockNumber"], 16) <= to_block
        ]
        return out, 1, True, int(to_block)


async def test_first_cycle_seeds_cursor_then_backfills_then_snapshots(db):
    """دورة واحدة على قاعدة فارغة: مؤشّر، فتعبئة كل التاريخ، فلقطة."""
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5), _log(A, B, 200, 6)])

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_cursor_init"] == 1
    assert stats["evm_backfilled"] == 1
    assert stats["evm_snapshots"] == 1
    assert stats["evm_errors"] == 0
    # المؤشّر عند الرأس ناقص التأكيدات لا عند الرأس.
    assert db.evm_cursor(NET)["last_block"] == 1_000 - config.EVM_CONFIRMATIONS
    # التعبئة من الكتلة صفر: الرصيد تراكم لا معدّل.
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 500), (B, 200)]
    row = db._conn.execute(
        "SELECT top1_pct, holder_count, top_accounts, network_id "
        "FROM chain_concentration"
    ).fetchone()
    assert row["network_id"] == NET
    assert row["holder_count"] == 2
    assert row["top1_pct"] == pytest.approx(500 / 700 * 100)
    assert decode_raw(
        db._conn.execute("SELECT raw_json FROM chain_concentration").fetchone()["raw_json"]
    )["source"] == "evm_ledger"


async def test_second_cycle_does_not_reapply_backfilled_range(db):
    """التطبيق **بعد** المؤشّر وحده: مدًى يُطبَّق مرّتين يضاعف كل رصيد."""
    _watch(db)
    old = _log(ZERO, A, 700, 5)
    rpc = _CycleRPC(head=1_000, logs=[old])
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]

    rpc.head = 2_000                                  # كتل جديدة، ونفس السجلّ القديم
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_due"] == 0             # التعبئة انتهت ولا تُعاد
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]
    applied = [r for r in rpc.ranges if r[2] > 5]
    assert applied, "الدورة الثانية يجب أن تطبّق ما بعد المؤشّر"


async def test_new_token_after_cursor_is_backfilled_without_live_double_apply(db):
    """غير المعبّأة لا تدخل التطبيق الحيّ؛ وإلا تكرر المدى الحديث في backfill."""
    db.set_evm_cursor(NET, 100, NOW, "ok")
    _watch(db)
    rpc = _CycleRPC(head=200, logs=[_log(ZERO, A, 700, 150)])

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 1
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]
    # نداء واحد هو التعبئة. التطبيق الحي لا يسأل عملة لم يكتمل دفترها بعد.
    assert len(rpc.ranges) == 1


async def test_base_backfill_starts_at_contract_creation_block(db, monkeypatch):
    """عملة Base الجديدة لا تمسح الكتل الفارغة التي سبقت إنشاء عقدها."""
    net = "8453"
    creation = 850
    _watch(db, network=net)
    db.set_evm_cursor(net, 988, NOW, "ok")
    monkeypatch.setattr(config, "EVM_NETWORKS", (net,))
    monkeypatch.setattr(config, "EVM_CREATION_BLOCK_NETWORKS", (net,))

    class _CreationRPC(_CycleRPC):
        async def contract_creation_block(self, network_id, address, head):
            assert (network_id, address, head) == (net, TOK, 988)
            return creation

    rpc = _CreationRPC(
        head=1_000,
        logs=[_log(ZERO, A, 700, creation, token=TOK)],
    )

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 1
    assert rpc.ranges[0][2:] == (creation, 988)
    assert db.evm_top_balances(net, TOK, 10) == [(A, 700)]


async def test_apply_uses_only_common_completed_prefix_across_address_batches(
    db, monkeypatch,
):
    """تأخر دفعة لا يسمح بكتابة مستقبل دفعة أخرى ثم إعادته في الدورة التالية."""
    tok2 = "0xbbbb000000000000000000000000000000000002"
    _watch(db, TOK)
    _watch(db, tok2)
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=101, to_block=100)
    db.set_evm_backfill_state(NET, tok2, "done", NOW, from_block=101, to_block=100)
    db.set_evm_cursor(NET, 100, NOW, "ok")
    monkeypatch.setitem(config.EVM_ADDRESS_BATCH, NET, 1)

    class _Uneven(_CycleRPC):
        def __init__(self):
            super().__init__(head=200)
            self.round = 0

        async def get_logs_paged(
            self, network_id, addresses, from_block, to_block, topics=None,
            max_calls=None, sleep=None, deadline=None,
        ):
            self.ranges.append((str(network_id), tuple(addresses), from_block, to_block))
            token = addresses[0].lower()
            if self.round == 0 and token == tok2:
                return [], 1, False, 130
            logs = [_log(A, B, 100, 150, token=TOK)] if token == TOK else []
            return logs, 1, True, int(to_block)

    rpc = _Uneven()
    db.evm_apply_transfers(NET, TOK, {A: (100, 100)}, NOW)
    first = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert first["evm_lagging"] == 1
    assert db.evm_cursor(NET)["last_block"] == 129
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 100)]

    rpc.round = 1
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_top_balances(NET, TOK, 10) == [(B, 100)]


async def test_apply_rolls_back_balances_when_cursor_write_fails(db, monkeypatch):
    """الأرصدة والمؤشر وحدة ذرية؛ فشل المؤشر لا يترك تحويلات ستُعاد."""
    _watch(db)
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=101, to_block=100)
    db.set_evm_cursor(NET, 100, NOW, "ok")
    rpc = _CycleRPC(head=200, logs=[_log(ZERO, A, 700, 150)])
    original = db.set_evm_cursor

    failed = False

    def _fail_cursor(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("cursor write failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "set_evm_cursor", _fail_cursor)
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    monkeypatch.setattr(db, "set_evm_cursor", original)

    assert stats["evm_errors"] == 1
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0
    assert db.evm_cursor(NET)["last_block"] == 100


async def test_apply_aborts_when_repair_changes_ledger_generation(db):
    _watch(db)
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=101, to_block=100)
    db.set_evm_cursor(NET, 100, NOW, "ok")

    class _ResetDuringFetch(_CycleRPC):
        async def get_logs_paged(self, *args, **kwargs):
            db.bump_evm_ledger_generation()
            return await super().get_logs_paged(*args, **kwargs)

    rpc = _ResetDuringFetch(head=200, logs=[_log(ZERO, A, 700, 150)])
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_errors"] == 0
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0


async def test_apply_does_not_advance_past_a_backfill_completed_during_head_fetch(db):
    _watch(db)
    db.set_evm_cursor(NET, 100, NOW, "ok")
    db_path = db._conn.execute("PRAGMA database_list").fetchone()["file"]
    other = RecorderDB(db_path, SCHEMA)

    class _BackfillCompletesDuringHead(_CycleRPC):
        async def block_number(self, network_id):
            other.set_evm_backfill_state(
                NET, TOK, "done", NOW, from_block=101, to_block=100,
            )
            return await super().block_number(network_id)

    try:
        stats = await evm_layer.run_evm_cycle(
            _BackfillCompletesDuringHead(head=200), db, NOW, sleep=_noop,
        )
    finally:
        other.close()

    assert stats["evm_errors"] == 0
    assert db.evm_cursor(NET)["last_block"] == 100


async def test_new_transfer_after_cursor_is_applied(db):
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    rpc.head = 2_000
    rpc._logs.append(_log(A, B, 300, 1_500))
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert db.evm_top_balances(NET, TOK, 10) == [(A, 400), (B, 300)]


async def test_partial_backfill_blocks_snapshot(db):
    """لقطة عملة نصف معبَّأة رقمٌ كاذب لا رقم ناقص."""
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])

    async def _partial(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        # نصف المدى وحده قُرِئ ⇒ الاستئناف من منتصفه.
        mid = (from_block + to_block) // 2
        return [], 1, False, mid

    rpc.get_logs_paged = _partial
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_partial"] == 1
    assert stats["evm_snapshots"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration"
    ).fetchone()[0] == 0
    state = db.evm_backfill_state(NET, TOK)
    assert state["status"] == "partial"
    assert state["from_block"] > 0                    # يُستأنف لا يُعاد من الصفر


async def test_completed_old_backfill_catches_up_before_done(db):
    """عملة مستثناة من التطبيق الحي تلحق ما تقدم أثناء تعبئتها قبل اللقطة."""
    _watch(db)
    db.set_evm_cursor(NET, 200, NOW, "ok")
    db.set_evm_backfill_state(
        NET, TOK, "partial", NOW, from_block=50, to_block=100,
    )
    rpc = _CycleRPC(head=300, logs=[_log(ZERO, A, 700, 75)])

    first = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    state = db.evm_backfill_state(NET, TOK)
    assert first["evm_backfill_partial"] == 1
    assert state["status"] == "partial"
    assert (state["from_block"], state["to_block"]) == (101, 288)
    assert first["evm_snapshots"] == 0

    rpc._logs.append(_log(A, B, 200, 250))
    second = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert second["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOK)["status"] == "done"
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 500), (B, 200)]


async def test_incomplete_apply_pulls_cursor_back(db):
    """دفعة لم تكتمل ⇒ المؤشّر يتوقّف قبل الفجوة؛ التقدّم يفقد سجلّات للأبد."""
    _watch(db)
    rpc = _CycleRPC(head=1_000)
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    seeded = db.evm_cursor(NET)["last_block"]

    async def _incomplete(network_id, addresses, from_block, to_block, topics=None,
                          max_calls=None, sleep=None, deadline=None):
        return [], 1, False, from_block + 10

    rpc.get_logs_paged = _incomplete
    rpc.head = 5_000
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_lagging"] == 1
    # `resume − 1`: أوّل كتلة غير مقروءة هي (المؤشّر+1)+10 فالمؤشّر يقف قبلها.
    assert db.evm_cursor(NET)["last_block"] == seeded + 10


async def test_network_error_keeps_valid_cursor_and_records_meta(db):
    _watch(db)
    rpc = _CycleRPC(head=1_000)
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)   # يبني المؤشّر

    rpc._fail = {NET}
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_errors"] == 1
    cursor = db.evm_cursor(NET)
    # المؤشر يصف آخر كتلة مطبقة فعلاً؛ خطأ قراءة الرأس لا يبطل ذلك التقدم،
    # وكتابته `error` هنا قد تخفض حالة عامل متزامن نجح بعدنا.
    assert cursor["last_status"] == "ok"
    assert cursor["last_error"] is None
    assert "503" in (db.get_meta("last_error_evm") or "")


async def test_empty_networks_tuple_touches_nothing(db, monkeypatch):
    """قائمة فارغة تعني «لا شيء» لا «كل الشبكات»."""
    _watch(db)
    monkeypatch.setattr(config, "EVM_NETWORKS", ())
    rpc = _CycleRPC(head=1_000)

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.heads == []
    assert stats["evm_networks"] == 0


async def test_solana_watch_is_never_touched(db):
    """الشبكات منفصلة: عملة سولانا لا تدخل دفتر EVM ولا تُسأل عنه."""
    _watch(db, "SoLmint111", network=config.SOLANA_NETWORK_ID)
    rpc = _CycleRPC(head=1_000)

    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert all(TOK not in addrs for _, addrs, _, _ in rpc.ranges)
    assert db._conn.execute("SELECT COUNT(*) FROM evm_balances").fetchone()[0] == 0


async def test_backfill_limit_error_is_recorded_and_not_retried_every_cycle(db):
    """كتلة واحدة تفوق السقف: تُسجَّل `error` ولا تُعاد كل دقيقة."""
    _watch(db)
    rpc = _CycleRPC(head=1_000)

    async def _boom(network_id, addresses, from_block, to_block, topics=None,
                    max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMLogLimit("eth_getLogs: 10000 سجلّاً ⇒ السقف بلغ")

    rpc.get_logs_paged = _boom
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_errors"] == 1
    state = db.evm_backfill_state(NET, TOK)
    assert state["status"] == "error"
    assert "EVMLogLimit" in state["last_error"]

    # والدورة التالية لا تعيدها ولو صحّت العقدة: العطب دائم لا عابر.
    del rpc.get_logs_paged
    again = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert again["evm_backfill_due"] == 0


async def test_transient_backfill_failure_is_requeued_not_buried(db):
    """عطبٌ عابر ⇒ `retry` ويعود في الدورة التالية.

    مقيس على أوّل دورة حيّة: مهلة استعلام وكتمان 429 أخرجا 3 من 58 عملة من
    الدفتر **إلى الأبد** حين كان كل فشل يُكتب `error`. الفرق بين الحالتين هو
    الفرق بين ثانيةٍ سيّئة وعطبٍ لا علاج له.
    """
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])

    async def _stumble(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMRPCError("eth_getLogs [4663] HTTP 503")

    rpc.get_logs_paged = _stumble
    first = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert first["evm_backfill_retry"] == 1
    assert first["evm_backfill_errors"] == 0        # عابر ⇒ ليس في عدّاد الدائم
    assert first["evm_snapshots"] == 0              # لا لقطة قبل دفتر مكتمل
    state = db.evm_backfill_state(NET, TOK)
    assert state["status"] == "retry"
    assert "503" in (state["last_error"] or "")

    del rpc.get_logs_paged                          # العقدة أفاقت
    second = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert second["evm_backfill_due"] == 1          # عادت إلى الطابور
    assert second["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOK)["status"] == "done"
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]


async def test_retry_goes_to_the_tail_of_the_queue(db, monkeypatch):
    """الترتيب بوقت المحاولة: من جُرِّب الآن يُجرَّب آخراً.

    بلا هذا تحتلّ عملةٌ متعثّرة سقفَ الدورة كل دقيقة فلا تُعبَّأ الجديدات أبداً.
    """
    monkeypatch.setattr(config, "EVM_BACKFILL_TOKENS_PER_CYCLE", 1)
    _watch(db)
    rpc = _CycleRPC(head=1_000)

    async def _stumble(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMRPCError("eth_getLogs [4663] HTTP 503")

    rpc.get_logs_paged = _stumble
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_backfill_state(NET, TOK)["status"] == "retry"

    fresh = "0xbbbb000000000000000000000000000000000002"
    _watch(db, fresh)
    del rpc.get_logs_paged
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    # الجديدة (بلا وقت محاولة) سبقت المتعثّرة، والسقف عملةٌ واحدة.
    assert db.evm_backfill_state(NET, fresh)["status"] == "done"
    assert db.evm_backfill_state(NET, TOK)["status"] == "retry"


async def test_retry_resumes_from_its_saved_block_and_never_doubles(db):
    """`retry` يستأنف من نقطته: البدء من الصفر يضاعف كل رصيد مطبَّق سلفاً.

    نقطة الاستئناف تبقى محفوظة عند الفشل العابر (`COALESCE`)، فإعادة المدى
    كلّه ليست إبطاءً بل **أرقاماً كاذبة**: `evm_apply_transfers` يجمع لا يستبدل.
    """
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])

    async def _partial(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        return [_log(ZERO, A, 700, 5)], 1, False, 400

    rpc.get_logs_paged = _partial
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]

    # ثمّ تعثّر عابر: الحالة `retry` والنقطة 400 محفوظة.
    async def _stumble(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMRPCError("eth_getLogs [4663] HTTP 429")

    rpc.get_logs_paged = _stumble
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    state = db.evm_backfill_state(NET, TOK)
    assert (state["status"], state["from_block"]) == ("retry", 400)

    seen = []

    async def _record(network_id, addresses, from_block, to_block, topics=None,
                      max_calls=None, sleep=None, deadline=None):
        seen.append((from_block, to_block))
        return [], 1, True, to_block

    rpc.get_logs_paged = _record
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert seen and seen[-1][0] == 400            # لا من الصفر
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]   # ولا مضاعفة


async def test_backfill_rolls_back_balances_when_state_write_fails(db, monkeypatch):
    """حالة التعبئة ورصيدها معاملة واحدة؛ لا checkpoint يعني لا رصيد مثبت."""
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])
    original = db.set_evm_backfill_state
    failed = False

    def _fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("backfill state write failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "set_evm_backfill_state", _fail_once)
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_retry"] == 1
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0
    assert db.evm_backfill_state(NET, TOK)["status"] == "retry"


async def test_backfill_stops_starting_tokens_when_the_budget_is_spent(db, monkeypatch):
    """ميزانية الزمن تحمي الفترة: الأولى تُعبَّأ والبقيّة تنتظر الدورة القادمة.

    الطبقة السريعة على سولانا في نفس العملية، ودورةٌ من 118 ثانية (مقيسة) تكسر
    إيقاعها الخمس‑دقائقيّ. والتأخير هنا بلا فقدان: التعبئة تُستأنف من نقطتها.
    """
    _watch(db)
    _watch(db, "0xbbbb000000000000000000000000000000000003")
    rpc = _CycleRPC(head=1_000)
    # ميزانية منتهية سلفاً ⇒ الأولى تمضي (نداء واحد مضمون) والثانية تُؤخَّر.
    monkeypatch.setattr(config, "EVM_BACKFILL_BUDGET_SECONDS", -1.0)

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_due"] == 2
    assert stats["evm_backfilled"] == 1
    assert stats["evm_backfill_skipped"] == 1
    assert stats["evm_backfill_errors"] == 0
    assert stats["evm_backfill_retry"] == 0
    # المؤخَّرة بلا صفّ حالة أصلاً ⇒ تتصدّر طابور الدورة القادمة.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM evm_backfill_state"
    ).fetchone()[0] == 1
