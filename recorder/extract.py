"""Pure transforms: the raw fomo response → rows for the recorder's tables.

No network, no database here — pure functions testable against real captured
raw shapes. They work directly on the raw payload (before any mapping in
fomo_client) because `_map_trending_token` drops the most valuable fields
(change/volume/holders/top10/mintable/creator/socials...).

FR-007 principle: a missing field = None, never fabricated. No label is computed
here (preventing future leakage).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import config


def _num(v: Any) -> float | None:
    """Safe cast to float; None/empty/non-numeric → None (no fabricated zero)."""
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
        return int(float(v))  # accepts "42" and 42.0
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        # Upstream occasionally sends a lone UTF-16 surrogate in user text.
        # Preserve it as a JSON-style escape so SQLite can encode the field;
        # raw_json still retains the original payload separately.
        return v.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return str(v).encode("utf-8", errors="backslashreplace").decode("utf-8")


def canonical_token_address(value: Any, network_id: Any = None) -> str | None:
    """Canonicalize EVM hex addresses without changing case-sensitive mints."""
    address = _str(value)
    if (
        address is not None
        and str(network_id or "") != config.SOLANA_NETWORK_ID
        and address.lower().startswith("0x")
    ):
        return address.lower()
    return address


def _bool_to_int(v: Any) -> int | None:
    """bool → 0/1, preserving False. None → None (we do not assume)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    return None


def _authority_to_int(v: Any, network_id: Any) -> int | None:
    """Mint/freeze authority → 0/1, distinguishing "revoked" from "unknown" (FR-007).

    The field is not boolean as its name suggests: fomo returns the authority's
    **address** or `null`. Measured on 571 snapshots (2026-08-09) and confirmed
    at the time by an independent second source (a direct on-chain check later
    removed, so the measurement below remains the reference):

    - Solana (1399811149): 56 addresses and 256 `null` — the field carries
      meaning, and `null` means the authority is actually revoked (0). Reading
      the chain gave 707/4147 at the same ratio (~17-18%) and the same
      addresses.
    - EVM (56 · 4663 · 8453): `null` in 259/259 without a single exception. Not
      "revoked" but **unmeasured** — only Solana has mint authority in this
      sense. The on-chain check agreed: 0 of 3,189 EVM rows.

    Returning 0 for EVM would fabricate "safe" for a coin never measured at
    all — exactly what FR-007 forbids. So we return None there and leave the
    coverage honestly incomplete.
    """
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, str) and v.strip():
        return 1                      # an authority address exists ⇒ the power stands
    if v is None:
        return 0 if _str(network_id) == config.SOLANA_NETWORK_ID else None
    return None


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# feed event (multi_user_buy / large_buy / multi_user_sell)
# Confirmed live shape: responseObject.feed[]; each event:
#   {id, userId, tokenAddress, networkId, createdAt, type,
#    body{fdv, price, ticker, minutes, marketCap, numTrades,
#         topTraders[{id, userHandle, displayName, userImageUrl}],
# ---------------------------------------------------------------------------
# feed event — two kinds confirmed live:
#   multi_user_buy: body{ticker, price, fdv, marketCap, numTrades, uniqueTraders,
#       minutes, priceChangePercent, totalVolume, areTopTraders,
#       topTraders[{id, userHandle, displayName}]}  ← a "multiple leaders" signal
#   large_buy: body{ticker, price, fdv, marketCap, userId, userHandle, numSwaps,
#       isFirstBuy, percentPnl, avgCost}  ← a single buyer (no topTraders)
# We match buyer ids (multiple + single) against the leaderboard to compute buyers_best_rank.
# ---------------------------------------------------------------------------
def unwrap_feed(raw_envelope: Any) -> list[dict[str, Any]]:
    """Extracts the feed event list from the raw envelope. Missing → []."""
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


# Leaderboard periods that have their own columns in signal_events. "all" is not
# one of them: it is the unsuffixed `top_trader_match_count`/`buyers_best_rank` (the old contract is preserved).
LEADERBOARD_PERIOD_KEYS: tuple[str, ...] = ("24h", "7d", "30d")


