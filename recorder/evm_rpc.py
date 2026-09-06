"""A read-only client for official EVM nodes — no key, no provider, no signup.

**Read-only** (FR-012): `eth_blockNumber`, `eth_getLogs`, `eth_call`,
`eth_getCode` — no signing and no transaction sending.

**No key at all**, and this is a measured outcome, not a preference: Etherscan
V2 refuses without a key (`Missing/Invalid API Key`), the old V1 is retired,
Sourcify knows 1 of 10 tokens, and Blockscout costs 5.6 seconds per token and
gives only the top 50. The official nodes, by contrast, are free and 27 times
faster (0.21s/token on Robinhood). So nothing here needs revoking, because
there is no secret in the URL — unlike `solana_rpc.py`, where the key sits in
the URL itself.

**The caps are the provider's limits, not our choices**, all measured live on
2026-08-13:
- `publicnode` (BSC) rejects an address array larger than 5 with 403 ⇒ `EVM_ADDRESS_BATCH`.
- `bsc-dataseed1` rejects the range with `-32005 limit exceeded` at every size ⇒ rejected.
- Robinhood and Base accepted 57 and 22 addresses in a single call (0.5 and 0.4 seconds).
- Any node truncates at 10,000 logs ⇒ `_get_logs_paged` splits the range in half and retries.
- And Robinhood answers a backfill from block zero with `-32000 log query timed out`
  (measured on the first live cycle) — which is "the range is wider than my
  capacity" worded differently ⇒ it is split like the others.
- And it rate-limits with 429 after a heavy query ⇒ wait, then reissue the **same**
  range: surrendering it means a permanent hole in the ledger, not a delay.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from typing import Any

import config
import httpx

# The signature of the `Transfer(address,address,uint256)` event — keccak-256 of
# the signature text. This is **the only route** to a holder list on EVM: the
# ERC-20 standard does not store the list, so no node call returns it, and a
# balance can only be known by replaying every transfer.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# A 32-byte-wide topic for the zero address. `topics=[Transfer, ZERO_TOPIC]`
# means "transfers **from** the zero address", i.e. mints only — and the lowest
# block in it is effectively the token's creation block: no balance exists
# without a mint, and the ERC-20 standard emits `Transfer(0x0, …)` on every
# `_mint`.
ZERO_TOPIC = "0x" + "0" * 64

# Addresses that do not count as holders: zero (mint and burn) and the explicit burn address.
BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)


class EVMRPCError(RuntimeError):
    """An EVM node call failed."""


class EVMLogLimit(EVMRPCError):
    """The range is wider than the node can carry ⇒ it must be split, not accepted or abandoned.

    A separate exception rather than text checked with `in` at the use site:
    accepting a truncated response means a silently incomplete balance ledger,
    which is worse than no ledger — the numbers look sound and are false.

    And it covers the **query timeout**, not just truncation: Robinhood answered
    a backfill from block zero with `-32000 log query timed out` (measured
    2026-08-13) — the same information worded differently: "the range is wider
    than my capacity". Without that, the token's entire backfill dies instead
    of being split.
    """


class EVMBatchLimit(EVMRPCError):
    """The combined response of a batch larger than the node can carry ⇒ the **count** of calls is reduced, not the range.

    Separate from `EVMLogLimit` because the remedy differs: there the range is
    wide; here the range is acceptable, but packing ten acceptable responses
    into one response exceeds the size limit. Measured on Base 2026-08-19:
    `-32020 backend response too large` drops the **entire batch atomically**,
    and the largest acceptable batch is a property of the token, not of the
    network — measured 10, 5, 3, 2 and 1 across five watched tokens. So
    splitting the range here is a fix in the wrong place: it doubles the call
    count to solve a size problem and wastes the available range cap.

    And when the batch is already **one**, there is no count to reduce ⇒ it is treated as a truncation of the range.
    """


class EVMRateLimit(EVMRPCError):
    """The public node rate-limited us (429) ⇒ it is waited out and retried; the range is not abandoned.

    Separate from `EVMRPCError` because the remedy is entirely different:
    truncation is split, rate limiting is waited out — and splitting the range
    on a 429 doubles the call count and makes the rate limiting worse.
    """


def _num(value: Any) -> int | None:
    """Converts a hexadecimal (`0x…`) or decimal number to int, or None if that fails."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except (TypeError, ValueError):
        return None


