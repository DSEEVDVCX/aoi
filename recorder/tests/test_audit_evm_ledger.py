"""اختباراتُ مدقّقِ الدفتر: ما يمسكه، وما يرفض أن يُسمّيه مطابقاً.

القيمةُ هنا كلُّها في الأحكام. أداةُ تدقيقٍ تقول «مطابق» لدفترٍ مقصوصٍ أسوأُ من
عدمِها: كانت المصيدةُ ستبقى مخفيّةً، ومعها *شهادةُ سلامة*. فالمفحوصُ بالضبط:
كتلةُ الحدّ تُوجَد بطوابعَ حقيقيّة، والنقصُ يُسمّى نقصاً، والانزياحُ الطفيفُ لا
يُصرَخ به، والمفتاحُ لا يظهر في أيّ رسالةِ خطأ.
"""
import json
import os

import audit_evm_ledger as audit
import pytest
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NET = "8453"
TOK = "0xaaaa000000000000000000000000000000000001"
# 2026-08-15T18:15:00+00:00 = 1786** — يُحسب لا يُكتب يدويّاً كي لا يفترق الاثنان.
STAMP = "2026-08-15T18:15:00+00:00"
WHALES = [f"0x{index:040x}" for index in range(1, 6)]


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


class FakeRPC:
    """عقدةُ أرشيفٍ مزيّفة: كتلةٌ كلَّ ثانيةٍ، وأرصدةٌ ثابتةٌ لا تعتمد على الكتلة.

    ثباتُ الأرصدة مقصود: الاختباراتُ تفحص **حكمَ** المدقّق لا حسابَ السلسلة، فأيُّ
    اعتمادٍ على الكتلة يخلط فشلَ بحثِ الحدّ بفشلِ المقارنة.
    """

    def __init__(self, *, supply, balances, burned=0, genesis=1_000_000_000,
                 head=2_000, fail_at=None):
        self.supply, self.balances, self.burned = supply, balances, burned
        self.genesis, self.head, self.fail_at = genesis, head, fail_at
        self.calls = 0
        self.asked = []

    def block_number(self):
        self.calls += 1
        return self.head

    def block_timestamp(self, block):
        self.calls += 1
        if block > self.head:
            raise audit.ArchiveError(f"لا كتلةَ عند {block}")
        return self.genesis + int(block)

    def total_supply(self, token, block):
        self.calls += 1
        if self.fail_at == "supply":
            raise audit.ArchiveError("eth_call: أرشيفٌ غائب")
        return self.supply

    def balance_of(self, token, holder, block):
        self.calls += 1
        self.asked.append((holder, block))
        if holder in audit.BURN_ADDRESSES:
            return self.burned if holder.endswith("dead") else 0
        return self.balances.get(holder, 0)


def _seed(db, *, balances, supply_base=None, pct=None, stamp=STAMP):
    """لقطةُ إعادةٍ واحدةٌ بأرصدةٍ معلومة — المقامُ مجموعُها إن لم يُذكر غيره."""
    total = supply_base if supply_base is not None else sum(balances.values())
    top = sorted(balances.items(), key=lambda item: -item[1])
    shares = pct or {
        f"top{n}_pct": sum(v for _, v in top[:n]) / total * 100 for n in (1, 5, 10, 20)
    }
    db.upsert_watch(TOK, NET, "trending", "sig", 48, stamp)
    assert db.insert_chain_concentration({
        "token_address": TOK, "network_id": NET, "recorded_at": stamp,
        "watch_first_seen_at": stamp, "is_control": 0, "is_replay": 1,
        "holder_count": len(balances), "top_accounts": len(top),
        "decimals": 18, "supply": float(total),
        **shares,
        "raw_json": {
            "source": "evm_ledger", "supply_base": str(total),
            "holder_count": len(balances),
            "top": [[address, str(value)] for address, value in top],
        },
    })
    return dict(db._conn.execute(
        "SELECT * FROM chain_concentration WHERE recorded_at = ?", (stamp,),
    ).fetchone())


def test_a_complete_ledger_is_called_matching(db):
    """الحالةُ السليمة: المجموعُ يساوي المعروضَ، وكلُّ حائزٍ يطابق ⇒ «مطابق»."""
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)
    rpc = FakeRPC(supply=500, balances=balances)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "مطابق", record
    assert record["holders_matched"] == 5
    assert record["coverage_pct"] == pytest.approx(100.0)