def match_ranks_by_period(
    trader_ids: Sequence[str],
    rank_lookups: Mapping[str, Mapping[str, int]] | None,
) -> dict[str, tuple[int | None, int | None]]:
    """Buyer ids × period maps → {period: (match count, best rank)}.

    A missing period, or one whose map is empty (not loaded yet) ⇒ (None, None), not (0, None):
    "we did not measure" is not "nobody matched" — a zero here fabricates a negative (FR-007).
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
    """Raw feed event → a signal_events row. Requires id and tokenAddress (else None).

    rank_lookup: a map of trader_id → leaderboard rank (from leaderboard_cache) for
    computing top_trader_match_count and buyers_best_rank. Its absence does not fail the extraction.

    rank_lookups: per-period maps {period → {id → rank}} — the source caps the main
    leaderboard at 50, but each period leaderboard (24h/7d/30d) returns 100, so
    their union is 214 traders (match coverage 3.68% → 15.26% measured on 7,200
    events). They stay separate, not merged: rank 7 in 24h is not rank 7 in
    totalPnL. A period with no loaded map stays None in its columns — absent ≠
    zero (FR-007).
    """
    ev_id = _str(event.get("id"))
    token_address = canonical_token_address(
        event.get("tokenAddress"), event.get("networkId")
    )
    if not ev_id or not token_address:
        return None  # FR-007: no key → we drop it, we do not fabricate

    body = event.get("body")
    body = body if isinstance(body, Mapping) else {}

    # Buyers: from topTraders[] (multi_user_buy) and/or the single buyer (large_buy).
    # Both are matched against the leaderboard to compute buyers_best_rank and top_trader_match_count.
    top_traders = body.get("topTraders")
    top_traders = top_traders if isinstance(top_traders, list) else []
    top_ids = [
        _str(t.get("id"))
        for t in top_traders
        if isinstance(t, Mapping) and t.get("id") is not None
    ]
    top_ids = [t for t in top_ids if t]

    # The single buyer in large_buy: userId inside body (or the top-level userId as a fallback).
    buyer_id = _str(body.get("userId")) or _str(event.get("userId"))
    # All candidate ids for matching (multiple + single), deduplicated while preserving order.
    all_ids = list(top_ids)
    if buyer_id and buyer_id not in all_ids:
        all_ids.append(buyer_id)

    match_count: int | None = None
    best_rank: int | None = None
    if rank_lookup is not None:
        ranks = [rank_lookup[t] for t in all_ids if t in rank_lookup]
        match_count = len(ranks)
        best_rank = min(ranks) if ranks else None

    # Period matching: each period independent. An empty period (never loaded) stays None.
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
        # Period leaderboards — they double coverage and separate "top of today" from "top of all time".
        "top_trader_match_count_24h": per_period["24h"][0],
        "buyers_best_rank_24h": per_period["24h"][1],
        "top_trader_match_count_7d": per_period["7d"][0],
        "buyers_best_rank_7d": per_period["7d"][1],
        "top_trader_match_count_30d": per_period["30d"][0],
        "buyers_best_rank_30d": per_period["30d"][1],
        # In how many periods at least one buyer appeared (0-4): presence, not depth —
        # a leader in all four is a different animal from a leader in 24h alone.
        "top_trader_periods_matched": periods_matched,

        # Single-buy fields (large_buy) — None in multi_user_buy, and that is correct (FR-007).
        "buyer_id": buyer_id,
        "buyer_handle": _str(body.get("userHandle")),
        "num_swaps": _int(body.get("numSwaps")),
        "is_first_buy": _bool_to_int(body.get("isFirstBuy")),
        "buyer_pnl_pct": _num(body.get("percentPnl")),
        "avg_cost": _num(body.get("avgCost")),
        # Trade size — the most valuable part of large_buy, previously wasted entirely.
        # `currentSizeUsd` is the position size after the buy, `inHumanAmount` what was
        # actually paid; the difference tells "added 3k to a 42k position" from "entered
        # with 45k in one go". `outTokenAddress` complements `inTokenAddress`. Measured
        # on 51,066 backfilled events: the counter side is **USDC in 100%** and the
        # direction is set by `signal_type` alone (every large_buy: out=the coin, every
        # large_sell: out=USDC). So it carries no variance today and we build no feature
        # on it — we capture it because it is cheap and it exposes the moment the source
        # starts routing non-USDC pairs (coin↔coin), at which point it turns useful.
        "size_usd": _num(body.get("currentSizeUsd")),
        "in_amount": _num(body.get("inHumanAmount")),
        "in_token_address": _str(body.get("inTokenAddress")),
        "out_amount": _num(body.get("outHumanAmount")),
        "out_token_address": _str(body.get("outTokenAddress")),
        "token_amount": _num(body.get("humanTokenAmount")),
        "realized_pnl_usd": _num(body.get("realizedPnlUsd")),
        # Engagement on the event itself — from the **top level**, not body.
        # Measured on 51,062 events after backfill: the fields are present in 100%
        # of events and their value is **always zero** (likes/views/pinned with no
        # variance at all, and numReplies zero in 238 rows and absent in the rest).
        # The source sends the structure and never fills it, and a feature with no
        # variance teaches the model nothing — so we keep capturing it (cheap, and
        # it exposes the moment the source starts filling it) but build no feature
        # on it. `numReplies` stays None where it was absent, un-fabricated (FR-007).
        "likes": _int(event.get("likes")),
        "views": _int(event.get("views")),
        "num_replies": _int(event.get("numReplies")),
        "pinned": _bool_to_int(event.get("pinned")),
        # fomo's tag on the event. Measured on 4,000 events: a **single** value
        # 'Top Trader' in 3.4% ⇒ we store presence, not the text. Absence here is
        # zero, not None: the tag is present in every response (a structural
        # field), and its absence is a source decision, not a lost measurement.
        "is_top_trader_tagged": 1 if _str(body.get("tag")) else 0,
        "raw_json": _dumps(event),
    }


# ---------------------------------------------------------------------------
# trending / verified item
# Confirmed live shape: items under responseObject.tokens[] (or trendingTokens/data);
# each item top-level: {change5m, change1, change4, change12, change24, liquidity,
#   marketCap, priceUSD, volume5m/1/4/12/24, txnCount1/4/12/24, buyCount…,
#   sellCount…, uniqueBuys…, uniqueSells…, holders} + nested token{...}.
# ---------------------------------------------------------------------------
def unwrap_token_list(raw_envelope: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_envelope, Mapping):
        return []
    ro = raw_envelope.get("responseObject")
    # Some paths return responseObject directly as a list.
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
    network = tok.get("networkId") or item.get("networkId")
    return canonical_token_address(
        tok.get("address") or item.get("address"), network
    )


def token_list_address(item: Mapping[str, Any]) -> str | None:
    """Address of a token-list item — for joining by address, not by position.

    `filterTokens` **silently drops the dead address** (measured: 5 of 6 returned
    `[200]`), so the index slides and the order lies. This is the public face of
    what the extractor does internally.
    """
    return _token_address(item)


def token_list_network(item: Mapping[str, Any]) -> str:
    """Network identifier carried by a token-list item."""
    tok = _token_obj(item)
    return _str(tok.get("networkId")) or _str(item.get("networkId")) or ""


def token_list_created_at(item: Mapping[str, Any]) -> str | None:
    """Creation timestamp carried by a token-list item, if present."""
    tok = _token_obj(item)
    return _str(tok.get("createdAt")) or _str(item.get("createdAt"))


def filter_item_protocol(item: Mapping[str, Any]) -> str | None:
    """DEX protocol from a `filterTokens` item (e.g. `PumpAmm`).

    Measured: **entirely absent from raw trending (0 of 3,000)**, so this is its
    only source; and it lives at the **item's top level, not under `token`** — the
    fallback below covers both shapes.
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
    """Raw trending/verified item → a market_ticks row. No address → None."""
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
    """Raw trending item → a token_static row (the token's constants). No address → None."""
    address = _token_address(item)
    if not address:
        return None
    tok = _token_obj(item)
    socials = tok.get("socialLinks") if isinstance(tok.get("socialLinks"), Mapping) else {}
    launchpad = tok.get("launchpad") if isinstance(tok.get("launchpad"), Mapping) else {}
    info = tok.get("info") if isinstance(tok.get("info"), Mapping) else {}
    pair = item.get("pair")
    pair = pair if isinstance(pair, Mapping) else {}

    # External legitimacy signals — previously wasted entirely. The platforms come
    # as a list of {name} objects or strings; we count them and keep the names
    # (the source may change the shape).
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
        exchanges_count = None   # absent ≠ zero (FR-007)

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
        "token_created_at_observed_at": (
            recorded_at if tok.get("createdAt") not in (None, "") else None
        ),
        "exchanges_count": exchanges_count,
        "exchanges_json": exchanges_json,
        "cmc_id": _str(info.get("cmcId")),
        "description": desc,
        "description_len": len(desc) if desc else 0,
        "has_banner": 1 if banner else 0,
        "has_image": 1 if has_image else 0,
        "dex_protocol": _str(pair.get("protocol")),
        "raw_json": _dumps(item),
    }


