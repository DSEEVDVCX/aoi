"""Unit tests locking in the CONFIRMED (2026-07-25) real fomo.family API shapes
that fomo_client.py maps. Guards against regressions if the mappers drift back
to the old best-guess field names."""
from __future__ import annotations

from fomo_api.clients.fomo_client import (
    _map_activity,
    _map_alert,
    _map_balance_position,
    _map_bars,
    _map_comment,
    _map_feed_post,
    _map_leaderboard,
    _map_spotlight,
    _map_thesis_item,
    _map_token_market_detail,
    _map_token_warnings,
    _map_trader,
    _map_trending_token,
    _unwrap_obj,
)


def _user(**over):
    base = {
        "id": "u1",
        "userHandle": "alice",
        "displayName": "Alice",
        "followers": 100,
        "numTrades": 12,
        "totalVolume": 5000.0,
        "totalPnL": 1234.5,
        "evmAddress": "0xabc",
        "address": "0xabc",
    }
    base.update(over)
    return base


def test_unwrap_obj_prefers_response_object():
    assert _unwrap_obj({"responseObject": {"x": 1}}) == {"x": 1}
    assert _unwrap_obj({"x": 1}) == {"x": 1}  # bare fallback
    assert _unwrap_obj("nope") == {}


def test_map_trader_uses_real_field_names():
    t = _map_trader(_user())
    assert t is not None
    assert t["id"] == "u1"
    assert t["handle"] == "alice"
    assert t["followers_count"] == 100
    assert t["total_volume_usd"] == 5000.0
    assert t["wallet_address"] == "0xabc"
    # PnL surfaces as realized_pnl_usd; no percentage/win-rate upstream.
    assert t["metrics"]["realized_pnl_usd"] == 1234.5
    assert t["metrics"]["volume_usd"] == 5000.0
    assert t["metrics"]["pnl_pct"] is None
    assert t["metrics"]["win_rate_pct"] is None


def test_map_leaderboard_derives_rank_from_position():
    env = {"responseObject": {"leaderboard": [_user(id="u1"), _user(id="u2")]}}
    mapped = _map_leaderboard(env)
    assert mapped is not None
    assert [t["rank"] for t in mapped["traders"]] == [1, 2]
    assert mapped["total_items"] == 2


def test_map_leaderboard_period_pnl_field():
    # 7d variant carries pnl7d instead of totalPnL.
    env = {"responseObject": {"leaderboard": [_user(totalPnL=None, pnl7d=99.0)]}}
    mapped = _map_leaderboard(env)
    assert mapped["traders"][0]["metrics"]["realized_pnl_usd"] == 99.0


def test_map_activity_maps_swaps_to_out_token():
    env = {
        "responseObject": {
            "swaps": [
                {
                    "id": "s1",
                    "networkId": 8453,
                    "outTokenAddress": "0xtok",
                    "outHumanAmount": "42",
                    "humanUsdAmountOut": "99.5",
                    "createdAt": "2026-07-24T00:00:00Z",
                }
            ]
        }
    }
    mapped = _map_activity(env, trader_id="u1")
    assert mapped is not None
    a = mapped["actions"][0]
    assert a["action"] == "swap"
    assert a["trader_id"] == "u1"
    assert a["token"]["address"] == "0xtok"
    assert a["chain"] == "8453"
    assert a["amount_usd"] == 99.5
    assert a["token_amount"] == 42.0


def test_map_alert_from_feed_item():
    item = {
        "id": "f1",
        "userId": "u1",
        "tokenAddress": "0xtok",
        "networkId": 56,
        "createdAt": "2026-07-24T00:00:00Z",
        "type": "trade",
    }
    m = _map_alert(item)
    assert m is not None
    assert m["id"] == "f1"
    assert m["trader_id"] == "u1"
    assert m["token"]["address"] == "0xtok"
    assert m["token"]["chain"] == "56"


def test_map_trader_enriches_social_fields():
    t = _map_trader(
        _user(
            twitter="alice_x",
            description="gm",
            averageHoldTimeSeconds=3600,
            following=42,
            createdAt="2024-01-01T00:00:00Z",
            profilePictureLink="https://img/1.png",
            private=False,
        )
    )
    assert t["twitter"] == "alice_x"
    assert t["description"] == "gm"
    assert t["avg_hold_time_seconds"] == 3600
    assert t["following"] == 42
    assert t["created_at"] == "2024-01-01T00:00:00Z"
    assert t["profile_picture_url"] == "https://img/1.png"
    assert t["is_private"] is False


