from __future__ import annotations

import pytest
from pydantic import ValidationError

from fomo_api.models.activity import ActionType, TraderActivity
from fomo_api.models.envelope import make_page
from fomo_api.models.feed import FeedPost, Spotlight, ThesisItem, TradeComment
from fomo_api.models.market import (
    TokenBars,
    TokenMarketDetail,
    TokenWarnings,
    TrendingToken,
)
from fomo_api.models.trader import Trader, TraderMetrics


def test_page_size_bounds():
    p = make_page(1, 20, 500)
    assert p.page == 1
    assert p.page_size == 20
    assert p.total_items == 500
    assert p.total_pages == 25


def test_page_size_bounds_zero_total():
    p = make_page(1, 20, 0)
    assert p.total_pages == 1


def test_trader_metrics_nullable():
    m = TraderMetrics()
    assert m.pnl_pct is None
    assert m.win_rate_pct is None
    assert m.volume_usd is None


def test_trader_metrics_win_rate_bounds():
    with pytest.raises(ValidationError):
        TraderMetrics(win_rate_pct=150.0)


def test_trader_rank_must_be_positive():
    with pytest.raises(ValidationError):
        Trader(id="t_1", handle="x", rank=0, followers_count=0, metrics=TraderMetrics())


def test_action_type_enum():
    assert ActionType("buy") == ActionType.buy
    assert ActionType("sell") == ActionType.sell


def test_trader_social_fields_optional():
    # New social/profile fields all default to None (FR-007: no fabrication).
    t = Trader(id="t_1", handle="x", followers_count=0, metrics=TraderMetrics())
    assert t.twitter is None
    assert t.description is None
    assert t.avg_hold_time_seconds is None
    assert t.following is None
    assert t.created_at is None
    assert t.profile_picture_url is None
    assert t.is_private is None
    # And they round-trip when present.
    t2 = Trader(
        id="t_2",
        handle="y",
        followers_count=0,
        metrics=TraderMetrics(),
        twitter="y_x",
        description="bio",
        is_private=True,
    )
    assert t2.twitter == "y_x"
    assert t2.is_private is True


def test_feed_models_minimal_required_fields():
    # Only the stable id is required; everything else is optional.
    assert FeedPost(id="p1").body is None
    assert ThesisItem(id="th1").comment is None
    assert TradeComment(id="c1").num_likes is None
    sp = Spotlight()
    assert sp.best_trades == []
    assert sp.best_comments == []


def test_market_models_minimal_required_fields():
    # Only the stable identifier is required; market fields default to None/[].
    t = TrendingToken(address="0xtok")
    assert t.symbol is None
    assert t.price_usd is None
    b = TokenBars(symbol="0xtok")
    assert b.t == []
    assert b.c == []
    assert b.status is None
    d = TokenMarketDetail(token_id="0xtok")
    assert d.holders is None
    assert d.volume_24h_usd is None
    w = TokenWarnings(address="0xtok")
    assert w.warnings == []
    assert w.disable_buying is None


def test_token_bars_arrays_round_trip():
    b = TokenBars(symbol="0xtok", resolution="60", status="ok", t=[1, 2], c=[1.1, 1.2])
    assert b.t == [1, 2]
    assert b.c == [1.1, 1.2]
    assert b.resolution == "60"


def test_trader_activity_amount_non_negative():
    a = TraderActivity(
        id="a_1",
        trader_id="t_1",
        action=ActionType.buy,
        token={"id": "x", "symbol": "P", "chain": "sol"},
        amount_usd=None,
        chain="sol",
        timestamp="2026-07-23T19:39:00Z",
    )
    assert a.amount_usd is None
    with pytest.raises(ValidationError):
        TraderActivity(
            id="a_2",
            trader_id="t_1",
            action=ActionType.buy,
            token={"id": "x", "symbol": "P", "chain": "sol"},
            amount_usd=-1.0,
            chain="sol",
            timestamp="2026-07-23T19:39:00Z",
        )