# ---------------------------------------------------------------------------
# OHLCV candles — POST /proxy/getBarsNew
# Envelope: responseObject{s, t[], o[], h[], l[], c[], v[]} with parallel arrays
# (TradingView style). s = "ok" or "no_data".
#
# Confirmed live (2026-07-26): symbol must be "address:networkId" — a bare
# address makes the fomo server throw a 502 from Cloudflare (it looks like an
# outage but is a malformed request), and from/to are mandatory (without them:
# 400 "body.from - Required").
# ---------------------------------------------------------------------------
def bar_wick_flags(
    o: float | None, h: float | None, low: float | None, c: float | None,
    max_ratio: float | None = None,
) -> tuple[int, int]:
    """(h_suspect, l_suspect) for one isolated candle — a wick exceeding its body by ×K.

    A fallback check only (a candle without neighbors). The stronger check is
    `bar_context_flags`, because the corruption sometimes hits the close itself,
    stretching the body so the wick looks reasonable.
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
    """A time-ordered candle series → [(h_suspect, l_suspect, c_suspect)] for each.

    **The judging principle: price is continuous in the aggregate.** A candle's
    close is the next one's open, so any value that exceeds both its neighbors
    by ×K and then **does not persist** is not a tradable price but source
    corruption. That is why the neighbor is the reference, not the body:

    - A **persistent** jump (MarsCoin: 0.0040 → 0.0219 and it stayed 0.0202) = a real price ✅
    - A jump that **does not persist** (0.000358 → 12052.5 → 0.000395) = corruption ❌

    Three degrees because the corruption hit three positions measured live:
    `h` alone (2,626,092 with a close of 0.0219), `c` itself (12052.5), and `l`
    (a microscopic bottom). `o` is never used as a reference: it is the previous
    close and carries no independent information — and when the close is
    corrupted, it is corrupted along with it, hiding the damage.

    Order of operations: judge the closes first, then use **only the clean
    closes** as the reference for the wicks — otherwise a corrupted close would
    mask the corruption of its own candle's wick.
    """
    ratio = max_ratio or config.BAR_WICK_MAX_RATIO
    n = len(series)
    closes = [
        (b.get("c") if isinstance(b.get("c"), (int, float)) and (b.get("c") or 0) > 0 else None)
        for b in series
    ]

    # 1) Closes: a local top/bottom that **neither** neighbor vouches for.
    # The condition is that a neighbor exists on both sides: without it, one
    # cannot tell a "transient spike" from "the start of a trend" — the first
    # candle before a real rug rises ×100 over the next one and is legitimate.
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

    # 2) Wicks: the reference = the candle's close (if clean) + the neighbors' clean closes.
    out: list[tuple[int, int, int]] = []
    for i, b in enumerate(series):
        h, low = b.get("h"), b.get("l")
        refs = [closes[i]] if closes[i] is not None and not c_bad[i] else []
        refs += [
            closes[j] for j in (i - 1, i + 1)
            if 0 <= j < n and closes[j] is not None and not c_bad[j]
        ]
        if not refs:  # no trusted reference → fall back to the isolated check
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
    """(asset_class, reason) for a token from the sum of its observations.

    fomo is a **multi-asset** platform, not a meme-only market: our archive has
    measured BTC, ETH, SOL, USDT, PAXG gold, and tokenized stocks (AAPL, MSTR,
    HOOD, INTC, META, SNDK, MU). Mixing them with memes corrupts training: a
    trillion-dollar asset or an Apple share does not behave like a coin two
    hours old.

    The order is deliberate and measured:
    1. `stable` — all observations inside the dollar band (USDT).
    2. `major` — market cap > $1B **if it is credible**: above $5T we ignore the
       number (saw $69T for a coin at $0.0888 — price × a fantasy supply) and
       judge by price.
    3. `priced` — price > $5: tokenized stocks and commodities. **Market cap
       does not expose them** (AAPL at only $1.36M because the tokenized part
       is a sliver) — price alone exposes them.
    4. `symbol` — a name-based safety net for an asset priced under the
       threshold (XRP ~$1), **conditional on a credible market cap**: memes
       forge symbols (measured: "BTC" at $3.5M).
    5. `meme` — the rest, which is the overwhelming majority and the project's
       target.

    Non-meme classes stay **recorded** and classified: classification exists to
    separate at analysis and training time, not to delete (the raw data is
    sacred).
    """
    sym = (symbol or "").strip().upper()
    lo, hi = config.ASSET_STABLE_PRICE_BAND
    if (price_min is not None and price_max is not None and price_min > 0
            and lo <= price_min and price_max <= hi):
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
    """Raw getBarsNew envelope → token_bars rows. Missing/corrupt → [].

    We accept a candle only if its timestamp is a valid number; the other fields
    may be None (FR-007: we do not fabricate a zero). The parallel arrays may
    differ in length under corruption, so we cut them to the shortest length
    rather than assume.
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
            continue  # a candle without a timestamp is useless for labeling
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
    # The flags are computed over the batch as a series: the neighbor is the
    # reference (see bar_context_flags). The last candle has no later neighbor
    # yet, so it is recomputed later via db.recompute_bar_flags once its
    # successor arrives.
    for row, (h_bad, l_bad, c_bad) in zip(rows, bar_context_flags(rows), strict=True):
        row["h_suspect"], row["l_suspect"], row["c_suspect"] = h_bad, l_bad, c_bad
    return rows


