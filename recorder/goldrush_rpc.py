"""GoldRush event-log adapter for initial and historical EVM backfills."""
from __future__ import annotations

import time
from datetime import datetime
import re
from typing import Any, Sequence

import httpx

import config
import evm_rpc
from provider_keys import KeyPool, read_keys


class GoldRushCreditError(evm_rpc.EVMRPCError):
    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class GoldRushRangeLimit(evm_rpc.EVMLogLimit):
    """مدىً أوسع من طاقة المزوّد: العلاج تصغيرُه، وهو ما يفعله `get_logs_paged`.

    `truncated` يفرّق بين رفضٍ صريح («المدى كبير») وصفحةٍ **مقتطعة** ردَّها
    المزوّد بنجاح ظاهر. الأوّل يُصغَّر بلا حدّ، والثاني له قاع: مدى كتلةٍ واحدة
    لا يُصغَّر أكثر، فلو بقي مقتطعاً فالعطب ليس في المدى.
    """

    def __init__(
        self, message: str, attempts: int,
        suggested_range: tuple[int, int] | None = None,
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.suggested_range = suggested_range
        self.truncated = truncated


def _is_truncated(data: dict[str, Any]) -> bool:
    """هل بقيت صفحةٌ لم تُقرأ؟ ثلاث دلائل لأنّ الردّ لا يثبت على شكلٍ واحد.

    `pagination` قد يكون `None` في بعض النهايات، و`links.prev` هو رابط الصفحة
    التالية في نهاية الأحداث (تسميةٌ معاكسة للحدس)، و`total_count` يفضح البقيّة
    حين يسكت الاثنان. وأيُّ دليلٍ يكفي: الشكّ يُعالَج بتصغير المدى وهو رخيص،
    والخطأ في الاتجاه الآخر بياناتٌ كاذبة.
    """
    pagination = data.get("pagination")
    if isinstance(pagination, dict):
        if pagination.get("has_more") is True:
            return True
        total = pagination.get("total_count")
        items = data.get("items")
        if isinstance(total, int) and isinstance(items, list) and total > len(items):
            return True
    links = data.get("links")
    return isinstance(links, dict) and bool(links.get("prev") or links.get("next"))


class GoldRushReplayRPC(evm_rpc.EVMRPC):
    """Use paginated GoldRush events where configured, normal RPC elsewhere."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._goldrush_disabled = False
        self._goldrush_keys = KeyPool(self._keys_from_disk())
        self._goldrush_calls = 0
        self._goldrush_events = 0
        self._fallback_calls = 0
        self._fallback_events = 0

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
        return {
            **self._goldrush_keys.stats(),
            "disabled": bool(self._goldrush_disabled),
            "goldrush_calls": self._goldrush_calls,
            "goldrush_events": self._goldrush_events,
            "fallback_calls": self._fallback_calls,
            "fallback_events": self._fallback_events,
        }

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
            info = body.get("info")
            if isinstance(info, dict) and info.get("message"):
                match = re.search(r"\[(\d+)\s*,\s*(\d+)\]", str(info["message"]))
                suggested = (
                    (int(match.group(1)), int(match.group(2)))
                    if match is not None else None
                )
                raise GoldRushRangeLimit(
                    f"GoldRush [{chain}]: {info['message']}", attempts,
                    suggested_range=suggested,
                )
            raise evm_rpc.EVMRPCError(f"GoldRush [{chain}]: data مفقودة")
        items = data.get("items")
        if not isinstance(items, list):
            raise evm_rpc.EVMRPCError(f"GoldRush [{chain}]: items مفقودة")
        # صفحةٌ واحدة تُقرأ، فالصفحة الثانية لو وُجدت **تحويلاتٌ تُفقد بصمت** —
        # والدفتر تراكميّ فالفقد لا يظهر نقصاً بل رصيداً سالباً أو تركّزاً كاذباً،
        # بعد آلاف النداءات. مقيس على Base: عملتان أنفقتا 15,270 و11,665 نداءً
        # وانتهتا إلى `negative` بصفر صفّ، والمدى المطلوب هنا مليونا كتلة ‎(‎
        # `GOLDRUSH_BLOCK_CHUNK`‎)‎ أي أضعافُ صفحةٍ لأي عملة متحرّكة. فالاقتطاع
        # يُرفَع كحدّ مدى: يُصغَّر المدى ويُعاد، ولا يُقبل نصفُ ردّ أبداً.
        if _is_truncated(data):
            raise GoldRushRangeLimit(
                f"GoldRush [{chain}]: صفحةٌ مقتطعة ({len(items)} حدثاً) "
                f"في المدى [{lo}, {hi}]",
                attempts, truncated=True,
            )
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
            self._fallback_calls += 1
            result = await super().get_logs_paged(
                net, addresses, from_block, to_block, topics=topics,
                max_calls=max_calls, sleep=sleep or __import__("asyncio").sleep,
                deadline=deadline,
            )
            self._fallback_events += len(result[0])
            return result
        cap = config.EVM_REPLAY_MAX_CALLS if max_calls is None else int(max_calls)
        pause = sleep or __import__("asyncio").sleep
        token = str(addresses[0]).lower()
        chunk_size = max(1, int(config.GOLDRUSH_BLOCK_CHUNK))
        out: list[dict[str, Any]] = []
        calls = 0
        lo = int(from_block)
        end = int(to_block)
        hinted_chunk_size: int | None = None
        while lo <= end:
            hi = min(end, lo + chunk_size - 1)
            if calls >= cap or (
                calls > 0 and deadline is not None and time.monotonic() >= deadline
            ):
                return out, calls, False, lo
            if calls:
                await pause(config.EVM_PACING_SECONDS)
            try:
                self._goldrush_calls += 1
                logs, attempts = await self._chunk(chain, token, lo, hi)
            except GoldRushRangeLimit as exc:
                calls += exc.attempts
                if exc.truncated and lo >= hi:
                    # قاعُ التصغير. الاستمرار هنا حلقةٌ لا تنتهي، والقبول دفترٌ
                    # كاذب — فيُرفَع خطأً صريحاً تراه العملة في `last_error`.
                    raise evm_rpc.EVMRPCError(str(exc)) from None
                suggested = exc.suggested_range
                if suggested is not None:
                    suggested_lo, suggested_hi = suggested
                    if suggested_lo == lo and suggested_hi >= hi:
                        chunk_size = max(1, (chunk_size + 1) // 2)
                    elif suggested_lo > lo:
                        chunk_size = suggested_lo - lo
                        hinted_chunk_size = suggested_hi - suggested_lo + 1
                    elif suggested_hi >= lo:
                        chunk_size = suggested_hi - lo + 1
                    else:
                        chunk_size = max(1, (chunk_size + 1) // 2)
                else:
                    chunk_size = max(1, (chunk_size + 1) // 2)
                continue
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
                self._fallback_calls += 1
                self._fallback_events += len(fallback)
                return out, calls + used + failed_attempts, complete, resume
            calls += attempts
            out.extend(logs)
            self._goldrush_events += len(logs)
            lo = hi + 1
            if hinted_chunk_size is not None:
                chunk_size = hinted_chunk_size
                hinted_chunk_size = None
        out.sort(key=lambda row: (
            evm_rpc._num(row.get("blockNumber")) or 0,
            evm_rpc._num(row.get("transactionIndex")) or 0,
            evm_rpc._num(row.get("logIndex")) or 0,
        ))
        return out, calls, True, end
