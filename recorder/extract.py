"""تحويلات خالصة (pure): الاستجابة الخام من fomo → صفوف جداول المسجّل.

لا شبكة، لا قاعدة بيانات هنا — دوال خالصة قابلة للاختبار على أشكال خام حقيقية
ملتقطة. تعمل مباشرة على الخام (قبل أي تعيين في fomo_client) لأن `_map_trending_token`
يُسقط أثمن الحقول (change/volume/holders/top10/mintable/creator/socials...).

مبدأ FR-007: الحقل الغائب = None، لا فبركة. لا نحسب أي label هنا (منع تسرّب المستقبل).
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import config


def _num(v: Any) -> float | None:
    """تحويل آمن إلى float؛ None/فارغ/غير رقمي → None (لا صفر مفبرك)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))  # يقبل "42" و 42.0
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return str(v)


def _bool_to_int(v: Any) -> int | None:
    """bool → 0/1، مع الحفاظ على False. None → None (لا نفترض)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    return None


def _authority_to_int(v: Any, network_id: Any) -> int | None:
    """سلطة السكّ/التجميد → 0/1، مع تمييز «مُلغاة» من «مجهولة» (FR-007).

    الحقل ليس منطقياً كما يوحي اسمه: fomo يعيد **عنوان** السلطة أو `null`.
    مقيس على 571 لقطة (2026-08-09) وكان مؤكَّداً وقتها من مصدر ثانٍ مستقلّ
    (فحص سلسلة مباشر أُزيل لاحقاً، فيبقى القياس أدناه هو المرجع):

    - سولانا (1399811149): 56 عنواناً و256 `null` — للحقل معنى، و`null`
      تعني السلطة مُلغاة فعلاً (0). قراءة السلسلة أعطت 707/4147 بالنسبة
      نفسها (~17-18%) وبالعناوين نفسها.
    - EVM (56 · 4663 · 8453): `null` في 259/259 بلا استثناء واحد. ليست
      «مُلغاة» بل **غير مقيسة** — لسولانا وحدها سلطة سكّ بهذا المعنى.
      فحص السلسلة وافق: 0 من 3,189 صفّاً EVM.

    فإرجاع 0 لـ EVM يفبرك «آمن» لعملة لم تُقَس أصلاً — وهو بالضبط ما
    يمنعه FR-007. لذا نُرجع None هناك ونترك التغطية ناقصة بصدق.
    """
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, str) and v.strip():
        return 1                      # عنوان سلطة موجود ⇒ الصلاحية قائمة
    if v is None:
        return 0 if _str(network_id) == config.SOLANA_NETWORK_ID else None
    return None


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# feed event (multi_user_buy / large_buy / multi_user_sell)
# الشكل الحيّ المؤكّد: responseObject.feed[] ؛ كل حدث:
#   {id, userId, tokenAddress, networkId, createdAt, type,
#    body{fdv, price, ticker, minutes, marketCap, numTrades,
#         topTraders[{id, userHandle, displayName, userImageUrl}],
# ---------------------------------------------------------------------------
# feed event — نوعان مؤكّدان حيّاً:
#   multi_user_buy: body{ticker, price, fdv, marketCap, numTrades, uniqueTraders,
#       minutes, priceChangePercent, totalVolume, areTopTraders,
#       topTraders[{id, userHandle, displayName}]}  ← إشارة "عدّة متصدّرين"
#   large_buy: body{ticker, price, fdv, marketCap, userId, userHandle, numSwaps,
#       isFirstBuy, percentPnl, avgCost}  ← مشترٍ واحد (لا topTraders)
# نطابق معرّفات المشترين (المتعدّدين + المفرد) بالصدارة لحساب buyers_best_rank.
# ---------------------------------------------------------------------------
def unwrap_feed(raw_envelope: Any) -> list[dict[str, Any]]:
    """يستخرج قائمة أحداث الـ feed من المغلّف الخام. غياب → []."""
    if not isinstance(raw_envelope, Mapping):
        return []
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return []
    for key in ("feed", "items", "data"):
        arr = ro.get(key)
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, Mapping)]
    return []


# مدد الصدارة التي لها أعمدة مستقلّة في signal_events. "all" ليست منها: هي
# `top_trader_match_count`/`buyers_best_rank` بلا لاحقة (العقد القديم محفوظ).
LEADERBOARD_PERIOD_KEYS: tuple[str, ...] = ("24h", "7d", "30d")


def match_ranks_by_period(
    trader_ids: Sequence[str],
    rank_lookups: Mapping[str, Mapping[str, int]] | None,
) -> dict[str, tuple[int | None, int | None]]:
    """معرّفات مشترين × خرائط المدد → {period: (عدد المطابقات, أفضل رتبة)}.

    مدّة غائبة أو خريطتها فارغة (لم تُحمّل بعد) ⇒ (None, None) لا (0, None):
    «لم نقس» ليس «لم يطابق أحد» — الصفر هنا يفبرك نفياً (FR-007).
    """
    out: dict[str, tuple[int | None, int | None]] = {}
    for period in LEADERBOARD_PERIOD_KEYS:
        lut = (rank_lookups or {}).get(period)
        if not lut:
            out[period] = (None, None)
            continue
        ranks = [lut[t] for t in trader_ids if t in lut]
        out[period] = (len(ranks), min(ranks) if ranks else None)
    return out


def extract_signal_event(
    event: Mapping[str, Any],
    recorded_at: str,
    rank_lookup: Mapping[str, int] | None = None,
    rank_lookups: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, Any] | None:
    """حدث feed خام → صفّ signal_events. يحتاج id و tokenAddress (وإلا None).

    rank_lookup: خريطة trader_id → رتبة صدارة (من leaderboard_cache) لحساب
    top_trader_match_count و buyers_best_rank. غيابها لا يُفشل الاستخراج.

    rank_lookups: خرائط المدد {period → {id → rank}} — المصدر يسقّف الصدارة
    الأساسيّة عند 50، لكنّ صدارات المدد (24h/7d/30d) تعيد كلٌّ 100 فاتّحادها 214
    متداولاً (تغطية المطابقة 3.68% ← 15.26% مقيسة على 7,200 حدثاً). تبقى منفصلة
    لا مدموجة: رتبة 7 في 24h ليست رتبة 7 في totalPnL. مدّة بلا خريطة محمّلة
    تبقى None في أعمدتها — غائب ≠ صفر (FR-007).
    """
    ev_id = _str(event.get("id"))
    token_address = _str(event.get("tokenAddress"))
    if not ev_id or not token_address:
        return None  # FR-007: لا مفتاح → نُسقط، لا نفبرك

    body = event.get("body")
    body = body if isinstance(body, Mapping) else {}

    # المشترون: من topTraders[] (multi_user_buy) و/أو المشتري المفرد (large_buy).
    # كلاهما مطابَق بالصدارة لحساب buyers_best_rank و top_trader_match_count.
    top_traders = body.get("topTraders")
    top_traders = top_traders if isinstance(top_traders, list) else []
    top_ids = [
        _str(t.get("id"))
        for t in top_traders
        if isinstance(t, Mapping) and t.get("id") is not None
    ]
    top_ids = [t for t in top_ids if t]

    # المشتري المفرد في large_buy: userId داخل body (أو userId العلوي كاحتياط).
    buyer_id = _str(body.get("userId")) or _str(event.get("userId"))
    # كل المعرّفات المرشّحة للمطابقة (متعدّدون + مفرد)، بلا تكرار مع الحفاظ على الترتيب.
    all_ids = list(top_ids)
    if buyer_id and buyer_id not in all_ids:
        all_ids.append(buyer_id)

    match_count: int | None = None
    best_rank: int | None = None
    if rank_lookup is not None:
        ranks = [rank_lookup[t] for t in all_ids if t in rank_lookup]
        match_count = len(ranks)
        best_rank = min(ranks) if ranks else None

    # مطابقة المدد: كلٌّ مستقلّة. المدّة الفارغة (لم تُحمّل قطّ) تبقى None.
    per_period = match_ranks_by_period(all_ids, rank_lookups)
    matched_flags = [
        c for c in (match_count, *(per_period[p][0] for p in LEADERBOARD_PERIOD_KEYS))
        if c is not None
    ]
    periods_matched = (
        sum(1 for c in matched_flags if c > 0) if matched_flags else None
    )

    return {
        "id": ev_id,
        "token_address": token_address,
        "network_id": _str(event.get("networkId")),
        "ts": _str(event.get("createdAt")),
        "recorded_at": recorded_at,
        "signal_type": _str(event.get("type")) or "unknown",
        "ticker": _str(body.get("ticker")),
        "price_usd": _num(body.get("price")),
        "fdv": _num(body.get("fdv")),
        "market_cap": _num(body.get("marketCap")),
        "num_trades": _int(body.get("numTrades")),
        "unique_traders": _int(body.get("uniqueTraders")),
        "minutes": _int(body.get("minutes")),
        "price_change_pct": _num(body.get("priceChangePercent")),
        "total_volume": _num(body.get("totalVolume")),
        "are_top_traders": _bool_to_int(body.get("areTopTraders")),
        "top_trader_ids_json": _dumps(top_ids),
        "top_trader_match_count": match_count,
        "buyers_best_rank": best_rank,
        # صدارات المدد — تضاعف التغطية وتفصل «متصدّر اليوم» عن «متصدّر الأبد».
        "top_trader_match_count_24h": per_period["24h"][0],
        "buyers_best_rank_24h": per_period["24h"][1],
        "top_trader_match_count_7d": per_period["7d"][0],
        "buyers_best_rank_7d": per_period["7d"][1],
        "top_trader_match_count_30d": per_period["30d"][0],
        "buyers_best_rank_30d": per_period["30d"][1],
        # في كم مدّة ظهر مشترٍ واحد على الأقل (0-4): عرض الحضور لا عمقه —
        # متصدّر في الأربع كلّها حيوان آخر عن متصدّر في 24h وحدها.
        "top_trader_periods_matched": periods_matched,

        # حقول الشراء المفرد (large_buy) — None في multi_user_buy، وهذا صحيح (FR-007).
        "buyer_id": buyer_id,
        "buyer_handle": _str(body.get("userHandle")),
        "num_swaps": _int(body.get("numSwaps")),
        "is_first_buy": _bool_to_int(body.get("isFirstBuy")),
        "buyer_pnl_pct": _num(body.get("percentPnl")),
        "avg_cost": _num(body.get("avgCost")),
        # حجم الصفقة — أثمن ما في large_buy وكان مُهدراً بالكامل.
        # `currentSizeUsd` حجم المركز بعد الشراء، و`inHumanAmount` ما دُفع فعلاً؛
        # الفرق بينهما يميّز "أضاف 3آلاف إلى مركز 42ألف" عن "دخل بـ 45ألف دفعة".
        # `outTokenAddress` يكمل `inTokenAddress`. مقيس على 51,066 حدثاً معبّأً:
        # الطرف المقابل **USDC في 100%** والاتجاه يحدّده `signal_type` وحده
        # (كل large_buy: out=العملة، كل large_sell: out=USDC). أي أنّه بلا تباين
        # اليوم فلا نبني عليه فيتشر — نلتقطه لأنّه رخيص ويكشف اللحظة التي يبدأ
        # فيها المصدر بتوجيه أزواج غير USDC (عملة↔عملة) فينقلب مفيداً.
        "size_usd": _num(body.get("currentSizeUsd")),
        "in_amount": _num(body.get("inHumanAmount")),
        "in_token_address": _str(body.get("inTokenAddress")),
        "out_amount": _num(body.get("outHumanAmount")),
        "out_token_address": _str(body.get("outTokenAddress")),
        "token_amount": _num(body.get("humanTokenAmount")),
        "realized_pnl_usd": _num(body.get("realizedPnlUsd")),
        # التفاعل على الحدث نفسه — من **المستوى الأعلى** لا body.
        # مقيس على 51,062 حدثاً بعد التعبئة الرجعية: الحقول موجودة في 100% من
        # الأحداث وقيمتها **صفر دائماً** (likes/views/pinned بلا أي تباين،
        # وnumReplies صفر في 238 صفّاً وغائب في الباقي). المصدر يرسل الهيكل ولا
        # يعبّئه، وميزة بلا تباين لا تُعلّم النموذج شيئاً — فنُبقي الالتقاط
        # (رخيص، ويكشف اللحظة التي يبدأ المصدر فيها بالتعبئة) ولا نبني عليه
        # فيتشر. `numReplies` يبقى None حيث غاب بلا فبركة (FR-007).
        "likes": _int(event.get("likes")),
        "views": _int(event.get("views")),
        "num_replies": _int(event.get("numReplies")),
        "pinned": _bool_to_int(event.get("pinned")),
        # وسم fomo للحدث. مقيس على 4,000 حدث: قيمة **وحيدة** 'Top Trader' في
        # 3.4% ⇒ نخزّن الوجود لا النصّ. الغياب هنا صفر لا None: الوسم حاضر في
        # كل ردّ (حقل من الهيكل)، وغيابه قرار من المصدر لا قياس مفقود.
        "is_top_trader_tagged": 1 if _str(body.get("tag")) else 0,
        "raw_json": _dumps(event),
    }


# ---------------------------------------------------------------------------
# trending / verified item
# الشكل الحيّ المؤكّد: عناصر تحت responseObject.tokens[] (أو trendingTokens/data)؛
# كل عنصر top-level: {change5m, change1, change4, change12, change24, liquidity,
#   marketCap, priceUSD, volume5m/1/4/12/24, txnCount1/4/12/24, buyCount…,
#   sellCount…, uniqueBuys…, uniqueSells…, holders} + nested token{...}.
# ---------------------------------------------------------------------------
def unwrap_token_list(raw_envelope: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_envelope, Mapping):
        return []
    ro = raw_envelope.get("responseObject")
    # بعض المسارات تعيد responseObject كقائمة مباشرة.
    if isinstance(ro, list):
        return [e for e in ro if isinstance(e, Mapping)]
    if not isinstance(ro, Mapping):
        return []
    for key in ("tokens", "trendingTokens", "data", "verifiedTokens"):
        arr = ro.get(key)
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, Mapping)]
    return []


def _token_obj(item: Mapping[str, Any]) -> Mapping[str, Any]:
    tok = item.get("token")
    return tok if isinstance(tok, Mapping) else {}


def _token_address(item: Mapping[str, Any]) -> str | None:
    tok = _token_obj(item)
    return _str(tok.get("address")) or _str(item.get("address"))


def token_list_address(item: Mapping[str, Any]) -> str | None:
    """عنوان عنصر من قائمة عملات — للربط بالعنوان لا بالترتيب.

    `filterTokens` **يحذف العنوان الميّت بصمت** (مقيس: 5 من 6 رجعت بـ`[200]`)،
    فالفهرس ينزلق والترتيب يكذب. هذه الواجهة العامّة لما يفعله المستخرِج داخلياً.
    """
    return _token_address(item)


def filter_item_protocol(item: Mapping[str, Any]) -> str | None:
    """بروتوكول الـDEX من عنصر `filterTokens` (مثل `PumpAmm`).

    مقيس: **غائب تماماً من خام trending (0 من 3,000)** فهذا مصدره الوحيد؛
    ويسكن **المستوى الأعلى للعنصر لا تحت `token`** — الاحتياط أدناه للشكلين.
    """
    pair = item.get("pair")
    if isinstance(pair, Mapping):
        proto = _str(pair.get("protocol"))
        if proto:
            return proto
    tok = _token_obj(item)
    tok_pair = tok.get("pair")
    if isinstance(tok_pair, Mapping):
        return _str(tok_pair.get("protocol"))
    return None


def extract_market_tick(
    item: Mapping[str, Any], recorded_at: str, source: str
) -> dict[str, Any] | None:
    """عنصر trending/verified خام → صفّ market_ticks. بلا عنوان → None."""
    address = _token_address(item)
    if not address:
        return None
    tok = _token_obj(item)
    info = tok.get("info") if isinstance(tok.get("info"), Mapping) else {}

    return {
        "token_address": address,
        "network_id": _str(tok.get("networkId")) or _str(item.get("networkId")),
        "recorded_at": recorded_at,
        "source": source,
        "price_usd": _num(item.get("priceUSD")),
        "liquidity": _num(item.get("liquidity")),
        "market_cap": _num(item.get("marketCap")),
        "holders": _int(item.get("holders")),
        "top10_holders_pct": _num(item.get("top10HoldersPercent")),
        "change_5m": _num(item.get("change5m")),
        "change_1h": _num(item.get("change1")),
        "change_4h": _num(item.get("change4")),
        "change_12h": _num(item.get("change12")),
        "change_24h": _num(item.get("change24")),
        "volume_5m": _num(item.get("volume5m")),
        "volume_1h": _num(item.get("volume1")),
        "volume_4h": _num(item.get("volume4")),
        "volume_12h": _num(item.get("volume12")),
        "volume_24h": _num(item.get("volume24")),
        "txn_count_1h": _int(item.get("txnCount1")),
        "txn_count_4h": _int(item.get("txnCount4")),
        "txn_count_12h": _int(item.get("txnCount12")),
        "txn_count_24h": _int(item.get("txnCount24")),
        "buy_count_1h": _int(item.get("buyCount1")),
        "buy_count_4h": _int(item.get("buyCount4")),
        "buy_count_12h": _int(item.get("buyCount12")),
        "buy_count_24h": _int(item.get("buyCount24")),
        "sell_count_1h": _int(item.get("sellCount1")),
        "sell_count_4h": _int(item.get("sellCount4")),
        "sell_count_12h": _int(item.get("sellCount12")),
        "sell_count_24h": _int(item.get("sellCount24")),
        "unique_buys_1h": _int(item.get("uniqueBuys1")),
        "unique_buys_4h": _int(item.get("uniqueBuys4")),
        "unique_buys_12h": _int(item.get("uniqueBuys12")),
        "unique_buys_24h": _int(item.get("uniqueBuys24")),
        "unique_sells_1h": _int(item.get("uniqueSells1")),
        "unique_sells_4h": _int(item.get("uniqueSells4")),
        "unique_sells_12h": _int(item.get("uniqueSells12")),
        "unique_sells_24h": _int(item.get("uniqueSells24")),
        "circulating_supply": _num(info.get("circulatingSupply")),
        "total_supply": _num(info.get("totalSupply")),
        "raw_json": _dumps(item),
    }


def extract_token_static(
    item: Mapping[str, Any], recorded_at: str
) -> dict[str, Any] | None:
    """عنصر trending خام → صفّ token_static (ثوابت العملة). بلا عنوان → None."""
    address = _token_address(item)
    if not address:
        return None
    tok = _token_obj(item)
    socials = tok.get("socialLinks") if isinstance(tok.get("socialLinks"), Mapping) else {}
    launchpad = tok.get("launchpad") if isinstance(tok.get("launchpad"), Mapping) else {}
    info = tok.get("info") if isinstance(tok.get("info"), Mapping) else {}

    # إشارات شرعية خارجية — كانت تُهدر بالكامل. المنصّات قائمة كائنات
    # {name} أو سلاسل؛ نعدّها ونحفظ الأسماء (المصدر قد يغيّر الشكل).
    exchanges = item.get("exchanges")
    if isinstance(exchanges, list):
        names: list[str] = []
        for e in exchanges:
            name = _str(e.get("name")) if isinstance(e, Mapping) else _str(e)
            if name:
                names.append(name)
        exchanges_json = _dumps(names)
        exchanges_count = len(names)
    else:
        exchanges_json = _dumps([])
        exchanges_count = None   # غائب ≠ صفر (FR-007)

    desc = _str(info.get("description"))
    banner = info.get("imageBannerUrl")
    has_image = any(
        info.get(k) for k in (
            "imageBannerUrl", "imageLargeUrl", "imageSmallUrl", "imageThumbUrl",
        )
    )

    network_id = _str(tok.get("networkId")) or _str(item.get("networkId")) or ""

    return {
        "token_address": address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "name": _str(tok.get("name")),
        "symbol": _str(tok.get("symbol")),
        "decimals": _int(tok.get("decimals")),
        "mintable": _authority_to_int(tok.get("mintable"), network_id),
        "freezable": _authority_to_int(tok.get("freezable"), network_id),
        "is_scam": _bool_to_int(tok.get("isScam")),
        "creator_address": _str(tok.get("creatorAddress")),
        "launchpad_name": _str(launchpad.get("launchpadName")),
        "migrated": _bool_to_int(launchpad.get("migrated")),
        "graduation_percent": _num(launchpad.get("graduationPercent")),
        "twitter": _str(socials.get("twitter")),
        "telegram": _str(socials.get("telegram")),
        "website": _str(socials.get("website")),
        "discord": _str(socials.get("discord")),
        "token_created_at": _str(tok.get("createdAt")),
        "exchanges_count": exchanges_count,
        "exchanges_json": exchanges_json,
        "cmc_id": _str(info.get("cmcId")),
        "description": desc,
        "description_len": len(desc) if desc else 0,
        "has_banner": 1 if banner else 0,
        "has_image": 1 if has_image else 0,
        "raw_json": _dumps(item),
    }


# ---------------------------------------------------------------------------
# شموع OHLCV — POST /proxy/getBarsNew
# المغلّف: responseObject{s, t[], o[], h[], l[], c[], v[]} بمصفوفات متوازية
# (نمط TradingView). s = "ok" أو "no_data".
#
# مؤكَّد حيّاً (2026-07-26): symbol يجب أن يكون "address:networkId" — العنوان
# المجرّد يجعل خادم fomo يرمي 502 من Cloudflare (يبدو عطلاً وهو طلب مشوّه)،
# و from/to إلزاميان (بدونهما 400 "body.from - Required").
# ---------------------------------------------------------------------------
def bar_wick_flags(
    o: float | None, h: float | None, low: float | None, c: float | None,
    max_ratio: float | None = None,
) -> tuple[int, int]:
    """(h_suspect, l_suspect) لشمعة واحدة معزولة — ذيل يتجاوز جسمها بـ×K.

    فحص احتياطيّ فقط (شمعة بلا جيران). الفحص الأقوى هو `bar_context_flags`
    لأنّ التشوّه يصيب الإغلاق نفسه أحياناً فيتمدّد الجسم ويبدو الذيل معقولاً.
    """
    ratio = max_ratio or config.BAR_WICK_MAX_RATIO
    body_hi = max((v for v in (o, c) if v is not None and v > 0), default=None)
    body_lo = min((v for v in (o, c) if v is not None and v > 0), default=None)
    h_bad = 1 if (h is not None and body_hi is not None and h > ratio * body_hi) else 0
    l_bad = 1 if (
        low is not None and low > 0 and body_lo is not None and body_lo > ratio * low
    ) else 0
    return h_bad, l_bad


def bar_context_flags(
    series: Sequence[Mapping[str, Any]], max_ratio: float | None = None,
) -> list[tuple[int, int, int]]:
    """سلسلة شموع مرتّبة زمنياً → [(h_suspect, l_suspect, c_suspect)] لكلٍّ.

    **مبدأ الحكم: السعر متّصل في المجمّع.** إغلاق الشمعة هو افتتاح تاليتها، فأيّ
    قيمة تتجاوز جارتيها بـ×K ثمّ **لا تستمرّ** ليست سعراً قابلاً للتداول بل تشوّه
    منبع. لهذا الجار هو المرجع لا الجسم:

    - قفزة **مستمرّة** (MarsCoin: 0.0040 → 0.0219 وبقيت 0.0202) = سعر حقيقيّ ✅
    - قفزة **لا تستمرّ** (0.000358 → 12052.5 → 0.000395) = تشوّه ❌

    ثلاث درجات لأنّ التشوّه أصاب ثلاثة مواضع مقيسة حيّاً:
    `h` وحده (2,626,092 بإغلاق 0.0219)، و`c` نفسه (12052.5)، و`l` (قاع مجهريّ).
    `o` لا يُستعمل مرجعاً: هو إغلاق ما قبله فلا يحمل معلومة مستقلّة — وحين
    يتشوّه الإغلاق يتشوّه معه فيُخفي العطب.

    الترتيب: نحكم على الإغلاقات أوّلاً، ثمّ نستعمل **الإغلاقات السليمة وحدها**
    مرجعاً للذيول — وإلّا حجب إغلاقٌ فاسد فساد ذيل شمعته.
    """
    ratio = max_ratio or config.BAR_WICK_MAX_RATIO
    n = len(series)
    closes = [
        (b.get("c") if isinstance(b.get("c"), (int, float)) and (b.get("c") or 0) > 0 else None)
        for b in series
    ]

    # 1) الإغلاقات: قمّة/قاع محليّ لا يُصدّقه **أيّ** من الجارين.
    # الشرط أن يوجد جارٌ على الطرفين: بلا ذلك لا يمكن تمييز «قفزة عابرة» من
    # «بداية اتجاه» — أوّل شمعة قبل rug حقيقيّ تعلو تاليتها بـ×100 وهي سليمة.
    c_bad = [0] * n
    for i in range(n):
        ci = closes[i]
        if ci is None or i == 0 or i == n - 1:
            continue
        prev_c, next_c = closes[i - 1], closes[i + 1]
        if prev_c is None or next_c is None:
            continue
        hi_n, lo_n = max(prev_c, next_c), min(prev_c, next_c)
        if ci > ratio * hi_n or lo_n > ratio * ci:
            c_bad[i] = 1

    # 2) الذيول: المرجع = إغلاق الشمعة (إن سلم) + إغلاقات الجيران السليمة.
    out: list[tuple[int, int, int]] = []
    for i, b in enumerate(series):
        h, low = b.get("h"), b.get("l")
        refs = [closes[i]] if closes[i] is not None and not c_bad[i] else []
        refs += [
            closes[j] for j in (i - 1, i + 1)
            if 0 <= j < n and closes[j] is not None and not c_bad[j]
        ]
        if not refs:  # لا مرجع موثوق → الفحص المعزول احتياطاً
            h_bad, l_bad = bar_wick_flags(
                b.get("o"), h, low, b.get("c"), max_ratio=ratio
            )
            out.append((h_bad, l_bad, c_bad[i]))
            continue
        hi, lo = max(refs), min(refs)
        h_bad = 1 if (isinstance(h, (int, float)) and h > ratio * hi) else 0
        l_bad = 1 if (
            isinstance(low, (int, float)) and low > 0 and lo > ratio * low
        ) else 0
        out.append((h_bad, l_bad, c_bad[i]))
    return out


def classify_asset(
    symbol: str | None,
    price_min: float | None,
    price_max: float | None,
    market_cap_max: float | None,
) -> tuple[str, str]:
    """(asset_class, reason) لعملة من مجموع مشاهداتها.

    fomo منصّة **متعدّدة الأصول** لا سوق ميمات: مقيس في أرشيفنا BTC وETH وSOL
    وUSDT وذهب PAXG وأسهم مرمّزة (AAPL, MSTR, HOOD, INTC, META, SNDK, MU).
    خلطها بالميمات يفسد التدريب: أصل بتريليون أو سهم آبل لا يسلك سلوك عملة
    عمرها ساعتان.

    الترتيب مقصود ومقيس:
    1. `stable` — كل المشاهدات داخل نطاق الدولار (USDT).
    2. `major` — قيمة سوقية > $1B **إن كانت ذات مصداقية**: فوق $5T نتجاهل الرقم
       (شوهد $69T لعملة بـ$0.0888 — حاصل سعر × معروض خرافيّ) ونحكم بالسعر.
    3. `priced` — سعر > $5: الأسهم المرمّزة والسلع. **لا تكشفها القيمة السوقية**
       (AAPL بـ$1.36M فقط لأنّ المرمَّز جزء ضئيل) — السعر وحده يكشفها.
    4. `symbol` — شبكة أمان بالاسم لأصلٍ سعره تحت العتبة (XRP ~$1)، **مشروطة
       بقيمة سوقية معتبرة**: الميمات تنتحل الرموز (مقيس: «BTC» بـ$3.5M).
    5. `meme` — الباقي، وهو الأغلبية الساحقة وهدف المشروع.

    الأصناف غير الميمية تبقى **مسجَّلة** ومصنَّفة: التصنيف للفصل عند التحليل
    والتدريب، لا للحذف (الخام مقدَّس).
    """
    sym = (symbol or "").strip().upper()
    lo, hi = config.ASSET_STABLE_PRICE_BAND
    if price_min is not None and price_max is not None and price_min > 0:
        if lo <= price_min and price_max <= hi:
            return "stable", f"price pinned in [{lo}, {hi}]"
    credible_mc = (
        market_cap_max
        if market_cap_max is not None
        and market_cap_max <= config.ASSET_MAX_CREDIBLE_MARKET_CAP_USD
        else None
    )
    if credible_mc is not None and credible_mc > config.MAJOR_ASSET_MARKET_CAP_USD:
        return "major", f"market_cap {credible_mc:.3g} > {config.MAJOR_ASSET_MARKET_CAP_USD:.0e}"
    if price_max is not None and price_max > config.ASSET_MEME_MAX_PRICE_USD:
        return "priced", f"price {price_max:.4g} > {config.ASSET_MEME_MAX_PRICE_USD}"
    if sym and sym in config.ASSET_NON_MEME_SYMBOLS and (
        credible_mc is None
        or credible_mc >= config.ASSET_SYMBOL_TRUST_MIN_MARKET_CAP_USD
    ):
        return "major", f"known symbol {sym}"
    return "meme", "default"


def extract_bars(
    raw_envelope: Any,
    token_address: str,
    network_id: str,
    resolution: str,
    fetched_at: str,
) -> list[dict[str, Any]]:
    """مغلّف getBarsNew الخام → صفوف token_bars. غياب/تشوّه → [].

    نقبل الشمعة فقط إذا كان ختمها رقمياً صالحاً؛ باقي الحقول قد تكون None
    (FR-007: لا نفبرك صفراً). المصفوفات المتوازية قد تختلف أطوالها عند التشوّه،
    فنقصّها على أقصر طول بدل الافتراض.
    """
    if not isinstance(raw_envelope, Mapping):
        return []
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return []
    ts_arr = ro.get("t")
    if not isinstance(ts_arr, list) or not ts_arr:
        return []

    def _col(key: str) -> list[Any]:
        v = ro.get(key)
        return v if isinstance(v, list) else []

    o, h, low, close, vol = (_col(k) for k in ("o", "h", "l", "c", "v"))

    def _at(arr: list[Any], i: int) -> float | None:
        return _num(arr[i]) if i < len(arr) else None

    rows: list[dict[str, Any]] = []
    for i, raw_ts in enumerate(ts_arr):
        ts = _int(raw_ts)
        if ts is None:
            continue  # شمعة بلا ختم لا تُفيد التوسيم
        rows.append(
            {
                "token_address": token_address,
                "network_id": network_id,
                "resolution": resolution,
                "ts": ts,
                "o": _at(o, i),
                "h": _at(h, i),
                "l": _at(low, i),
                "c": _at(close, i),
                "v": _at(vol, i),
                "fetched_at": fetched_at,
            }
        )
    # الأعلام على الدفعة كسلسلة: الجار هو المرجع (انظر bar_context_flags).
    # الشمعة الأخيرة بلا جار لاحق بعد، فيُعاد الحساب لاحقاً عبر
    # db.recompute_bar_flags حين تصل تاليتها.
    for row, (h_bad, l_bad, c_bad) in zip(rows, bar_context_flags(rows)):
        row["h_suspect"], row["l_suspect"], row["c_suspect"] = h_bad, l_bad, c_bad
    return rows


def bars_status(raw_envelope: Any) -> str | None:
    """حقل `s` من مغلّف getBarsNew ("ok" / "no_data") — أو None عند التشوّه."""
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None
    return _str(ro.get("s"))


# ---------------------------------------------------------------------------
# الطبقة الاجتماعية — GET /feed/token/thesis
# المغلّف: responseObject.items[] (أو .feed) وكل عنصر:
#   {id, type, comment{comment, numLikes}, numReplies, equity, userHandle,
#    createdAt, ticker, tokenAddress, networkId, authorTrade{...}}
# حقلان ميتان من المنبع لا تعوّل عليهما (مقيسان على 28,186 أطروحة 2026-08-09):
#   `equity` = 0 في 100% من العناصر — المركز الحقيقي في `authorTrade`.
#   `numReplies` = 0 في 100% — والردود لا تصل أصلاً (كل parentId فارغ).
# و`comment.reactions.counts.likeCount` صفر دائماً لأنه حالة **القارئ** لا العدّ
# العام؛ العدّ العام هو `comment.numLikes` (غير صفري في 46.7%).
# ---------------------------------------------------------------------------
def unwrap_thesis(raw_envelope: Any) -> list[dict[str, Any]]:
    """يستخرج قائمة الأطروحات من المغلّف الخام. غياب → []."""
    if not isinstance(raw_envelope, Mapping):
        return []
    ro = raw_envelope.get("responseObject")
    if isinstance(ro, list):
        return [e for e in ro if isinstance(e, Mapping)]
    if not isinstance(ro, Mapping):
        return []
    for key in ("items", "feed", "data"):
        arr = ro.get(key)
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, Mapping)]
    return []


def extract_thesis_items(
    raw_envelope: Any, token_address: str, network_id: str, fetched_at: str
) -> list[dict[str, Any]]:
    """مغلّف الأطروحات → صفّ لكل أطروحة (لجدول token_thesis).

    الغرض إعادة بناء **العدد التاريخي**: كل أطروحة تحمل `createdAt`، فيصير
    «كم أطروحة كانت لحظة الإشارة» استعلاماً بسيطاً. بلا هذا التفصيل نملك
    اللحظة الراهنة فقط.
    """
    rows: list[dict[str, Any]] = []
    for it in unwrap_thesis(raw_envelope):
        tid = _str(it.get("id"))
        created = _str(it.get("createdAt"))
        if not tid or not created:
            continue  # بلا معرّف أو ختم لا تفيد إعادة البناء
        comment = it.get("comment") if isinstance(it.get("comment"), Mapping) else {}
        rows.append({
            "id": tid,
            "token_address": token_address,
            "network_id": network_id,
            "created_at": created,
            "user_handle": _str(it.get("userHandle")),
            "user_id": _str(it.get("userId")),
            "num_likes": _int(comment.get("numLikes")),
            "num_replies": _int(it.get("numReplies")),
            "equity": _num(it.get("equity")),
            "trade_id": _str(it.get("tradeId")),
            "comment": _str(comment.get("comment")),
            "fetched_at": fetched_at,
            "raw_json": _dumps(it),
        })
    return rows


def thesis_total(raw_envelope: Any) -> tuple[int | None, bool]:
    """(العدد الكلّي، هل توجد صفحة تالية) من المغلّف.

    **حاسم**: الاستجابة تعيد 100 عنصر كحدّ أقصى بينما `count` قد يبلغ الآلاف
    (شوهد 3111). عدّ العناصر وحده يتشبّع عند 100، فتبدو عملة فيها 3111 أطروحة
    مطابقةً لعملة فيها 100 بالضبط — وهو إهدار لأقوى تمييز في الطبقة الاجتماعية.
    """
    if not isinstance(raw_envelope, Mapping):
        return None, False
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None, False
    return _int(ro.get("count")), bool(ro.get("hasNextPage"))


def extract_social(
    raw_envelope: Any, token_address: str, network_id: str, recorded_at: str
) -> dict[str, Any]:
    """مغلّف الأطروحات الخام → صفّ token_social (مجاميع + الخام).

    نعدّ الكتّاب المميّزين لا الأطروحات وحدها: عشر أطروحات من شخص واحد ليست
    زخماً اجتماعياً. و`holder_authors` يميّز من يملك حصّة فعلاً — الترويج ممّن
    يملك مختلف عن الترويج ممّن لا يملك.

    **مصدر الحصّة**: `authorTrade.humanTokenAmount` لا `equity`. الحقل `equity`
    موجود في المغلّف لكنّه ميت من المنبع: صفر صحيح في 28,186 من 28,186 أطروحة
    مقيسة (2026-08-09)، فكان العمود ثابتاً على 0 في 46,040 صفّاً — عمود بلا
    معلومة. مركز الصفقة الحقيقي في `authorTrade`، ومقيسٌ فيه تباين فعلي:
    15,249/28,186 (54.1%) يملكون كمية موجبة. `closedAt is None` يطابق
    «كمية موجبة» تماماً للمراكز المفتوحة (12,713 كلاهما، وصفر مفتوح بكمية
    صفر) لكنّه يفوّت 2,536 أغلقوا صفقة وما زالوا يملكون بقيّة — فالكمية هي
    المقياس المباشر لـ«يملك الآن».

    **تحذير للقارئ لاحقاً**: `thesis_total` هو العدد الحقيقي من المغلّف، أمّا
    `thesis_likes/replies/authors` فمحسوبة على **أحدث 100 أطروحة فقط** (سقف
    الصفحة). فهي مقاييس عيّنة لا مجاميع كاملة — لا تقارنها بـ`thesis_total`
    كأنّها من المقياس نفسه.
    """
    items = unwrap_thesis(raw_envelope)
    total, has_next = thesis_total(raw_envelope)
    likes = replies = 0
    authors: set[str] = set()
    holders: set[str] = set()
    newest: str | None = None

    for it in items:
        comment = it.get("comment") if isinstance(it.get("comment"), Mapping) else {}
        likes += _int(comment.get("numLikes")) or 0
        replies += _int(it.get("numReplies")) or 0
        handle = _str(it.get("userHandle"))
        if handle:
            authors.add(handle)
            trade = it.get("authorTrade") if isinstance(it.get("authorTrade"), Mapping) else {}
            if (_num(trade.get("humanTokenAmount")) or 0) > 0:
                holders.add(handle)
        created = _str(it.get("createdAt"))
        if created and (newest is None or created > newest):
            newest = created

    return {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        # الحقيقيّ من المغلّف؛ يسقط إلى العدد المرئي إن غاب
        "thesis_total": total if total is not None else len(items),
        "thesis_sampled": len(items),
        "has_next_page": 1 if has_next else 0,
        "thesis_count": len(items),   # مُبقى للتوافق مع القراءات القديمة
        "thesis_likes": likes,
        "thesis_replies": replies,
        "thesis_authors": len(authors),
        "holder_authors": len(holders),
        "newest_thesis_at": newest,
        "raw_json": _dumps(raw_envelope),
    }


# ---------------------------------------------------------------------------
# leaderboard: صفّ trader مُعيَّن (id, rank) → خريطة id→rank
# get_leaderboard تعيد {"traders": [{id, rank, ...}], "total_items": N}
# ---------------------------------------------------------------------------
def leaderboard_items(raw_envelope: Any) -> list[dict[str, Any]]:
    """مغلّف /v2/leaderboard الخام → قائمة المتداولين (dicts) بترتيب الصدارة.

    الشكل الحيّ المؤكّد: responseObject.leaderboard[] بلا حقل rank — الرتبة هي
    موضع العنصر (1-based)، لذا نحافظ على الترتيب ولا نعيد فرزه. نعمل على الخام
    قبل أي تعيين: `_map_trader` يسقط حقولاً قد نحتاجها لاحقاً (سابقة موثّقة:
    حقول التواصل الاجتماعي أُسقطت ثمّ أُعيدت)، والأرشيف الخام وحده يضمن
    إعادة الاشتقاق.
    """
    if not isinstance(raw_envelope, Mapping):
        return []
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return []
    for key in ("leaderboard", "traders", "data", "items"):
        arr = ro.get(key)
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, Mapping)]
    return []


def build_rank_lookup(traders: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """يبني خريطة trader_id → أفضل رتبة. غياب id أو rank → يُتخطّى."""
    lookup: dict[str, int] = {}
    for t in traders:
        if not isinstance(t, Mapping):
            continue
        tid = _str(t.get("id"))
        rank = _int(t.get("rank"))
        if tid is None or rank is None:
            continue
        if tid not in lookup or rank < lookup[tid]:
            lookup[tid] = rank
    return lookup


# ---------------------------------------------------------------------------
# tradingActivity (GET /feed/tradingActivity) — التاريخ القابل للمشيّاط بـlastId.
# الشكل الحيّ المؤكّد (2026-07-28): responseObject.items[] + hasNextPage.
# شكلان للحدث:
#   مسطّح (swap_buy/swap_sell/thesis): usdAmount/marketCap/price/userId في الأعلى.
#   متداخٍ (multi_user_buy/multi_user_sell): body بنفس حقول /feed (numTrades,
#   uniqueTraders, topTraders[]...) + حقول أعلى (likes/views/pinned).
# ---------------------------------------------------------------------------
def activity_page(raw_envelope: Any) -> tuple[list[dict[str, Any]], bool]:
    """مغلّف tradingActivity → (الأحداث, هل توجد صفحة تالية).

    صفحة فارغة تعني نهاية التاريخ (أو تغيّر شكل) — المشيّاط يتوقّف عليها.
    """
    if not isinstance(raw_envelope, Mapping):
        return [], False
    ro = raw_envelope.get("responseObject")
    if isinstance(ro, list):  # مغلّف عارٍ بلا مفاتيح — لا hasNextPage متاح
        return [e for e in ro if isinstance(e, Mapping)], False
    if not isinstance(ro, Mapping):
        return [], False
    for key in ("items", "feed", "activities", "tradingActivity", "data"):
        arr = ro.get(key)
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, Mapping)], bool(ro.get("hasNextPage"))
    return [], bool(ro.get("hasNextPage"))


def _coalesce(*vals: Any) -> Any:
    """أوّل قيمة غير None — الدمج بين الشكل المسطّح وbody بلا فبركة (FR-007)."""
    for v in vals:
        if v is not None:
            return v
    return None


def extract_activity_event(ev: Mapping[str, Any], recorded_at: str) -> dict[str, Any] | None:
    """حدث tradingActivity خام → صفّ activity_events. يحتاج id (وإلّا None).

    الحقول من الأعلى أوّلاً ثمّ body (الأعلى يخصّ الأحداث المسطّحة، وbody يخصّ
    multi_user_*) — كلاهما قد يكون نصّاً رقميّاً و_num يتعامل معه.
    """
    if not isinstance(ev, Mapping):
        return None
    eid = _str(ev.get("id"))
    if not eid:
        return None  # FR-007: لا مفتاح → نُسقط، لا نفبرك
    body = ev.get("body")
    body = body if isinstance(body, Mapping) else {}
    top_traders = body.get("topTraders")
    top_traders = top_traders if isinstance(top_traders, list) else []
    top_ids = [
        _str(t.get("id"))
        for t in top_traders
        if isinstance(t, Mapping) and t.get("id") is not None
    ]
    top_ids = [t for t in top_ids if t]

    return {
        "id": eid,
        "event_type": _str(ev.get("type")) or "unknown",
        "token_address": _str(ev.get("tokenAddress")),
        "network_id": _str(ev.get("networkId")),
        "ts": _str(ev.get("createdAt")),
        "recorded_at": recorded_at,
        "user_id": _str(ev.get("userId")),
        "user_handle": _str(ev.get("userHandle")),
        "trade_id": _str(ev.get("tradeId")),
        "usd_amount": _num(ev.get("usdAmount")),
        "price_usd": _coalesce(_num(ev.get("price")), _num(body.get("price"))),
        "market_cap": _coalesce(_num(ev.get("marketCap")), _num(body.get("marketCap"))),
        "fdv": _coalesce(_num(ev.get("fdv")), _num(body.get("fdv"))),
        "equity": _num(ev.get("equity")),
        "num_trades": _int(body.get("numTrades")),
        "unique_traders": _int(body.get("uniqueTraders")),
        "minutes": _int(body.get("minutes")),
        "price_change_pct": _num(body.get("priceChangePercent")),
        "total_volume": _num(body.get("totalVolume")),
        "are_top_traders": _bool_to_int(body.get("areTopTraders")),
        "top_trader_ids_json": _dumps(top_ids),
        "ticker": _coalesce(_str(ev.get("ticker")), _str(body.get("ticker"))),
        "raw_json": _dumps(ev),
    }


# ---------------------------------------------------------------------------
# تركّز الحيازة وتموضع الحشد — POST /proxy/tokenDetails و GET /hodlers/top
#
# **لماذا مصدران**: `market_ticks.top10_holders_pct` عمود ميّت (صفر من 1.43
# مليون صفّ) لأنّ قوائم trending/verified لا تحمل المفتاح إطلاقاً. وفحص
# السلسلة يغطّي Solana وحدها (2,929 من 3,044) ويصمت كلياً على EVM (صفر من
# 2,378). فالتركّز — أقوى مؤشّر rug — مفقود لكل عملة EVM في الأرشيف.
#
# المصدران **يقيسان شيئين مختلفين**، وهذا مؤكَّد حيّاً 2026-08-09 لا مفترَضاً:
#
# - `tokenDetails` → تركّز السلسلة: `top10HoldersPercent` جاهز (شوهد 83.6% و
#   90.1% و21.9%) مع `holders` الكلّي. يعمل على EVM وSolana معاً — وهو الإصلاح
#   المباشر للعمود الميّت.
# - `/hodlers/top` → **ليس تركّزاً إطلاقاً**: يعيد مستخدمي fomo الحائزين للعملة
#   (276 من 947 · 118 من 14,371) بلا أي نسبة من المعروض، لكن مع تكلفة كل
#   مركز وربحه غير المحقّق ومدّة حمله وعلَم `isDev`. أي أنّه **تموضع الحشد**:
#   «هل حاملو المنصّة تحت الماء؟» سؤال مختلف عن «هل الملكية مركَّزة؟».
#   قياس أوّليّ: 50 من 50 حائزاً تحت الماء في عملة، مقابل 12 من 49 في أخرى.
#
# لذلك لكل مصدر مستخرِج مستقلّ وصفّ مستقلّ (`source` داخل المفتاح الأساسي)،
# فلا يُحسب مقياس مكان الآخر ولا يطمس أحدهما نتيجة الثاني.
# ---------------------------------------------------------------------------
def extract_token_details_holders(
    raw_envelope: Any,
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
) -> dict[str, Any] | None:
    """`tokenDetails` خام → صفّ تركّز سلسلة. بلا مغلّف صالح → None."""
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None

    top10_pct = _num(ro.get("top10HoldersPercent"))
    holder_count = _int(ro.get("holders"))
    if top10_pct is None and holder_count is None:
        return None  # لا معلومة حيازة — لا نكتب صفّاً فارغاً (FR-007)

    return {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
        "source": "token_details",
        "top10_pct": top10_pct,
        "holder_count": holder_count,
        # حقول الحشد لا معنى لها هنا: هذا المصدر لا يعرف مستخدمي المنصّة.
        "platform_holders": None,
        "platform_holders_listed": None,
        "platform_value_usd": None,
        "platform_underwater": None,
        "platform_median_hold_seconds": None,
        "platform_dev_holding": None,
        "top_holders_json": None,
        "raw_json": _dumps(raw_envelope),
    }


def extract_platform_holders(
    raw_envelope: Any,
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
) -> dict[str, Any] | None:
    """`/hodlers/top` خام → صفّ تموضع حشد المنصّة. بلا مغلّف صالح → None.

    `responseObject` قائمة عنصر لكل عملة مطلوبة، وكلّ عنصر يحمل `topHolders`
    (مراكز مستخدمي fomo) و`totalHolders`. النِّسب غائبة تماماً، فلا نشتقّ
    تركّزاً من هنا ولا نخمّنه.
    """
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, list) or not ro:
        return None
    entry = next((e for e in ro if isinstance(e, Mapping)), None)
    if entry is None:
        return None

    raw_holders = entry.get("topHolders")
    holders = [h for h in raw_holders if isinstance(h, Mapping)] if isinstance(raw_holders, list) else []
    total = _int(entry.get("totalHolders"))
    if total is None and not holders:
        return None

    values = [v for v in (_num(h.get("value")) for h in holders) if v is not None]
    # «تحت الماء» = ربح غير محقّق سالب. الغائب لا يُحسب في البسط ولا المقام.
    unreal = [u for u in (_num(h.get("unrealizedPnl")) for h in holders) if u is not None]
    holds = sorted(
        t for t in (_num(h.get("averageHoldTimeSeconds")) for h in holders) if t is not None
    )
    median_hold = holds[len(holds) // 2] if holds else None
    dev = 1 if any(h.get("isDev") for h in holders) else (0 if holders else None)

    return {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
        "source": "hodlers_top",
        # التركّز مجهول من هذا المصدر — يبقى NULL ولا يُفبرك (FR-007).
        "top10_pct": None,
        "holder_count": None,
        "platform_holders": total,
        "platform_holders_listed": len(holders) or None,
        "platform_value_usd": sum(values) if values else None,
        "platform_underwater": sum(1 for u in unreal if u < 0) if unreal else None,
        "platform_median_hold_seconds": median_hold,
        "platform_dev_holding": dev,
        # نحفظ المراكز بلا كتلة `user` الضخمة: المُعرّف والمقبض يكفيان للربط
        # بالمتصدّرين لاحقاً، والباقي يبقى في raw_json على أي حال.
        "top_holders_json": _dumps([
            {
                "user_id": _str((h.get("user") or {}).get("id")) if isinstance(h.get("user"), Mapping) else None,
                "handle": _str((h.get("user") or {}).get("userHandle")) if isinstance(h.get("user"), Mapping) else None,
                "value": _num(h.get("value")),
                "cost_basis": _num(h.get("costBasis")),
                "unrealized_pnl": _num(h.get("unrealizedPnl")),
                "hold_seconds": _num(h.get("averageHoldTimeSeconds")),
                "is_dev": 1 if h.get("isDev") else 0,
            }
            for h in holders[:50]
        ]),
        "raw_json": _dumps(raw_envelope),
    }


# ---------------------------------------------------------------------------
# تدفّق الشراء والبيع — من نفس ردّ `tokenDetails` المجلوب لدورة الحائزين
#
# **صفر نداء إضافي**: `run_holders_cycle` يستدعي `tokenDetails` ستّ مرّات في
# الدورة ثمّ يرمي كل الردّ إلا حقلين (`top10HoldersPercent`, `holders`). الردّ
# يحمل — بحضور **100%** في 300 ردّ مؤرشف — ما لا يعطيه أي مصدر آخر عندنا:
#
# - **انقسام الشراء/البيع**: `buyVolume*` و`sellVolume*`. قوائم trending
#   وverified تعطي `volume_24h` مجموعاً فقط، فاتّجاه التدفّق مجهول اليوم.
# - **طبقة 5 دقائق كاملة**: `buyCount5m`, `sellCount5m`, `uniqueBuys5m`,
#   `uniqueSells5m`. أقصر طبقة نملكها اليوم ساعة — وهي عمياء عن الانعطاف
#   داخل نافذة الـ48 ساعة التي نقيسها.
#
# **لا طبقة 12h في هذا المصدر** ⇒ لا عمود `*_12h` (سيبقى NULL أبداً).
# القيم تصل **نصوصاً** (`'90135'`) — `_num`/`_int` يتكفّلان بالتحويل.
#
# **لماذا جدول مستقلّ** لا أعمدة على `token_holders`: ذاك موصوف بأنّه تركّز
# الملكية، ومستخرِجه يعيد `None` حين تغيب بيانات الحيازة — فيبتلع التدفّق
# معها. ولا صفوف على `market_ticks`: لا سعر هنا، فيصير الصفّ فقيراً ويُفاقم
# مشكلة «أحدث صفّ يفوز» التي أصلحناها في features.market_features.
# ---------------------------------------------------------------------------
# طبقات التدفّق ولاحقة كل طبقة في مفاتيح المصدر. الترتيب يطابق أعمدة الجدول.
_FLOW_PERIODS: tuple[tuple[str, str], ...] = (
    ("5m", "5m"), ("1h", "1"), ("4h", "4"), ("24h", "24"),
)


def extract_token_flow(
    raw_envelope: Any,
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
) -> dict[str, Any] | None:
    """`tokenDetails` خام → صفّ تدفّق شراء/بيع. بلا مغلّف صالح → None.

    نفس توقيع `extract_token_details_holders` بالضبط: **جلب واحد، مستخرِجان،
    جدولان**. الحقل الغائب يبقى `None` ولا يصير صفراً (FR-007) — فرق «لم
    يُقَس» عن «قيس فكان صفراً» هو نفسه معلومةٌ للنموذج.
    """
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None

    row: dict[str, Any] = {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
    }
    # المفاتيح: buyCount5m/buyCount1/buyCount4/buyCount24 — اللاحقة تختلف عن
    # اسم الطبقة في كل ما عدا 5m، فالخريطة أعلاه لا تُختصر إلى صيغة واحدة.
    measured = 0
    for col_pfx, src_pfx, cast in (
        ("buy_count", "buyCount", _int), ("sell_count", "sellCount", _int),
        ("buy_volume", "buyVolume", _num), ("sell_volume", "sellVolume", _num),
        ("unique_buys", "uniqueBuys", _int), ("unique_sells", "uniqueSells", _int),
    ):
        for period, suffix in _FLOW_PERIODS:
            val = cast(ro.get(f"{src_pfx}{suffix}"))
            row[f"{col_pfx}_{period}"] = val
            if val is not None:
                measured += 1

    # لا قيمة واحدة وصلت ⇒ الردّ لا يحمل تدفّقاً: لا نكتب صفّاً فارغاً (FR-007).
    if not measured:
        return None

    # `isLowFees` ليس ميتاً: 11 True من 800 ردّ مؤرشف (1.4%). `_bool_to_int`
    # يحفظ False صفراً ويحفظ الغياب None — والفرق بينهما مقصود.
    row["is_low_fees"] = _bool_to_int(ro.get("isLowFees"))
    row["raw_json"] = _dumps(raw_envelope)
    return row


# ---------------------------------------------------------------------------
# ملفّ المتداول — GET /v2/users/{trader_id}
#
# `signal_events.buyer_id` مخزَّن منذ البداية ولا جدول تجّار في القاعدة: 5,572
# معرّفاً مميّزاً، **3,202 منهم بـ≥3 أحداث**. فسؤال «من اشترى؟» كان بلا جواب
# رغم أنّ الجواب في أيدينا. من يظهر مرّة واحدة لا سلوك له نتعلّمه، فالمتكرّرون
# وحدهم يُجلبون.
#
# الحقول مقيسة حيّاً 2026-08-10 على متداولَين (26 مفتاحاً في `responseObject`):
# `followers` 2,143 و214,422 · `swapCount` 5,450 و3,319 · `numTrades` 518 و587
# · `averageHoldTimeSeconds` 38,304 و169,883 · `totalVolume` 12.99M و5.45M.
# **لا ربح ولا نسبة نجاح في هذا الردّ** — الملفّ لا يحملهما (لهما endpoint
# منفصل)، فلا عمود لهما: العمود الميّت يكلّف ولا يُفيد.
# ---------------------------------------------------------------------------
def extract_trader(
    raw_envelope: Any, trader_id: str, recorded_at: str
) -> dict[str, Any] | None:
    """`/v2/users/{id}` خام → صفّ `traders`. بلا مغلّف صالح → None."""
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None

    # المعرّف من الردّ أوثق من المطلوب، لكنّ غيابه لا يُسقط الصفّ: المفتاح
    # الأساسي هو ما طلبناه به، وهو ما يربط بـ signal_events.buyer_id.
    return {
        "trader_id": _str(ro.get("id")) or trader_id,
        "recorded_at": recorded_at,
        "handle": _str(ro.get("userHandle")),
        "display_name": _str(ro.get("displayName")),
        "followers_count": _int(ro.get("followers")),
        "following_count": _int(ro.get("following")),
        "swap_count": _int(ro.get("swapCount")),
        "num_trades": _int(ro.get("numTrades")),
        "total_volume_usd": _num(ro.get("totalVolume")),
        "avg_hold_seconds": _num(ro.get("averageHoldTimeSeconds")),
        "is_restricted": _bool_to_int(ro.get("isRestricted")),
        "is_private": _bool_to_int(ro.get("private")),
        "wallet_address": _str(ro.get("address")),
        "evm_address": _str(ro.get("evmAddress")),
        "twitter_url": _str(ro.get("twitter")),
        "created_at": _str(ro.get("createdAt")),
        "raw_json": _dumps(raw_envelope),
    }