def test_a_truncated_ledger_is_called_deficient_not_matching(db):
    """المصيدةُ نفسُها: كلُّ حائزٍ نعرفه صحيحٌ، والدفترُ مع ذلك ناقص.

    هذا بالضبط ما يخلّفه ردٌّ مقصوصٌ عند الصفحة الأولى: العناوينُ التي وصلَتنا
    أرصدتُها سليمة، والغائبُ غائبٌ بلا أثر. فلو كان الحكمُ على الحائزين وحدَهم
    لقال «مطابق» — والمتطابقةُ مع `totalSupply()` هي التي تكشفه.
    """
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)                 # الدفتر يعرف 500
    rpc = FakeRPC(supply=1_000, balances=balances)     # والسلسلة تقول 1000
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "ناقص", record
    assert record["holders_matched"] == 5              # ولا حائزَ واحدٌ مخطئ
    assert record["coverage_pct"] == pytest.approx(50.0)
    assert "لم تُقرأ" in record["note"]


def test_burned_supply_is_added_back_before_judging_completeness(db):
    """عملةٌ حُرق نصفُها ليست دفتراً ناقصاً — والفرقُ حسابُ عناوين الحرق."""
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)
    rpc = FakeRPC(supply=1_000, balances=balances, burned=500)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "مطابق", record
    assert record["burned"] == 500
    assert record["coverage_pct"] == pytest.approx(100.0)


def test_a_transfer_missed_between_two_known_holders_is_caught_by_balances(db):
    """المجموعُ سليمٌ والتوزيعُ لا — وهذه العطبةُ لا يمسكها سؤالُ الاكتمالِ أبداً.

    تحويلٌ ضائعٌ **بين حائزَين نعرفهما** لا يغيّر المجموع: ما نقص من هذا زاد في
    ذاك. فالمتطابقةُ مع `totalSupply()` تمرّ بامتياز، والتركّزُ — وهو كلُّ ما
    نقيسه — مغلوط. ولهذا لا يكفي أحدُ السؤالين عن الآخر.
    """
    stored = {address: 100 for address in WHALES}
    row = _seed(db, balances=stored)
    chain = {**stored, WHALES[0]: 40, WHALES[1]: 160}   # ستّون انتقلت بلا سجلّ
    rpc = FakeRPC(supply=500, balances=chain)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "تفاوت", record
    assert record["coverage_pct"] == pytest.approx(100.0)   # الاكتمالُ يمرّ
    assert record["holders_matched"] == 3
    assert record["delta_points"] == pytest.approx(12.0)    # top1: 20% ← 32%
    assert record["worst_holder"]["address"] in (WHALES[0], WHALES[1])


def test_a_hair_of_drift_is_not_screamed_about(db):
    """فارقٌ دون عُشرِ نقطةٍ يُسمّى انزياحاً: كتلةُ حدٍّ تحرّك رصيداً، وهذا متوقّع.

    بلا هذا التمييز تصير الأداةُ عديمةَ النفع بكثرةِ إنذارها — وأداةٌ يُتجاهَل
    إنذارُها لا تُنذر.
    """
    stored = {address: 1_000_000 for address in WHALES}
    row = _seed(db, balances=stored)
    chain = {**stored, WHALES[0]: 1_000_001}
    rpc = FakeRPC(supply=5_000_001, balances=chain)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "انزياح", record
    assert record["delta_points"] < audit.DRIFT_POINTS


def test_the_boundary_block_is_the_last_one_at_or_before_the_stamp():
    """الجوابُ محدَّدٌ لا مقارَب: الكتلةُ التي تليه أحدثُ من الوقت، وقد قُرئت فعلاً."""
    rpc = FakeRPC(supply=1, balances={}, genesis=1_000_000_000, head=50_000)
    target = 1_000_000_000 + 37_411
    assert audit.boundary_block(rpc, target, 0, 50_000) == 37_411
    assert rpc.block_timestamp(37_411) <= target < rpc.block_timestamp(37_412)


def test_the_search_costs_four_calls_across_forty_million_blocks():
    """الاستقراءُ هو ما يجعل التدقيقَ ممكناً — والحدُّ هنا ضيّقٌ كي يُمسك تراجعَه.

    تنصيفٌ محضٌ على هذا المدى خمسٌ وعشرون نداءً، والاستقراءُ أربعة. ولو ضاع طابعُ
    أحدِ الطرفين مستقبلاً (وهو ما كان يحدث للكتلة صفر) لهبط الأداءُ إلى التنصيف
    بلا أن يفشل اختبارٌ واحد — فحدٌّ فضفاضٌ هنا يعني تراجعاً صامتاً.
    """
    rpc = FakeRPC(supply=1, balances={}, head=40_000_000)
    assert audit.boundary_block(rpc, rpc.genesis + 31_415_926, 0, 40_000_000)         == 31_415_926
    assert rpc.calls <= 6, rpc.calls


