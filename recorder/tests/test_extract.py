"""اختبارات التحويلات الخالصة على أشكال خام حقيقية ملتقطة (بلا شبكة).

الأشكال أدناه مطابقة لِما التُقط حيّاً عبر الـ probe (feed multi_user_buy،
trending item). نتحقّق من: استخراج الحقول الذهبية، مطابقة المتصدّرين،
سلوك FR-007 (غياب → None لا فبركة)، والحفاظ على False.
"""
import json

import config
import extract

# --- أشكال خام مؤكّدة ---
FEED_EVENT = {
    "id": "evt_123",
    "userId": "u_1",
    "tokenAddress": "So1111tokenAddr",
    "networkId": 1399811149,
    "createdAt": "2026-07-25T10:00:00Z",
    "type": "multi_user_buy",
    "body": {
        "fdv": 1234567.0,
        "price": 0.00042,
        "ticker": "PEPE2",
        "minutes": 15,
        "marketCap": 987654.0,
        "numTrades": 42,
        "topTraders": [
            {"id": "trader_A", "userHandle": "whale1", "displayName": "Whale One"},
            {"id": "trader_B", "userHandle": "whale2", "displayName": "Whale Two"},
            {"id": "trader_Z", "userHandle": "nobody", "displayName": "No Body"},
        ],
        "totalVolume": 55000.0,
        "areTopTraders": True,
        "uniqueTraders": 30,
        "priceChangePercent": 12.5,
    },
}

TRENDING_ITEM = {
    "change5m": 1.2, "change1": 3.4, "change4": -5.6, "change12": 10.0, "change24": 25.0,
    "liquidity": 45000.0, "marketCap": 987654.0, "priceUSD": 0.00042,
    "volume5m": 100.0, "volume1": 2000.0, "volume4": 8000.0, "volume12": 20000.0, "volume24": 55000.0,
    "txnCount1": 10, "txnCount4": 40, "txnCount12": 120, "txnCount24": 300,
    "buyCount1": 7, "buyCount4": 25, "buyCount12": 80, "buyCount24": 200,
    "sellCount1": 3, "sellCount4": 15, "sellCount12": 40, "sellCount24": 100,
    "uniqueBuys1": 6, "uniqueBuys4": 20, "uniqueBuys12": 60, "uniqueBuys24": 150,
    "uniqueSells1": 2, "uniqueSells4": 10, "uniqueSells12": 30, "uniqueSells24": 70,
    "holders": 512,
    "token": {
        "address": "So1111tokenAddr", "decimals": 6, "networkId": 1399811149,
        "createdAt": "2026-07-01T00:00:00Z", "name": "Pepe Two", "symbol": "PEPE2",
        "freezable": False, "mintable": True, "isScam": False,
        "creatorAddress": "Creator111",
        "info": {"circulatingSupply": 1000000.0, "totalSupply": 2000000.0},
        "socialLinks": {"twitter": "https://x.com/pepe2", "website": "https://pepe2.fun",
                        "telegram": None, "discord": None},
        "launchpad": {"launchpadName": "pump.fun", "graduationPercent": 100.0, "migrated": True},
    },
}


def test_unwrap_feed_reads_feed_key():
    env = {"responseObject": {"feed": [FEED_EVENT]}}
    out = extract.unwrap_feed(env)
    assert len(out) == 1 and out[0]["id"] == "evt_123"


def test_unwrap_feed_missing_returns_empty():
    assert extract.unwrap_feed(None) == []
    assert extract.unwrap_feed({}) == []
    assert extract.unwrap_feed({"responseObject": {}}) == []


def test_extract_signal_event_core_fields():
    row = extract.extract_signal_event(FEED_EVENT, "2026-07-25T10:00:01Z")
    assert row is not None
    assert row["id"] == "evt_123"
    assert row["token_address"] == "So1111tokenAddr"
    assert row["signal_type"] == "multi_user_buy"
    assert row["ticker"] == "PEPE2"
    assert row["price_usd"] == 0.00042
    assert row["num_trades"] == 42
    assert row["are_top_traders"] == 1
    assert json.loads(row["top_trader_ids_json"]) == ["trader_A", "trader_B", "trader_Z"]
    # بلا rank_lookup تبقى المطابقة None (لا فبركة)
    assert row["top_trader_match_count"] is None
    assert row["buyers_best_rank"] is None


