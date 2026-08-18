"""GoldRush event-log adapter for initial and historical EVM backfills."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Sequence

import httpx

import config
import evm_rpc
from provider_keys import KeyPool, read_keys


class GoldRushCreditError(evm_rpc.EVMRPCError):
    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class GoldRushReplayRPC(evm_rpc.EVMRPC):
    """Use paginated GoldRush events where configured, normal RPC elsewhere."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._goldrush_disabled = False
        self._goldrush_keys = KeyPool(self._keys_from_disk())

    @staticmethod
    def _keys_from_disk() -> list[str]:
        return read_keys("goldrush_api_keys", "goldrush_api_key", "GOLDRUSH_API_KEY")

    def _refresh_keys(self) -> None:
        before = tuple(self._goldrush_keys.keys)
        self._goldrush_keys.refresh(self._keys_from_disk())
        if tuple(self._goldrush_keys.keys) != before:
            self._goldrush_disabled = False

    def _key(self) -> str:
        self._refresh_keys()
        if not self._goldrush_keys.keys:
            raise evm_rpc.EVMRPCError(f"مفتاح GoldRush غائب: {config.chain_keys_path()}")
        return self._goldrush_keys.current()

    def key_stats(self) -> dict[str, Any]:
        """صورةُ حوض مفاتيح GoldRush للرصد — أعدادٌ فقط، بلا أي قيمة (FR-013).

        `disabled` هو ما لا يقوله عددُ المفاتيح: نفادُ رصيد **كلّها** (402)
        يُسكِت المزوّد لبقيّة العمر ويسقط إلى RPC العادي، فتبدو الأحواض سليمة
        والمزوّد معطَّل. ولا يعود إلّا بتغيّر المفاتيح على القرص.
        """
        try:
            self._refresh_keys()
        except Exception:  # noqa: BLE001 — قراءةُ قرصٍ فاشلة لا تُسقط تقريراً
            pass
        return {**self._goldrush_keys.stats(), "disabled": bool(self._goldrush_disabled)}

    @staticmethod
    def _event_log(item: Any, token: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        topics = item.get("raw_log_topics")
        if (
            not isinstance(topics, list)
            or not topics
            or str(topics[0]).lower() != evm_rpc.TRANSFER_TOPIC
            or str(item.get("sender_address") or "").lower() != token.lower()
        ):
            return None
        block = evm_rpc._num(item.get("block_height"))
        if block is None:
            return None
        try:
            timestamp = int(datetime.fromisoformat(
                str(item["block_signed_at"]).replace("Z", "+00:00")
            ).timestamp())
        except (KeyError, TypeError, ValueError):
            timestamp = 0
        return {
            "address": token.lower(),
            "topics": topics,
            "data": str(item.get("raw_log_data") or "0x"),
            "blockNumber": hex(block),
            "blockTimestamp": hex(timestamp),
            "transactionHash": item.get("tx_hash"),
            "transactionIndex": hex(int(item.get("tx_offset") or 0)),
            "logIndex": hex(int(item.get("log_offset") or 0)),
        }

    async def _chunk(
        self, chain: str, token: str, lo: int, hi: int,
    ) -> tuple[list[dict[str, Any]], int]:
        url = f"https://api.covalenthq.com/v1/{chain}/events/"
        attempts = 0
        keys_tried = 0
        while True:
            attempts += 1
            key = self._key()
            try:
                response = await self._client.get(
                    url,
                    params={
                        "starting-block": "earliest" if int(lo) == 0 else int(lo),
                        "ending-block": int(hi),
                        "address": token, "topics": evm_rpc.TRANSFER_TOPIC,
                        "skip-decode": "true",
                    },
                    headers={"authorization": f"Bearer {key}"},
                )
                if response.status_code in (401, 402, 403, 429):
                    keys_tried += 1
                    if keys_tried < len(self._goldrush_keys.keys):
                        self._goldrush_keys.rotate(block_current=True)
                        continue
                    if response.status_code == 402:
                        raise GoldRushCreditError(
                            f"GoldRush [{chain}] HTTP 402: نفد رصيد كل المفاتيح",
                            attempts,
                        )
                if response.status_code == 429 or response.status_code >= 500:
                    raise evm_rpc.EVMRateLimit(
                        f"GoldRush [{chain}] HTTP {response.status_code}"
                    )
                if response.status_code >= 400:
                    raise evm_rpc.EVMRPCError(
                        f"GoldRush [{chain}] HTTP {response.status_code}: {response.text[:150]}"
                    )
                body = response.json()
                break
            except (evm_rpc.EVMRateLimit, httpx.TransportError) as exc:
                if attempts >= int(config.GOLDRUSH_RETRIES) + 1:
                    raise evm_rpc.EVMRPCError(
                        f"GoldRush [{chain}] {type(exc).__name__}: {exc}"
                    ) from None
                await __import__("asyncio").sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
            except evm_rpc.EVMRPCError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise evm_rpc.EVMRPCError(
                    f"GoldRush [{chain}] {type(exc).__name__}: {exc}"
                ) from None
        if not isinstance(body, dict) or body.get("error") is True:
            raise evm_rpc.EVMRPCError(f"GoldRush [{chain}]: ردّ خطأ")
        data = body.get("data")
        if not isinstance(data, dict):
            raise evm_rpc.EVMRPCError(f"GoldRush [{chain}]: data مفقودة")
        items = data.get("items")
        if not isinstance(items, list):
            raise evm_rpc.EVMRPCError(f"GoldRush [{chain}]: items مفقودة")
        logs = [
            log for item in items
            if (log := self._event_log(item, token)) is not None
        ]
        return logs, attempts

    async def get_logs_paged(
        self,
        network_id: str,
        addresses: Sequence[str],
        from_block: int,
        to_block: int,
        topics: Sequence[Any] | None = None,
        max_calls: int | None = None,
        sleep=None,
        deadline: float | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool, int]:
        net = str(network_id)
        self._refresh_keys()
        chain = config.GOLDRUSH_REPLAY_CHAINS.get(net)
        if (
            self._goldrush_disabled
            or not chain
            or len(addresses) != 1
            or int(to_block) - int(from_block) < config.GOLDRUSH_MIN_RANGE
        ):
            return await super().get_logs_paged(
                net, addresses, from_block, to_block, topics=topics,
                max_calls=max_calls, sleep=sleep or __import__("asyncio").sleep,
                deadline=deadline,
            )
        cap = config.EVM_REPLAY_MAX_CALLS if max_calls is None else int(max_calls)
        pause = sleep or __import__("asyncio").sleep
        token = str(addresses[0]).lower()
        chunk_size = max(1, int(config.GOLDRUSH_BLOCK_CHUNK))
        out: list[dict[str, Any]] = []
        calls = 0
        lo = int(from_block)
        end = int(to_block)
        while lo <= end:
            hi = min(end, lo + chunk_size - 1)
            if calls >= cap or (
                calls > 0 and deadline is not None and time.monotonic() >= deadline
            ):
                return out, calls, False, lo
            if calls:
                await pause(config.EVM_PACING_SECONDS)
            try:
                logs, attempts = await self._chunk(chain, token, lo, hi)
            except evm_rpc.EVMRPCError as exc:
                if not isinstance(exc, GoldRushCreditError):
                    raise
                self._goldrush_disabled = True
                failed_attempts = exc.attempts
                remaining = cap - calls - failed_attempts
                if remaining <= 0:
                    return out, calls + failed_attempts, False, lo
                fallback, used, complete, resume = await super().get_logs_paged(
                    net, addresses, lo, end, topics=topics,
                    max_calls=remaining, sleep=pause, deadline=deadline,
                )
                out.extend(fallback)
                return out, calls + used + failed_attempts, complete, resume
            calls += attempts
            out.extend(logs)
            lo = hi + 1
        out.sort(key=lambda row: (
            evm_rpc._num(row.get("blockNumber")) or 0,
            evm_rpc._num(row.get("transactionIndex")) or 0,
            evm_rpc._num(row.get("logIndex")) or 0,
        ))
        return out, calls, True, end
