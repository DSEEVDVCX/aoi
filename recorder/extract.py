"""تحويلات خالصة (pure): الاستجابة الخام من fomo → صفوف جداول المسجّل.

لا شبكة، لا قاعدة بيانات هنا — دوال خالصة قابلة للاختبار على أشكال خام حقيقية
ملتقطة. تعمل مباشرة على الخام (قبل أي تعيين في fomo_client) لأن `_map_trending_token`
يُسقط أثمن الحقول (change/volume/holders/top10/mintable/creator/socials...).

مبدأ FR-007: الحقل الغائب = None، لا فبركة. لا نحسب أي label هنا (منع تسرّب المستقبل).
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


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


def extract_signal_event(
    event: Mapping[str, Any],
    recorded_at: str,
    rank_lookup: Mapping[str, int] | None = None,
) -> dict[str, Any] | None:
    """حدث feed خام → صفّ signal_events. يحتاج id و tokenAddress (وإلا None).

    rank_lookup: خريطة trader_id → رتبة صدارة (من leaderboard_cache) لحساب
    top_trader_match_count و buyers_best_rank. غيابها لا يُفشل الاستخراج.
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
        "size_usd": _num(body.get("currentSizeUsd")),
        "in_amount": _num(body.get("inHumanAmount")),
        "in_token_address": _str(body.get("inTokenAddress")),
        "out_amount": _num(body.get("outHumanAmount")),
        "token_amount": _num(body.get("humanTokenAmount")),
        "realized_pnl_usd": _num(body.get("realizedPnlUsd")),
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

    return {
        "token_address": address,
        "network_id": _str(tok.get("networkId")) or _str(item.get("networkId")) or "",
        "recorded_at": recorded_at,
        "name": _str(tok.get("name")),
        "symbol": _str(tok.get("symbol")),
        "decimals": _int(tok.get("decimals")),
        "mintable": _bool_to_int(tok.get("mintable")),
        "freezable": _bool_to_int(tok.get("freezable")),
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
#    createdAt, ticker, tokenAddress, networkId}
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
    زخماً اجتماعياً. و`holder_authors` يميّز من يملك حصّة فعلاً (equity>0) —
    الترويج ممّن يملك مختلف عن الترويج ممّن لا يملك.

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
            if (_num(it.get("equity")) or 0) > 0:
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
