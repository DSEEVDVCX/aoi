"""عميل قراءة NodeReal لقياسات حائزي BSC.

NodeReal يعيد عدد الحائزين الدقيق وأعلى الأرصدة مرتبة، وهما ما لا يقدمه
ERC-20 من خلال JSON-RPC القياسي. المفتاح يُقرأ من الملف المحلي كل دورة ولا
يُطبع أو يُحفظ في الاستجابة.
"""
from __future__ import annotations

import asyncio
from typing import Any

import config
import httpx
from provider_keys import KeyPool, read_keys


class NodeRealError(RuntimeError):
    """فشل طلب NodeReal."""


class NodeRealRateLimit(NodeRealError):
    """NodeReal رفض الطلب بسبب CUPS أو الحصة."""


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
    """يفكّ الغلاف الإضافي الذي تعيده بعض واجهات NodeReal."""
    if isinstance(result, dict) and set(result) == {"result"}:
        return result["result"]
    return result


def _read_keys() -> list[str]:
    """كل مفاتيح NodeReal: البيئة، ثمّ الجمع، ثمّ المفرد القديم.

    (حُذف `_read_key()` المفرد: طريقُ قراءةٍ ثانٍ ميّت يعني تدويراً يُفقد بالخطأ
    بلا أثر ظاهر. مسار الملف انتقل إلى رسالة الغياب في `_call`.)
    """
    return read_keys("nodereal_api_keys", "nodereal_api_key", "NODEREAL_API_KEY")


class NodeRealRPC:
    """عميل HTTP غير متزامن لواجهة BSC Enhanced API."""

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
        """صورةُ حوض المفاتيح للرصد — أعدادٌ فقط، بلا أي قيمة (FR-013)."""
        if not hasattr(self, "_keys"):
            self._keys = KeyPool(_read_keys())
        try:
            self._keys.refresh(_read_keys())
        except Exception:  # noqa: BLE001 — قراءةُ قرصٍ فاشلة لا تُسقط تقريراً
            pass
        return self._keys.stats()

    async def _step_aside(self, *, rejected: bool) -> None:
        """المفتاح المرفوض يُدوَّر، والخدمة المتعطّلة تُمهَل. راجع `solana_rpc`."""
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
            raise NodeRealError(f"مفاتيح NodeReal غائبة: {config.chain_keys_path()}")
        # المحاولات لا تُشتقّ من عدد المفاتيح وحده (راجع `CHAIN_TRANSIENT_RETRIES`).
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
                    await self._step_aside(rejected=False)   # عطل الخدمة لا المفتاح
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
                raise NodeRealError(f"{method}: رد غير متوقع")
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
                raise NodeRealError(f"{method}: لا توجد نتيجة")
            return _result_value(body["result"])
        raise NodeRealRateLimit(f"{method}: كل مفاتيح NodeReal مرفوضة مؤقتاً")

    async def holder_count(self, token: str) -> int | None:
        raw = _result_value(await self._call("nr_getTokenHolderCount", [token.lower()]))
        if raw is None:
            return None  # المصدر لا يعرف بعض العقود؛ غياب مقيس لا صفر.
        value = _hex_int(raw)
        if value is None or value < 0:
            raise NodeRealError("nr_getTokenHolderCount: عدد غير مفهوم")
        return value

    async def top_holders(self, token: str, top_n: int = 20) -> list[tuple[str, int]]:
        # pageSize=100, pageKey="", topN=20. هذا يطلب ترتيباً تنازلياً من المصدر.
        result = await self._call(
            "nr_getTokenHolders", [token.lower(), "0x64", "", hex(int(top_n))],
        )
        details = result.get("details") if isinstance(result, dict) else None
        if not isinstance(details, list):
            raise NodeRealError("nr_getTokenHolders: تفاصيل غير موجودة")
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
            raise NodeRealError("eth_call: قيمة غير مفهومة")
        return value

    async def total_supply(self, token: str) -> int:
        return await self.call(token, "0x18160ddd")

    async def balance_of(self, token: str, holder: str) -> int:
        address_word = holder.lower().removeprefix("0x").rjust(64, "0")
        return await self.call(token, "0x70a08231" + address_word)