def _topic_address(topic: Any) -> str | None:
    """Extracts an address from a 32-byte-wide topic (the last 20 bytes)."""
    if (
        not isinstance(topic, str)
        or re.fullmatch(r"0x[0-9a-fA-F]{64}", topic) is None
        or topic[2:26] != "0" * 24
    ):
        return None
    return "0x" + topic[-40:].lower()


def _evm_address(value: Any) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        return None
    return value.lower()


def _uint256(value: Any) -> int | None:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value) is None:
        return None
    return int(value, 16)


def _classify(label: str, code: Any, message: str) -> EVMRPCError:
    """Error code and message ⇒ the exception that carries the **remedy**, not the description.

    Three remedies that must not be mixed up: rate limiting is waited out, a
    wide range is split, a heavy batch has its count reduced. And the order is
    deliberate: "too many requests" is rate limiting, not range capacity, even
    though it shares one word with "too many logs".
    """
    low = message.lower()
    if (
        code in (429, -32011) or "too many requests" in low
        or "rate limit" in low or "no backend" in low or "try again" in low
    ):
        return EVMRateLimit(f"{label}: {message[:150]}")
    # A **response size** limit, not a range limit: Base answers `-32020 backend
    # response too large` on a batch whose ranges are all acceptable (measured
    # 2026-08-19) ⇒ the count is reduced. And when the count is already one,
    # `get_logs_paged` escalates it to a split of the range.
    if code == -32020 or "response too large" in low:
        return EVMBatchLimit(f"{label}: {message[:150]}")
    # Nodes word "the range is wider than my capacity" differently (`exceeds
    # limit of 10000`, `query returned more than`, `limit exceeded`, `limited
    # to a 10,000 range`, and **query timeout**) and all of them mean one
    # thing: split the range.
    if (
        code == -32614
        or "exceed" in low or "more than" in low or "too many" in low
        or "timed out" in low or "timeout" in low or "limited to a" in low
    ):
        return EVMLogLimit(f"{label}: {message[:150]}")
    return EVMRPCError(f"{label}: JSON-RPC {code}: {message[:150]}")


def _split_range(lo: int, hi: int, hint: int) -> list[tuple[int, int]]:
    """A rejected range ⇒ smaller ranges ordered **ascending**, with no gap and no overlap.

    The hint is used **only if it actually shrinks the range**, and this is a
    liveness condition, not an optimization: a range exactly 10,000 long on
    Base makes `range(lo, hi+1, 10_000)` return a single range that is the same
    one, which gets pushed back onto the stack to be rejected again — a loop
    that eats the whole call cap with zero progress. Halving is the only exit
    guaranteed to shrink.
    """
    if hint > 0 and (hi - lo + 1) > hint:
        return [(start, min(hi, start + hint - 1)) for start in range(lo, hi + 1, hint)]
    mid = lo + (hi - lo) // 2
    return [(lo, mid), (mid + 1, hi)]


