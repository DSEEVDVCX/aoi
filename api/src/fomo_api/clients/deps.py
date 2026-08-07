from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends

from fomo_api.auth.deps import get_session_store, require_consumer_key
from fomo_api.auth.session import SessionStore
from fomo_api.clients.fomo_client import FomoClient


async def get_fomo_client(session_store: SessionStore, consumer_key: str) -> FomoClient:
    token = await session_store.verify(consumer_key)
    if token is None:
        from fomo_api.api.errors import UnauthorizedError

        raise UnauthorizedError()
    return FomoClient(token)


async def fomo_client_dep(
    consumer_key: Annotated[str, Depends(require_consumer_key)],
    store: SessionStore = Depends(get_session_store),
) -> AsyncIterator[FomoClient]:
    client = await get_fomo_client(store, consumer_key)
    try:
        yield client
    finally:
        await client.aclose()


FomoClientDep = Annotated[FomoClient, Depends(fomo_client_dep)]
