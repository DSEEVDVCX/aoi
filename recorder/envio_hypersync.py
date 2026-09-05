"""Envio HyperSync: the Base backfill's dedicated historical-log source.

**Why this module exists.** The Base queue exists because the public node caps
`eth_getLogs` at a 10,000-block range, and a token's full history is millions of
blocks — thousands of capped calls per token, weeks of wall clock for the
watchlist. Measured 2026-09-04: HyperSync answered the **complete** transfer
history of a queue token (31,215 logs, 239 pages) in 185 seconds where the
public-RPC ledger had recorded `transfers=0` after weeks of trying. And the
other two candidate providers were measured on the same day and are **not**
routed here: Alchemy was `range_limited` at the same public caps on all four
networks, and dRPC matched the public 10K on Base without exceeding it — a
route is earned by a measurement, not by a hope (`probe_evm_providers.py`).

**What it is not:** JSON-RPC. It is an indexed query API — `POST /query` with
`from_block`/`to_block`, a log filter and field selection, answered with a page
of logs plus `next_block` for pagination. So it cannot replace an RPC URL; it
implements the one method the backfill needs, `get_logs_paged`, in the same
shape `EVMRPC` returns — (logs, requests, complete, resume) — so `evm_layer`
uses it through the same contract, and only for **backfill**. Live apply stays
on the public node: it needs `eth_blockNumber` anchoring and multi-address
filters that HyperSync is not the tool for, and mixing the two would put a
second, differently-shaped path on the one range that must never be applied
twice.

**Secrets:** the key is read from `chain_keys.json` per call (the same
disk-every-time rule as the session token and `solana_rpc`) and travels in the
`Authorization: Bearer` header — never in the URL, never in a message: every
error string passes through `_hide`, which strips the key before the text
leaves this module (FR-013). A missing or rejected key is **not** an error:
the adapter reports itself unavailable and the backfill falls back to the
public node, so a key problem degrades Base to yesterday's speed instead of
stopping the ledger. No key ⇒ the module was never enabled for that cycle.

Field names are HyperSync's own — unprefixed (`block_number`, `topic0`,
`data`), not JSON-RPC's camelCase — and the values are decimal, not hex. The
translation happens once, here, and the logs leave as ordinary JSON-RPC-shaped
`Transfer` records so `evm_rpc.decode_transfer` decodes them unchanged.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import config
import httpx
from evm_rpc import TRANSFER_TOPIC
from provider_keys import KeyPool, read_keys

# HyperSync serves queries from `https://<network>.hypersync.xyz/query`. A
# network joins this map after a probe, not before:
# - 8453 (Base, 2026-09-04): the complete transfer history of a queue token
#   (31,215 logs, 239 pages) in 185s where the public node had weeks of
#   `transfers=0`.
# - 4663 (Robinhood, 2026-09-04): `robinhood.hypersync.xyz` answered with the
#   same shape and an archive ahead of our head (54,500,138 vs 54,498,936),
#   and the queue's biggest token walked 64,090 rows in 60 pages — the same
#   disease Base had, measured on the same day.
HYPERSYNC_URLS = {
    "8453": "https://base.hypersync.xyz/query",
    "4663": "https://robinhood.hypersync.xyz/query",
}

# One page is bounded by the service, not by us; this is the largest observed in
# the 239-page walk (2026-09-04), with margin. Requests, not logs, are the cost.
_PAGE_LOGS = 300

# How many seconds one query request may take before the backfill's deadline
# should take over. The measured p95 was well under 2s; this is a guard, not a pace.
_TIMEOUT_SECONDS = 30.0


def _read_keys() -> list[str]:
    """The envio key pool — env var first, then `chain_keys.json` (FR-013: value never printed)."""
    return read_keys("envio_api_keys", "envio_api_key", "ENVIO_API_KEY")


class EnvioUnavailable(RuntimeError):
    """HyperSync is not usable (no key, or the key is rejected) ⇒ fall back to public RPC."""


class EnvioHyperSync:
    """Backfill source with the `EVMRPC.get_logs_paged` contract, over the query API.

    One instance per process, like `EVMRPC` itself. The key pool is refreshed
    from disk on every call — a rotated key takes effect on the next query with
    no restart, the same lesson as the recorder's session token.
    """

    def __init__(
        self,
        urls: dict[str, str] | None = None,
        timeout: float | None = None,
        keys: list[str] | None = None,
    ) -> None:
        self._urls = dict(urls if urls is not None else HYPERSYNC_URLS)
        self._timeout = timeout if timeout is not None else _TIMEOUT_SECONDS
        # `keys` given ⇒ a test or an explicit override: the pool is fixed and
        # disk is never consulted (refreshing would silently replace the
        # caller's keys with the file's). `keys=None` ⇒ the real path, whose
        # pool is refreshed from disk on every call — a rotated key takes
        # effect on the next query with no restart.
        self._fixed_keys = keys is not None
        self._keys = KeyPool(keys if keys is not None else _read_keys())
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def covers(self, network_id: str) -> bool:
        """True when this network is on HyperSync **and** a key exists — routing, not hope."""
        return str(network_id) in self._urls and bool(self._keys.keys)

    def key_stats(self) -> dict[str, Any]:
        """Pool snapshot for the meta report — counts only, no values (FR-013)."""
        if not self._fixed_keys:
            try:
                self._keys.refresh(_read_keys())
            except Exception:  # noqa: BLE001 — a failed disk read must not sink the report
                pass
        return self._keys.stats()

    def _hide(self, text: str) -> str:
        """Strips every configured key from the text before it can reach a log or an exception."""
        out = text
        for key in self._keys.keys:
            out = out.replace(key, "<redacted>")
        return " ".join(out.split())[:200]

    async def _query(
        self, network_id: str, body: dict[str, Any], *, _retries: int = 0,
    ) -> dict[str, Any]:
        """One POST /query. Raises `EnvioUnavailable` on auth trouble (fallback, not error);
        a redacted `EVMRPCError`-compatible raise on anything the caller should treat as retry.
        """
        if not self._fixed_keys:
            self._keys.refresh(_read_keys())
        if not self._keys.keys:
            raise EnvioUnavailable("no envio key in chain_keys.json")
        url = self._urls[str(network_id)]
        key = self._keys.current()
        try:
            resp = await self._client.post(
                url, json=body, headers={"authorization": f"Bearer {key}"},
            )
        except httpx.TransportError as exc:
            # A wait, not a failure: raising rate-limit-shaped lets the caller's
            # existing backoff handle it (the `EVMRateLimit` remedy).
            from evm_rpc import EVMRateLimit  # local import keeps the module import-light

            raise EVMRateLimit(
                f"hypersync [{network_id}] {type(exc).__name__}",
            ) from None
        if resp.status_code in (401, 403):
            # The key is the problem: rotate once; if the next key also fails,
            # unavailability is the honest verdict ⇒ the public node continues.
            if len(self._keys.keys) > 1:
                self._keys.rotate(block_current=True)
                return await self._query(network_id, body)
            raise EnvioUnavailable(f"hypersync [{network_id}] HTTP {resp.status_code}")
        if resp.status_code == 429:
            # A quota answer, not a broken service. The second key on a second
            # account is exactly what the pool is for (added 2026-09-05 for the
            # Robinhood drain): the cooled key rotates out and the request rides
            # the other account's quota. Each key is tried at most once per
            # request — when they have all said 429 the honest answer is the
            # raise, and the caller's backoff does the waiting.
            if _retries < len(self._keys.keys) - 1:
                self._keys.rotate(block_current=True)
                return await self._query(network_id, body, _retries=_retries + 1)
            from evm_rpc import EVMRateLimit

            raise EVMRateLimit(
                f"hypersync [{network_id}] HTTP 429: "
                f"{self._hide(resp.text[:120])}",
            )
        if resp.status_code >= 500:
            from evm_rpc import EVMRateLimit

            raise EVMRateLimit(
                f"hypersync [{network_id}] HTTP {resp.status_code}: "
                f"{self._hide(resp.text[:120])}",
            )
        if resp.status_code >= 400:
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{network_id}] HTTP {resp.status_code}: "
                f"{self._hide(resp.text[:120])}",
            )
        try:
            parsed = resp.json()
        except ValueError:
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{network_id}]: non-JSON response",
            ) from None
        if not isinstance(parsed, dict):
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{network_id}]: unexpected response shape",
            )
        return parsed

    @staticmethod
    def _to_rpc_log(entry: dict[str, Any]) -> dict[str, Any] | None:
        """A HyperSync log row → a JSON-RPC-shaped record `decode_transfer` can read.

        Decimal in, hex out: the rest of the pipeline speaks JSON-RPC, and this
        is the one place the translation belongs. `None` on a malformed row —
        ignored, never guessed at, the same rule as `decode_transfer` itself.
        """
        try:
            block = int(entry["block_number"])
            log_index = int(entry.get("log_index") or 0)
            tx_index = int(entry.get("transaction_index") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        topics = []
        for field in ("topic0", "topic1", "topic2", "topic3"):
            raw = entry.get(field)
            if raw in (None, ""):
                break
            text = str(raw)
            if not text.startswith("0x"):
                text = "0x" + text
            topics.append(text)
        data = str(entry.get("data") or "")
        if data and not data.startswith("0x"):
            data = "0x" + data
        return {
            "address": entry.get("address"),
            "topics": topics,
            "data": data or "0x",
            "blockNumber": hex(block),
            "transactionIndex": hex(tx_index),
            "logIndex": hex(log_index),
        }

    async def get_logs_paged(
        self,
        network_id: str,
        addresses: Sequence[str],
        from_block: int,
        to_block: int,
        topics: Sequence[Any] | None = None,
        max_calls: int | None = None,
        sleep=asyncio.sleep,
        deadline: float | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool, int]:
        """The `EVMRPC.get_logs_paged` contract over paginated HyperSync queries.

        Pagination is the service's (`next_block`), not ours: each page ends at
        `next_block - 1`, and the next query starts there — no gap, no overlap,
        the same never-broken contract as the RPC version (every returned log's
        block is below the resume point). Only the standard single-topic
        Transfer filter is supported; the backfill is the sole caller and it
        never passes anything else (a wrong shape fails loudly, not silently).

        `max_calls` counts **requests**, matching the RPC version's quota
        semantics. Hitting it exits short with the resume point — the next
        cycle continues from there.
        """
        net = str(network_id)
        if net not in self._urls:
            raise EnvioUnavailable(f"hypersync does not serve network {net}")
        cap = max(1, max_calls) if max_calls is not None else 10 ** 9
        lo = int(from_block)
        hi = int(to_block)
        # One address per query — the backfill always calls with exactly one,
        # and a multi-address filter over HyperSync would need OR-semantics the
        # measured path never exercised. Enforced, not assumed.
        if len(addresses) != 1:
            raise EnvioUnavailable(
                "hypersync path serves the single-token backfill only",
            )
        token = addresses[0].lower()
        topic0 = (
            str(topics[0]).lower()
            if topics and len(topics) >= 1 and topics[0] is not None
            else TRANSFER_TOPIC
        )
        out: list[dict[str, Any]] = []
        requests = 0
        while lo <= hi:
            spent = (
                requests > 0 and deadline is not None and time.monotonic() >= deadline
            )
            if requests >= cap or spent:
                # What is unread is everything from `lo` up ⇒ honest resume point.
                return out, requests, False, lo
            if requests:
                await sleep(config.EVM_HYPERSYNC_PACING_SECONDS)
            body = {
                "from_block": lo,
                "to_block": hi,
                "logs": [{"address": [token], "topics": [[topic0]]}],
                "field_selection": {"log": [
                    "transaction_hash", "block_number", "transaction_index",
                    "log_index", "address", "data", "topic0", "topic1", "topic2",
                ]},
            }
            page = await self._query(net, body)
            requests += 1
            # The live response shape (measured 2026-09-04, live query): `data`
            # is a list of block groups, each carrying its own `logs`; a range
            # with no logs answers `data: []`. The nested-dict reading is kept
            # only as a tolerated variant — the first deployment shipped with
            # it as the *only* reading and every page failed with "response has
            # no data.logs", which is the cost of a fixture written from memory
            # instead of from a captured response.
            data = page.get("data")
            if isinstance(data, list):
                rows = [
                    row
                    for entry in data if isinstance(entry, dict)
                    for row in (entry.get("logs") or [])
                ]
            elif isinstance(data, dict):
                rows = data.get("logs")
            else:
                rows = None
            if rows is None:
                # The response is a shape we do not recognize: refusing beats
                # guessing a resume point on a service whose contract we cannot read.
                from evm_rpc import EVMRPCError

                raise EVMRPCError(
                    f"hypersync [{net}]: unrecognized response shape",
                )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                converted = self._to_rpc_log(row)
                if converted is not None:
                    out.append(converted)
            next_block = page.get("next_block")
            try:
                nxt = int(next_block) if next_block is not None else None
            except (TypeError, ValueError):
                nxt = None
            if nxt is None:
                # No next_block means the service believes the range is
                # complete — the measured behavior at the range's end.
                return out, requests, True, int(hi)
            if nxt <= lo:
                if lo >= hi and not rows:
                    # The exhausted tail (measured live 2026-09-04, token
                    # 0xc52aedec…): when the walk's last page lands exactly on
                    # the final single-block range, the service answers an
                    # empty page with `next_block` pinned at `from_block`
                    # instead of omitting it. The block was queried and held
                    # nothing, so the range is complete — but only in this
                    # exact shape: a non-advancing cursor with rows present,
                    # or with range still remaining, stays a loud error.
                    return out, requests, True, int(hi)
                # A service bug or a shape change: a non-advancing cursor would
                # spin forever, so it is treated as a hard, loud error.
                from evm_rpc import EVMRPCError

                raise EVMRPCError(
                    f"hypersync [{net}]: next_block {nxt} did not advance "
                    f"past from_block {lo}",
                )
            lo = nxt
        return out, requests, True, int(hi)

    async def first_mint_block(
        self, network_id: str, address: str, head: int,
    ) -> int | None:
        """The token's first mint over the whole range — HyperSync's one-query answer.

        The public node cannot do this on Base at all (the 10K range cap forbids
        a `0->head` mint query, which is why Base sits in
        `EVM_CREATION_BLOCK_NETWORKS` doing ~20 binary-search `eth_getCode`
        calls per token). HyperSync answers the same question in one query
        with a two-topic filter, same trick as the Robinhood mint scan.
        """
        net = str(network_id)
        if net not in self._urls:
            raise EnvioUnavailable(f"hypersync does not serve network {net}")
        token = str(address).lower()
        zero_topic = "0x" + "0" * 64
        body = {
            "from_block": 0,
            "to_block": int(head),
            "logs": [{"address": [token], "topics": [[TRANSFER_TOPIC], [zero_topic]]}],
            "field_selection": {"log": ["block_number"]},
        }
        page = await self._query(net, body)
        data = page.get("data")
        if isinstance(data, list):
            rows = [
                row
                for entry in data if isinstance(entry, dict)
                for row in (entry.get("logs") or [])
            ]
        elif isinstance(data, dict):
            rows = data.get("logs") or []
        else:
            rows = []
        if not rows:
            return None
        blocks = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                blocks.append(int(row["block_number"]))
            except (KeyError, TypeError, ValueError):
                continue
        return min(blocks) if blocks else None