def bars_status(raw_envelope: Any) -> str | None:
    """The `s` field of a getBarsNew envelope ("ok" / "no_data") — or None when corrupt."""
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None
    return _str(ro.get("s"))


# ---------------------------------------------------------------------------
# The social layer — GET /feed/token/thesis
# Envelope: responseObject.items[] (or .feed), each item:
#   {id, type, comment{comment, numLikes}, numReplies, equity, userHandle,
#    createdAt, ticker, tokenAddress, networkId, authorTrade{...}}
# Two fields are dead from upstream — do not rely on them (measured on 28,186
# theses 2026-08-09):
#   `equity` = 0 in 100% of items — the real position is in `authorTrade`.
#   `numReplies` = 0 in 100% — and no replies ever arrive (every parentId is
#   empty).
# And `comment.reactions.counts.likeCount` is always zero because it is the
# **reader's** state, not the public count; the public count is
# `comment.numLikes` (non-zero in 46.7%).
# ---------------------------------------------------------------------------
def unwrap_thesis(raw_envelope: Any) -> list[dict[str, Any]]:
    """Extracts the thesis list from the raw envelope. Missing → []."""
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
    """Thesis envelope → one row per thesis (for the token_thesis table).

    The goal is to reconstruct **the historical count**: every thesis carries
    `createdAt`, so "how many theses existed at the moment of the signal"
    becomes a simple query. Without this detail we would only ever have the
    present moment.
    """
    rows: list[dict[str, Any]] = []
    for it in unwrap_thesis(raw_envelope):
        tid = _str(it.get("id"))
        created = _str(it.get("createdAt"))
        if not tid or not created:
            continue  # without an id or timestamp it is useless for reconstruction
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
    """(The total count, whether a next page exists) from the envelope.

    **Critical**: the response returns at most 100 items while `count` can reach
    the thousands (3111 seen). Counting items alone saturates at 100, so a coin
    with 3111 theses looks identical to one with exactly 100 — a waste of the
    social layer's strongest discriminator.
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
    """Raw thesis envelope → a token_social row (aggregates + raw).

    We count distinct authors, not theses alone: ten theses from one person is
    not social momentum. And `holder_authors` distinguishes those who actually
    hold a stake — promotion from a holder is different from promotion from a
    non-holder.

    **Stake source**: `authorTrade.humanTokenAmount`, not `equity`. The `equity`
    field exists in the envelope but is dead from upstream: a true zero in
    28,186 of 28,186 measured theses (2026-08-09), leaving the column constant
    at 0 across 46,040 rows — a column with no information. The trade's real
    position is in `authorTrade`, and real variance was measured there:
    15,249/28,186 (54.1%) hold a positive quantity. `closedAt is None` matches
    "positive quantity" exactly for open positions (12,713 both, and zero open
    positions with zero quantity), but it misses 2,536 who closed a trade and
    still hold the remainder — so quantity is the direct measure of "holds now".

    **A warning for the future reader**: `thesis_total` is the true count from
    the envelope, while `thesis_likes/replies/authors` are computed over **only
    the newest 100 theses** (the page cap). They are sample metrics, not full
    aggregates — do not compare them to `thesis_total` as if they came from the
    same measure.
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
        # The true count from the envelope; falls back to the visible count if absent
        "thesis_total": total if total is not None else len(items),
        "thesis_sampled": len(items),
        "has_next_page": 1 if has_next else 0,
        "thesis_count": len(items),   # kept for compatibility with old reads
        "thesis_likes": likes,
        "thesis_replies": replies,
        "thesis_authors": len(authors),
        "holder_authors": len(holders),
        "newest_thesis_at": newest,
        "raw_json": _dumps(raw_envelope),
    }


