from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ActionType(StrEnum):
    buy = "buy"
    sell = "sell"
    swap = "swap"


class TokenRef(BaseModel):
    # id/symbol may be absent for raw swaps (upstream gives token addresses, and
    # symbol resolution happens elsewhere); address+chain are the stable keys.
    id: str | None = None
    symbol: str | None = None
    chain: str
    address: str | None = None
    price_usd: float | None = Field(default=None, ge=0)


class TraderActivity(BaseModel):
    id: str
    trader_id: str
    action: ActionType
    token: TokenRef
    amount_usd: float | None = Field(default=None, ge=0)
    token_amount: float | None = Field(default=None, ge=0)
    price_usd: float | None = Field(default=None, ge=0)
    chain: str
    timestamp: str
    tx_hash: str | None = None