def test_map_trader_social_fields_absent_are_none():
    # FR-007: absent upstream fields must be None, never fabricated.
    t = _map_trader(_user())
    assert t["twitter"] is None
    assert t["description"] is None
    assert t["avg_hold_time_seconds"] is None
    assert t["following"] is None
    assert t["profile_picture_url"] is None
    assert t["is_private"] is None


def test_map_feed_post_preserves_body_dict_and_meta():
    item = {
        "id": "p1",
        "userId": "u1",
        "type": "multi_user_buy",
        "body": {"ticker": "TA", "fdv": 860136.12, "numTrades": 4462},
        "likes": 10,
        "views": 200,
        "pinned": True,
        "tokenAddress": "0xtok",
        "networkId": 56,
        "tradeId": "t1",
        "createdAt": "2026-07-24T00:00:00Z",
    }
    m = _map_feed_post(item)
    assert m is not None
    assert m["id"] == "p1"
    assert m["trader_id"] == "u1"
    assert m["type"] == "multi_user_buy"
    assert m["body"] == {"ticker": "TA", "fdv": 860136.12, "numTrades": 4462}
    assert m["likes"] == 10
    assert m["views"] == 200
    assert m["pinned"] is True
    assert m["network_id"] == "56"
    assert m["trade_id"] == "t1"


def test_map_feed_post_scalar_body_dropped_not_stringified():
    # Some items have body=None; a non-dict body must never leak as a string.
    m = _map_feed_post({"id": "p2", "userId": "u1", "type": "swap_sell", "body": None})
    assert m["body"] is None


def test_map_thesis_item_extracts_nested_comment_text():
    item = {
        "id": "th1",
        "type": "thesis",
        "comment": {"comment": "strong fundamentals", "numLikes": 9},
        "ticker": "DOGE",
        "tokenAddress": "0xtok",
        "networkId": 56,
        "userHandle": "alice",
        "displayName": "Alice",
        "numReplies": 3,
        "equity": 1500.0,
        "tradeId": "t1",
        "createdAt": "2026-07-24T00:00:00Z",
    }
    m = _map_thesis_item(item)
    assert m is not None
    assert m["comment"] == "strong fundamentals"  # extracted from nested object
    assert m["num_likes"] == 9
    assert m["ticker"] == "DOGE"
    assert m["user_handle"] == "alice"
    assert m["num_replies"] == 3
    assert m["equity"] == 1500.0
    assert m["network_id"] == "56"


def test_map_comment_maps_num_likes():
    item = {
        "id": "c1",
        "userId": "u1",
        "tradeId": "t1",
        "comment": "nice trade",
        "numLikes": 5,
        "parentId": None,
        "tokenAddress": "0xtok",
        "networkId": 56,
        "createdAt": "2026-07-24T00:00:00Z",
    }
    m = _map_comment(item)
    assert m is not None
    assert m["comment"] == "nice trade"
    assert m["num_likes"] == 5
    assert m["trader_id"] == "u1"
    assert m["trade_id"] == "t1"
    assert m["parent_id"] is None


def test_map_spotlight_extracts_nested_comment_and_trade():
    env = {
        "responseObject": {
            "bestTrades": [
                {
                    "userHandle": "alice",
                    "displayName": "Alice",
                    "profilePictureLink": "https://img/a.png",
                    "trade": {
                        "id": "t1",
                        "realizedPnlUsd": 1234.5,
                        "tokenAddress": "0xtok",
                    },
                    "comment": {
                        "id": "c1",
                        "comment": "chop chop chop",
                        "numLikes": 257,
                        "tokenAddress": "0xtok",
                        "createdAt": "2026-03-02T23:16:56Z",
                    },
                }
            ],
            "bestComments": [
                {
                    "userHandle": "bob",
                    "comment": {"comment": "great call", "numLikes": 12},
                    "trade": {"id": "t2"},
                }
            ],
        }
    }
    m = _map_spotlight(env)
    assert len(m["best_trades"]) == 1
    bt = m["best_trades"][0]
    assert bt["trade_id"] == "t1"
    assert bt["comment"] == "chop chop chop"  # extracted from nested object
    assert bt["num_likes"] == 257
    assert bt["realized_pnl_usd"] == 1234.5
    assert bt["token_address"] == "0xtok"
    assert len(m["best_comments"]) == 1
    assert m["best_comments"][0]["comment"] == "great call"
    assert m["best_comments"][0]["trade_id"] == "t2"
    assert m["best_comments"][0]["user_handle"] == "bob"