def test_extract_signal_event_top_trader_matching():
    """إشارة "أكثر من متصدّر اشترى": مطابقة id→رتبة تُحسب بدقّة."""
    rank_lookup = {"trader_A": 3, "trader_B": 17}  # trader_Z ليس متصدّراً
    row = extract.extract_signal_event(FEED_EVENT, "2026-07-25T10:00:01Z", rank_lookup)
    assert row["top_trader_match_count"] == 2       # اثنان من المتصدّرين اشتروا
    assert row["buyers_best_rank"] == 3             # أفضل (أصغر) رتبة


# --- صدارات المدد (v7): 50 لكل مدّة، والاتّحاد يضاعف التغطية ---
def test_period_ranks_are_separate_not_merged():
    """رتبة 24h لا تُدمج مع رتبة totalPnL — قياسان مختلفان في عمودين."""
    row = extract.extract_signal_event(
        FEED_EVENT, "t",
        rank_lookup={"trader_A": 40},
        rank_lookups={
            "24h": {"trader_A": 2, "trader_Z": 9},
            "7d": {"trader_B": 11},
            "30d": {},                      # لم تُحمّل → لا قياس
        },
    )
    assert (row["top_trader_match_count"], row["buyers_best_rank"]) == (1, 40)
    assert (row["top_trader_match_count_24h"], row["buyers_best_rank_24h"]) == (2, 2)
    assert (row["top_trader_match_count_7d"], row["buyers_best_rank_7d"]) == (1, 11)
    # مدّة بلا خريطة: غياب لا صفر (FR-007)
    assert row["top_trader_match_count_30d"] is None
    assert row["buyers_best_rank_30d"] is None
    # عرض الحضور: all + 24h + 7d = ثلاث صدارات فيها مشترٍ
    assert row["top_trader_periods_matched"] == 3


def test_period_ranks_absent_lookups_stay_null():
    """بلا rank_lookups كلّها None — لا فبركة صفر لمدّة لم تُقس."""
    row = extract.extract_signal_event(FEED_EVENT, "t")
    for c in (
        "top_trader_match_count_24h", "buyers_best_rank_24h",
        "top_trader_match_count_7d", "buyers_best_rank_7d",
        "top_trader_match_count_30d", "buyers_best_rank_30d",
        "top_trader_periods_matched",
    ):
        assert row[c] is None, c


def test_periods_matched_counts_zero_when_measured_and_empty():
    """مقيس ولم يطابق أحد ⇒ 0 (لا None): «قِسنا فلم نجد» معلومة."""
    row = extract.extract_signal_event(
        FEED_EVENT, "t",
        rank_lookup={"someone_else": 1},
        rank_lookups={"24h": {"other": 5}, "7d": {"other": 5}, "30d": {"other": 5}},
    )
    assert row["top_trader_periods_matched"] == 0
    assert row["top_trader_match_count_24h"] == 0
    assert row["buyers_best_rank_24h"] is None   # لا مطابقة ⇒ لا رتبة


def test_period_matching_includes_single_buyer_id():
    """large_buy بمشترٍ مفرد يُطابَق في المدد كما في الصدارة الأساسيّة."""
    ev = {
        "id": "evt_lb", "tokenAddress": "So1", "type": "large_buy",
        "createdAt": "2026-07-25T10:00:00Z",
        "body": {"userId": "trader_solo", "ticker": "X"},
    }
    row = extract.extract_signal_event(
        ev, "t", rank_lookup={}, rank_lookups={"7d": {"trader_solo": 4}}
    )
    assert row["buyers_best_rank_7d"] == 4
    assert row["top_trader_match_count_7d"] == 1
    assert row["top_trader_periods_matched"] == 1   # all قِيست بلا مطابقة


def test_match_ranks_by_period_pure():
    out = extract.match_ranks_by_period(
        ["a", "b", "c"], {"24h": {"b": 7, "c": 3}, "7d": None}
    )
    assert out["24h"] == (2, 3)
    assert out["7d"] == (None, None)
    assert out["30d"] == (None, None)


def test_extract_signal_event_missing_id_dropped():
    bad = dict(FEED_EVENT)
    del bad["id"]
    assert extract.extract_signal_event(bad, "t") is None


def test_extract_signal_event_missing_token_dropped():
    bad = {k: v for k, v in FEED_EVENT.items() if k != "tokenAddress"}
    assert extract.extract_signal_event(bad, "t") is None


def test_unwrap_token_list_variants():
    assert len(extract.unwrap_token_list({"responseObject": {"tokens": [TRENDING_ITEM]}})) == 1
    assert len(extract.unwrap_token_list({"responseObject": [TRENDING_ITEM]})) == 1
    assert extract.unwrap_token_list({"responseObject": {}}) == []


