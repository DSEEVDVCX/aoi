"""اختبارات جمع الداتا المضافة: التدفّق · سدّ الفجوة · التجّار · دمج المصادر.

كلّها بلا شبكة (عملاء مزيّفون) وبلا مساس بالقاعدة الحيّة (tmp_path).
"""
import json
import os
from datetime import datetime

import pytest

import config
import extract
import features
import recorder
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-10T12:00:00+00:00"
# مشتقّ لا مكتوب: رقم ثابت يخالف NOW بصمت يجعل «الصفّ المستقبليّ» ماضياً
# فيمرّ اختبار قانون النقطة الزمنية بلا أن يختبر شيئاً.
NOW_TS = int(datetime.fromisoformat(NOW).timestamp())


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token, *, network="56"):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)


def _details_envelope(**over):
    """ردّ tokenDetails بالمفاتيح **المقيسة حيّاً** (25 مفتاح تدفّق، حضور 100%)."""
    ro = {
        "top10HoldersPercent": 42.0, "holders": 900,
        "buyCount5m": 57, "buyCount1": 300, "buyCount4": 900, "buyCount24": 4000,
        "sellCount5m": 20, "sellCount1": 150, "sellCount4": 600, "sellCount24": 3000,
        # القيم تصل **نصوصاً** من المنبع — مقيس.
        "buyVolume5m": "458", "buyVolume1": "9000", "buyVolume4": "40000",
        "buyVolume24": "117918",
        "sellVolume5m": "229", "sellVolume1": "6000", "sellVolume4": "30000",
        "sellVolume24": "90135",
        "uniqueBuys5m": 30, "uniqueBuys1": 200, "uniqueBuys4": 500,
        "uniqueBuys24": 691,
        "uniqueSells5m": 12, "uniqueSells1": 100, "uniqueSells4": 300,
        "uniqueSells24": 400,
        "isLowFees": False,
    }
    ro.update(over)
    return {"responseObject": ro}


# ---------------------------------------------------------------------------
# مستخرِج التدفّق
# ---------------------------------------------------------------------------
def test_flow_extractor_converts_string_volumes():
    """المنبع يرسل الحجم نصّاً؛ عمود REAL يحتاج رقماً."""
    row = extract.extract_token_flow(
        _details_envelope(), "tok", "56", NOW, NOW, "sig-1", 0
    )
    assert row is not None
    assert row["buy_volume_24h"] == 117918.0
    assert row["sell_volume_24h"] == 90135.0
    assert row["buy_count_5m"] == 57
    assert row["unique_buys_24h"] == 691


def test_flow_extractor_has_no_12h_tier():
    """المصدر لا يعطي 12h — فلا عمود لها أصلاً (لا عمود ميّت)."""
    row = extract.extract_token_flow(
        _details_envelope(), "tok", "56", NOW, NOW, None, 0
    )
    assert not [k for k in row if "12h" in k]


def test_flow_extractor_keeps_false_distinct_from_missing():
    """False ≠ غائب: الأولى قياس والثانية لا (FR-007)."""
    has = extract.extract_token_flow(
        _details_envelope(isLowFees=False), "t", "56", NOW, NOW, None, 0
    )
    assert has["is_low_fees"] == 0

    env = _details_envelope()
    del env["responseObject"]["isLowFees"]
    absent = extract.extract_token_flow(env, "t", "56", NOW, NOW, None, 0)
    assert absent["is_low_fees"] is None


def test_flow_extractor_rejects_response_without_flow():
    """ردّ حيازة بلا تدفّق لا يصنع صفّاً فارغاً."""
    assert extract.extract_token_flow(
        {"responseObject": {"top10HoldersPercent": 42.0, "holders": 900}},
        "t", "56", NOW, NOW, None, 0,
    ) is None
    assert extract.extract_token_flow(None, "t", "56", NOW, NOW, None, 0) is None
    assert extract.extract_token_flow(
        {"responseObject": []}, "t", "56", NOW, NOW, None, 0
    ) is None


