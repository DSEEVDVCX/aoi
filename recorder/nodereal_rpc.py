"""Read-only NodeReal client for BSC holder metrics.

NodeReal returns the exact holder count and the top balances ranked — the two
things ERC-20 does not provide through standard JSON-RPC. The key is read
from the local file every cycle and is never printed or stored in the
response.
"""
from __future__ import annotations

import asyncio
from typing import Any

import config
import httpx
from provider_keys import KeyPool, read_keys


class NodeRealError(RuntimeError):
    """A NodeReal request failed."""


class NodeRealRateLimit(NodeRealError):
    """NodeReal rejected the request due to CUPS or quota."""


def _hex_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value)
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except (TypeError, ValueError):
        return None


def _result_value(result: Any) -> Any:
    """Unwraps the extra envelope some NodeReal endpoints return."""
    if isinstance(result, dict) and set(result) == {"result"}:
        return result["result"]
    return result


def _read_keys() -> list[str]:
    """All NodeReal keys in order: the environment, then the plural, then the old singular.

    (The singular `_read_key()` was deleted: a dead second read path means a
    rotation silently lost by mistake. The file path moved to the
    missing-key message in `_call`.)
    """
    return read_keys("nodereal_api_keys", "nodereal_api_key", "NODEREAL_API_KEY")


class NodeRealRPC:
    """Async HTTP client for the BSC Enhanced API."""

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"user-agent": "fomo-recorder/1.0", "content-type": "application/json"},
        )
        self._keys = KeyPool(_read_keys())

    async def aclose(self) -> None:
        await self._client.aclose()

    def key_stats(self) -> dict[str, Any]:
        """A snapshot of the key pool for monitoring — counts only, no values (FR-013)."""
        if not hasattr(self, "_keys"):
            self._keys = KeyPool(_read_keys())
        try:
            self._keys.refresh(_read_keys())
        except Exception:  # noqa: BLE001 — a failed disk read must not sink the report
            pass
        return self._keys.stats()

    async def _step_aside(self, *, rejected: bool) -> None:
        """A rejected key gets rotated; a down service gets a backoff. See `solana_rpc`."""
        if rejected and len(self._keys.keys) > 1:
            self._keys.rotate(block_current=True)
            return
        await asyncio.sleep(float(config.CHAIN_TRANSIENT_BACKOFF_SECONDS))

    async def _call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        if not hasattr(self, "_keys"):
            self._keys = KeyPool(_read_keys())
        self._keys.refresh(_read_keys())
        if not self._keys.keys:
            raise NodeRealError(f"NodeReal keys missing: {config.chain_keys_path()}")
        # Attempts are not derived from the key count alone (see `CHAIN_TRANSIENT_RETRIES`).
        attempts = max(
            int(config.CHAIN_TRANSIENT_RETRIES) + 1, len(self._keys.keys),
        )
        for attempt in range(attempts):
            key = self._keys.current()
            url = f"https://bsc-mainnet.nodereal.io/v1/{key}"
            try:
                response = await self._client.post(url, json=payload)
                if response.status_code in (401, 403, 429):
                    if attempt + 1 < attempts:
                        await self._step_aside(rejected=True)
                        continue
                elif response.status_code >= 500 and attempt + 1 < attempts:
                    await self._step_aside(rejected=False)   # service fault, not the key
                    continue
                if response.status_code == 429:
                    raise NodeRealRateLimit(f"{method}: HTTP 429")
                if response.status_code >= 400:
                    raise NodeRealError(f"{method}: HTTP {response.status_code}")
                body = response.json()
            except (NodeRealError, NodeRealRateLimit):
                raise
            except httpx.TransportError as exc:
                if attempt + 1 < attempts:
                    await self._step_aside(rejected=False)
                    continue
                raise NodeRealError(f"{method}: {type(exc).__name__}") from None
            except Exception as exc:  # noqa: BLE001
                raise NodeRealError(f"{method}: {type(exc).__name__}") from None
            if not isinstance(body, dict):
                raise NodeRealError(f"{method}: unexpected response")
            error = body.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                message = str(error.get("message") if isinstance(error, dict) else error)
                limited = code in (429, -32005) or "compute units" in message.lower()
                if limited and attempt + 1 < attempts:
                    await self._step_aside(rejected=True)
                    continue
                if limited:
                    raise NodeRealRateLimit(f"{method}: {message[:160]}")
                raise NodeRealError(f"{method}: {message[:160]}")
            if "result" not in body:
                raise NodeRealError(f"{method}: no result")
            return _result_value(body["result"])
        raise NodeRealRateLimit(f"{method}: all NodeReal keys temporarily rejected")

    async def holder_count(self, token: str) -> int | None:
        raw = _result_value(await self._call("nr_getTokenHolderCount", [token.lower()]))
        if raw is None:
            return None  # the source does not know some contracts; a measured absence, not zero.
        value = _hex_int(raw)
        if value is None or value < 0:
            raise NodeRealError("nr_getTokenHolderCount: unparseable count")
        return value

    async def top_holders(self, token: str, top_n: int = 20) -> list[tuple[str, int]]:
        # pageSize=100, pageKey="", topN=20. This asks the source for descending order.
        result = await self._call(
            "nr_getTokenHolders", [token.lower(), "0x64", "", hex(int(top_n))],
        )
        details = result.get("details") if isinstance(result, dict) else None
        if not isinstance(details, list):
            raise NodeRealError("nr_getTokenHolders: details missing")
        out: list[tuple[str, int]] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            address = item.get("accountAddress")
            balance = _hex_int(item.get("tokenBalance"))
            if isinstance(address, str) and balance is not None and balance > 0:
                out.append((address.lower(), balance))
        return out[:top_n]

    async def call(self, token: str, data: str) -> int:
        result = await self._call(
            "eth_call", [{"to": token.lower(), "data": data}, "latest"],
        )
        value = _hex_int(result)
        if value is None or value < 0:
            raise NodeRealError("eth_call: unparseable value")
        return value

    async def total_supply(self, token: str) -> int:
        return await self.call(token, "0x18160ddd")

    async def balance_of(self, token: str, holder: str) -> int:
        address_word = holder.lower().removeprefix("0x").rjust(64, "0")
        return await self.call(token, "0x70a08231" + address_word)