def test_extract_market_tick_golden_fields():
    tick = extract.extract_market_tick(TRENDING_ITEM, "2026-07-25T10:00:00Z", "trending")
    assert tick is not None
    assert tick["token_address"] == "So1111tokenAddr"
    assert tick["network_id"] == "1399811149"
    assert tick["price_usd"] == 0.00042
    assert tick["holders"] == 512
    assert tick["change_24h"] == 25.0
    assert tick["volume_24h"] == 55000.0
    assert tick["buy_count_24h"] == 200
    assert tick["circulating_supply"] == 1000000.0
    assert tick["source"] == "trending"


def test_extract_market_tick_missing_optionals_are_none():
    """FR-007: عنصر بحقول ناقصة → None في مكانها، لا صفر."""
    minimal = {"token": {"address": "Addr2", "networkId": 1}}
    tick = extract.extract_market_tick(minimal, "t", "verified")
    assert tick["price_usd"] is None
    assert tick["holders"] is None
    assert tick["volume_24h"] is None
    assert tick["circulating_supply"] is None


def test_extract_token_static_flags_and_socials():
    st = extract.extract_token_static(TRENDING_ITEM, "2026-07-25T10:00:00Z")
    assert st["symbol"] == "PEPE2"
    assert st["mintable"] == 1
    assert st["freezable"] == 0          # False محفوظ صراحةً، ليس None
    assert st["is_scam"] == 0
    assert st["creator_address"] == "Creator111"
    assert st["launchpad_name"] == "pump.fun"
    assert st["migrated"] == 1
    assert st["twitter"] == "https://x.com/pepe2"
    assert st["telegram"] is None        # غائب فعلاً


def test_false_preserved_not_nulled():
    """freezable=False يجب أن يصبح 0 لا None — تمييز 'آمن' عن 'مجهول'."""
    assert extract._bool_to_int(False) == 0
    assert extract._bool_to_int(True) == 1
    assert extract._bool_to_int(None) is None


SOL = config.SOLANA_NETWORK_ID


def test_authority_address_means_live_authority():
    """fomo يعيد **عنوان** السلطة لا قيمة منطقية؛ وجوده ⇒ الصلاحية قائمة (1)."""
    addr = "ATESfxbwt3SRhSHc4nbk8BG6P8Nm8TAjsHfQCbgC2er4"
    assert extract._authority_to_int(addr, SOL) == 1
    assert extract._authority_to_int(addr, "8453") == 1


def test_authority_null_on_solana_is_revoked():
    """على سولانا null تعني السلطة مُلغاة فعلاً — صفر مقيس لا مجهول.
    مؤكَّد من مصدر ثانٍ: قراءة السلسلة تعطي النسبة والعناوين نفسها."""
    assert extract._authority_to_int(None, SOL) == 0


def test_authority_null_on_evm_stays_unknown():
    """على EVM القيمة null في 259/259 لقطة: غير مقيسة لا «آمنة».
    إرجاع 0 هنا يفبرك أماناً لعملة لم تُقَس (FR-007)."""
    for net in ("56", "4663", "8453", "1", ""):
        assert extract._authority_to_int(None, net) is None


def test_authority_blank_string_is_unknown():
    """نصّ فارغ ليس عنواناً ولا نفياً — يبقى مجهولاً."""
    assert extract._authority_to_int("", SOL) is None
    assert extract._authority_to_int("   ", SOL) is None


def test_evm_static_authorities_are_null_not_zero():
    """اختبار تكاملي: عملة EVM بلا سلطتين تُخزَّن NULL لا 0."""
    item = json.loads(json.dumps(TRENDING_ITEM))
    item["token"].update({"networkId": 8453, "mintable": None, "freezable": None})
    st = extract.extract_token_static(item, "2026-07-25T10:00:00Z")
    assert st["network_id"] == "8453"
    assert st["mintable"] is None
    assert st["freezable"] is None


def test_solana_static_authorities_measured():
    """اختبار تكاملي: على سولانا عنوان ⇒ 1 و null ⇒ 0 في الصفّ المخزَّن."""
    item = json.loads(json.dumps(TRENDING_ITEM))
    item["token"].update({"networkId": int(SOL),
                          "mintable": "ATESfxbwt3SRhSHc4nbk8BG6P8Nm8TAjsHfQCbgC2er4",
                          "freezable": None})
    st = extract.extract_token_static(item, "2026-07-25T10:00:00Z")
    assert st["network_id"] == SOL
    assert st["mintable"] == 1
    assert st["freezable"] == 0


