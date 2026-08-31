"""Read-only client for Solana RPC (Helius) — the chain layer.

**Read-only** (FR-012): it calls query methods only (`getTokenSupply`,
`getTokenLargestAccounts`, `getAccountInfo`, `getAsset`, `getTokenAccountsByOwner`)
and signs no transaction and sends nothing to the chain.

**The key is never printed nor logged** (FR-013), and that is more dangerous
here than it looks: the Helius key sits in the URL *path* (`?api-key=...`),
and httpx exception messages carry the full URL (`httpx.HTTPStatusError` and
`ConnectError` both do). The recorder writes the exception text to
`meta.last_error_*` and the dashboard displays it ⇒ an unhandled exception =
an exposed key in the database and on screen. So every exception passes
through `_redact()` before being seen, and the original is never re-raised
outside this module.

And the key is **read from disk on every call**, not once at startup: a
long-lived process freezes the startup value and then fails after rotation
with no visible cause — the same lesson as the FOMO session token.
"""
from __future__ import annotations

import asyncio
from typing import Any

import config
import httpx
from provider_keys import KeyPool, read_keys


class ChainKeyMissing(RuntimeError):
    """No key on disk — the layer stops clearly instead of failing with 401 every cycle."""


class ChainRPCError(RuntimeError):
    """A chain call failed. Its message is always **scrubbed** of the key."""


def _read_keys() -> list[str]:
    """All available keys in order: the environment, then the plural, then the old singular.

    (A singular `_read_key()` used to live here before the plural support; it
    was deleted once every call started going through the pool — keeping it
    dead leaves a second read path with different rules, waiting for someone
    to use it by mistake and silently lose rotation. The missing-key message
    moved to `_post`, keeping the file path.)
    """
    return read_keys("helius_api_keys", "helius_api_key", "HELIUS_API_KEY")


def _redact(text: str, key: str) -> str:
    """Scrubs the key from any text before logging or raising it.

    It also scrubs `api-key=<anything>` as a safety net: if the key on disk
    changed between the call and the exception, `key` no longer matches what
    is in the URL, so scrubbing by value alone is not enough.
    """
    out = text.replace(key, "<redacted>") if key else text
    if "api-key=" in out:
        head, _, tail = out.partition("api-key=")
        rest = tail
        for i, ch in enumerate(tail):
            if ch in " \t\"'>)&#":
                rest = tail[i:]
                break
        else:
            rest = ""
        out = head + "api-key=<redacted>" + rest
    return out