def test_map_spotlight_empty_when_absent():
    m = _map_spotlight({"responseObject": {}})
    assert m == {"best_trades": [], "best_comments": []}


def _trending_item(**over):
    base = {
        "change": 12.5,
        "liquidity": 50000.0,
        "marketCap": 860136.12,
        "priceUSD": 0.0042,
        "volume": 12345.0,
        "token": {
            "address": "0xtok",
            "decimals": 9,
            "networkId": 56,
            "name": "Test Token",
            "symbol": "TT",
            "isScam": False,
            "launchpad": "pump",
        },
    }
    base.update(over)
    return base


def test_map_trending_token_flattens_nested_token_and_market():
    m = _map_trending_token(_trending_item())
    assert m is not None
    assert m["address"] == "0xtok"
    assert m["name"] == "Test Token"
    assert m["symbol"] == "TT"
    assert m["chain"] == "56"
    assert m["decimals"] == 9
    assert m["is_scam"] is False  # falsy bool preserved
    assert m["launchpad"] == "pump"
    assert m["price_usd"] == 0.0042
    assert m["change_pct"] == 12.5
    assert m["liquidity_usd"] == 50000.0
    assert m["market_cap_usd"] == 860136.12
    assert m["volume_usd"] == 12345.0


def test_map_trending_token_requires_address():
    # No token.address and no top-level address -> dropped (FR-007, no fabrication).
    assert _map_trending_token({"token": {"symbol": "X"}, "change": 1.0}) is None
    assert _map_trending_token("nope") is None


def test_map_trending_token_absent_fields_are_none():
    m = _map_trending_token({"token": {"address": "0xtok"}})
    assert m["symbol"] is None
    assert m["price_usd"] is None
    assert m["change_pct"] is None
    assert m["is_scam"] is None


def test_map_balance_position_reads_nested_balance_shape():
    item = {
        "balance": {
            "tokenAddress": "9cRCn9rGT8pump",
            "shiftedBalance": 311302.022772,
            "tokenId": "9cRCn9rGT8pump:1399811149",
        },
        "userToken": {
            "networkId": 1399811149,
            "humanAmountRemaining": 311302.022772,
            "averageEntryPriceUsd": 0.1825,
            "currentRealizedPnlUsd": 67.79,
            "totalRealizedPnlUsd": -94.56,
            "currentCostBasisUsd": 61732.25,
            "holdingSince": "2026-07-23T22:07:37.724Z",
        },
        "tokenFilterResult": {
            "priceUSD": 0.2,
            "marketCap": 5000000.0,
            "token": {"name": "Pump Coin", "symbol": "PUMP", "decimals": 6},
        },
    }
    m = _map_balance_position(item)
    assert m is not None
    assert m["token"]["address"] == "9cRCn9rGT8pump"
    assert m["token"]["symbol"] == "PUMP"
    assert m["token"]["decimals"] == 6
    assert m["token"]["network_id"] == "1399811149"
    assert m["token_amount"] == 311302.022772
    assert m["avg_entry_price"] == 0.1825
    assert m["current_price_usd"] == 0.2
    assert m["realized_pnl_usd"] == 67.79
    assert m["total_realized_pnl_usd"] == -94.56
    assert m["market_cap_usd"] == 5000000.0
    assert m["opened_at"] == "2026-07-23T22:07:37.724Z"
    # amount_usd derived = token_amount * price
    assert abs(m["amount_usd"] - 311302.022772 * 0.2) < 1e-6


def test_map_balance_position_requires_address():
    assert _map_balance_position({"userToken": {"networkId": 1}}) is None
    assert _map_balance_position("nope") is None


def test_map_bars_parses_ohlcv_arrays():
    env = {
        "responseObject": {
            "s": "ok",
            "t": [1700000000, 1700003600],
            "o": [1.0, 1.1],
            "h": [1.5, 1.6],
            "l": [0.9, 1.0],
            "c": [1.2, 1.3],
            "v": [1000.0, 2000.0],
        }
    }
    m = _map_bars(env, symbol="0xtok", resolution="60")
    assert m is not None
    assert m["symbol"] == "0xtok"
    assert m["resolution"] == "60"
    assert m["status"] == "ok"
    assert m["t"] == [1700000000, 1700003600]
    assert m["o"] == [1.0, 1.1]
    assert m["c"] == [1.2, 1.3]
    assert m["v"] == [1000.0, 2000.0]