def test_num_helpers_no_fabrication():
    assert extract._num(None) is None
    assert extract._num("") is None
    assert extract._num("abc") is None
    assert extract._num("3.5") == 3.5
    assert extract._int("42") == 42
    assert extract._int(None) is None


def test_build_rank_lookup_keeps_best_rank():
    traders = [
        {"id": "x", "rank": 10},
        {"id": "x", "rank": 4},   # الأفضل يفوز
        {"id": "y", "rank": 7},
        {"id": "z"},              # بلا rank → يُتخطّى
    ]
    lut = extract.build_rank_lookup(traders)
    assert lut == {"x": 4, "y": 7}


# --- شكل large_buy المؤكّد حيّاً: مشترٍ واحد، لا topTraders ---
LARGE_BUY_EVENT = {
    "id": "lb_1",
    "userId": "buyer_top",
    "tokenAddress": "0xBrodie",
    "networkId": 8453,
    "createdAt": "2026-07-25T23:00:00Z",
    "type": "large_buy",
    "body": {
        "fdv": 3572175.14,
        "price": 0.0036,
        "ticker": "BRODIE",
        "userId": "buyer_top",
        "avgCost": 0.00347,
        "numSwaps": 3,
        "isFirstBuy": False,
        "percentPnl": -7.66,
        "userHandle": "okay_",
        "displayName": "ok",
    },
}


def test_large_buy_single_buyer_fields():
    row = extract.extract_signal_event(LARGE_BUY_EVENT, "t")
    assert row["signal_type"] == "large_buy"
    assert row["buyer_id"] == "buyer_top"
    assert row["buyer_handle"] == "okay_"
    assert row["num_swaps"] == 3
    assert row["is_first_buy"] == 0          # False محفوظ
    assert row["buyer_pnl_pct"] == -7.66
    assert row["avg_cost"] == 0.00347
    # لا topTraders في large_buy → القائمة فارغة، والحقول المتعدّدة None
    assert json.loads(row["top_trader_ids_json"]) == []
    assert row["num_trades"] is None
    assert row["are_top_traders"] is None


def test_large_buy_single_buyer_matched_by_leaderboard():
    """المشتري المفرد يُطابَق بالصدارة أيضاً (لا يقتصر على topTraders)."""
    row = extract.extract_signal_event(LARGE_BUY_EVENT, "t", {"buyer_top": 12})
    assert row["top_trader_match_count"] == 1
    assert row["buyers_best_rank"] == 12


def test_multi_buy_still_has_no_single_buyer_fields():
    """multi_user_buy لا يحمل numSwaps/isFirstBuy → None (FR-007، لا فبركة)."""
    row = extract.extract_signal_event(FEED_EVENT, "t")
    assert row["num_swaps"] is None
    assert row["is_first_buy"] is None
    assert row["buyer_pnl_pct"] is None


# --- شكل large_sell المؤكّد حيّاً (2026-07-28): نفس body باتجاه معاكس ---
LARGE_SELL_EVENT = {
    "id": "ls_1",
    "userId": "seller_top",
    "tokenAddress": "0xDAHOOD",
    "networkId": 4663,
    "createdAt": "2026-07-28T10:38:09.910Z",
    "type": "large_sell",
    "body": {
        "fdv": 509830.15,
        "price": 0.000505,
        "ticker": "DAHOOD",
        "userId": "seller_top",
        "avgCost": 0.000669,
        "numSwaps": 6,
        "isFirstBuy": False,
        "percentPnl": -21.82,
        "userHandle": "3pink10sss",
        "displayName": "Rando",
        "currentSizeUsd": 2441.0,
        "inHumanAmount": 4835754.15,     # العملة المباعة (باتجاه البيع)
        "outHumanAmount": 2440.9,        # ما استلمه مقابلها
        "realizedPnlUsd": -680.5,
    },
}


def test_large_sell_extracts_like_large_buy():
    """large_sell = نفس شكل large_buy: البائع المفرد يُلتقط ويُطابَق بالصدارة."""
    row = extract.extract_signal_event(LARGE_SELL_EVENT, "t")
    assert row["signal_type"] == "large_sell"
    assert row["buyer_id"] == "seller_top"   # الحقل اسمه buyer_* لكنه «الفاعل» هنا
    assert row["buyer_handle"] == "3pink10sss"
    assert row["size_usd"] == 2441.0
    assert row["in_amount"] == 4835754.15
    assert row["realized_pnl_usd"] == -680.5


