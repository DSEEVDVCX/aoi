from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Social/content models for fomo.family feed, thesis, comments and spotlight.
# Every field except stable identifiers is optional (FR-007): absent upstream
# fields map to None rather than being fabricated. Shapes CONFIRMED via live
# network capture + live probe (2026-07-25).


class FeedPost(BaseModel):
    """An item from /feed/tradingActivity responseObject.items[].

    `body` is a structured payload (dict) whose shape varies by `type` — e.g.
    a `multi_user_buy` carries {fdv, price, ticker, marketCap, numTrades,
    topTraders[]}; many types have no body (None). We surface it verbatim
    rather than flattening, so no information is lost or fabricated."""

    id: str
    trader_id: str | None = None
    type: str | None = None
    body: dict[str, Any] | None = None
    likes: int | None = Field(default=None, ge=0)
    views: int | None = Field(default=None, ge=0)
    pinned: bool | None = None
    token_address: str | None = None
    network_id: str | None = None
    trade_id: str | None = None
    timestamp: str | None = None


class ThesisItem(BaseModel):
    """An item from /feed/token/thesis responseObject.items[] — a trader's
    thesis/commentary tweet on a specific coin."""

    id: str
    type: str | None = None
    comment: str | None = None
    num_likes: int | None = Field(default=None, ge=0)
    ticker: str | None = None
    token_address: str | None = None
    network_id: str | None = None
    user_handle: str | None = None
    display_name: str | None = None
    profile_picture_url: str | None = None
    num_replies: int | None = Field(default=None, ge=0)
    equity: float | None = None
    trade_id: str | None = None
    created_at: str | None = None


class TradeComment(BaseModel):
    """A comment on a trade from /trades/{id}/comments responseObject.comments[]."""

    id: str
    trader_id: str | None = None
    comment: str | None = None
    num_likes: int | None = Field(default=None, ge=0)
    parent_id: str | None = None
    token_address: str | None = None
    network_id: str | None = None
    trade_id: str | None = None
    created_at: str | None = None


class SpotlightItem(BaseModel):
    """A single highlighted trade or comment inside a trader's spotlight. The
    `comment` text and its like count come from the nested comment object;
    realized_pnl_usd comes from the nested trade object."""

    user_handle: str | None = None
    display_name: str | None = None
    profile_picture_url: str | None = None
    comment: str | None = None
    num_likes: int | None = Field(default=None, ge=0)
    token_address: str | None = None
    realized_pnl_usd: float | None = None
    trade_id: str | None = None
    created_at: str | None = None


class Spotlight(BaseModel):
    """A trader's spotlight from /v2/users/{id}/spotlight responseObject:
    their best trades and best comments."""

    best_trades: list[SpotlightItem] = Field(default_factory=list)
    best_comments: list[SpotlightItem] = Field(default_factory=list)