def test_map_bars_missing_arrays_become_empty():
    m = _map_bars({"responseObject": {"s": "no_data"}}, symbol="0xtok", resolution="60")
    assert m["status"] == "no_data"
    assert m["t"] == []
    assert m["c"] == []


def test_map_token_market_detail_extracts_volume_windows():
    env = {
        "responseObject": {
            "buyCount": 100,
            "sellCount": 40,
            "holders": 1234,
            "top10HoldersPercent": 22.5,
            "isLowFees": True,
            "volume": {"5m": 1000.0, "1": 5000.0, "4": 20000.0, "24": 90000.0},
        }
    }
    m = _map_token_market_detail(env, token_id="0xtok")
    assert m is not None
    assert m["token_id"] == "0xtok"
    assert m["buy_count"] == 100
    assert m["sell_count"] == 40
    assert m["holders"] == 1234
    assert m["top10_holders_pct"] == 22.5
    assert m["is_low_fees"] is True
    assert m["volume_5m_usd"] == 1000.0
    assert m["volume_1h_usd"] == 5000.0
    assert m["volume_4h_usd"] == 20000.0
    assert m["volume_24h_usd"] == 90000.0


def test_map_token_market_detail_absent_volume_is_none():
    m = _map_token_market_detail({"responseObject": {"holders": 5}}, token_id="0xtok")
    assert m["holders"] == 5
    assert m["volume_5m_usd"] is None
    assert m["buy_count"] is None


def test_map_token_warnings_surfaces_gates_and_list():
    env = {
        "responseObject": {
            "disableBuying": False,
            "disableSelling": True,
            "warnings": [{"type": "honeypot", "severity": "high"}],
        }
    }
    m = _map_token_warnings(env, address="0xtok", network_id=56)
    assert m is not None
    assert m["address"] == "0xtok"
    assert m["network_id"] == "56"
    assert m["disable_buying"] is False  # falsy bool preserved
    assert m["disable_selling"] is True
    assert m["warnings"] == [{"type": "honeypot", "severity": "high"}]


def test_map_token_warnings_missing_warnings_is_empty_list():
    m = _map_token_warnings({"responseObject": {}}, address="0xtok", network_id=1)
    assert m["warnings"] == []
    assert m["disable_buying"] is None


# --- token pair ids (address:networkId) ---
def test_pair_id_joins_address_and_network():
    """Regression: a bare address makes fomo's backend answer Cloudflare 502,
    which reads as an upstream outage but is really a malformed request."""
    from fomo_api.clients.fomo_client import _pair_id

    assert _pair_id("0xabc", 56) == "0xabc:56"
    assert _pair_id("0xabc", "1399811149") == "0xabc:1399811149"
    # Already joined, or no network available -> unchanged (no double suffix).
    assert _pair_id("0xabc:56", 56) == "0xabc:56"
    assert _pair_id("0xabc", None) == "0xabc"


async def test_get_token_bars_sends_joined_symbol(respx_mock, fomo_json):
    import json as _json

    from fomo_api.clients.fomo_client import FomoClient

    route = respx_mock.post("/proxy/getBarsNew").mock(
        return_value=fomo_json({"responseObject": {"s": "ok", "t": [1], "c": [2.0]}})
    )
    client = FomoClient("tok")
    await client.get_token_bars(symbol="0xabc", network_id=56)
    await client.aclose()

    assert _json.loads(route.calls[0].request.content)["symbol"] == "0xabc:56"


async def test_get_token_details_sends_joined_token_id(respx_mock, fomo_json):
    import json as _json

    from fomo_api.clients.fomo_client import FomoClient

    route = respx_mock.post("/proxy/tokenDetails").mock(
        return_value=fomo_json({"responseObject": {"holders": 10}})
    )
    client = FomoClient("tok")
    detail = await client.get_token_details("0xabc", network_id=56)
    await client.aclose()

    assert _json.loads(route.calls[0].request.content)["tokenId"] == "0xabc:56"
    assert detail["token_id"] == "0xabc:56"
