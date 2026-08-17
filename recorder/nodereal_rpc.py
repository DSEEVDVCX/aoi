# -*- coding: utf-8 -*-
"""عميل قراءة NodeReal لقياسات حائزي BSC.

NodeReal يعيد عدد الحائزين الدقيق وأعلى الأرصدة مرتبة، وهما ما لا يقدمه
ERC-20 من خلال JSON-RPC القياسي. المفتاح يُقرأ من الملف المحلي كل دورة ولا
يُطبع أو يُحفظ في الاستجابة.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

import config


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


def _read_key() -> str:
    env = os.environ.get("NODEREAL_API_KEY", "").strip()
    if env:
        return env
    path = config.chain_keys_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = str((json.load(fh) or {}).get("nodereal_api_key") or "").strip()
    except (OSError, ValueError, TypeError):
        key = ""
    if not key:
        raise NodeRealError(f"مفتاح NodeReal غائب: {path}")
    return key


class NodeRealRPC:
    """عميل HTTP غير متزامن لواجهة BSC Enhanced API."""

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"user-agent": "fomo-recorder/1.0", "content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, params: list[Any]) -> Any:
        key = _read_key()
        url = f"https://bsc-mainnet.nodereal.io/v1/{key}"
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        try:
            response = await self._client.post(url, json=payload)
            if response.status_code == 429:
                raise NodeRealRateLimit(f"{method}: HTTP 429")
            if response.status_code >= 400:
                raise NodeRealError(f"{method}: HTTP {response.status_code}")
            body = response.json()
        except (NodeRealError, NodeRealRateLimit):
            raise
        except Exception as exc:  # noqa: BLE001
            raise NodeRealError(f"{method}: {type(exc).__name__}") from None
        if not isinstance(body, dict):
            raise NodeRealError(f"{method}: رد غير متوقع")
        error = body.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            message = str(error.get("message") if isinstance(error, dict) else error)
            if code in (429, -32005) or "compute units" in message.lower():
                raise NodeRealRateLimit(f"{method}: {message[:160]}")
            raise NodeRealError(f"{method}: {message[:160]}")
        if "result" not in body:
            raise NodeRealError(f"{method}: لا توجد نتيجة")
        return _result_value(body["result"])

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