def test_large_sell_seller_matched_by_leaderboard():
    """تصريف متصدّر: البائع المفرد يُطابَق بالصدارة كالمشتري تماماً."""
    row = extract.extract_signal_event(LARGE_SELL_EVENT, "t", {"seller_top": 5})
    assert row["top_trader_match_count"] == 1
    assert row["buyers_best_rank"] == 5



# --- شموع OHLCV (getBarsNew) ---
def _bars_envelope(**over):
    ro = {
        "s": "ok",
        "t": [1000, 1300, 1600],
        "o": [1.0, 1.1, 1.2],
        "h": [1.5, 1.6, 1.7],
        "l": [0.9, 1.0, 1.1],
        "c": [1.1, 1.2, 1.3],
        "v": [100.0, 200.0, 300.0],
    }
    ro.update(over)
    return {"success": True, "responseObject": ro}


def test_extract_bars_maps_parallel_arrays_to_rows():
    rows = extract.extract_bars(_bars_envelope(), "0xtok", "56", "5", "2026-07-26T00:00:00Z")
    assert len(rows) == 3
    assert rows[0] == {
        "token_address": "0xtok", "network_id": "56", "resolution": "5", "ts": 1000,
        "o": 1.0, "h": 1.5, "l": 0.9, "c": 1.1, "v": 100.0,
        "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
        "fetched_at": "2026-07-26T00:00:00Z",
    }
    assert [r["ts"] for r in rows] == [1000, 1300, 1600]


def test_extract_bars_truncates_to_shortest_column():
    """مصفوفات متوازية غير متساوية → الحقل الغائب None، لا انهيار ولا فبركة."""
    rows = extract.extract_bars(
        _bars_envelope(v=[100.0]), "0xtok", "56", "5", "t"
    )
    assert len(rows) == 3
    assert rows[0]["v"] == 100.0
    assert rows[1]["v"] is None and rows[2]["v"] is None


def test_extract_bars_skips_candles_without_timestamp():
    rows = extract.extract_bars(
        _bars_envelope(t=[1000, None, 1600]), "0xtok", "56", "5", "t"
    )
    assert [r["ts"] for r in rows] == [1000, 1600]


def test_extract_bars_handles_no_data_and_garbage():
    assert extract.extract_bars({"responseObject": {"s": "no_data", "t": []}}, "a", "1", "5", "t") == []
    assert extract.extract_bars({"responseObject": {}}, "a", "1", "5", "t") == []
    assert extract.extract_bars({}, "a", "1", "5", "t") == []
    assert extract.extract_bars(None, "a", "1", "5", "t") == []
    assert extract.extract_bars("nope", "a", "1", "5", "t") == []


def test_bars_status_reads_s_field():
    assert extract.bars_status(_bars_envelope()) == "ok"
    assert extract.bars_status(_bars_envelope(s="no_data")) == "no_data"
    assert extract.bars_status({"responseObject": {}}) is None
    assert extract.bars_status(None) is None


# --- حقول حجم الصفقة (large_buy) ---
def test_extract_signal_captures_trade_size_fields():
    """الانحدار المقصود: بلا هذه الحقول كانت صفقة 1,000$ وأخرى 141,000$
    متطابقتين تماماً، رغم أنّ وسيط "الشراء الكبير" 3,448$ فقط."""
    ev = {
        "id": "e1", "tokenAddress": "0xtok", "networkId": 56,
        "createdAt": "2026-07-26T00:00:00Z", "type": "large_buy",
        "body": {
            "ticker": "AAA", "price": 1.5,
            "currentSizeUsd": 41825.86, "inHumanAmount": 3000,
            "inTokenAddress": "USDC", "outHumanAmount": 647370.7,
            "humanTokenAmount": 9066411.5, "realizedPnlUsd": 12.25,
        },
    }
    row = extract.extract_signal_event(ev, "now", None)
    assert row["size_usd"] == 41825.86      # حجم المركز بعد الشراء
    assert row["in_amount"] == 3000.0       # ما دُفع فعلاً
    assert row["in_token_address"] == "USDC"
    assert row["out_amount"] == 647370.7
    assert row["token_amount"] == 9066411.5
    assert row["realized_pnl_usd"] == 12.25


