"""Envio HyperSync: the dedicated historical-log source for EVM backfill.

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
import evm_rpc
import httpx
from evm_rpc import TRANSFER_TOPIC
from provider_keys import KeyPool, read_keys

# HyperSync serves queries from `https://<network>.hypersync.xyz/query`. A
# network joins this map after a probe, not before. All four EVM endpoints
# answered the transport/query smoke test on 2026-09-05; live historical
# capacity still falls back per network when a request fails.
HYPERSYNC_URLS = {
    "143": "https://monad.hypersync.xyz/query",
    "56": "https://bsc.hypersync.xyz/query",
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
        # Coverage is evidence of a successful request, not merely a configured
        # key. The admission gate uses this set so a live Envio outage falls back
        # to conservative public-RPC capacity on the next cycle.
        self._successful_networks: set[str] = set()
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def can_attempt(self, network_id: str) -> bool:
        """True when a configured keyed route may be probed or used."""
        net = str(network_id)
        if not self._fixed_keys:
            try:
                self._keys.refresh(_read_keys())
            except Exception:  # noqa: BLE001 — availability checks must fail closed
                self._keys.refresh([])
        return net in self._urls and bool(self._keys.keys)

    def covers(self, network_id: str) -> bool:
        """True when this network is configured and currently usable.

        A key in the pool proves only that the route can be attempted. Once a
        request has failed with a transport, rate-limit, or server error, the
        route is no longer trusted for admission until a later request succeeds.
        """
        net = str(network_id)
        if not self._fixed_keys:
            try:
                self._keys.refresh(_read_keys())
            except Exception:  # noqa: BLE001 — availability checks must fail closed
                self._keys.refresh([])
        return net in self._urls and bool(self._keys.keys) and (
            net in self._successful_networks
        )

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
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """One POST /query. Raises `EnvioUnavailable` on auth trouble (fallback, not error);
        a redacted `EVMRPCError`-compatible raise on anything the caller should treat as retry.
        """
        if not self._fixed_keys:
            self._keys.refresh(_read_keys())
        if not self._keys.keys:
            raise EnvioUnavailable("no envio key in chain_keys.json")
        if deadline is not None and time.monotonic() >= deadline:
            # The budget is over **before** the request starts: a key rotation
            # or a first page may not spend one more second of it. Checked
            # here, in the one place every request passes, so the walk's
            # between-page check is a second gate, not the only one.
            from evm_rpc import EVMBudgetExpired

            raise EVMBudgetExpired(
                f"hypersync [{network_id}]: budget expired before request",
            )
        url = self._urls[str(network_id)]
        key = self._keys.current()
        # The request is bounded by the **remaining** budget twice over: the
        # httpx timeout caps each individual read/write phase, and
        # `asyncio.wait_for` cancels the whole request at the deadline —
        # httpx's read timeout is per-chunk, so a slow trickle of response
        # bytes could outlive the budget while every chunk stays "on time".
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                from evm_rpc import EVMBudgetExpired

                raise EVMBudgetExpired(
                    f"hypersync [{network_id}]: budget expired before request",
                )
        request_timeout = (
            self._timeout if remaining is None else min(self._timeout, remaining)
        )
        post = self._client.post(
            url, json=body, headers={"authorization": f"Bearer {key}"},
            timeout=request_timeout,
        )
        try:
            if remaining is None:
                resp = await post
            else:
                resp = await asyncio.wait_for(post, timeout=remaining + 0.1)
        except TimeoutError:
            # The deadline arrived mid-request. Not a transport failure and
            # not a retry: the same budget verdict as the pre-request check.
            from evm_rpc import EVMBudgetExpired

            raise EVMBudgetExpired(
                f"hypersync [{network_id}]: budget expired during request",
            ) from None
        except httpx.TimeoutException as exc:
            # The httpx timer can fire **before** wait_for's: it is capped to
            # the remaining budget (`min(self._timeout, remaining)`), so a
            # ReadTimeout raised at the deadline is the budget expiring — and
            # without this branch it would fall into the TransportError
            # handler below, be classified as a network failure, drop the
            # network from `_successful_networks`, and destroy the partial
            # return the walk already paid for. Decided by the clock, not by
            # which timer happened to fire first.
            if deadline is not None and time.monotonic() >= deadline:
                from evm_rpc import EVMBudgetExpired

                raise EVMBudgetExpired(
                    f"hypersync [{network_id}]: budget expired during request "
                    f"(httpx {type(exc).__name__})",
                ) from None
            # A timeout with budget still on the clock is a genuine network
            # failure: the server is slow past its own guard, not budget-bound.
            self._successful_networks.discard(str(network_id))
            from evm_rpc import EVMRateLimit

            raise EVMRateLimit(
                f"hypersync [{network_id}] {type(exc).__name__}",
            ) from None
        except httpx.TransportError as exc:
            self._successful_networks.discard(str(network_id))
            # A wait, not a failure: raising rate-limit-shaped lets the caller's
            # existing backoff handle it (the `EVMRateLimit` remedy).
            from evm_rpc import EVMRateLimit  # local import keeps the module import-light

            raise EVMRateLimit(
                f"hypersync [{network_id}] {type(exc).__name__}",
            ) from None
        if resp.status_code in (401, 403):
            # The key is the problem: rotate to the next key; when every key
            # has said no, unavailability is the honest verdict ⇒ the public
            # node continues. Bounded exactly like the 429 branch — each key
            # at most once per request, never an unbounded rotation.
            if _retries < len(self._keys.keys) - 1:
                self._keys.rotate(block_current=True)
                return await self._query(
                    network_id, body, _retries=_retries + 1, deadline=deadline,
                )
            self._successful_networks.discard(str(network_id))
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
                return await self._query(
                    network_id, body, _retries=_retries + 1, deadline=deadline,
                )
            self._successful_networks.discard(str(network_id))
            from evm_rpc import EVMRateLimit

            raise EVMRateLimit(
                f"hypersync [{network_id}] HTTP 429: "
                f"{self._hide(resp.text[:120])}",
            )
        if resp.status_code >= 500:
            self._successful_networks.discard(str(network_id))
            from evm_rpc import EVMRateLimit

            raise EVMRateLimit(
                f"hypersync [{network_id}] HTTP {resp.status_code}: "
                f"{self._hide(resp.text[:120])}",
            )
        if resp.status_code >= 400:
            self._successful_networks.discard(str(network_id))
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{network_id}] HTTP {resp.status_code}: "
                f"{self._hide(resp.text[:120])}",
            )
        try:
            parsed = resp.json()
        except ValueError:
            self._successful_networks.discard(str(network_id))
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{network_id}]: non-JSON response",
            ) from None
        if not isinstance(parsed, dict):
            self._successful_networks.discard(str(network_id))
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{network_id}]: unexpected response shape",
            )
        self._successful_networks.add(str(network_id))
        return parsed

    @staticmethod
    def _strict_int(value: Any) -> int | None:
        """A block/index field as a genuine integer — or None.

        `int()` alone is a coercion, not a check: `int(101.9)` is 101 and
        `int(True)` is 1, so a fractional or boolean block would be adopted
        as a plausible number instead of refused. The pointer got this
        strictness in round 2; the row fields get it here — same rule.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @staticmethod
    def _to_rpc_log(entry: dict[str, Any]) -> dict[str, Any] | None:
        """A HyperSync log row → a JSON-RPC-shaped record `decode_transfer` can read.

        Decimal in, hex out: the rest of the pipeline speaks JSON-RPC, and this
        is the one place the translation belongs. `None` on a malformed row —
        ignored, never guessed at, the same rule as `decode_transfer` itself.
        """
        block = EnvioHyperSync._strict_int(entry.get("block_number"))
        if block is None:
            return None
        log_index = EnvioHyperSync._strict_int(entry.get("log_index"))
        if log_index is None:
            return None
        tx_index = EnvioHyperSync._strict_int(entry.get("transaction_index"))
        if tx_index is None:
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

    @staticmethod
    def _parse_next_block(
        page: dict[str, Any], net: str,
    ) -> int | None:
        """The pagination pointer, parsed strictly — or None for "range complete".

        Completion is only the field's **measured absence** (the service omits
        `next_block` exactly when the range is fully served). A JSON `null` is
        a *present* field with no value — kept distinct from absence via the
        sentinel below, and refused, because we cannot tell a service bug from
        an intentional end signal and the honest answer is the raise. Anything
        present must be a genuine integer: `int()` alone accepts floats
        (`int(101.9)`) and booleans, so those are contract breaks, not numbers.
        """
        sentinel = object()
        next_block = page.get("next_block", sentinel)
        if next_block is sentinel:
            return None
        if isinstance(next_block, bool) or not isinstance(next_block, int):
            from evm_rpc import EVMRPCError

            raise EVMRPCError(
                f"hypersync [{net}]: non-integer next_block {next_block!r}",
            )
        return next_block

    @staticmethod
    def _page_rows(
        page: dict[str, Any], net: str, *, where: str,
    ) -> list[dict[str, Any]]:
        """The page's log rows, extracted under **one** policy for every path.

        The live shape (measured 2026-09-04) is `data` as a list of block
        groups each carrying `logs`; the nested-dict `data: {"logs": [...]}`
        shape is a tolerated variant from the first deployment. The policy,
        identical in both branches and in the mint scan (round 4, review
        item 3):

        * `data` as a list ⇒ every entry must be an object; a missing `logs`
          key is an empty group; anything **present** must be a genuine list —
          `False`/`{}`/`"raw"` are corrupt shapes, not "no events here".
        * `data` as a dict ⇒ same rule for its own `logs` key.
        * `data` of any other type ⇒ an unrecognized shape, refused.

        Raises `EVMRPCError` on a shape we cannot read — the caller decides
        whether that is a loud error (the transfer walk) or an unknown start
        (the mint scan).
        """
        from evm_rpc import EVMRPCError

        data = page.get("data")
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    raise EVMRPCError(
                        f"hypersync [{net}]: non-object block group ({where})",
                    )
                raw = entry.get("logs")
                if raw is None:
                    continue
                if not isinstance(raw, list):
                    raise EVMRPCError(
                        f"hypersync [{net}]: non-list logs in block group ({where})",
                    )
            return [
                row
                for entry in data
                for row in (entry.get("logs") or [])
            ]
        if isinstance(data, dict):
            raw = data.get("logs")
            if raw is None:
                raise EVMRPCError(
                    f"hypersync [{net}]: response has no logs ({where})",
                )
            if not isinstance(raw, list):
                raise EVMRPCError(
                    f"hypersync [{net}]: non-list logs in response ({where})",
                )
            return raw
        raise EVMRPCError(
            f"hypersync [{net}]: unrecognized response shape ({where})",
        )

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

        The caller's `from_block`/`to_block` are inclusive (the RPC contract
        this adapter mirrors), but HyperSync's wire `to_block` is **exclusive**
        (measured live 2026-09-07, net 4663, token 0x2d8d6f4a…: `[b, b]`
        answered 0 events, `[b, b+1]` answered 6 events at block b). Sending
        the caller's `hi` as-is would silently skip the final block — benign
        only while live apply happens to cover the seam from above — so the
        wire range is always `[lo, hi + 1)`, invisible to callers.
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
            if requests:
                await sleep(config.EVM_HYPERSYNC_PACING_SECONDS)
            # Checked **after** the pacing sleep, not before it: the sleep
            # spends budget too, and a deadline that dies during pacing must
            # not still buy the next page request (round 4: no request may
            # start outside the budget, wherever the time went).
            spent = deadline is not None and time.monotonic() >= deadline
            if requests >= cap or spent:
                # What is unread is everything from `lo` up ⇒ honest resume point.
                return out, requests, False, lo
            body = {
                # Wire semantics are exclusive on the top end — see the
                # docstring. +1 makes the caller's inclusive `hi` land.
                "from_block": lo,
                "to_block": hi + 1,
                "logs": [{"address": [token], "topics": [[topic0]]}],
                "field_selection": {"log": [
                    "transaction_hash", "block_number", "transaction_index",
                    "log_index", "address", "data", "topic0", "topic1", "topic2",
                ]},
            }
            try:
                page = await self._query(net, body, deadline=deadline)
            except evm_rpc.EVMBudgetExpired:
                # The budget ran out before this page: **not** a fault, and
                # not a fallback reason. The pages already collected in `out`
                # are fetched, verified and paid for — throwing them away
                # here (the old shape: a generic raise ⇒ `retry` ⇒ re-read
                # from the last *saved* point) is what wasted progress and
                # re-read the same pages every cycle. The unread range
                # starts at `lo`, the honest resume point.
                return out, requests, False, lo
            requests += 1
            # One extraction policy for every path (round 4, review item 3):
            # strict list typing in both the block-group shape and the
            # nested-dict variant, `False`/`{}`/null refused rather than
            # read as "no events here".
            rows = self._page_rows(page, net, where="logs walk")
            nxt = self._parse_next_block(page, net)
            if nxt is None:
                # Absent next_block is the **measured** completion signal
                # (2026-09-04, live): the service omits the field exactly when
                # the range is fully served. Only that absence counts — every
                # present-but-corrupt shape is refused below, never silently
                # equated with "complete". The page's coverage is the whole
                # requested range, so the rows below validate against hi.
                nxt = int(hi) + 1
            else:
                if nxt < lo:
                    # A pointer that went **backward** is a contract break in
                    # every shape, including the single-block range: the
                    # measured tail pins the cursor *at* from_block, never
                    # behind it.
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: next_block {nxt} behind from_block {lo}",
                    )
                if nxt == lo:
                    if lo >= hi and not rows:
                        # The exhausted tail (measured live 2026-09-04, token
                        # 0xc52aedec…): when the walk's last page lands exactly
                        # on the final single-block range, the service answers
                        # an empty page with `next_block` pinned at
                        # `from_block` instead of omitting it. The block was
                        # queried and held nothing, so the range is complete —
                        # but only in this exact shape: a non-advancing cursor
                        # with rows present, or with range still remaining,
                        # stays a loud error.
                        return out, requests, True, int(hi)
                    # A service bug or a shape change: a non-advancing cursor
                    # would spin forever, so it is treated as a hard, loud error.
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: next_block {nxt} did not advance "
                        f"past from_block {lo}",
                    )
                if nxt > hi + 1:
                    # The service's cursor claims coverage beyond the exclusive
                    # ceiling we asked for: a corrupt pointer that would mark
                    # unserved blocks as read. Refused, not adopted.
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: next_block {nxt} exceeds to_block {hi + 1}",
                    )
            # The never-broken contract, now enforced against the page's own
            # pointer instead of assumed: every returned log's block is below
            # the resume point. A row above the pointer (the pointing-back
            # shape: event at 150, next_block 100, cap hit) would be applied
            # now and read again after the resume ⇒ a doubled balance.
            page_cover = nxt  # exclusive ceiling of what this page delivered
            for row in rows:
                if not isinstance(row, dict):
                    # Fail closed (plan 4.2): a row we cannot even type-check
                    # could carry a balance change, and dropping it while
                    # declaring the range complete writes a silent hole.
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: non-object log row in response",
                    )
                converted = self._to_rpc_log(row)
                if converted is None:
                    # Same rule as a non-dict row: the event exists but cannot
                    # be read, and an undecodable event that might affect
                    # balances must quarantine the range, not vanish in it.
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: undecodable log row in response",
                    )
                decoded = evm_rpc.decode_transfer(converted)
                if decoded is None:
                    # Full transfer validation, not just "readable row" (plan
                    # 4.2, second pass): the filter asked for Transfers, so a
                    # row that answers it and cannot decode is a contract
                    # break — quarantine the range, never bless a hole.
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: log row failed transfer validation",
                    )
                # And the event must belong to **this** page: the address we
                # filtered on, and the block coverage the pointer claims.
                # A valid Transfer for another token, or a block at/above the
                # resume point, is a response we cannot trust — the latter is
                # the doubled-balance shape, not noise.
                if decoded["token_address"] != token:
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: log row for unrequested token",
                    )
                if not lo <= decoded["block"] < page_cover:
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: log row block {decoded['block']} "
                        f"outside page coverage [{lo}, {page_cover})",
                    )
                out.append(converted)
            if nxt > int(hi):
                # The pointer reached (or passed) the exclusive ceiling: the
                # range is complete and every row was validated against it.
                return out, requests, True, int(hi)
            lo = nxt
        return out, requests, True, int(hi)

    async def first_mint_block(
        self, network_id: str, address: str, head: int,
        max_calls: int | None = None, deadline: float | None = None,
    ) -> int | None:
        """The token's first mint over the whole range — HyperSync's one-query answer.

        The public node cannot do this on Base at all (the 10K range cap forbids
        a `0->head` mint query, which is why Base sits in
        `EVM_CREATION_BLOCK_NETWORKS` doing ~20 binary-search `eth_getCode`
        calls per token). HyperSync answers the same question in one query
        with a two-topic filter, same trick as the Robinhood mint scan.

        The service can still paginate a mint scan (the measured behavior of
        `get_logs_paged` applies here too): the walk follows `next_block`
        until a mint appears or the range is exhausted — a first page without
        a mint is **not** proof none exists later, so a single-page read could
        silently hand back a wrong starting block. A non-advancing cursor is
        the same loud error here as in `get_logs_paged`, never a spin. `None`
        stays the honest "unknown" — the caller keeps its conservative walk,
        never a guessed recent block.
        """
        net = str(network_id)
        if net not in self._urls:
            raise EnvioUnavailable(f"hypersync does not serve network {net}")
        token = str(address).lower()
        zero_topic = "0x" + "0" * 64
        cap = max(1, max_calls) if max_calls is not None else 10 ** 9
        lo = 0
        hi = int(head)
        requests = 0
        while lo <= hi:
            if requests >= cap:
                # The budget stopped the scan before the range was proven
                # mint-free: unknown, not "no mint".
                return None
            if deadline is not None and time.monotonic() >= deadline:
                # Checked before the **first** request too, not only between
                # pages: an already-expired budget must mean zero HTTP calls,
                # never one courtesy request past the deadline.
                return None
            body = {
                # Exclusive wire semantics, same as get_logs_paged: +1 or the
                # head block's own mints never come back.
                "from_block": lo,
                "to_block": hi + 1,
                "logs": [{"address": [token], "topics": [[TRANSFER_TOPIC], [zero_topic]]}],
                "field_selection": {"log": ["block_number"]},
            }
            try:
                page = await self._query(net, body, deadline=deadline)
            except evm_rpc.EVMBudgetExpired:
                # Budget out mid-scan: unknown start (None) — the caller
                # keeps its conservative walk, and the next cycle's scan
                # starts over. Not a raise, and never a fallback to the
                # public scan: the budget is spent, not the provider.
                return None
            requests += 1
            # One extraction policy for every path (round 4, review item 3),
            # with the mint scan's own verdict: an unreadable shape is an
            # unknown start (None), never a mint-free "complete" read.
            try:
                rows = self._page_rows(page, net, where="mint scan")
            except evm_rpc.EVMRPCError:
                return None
            # The pointer is parsed **before** any block is trusted: a page
            # whose cursor is corrupt cannot tell us its own coverage, so a
            # block number it hands back is unverified (the exact gap the
            # get_logs_paged walk closed — same rule, same order). A present
            # but corrupt pointer (null/float/bool) is the same verdict here
            # as a corrupt row: unknown start, never a raise — the from-zero
            # walk is the safe direction, and a raise would retry the same
            # broken page every cycle.
            try:
                nxt = self._parse_next_block(page, net)
            except evm_rpc.EVMRPCError:
                return None
            if nxt is not None:
                if nxt < lo:
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: next_block {nxt} behind "
                        f"from_block {lo} (mint scan)",
                    )
                if nxt == lo:
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: next_block {nxt} did not advance "
                        f"past from_block {lo} (mint scan)",
                    )
                if nxt > hi + 1:
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: next_block {nxt} exceeds "
                        f"to_block {hi + 1} (mint scan)",
                    )
            page_cover = (nxt if nxt is not None else int(hi) + 1)
            blocks = []
            for row in rows:
                if not isinstance(row, dict):
                    from evm_rpc import EVMRPCError

                    raise EVMRPCError(
                        f"hypersync [{net}]: non-object mint row in response",
                    )
                raw = row.get("block_number")
                if isinstance(raw, bool) or not isinstance(raw, int):
                    # A corrupt mint record next to a valid one: the page says
                    # mints exist but one cannot be placed, so a "confirmed"
                    # starting block would be a guess. Unknown, not a min()
                    # over the rows that happened to parse.
                    return None
                if raw < 0:
                    # Blocks are unsigned; a negative "block" is a contract
                    # break, and an unplaceable mint is unknown, never a
                    # confirmed start.
                    return None
                if not lo <= raw < page_cover:
                    # A mint outside the page's own claimed coverage (or above
                    # the head we asked for): the page is unreliable, and its
                    # block numbers cannot be trusted either. Unknown, not a
                    # guessed start — the same never-broken contract the
                    # transfer walk enforces with a hard raise; here `None`
                    # keeps the conservative from-zero walk, which is the
                    # safe direction for a *start* (a raise would retry the
                    # same broken page every cycle).
                    return None
                blocks.append(raw)
            if blocks:
                return min(blocks)   # ascending pages ⇒ the first mint found is the lowest
            if nxt is None:
                # No next_block: the service says the range is complete and
                # mint-free — that *is* proof, unlike an empty first page.
                return None
            lo = nxt
        return None
