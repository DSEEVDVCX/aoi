"""إدارة آمنة لمخزن مفاتيح Helius المشترك مع مشروع crib.

القائمة المعادة للواجهة مقنّعة دائماً. الكتابة ذرّية وتحت قفل ملف متوافق مع
بروتوكول crib حتى لا تضيع تعديلات عمليتين متزامنتين.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

HELIUS_HTTP_BASE = "https://mainnet.helius-rpc.com/?api-key="
# رمز ذو عدد حسابات مناسب لاختبار نفس الاستعلام الذي يعتمد عليه فاحص التركّز.
# لا نستخدم WSOL/USDC هنا لأن Helius يرفض استعلامهما قبل فحص المفتاح لكثرة
# الحسابات (JSON-RPC -32600: Too many accounts requested).
_VERIFY_MINT = "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"
_KEY_PATTERN = re.compile(r"^[0-9A-Za-z-]{20,64}$")
_LOCK_RETRY_SECONDS = 0.05
_LOCK_WAIT_SECONDS = 2.0
_LOCK_STALE_SECONDS = 10.0


class KeyStoreError(RuntimeError):
    """فشل يمنع القراءة/الكتابة الآمنة للمخزن."""


class StoreLockTimeout(KeyStoreError):
    """كاتب آخر احتفظ بالقفل أكثر من المهلة."""


def extract_api_key(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    match = re.search(r"api-key=([^&\s]+)", key, flags=re.IGNORECASE)
    return match.group(1) if match else key


def is_valid_api_key(value: Any) -> bool:
    key = extract_api_key(value)
    return bool(key and _KEY_PATTERN.fullmatch(key))


def mask_api_key(value: Any) -> str:
    key = str(value or "")
    if len(key) <= 8:
        return "•" * len(key)
    hidden = "•" * min(len(key) - 8, 12)
    return f"{key[:4]}{hidden}{key[-4:]}"


def key_id(api_key: str) -> str:
    """نفس hash ذي 32 بت في crib كي تبقى المعرّفات متوافقة."""
    value = 0
    for char in api_key:
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
    return f"k{value:x}"


def _sanitize_store(parsed: Any) -> dict[str, list[dict[str, Any]]]:
    raw_keys = parsed.get("keys") if isinstance(parsed, dict) else None
    if not isinstance(raw_keys, list):
        return {"keys": []}
    keys: list[dict[str, Any]] = []
    for item in raw_keys:
        if not isinstance(item, dict):
            continue
        api_key = extract_api_key(item.get("apiKey"))
        if not api_key or not is_valid_api_key(api_key):
            continue
        keys.append({
            "id": str(item.get("id") or key_id(api_key)),
            "username": str(item.get("username") or "")[:60],
            "apiKey": api_key,
            "addedAt": item.get("addedAt"),
            "disabledAt": item.get("disabledAt"),
            "disabledReason": str(item.get("disabledReason") or "")[:120],
        })
    return {"keys": keys}


def _read_json(path: Path) -> dict[str, list[dict[str, Any]]]:
    return _sanitize_store(json.loads(path.read_text(encoding="utf-8")))


def load_store(path: str | os.PathLike[str]) -> dict[str, list[dict[str, Any]]]:
    target = Path(path)
    if not target.exists():
        return {"keys": []}
    try:
        return _read_json(target)
    except (OSError, ValueError, TypeError) as primary_error:
        backup = Path(str(target) + ".bak")
        try:
            return _read_json(backup)
        except (OSError, ValueError, TypeError):
            raise KeyStoreError(
                f"مخزن مفاتيح Helius تالف ولا توجد نسخة احتياطية سليمة: "
                f"{type(primary_error).__name__}"
            ) from None


def save_store(
    store: dict[str, list[dict[str, Any]]], path: str | os.PathLike[str]
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = _sanitize_store(store)
    backup = Path(str(target) + ".bak")
    if target.exists():
        try:
            _read_json(target)
            shutil.copy2(target, backup)
        except (OSError, ValueError, TypeError):
            pass  # لا نستبدل backup سليمة بملف تالف

    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _store_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    lock_path = Path(str(path) + ".lock")
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
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
                age = time.time() - lock_path.stat().st_mtime
                if age > _LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise StoreLockTimeout("تعذّر الحصول على قفل مخزن المفاتيح")
            time.sleep(_LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        try:
            if lock_path.read_text(encoding="utf-8") == token:
                lock_path.unlink()
        except OSError:
            pass


def update_store(
    path: str | os.PathLike[str],
    mutator: Callable[[dict[str, list[dict[str, Any]]]], Any],
) -> tuple[dict[str, list[dict[str, Any]]], Any]:
    with _store_lock(path):
        store = load_store(path)
        result = mutator(store)
        if result is not False:
            save_store(store, path)
        return store, result


def list_masked(store: dict[str, list[dict[str, Any]]], primary: Any = None) -> list[dict[str, Any]]:
    primary_key = extract_api_key(primary)
    enabled = [item for item in store["keys"] if not item.get("disabledAt")]
    if not any(item["apiKey"] == primary_key for item in enabled):
        primary_key = enabled[0]["apiKey"] if enabled else None
    active_index = 0
    result = []
    for item in store["keys"]:
        disabled = bool(item.get("disabledAt"))
        result.append({
            "id": item["id"],
            "username": item.get("username") or "(بلا اسم)",
            "apiKeyMasked": mask_api_key(item["apiKey"]),
            "addedAt": item.get("addedAt"),
            "disabled": disabled,
            "disabledAt": item.get("disabledAt"),
            "disabledReason": item.get("disabledReason") or "",
            "activeIndex": None if disabled else active_index,
            "primary": not disabled and item["apiKey"] == primary_key,
        })
        if not disabled:
            active_index += 1
    return result


def add_key(store: dict[str, list[dict[str, Any]]], username: Any, value: Any) -> None:
    api_key = extract_api_key(value)
    if not api_key or not is_valid_api_key(api_key):
        raise ValueError("api-key غير صالح؛ ألصق UUID من Helius أو عنوان RPC يحويه")
    if any(item["apiKey"] == api_key for item in store["keys"]):
        raise ValueError("هذا المفتاح مضاف مسبقاً")
    identifier = key_id(api_key)
    used_ids = {item["id"] for item in store["keys"]}
    suffix = 2
    base_identifier = identifier
    while identifier in used_ids:
        identifier = f"{base_identifier}-{suffix}"
        suffix += 1
    store["keys"].append({
        "id": identifier,
        "username": str(username or "").strip()[:60],
        "apiKey": api_key,
        "addedAt": int(time.time() * 1000),
        "disabledAt": None,
        "disabledReason": "",
    })


def find_key(store: dict[str, list[dict[str, Any]]], identifier: Any) -> dict[str, Any] | None:
    return next(
        (item for item in store["keys"] if item["id"] == str(identifier or "")),
        None,
    )


def remove_key(store: dict[str, list[dict[str, Any]]], identifier: Any) -> bool:
    item = find_key(store, identifier)
    if item is None:
        return False
    store["keys"].remove(item)
    return True


def set_key_disabled(
    store: dict[str, list[dict[str, Any]]], identifier: Any, disabled: bool,
    reason: Any = "",
) -> bool:
    item = find_key(store, identifier)
    if item is None:
        return False
    if disabled:
        item["disabledAt"] = int(time.time() * 1000)
        item["disabledReason"] = str(reason or "معطّل يدوياً").strip()[:120]
    else:
        item["disabledAt"] = None
        item["disabledReason"] = ""
    return True


def interpret_verify(status: int | None, body: Any = None, error: str | None = None) -> dict[str, Any]:
    if error:
        return {"ok": False, "status": "network", "message": f"تعذّر الاتصال: {error}"}
    if status in {401, 403}:
        return {
            "ok": False, "status": "unauthorized",
            "message": "المفتاح غير صالح أو غير مصرّح",
        }
    if status == 429:
        return {
            "ok": False, "status": "rate_limited",
            "message": "المفتاح صالح غالباً لكن حصته/معدل طلباته مستنفد الآن (429)",
        }
    if status is not None and 200 <= status < 300:
        if isinstance(body, dict) and body.get("error"):
            rpc_error = body["error"]
            message = rpc_error.get("message", "") if isinstance(rpc_error, dict) else ""
            state = "unauthorized" if re.search(r"api key|unauthor|invalid", message, re.I) else "error"
            return {"ok": False, "status": state, "message": "ردّ Helius بخطأ RPC"}
        if isinstance(body, dict) and body.get("result") is not None:
            return {"ok": True, "status": "ok", "message": "المفتاح يعمل ✓"}
        return {"ok": True, "status": "ok", "message": "استجابة Helius سليمة"}
    return {
        "ok": False, "status": "error",
        "message": f"استجابة غير متوقعة (HTTP {status})",
    }


async def verify_key(api_key: str, timeout_seconds: float = 6.0) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            auth_response = await client.post(
                HELIUS_HTTP_BASE + api_key,
                json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"},
            )
            try:
                auth_body: Any = auth_response.json()
            except ValueError:
                auth_body = None
            auth_result = interpret_verify(auth_response.status_code, auth_body)
            if not auth_result["ok"]:
                return auth_result

            # getSlot يثبت المصادقة فقط؛ بعض المفاتيح تقبله بينما النداء الذي
            # يحتاجه فاحص التركّز يعيد 429. نختبر المسار الحقيقي على رمز مرجعي
            # محدود الحسابات حتى لا تختلط صلاحية المفتاح بضخامة WSOL/USDC.
            response = await client.post(
                HELIUS_HTTP_BASE + api_key,
                json={
                    "jsonrpc": "2.0", "id": 2,
                    "method": "getTokenLargestAccounts",
                    "params": [_VERIFY_MINT, {"commitment": "confirmed"}],
                },
            )
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        result = interpret_verify(response.status_code, body)
        if result["ok"]:
            result["message"] = "المفتاح يعمل لفحص التركّز ✓"
        return result
    except httpx.TimeoutException:
        return interpret_verify(None, error="انتهت المهلة")
    except httpx.HTTPError:
        return interpret_verify(None, error="فشل اتصال HTTP")
