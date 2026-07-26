from __future__ import annotations

from fastapi import APIRouter, Query

from fomo_api.api.errors import NotFoundError
from fomo_api.clients.deps import FomoClientDep
from fomo_api.models.envelope import Envelope, utcnow
from fomo_api.models.market import TokenBars, TokenMarketDetail, TokenWarnings

router = APIRouter()


@router.get("/tokens/{symbol}/bars", response_model=Envelope[TokenBars])
async def get_token_bars(
    client: FomoClientDep,
    symbol: str,
    resolution: str = Query(default="60"),
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    count_back: int = Query(default=300, ge=1, le=5000, alias="countBack"),
    network_id: int | None = Query(default=None, alias="networkId"),
) -> Envelope[TokenBars]:
    """CONFIRMED: POST /proxy/getBarsNew — historical OHLCV candles for a token.
    This is the price ground-truth used for labelling and paper trading.

    `symbol` is the token address; pass `networkId` (or a pre-joined
    `address:networkId`) — without it upstream answers 502."""
    bars = await client.get_token_bars(
        symbol=symbol,
        resolution=resolution,
        from_ts=from_ts,
        to_ts=to_ts,
        count_back=count_back,
        network_id=network_id,
    )
    if bars is None:
        raise NotFoundError("TokenBars", symbol)
    return Envelope(data=TokenBars(**bars), last_refreshed_at=utcnow())


@router.get("/tokens/{token_id}/details", response_model=Envelope[TokenMarketDetail])
async def get_token_details(
    client: FomoClientDep,
    token_id: str,
    network_id: int | None = Query(default=None, alias="networkId"),
) -> Envelope[TokenMarketDetail]:
    """CONFIRMED: POST /proxy/tokenDetails — liquidity/volume/holders/top-10.
    Pass `networkId` (or a pre-joined `address:networkId`); upstream 502s
    on a bare address."""
    detail = await client.get_token_details(token_id, network_id=network_id)
    if detail is None:
        raise NotFoundError("TokenDetail", token_id)
    return Envelope(data=TokenMarketDetail(**detail), last_refreshed_at=utcnow())


@router.get("/tokens/{address}/warnings", response_model=Envelope[TokenWarnings])
async def get_token_warnings(
    client: FomoClientDep,
    address: str,
    network_id: int = Query(alias="networkId"),
) -> Envelope[TokenWarnings]:
    """CONFIRMED: POST /proxy/tokenWarnings — rug/scam trade gates + warnings."""
    warnings = await client.get_token_warnings(address, network_id)
    if warnings is None:
        raise NotFoundError("TokenWarnings", address)
    return Envelope(data=TokenWarnings(**warnings), last_refreshed_at=utcnow())
