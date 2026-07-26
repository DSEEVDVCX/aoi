from __future__ import annotations

from pydantic import BaseModel, Field


class Token(BaseModel):
    id: str
    name: str
    symbol: str
    chain: str
    price_usd: float | None = Field(default=None, ge=0)
    volume_24h_usd: float | None = Field(default=None, ge=0)
    liquidity_usd: float | None = Field(default=None, ge=0)
    market_cap_usd: float | None = Field(default=None, ge=0)