# ---------------------------------------------------------------------------
# leaderboard: each trader row (id, rank) → an id→rank map
# get_leaderboard returns {"traders": [{id, rank, ...}], "total_items": N}
# ---------------------------------------------------------------------------
def leaderboard_items(raw_envelope: Any) -> list[dict[str, Any]]:
    """Raw /v2/leaderboard envelope → the trader list (dicts) in leaderboard order.

    Confirmed live shape: responseObject.leaderboard[] with no rank field — the
    rank is the item's position (1-based), so we preserve the order and do not
    re-sort. We work on the raw payload before any mapping: `_map_trader` drops
    fields we may need later (a documented precedent: the social fields were
    dropped and then reintroduced), and only the raw archive guarantees
    re-derivation.
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
    """Builds a trader_id → best-rank map. A missing id or rank → skipped."""
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
# tradingActivity (GET /feed/tradingActivity) — the history walkable with lastId.
# Confirmed live shape (2026-07-28): responseObject.items[] + hasNextPage.
# Two event shapes:
#   flat (swap_buy/swap_sell/thesis): usdAmount/marketCap/price/userId at the top level.
#   nested (multi_user_buy/multi_user_sell): body with the same fields as /feed
#   (numTrades, uniqueTraders, topTraders[]...) + top-level fields (likes/views/pinned).
# ---------------------------------------------------------------------------
def activity_page(raw_envelope: Any) -> tuple[list[dict[str, Any]], bool]:
    """tradingActivity envelope → (the events, whether a next page exists).

    An empty page means the end of history (or a shape change) — the walker stops on it.
    """
    if not isinstance(raw_envelope, Mapping):
        return [], False
    ro = raw_envelope.get("responseObject")
    if isinstance(ro, list):  # a bare envelope without keys — no hasNextPage available
        return [e for e in ro if isinstance(e, Mapping)], False
    if not isinstance(ro, Mapping):
        return [], False
    for key in ("items", "feed", "activities", "tradingActivity", "data"):
        arr = ro.get(key)
        if isinstance(arr, list):
            return [e for e in arr if isinstance(e, Mapping)], bool(ro.get("hasNextPage"))
    return [], bool(ro.get("hasNextPage"))


def _coalesce(*vals: Any) -> Any:
    """The first value that is not None — merging the flat shape and body without fabricating (FR-007)."""
    for v in vals:
        if v is not None:
            return v
    return None


def extract_activity_event(ev: Mapping[str, Any], recorded_at: str) -> dict[str, Any] | None:
    """Raw tradingActivity event → an activity_events row. Requires id (else None).

    Fields are read from the top level first, then body (the top level serves
    flat events, body serves multi_user_*) — both may be numeric strings, and
    _num handles them.
    """
    if not isinstance(ev, Mapping):
        return None
    eid = _str(ev.get("id"))
    if not eid:
        return None  # FR-007: no key → we drop it, we do not fabricate
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
# Ownership concentration and crowd positioning — POST /proxy/tokenDetails and GET /hodlers/top
#
# **Why two sources**: `market_ticks.top10_holders_pct` is a dead column (zero
# of 1.43 million rows) because the trending/verified lists never carry the key
# at all. The on-chain check covers Solana only (2,929 of 3,044) and is
# completely silent on EVM (zero of 2,378). So concentration — the strongest
# rug indicator — is missing for every EVM coin in the archive.
#
# The two sources **measure two different things**, confirmed live 2026-08-09, not assumed:
#
# - `tokenDetails` → on-chain concentration: `top10HoldersPercent` ready-made
#   (saw 83.6%, 90.1%, and 21.9%) along with the overall `holders`. It works on
#   EVM and Solana alike — the direct fix for the dead column.
# - `/hodlers/top` → **not concentration at all**: it returns the fomo users
#   holding the coin (276 of 947 · 118 of 14,371) with no ratio of supply, but
#   with each position's cost, unrealized profit, holding duration, and the
#   `isDev` flag. That makes it **crowd positioning**: "are the platform's
#   holders underwater?" is a different question from "is ownership
#   concentrated?". A first measurement: 50 of 50 holders underwater in one
#   coin, versus 12 of 49 in another.
#
# Hence each source gets its own extractor and its own row (`source` is part of
# the primary key), so neither metric is counted in place of the other and
# neither masks the other's result.
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
    """Raw `tokenDetails` → an on-chain concentration row. No valid envelope → None."""
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None

    top10_pct = _num(ro.get("top10HoldersPercent"))
    holder_count = _int(ro.get("holders"))
    if top10_pct is None and holder_count is None:
        return None  # no holder information — we do not write an empty row (FR-007)

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
        # The crowd fields are meaningless here: this source knows nothing about platform users.
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
    """Raw `/hodlers/top` → a platform crowd-positioning row. No valid envelope → None.

    `responseObject` is a list with one element per requested coin, and each
    element carries `topHolders` (fomo users' positions) and `totalHolders`.
    The ratios are entirely absent, so we derive no concentration here and do
    not guess one.
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
    # "Underwater" = a negative unrealized profit. A missing value counts in neither numerator nor denominator.
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
        # Concentration is unknown from this source — it stays NULL, un-fabricated (FR-007).
        "top10_pct": None,
        "holder_count": None,
        "platform_holders": total,
        "platform_holders_listed": len(holders) or None,
        "platform_value_usd": sum(values) if values else None,
        "platform_underwater": sum(1 for u in unreal if u < 0) if unreal else None,
        "platform_median_hold_seconds": median_hold,
        "platform_dev_holding": dev,
        # We save the positions without the bulky `user` block: the id and the
        # handle are enough to join with the leaders later, and the rest stays
        # in raw_json anyway.
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
# Buy and sell flow — from the same `tokenDetails` response fetched for the holders cycle
#
# **Zero extra calls**: `run_holders_cycle` calls `tokenDetails` six times per
# cycle and then throws the whole response away except two fields
# (`top10HoldersPercent`, `holders`). The response carries — present in **100%**
# of 300 archived responses — what no other source we have provides:
#
# - **The buy/sell split**: `buyVolume*` and `sellVolume*`. The trending and
#   verified lists give only an aggregate `volume_24h`, so flow direction is
#   unknown today.
# - **A full 5-minute tier**: `buyCount5m`, `sellCount5m`, `uniqueBuys5m`,
#   `uniqueSells5m`. The shortest tier we have today is one hour — blind to the
#   turns inside the 48-hour window we measure.
#
# **No 12h tier in this source** ⇒ no `*_12h` column (it would stay NULL
# forever). The values arrive **as strings** (`'90135'`) — `_num`/`_int` handle
# the conversion.
#
# **Why a separate table** rather than columns on `token_holders`: that one is
# described as ownership concentration, and its extractor returns `None` when
# the holder data is missing — it would swallow the flow along with it. And no
# rows on `market_ticks`: there is no price here, so the row would be poor and
# would worsen the "latest row wins" problem we fixed in
# features.market_features.
# ---------------------------------------------------------------------------
# The flow tiers and each tier's suffix in the source keys. The order matches the table columns.
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
    """Raw `tokenDetails` → a buy/sell flow row. No valid envelope → None.

    Exactly the same signature as `extract_token_details_holders`: **one fetch,
    two extractors, two tables**. A missing field stays `None` and never becomes
    zero (FR-007) — the difference between "not measured" and "measured as
    zero" is itself information for the model.
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
    # The keys: buyCount5m/buyCount1/buyCount4/buyCount24 — the suffix differs
    # from the tier name in everything except 5m, so the map above cannot
    # collapse into a single formula.
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

    # Not a single value arrived ⇒ the response carries no flow: we do not write an empty row (FR-007).
    if not measured:
        return None

    # `isLowFees` is not dead: 11 True in 800 archived responses (1.4%).
    # `_bool_to_int` keeps False as zero and keeps absence as None — and the
    # difference between the two is deliberate.
    row["is_low_fees"] = _bool_to_int(ro.get("isLowFees"))
    row["raw_json"] = _dumps(raw_envelope)
    return row


# ---------------------------------------------------------------------------
# The trader profile — GET /v2/users/{trader_id}
#
# `signal_events.buyer_id` has been stored from the start and there is no
# traders table in the DB: 5,572 distinct ids, **3,202 of them with ≥3
# events**. So "who bought?" had no answer even though the answer was in our
# hands. Someone who appears once has no behavior for us to learn; only the
# repeat visitors get fetched.
#
# Fields measured live 2026-08-10 on two traders (26 keys in `responseObject`):
# `followers` 2,143 and 214,422 · `swapCount` 5,450 and 3,319 · `numTrades` 518
# and 587 · `averageHoldTimeSeconds` 38,304 and 169,883 · `totalVolume` 12.99M
# and 5.45M. **No profit and no win rate in this response** — the profile does
# not carry them (they have a separate endpoint), so no column for them: a dead
# column costs and gives nothing.
# ---------------------------------------------------------------------------
def extract_traders(raw_envelope: Any, recorded_at: str) -> dict[str, dict[str, Any]]:
    """Raw `/v2/users?userIds=…` → {id: a `traders` row}.

    The batch replaced the single call because `/v2/users/{id}` now returns 404
    for every id (measured 2026-08-20). Whoever is absent from `users` gets no
    row: the source silently drops the unknown without erroring on it, so
    absence is an answer, not a failure.

    And each row's `raw_json` is **the user object alone**, not the whole
    envelope: a hundred rows each carrying the full hundred-user response means
    a hundredfold size inflation in a table that gets overwritten by `INSERT OR
    REPLACE` every cycle.

    And the keying: what the source returns in `id` is the key, not what we
    requested by — and the comparison in `run_traders_cycle` is what ties the
    two together.
    """
    if not isinstance(raw_envelope, Mapping):
        return {}
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return {}
    users = ro.get("users")
    if not isinstance(users, Sequence) or isinstance(users, (str, bytes)):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for user in users:
        if not isinstance(user, Mapping):
            continue
        tid = _str(user.get("id"))
        if not tid:
            continue
        out[tid] = _trader_row(user, tid, recorded_at)
    return out


def extract_trader(
    raw_envelope: Any, trader_id: str, recorded_at: str
) -> dict[str, Any] | None:
    """Raw `/v2/users/{id}` → a `traders` row. No valid envelope → None.

    The single-item path is dead upstream (404 for every id since
    2026-08-19T14:53Z), so this one remains for the saved raw archive and for
    tests, not for live collection.
    """
    if not isinstance(raw_envelope, Mapping):
        return None
    ro = raw_envelope.get("responseObject")
    if not isinstance(ro, Mapping):
        return None
    return _trader_row(ro, trader_id, recorded_at, envelope=raw_envelope)


def _trader_row(
    ro: Mapping[str, Any],
    trader_id: str,
    recorded_at: str,
    envelope: Any = None,
) -> dict[str, Any]:
    """One user object → a `traders` row. One implementation for both paths,
    batch and single, so a field mapping cannot drift in one without the other."""
    # The id from the response is more trustworthy than the requested one, but
    # its absence does not drop the row: the primary key is what we requested
    # by, and that is what joins to signal_events.buyer_id.
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
        "raw_json": _dumps(envelope if envelope is not None else ro),
    }


# ---------------------------------------------------------------------------
# Ownership concentration from **the chain** (Solana RPC) — not from FOMO
#
# FOMO gives `top10HoldersPercent` alone and only every ~25 minutes (measured
# median gap 25.0 over 19,440 pairs). The chain gives top1/5/10/20 in one call
# (measured 230ms), so top1 — the single whale — becomes measured rather than
# derived, and the cadence becomes 5 minutes.
#
# **The arithmetic runs on integers in base units**, not the ready-made
# decimals (`uiAmount`): the source returns `amount` as an integer string with
# the same `decimals` for display and for math, and Python integers have
# unlimited precision — while `uiAmount` is a float that loses digits at an
# 18-decimal supply. Numerator and denominator from the same batch ⇒ the same
# instant.
#
# `top_accounts` is a necessary column, not decoration: the source returns **at
# most 20 accounts**, so a coin with seven holders returns seven and its
# `top20_pct` from them is the sum of all (100%), not "the top 20". Without
# this column nobody can tell real concentration from a coin with no holders.
# ---------------------------------------------------------------------------
_CHAIN_TIERS = ((1, "top1_pct"), (5, "top5_pct"), (10, "top10_pct"), (20, "top20_pct"))


def extract_chain_concentration(
    raw_envelope: Any,
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
) -> dict[str, Any] | None:
    """A `{supply, largest}` envelope from `SolanaRPC` → a `chain_concentration` row.

    With no valid envelope, or with neither supply nor accounts → `None` (no
    fabricated row, FR-007). A zero supply (a full burn) keeps the row — zero
    here is a **measurement** — but the ratios stay `NULL` because division by
    zero has no result.
    """
    if not isinstance(raw_envelope, Mapping):
        return None

    supply_v = raw_envelope.get("supply")
    supply_v = supply_v.get("value") if isinstance(supply_v, Mapping) else None
    largest_v = raw_envelope.get("largest")
    largest_v = largest_v.get("value") if isinstance(largest_v, Mapping) else None

    supply_base: int | None = None
    supply_ui: float | None = None
    decimals: int | None = None
    if isinstance(supply_v, Mapping):
        decimals = _int(supply_v.get("decimals"))
        supply_base = _int(supply_v.get("amount"))
        if supply_base is not None and decimals is not None and decimals >= 0:
            supply_ui = supply_base / (10 ** decimals)
        else:
            supply_ui = _num(supply_v.get("uiAmount"))

    amounts: list[int] = []
    if isinstance(largest_v, Sequence) and not isinstance(largest_v, (str, bytes)):
        for acc in largest_v:
            if not isinstance(acc, Mapping):
                continue
            amt = _int(acc.get("amount"))
            if amt is not None:
                amounts.append(amt)
    # The source returns them sorted descending; sorting here is hardening, not
    # trust: the order is the whole meaning of "the largest one".
    amounts.sort(reverse=True)

    if supply_base is None and not amounts:
        return None  # no measurement at all

    row: dict[str, Any] = {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
        "supply": supply_ui,
        "decimals": decimals,
        "top_accounts": len(amounts),
        "raw_json": _dumps(raw_envelope),
    }
    for _n, col in _CHAIN_TIERS:
        row[col] = None
    if supply_base and amounts:              # zero or None ⇒ no ratio
        for n, col in _CHAIN_TIERS:
            row[col] = 100.0 * sum(amounts[:n]) / supply_base
    return row


# ---------------------------------------------------------------------------
# The slow layer: mint authorities, mutability, and creator holdings
#
# Two sources in one batch because neither suffices alone (measured on 48 live
# coins, 2026-08-13): `getAccountInfo` gives `mintAuthority`/`freezeAuthority`
# and the supply but knows nothing of `mutable`; `getAsset` gives `mutable` and
# the creators but no mint authority. And 21 of 48 are on old `spl-token` (no
# metadata extension) and 27 on `spl-token-2022` (the update authority inside
# the mint account) — so the very location of the update authority differs
# between the two programs, and reading it needs both paths.
#
# **A live mint authority = an open minting door** (measured: 3 of 48), and
# **a live freeze authority = whoever holds it can freeze your wallet** (1 of
# 48). Both are rare, and that is exactly what makes them information: the flag
# that rises in 6% of cases discriminates; the one that always rises does not.
# ---------------------------------------------------------------------------
def _mint_info(raw_envelope: Mapping[str, Any]) -> dict[str, Any]:
    """`{program, info}` from a `getAccountInfo` response — or empty if the account is absent."""
    val = raw_envelope.get("mint")
    val = val.get("value") if isinstance(val, Mapping) else None
    if not isinstance(val, Mapping):
        return {}
    data = val.get("data")
    if not isinstance(data, Mapping):
        return {}
    parsed = data.get("parsed")
    info = parsed.get("info") if isinstance(parsed, Mapping) else None
    return {
        "program": data.get("program"),
        "info": info if isinstance(info, Mapping) else {},
    }


def _token2022_update_authority(info: Mapping[str, Any]) -> str | None:
    """The metadata update authority from the `tokenMetadata` extension (Token-2022 only)."""
    exts = info.get("extensions")
    if not isinstance(exts, Sequence) or isinstance(exts, (str, bytes)):
        return None
    for ext in exts:
        if not isinstance(ext, Mapping) or ext.get("extension") != "tokenMetadata":
            continue
        state = ext.get("state")
        if isinstance(state, Mapping):
            ua = state.get("updateAuthority")
            return ua if isinstance(ua, str) and ua else None
    return None


def _das_authority(asset: Mapping[str, Any]) -> str | None:
    """The first authority from `authorities[]` in a DAS response (the old spl-token pattern)."""
    auths = asset.get("authorities")
    if not isinstance(auths, Sequence) or isinstance(auths, (str, bytes)):
        return None
    for a in auths:
        if isinstance(a, Mapping):
            addr = a.get("address")
            if isinstance(addr, str) and addr:
                return addr
    return None


def _das_creators(asset: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    creators = asset.get("creators")
    if isinstance(creators, Sequence) and not isinstance(creators, (str, bytes)):
        for c in creators:
            if isinstance(c, Mapping):
                addr = c.get("address")
                if isinstance(addr, str) and addr:
                    out.append(addr)
    return out


def pick_dev_owner(raw_envelope: Any) -> str | None:
    """The "developer" address whose balance we measure — or `None`, so no second call.

    Priority: a declared creator (`creators[0]`), then the update authority. The
    creator is more truthful but **measured in only 7 of 48** because Token-2022
    always returns `creators: []`; the update authority is a reasonable
    substitute, and that is why `dev_owner` is stored alongside the percentage —
    a number without an owner is not interpretable.
    """
    if not isinstance(raw_envelope, Mapping):
        return None
    asset = raw_envelope.get("asset")
    asset = asset if isinstance(asset, Mapping) else {}
    creators = _das_creators(asset)
    if creators:
        return creators[0]
    mint = _mint_info(raw_envelope)
    ua = _token2022_update_authority(mint.get("info") or {})
    return ua or _das_authority(asset)


def extract_chain_authority(
    raw_envelope: Any,
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
) -> dict[str, Any] | None:
    """A `{mint, asset, owner_accounts?}` envelope → a `chain_authority` row.

    With no valid mint account → `None`: an absent account means the measurement
    never happened, and an all-NULL row would suggest the coin "has no
    authorities" — the most dangerous possible reading.

    `is_mutable` stays `None` if DAS alone failed — not `0`: "we did not
    measure" is not "immutable" (FR-007).
    """
    if not isinstance(raw_envelope, Mapping):
        return None
    mint = _mint_info(raw_envelope)
    info = mint.get("info") or {}
    if not info:
        return None

    asset = raw_envelope.get("asset")
    asset = asset if isinstance(asset, Mapping) else {}

    decimals = _int(info.get("decimals"))
    supply_base = _int(info.get("supply"))
    supply_ui: float | None = None
    if supply_base is not None and decimals is not None and decimals >= 0:
        supply_ui = supply_base / (10 ** decimals)

    creators = _das_creators(asset)
    update_authority = (
        _token2022_update_authority(info) or _das_authority(asset)
    )
    mutable = asset.get("mutable")

    # Developer holdings: the sum of his accounts' balances of this coin ÷ the
    # supply. An address with no token account is a **measured** zero (sold, or
    # never held), not an absence — the distinction here is between "we asked
    # and found no account" and "we never asked at all".
    dev_owner = raw_envelope.get("dev_owner")
    dev_owner = dev_owner if isinstance(dev_owner, str) and dev_owner else None
    dev_pct: float | None = None
    accounts = raw_envelope.get("owner_accounts")
    accounts = accounts.get("value") if isinstance(accounts, Mapping) else accounts
    if dev_owner and isinstance(accounts, Sequence) and not isinstance(accounts, (str, bytes)):
        held = 0
        for acc in accounts:
            if not isinstance(acc, Mapping):
                continue
            parsed = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {})
            amt = _int(((parsed.get("info") or {}).get("tokenAmount") or {}).get("amount"))
            if amt is not None:
                held += amt
        if supply_base:
            dev_pct = 100.0 * held / supply_base

    return {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
        "token_program": mint.get("program"),
        "mint_authority": info.get("mintAuthority") or None,
        "freeze_authority": info.get("freezeAuthority") or None,
        "update_authority": update_authority,
        "is_mutable": None if mutable is None else (1 if mutable else 0),
        "creator_address": creators[0] if creators else None,
        # `0` creators is a valid measurement (Token-2022 really does return
        # `creators: []`), but it counts **only if DAS responded**; if DAS alone
        # failed, the number is unknown, not zero.
        "creator_count": len(creators) if asset else None,
        "supply": supply_ui,
        "decimals": decimals,
        "dev_owner": dev_owner,
        "dev_holding_pct": dev_pct,
        "raw_json": _dumps(raw_envelope),
    }

