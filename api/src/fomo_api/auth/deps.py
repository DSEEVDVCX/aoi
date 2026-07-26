from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from fomo_api.api.errors import UnauthorizedError
from fomo_api.auth.session import SessionStore
from fomo_api.redis_state import get_redis


async def get_session_store() -> SessionStore:
    return SessionStore(get_redis())


async def require_consumer_key(
    authorization: Annotated[str | None, Header()] = None,
    store: SessionStore = Depends(get_session_store),
) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError()
    key = authorization.split(" ", 1)[1].strip()
    token = await store.verify(key)
    if token is None:
        raise UnauthorizedError()
    await store.touch(key)
    return key


ConsumerKey = Annotated[str, Depends(require_consumer_key)]
SessionStoreDep = Annotated[SessionStore, Depends(get_session_store)]