def test_flow_extractor_survives_partial_tiers():
    """طبقة واحدة حاضرة تكفي؛ الباقي يبقى NULL لا صفراً."""
    row = extract.extract_token_flow(
        {"responseObject": {"buyCount5m": 3}}, "t", "56", NOW, NOW, None, 0
    )
    assert row is not None
    assert row["buy_count_5m"] == 3
    assert row["buy_count_24h"] is None
    assert row["sell_volume_5m"] is None


# ---------------------------------------------------------------------------
# دورة الحائزين تكتب التدفّق من **نفس** الردّ
# ---------------------------------------------------------------------------
class _DetailsClient:
    def __init__(self, replies=None, fail=None, top_fail=None):
        self.calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._top_fail = top_fail or set()

    async def _post(self, path, body):
        self.calls.append(("POST", path, body))
        addr = body.get("tokenId", "").split(":")[0]
        if addr in self._fail:
            raise RuntimeError("upstream boom")
        return self._replies.get(addr, _details_envelope())

    async def _get(self, path, params):
        self.calls.append(("GET", path, params))
        tokens = json.loads(params["tokens"])
        addr = tokens[0]["address"] if tokens else ""
        if addr in self._top_fail:
            raise RuntimeError("upstream boom")
        return {"responseObject": [{"totalHolders": 42, "topHolders": []}]}


async def _noop(_seconds):
    pass


async def test_holders_cycle_writes_flow_without_extra_call(db):
    """التدفّق يأتي من ردّ tokenDetails نفسه — **صفر نداء إضافيّ**."""
    _watch(db, "tok")
    client = _DetailsClient()

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["flow_rows"] == 1
    assert stats["holders_details"] == 1
    # نداءان لا ثلاثة: tokenDetails و hodlers/top فقط.
    assert len(client.calls) == 2

    row = db._conn.execute("SELECT * FROM token_flow").fetchone()
    assert row["buy_volume_24h"] == 117918.0
    assert row["buy_count_5m"] == 57
    assert row["is_low_fees"] == 0
    assert decode_raw(row["raw_json"])["responseObject"]["buyCount5m"] == 57


async def test_flow_written_even_when_holding_data_absent(db):
    """مستخرِج الحيازة يعيد None بلا نسب — ويجب ألّا يبتلع التدفّق معه.

    هذا هو سبب فصل الجدولين: لو عُلّق التدفّق على نجاح الحيازة لضاع، وهو
    حاضر 100% بينما نسب الحيازة ليست كذلك.
    """
    _watch(db, "noholders")
    env = _details_envelope()
    del env["responseObject"]["top10HoldersPercent"]
    del env["responseObject"]["holders"]
    client = _DetailsClient(replies={"noholders": env})

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["holders_details"] == 0   # لا صفّ حيازة
    assert stats["flow_rows"] == 1         # لكنّ التدفّق نجا
    assert db._conn.execute("SELECT COUNT(*) FROM token_flow").fetchone()[0] == 1


async def test_fetch_failure_writes_no_flow_row(db):
    """فشل الجلب لا يترك `raw` قديماً يُكتب منه صفّ تدفّق كاذب."""
    _watch(db, "dead")
    client = _DetailsClient(fail={"dead"})

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["flow_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM token_flow").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# سدّ فجوة القياس (filterTokens)
# ---------------------------------------------------------------------------
def _filter_item(addr, *, net="56", price=1.0, protocol=None):
    item = {
        "token": {"address": addr, "networkId": net, "symbol": "AAA"},
        "priceUSD": price, "liquidity": 5000.0, "volume24": 1000.0,
    }
    if protocol is not None:
        item["pair"] = {"protocol": protocol}
    return item


class _FilterClient:
    def __init__(self, items=None, fail=False):
        self.calls = []
        self._items = items
        self._fail = fail

    async def _post(self, path, body):
        self.calls.append(body)
        if self._fail:
            raise RuntimeError("upstream boom")
        if self._items is not None:
            return {"responseObject": self._items}
        addrs = [s.split(":")[0] for s in body]
        return {"responseObject": [_filter_item(a) for a in addrs]}