class SolanaRPC:
    """Async client for a Solana node. The connection is reused across calls."""

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self._base = url or config.SOLANA_RPC_URL
        self._timeout = timeout if timeout is not None else config.CHAIN_TIMEOUT_SECONDS
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._keys = KeyPool(_read_keys())

    async def aclose(self) -> None:
        await self._client.aclose()

    def key_stats(self) -> dict[str, Any]:
        """A snapshot of the key pool for monitoring — counts only, no values (FR-013).

        Disk is read first so the count reflects what exists **now**, not what
        existed at startup: a new key can be added without a restart, and a
        report frozen on the startup value would still say "one key" after a
        second one is added.
        """
        try:
            self._keys.refresh(_read_keys())
        except Exception:  # noqa: BLE001 — a failed disk read must not sink the report
            pass
        return self._keys.stats()

    async def _step_aside(self, *, rejected: bool) -> None:
        """A rejected key gets rotated; a transient fault backs off on the same key.

        The difference is essential: 401/429 means "this key will not serve
        you now" ⇒ switch keys. But 522 and read timeouts mean "the service
        itself is down" ⇒ rotating keys cools them all down, at no fault of
        theirs, and brings no success. And with a single key there is no
        alternative anyway ⇒ backing off is all we have, and that is what was
        missing.
        """
        if rejected and len(self._keys.keys) > 1:
            self._keys.rotate(block_current=True)
            return
        await asyncio.sleep(float(config.CHAIN_TRANSIENT_BACKOFF_SECONDS))

    async def _post(self, payload: Any) -> Any:
        """One HTTP request (single or batch). Raises a redacted ChainRPCError on failure."""
        self._keys.refresh(_read_keys())
        if not self._keys.keys:
            # The path in the message (it is not a secret): "no keys" without
            # the file location sends the reader hunting for where to put a key.
            raise ChainKeyMissing(
                "no values in helius_api_keys or helius_api_key — file: "
                f"{config.chain_keys_path()}"
            )
        # Attempts are not derived from the key count alone (see `CHAIN_TRANSIENT_RETRIES`).
        attempts = max(
            int(config.CHAIN_TRANSIENT_RETRIES) + 1, len(self._keys.keys),
        )
        for attempt in range(attempts):
            key = self._keys.current()
            url = f"{self._base}?api-key={key}"
            try:
                resp = await self._client.post(url, json=payload)
                status = resp.status_code
                if status in (401, 403, 429):
                    if attempt + 1 < attempts:
                        await self._step_aside(rejected=True)
                        continue
                elif status >= 500 and attempt + 1 < attempts:
                    # 522 is a Cloudflare timeout to the origin: the key is fine, the service is not.
                    await self._step_aside(rejected=False)
                    continue
                if status >= 400:
                    raise ChainRPCError(_redact(f"HTTP {status}: {resp.text[:200]}", key))
                body = resp.json()
                errors = []
                if isinstance(body, dict) and body.get("error") is not None:
                    errors.append(body["error"])
                elif isinstance(body, list):
                    errors.extend(
                        item["error"] for item in body
                        if isinstance(item, dict) and item.get("error") is not None
                    )
                retryable = any(
                    (error.get("code") if isinstance(error, dict) else None)
                    in (-32600, -32603, 429)
                    or any(
                        word in str(
                            error.get("message") if isinstance(error, dict) else error
                        ).lower()
                        for word in ("deprioritized", "overloaded", "rate limit")
                    )
                    for error in errors
                )
                if retryable and attempt + 1 < attempts:
                    # "Slow down requests" is a load signal: with a second key
                    # we try it, with a single key we back off — rather than
                    # passing through doing nothing, as it used to be.
                    await self._step_aside(rejected=True)
                    continue
                return body
            except ChainRPCError:
                raise
            except httpx.TransportError as exc:
                # Read timeout or connection drop: a transient fault, not a broken key.
                if attempt + 1 < attempts:
                    await self._step_aside(rejected=False)
                    continue
                raise ChainRPCError(_redact(f"{type(exc).__name__}: {exc}", key)) from None
            except Exception as exc:  # noqa: BLE001 — no original exception leaves this point
                raise ChainRPCError(_redact(f"{type(exc).__name__}: {exc}", key)) from None
        raise ChainRPCError("call failed after exhausting retries and keys")

    @staticmethod
    def _unwrap(body: Any, method: str) -> Any:
        """Extracts `result`, or raises a JSON-RPC error.

        The source returns 200 with an `error` block (`-32603` and `-32600`
        were seen under contention), so success is not the status code but the
        presence of `result`.
        """
        if not isinstance(body, dict):
            raise ChainRPCError(f"{method}: unexpected response ({type(body).__name__})")
        if "error" in body and body["error"] is not None:
            err = body["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ChainRPCError(f"{method}: JSON-RPC {code}: {str(msg)[:150]}")
        if "result" not in body:
            raise ChainRPCError(f"{method}: neither result nor error in the response")
        return body["result"]

    async def fetch_concentration_raw(self, mint: str) -> dict[str, Any]:
        """Supply + largest accounts in **one HTTP request** via a JSON-RPC batch.

        `getTokenLargestAccounts` gives the amounts but not the total supply,
        and a ratio needs its denominator — so both calls are required. The
        batch makes them a single request (measured: 4 of 4 in one request),
        keeping the cycle's cost at one call per token.

        We deliberately do not take supply from `market_ticks.total_supply`
        (100% populated): it is another source at another cadence, so it may
        measure supply at a moment other than the amounts' — and burns and
        minting really do change supply. Numerator and denominator from the
        same moment, or no ratio at all.
        """
        payload = [
            {"jsonrpc": "2.0", "id": "supply", "method": "getTokenSupply", "params": [mint]},
            {"jsonrpc": "2.0", "id": "largest",
             "method": "getTokenLargestAccounts", "params": [mint]},
        ]
        body = await self._post(payload)
        if not isinstance(body, list):
            raise ChainRPCError(f"batch returned {type(body).__name__}, not a list")
        by_id = {str(item.get("id")): item for item in body if isinstance(item, dict)}
        missing = [k for k in ("supply", "largest") if k not in by_id]
        if missing:
            raise ChainRPCError(f"batch is missing entries: {', '.join(missing)}")
        supply = self._unwrap(by_id["supply"], "getTokenSupply")
        largest = self._unwrap(by_id["largest"], "getTokenLargestAccounts")
        supply_slot = (supply.get("context") or {}).get("slot") if isinstance(supply, dict) else None
        largest_slot = (largest.get("context") or {}).get("slot") if isinstance(largest, dict) else None
        if (isinstance(supply_slot, int) and isinstance(largest_slot, int)
                and abs(supply_slot - largest_slot) > config.CHAIN_MAX_SLOT_LAG):
            raise ChainRPCError(
                "supply and largest-accounts snapshots are not synchronized: "
                f"delta {abs(supply_slot - largest_slot)} slots"
            )
        return {
            "supply": supply,
            "largest": largest,
        }

    async def fetch_authority_raw(self, mint: str) -> dict[str, Any]:
        """Mint account + DAS asset in **one HTTP request** (the slow layer).

        Both sources are required and neither replaces the other — measured
        on 48 live tokens: `getAccountInfo` gives mint authority, freeze, and
        supply from the mint account itself but knows nothing of `mutable`;
        `getAsset` (DAS) gives `mutable` and the creators but not mint
        authority. And 21 of 48 tokens are on legacy `spl-token` with no
        metadata extensions at all, so their mutability can only be read
        from DAS.
        """
        payload = [
            {"jsonrpc": "2.0", "id": "mint", "method": "getAccountInfo",
             "params": [mint, {"encoding": "jsonParsed"}]},
            {"jsonrpc": "2.0", "id": "asset", "method": "getAsset", "params": {"id": mint}},
        ]
        body = await self._post(payload)
        if not isinstance(body, list):
            raise ChainRPCError(f"batch returned {type(body).__name__}, not a list")
        by_id = {str(item.get("id")): item for item in body if isinstance(item, dict)}
        missing = [k for k in ("mint", "asset") if k not in by_id]
        if missing:
            raise ChainRPCError(f"batch is missing entries: {', '.join(missing)}")
        out = {"mint": self._unwrap(by_id["mint"], "getAccountInfo")}
        # DAS may fail alone (an unindexed token) and the mint account alone
        # suffices for the two most important columns — so we do not drop the
        # whole measurement because of it; we save its error in place of the
        # value.
        try:
            out["asset"] = self._unwrap(by_id["asset"], "getAsset")
        except ChainRPCError as exc:
            out["asset"] = None
            out["asset_error"] = str(exc)
        return out

    async def fetch_owner_token_balance_raw(self, owner: str, mint: str) -> Any:
        """A specific address's balance of a specific token — the conditional second call for developer holdings.

        `getTokenAccountsByOwner` with a `mint` filter returns only that
        address's accounts, so it is a cheap call (no chain scan). It cannot
        be merged into the mint batch because the developer's address itself
        is known only from that batch's reply.
        """
        body = await self._post({
            "jsonrpc": "2.0", "id": "owner", "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        })
        return self._unwrap(body, "getTokenAccountsByOwner")
