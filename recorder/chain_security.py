"""فاحص أمان على السلسلة لسولانا وشبكات EVM.

كل النداءات قراءة فقط (`getAccountInfo`/`eth_call`/`simulateTransaction`). لا
توقيع، لا بثّ، ولا مفاتيح خاصة. الفشل لا يتحول إلى أمان: النتيجة `unknown`.

المحاكاة هنا تختبر **نقل التوكن من حائز فعلي** إن أمكن؛ لا تدّعي أنها محاكاة
بيع على DEX. البيع الدقيق يحتاج مسار router/المجمّع والحجم والمحفظة نفسها.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

SOLANA_NETWORK_ID = "1399811149"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

ZERO_EVM_ADDRESS = "0x" + "0" * 40
DEAD_EVM_ADDRESS = "0x" + "0" * 36 + "dead"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
HELIUS_HTTP_BASE = "https://mainnet.helius-rpc.com/?api-key="

# حالة مجمّع Helius داخل جلسة المسجّل. لا تُكتب العناوين أو المفاتيح في السجلّ؛
# المفتاح هنا عنوان داخلي في الذاكرة فقط. مثل crib، 429 المتكرر ثلاث مرات يستبعد
# المفتاح للجلسة فقط، أمّا نفاد credits الصريح فيُحفظ في المخزن المشترك أيضاً.
_SOLANA_POOL_CURSOR = 0
_RPC_RATE_LIMIT_STRIKES: dict[str, int] = {}
_RPC_SESSION_DEAD: set[str] = set()
_RATE_LIMIT_STRIKES_TO_DISABLE = 3

_CREDIT_EXHAUSTED_MARKERS = (
    "out of credits", "credits exhausted", "credit limit",
    "insufficient credit", "no credits", "monthly credit",
    "monthly usage limit", "monthly limit", "payment required",
    "plan limit",
)
_RATE_LIMIT_MARKERS = ("too many requests", "rate limit", "rate-limit")
_TRANSIENT_RPC_MARKERS = (
    "overloaded", "please try again", "temporarily unavailable",
    "service unavailable", "timeout", "timed out", "deprioritized",
    "slow down requests",
)

EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
EIP1967_ADMIN_SLOT = (
    "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
)
EIP1967_BEACON_SLOT = (
    "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
)

# selectors ثابتة (Keccak-256 لأول أربعة بايتات). وجودها قدرة محتملة لا دليل
# على استعمالها، لذلك يرفع `review` ولا يحكم بالنصب وحده.
RISKY_SELECTORS = {
    "8456cb59": "pause()",
    "3f4ba83a": "unpause()",
    "c2e5ec04": "setTradingEnabled(bool)",
    "8a8c523c": "enableTrading()",
    "c9567bf9": "openTrading()",
    "f9f92be4": "blacklist(address)",
    "153b0d1e": "setBlacklist(address,bool)",
    "d01dd6d2": "setBlacklisted(address,bool)",
    "fe575a87": "isBlacklisted(address)",
    "40c10f19": "mint(address,uint256)",
    "0b78f9c0": "setFees(uint256,uint256)",
    "0cc835a3": "setBuyFee(uint256)",
    "8b4cee08": "setSellFee(uint256)",
    "061c82d0": "setTaxFeePercent(uint256)",
    "74010ece": "setMaxTxnAmount(uint256)",
    "ea1644d5": "setMaxWalletSize(uint256)",
}


@dataclass(frozen=True)
class NetworkSpec:
    kind: str
    name: str
    rpc_url: str | None
    rpc_chain_id: int | None = None


def network_spec(network_id: str) -> NetworkSpec | None:
    """Fomo networkId → نوع السلسلة وRPC. متغير البيئة يتغلب على الافتراضي."""
    specs = {
        SOLANA_NETWORK_ID: NetworkSpec(
            "solana", "solana",
            os.getenv("AOI_SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        ),
        "56": NetworkSpec(
            "evm", "bnb", os.getenv(
                "AOI_EVM_RPC_56", "https://bsc-dataseed.bnbchain.org"
            ), 56,
        ),
        "8453": NetworkSpec(
            "evm", "base", os.getenv("AOI_EVM_RPC_8453", "https://mainnet.base.org"),
            8453,
        ),
        "143": NetworkSpec(
            "evm", "monad", os.getenv("AOI_EVM_RPC_143", "https://rpc.monad.xyz"),
            143,
        ),
        "4663": NetworkSpec(
            "evm", "robinhood", os.getenv(
                "AOI_EVM_RPC_4663", "https://rpc.mainnet.chain.robinhood.com"
            ), 4663,
        ),
        # Fomo يسمّي Hyperliquid بالمعرّف 1337؛ HyperEVM الرئيسية نفسها 999.
        "1337": NetworkSpec(
            "evm", "hyperevm", os.getenv(
                "AOI_EVM_RPC_1337", "https://rpc.hyperliquid.xyz/evm"
            ), 999,
        ),
        # لا يوجد endpoint عام رسميّ مركزيّ لإيثريوم؛ الإعداد صريح حتى لا نرسل
        # العناوين إلى مزوّد ثالث افتراضي بلا قرار من مالك المشروع.
        "1": NetworkSpec("evm", "ethereum", os.getenv("AOI_EVM_RPC_1"), 1),
    }
    return specs.get(str(network_id))


class RpcError(RuntimeError):
    """خطأ RPC منزوع عنوان endpoint كي لا يتسرّب مفتاح ضمن URL."""


def _rpc_failure_kind(
    *, http_status: int | None = None, code: Any = None, message: Any = None,
) -> str:
    """يصنّف فشل المزوّد بلا الاحتفاظ بعنوانه أو مفتاحه.

    Helius قد يعيد نفاد الرصيد أو overload داخل JSON-RPC مع HTTP 200، لذلك لا
    يكفي فحص status. الأخطاء الدلالية مثل invalid params تبقى غير قابلة للتدوير.
    """
    text = str(message or "").lower()
    if any(marker in text for marker in _CREDIT_EXHAUSTED_MARKERS):
        return "credits_exhausted"
    if http_status in {401, 403} or any(
        marker in text for marker in ("invalid api key", "unauthorized", "forbidden")
    ):
        return "unauthorized"
    if http_status == 429 or code == 429 or any(
        marker in text for marker in _RATE_LIMIT_MARKERS
    ):
        return "rate_limited"
    if http_status in {500, 502, 503, 504} or any(
        marker in text for marker in _TRANSIENT_RPC_MARKERS
    ):
        return "transient"
    return "fatal"


def _safe_rpc_message(value: Any) -> str:
    text = str(value or "")[:240]
    text = re.sub(r"(?i)(api-key=)[^&\s]+", r"\1•••", text)
    return re.sub(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b", "<uuid>", text
    )


def _endpoint_available(url: str) -> bool:
    return url not in _RPC_SESSION_DEAD


def _note_endpoint_success(url: str) -> None:
    _RPC_RATE_LIMIT_STRIKES.pop(url, None)


def _note_endpoint_failure(url: str, kind: str) -> None:
    # الاستبعاد/الضربات سياسة مجمّع مفاتيح Helius فقط. endpoint عام وحيد لشبكة
    # EVM لا بديل له؛ قتله للجلسة بعد 429 يجعل كل الفحوص اللاحقة تفشل بلا محاولة.
    if _extract_helius_key(url) is None:
        return
    if kind in {"credits_exhausted", "unauthorized"}:
        _RPC_SESSION_DEAD.add(url)
        _RPC_RATE_LIMIT_STRIKES.pop(url, None)
        return
    if kind != "rate_limited":
        return
    strikes = _RPC_RATE_LIMIT_STRIKES.get(url, 0) + 1
    if strikes < _RATE_LIMIT_STRIKES_TO_DISABLE:
        _RPC_RATE_LIMIT_STRIKES[url] = strikes
        return
    _RPC_RATE_LIMIT_STRIKES.pop(url, None)
    _RPC_SESSION_DEAD.add(url)


def _extract_helius_key(url: str) -> str | None:
    match = re.search(r"[?&]api-key=([^&\s]+)", url, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _persist_credit_exhausted(url: str) -> bool:
    """يعطّل مفتاح credits المنتهي في مخزن crib تحت قفله المتوافق.

    لا نعطّل 429 فارغاً/عابراً في القرص؛ وحده نص نفاد الرصيد الصريح يصل هنا.
    """
    api_key = _extract_helius_key(url)
    store_value = os.getenv("AOI_HELIUS_KEYS_PATH", "").strip()
    if not api_key or not store_value:
        return False
    target = Path(store_value)
    lock_path = Path(str(target) + ".lock")
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + 2.0
    while True:
        try:
            descriptor = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 10.0:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        except OSError:
            return False

    try:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            return False
        item = next(
            (
                entry for entry in keys
                if isinstance(entry, dict) and entry.get("apiKey") == api_key
            ),
            None,
        )
        if item is None:
            return False
        if item.get("disabledAt"):
            return True
        item["disabledAt"] = int(time.time() * 1000)
        item["disabledReason"] = "نفدت الحصّة تلقائياً"

        backup = Path(str(target) + ".bak")
        shutil.copy2(target, backup)
        temp = target.with_name(
            f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            descriptor = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    except OSError:
        return False
    finally:
        try:
            if lock_path.read_text(encoding="utf-8") == token:
                lock_path.unlink()
        except OSError:
            pass


class JsonRpc:
    def __init__(self, url: str | tuple[str, ...], client: httpx.AsyncClient) -> None:
        urls = (url,) if isinstance(url, str) else url
        self._urls = tuple(dict.fromkeys(item for item in urls if item))
        if not self._urls:
            raise ValueError("at least one RPC endpoint is required")
        self._url_index = 0
        self._client = client
        self._next_id = 1
        self.trace: list[dict[str, Any]] = []

    async def call(self, method: str, params: list[Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        tried: set[int] = set()
        last_failure = "no_available_endpoint"
        for _attempt in range(len(self._urls)):
            index = self._next_available_index(tried)
            if index is None:
                break
            tried.add(index)
            url = self._urls[index]
            try:
                response = await self._client.post(
                    url,
                    json={
                        "jsonrpc": "2.0", "id": request_id,
                        "method": method, "params": params,
                    },
                )
            except httpx.TransportError as exc:
                last_failure = "transport"
                self.trace.append({
                    "method": method,
                    "transport_error": type(exc).__name__,
                    "http_status": None,
                    "failure_kind": "transient",
                    "retrying": len(tried) < len(self._urls),
                })
                self._url_index = (index + 1) % len(self._urls)
                continue

            try:
                body: Any = response.json()
            except ValueError:
                body = None

            if response.status_code >= 400:
                error = body.get("error") if isinstance(body, dict) else None
                message = (
                    error.get("message") if isinstance(error, dict)
                    else response.text
                )
                kind = _rpc_failure_kind(
                    http_status=response.status_code, message=message
                )
                retryable = kind != "fatal"
                last_failure = kind
                self.trace.append({
                    "method": method,
                    "transport_error": "HTTPStatusError",
                    "http_status": response.status_code,
                    "failure_kind": kind,
                    "retrying": retryable and len(tried) < len(self._urls),
                })
                _note_endpoint_failure(url, kind)
                if kind == "credits_exhausted":
                    _persist_credit_exhausted(url)
                if retryable:
                    self._url_index = (index + 1) % len(self._urls)
                    continue
                raise RpcError(f"{method}: HTTP failure") from None

            if not isinstance(body, dict):
                last_failure = "invalid_protocol"
                self.trace.append({
                    "method": method,
                    "protocol_error": "non_object",
                    "failure_kind": "transient",
                    "retrying": len(tried) < len(self._urls),
                })
                self._url_index = (index + 1) % len(self._urls)
                continue

            if body.get("error") is not None:
                error = body["error"]
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else str(error)
                safe_error = {"code": code, "message": _safe_rpc_message(message)}
                kind = _rpc_failure_kind(code=code, message=message)
                retryable = kind != "fatal"
                last_failure = kind
                self.trace.append({
                    "method": method,
                    "error": safe_error,
                    "failure_kind": kind,
                    "retrying": retryable and len(tried) < len(self._urls),
                })
                _note_endpoint_failure(url, kind)
                if kind == "credits_exhausted":
                    _persist_credit_exhausted(url)
                if retryable:
                    self._url_index = (index + 1) % len(self._urls)
                    continue
                raise RpcError(f"{method}: RPC error") from None

            result = body.get("result")
            self._url_index = index
            _note_endpoint_success(url)
            self.trace.append({"method": method, "result": result})
            return result
        raise RpcError(f"{method}: all RPC endpoints failed ({last_failure})")

    def _next_available_index(self, tried: set[int]) -> int | None:
        for step in range(len(self._urls)):
            index = (self._url_index + step) % len(self._urls)
            if index not in tried and _endpoint_available(self._urls[index]):
                return index
        return None


def _solana_rpc_urls(
    fallback_url: str, *, rotate_start: bool = False
) -> tuple[str, ...]:
    """يعيد مفاتيح Helius المفعّلة، مع بداية دائرية اختيارية لكل فحص.

    لا تُخزّن العناوين في النتائج أو السجل؛ `JsonRpc.trace` يحفظ method/status فقط.
    المسار يضبط محلياً عبر AOI_HELIUS_KEYS_PATH ولا يُفترض داخل المستودع.
    """
    store_path = os.getenv("AOI_HELIUS_KEYS_PATH", "").strip()
    if not store_path:
        return (fallback_url,)
    try:
        payload = json.loads(Path(store_path).read_text(encoding="utf-8"))
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            return (fallback_url,)
        store_urls: list[str] = []
        for item in keys:
            if not isinstance(item, dict) or item.get("disabled") or item.get("disabledAt"):
                continue
            api_key = str(item.get("apiKey") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{16,}", api_key):
                continue
            store_urls.append(HELIUS_HTTP_BASE + api_key)
        # المخزن المضبوط هو مصدر الحقيقة: الحذف/التعطيل في اللوحة يسري في الدورة
        # التالية حتى لو بقي المفتاح القديم داخل AOI_SOLANA_RPC_URL.
        if not store_urls:
            return ()
        # إن كان fallback ما زال مفعّلاً نبدأ به ثم نكمل دائرياً؛ وإلّا نبدأ بأول
        # مفتاح مفعّل، فلا يبقى مفتاح حُذف أو عُطّل مستخدماً من البيئة القديمة.
        if fallback_url in store_urls:
            index = store_urls.index(fallback_url)
            store_urls = store_urls[index:] + store_urls[:index]
        if rotate_start and len(store_urls) > 1:
            global _SOLANA_POOL_CURSOR
            start = _SOLANA_POOL_CURSOR % len(store_urls)
            _SOLANA_POOL_CURSOR = (_SOLANA_POOL_CURSOR + 1) % len(store_urls)
            store_urls = store_urls[start:] + store_urls[:start]
    except (OSError, ValueError, TypeError):
        return (fallback_url,)
    return tuple(dict.fromkeys(store_urls))


async def _optional(rpc: JsonRpc, method: str, params: list[Any]) -> Any | None:
    try:
        return await rpc.call(method, params)
    except RpcError:
        return None


def _status(blocked: list[str], review: list[str], unknown: list[str]) -> str:
    if blocked:
        return "blocked"
    if unknown:
        return "unknown"
    if review:
        return "review"
    return "pass"


def _result_base(kind: str, chain_id: str | None = None) -> dict[str, Any]:
    return {
        "chain_kind": kind,
        "rpc_chain_id": chain_id,
        "gate_status": "unknown",
        "reason_codes": [],
        "contract_exists": None,
        "token_standard": None,
        "program_or_implementation": None,
        "owner_authority": None,
        "owner_renounced": None,
        "mint_authority": None,
        "freeze_authority": None,
        "paused": None,
        "upgradeable": None,
        "dangerous_capabilities": [],
        "transfer_simulation_status": "not_attempted",
        "top1_account_pct": None,
        "top10_accounts_pct": None,
        "details": {},
    }


async def scan_chain_token(
    token_address: str,
    network_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """يفحص عنواناً على شبكته ويعيد قياسات مشتقّة + أثر RPC خام."""
    spec = network_spec(str(network_id))
    if spec is None:
        out = _result_base("unsupported")
        out.update(gate_status="unsupported", reason_codes=["unsupported_network"])
        out["raw"] = {"rpc": []}
        return out
    if not spec.rpc_url:
        out = _result_base(spec.kind, str(spec.rpc_chain_id) if spec.rpc_chain_id else None)
        out.update(gate_status="unknown", reason_codes=["rpc_not_configured"])
        out["raw"] = {"rpc": []}
        return out

    rpc_urls = (
        _solana_rpc_urls(spec.rpc_url, rotate_start=True)
        if spec.kind == "solana" else (spec.rpc_url,)
    )
    if not rpc_urls:
        out = _result_base(spec.kind, str(spec.rpc_chain_id) if spec.rpc_chain_id else None)
        out.update(gate_status="unknown", reason_codes=["no_active_rpc_keys"])
        out["raw"] = {"rpc": []}
        return out
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))
    rpc = JsonRpc(rpc_urls, http)
    try:
        if spec.kind == "solana":
            out = await scan_solana(token_address, rpc)
        else:
            out = await scan_evm(token_address, rpc, int(spec.rpc_chain_id or 0))
    except Exception as exc:  # noqa: BLE001 — fail-closed والعملة لا تُسقط الشريحة
        out = _result_base(spec.kind, str(spec.rpc_chain_id) if spec.rpc_chain_id else None)
        out.update(
            gate_status="unknown",
            reason_codes=["scanner_error"],
            details={"error_type": type(exc).__name__},
        )
    finally:
        if owns_client:
            await http.aclose()
    out["raw"] = {"rpc": rpc.trace}
    return out


async def scan_solana(mint: str, rpc: JsonRpc) -> dict[str, Any]:
    out = _result_base("solana")
    blocked: list[str] = []
    review: list[str] = []
    unknown: list[str] = []

    account = await rpc.call(
        "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
    )
    value = account.get("value") if isinstance(account, dict) else None
    if not isinstance(value, dict):
        out.update(contract_exists=0, gate_status="blocked", reason_codes=["mint_not_found"])
        return out
    out["contract_exists"] = 1
    program_id = value.get("owner")
    out["program_or_implementation"] = program_id
    if program_id not in {SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
        out.update(
            gate_status="blocked",
            reason_codes=["unrecognized_token_program"],
            details={"account_owner": program_id},
        )
        return out

    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict) or parsed.get("type") != "mint":
        out.update(gate_status="unknown", reason_codes=["mint_parse_failed"])
        return out

    standard = "token-2022" if program_id == TOKEN_2022_PROGRAM else "spl-token"
    out["token_standard"] = standard
    out["mint_authority"] = info.get("mintAuthority")
    out["freeze_authority"] = info.get("freezeAuthority")
    if not info.get("isInitialized", False):
        blocked.append("mint_not_initialized")
    if out["mint_authority"]:
        review.append("mint_authority_active")
    if out["freeze_authority"]:
        review.append("freeze_authority_active")

    extensions = info.get("extensions") if isinstance(info.get("extensions"), list) else []
    dangerous: list[str] = []
    benign_extensions = {"immutableOwner", "metadataPointer", "tokenMetadata"}
    extension_details: dict[str, Any] = {}
    for extension in extensions:
        if not isinstance(extension, dict):
            review.append("unparsed_token_extension")
            continue
        name = str(extension.get("extension") or "")
        state = extension.get("state") if isinstance(extension.get("state"), dict) else {}
        extension_details[name] = state
        lowered = name.lower()
        if lowered == "nontransferable":
            blocked.append("non_transferable")
            dangerous.append(name)
        elif lowered == "defaultaccountstate":
            account_state = str(state.get("state") or "").lower()
            if account_state == "frozen":
                blocked.append("default_account_frozen")
                dangerous.append(name)
        elif lowered == "pausable":
            dangerous.append(name)
            if state.get("paused") is True:
                blocked.append("token_paused")
                out["paused"] = 1
            elif state.get("authority"):
                review.append("pausable_authority_active")
                out["paused"] = 0
        elif lowered == "transferhook":
            if state.get("authority") or state.get("programId"):
                dangerous.append(name)
                review.append("transfer_hook_configurable")
        elif lowered == "transferfeeconfig":
            dangerous.append(name)
            review.append("transfer_fee_config_present")
        elif lowered == "permanentdelegate":
            if state.get("delegate"):
                dangerous.append(name)
                review.append("permanent_delegate_active")
        elif lowered in {
            "mintcloseauthority", "confidentialtransfermint", "confidentialmintburn",
            "scaleduiamountconfig", "interestbearingconfig", "permissionedburn",
        }:
            dangerous.append(name)
            review.append(f"extension_{lowered}")
        elif name not in benign_extensions:
            dangerous.append(name or "unknown")
            review.append("unknown_token_extension")

    # سلطة metadata لا تمنع البيع مباشرة، لكنها تسمح بتبديل هوية الأصل.
    for extension in extensions:
        if not isinstance(extension, dict) or extension.get("extension") != "tokenMetadata":
            continue
        state = extension.get("state") if isinstance(extension.get("state"), dict) else {}
        if state.get("updateAuthority"):
            review.append("metadata_update_authority_active")

    out["dangerous_capabilities"] = sorted(set(dangerous))
    supply_raw = info.get("supply")
    try:
        supply = int(supply_raw)
    except (TypeError, ValueError):
        supply = 0
        unknown.append("invalid_supply")

    largest_values: list[dict[str, Any]] = []
    largest_rpc_failed = False
    try:
        largest = await rpc.call(
            "getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]
        )
    except RpcError:
        largest = None
        largest_rpc_failed = True
    if isinstance(largest, dict) and isinstance(largest.get("value"), list):
        largest_values = [v for v in largest["value"] if isinstance(v, dict)]
        amounts = []
        for item in largest_values:
            try:
                amounts.append(int(item.get("amount") or 0))
            except (TypeError, ValueError):
                amounts.append(0)
        if supply > 0 and amounts:
            out["top1_account_pct"] = amounts[0] / supply * 100
            out["top10_accounts_pct"] = sum(amounts[:10]) / supply * 100

    simulation = (
        {"status": "unavailable", "reason": "largest_accounts_rpc_failed"}
        if largest_rpc_failed
        else await _simulate_solana_transfer(
            rpc, mint, program_id, int(info.get("decimals") or 0), largest_values
        )
    )
    out["transfer_simulation_status"] = simulation["status"]
    if simulation["status"] != "success":
        if simulation.get("reason") in {
            "largest_accounts_rpc_failed",
            "token_accounts_unreadable",
            "simulation_rpc_failed",
        }:
            unknown.append("holder_transfer_check_unavailable")
        else:
            review.append("holder_transfer_not_proven")

    out["details"] = {
        "extensions": extension_details,
        "space": value.get("space"),
        "supply": supply_raw,
        "decimals": info.get("decimals"),
        "holder_concentration_scope": "token_accounts_including_pools",
        "transfer_simulation": simulation,
    }
    out["reason_codes"] = sorted(set(blocked + unknown + review))
    out["gate_status"] = _status(blocked, review, unknown)
    return out


async def _simulate_solana_transfer(
    rpc: JsonRpc,
    mint: str,
    program_id: str,
    decimals: int,
    largest: list[dict[str, Any]],
) -> dict[str, Any]:
    addresses = [str(v.get("address")) for v in largest[:8] if v.get("address")]
    if len(addresses) < 2:
        return {"status": "unavailable", "reason": "fewer_than_two_token_accounts"}
    accounts = await _optional(
        rpc, "getMultipleAccounts", [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}]
    )
    values = accounts.get("value") if isinstance(accounts, dict) else None
    if not isinstance(values, list):
        return {"status": "unavailable", "reason": "token_accounts_unreadable"}

    candidates: list[tuple[str, str]] = []
    for address, value in zip(addresses, values, strict=False):
        data = value.get("data") if isinstance(value, dict) else None
        parsed = data.get("parsed") if isinstance(data, dict) else None
        info = parsed.get("info") if isinstance(parsed, dict) else None
        owner = info.get("owner") if isinstance(info, dict) else None
        if owner:
            candidates.append((address, str(owner)))
    if len(candidates) < 2:
        return {"status": "unavailable", "reason": "owners_unresolved"}

    source, owner = candidates[0]
    destination = candidates[1][0]
    try:
        transaction = _build_solana_transfer_checked(
            source, mint, destination, owner, program_id, decimals
        )
    except ValueError:
        return {"status": "unavailable", "reason": "invalid_base58_account"}
    simulated = await _optional(
        rpc,
        "simulateTransaction",
        [
            base64.b64encode(transaction).decode("ascii"),
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "confirmed",
            },
        ],
    )
    value = simulated.get("value") if isinstance(simulated, dict) else None
    if not isinstance(value, dict):
        return {"status": "unavailable", "reason": "simulation_rpc_failed"}
    if value.get("err") is not None:
        return {
            "status": "failed",
            "reason": "token_transfer_rejected",
            "error": value.get("err"),
            "logs": value.get("logs"),
        }
    return {"status": "success", "units_consumed": value.get("unitsConsumed")}


def _build_solana_transfer_checked(
    source: str,
    mint: str,
    destination: str,
    owner: str,
    program_id: str,
    decimals: int,
) -> bytes:
    keys = [owner, source, destination, mint, program_id]
    key_bytes = [_b58decode(k) for k in keys]
    if any(len(k) != 32 for k in key_bytes):
        raise ValueError("Solana public key must decode to 32 bytes")
    message = bytearray([1, 0, 2])
    message.extend(_shortvec(len(key_bytes)))
    for key in key_bytes:
        message.extend(key)
    message.extend(b"\0" * 32)  # يستبدله simulateTransaction حديثاً
    message.extend(_shortvec(1))
    message.append(4)  # token program index
    message.extend(_shortvec(4))
    message.extend(bytes([1, 3, 2, 0]))  # source, mint, destination, owner
    instruction = bytes([12]) + struct.pack("<Q", 1) + bytes([decimals])
    message.extend(_shortvec(len(instruction)))
    message.extend(instruction)
    return _shortvec(1) + b"\0" * 64 + bytes(message)


def _shortvec(value: int) -> bytes:
    out = bytearray()
    while True:
        elem = value & 0x7F
        value >>= 7
        if value:
            elem |= 0x80
        out.append(elem)
        if not value:
            return bytes(out)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: i for i, char in enumerate(_B58_ALPHABET)}


def _b58decode(value: str) -> bytes:
    number = 0
    try:
        for char in value:
            number = number * 58 + _B58_INDEX[char]
    except KeyError as exc:
        raise ValueError("invalid base58") from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def _valid_evm_address(address: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", address))


def _word_address(value: Any, *, keep_zero: bool = False) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        return None
    address = "0x" + value[-40:].lower()
    return address if keep_zero or address != ZERO_EVM_ADDRESS else None


def _word_int(value: Any) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _address_arg(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


async def _eth_call(
    rpc: JsonRpc, token: str, data: str, *, from_address: str | None = None
) -> Any | None:
    call: dict[str, str] = {"to": token, "data": data}
    if from_address:
        call["from"] = from_address
    return await _optional(rpc, "eth_call", [call, "latest"])


def _call_success(value: Any) -> bool:
    if value == "0x":  # ERC-20 قديم لا يعيد bool
        return True
    parsed = _word_int(value)
    return parsed is not None and parsed != 0


async def scan_evm(token: str, rpc: JsonRpc, expected_chain_id: int) -> dict[str, Any]:
    out = _result_base("evm", str(expected_chain_id))
    blocked: list[str] = []
    review: list[str] = []
    unknown: list[str] = []
    if not _valid_evm_address(token):
        out.update(gate_status="blocked", reason_codes=["invalid_contract_address"])
        return out
    token = token.lower()

    actual_chain_hex = await rpc.call("eth_chainId", [])
    actual_chain = _word_int(actual_chain_hex)
    if actual_chain != expected_chain_id:
        out.update(
            gate_status="unknown", reason_codes=["rpc_chain_id_mismatch"],
            details={"expected_chain_id": expected_chain_id, "actual_chain_id": actual_chain},
        )
        return out

    code = await rpc.call("eth_getCode", [token, "latest"])
    if not isinstance(code, str) or code in {"0x", "0x0"}:
        out.update(contract_exists=0, gate_status="blocked", reason_codes=["contract_not_found"])
        return out
    out["contract_exists"] = 1
    out["token_standard"] = "erc20"

    proxy_type: str | None = None
    implementation: str | None = _minimal_proxy_implementation(code)
    admin: str | None = None
    beacon: str | None = None
    if implementation:
        proxy_type = "eip1167"
    else:
        impl_word = await _optional(rpc, "eth_getStorageAt", [token, EIP1967_IMPLEMENTATION_SLOT, "latest"])
        implementation = _word_address(impl_word)
        admin = _word_address(
            await _optional(rpc, "eth_getStorageAt", [token, EIP1967_ADMIN_SLOT, "latest"])
        )
        beacon = _word_address(
            await _optional(rpc, "eth_getStorageAt", [token, EIP1967_BEACON_SLOT, "latest"])
        )
        if implementation:
            proxy_type = "eip1967"
        elif beacon:
            proxy_type = "beacon"

    inspect_code = code
    if implementation:
        impl_code = await _optional(rpc, "eth_getCode", [implementation, "latest"])
        if isinstance(impl_code, str) and len(impl_code) > 2:
            inspect_code += impl_code
        else:
            unknown.append("proxy_implementation_unreadable")
    if proxy_type:
        review.append("upgradeable_or_delegating_contract")
        out["upgradeable"] = 1
    else:
        out["upgradeable"] = 0
    if admin:
        review.append("proxy_admin_active")
    out["program_or_implementation"] = implementation

    total_supply = _word_int(await _eth_call(rpc, token, "0x18160ddd"))
    decimals = _word_int(await _eth_call(rpc, token, "0x313ce567"))
    if total_supply is None or total_supply <= 0:
        unknown.append("erc20_total_supply_unreadable")
    if decimals is None or decimals > 255:
        unknown.append("erc20_decimals_unreadable")

    owner_raw = await _eth_call(rpc, token, "0x8da5cb5b")
    owner = _word_address(owner_raw, keep_zero=True)
    if owner is None:
        owner_raw = await _eth_call(rpc, token, "0x893d20e8")
        owner = _word_address(owner_raw, keep_zero=True)
    out["owner_authority"] = owner
    if owner:
        renounced = owner in {ZERO_EVM_ADDRESS, DEAD_EVM_ADDRESS}
        out["owner_renounced"] = 1 if renounced else 0
        if not renounced:
            review.append("owner_authority_active")

    paused_value = _word_int(await _eth_call(rpc, token, "0x5c975abb"))
    if paused_value is not None:
        out["paused"] = 1 if paused_value else 0
        if paused_value:
            blocked.append("contract_paused")
    trading_enabled = _word_int(await _eth_call(rpc, token, "0x4ada218b"))
    if trading_enabled == 0:
        blocked.append("trading_disabled")
    limits = _word_int(await _eth_call(rpc, token, "0x4a62bb65"))
    if limits:
        review.append("transaction_limits_active")
    max_tx = _word_int(await _eth_call(rpc, token, "0xc8c8ebe4"))
    if max_tx is not None and total_supply and max_tx / total_supply < 0.01:
        review.append("max_transaction_below_one_percent")

    code_lower = inspect_code.lower().removeprefix("0x")
    capabilities = sorted(
        signature for selector, signature in RISKY_SELECTORS.items() if selector in code_lower
    )
    out["dangerous_capabilities"] = capabilities
    if capabilities:
        review.append("administrative_selectors_present")

    transfer_sim = await _simulate_evm_holder_transfer(rpc, token)
    out["transfer_simulation_status"] = transfer_sim["status"]
    if transfer_sim["status"] == "failed":
        # A reverted eth_call only proves that this exact sender/amount/state failed.
        # The holder may have spent its balance between balanceOf and eth_call, and
        # the zero-amount fallback is optional ERC-20 behaviour.  Keep fail-closed,
        # but do not claim a global sell block without an explicit contract signal.
        review.append("holder_transfer_rejected")
    elif transfer_sim["status"] != "success":
        review.append("holder_transfer_not_proven")

    out["details"] = {
        "expected_chain_id": expected_chain_id,
        "actual_chain_id": actual_chain,
        "code_bytes": max(0, (len(code) - 2) // 2),
        "proxy_type": proxy_type,
        "proxy_admin": admin,
        "proxy_beacon": beacon,
        "total_supply": total_supply,
        "decimals": decimals,
        "trading_enabled": trading_enabled,
        "limits_in_effect": limits,
        "max_transaction_amount": max_tx,
        "transfer_simulation": transfer_sim,
    }
    out["reason_codes"] = sorted(set(blocked + unknown + review))
    out["gate_status"] = _status(blocked, review, unknown)
    return out


def _minimal_proxy_implementation(code: str) -> str | None:
    raw = code.lower().removeprefix("0x")
    match = re.fullmatch(
        r"363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3", raw
    )
    return "0x" + match.group(1) if match else None


async def _simulate_evm_holder_transfer(rpc: JsonRpc, token: str) -> dict[str, Any]:
    holder: str | None = None
    latest_hex = await _optional(rpc, "eth_blockNumber", [])
    latest = _word_int(latest_hex)
    if latest is not None:
        logs = await _optional(
            rpc,
            "eth_getLogs",
            [{
                "address": token,
                "fromBlock": hex(max(0, latest - 2_000)),
                "toBlock": "latest",
                "topics": [TRANSFER_TOPIC],
            }],
        )
        if isinstance(logs, list):
            for log in reversed(logs[-100:]):
                topics = log.get("topics") if isinstance(log, dict) else None
                candidate = _word_address(topics[2]) if isinstance(topics, list) and len(topics) > 2 else None
                if not candidate or candidate in {ZERO_EVM_ADDRESS, DEAD_EVM_ADDRESS}:
                    continue
                balance = _word_int(
                    await _eth_call(rpc, token, "0x70a08231" + _address_arg(candidate))
                )
                if balance and balance > 0:
                    holder = candidate
                    break

    if holder:
        data = "0xa9059cbb" + _address_arg(DEAD_EVM_ADDRESS) + hex(1)[2:].rjust(64, "0")
        result = await _eth_call(rpc, token, data, from_address=holder)
        return {
            "status": "success" if _call_success(result) else "failed",
            "kind": "holder_transfer_one_unit",
            "holder": holder,
        }

    # فحص أضعف عند غياب logs: كثير من العقود تسمح بتحويل صفر حتى من بلا رصيد.
    # نحفظه كـlimited ولا نرفعه إلى success كي لا يبدو إثبات بيع.
    sender = "0x" + "0" * 39 + "1"
    data = "0xa9059cbb" + _address_arg(DEAD_EVM_ADDRESS) + "0" * 64
    result = await _eth_call(rpc, token, data, from_address=sender)
    return {
        "status": "limited" if _call_success(result) else "failed",
        "kind": "zero_amount_transfer_only",
    }


def dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