def test_anchors_narrow_the_bracket_without_being_trusted(db):
    """المرساةُ تُقصّر البحثَ، ولا تُصدَّق: طرفا القوس يُقرآن من السلسلة قبل الثقة.

    ولهذا تُختبَر مرساةٌ **كاذبة**: لو صدّقها الباحثُ لأجاب فوقها بلا مراجعة،
    والصحيحُ أن يقرأ طابعَها فيراه بعد الوقت المطلوب فيهبط دونه.
    """
    target = 1_000_000_000 + 5_000
    for block, stamp in ((2_000, 1_000_002_000), (8_000, 1_000_008_000)):
        db.add_block_anchor(NET, block, stamp, STAMP)
    low, high = audit.bracket_from_anchors(db, NET, target, 40_000)
    assert (low, high) == (2_000, 8_000)
    rpc = FakeRPC(supply=1, balances={}, head=40_000)
    assert audit.boundary_block(rpc, target, low, high) == 5_000
    # ومرساةٌ كاذبة (طابعُها يسبق كتلتَها) لا تُنتِج جواباً كاذباً:
    assert audit.boundary_block(rpc, target, 9_999, 40_000) == 5_000


def test_a_missing_archive_is_reported_not_swallowed_as_zero(db):
    """ارتدادُ `eth_call` أو غيابُ الأرشيف ⇒ «تعذّر». صفرٌ صامتٌ يقرأ «مطابقاً»."""
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)
    rpc = FakeRPC(supply=500, balances=balances, fail_at="supply")
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "تعذّر"
    assert "أرشيفٌ غائب" in record["note"]
    assert record["holders_checked"] == 0


def test_the_key_never_reaches_an_error_message():
    """رسالةُ الخطأ تحمل الرابطَ، والرابطُ يحمل المفتاح — فالكتمُ عند التكوين."""
    secret = "alch_notARealKeyJustForTheTest"
    rpc = audit.ArchiveRPC(f"https://x.invalid/v2/{secret}", secret)
    try:
        with pytest.raises(audit.ArchiveError) as caught:
            rpc.block_number()
        assert secret not in str(caught.value)
        assert secret not in rpc.hide(f"boom https://x.invalid/v2/{secret} boom")
        assert "«مفتاح»" in rpc.hide(secret)
    finally:
        rpc.close()


def test_every_declared_route_has_a_key_field_triplet():
    """مسارٌ بلا حقولِ مفتاحٍ يفشل عند التشغيل لا عند الاختبار — فيُفحَص هنا."""
    for network, routes in audit.ARCHIVE_ROUTES.items():
        assert routes, network
        for name, pattern in routes:
            assert name in audit.PROVIDER_FIELDS, (network, name)
            assert "{key}" in pattern, (network, name)
            assert len(audit.PROVIDER_FIELDS[name]) == 3


def test_sampling_always_includes_the_last_snapshot(db):
    """أقصى انحرافٍ في دفترٍ تراكميّ يقع في آخرِ لقطة، فلا تُترَك للحظّ."""
    stamps = [f"2026-08-15T18:{minute:02d}:00+00:00" for minute in range(0, 40, 5)]
    for stamp in stamps:
        _seed(db, balances={address: 100 for address in WHALES}, stamp=stamp)
    picked = audit.sample_rows(db, NET, tokens=1, per_token=3)
    assert len(picked) == 3
    assert picked[-1]["recorded_at"] == stamps[-1]
    assert picked[0]["recorded_at"] == stamps[0]
    # وتشغيلان على نفس القاعدة يعطيان نفس اللقطات — أداةٌ متقلّبةٌ لا يُبنى عليها.
    again = audit.sample_rows(db, NET, tokens=1, per_token=3)
    assert [row["recorded_at"] for row in again] == [
        row["recorded_at"] for row in picked
    ]


def test_percentages_use_the_ledgers_own_denominator():
    """المقامُ `supply_base` لا `totalSupply()`: فرقُ التعريفِ يُقرأ فرقَ بيانات."""
    out = audit._percentages([50, 30, 20], 100)
    assert out["top1_pct"] == pytest.approx(50.0)
    assert out["top5_pct"] == pytest.approx(100.0)
    assert audit._percentages([1], 0)["top1_pct"] is None


def test_a_row_that_predates_the_chain_is_reported_not_audited(db):
    """وقتٌ قبل أوّلِ كتلةٍ ⇒ لا كتلةَ تُسأل، ويُقال ذلك بلا مقارنةٍ وهميّة."""
    row = _seed(db, balances={address: 100 for address in WHALES})
    rpc = FakeRPC(supply=500, balances={}, genesis=9_000_000_000, head=10)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "تعذّر"
    assert record["block"] == 0
    assert "لا كتلةَ قبل" in record["note"]


def test_json_report_survives_a_round_trip(db):
    """التقريرُ يُقرأ آليّاً بعد حين، فأرصدةُ الحوتِ نصوصٌ لا أعداداً عائمة."""
    balances = {address: 10 ** 30 for address in WHALES}
    row = _seed(db, balances=balances)
    chain = {**balances, WHALES[0]: 10 ** 29}
    rpc = FakeRPC(supply=sum(chain.values()), balances=chain)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    revived = json.loads(json.dumps(record, ensure_ascii=False))
    assert int(revived["worst_holder"]["chain"]) == 10 ** 29
