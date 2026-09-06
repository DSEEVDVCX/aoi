from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

from fomo_api.auth.session import SessionStore, _key
from fomo_api.redis_state import FakeRedis, _state, get_redis

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUITE_DIRS = {"api/tests", "recorder/tests", "dashboard/tests"}
_DISPATCH_ENV = "AOI_ROOT_PYTEST_DISPATCHED"


def _suite_name(argument: str) -> str:
    normalized = argument.replace("\\", "/").rstrip("/")
    try:
        return Path(normalized).resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return normalized.removeprefix("./")


def pytest_cmdline_main(config: pytest.Config) -> int | None:
    """Run mixed flat-import suites in isolated Python processes.

    ``recorder`` and ``dashboard`` both expose a top-level ``config`` module.
    Pytest selects ``api/pyproject.toml`` as the root for a mixed invocation,
    so this conftest is the common hook that is actually loaded. A single suite
    keeps the normal in-process behavior used by the official test runner.
    """
    if os.environ.get(_DISPATCH_ENV) == "1":
        return None
    arguments = list(config.invocation_params.args)
    suites = [_suite_name(arg) for arg in arguments if _suite_name(arg) in _SUITE_DIRS]
    suites = list(dict.fromkeys(suites))
    if len(suites) < 2:
        return None

    forwarded = [arg for arg in arguments if _suite_name(arg) not in _SUITE_DIRS]
    failed = False
    for suite in suites:
        env = os.environ.copy()
        env[_DISPATCH_ENV] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", suite, *forwarded],
            cwd=_REPO_ROOT,
            env=env,
            check=False,
        )
        failed = failed or result.returncode != 0
    return 1 if failed else 0


@pytest.fixture(autouse=True)
def fake_redis() -> FakeRedis:
    fake = FakeRedis()
    _state["redis"] = fake  # type: ignore[assignment]
    from fomo_api.config import settings

    settings.dev = True
    settings.upstream_impersonate = False
    # Tests must never launch a real browser, regardless of the local .env.
    settings.enable_live_login = False
    yield fake
    _state["redis"] = None


@pytest.fixture
def app_client(fake_redis):

    from httpx import ASGITransport, AsyncClient

    from fomo_api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)

    class _ClientFactory:
        def __init__(self) -> None:
            self._client: AsyncClient | None = None

        async def __aenter__(self) -> AsyncClient:
            self._client = AsyncClient(transport=transport, base_url="http://test")
            return self._client

        async def __aexit__(self, *exc) -> None:
            if self._client is not None:
                await self._client.aclose()

    return _ClientFactory()


def make_fomo_json() -> Any:
    def _fomo_json(body: Any, status: int = 200) -> Response:
        return Response(status_code=status, json=body)

    return _fomo_json


@pytest.fixture
def fomo_json():
    return make_fomo_json()


@pytest.fixture
def respx_mock():
    from fomo_api.config import settings

    with respx.mock(base_url=settings.upstream_base, assert_all_called=False) as router:
        yield router


async def seed_session(consumer_key: str = "test-key", token: str = "fomo-session-token") -> str:
    store = SessionStore(get_redis())
    await store.create(token)
    redis = get_redis()
    await redis.setex(_key(consumer_key), 900, token)
    await redis.setex(f"{_key(consumer_key)}:exp", 900, "2099-01-01T00:00:00+00:00")
    return consumer_key


@pytest.fixture
async def authed_client(app_client):
    key = await seed_session()
    async with app_client as client:
        client.headers["Authorization"] = f"Bearer {key}"
        yield client


@pytest.fixture
def trader_payload() -> dict[str, Any]:
    """A real fomo user object (as returned inside responseObject), CONFIRMED
    field names from live capture. No `rank`/`metrics` — those are derived."""
    return {
        "id": "t_1",
        "userHandle": "legend",
        "displayName": "Legend",
        "followers": 1200,
        "following": 30,
        "numTrades": 210,
        "swapCount": 210,
        "totalVolume": 98000.0,
        "totalPnL": 42500.0,
        "address": "0xabc",
        "evmAddress": "0xabc",
    }


@pytest.fixture
def leaderboard_payload(trader_payload) -> dict[str, Any]:
    """Real envelope: {responseObject: {leaderboard: [...]}}."""
    return {
        "success": True,
        "message": "ok",
        "responseObject": {
            "leaderboard": [
                trader_payload,
                {**trader_payload, "id": "t_2", "userHandle": "runner"},
            ]
        },
        "statusCode": 200,
    }