async def test_filter_cycle_only_requests_the_gap(db):
    """ما التقطته trending لا يُطلب ثانيةً — سدّ فجوة لا مسح شامل."""
    _watch(db, "seen")
    _watch(db, "missed")
    client = _FilterClient()
    watched = {("seen", "56"), ("missed", "56")}
    captured = {("seen", "56")}

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, watched, captured, sleep=_noop
    )

    assert stats["filter_requested"] == 1
    assert client.calls == [["missed:56"]]
    assert stats["filter_ticks"] == 1
    rows = db._conn.execute(
        "SELECT token_address, source FROM market_ticks"
    ).fetchall()
    assert [(r["token_address"], r["source"]) for r in rows] == [("missed", "filter")]


async def test_filter_cycle_no_gap_makes_no_call(db):
    _watch(db, "seen")
    client = _FilterClient()

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("seen", "56")}, {("seen", "56")}, sleep=_noop
    )

    assert client.calls == []
    assert stats == {"filter_requested": 0, "filter_ticks": 0, "filter_errors": 0}


async def test_filter_cycle_maps_by_address_not_position(db):
    """المنبع **يحذف الميّت بصمت** فينزلق الترتيب — الربط بالعنوان وحده."""
    for t in ("a", "b", "c"):
        _watch(db, t)
    watched = {("a", "56"), ("b", "56"), ("c", "56")}
    # 'b' مشطوبة: ترجع 'c' ثم 'a' بترتيب مقلوب أيضاً.
    client = _FilterClient(items=[_filter_item("c", price=3.0),
                                  _filter_item("a", price=1.0)])

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, watched, set(), sleep=_noop
    )

    assert stats["filter_requested"] == 3
    assert stats["filter_ticks"] == 2       # الميّتة لا تكسر الدفعة
    prices = {
        r["token_address"]: r["price_usd"]
        for r in db._conn.execute("SELECT token_address, price_usd FROM market_ticks")
    }
    assert prices == {"a": 1.0, "c": 3.0}   # لو رُبط بالفهرس لانعكست القيم


async def test_filter_cycle_fills_dex_protocol(db):
    """`dex_protocol` غائب من trending (0 من 3,000) — هذا مصدره الوحيد."""
    _watch(db, "tok")
    db.upsert_static({
        **{k: None for k in (
            "symbol", "name", "created_at", "first_seen_at", "decimals",
            "chain", "is_verified", "dex_protocol")},
        "token_address": "tok", "network_id": "56", "recorded_at": NOW,
        "raw_json": "{}",
    })
    client = _FilterClient(items=[_filter_item("tok", protocol="PumpAmm")])

    await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("tok", "56")}, set(), sleep=_noop
    )

    proto = db._conn.execute(
        "SELECT dex_protocol FROM token_static WHERE token_address='tok'"
    ).fetchone()["dex_protocol"]
    assert proto == "PumpAmm"


async def test_filter_cycle_error_does_not_raise(db):
    _watch(db, "tok")
    client = _FilterClient(fail=True)

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("tok", "56")}, set(), sleep=_noop
    )

    assert stats["filter_errors"] == 1
    assert stats["filter_ticks"] == 0
    assert "last_error_filter" in (db.get_meta("last_error_filter") or "") or \
        db.get_meta("last_error_filter") is not None


# ---------------------------------------------------------------------------
# دورة التجّار
# ---------------------------------------------------------------------------
def _trader_envelope(tid="u1", **over):
    """الحقول **المقيسة حيّاً** على /v2/users/{id} (26 مفتاحاً)."""
    ro = {
        "id": tid, "userHandle": "Thepennyflippe", "displayName": "Max",
        "followers": 2143, "following": 68, "swapCount": 5450,
        "numTrades": 518, "totalVolume": 12990492.90762,
        "averageHoldTimeSeconds": 38304, "isRestricted": False,
        "private": False, "address": "GW8Pf", "evmAddress": "0x47ae",
        "twitter": "https://x.com/Thepennyflippe",
        "createdAt": "2025-09-07T15:30:29.242Z",
    }
    ro.update(over)
    return {"responseObject": ro}