def test_trade_size_fields_absent_stay_none_not_zero():
    """multi_user_buy لا تحمل حجماً — None لا صفر (FR-007)."""
    ev = {"id": "e2", "tokenAddress": "0xtok", "type": "multi_user_buy",
          "body": {"ticker": "BBB", "uniqueTraders": 4}}
    row = extract.extract_signal_event(ev, "now", None)
    for f in ("size_usd", "in_amount", "in_token_address", "out_amount",
              "token_amount", "realized_pnl_usd"):
        assert row[f] is None, f


# --- الطبقة الاجتماعية ---
def _thesis(handle, likes=0, replies=0, equity=0.0, created="2026-07-26T10:00:00Z",
            amount=0.0, closed=None):
    """أطروحة بشكل المصدر الحقيقي: المركز في `authorTrade` لا في `equity`."""
    return {"id": f"t-{handle}-{created}", "userHandle": handle, "numReplies": replies,
            "equity": equity, "createdAt": created,
            "authorTrade": {"humanTokenAmount": amount, "usdValue": amount,
                            "closedAt": closed},
            "comment": {"comment": "gm", "numLikes": likes}}


def test_extract_social_uses_the_true_total_not_the_page_size():
    """الاستجابة تعيد 100 عنصر كحدّ أقصى بينما `count` قد يبلغ الآلاف (شوهد
    3111). عدّ العناصر وحده يتشبّع، فتبدو عملة فيها 3111 أطروحة مطابقةً لعملة
    فيها 100 بالضبط — إهدار لأقوى تمييز في هذه الطبقة."""
    raw = {"responseObject": {
        "count": 3111, "hasNextPage": True,
        "items": [_thesis(f"u{i}", likes=1) for i in range(100)],
    }}
    s = extract.extract_social(raw, "0xtok", "56", "now")
    assert s["thesis_total"] == 3111          # الحقيقيّ
    assert s["thesis_sampled"] == 100         # ما رأيناه
    assert s["has_next_page"] == 1
    assert extract.thesis_total(raw) == (3111, True)


def test_thesis_total_falls_back_to_visible_count_when_absent():
    raw = {"responseObject": {"items": [_thesis("a")]}}
    s = extract.extract_social(raw, "0xtok", "56", "now")
    assert s["thesis_total"] == 1
    assert s["has_next_page"] == 0


def test_extract_social_aggregates_engagement():
    raw = {"responseObject": {"items": [
        _thesis("a", likes=7, replies=1, amount=100.0),
        _thesis("b", likes=3, replies=0, amount=0.0, created="2026-07-26T12:00:00Z"),
        _thesis("a", likes=5, replies=2, amount=100.0, created="2026-07-26T11:00:00Z"),
    ]}}
    s = extract.extract_social(raw, "0xtok", "56", "now")
    assert s["thesis_count"] == 3
    assert s["thesis_likes"] == 15
    assert s["thesis_replies"] == 3
    # الكتّاب المميّزون لا الأطروحات: عشر أطروحات من شخص ليست زخماً اجتماعياً
    assert s["thesis_authors"] == 2
    assert s["holder_authors"] == 1          # من يملك حصّة فعلاً
    assert s["newest_thesis_at"] == "2026-07-26T12:00:00Z"


def test_holder_authors_reads_the_position_not_the_dead_equity_field():
    """`equity` صفر في 28,186/28,186 أطروحة مقيسة — حقل ميت من المنبع.

    قراءته أبقت العمود ثابتاً على 0 في 46,040 صفّاً: عمود بلا معلومة يتسلّل إلى
    التدريب كأنّه قياس. المركز الحقيقي في `authorTrade.humanTokenAmount`.
    """
    raw = {"responseObject": {"items": [
        # يملك فعلاً، لكن equity صفر كما يرسلها المصدر دائماً
        _thesis("holder", equity=0.0, amount=250.0),
        _thesis("exited", equity=0.0, amount=0.0, closed="2026-07-26T09:00:00Z"),
    ]}}
    s = extract.extract_social(raw, "0xtok", "56", "now")
    assert s["thesis_authors"] == 2
    assert s["holder_authors"] == 1


def test_holder_authors_counts_partial_exits_that_still_hold():
    """أُغلقت صفقة وبقيت كمية: 2,536 من 28,186 حالة مقيسة.

    لو اعتمدنا `closedAt is None` وحده لسقط هؤلاء، وهم مالكون فعلاً — الكمية
    هي المقياس المباشر لـ«يملك الآن».
    """
    raw = {"responseObject": {"items": [
        _thesis("partial", amount=40.0, closed="2026-07-26T09:00:00Z"),
    ]}}
    assert extract.extract_social(raw, "0xtok", "56", "now")["holder_authors"] == 1


