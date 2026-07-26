from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Token market-data models for fomo.family /proxy/* endpoints. Every field
# except stable identifiers is optional (FR-007): absent upstream fields map to
# None rather than being fabricated. Shapes CONFIRMED via live probe (2026-07-25).


class TrendingToken(BaseModel):
    """A token from POST /proxy/{trendingTokens,mostHeld} or GET /proxy/verifiedTokens.
    Market fields are top-level; identity (address/name/symbol/networkId) is nested
    under `token` upstream and flattened here."""

    address: str
    name: str | None = None
    symbol: str | None = None
    chain: str | None = None
    decimals: int | None = None
    is_scam: bool | None = None
    launchpad: str | None = None
    price_usd: float | None = None
    change_pct: float | None = None
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    volume_usd: float | None = None


class TokenBars(BaseModel):
    """Historical OHLCV series from POST /proxy/getBarsNew (TradingView shape).
    The parallel arrays t/o/h/l/c/v are index-aligned; `status` is upstream's
    `s` field ("ok"/"no_data"). This is the ground-truth price history."""

    symbol: str
    resolution: str | None = None
    status: str | None = None
    t: list[int] = Field(default_factory=list)
    o: list[float] = Field(default_factory=list)
    h: list[float] = Field(default_factory=list)
    l: list[float] = Field(default_factory=list)  # noqa: E741 — upstream's field name (low)
    c: list[float] = Field(default_factory=list)
    v: list[float] = Field(default_factory=list)


class TokenMarketDetail(BaseModel):
    """Liquidity/volume/holder detail from POST /proxy/tokenDetails."""

    token_id: str
    buy_count: int | None = None
    sell_count: int | None = None
    holders: int | None = None
    top10_holders_pct: float | None = None
    is_low_fees: bool | None = None
    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_4h_usd: float | None = None
    volume_24h_usd: float | None = None


class TokenWarnings(BaseModel):
    """Rug/scam trade gates from POST /proxy/tokenWarnings. `warnings` is the raw
    upstream list surfaced verbatim (shape varies), so no signal is lost."""

    address: str
    network_id: str | None = None
    disable_buying: bool | None = None
    disable_selling: bool | None = None
    warnings: list[Any] = Field(default_factory=list)