def test_trader_extractor_maps_measured_fields():
    row = extract.extract_trader(_trader_envelope(), "u1", NOW)
    assert row["trader_id"] == "u1"
    assert row["followers_count"] == 2143
    assert row["swap_count"] == 5450
    assert row["num_trades"] == 518
    assert row["total_volume_usd"] == pytest.approx(12990492.90762)
    assert row["avg_hold_seconds"] == 38304
    assert row["is_restricted"] == 0
    assert row["handle"] == "Thepennyflippe"


def test_trader_extractor_rejects_bad_envelope():
    assert extract.extract_trader({"success": True}, "u1", NOW) is None
    assert extract.extract_trader(None, "u1", NOW) is None


def _signal(db, sid, buyer, token="tok"):
    db._conn.execute(
        "INSERT INTO signal_events (id, token_address, network_id, ts, "
        "recorded_at, signal_type, buyer_id, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (sid, token, "56", NOW, NOW, "large_buy", buyer, b"{}"),
    )
    db._conn.commit()


class _TraderClient:
    def __init__(self, replies=None, fail=None, missing=None):
        self.calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._missing = missing or set()

    async def _get(self, path, params=None):
        tid = path.rstrip("/").split("/")[-1]
        self.calls.append(tid)
        if tid in self._fail:
            raise RuntimeError("upstream boom")
        if tid in self._missing:
            return None  # 404
        return self._replies.get(tid, _trader_envelope(tid))


async def test_traders_cycle_fetches_only_repeat_buyers(db, monkeypatch):
    """من يظهر مرّة واحدة لا سلوك له نتعلّمه ⇒ لا يُجلب."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    for i in range(3):
        _signal(db, f"s{i}", "repeat")
    _signal(db, "once", "oneshot")
    client = _TraderClient()

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert client.calls == ["repeat"]
    assert stats["traders_rows"] == 1
    row = db._conn.execute("SELECT * FROM traders").fetchone()
    assert row["trader_id"] == "repeat"
    assert row["followers_count"] == 2143


async def test_traders_cycle_upserts_changing_profile(db, monkeypatch):
    """الملفّ **يتغيّر** (متابعون، مدّة حمل) ⇒ REPLACE لا IGNORE."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_REFRESH_SECONDS", 0)
    for i in range(3):
        _signal(db, f"s{i}", "u1")

    await recorder.run_traders_cycle(
        _TraderClient(replies={"u1": _trader_envelope("u1", followers=100)}),
        db, NOW, sleep=_noop,
    )
    later = "2026-08-11T12:00:00+00:00"
    await recorder.run_traders_cycle(
        _TraderClient(replies={"u1": _trader_envelope("u1", followers=999)}),
        db, later, sleep=_noop,
    )

    rows = db._conn.execute("SELECT * FROM traders").fetchall()
    assert len(rows) == 1                       # صفّ واحد لا اثنان
    assert rows[0]["followers_count"] == 999    # والقيمة الأحدث فازت
    assert rows[0]["recorded_at"] == later


async def test_traders_cycle_marks_404_empty_not_error(db, monkeypatch):
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    for i in range(3):
        _signal(db, f"s{i}", "gone")
    client = _TraderClient(missing={"gone"})

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert stats["traders_errors"] == 0
    assert stats["traders_rows"] == 0
    state = db._conn.execute(
        "SELECT last_status FROM traders_fetch_state WHERE trader_id='gone'"
    ).fetchone()
    assert state["last_status"] == "empty"