def test_holder_authors_ignores_missing_or_malformed_position():
    """غياب `authorTrade` لا يُعدّ ملكية ولا يرفع استثناءً."""
    for trade in (None, "nope", {}, {"humanTokenAmount": None},
                  {"humanTokenAmount": ""}):
        item = _thesis("x")
        item["authorTrade"] = trade
        s = extract.extract_social({"responseObject": {"items": [item]}}, "a", "1", "t")
        assert s["thesis_authors"] == 1
        assert s["holder_authors"] == 0, trade


def test_extract_social_silence_is_recorded_as_zeros_not_dropped():
    """الصمت إشارة: سلسلة أصفار ثمّ ارتفاع مفاجئ هي ما نريد التقاطه."""
    s = extract.extract_social({"responseObject": {"items": []}}, "0xtok", "56", "now")
    assert s["thesis_count"] == 0
    assert s["thesis_likes"] == 0
    assert s["thesis_authors"] == 0
    assert s["newest_thesis_at"] is None
    assert s["raw_json"]                      # الخام محفوظ رغم الفراغ


def test_extract_social_tolerates_garbage():
    for bad in (None, "nope", {}, {"responseObject": None}, {"responseObject": {}}):
        s = extract.extract_social(bad, "a", "1", "t")
        assert s["thesis_count"] == 0


def test_unwrap_thesis_accepts_list_and_keyed_shapes():
    assert len(extract.unwrap_thesis({"responseObject": [_thesis("a")]})) == 1
    assert len(extract.unwrap_thesis({"responseObject": {"feed": [_thesis("a")]}})) == 1
    assert len(extract.unwrap_thesis({"responseObject": {"items": [_thesis("a")]}})) == 1
    assert extract.unwrap_thesis({"responseObject": {"other": [1]}}) == []


# --- أطروحات فردية (إعادة بناء العدد التاريخي) ---
def test_extract_thesis_items_keeps_creation_time_per_thesis():
    """`createdAt` لكل أطروحة هو ما يجعل استرجاع الماضي ممكناً."""
    raw = {"responseObject": {"items": [
        {"id": "t1", "userHandle": "a", "userId": "u1", "numReplies": 2,
         "equity": 5.0, "tradeId": "tr1", "createdAt": "2026-07-25T10:00:00Z",
         "comment": {"comment": "gm", "numLikes": 7}},
    ]}}
    rows = extract.extract_thesis_items(raw, "0xtok", "56", "fetched-now")
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "t1"
    assert r["created_at"] == "2026-07-25T10:00:00Z"   # وقت الكتابة
    assert r["fetched_at"] == "fetched-now"            # وقت قياس الإعجابات
    assert r["num_likes"] == 7 and r["num_replies"] == 2
    assert r["equity"] == 5.0
    assert r["comment"] == "gm"
    assert r["user_handle"] == "a"


def test_extract_thesis_items_drops_entries_without_id_or_timestamp():
    """بلا معرّف أو ختم لا تفيد إعادة البناء — تُسقط ولا تُفبرك."""
    raw = {"responseObject": {"items": [
        {"id": "t1", "createdAt": "2026-07-25T10:00:00Z", "comment": {}},
        {"id": "t2", "comment": {}},                       # بلا ختم
        {"createdAt": "2026-07-25T11:00:00Z", "comment": {}},  # بلا معرّف
    ]}}
    rows = extract.extract_thesis_items(raw, "0xtok", "56", "t")
    assert [r["id"] for r in rows] == ["t1"]


def test_extract_thesis_items_on_garbage_is_empty():
    for bad in (None, "x", {}, {"responseObject": {}}):
        assert extract.extract_thesis_items(bad, "a", "1", "t") == []


# ---------------------------------------------------------------------------
# الشرعية الخارجية — حقول كانت في الخام المخزّن ولا تُستخرج
# ---------------------------------------------------------------------------
def test_extract_static_counts_exchanges_and_keeps_names():
    """المنصّات قائمة كائنات {name}؛ نعدّها ونحفظ الأسماء لتغيّر الشكل."""
    item = json.loads(json.dumps(TRENDING_ITEM))
    item["exchanges"] = [{"name": "Binance"}, {"name": "MEXC"}, {"name": "Gate"}]
    st = extract.extract_token_static(item, "t")
    assert st["exchanges_count"] == 3
    assert json.loads(st["exchanges_json"]) == ["Binance", "MEXC", "Gate"]