def _in_block_order(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A strict chronological ordering for the returned logs.

    A batch returns its ranges in one response, and one of them may come back
    without its sibling, so the read order no longer matches the block order.
    The consumers sort and aggregate on their own, but the contract here is
    that what is returned is sorted: ordering once here is cheaper than lost
    trust at every call site.
    """
    return sorted(logs, key=lambda log: (
        _num(log.get("blockNumber")) or 0,
        _num(log.get("transactionIndex")) or 0,
        _num(log.get("logIndex")) or 0,
    ))


class EVMRPC:
    """An async client for EVM nodes. One httpx client for all networks.

    The network is passed on every call, not fixed on the object: one call
    covers a whole network, so the cycle passes over three networks in seconds
    — and three objects for three nodes are cost with no return.
    """

    def __init__(
        self,
        urls: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._urls = dict(urls or config.EVM_RPC_URLS)
        self._timeout = timeout if timeout is not None else config.EVM_TIMEOUT_SECONDS
        # Some public nodes reject a request without a `user-agent` header with
        # 403 (measured on `bsc-rpc.publicnode.com`) — one header is cheaper
        # than losing a network.
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"user-agent": "fomo-recorder/1.0", "content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def url_for(self, network_id: str) -> str:
        url = self._urls.get(str(network_id))
        if not url:
            raise EVMRPCError(f"no node defined for network {network_id}")
        return url

    async def _call(
        self, network_id: str, method: str, params: Any, *, url: str | None = None,
    ) -> Any:
        """A single JSON-RPC call. Raises `EVMLogLimit` on truncation and `EVMRPCError` otherwise."""
        url = url or self.url_for(network_id)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._client.post(url, json=payload)
            status = resp.status_code
            if status == 429:
                raise EVMRateLimit(f"{method} [{network_id}] HTTP 429")
            if status >= 400:
                # The status code alone is not enough: Base answers a range
                # wider than 10,000 blocks with **HTTP 413** and a body
                # containing `-32614 eth_getLogs is limited to a 10,000 range`
                # (measured 2026-08-13), so if it became a generic error the
                # call would be dropped instead of the range being split. And a
                # transient 5xx ("no backend is currently healthy") is a wait,
                # not a failure: the same range is retried.
                text = resp.text[:400]
                low = text.lower()
                if status == 413 or "-32614" in low or "limited to a" in low:
                    raise EVMLogLimit(f"{method} [{network_id}] HTTP {status}: {text[:150]}")
                if status >= 500:
                    raise EVMRateLimit(f"{method} [{network_id}] HTTP {status}: {text[:150]}")
                raise EVMRPCError(f"{method} [{network_id}] HTTP {status}: {text[:200]}")
            body = resp.json()
        except EVMRPCError:
            raise
        except httpx.TransportError as exc:
            # A read timeout or a dropped connection: a wait, not a failure —
            # this is what used to kill the call for good (seen as
            # `eth_blockNumber [4663] ReadTimeout` 2026-08-17) because it lands
            # in the broad `except Exception`. Classifying it as rate limiting
            # triggers the backoff already present in `get_logs_paged` instead
            # of surrendering the range — and surrendering it is a permanent
            # hole.
            raise EVMRateLimit(f"{method} [{network_id}] {type(exc).__name__}") from None
        except Exception as exc:  # noqa: BLE001
            raise EVMRPCError(f"{method} [{network_id}] {type(exc).__name__}: {exc}") from None
        if not isinstance(body, dict):
            raise EVMRPCError(f"{method} [{network_id}]: unexpected response ({type(body).__name__})")
        err = body.get("error")
        if err is not None:
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message") if isinstance(err, dict) else err)
            raise _classify(f"{method} [{network_id}]", code, msg)
        if "result" not in body:
            raise EVMRPCError(f"{method} [{network_id}]: no result and no error")
        return body["result"]

    async def block_number(self, network_id: str) -> int:
        """The chain head — the cycle's **anchor**, so it is retried on rate limiting and transient failure.

        Its failure does not drop a token but the sweep of the entire network
        (`evm_layer._apply_live`), and one cheap call is not worth that price.
        See `EVM_HEAD_RETRIES`.
        """
        attempts = int(config.EVM_HEAD_RETRIES) + 1
        for attempt in range(attempts):
            try:
                raw = await self._call(network_id, "eth_blockNumber", [])
            except EVMRateLimit:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            block = _num(raw)
            if block is None:
                raise EVMRPCError(f"eth_blockNumber [{network_id}]: unreadable block number")
            return block
        raise EVMRateLimit(f"eth_blockNumber [{network_id}]: failed after {attempts} attempts")

    async def block_timestamp(self, network_id: str, block: int) -> int | None:
        """The timestamp of one block, in seconds. Needed by the historical replay alone.

        The live layer does not need it (its time is now), while the replay
        converts a time to a block number and back — and the Robinhood node
        returns `blockTimestamp: '0x0'` in `eth_getLogs` records (measured
        2026-08-13), so the block itself is the only source of time.

        A nonexistent block (above the head) returns `null` ⇒ `None`, not an
        exception: the answer to a question about a block not yet produced is
        "not yet", not a failure.
        """
        blk = await self._call(network_id, "eth_getBlockByNumber", [hex(int(block)), False])
        if not isinstance(blk, dict):
            return None
        return _num(blk.get("timestamp"))

    async def get_code(self, network_id: str, address: str) -> str:
        """The contract's bytecode. `0x` = not a contract (a wallet or an empty address)."""
        result = await self._call(network_id, "eth_getCode", [address, "latest"])
        return result if isinstance(result, str) else "0x"

    async def get_code_at(self, network_id: str, address: str, block: int) -> str:
        """Bytecode at a specific block, using the historical-state service when one is defined."""
        net = str(network_id)
        historical = config.EVM_HISTORICAL_RPC_URLS.get(net)
        if not historical:
            result = await self._call(net, "eth_getCode", [address, hex(int(block))])
            return result if isinstance(result, str) else "0x"
        result = await self._call(
            net, "eth_getCode", [address, hex(int(block))], url=historical,
        )
        return result if isinstance(result, str) else "0x"

    async def historical_block_number(self, network_id: str) -> int:
        """The head of the historical-state service, or the live RPC head when no dedicated service exists."""
        net = str(network_id)
        historical = config.EVM_HISTORICAL_RPC_URLS.get(net)
        if not historical:
            return await self.block_number(net)
        block = _num(await self._call(net, "eth_blockNumber", [], url=historical))
        if block is None:
            raise EVMRPCError(f"eth_blockNumber [{net}]: unreadable block number")
        return block

    async def contract_creation_block(
        self, network_id: str, address: str, head: int,
    ) -> int | None:
        """The first block in which the contract had bytecode, via an exact binary search."""
        historical_head = await self.historical_block_number(network_id)
        hi = min(max(0, int(head)), historical_head)
        if await self.get_code_at(network_id, address, hi) in ("0x", "0x0", ""):
            return None
        lo = 0
        while lo < hi:
            mid = lo + (hi - lo) // 2
            code = await self.get_code_at(network_id, address, mid)
            if code in ("0x", "0x0", ""):
                lo = mid + 1
            else:
                hi = mid
        return lo

    async def eth_call(
        self, network_id: str, to: str, data: str, block: str = "latest",
    ) -> str | None:
        """A read-function call. Returns None on revert instead of dropping the cycle.

        A revert is **expected**, not anomalous: we ask `owner()` of a contract
        that may not have it, and a missing function reverts. So failure here
        is an answer ("no such function"), not an error.

        But rate limiting (429) is not an answer: swallowing it writes "this
        contract has no owner", which is false information stored as if
        measured ⇒ it is raised, so the row becomes an error that gets retried.
        """
        try:
            result = await self._call(
                network_id, "eth_call", [{"to": to, "data": data}, block],
            )
        except (EVMRateLimit, EVMLogLimit):
            raise
        except EVMRPCError:
            return None
        return result if isinstance(result, str) else None

    async def get_logs(
        self,
        network_id: str,
        addresses: Sequence[str],
        from_block: int,
        to_block: int,
        topics: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """`eth_getLogs` in a single call. Raises `EVMLogLimit` if the node truncates."""
        params = [{
            "fromBlock": hex(int(from_block)),
            "toBlock": hex(int(to_block)),
            "address": [a.lower() for a in addresses],
            "topics": list(topics) if topics is not None else [TRANSFER_TOPIC],
        }]
        result = await self._call(network_id, "eth_getLogs", params)
        if not isinstance(result, list):
            raise EVMRPCError(f"eth_getLogs [{network_id}]: response is not a list")
        # Some nodes return 200 with a list truncated at the cap and no `error`
        # block ⇒ reaching exactly the limit count is treated as truncation. An
        # extra half-range call is cheaper than an incomplete ledger.
        if len(result) >= config.EVM_LOG_LIMIT:
            raise EVMLogLimit(
                f"eth_getLogs [{network_id}]: {len(result)} logs => limit reached"
            )
        return [r for r in result if isinstance(r, dict)]

    async def get_logs_multi(
        self,
        network_id: str,
        addresses: Sequence[str],
        ranges: Sequence[tuple[int, int]],
        topics: Sequence[Any] | None = None,
    ) -> list[list[dict[str, Any]] | EVMRPCError]:
        """Several `eth_getLogs` calls in **one HTTP request** (a JSON-RPC 2.0 batch).

        This is the way out of the range caps without gaming them: the cap
        limits the individual call, not the request, so packing ten acceptable
        calls into one request is legitimate under the standard itself and
        doubles the yield. Measured 2026-08-19: Base 5×10,000 blocks in 1.16s
        (versus 0.88s for a single call), and Monad 100×100 blocks = 10,000
        blocks in one request, while a single call there is capped at 100.

        And it returns a list **aligned with `ranges`**: for each range, either
        its logs or its exception. Raising at the first failed range throws
        away nine successful results that were paid for — a batch mixes success
        and failure in one response by its nature. But a failure of the
        **request itself** (rate limit or size limit) is raised: there is no
        result in it worth saving.
        """
        url = self.url_for(network_id)
        label = f"eth_getLogs×{len(ranges)} [{network_id}]"
        topic_filter = list(topics) if topics is not None else [TRANSFER_TOPIC]
        addrs = [a.lower() for a in addresses]
        payload = [
            {
                "jsonrpc": "2.0", "id": index, "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(int(lo)), "toBlock": hex(int(hi)),
                    "address": addrs, "topics": topic_filter,
                }],
            }
            for index, (lo, hi) in enumerate(ranges)
        ]
        try:
            resp = await self._client.post(url, json=payload)
            status = resp.status_code
            if status == 429:
                raise EVMRateLimit(f"{label} HTTP 429")
            if status >= 500:
                raise EVMRateLimit(f"{label} HTTP {status}")
            if status >= 400:
                # 413 on a batch is ambiguous: a wide range or a heavy
                # response? It is read as a **heavy batch**, because reducing
                # the count is guaranteed progress in both cases, and at a
                # count of one the caller escalates it to a range split. The
                # converse is not true: splitting the range to solve a size
                # problem wastes the whole available cap.
                raise EVMBatchLimit(f"{label} HTTP {status}: {resp.text[:150]}")
            body = resp.json()
        except EVMRPCError:
            raise
        except httpx.TransportError as exc:
            raise EVMRateLimit(f"{label} {type(exc).__name__}") from None
        except Exception as exc:  # noqa: BLE001
            raise EVMRPCError(f"{label} {type(exc).__name__}: {exc}") from None
        if isinstance(body, dict):
            # A single-object response to an array request: an error concerning the entire request, not one range in it.
            err = body.get("error")
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message") if isinstance(err, dict) else err)
            raise _classify(label, code, msg)
        if not isinstance(body, list):
            raise EVMRPCError(f"{label}: response is not an array ({type(body).__name__})")
        by_id: dict[int, dict[str, Any]] = {}
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                by_id[item["id"]] = item
        out: list[list[dict[str, Any]] | EVMRPCError] = []
        for index in range(len(ranges)):
            item = by_id.get(index)
            if item is None:
                out.append(EVMRPCError(f"{label}: no response for call {index}"))
                continue
            err = item.get("error")
            if err is not None:
                code = err.get("code") if isinstance(err, dict) else None
                msg = str(err.get("message") if isinstance(err, dict) else err)
                out.append(_classify(label, code, msg))
                continue
            result = item.get("result")
            if not isinstance(result, list):
                out.append(EVMRPCError(f"{label}: call {index} has no result"))
                continue
            if len(result) >= config.EVM_LOG_LIMIT:
                out.append(EVMLogLimit(f"{label}: {len(result)} logs => limit reached"))
                continue
            out.append([row for row in result if isinstance(row, dict)])
        return out

    async def first_mint_block(
        self, network_id: str, address: str, head: int,
    ) -> int | None:
        """The first block in which this contract was minted — **one call** over the `0→head` range.

        The replacement for `contract_creation_block` where there is no
        archive: the Robinhood node keeps only ~128 blocks of state, so a
        binary search on `eth_getCode` is impossible there, and the only
        alternative was walking from block zero. The difference is measured
        2026-08-19: four watched tokens were minted at 0.2%, 67.7%, 88.6% and
        93.6% of the chain, i.e. 27–37 **million** empty blocks walked before
        the first transfer — and the scan answers in 0.17 seconds.

        The trick is that the two-topic filter trims the response down to
        mints alone (a single mint across the four tokens), so it never
        reaches the 10,000 cap even if the token is noisy.

        It returns `None` when the answer is unknown, and then the caller
        **must** start from zero rather than guess: a token that mints
        continuously may truncate the response (`EVMLogLimit`), making the
        lowest block we saw higher than the truth, and a wrong low means a
        holder who received before it shows a negative balance — i.e. the
        whole token gets rejected. And rate limiting is raised, not
        swallowed: "I could not ask" is not "no mint before this block".
        """
        try:
            logs = await self.get_logs(
                network_id, [address], 0, int(head),
                topics=[TRANSFER_TOPIC, ZERO_TOPIC],
            )
        except EVMLogLimit:
            return None
        blocks = [
            block for block in (_num(log.get("blockNumber")) for log in logs)
            if block is not None
        ]
        return min(blocks) if blocks else None

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
        """The same call, with the range split on truncation and acceptable ranges packed into a batch.

        Returns (the logs, the number of **requests**, whether the whole range
        was completed, the first unread block). The fourth is the resume
        point: on completion it equals `to_block`, and on hitting the cap it
        equals the lowest block left on the stack — so work resumes where it
        stopped, instead of the whole range being redone or falsely declared
        complete. And the count is now the number of **HTTP requests**, not
        calls: the quota measured on public nodes is a request quota (nine in
        ~15s on Base), so a batch buys ten times the range for the same price.

        And **the contract that is never broken**: every returned log's block
        is below the resume point. Without it, a log gets applied and then
        read again in the next cycle ⇒ a doubled balance, or the cursor
        advances over an unread range ⇒ a permanent hole. And the batch
        threatens it directly: of ten ranges in one request, the oldest may
        fail while the newest succeeds, so the newest goes back on the stack
        **and its logs are discarded** even though they were paid for — one
        extra request is cheaper than a hole.

        The split is adaptive, not a fixed step: the quiet token is measured
        at one call for 1.71 million blocks, and the noisy one truncates at
        10,000 — and a fixed step means either hundreds of calls for the quiet
        one or truncation for the noisy one. And the `max_calls` cap stops a
        single token from eating the cycle's whole minute.

        And `deadline` (a `monotonic` instant) is a cap **in time**, not in
        count, which is what is actually needed: a single call can hang for up
        to `EVM_TIMEOUT_SECONDS` (25s), so twenty calls could be two seconds
        or eight minutes — and a count cannot tell them apart. Measured on the
        second live cycle: 72 calls in 118 seconds against a 60-second period.
        And stopping here is **not a loss**: exiting short with a resume point
        is the same path as the cap, so the next cycle continues from where we
        stopped. And one request is always allowed even when the budget is
        spent, so the token never spins with no progress at all.
        """
        cap = config.EVM_BACKFILL_MAX_CALLS if max_calls is None else max_calls
        net = str(network_id)
        hint = int(config.EVM_LOG_RANGE_HINT.get(net, 0))
        # The batch size starts from the network's measured cap and then
        # **only shrinks, never grows**: the largest acceptable batch is a
        # property of the token, not of the network (measured 10, 5, 3, 2 and
        # 1 across five Base tokens), so learning inside the call is truer
        # than a constant in the file. And climbing back up would mean a fresh
        # rejection every few requests, i.e. cost with no return.
        batch = max(1, int(config.EVM_BATCH_SIZE.get(net, 1)))
        out: list[dict[str, Any]] = []
        calls = 0
        # A single-range stack instead of recursion: the depth can reach 20
        # levels over a million-block range, and the stack makes respecting
        # `cap` a one-liner. And its invariant is that reading it from top to
        # bottom is **ascending**, which is what makes its top the resume
        # point.
        pending: list[tuple[int, int]] = [(int(from_block), int(to_block))]
        while pending:
            spent = (
                calls > 0 and deadline is not None and time.monotonic() >= deadline
            )
            if calls >= cap or spent:
                # What is left on the stack is unread ⇒ the range is incomplete.
                return (
                    _in_block_order(out), calls, False,
                    min(lo for lo, _ in pending),
                )
            taken: list[tuple[int, int]] = []
            while pending and len(taken) < batch:
                lo, hi = pending.pop()
                if lo <= hi:
                    taken.append((lo, hi))
            if not taken:
                continue
            if calls:
                await sleep(
                    config.EVM_BATCH_PACING_SECONDS if len(taken) > 1
                    else config.EVM_PACING_SECONDS
                )
            calls += 1
            if len(taken) == 1:
                lo, hi = taken[0]
                try:
                    results: list[Any] = [
                        await self.get_logs(net, addresses, lo, hi, topics)
                    ]
                except EVMRPCError as exc:
                    results = [exc]
            else:
                try:
                    results = list(
                        await self.get_logs_multi(net, addresses, taken, topics)
                    )
                except EVMRPCError as exc:
                    results = [exc] * len(taken)

            fresh: list[tuple[int, int, list[dict[str, Any]]]] = []
            requeue: list[tuple[int, int]] = []
            waited = False
            shrink = False
            # `strict`: one response per range by construction, and a length
            # mismatch means a response is being paired with a range that is
            # not its own — a false ledger, not a loud exception.
            for (lo, hi), result in zip(taken, results, strict=True):
                if isinstance(result, list):
                    fresh.append((lo, hi, result))
                    continue
                if isinstance(result, EVMRateLimit):
                    # Rate limiting is waited out, not split: splitting
                    # doubles the calls and makes the rate limiting worse. And
                    # the range goes back on the stack as is — surrendering it
                    # here means a hole in the ledger, not a delay. And the
                    # `cap` limit is what keeps the loop from spinning forever.
                    requeue.append((lo, hi))
                    waited = True
                    continue
                if isinstance(result, EVMBatchLimit) and len(taken) > 1:
                    requeue.append((lo, hi))
                    shrink = True
                    continue
                if isinstance(result, (EVMLogLimit, EVMBatchLimit)):
                    if lo >= hi:
                        # A single block exceeds the cap — no split is
                        # possible. The exception is raised rather than
                        # leaving the layer hung forever on one block.
                        raise result
                    requeue.extend(_split_range(lo, hi, hint))
                    continue
                raise result

            floor = min((lo for lo, _ in requeue), default=None)
            for lo, hi, logs in fresh:
                if floor is not None and lo > floor:
                    # This range succeeded, but an **older** range in the same
                    # batch failed. Keeping its logs means either reading them
                    # again later (a doubled balance) or advancing the resume
                    # point past the failed range (a permanent hole). So they
                    # are discarded and the range retried: one extra request
                    # is cheaper than a false ledger.
                    requeue.append((lo, hi))
                else:
                    out.extend(logs)
            if shrink:
                batch = max(1, batch // 2)
            requeue.sort()
            pending.extend(reversed(requeue))
            if waited:
                await sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
        return _in_block_order(out), calls, True, int(to_block)


def decode_transfer(log: dict[str, Any]) -> dict[str, Any] | None:
    """Decodes a `Transfer` log into (token, from, to, value, block).

    The value is in `data`, not in the topics (not indexed in standard ERC-20),
    and the two addresses are in `topics[1]` and `topics[2]`. A log with fewer
    than three topics is not a standard Transfer (some contracts emit an event
    with the same signature and different fields) ⇒ it is ignored, never guessed at.
    """
    topics = log.get("topics")
    if (
        not isinstance(topics, list)
        or len(topics) != 3
        or not isinstance(topics[0], str)
        or topics[0].lower() != TRANSFER_TOPIC
    ):
        return None
    sender = _topic_address(topics[1])
    recipient = _topic_address(topics[2])
    value = _uint256(log.get("data"))
    block = _num(log.get("blockNumber"))
    token = _evm_address(log.get("address"))
    if (
        sender is None or recipient is None or value is None
        or block is None or block < 0 or token is None
    ):
        return None
    return {
        "token_address": token,
        "from": sender,
        "to": recipient,
        "value": value,
        "block": block,
    }