async def test_traders_cycle_failure_isolated(db, monkeypatch):
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    for i in range(3):
        _signal(db, f"a{i}", "bad")
    for i in range(3):
        _signal(db, f"b{i}", "good", token="tok2")
    client = _TraderClient(fail={"bad"})

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert stats["traders_errors"] == 1
    assert stats["traders_rows"] == 1           # الثاني نجا
    assert db._conn.execute(
        "SELECT trader_id FROM traders"
    ).fetchone()["trader_id"] == "good"


async def test_traders_not_refetched_before_refresh(db, monkeypatch):
    """الملفّ يتغيّر بالأيّام — لا نهدر نداءً كل دقيقة."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    for i in range(3):
        _signal(db, f"s{i}", "u1")

    await recorder.run_traders_cycle(_TraderClient(), db, NOW, sleep=_noop)
    client2 = _TraderClient()
    await recorder.run_traders_cycle(client2, db, NOW, sleep=_noop)

    assert client2.calls == []


# ---------------------------------------------------------------------------
# فيتشرات التدفّق (قانون النقطة الزمنية)
# ---------------------------------------------------------------------------
def _insert_flow(db, ts, **over):
    row = {c: None for c in __import__("db")._FLOW_COLUMNS}
    row.update({
        "token_address": "tok", "network_id": "56", "recorded_at": ts,
        "watch_first_seen_at": NOW, "entry_signal_id": None, "is_control": 0,
        "buy_volume_5m": 300.0, "sell_volume_5m": 100.0,
        "buy_count_5m": 10, "sell_count_5m": 5,
        "unique_buys_5m": 4, "unique_sells_5m": 3,
        "raw_json": "{}",
    })
    row.update(over)
    db.insert_flow(row)


def test_flow_features_derive_ratios(db):
    _insert_flow(db, "2026-08-10T11:55:00+00:00")
    f = features.flow_features(db, "tok", "56", NOW_TS)

    assert f["flow_net_volume_5m"] == 200.0
    assert f["flow_buy_sell_volume_ratio_5m"] == 3.0
    assert f["flow_buy_sell_count_ratio_5m"] == 2.0
    assert f["flow_unique_ratio_5m"] == 0.4      # 4 فريدين ÷ 10 صفقات
    assert f["flow_trade_size_5m"] == 30.0       # 300$ ÷ 10 صفقات
    assert f["flow_age_min"] == pytest.approx(5.0)


def test_flow_features_absent_stays_null(db):
    """بلا صفّ: كل شيء None — «لم نقس» لا «صفر» (FR-007)."""
    f = features.flow_features(db, "tok", "56", NOW_TS)
    assert set(f.values()) == {None}
    assert "flow_net_volume_5m" in f


def test_flow_features_missing_side_is_not_zero(db):
    """بيع مجهول ليس بيعاً معدوماً — الصافي يبقى None لا يساوي الشراء."""
    _insert_flow(db, "2026-08-10T11:55:00+00:00", sell_volume_5m=None)
    f = features.flow_features(db, "tok", "56", NOW_TS)

    assert f["flow_buy_volume_5m"] == 300.0
    assert f["flow_net_volume_5m"] is None
    assert f["flow_buy_sell_volume_ratio_5m"] is None


def test_flow_features_ignore_future_rows(db):
    """قانون النقطة الزمنية: ما بعد t0 لا يُرى مهما كان أطزج."""
    _insert_flow(db, "2026-08-10T11:55:00+00:00", buy_volume_5m=300.0)
    before = features.flow_features(db, "tok", "56", NOW_TS)
    _insert_flow(db, "2026-08-10T12:30:00+00:00", buy_volume_5m=999.0)
    after = features.flow_features(db, "tok", "56", NOW_TS)

    assert before == after
    assert after["flow_buy_volume_5m"] == 300.0


def test_flow_features_zero_denominator_is_null_not_inf(db):
    """قسمة على صفر ⇒ None لا inf — والصفر في البسط يبقى قياساً."""
    _insert_flow(db, "2026-08-10T11:55:00+00:00",
                 buy_volume_5m=0.0, sell_volume_5m=0.0, buy_count_5m=0)
    f = features.flow_features(db, "tok", "56", NOW_TS)

    assert f["flow_buy_volume_5m"] == 0.0         # صفر مقيس ≠ غائب
    assert f["flow_net_volume_5m"] == 0.0
    assert f["flow_buy_sell_volume_ratio_5m"] is None
    assert f["flow_trade_size_5m"] is None


def test_flow_columns_registered_in_feature_columns():
    """عمود يُحسب ولا يُسجَّل = عمل ضائع بصمت."""
    empty = RecorderDB(":memory:", SCHEMA)
    try:
        f_keys = set(features.flow_features(empty, "x", "1", NOW_TS))
        h_keys = set(features.holders_features(empty, "x", "1", NOW_TS))
    finally:
        empty.close()
    assert f_keys <= set(features.FEATURE_COLUMNS)
    assert h_keys <= set(features.FEATURE_COLUMNS)
    assert "tick_rich_age_min" in features.FEATURE_COLUMNS


# ---------------------------------------------------------------------------
# تغيّر حائزي السلسلة عبر ساعة — من لقطتين لنا، فالمصدر لا يعطي فرقاً
# ---------------------------------------------------------------------------
def _holders(db, at, count, *, source="token_details", top10=42.0):
    db.insert_holders({
        "token_address": "tok", "network_id": "56", "recorded_at": at,
        "watch_first_seen_at": NOW, "entry_signal_id": "sig", "is_control": 0,
        "source": source, "top10_pct": top10, "holder_count": count,
        "raw_json": "{}",
    })


def test_holders_delta_measures_chain_not_platform(db):
    """الإيقاع 25د ⇒ السابقة بساعة+ تبعد 75د، والمدى عمود صريح لا مفترض."""
    _holders(db, "2026-08-10T10:45:00+00:00", 900)
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)      # +100 عبر 75د
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holder_count"] == 1000
    assert f["chain_holders_delta_1h"] == 100
    assert f["chain_holders_growth_1h"] == pytest.approx(100 / 900)
    assert f["chain_holders_span_min"] == pytest.approx(75.0)


def test_holders_delta_absent_prior_stays_null(db):
    """لقطة واحدة: الفرق None لا صفر — «لم نقس» ليس «لم يتغيّر» (FR-007)."""
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holder_count"] == 1000
    assert f["chain_holders_delta_1h"] is None
    assert f["chain_holders_growth_1h"] is None
    assert f["chain_holders_span_min"] is None


def test_holders_delta_rejects_stale_prior(db):
    """فوق 100د اللقطة من حقبة أخرى: الذيل المقيس يمتدّ إلى 4,834د."""
    _holders(db, "2026-08-10T06:00:00+00:00", 500)        # قبل 360د
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holders_span_min"] is None
    assert f["chain_holders_delta_1h"] is None


def test_holders_delta_zero_change_is_measured(db):
    """صفر مقيس ≠ غائب: 15.1% من الأزواج لا يتغيّر فيها العدد فعلاً."""
    _holders(db, "2026-08-10T10:45:00+00:00", 900)
    _holders(db, "2026-08-10T12:00:00+00:00", 900)
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holders_delta_1h"] == 0
    assert f["chain_holders_growth_1h"] == 0.0


def test_holders_delta_ignores_future_and_platform_rows(db):
    """قانون النقطة الزمنية + المصدر: hodlers_top لا يلوّث عدّ السلسلة."""
    _holders(db, "2026-08-10T10:45:00+00:00", 900)
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)
    before = features.holders_features(db, "tok", "56", NOW_TS)
    _holders(db, "2026-08-10T12:30:00+00:00", 9999)                    # بعد t0
    _holders(db, "2026-08-10T11:50:00+00:00", 7, source="hodlers_top")  # منصّة
    after = features.holders_features(db, "tok", "56", NOW_TS)

    assert before["chain_holders_delta_1h"] == after["chain_holders_delta_1h"] == 100
    assert after["chain_holder_count"] == 1000