def test_extract_static_accepts_exchange_strings():
    item = json.loads(json.dumps(TRENDING_ITEM))
    item["exchanges"] = ["Binance", "MEXC"]
    st = extract.extract_token_static(item, "t")
    assert st["exchanges_count"] == 2
    assert json.loads(st["exchanges_json"]) == ["Binance", "MEXC"]


def test_extract_static_absent_exchanges_is_unknown_not_zero():
    """FR-007: المفتاح غائب ⇒ None. حاضر وفارغ ⇒ صفر مقيس."""
    st = extract.extract_token_static(TRENDING_ITEM, "t")
    assert st["exchanges_count"] is None

    item = json.loads(json.dumps(TRENDING_ITEM))
    item["exchanges"] = []
    assert extract.extract_token_static(item, "t")["exchanges_count"] == 0


def test_extract_static_info_signals():
    item = json.loads(json.dumps(TRENDING_ITEM))
    item["token"]["info"].update({
        "cmcId": "1839",
        "description": "A community token for testing.",
        "imageBannerUrl": "https://cdn.example/banner.png",
        "imageThumbUrl": "https://cdn.example/thumb.png",
    })
    st = extract.extract_token_static(item, "t")
    assert st["cmc_id"] == "1839"
    assert st["description_len"] == len("A community token for testing.")
    assert st["has_banner"] == 1
    assert st["has_image"] == 1


def test_extract_static_image_without_banner():
    """صورة مصغّرة بلا بانر: has_image=1 وhas_banner=0 — علمان مستقلّان."""
    item = json.loads(json.dumps(TRENDING_ITEM))
    item["token"]["info"]["imageThumbUrl"] = "https://cdn.example/thumb.png"
    st = extract.extract_token_static(item, "t")
    assert st["has_banner"] == 0
    assert st["has_image"] == 1


def test_extract_static_empty_info_gives_zero_flags():
    st = extract.extract_token_static(TRENDING_ITEM, "t")
    assert st["cmc_id"] is None
    assert st["description"] is None
    assert st["description_len"] == 0
    assert st["has_banner"] == 0


# ---------------------------------------------------------------------------
# تفاعل الحدث — من المستوى الأعلى لا body
# ---------------------------------------------------------------------------
def test_extract_signal_engagement_from_top_level():
    event = json.loads(json.dumps(FEED_EVENT))
    event.update({"likes": 4, "views": 91, "numReplies": 2, "pinned": True})
    row = extract.extract_signal_event(event, "t")
    assert row["likes"] == 4
    assert row["views"] == 91
    assert row["num_replies"] == 2
    assert row["pinned"] == 1


def test_extract_signal_engagement_zero_preserved_not_nulled():
    """المصدر يرسل صفراً دائماً؛ صفر مقيس ≠ غائب — نميّزهما."""
    event = json.loads(json.dumps(FEED_EVENT))
    event.update({"likes": 0, "views": 0, "pinned": False})
    row = extract.extract_signal_event(event, "t")
    assert row["likes"] == 0
    assert row["views"] == 0
    assert row["pinned"] == 0
    assert row["num_replies"] is None      # غائب فعلاً، بلا فبركة


def test_extract_signal_captures_counter_asset():
    """الطرف المقابل ملتقط لا مُفسَّر: USDC في 100% اليوم (بلا تباين)،
    ونحفظه ليكشف بدء المصدر بتوجيه أزواج عملة↔عملة."""
    buy = json.loads(json.dumps(FEED_EVENT))
    buy["body"].update({"inTokenAddress": "EPjFWdd5AufqSS",
                        "outTokenAddress": "So1111tokenAddr"})
    row = extract.extract_signal_event(buy, "t")
    assert row["in_token_address"] == "EPjFWdd5AufqSS"
    assert row["out_token_address"] == "So1111tokenAddr"

    sell = json.loads(json.dumps(FEED_EVENT))
    sell["body"].update({"inTokenAddress": "So1111tokenAddr",
                         "outTokenAddress": "EPjFWdd5AufqSS"})
    assert extract.extract_signal_event(sell, "t")["out_token_address"] == "EPjFWdd5AufqSS"


def test_extract_signal_missing_out_token_is_none():
    assert extract.extract_signal_event(FEED_EVENT, "t")["out_token_address"] is None
