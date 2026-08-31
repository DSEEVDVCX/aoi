"""Managing provider keys from the dashboard: read, add, suspend, delete, and live-test.

This is the first write capability the dashboard has had since it became
read-only, so its limits are drawn precisely:

- The target is the `recorder/chain_keys.json` file alone. The database stays
  `mode=ro` as it is — not one write line to it, so no contention with the
  writer's lock.
- The file's format is not defined here but in `recorder/key_file.py`, and
  it's loaded by an explicit path, not via `sys.path`: the two projects each
  have a `config.py` with the same name, so adding the recorder's folder to
  the path would have made the `config` import order-dependent.
- The value is never returned. What leaves here: the account name, the last
  four characters (`key_file.tail` — a deliberate FR-013 relaxation the user
  requested for telling keys apart), and gauges.
- And the value is never logged: the test messages have the key redacted
  from them before display, because provider errors often return the full URL
  with the key in it.
"""
from __future__ import annotations

import hmac
import importlib.util
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

import config
import httpx


def _load_key_file():
    """Loads `recorder/key_file.py` by an explicit path under a unique name.

    Under a unique name (`aoi_key_file`), not `key_file`: if the dashboard ever
    imports a module by that name, the two won't fight over `sys.modules`.
    """
    path = os.path.join(config.RECORDER_DIR, "key_file.py")
    spec = importlib.util.spec_from_file_location("aoi_key_file", path)
    if spec is None or spec.loader is None:  # pragma: no cover - missing path
        raise RuntimeError(f"failed to load the key file format: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


key_file = _load_key_file()

# One lock per modification: a modification is a read then a write, and two
# concurrent requests from the same dashboard would have read the same state,
# silently dropping one of the changes.
_LOCK = threading.Lock()

# Live probe results — in memory, not in the database or the file. The reason:
# they're point-in-time check results, and writing them to the database would
# have dragged the tail into a file that gets backed up. They're lost on a
# dashboard restart, and that's acceptable: re-checking is one click.
_PROBES: dict[tuple[str, str], dict[str, Any]] = {}

# The account name: a short human text. We forbid control characters and tag
# brackets so it can't become a tag in the page, and truncate it so it doesn't
# crowd the row.
_LABEL_MAX = 60
_BAD_LABEL = re.compile(r"[\x00-\x1f<>]")

# The key: one value with no whitespace. The two bounds prevent a bad paste (a
# whole line from a .env file, say) before it becomes a dead key in the pool
# getting thawed every cycle.
_KEY_MIN, _KEY_MAX = 8, 200


class KeyStoreError(RuntimeError):
    """A rejected request, with a reason shown to the user as-is."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _meta(provider: str) -> dict[str, Any]:
    try:
        return key_file.PROVIDERS[provider]
    except KeyError:
        raise KeyStoreError(f"unknown provider: {provider}", 404) from None


def _path() -> str:
    return config.CHAIN_KEYS_PATH


def _env_override(provider: str) -> bool:
    """Does the environment variable take precedence over the file for this provider?

    If it's set, editing the file does nothing: `provider_keys.read_keys`
    returns from the environment before ever opening the file. So silence here
    would have meant "I added a key and it doesn't work" with no visible reason.
    """
    return bool(os.environ.get(_meta(provider)["env"], "").strip())


def _entries(provider: str) -> list[dict[str, Any]]:
    meta = _meta(provider)
    return key_file.load_entries(_path(), meta["plural"], meta["singular"])


def _write(provider: str, rows: list[dict[str, Any]]) -> None:
    meta = _meta(provider)
    try:
        key_file.save_entries(_path(), meta["plural"], meta["singular"], rows)
    except (OSError, ValueError) as exc:
        raise KeyStoreError(f"failed to write: {type(exc).__name__}", 500) from exc


def _clean_label(label: str) -> str:
    text = _BAD_LABEL.sub(" ", str(label or "")).strip()
    return text[:_LABEL_MAX]


def _clean_key(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        raise KeyStoreError("The key is empty or contains spaces — paste it alone")
    if not (_KEY_MIN <= len(text) <= _KEY_MAX):
        raise KeyStoreError(f"Unreasonable key length ({len(text)} characters)")
    return text


def _locate(
    provider: str, slot: int, expect_tail: str, expect_id: str = "",
) -> tuple[list[dict], int]:
    """Finds a row by slot and verifies the tail before modifying it.

    The slot alone isn't enough: between rendering the page and pressing the
    button the file may have changed (a manual edit, or another browser tab),
    so slot 1 becomes a different key and the healthy one gets deleted
    instead of the intended one. So we match the tail the row was rendered
    with — a comparison, not a disclosure: the user already has it in their page.
    """
    rows = _entries(provider)
    if not 0 <= slot < len(rows):
        raise KeyStoreError("The file changed — reload the page", 409)
    if key_file.tail(rows[slot]["key"]) != str(expect_tail or ""):
        raise KeyStoreError("The file changed — reload the page", 409)
    if expect_id:
        if _key_id(rows[slot]["key"]) != expect_id:
            raise KeyStoreError("The file changed — reload the page", 409)
    elif sum(key_file.tail(row["key"]) == str(expect_tail or "") for row in rows) > 1:
        raise KeyStoreError("The key's last characters are not unique — reload the page", 409)
    return rows, slot


def _redact(text: str, key: str) -> str:
    """Redacts the key from a text before displaying it. Same lesson as `solana_rpc._redact`."""
    out = str(text)
    if key:
        out = out.replace(key, "<redacted>")
    return re.sub(r"(api-key=|Bearer\s+)[^\s\"'&)>]+", r"\1<redacted>", out)


# A random salt generated once per process that never leaves memory.
_ID_SALT = secrets.token_bytes(32)


def _key_id(key: str) -> str:
    """An identity that tells one key from another: stable within the process, meaningless outside it.

    The four-character tail isn't enough as an identity — two keys ending in
    `wxyz` make `_locate` match the wrong one and delete the healthy key, and
    a probe result for one of them colors the other. So we needed a unique
    identifier sent along with the button.

    And it is **not** a fingerprint of the key: `sha256(key)` would have done
    the same job while breaking the rule ("no value, no fragment, no
    fingerprint"), because it works as a test oracle — anyone holding a list
    of candidate keys can confirm which one is in use here — and as a link
    matching the same key across two systems. HMAC with a random salt,
    though, produces an opaque random token: no value can be derived from it
    and nothing outside this process can be matched against it.

    And the salt dies with the process, so identities are voided at boot. And
    that is correct, not a flaw: a page opened before the boot gets reloaded
    rather than having its button trusted, and the key panel is re-rendered
    on every refresh cycle, so the window is seconds.
    """
    return hmac.new(_ID_SALT, key.strip().encode("utf-8"), "sha256").hexdigest()[:32]


# --- Display ---
def _pool_view(pool_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Gathers each provider's state from the pools of all owning processes.

    A provider can exist in two processes with two independent states (that's
    how GoldRush was in `FomoChain` and `FomoEVMReplay`), so we aggregate, we
    don't pick: a slot cooled in either one is really cooled, and naming the
    owner makes the reason understandable instead of a "cooled" with no source.
    """
    view: dict[str, dict[str, Any]] = {}
    for row in pool_rows:
        state = view.setdefault(
            row["provider"], {"cooled": {}, "in_use": set(), "disabled_by": [], "owners": []},
        )
        state["owners"].append(row["owner"])
        if row.get("disabled"):
            state["disabled_by"].append(row["owner"])
        # A stale report describes the past: cooling and in-use are momentary
        # states that end with the cycle, so coloring a specific key with them
        # from a stale report is an outright lie. The owner and the disabling,
        # though, describe configuration, not a moment, and hiding them would
        # say "healthy" about a provider that's switched off — and the pool row
        # carries the stale flag, so the reader sees the source.
        if row.get("stale"):
            continue
        for index in row.get("blocked_index") or []:
            state["cooled"].setdefault(int(index), []).append(row["owner"])
        if row.get("keys"):
            state["in_use"].add(int(row.get("index") or 0))
    return view


def rows(pool_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The key rows for display: account name, tail, and status. **No value**."""
    pools = _pool_view(pool_rows)
    out: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    for provider, meta in key_file.PROVIDERS.items():
        env = _env_override(provider)
        entries = _entries(provider)
        state = pools.get(provider, {})
        disabled_by = state.get("disabled_by") or []
        providers.append({
            "provider": provider,
            "title": meta["title"],
            "env": env,
            "env_name": meta["env"],
            "keys": len(entries),
            "enabled": sum(1 for row in entries if row["enabled"]),
            "owners": sorted(set(state.get("owners") or [])),
            "disabled_by": sorted(disabled_by),
        })
        # The pool slot is computed over the enabled keys only, in the same
        # order: the pool is built by `read_keys`, which filters out the
        # disabled, so the file's numbering would have shifted the colors.
        pool_slot = 0
        for slot, row in enumerate(entries):
            enabled = row["enabled"]
            cooled_by = sorted(state.get("cooled", {}).get(pool_slot, [])) if enabled else []
            in_use = enabled and pool_slot in (state.get("in_use") or set())
            tail = key_file.tail(row["key"])
            probe = _PROBES.get((provider, _key_id(row["key"])))
            out.append({
                "provider": provider,
                "title": meta["title"],
                "slot": slot,
                "pool_slot": pool_slot if enabled else None,
                "label": row["label"],
                "tail": tail,
                "key_id": _key_id(row["key"]),
                "enabled": enabled,
                "env": env,
                "cooled_by": cooled_by,
                "in_use": in_use,
                "provider_disabled": bool(disabled_by) and enabled,
                "probe": probe,
                "level": (
                    "off" if not enabled
                    else "bad" if (probe and probe["level"] == "bad") or disabled_by
                    else "warn" if cooled_by or (probe and probe["level"] == "warn")
                    else "good"
                ),
            })
            if enabled:
                pool_slot += 1
    return {"keys": out, "providers": providers}


# --- Modification ---
def add(provider: str, key: str, label: str) -> dict[str, Any]:
    value = _clean_key(key)
    name = _clean_label(label)
    with _LOCK:
        if _env_override(provider):
            raise KeyStoreError(
                f"set from the environment ({_meta(provider)['env']}) — the file is ignored there", 409,
            )
        rows_now = _entries(provider)
        if any(row["key"] == value for row in rows_now):
            raise KeyStoreError("this key already exists", 409)
        rows_now.append({"key": value, "label": name, "enabled": True})
        _write(provider, rows_now)
    return {"ok": True, "tail": key_file.tail(value), "keys": len(rows_now)}


def set_enabled(
    provider: str, slot: int, expect_tail: str, enabled: bool, expect_id: str = "",
) -> dict[str, Any]:
    with _LOCK:
        if _env_override(provider):
            raise KeyStoreError(
                f"set from the environment ({_meta(provider)['env']}) — the file is ignored there", 409,
            )
        rows_now, index = _locate(provider, slot, expect_tail, expect_id)
        rows_now[index]["enabled"] = bool(enabled)
        _write(provider, rows_now)
        left = sum(1 for row in rows_now if row["enabled"])
    return {"ok": True, "enabled": bool(enabled), "warning": _shortfall(provider, left)}


def remove(provider: str, slot: int, expect_tail: str, expect_id: str = "") -> dict[str, Any]:
    with _LOCK:
        if _env_override(provider):
            raise KeyStoreError(
                f"set from the environment ({_meta(provider)['env']}) — the file is ignored there", 409,
            )
        rows_now, index = _locate(provider, slot, expect_tail, expect_id)
        rows_now.pop(index)
        _write(provider, rows_now)
        left = sum(1 for row in rows_now if row["enabled"])
    return {"ok": True, "keys": len(rows_now), "warning": _shortfall(provider, left)}


def _shortfall(provider: str, enabled_left: int) -> str | None:
    """A warning after the fact, not a block before it: emptying may be intentional.

    We don't block deleting the last key — the provider's account may really
    have been cancelled — but the effect is stated plainly: the layer will
    stop with a "no keys" error on every cycle.
    """
    if enabled_left:
        return None
    return f"No enabled key left for {_meta(provider)['title']} — the layer will stop"


# --- Live probe ---
def _classify(status: int) -> tuple[str, str]:
    """Separates "the key is rejected" from "the service is down" — the same split as `_step_aside`.

    Conflating them is the costly mistake: showing a red dot on a healthy key
    because the provider was stumbling at probe time pushes the user to delete
    a valid key.
    """
    if status == 200:
        return "good", "healthy"
    if status in (401, 403):
        return "bad", f"rejected — HTTP {status}"
    if status == 402:
        return "bad", "out of credits — HTTP 402"
    if status == 429:
        return "warn", "temporarily rate-limited — HTTP 429 (the key is valid)"
    if status >= 500:
        return "warn", f"the service is down — HTTP {status} (not the key)"
    return "warn", f"HTTP {status}"


def probe(provider: str, slot: int, expect_tail: str, expect_id: str = "") -> dict[str, Any]:
    """One real call with this specific key, to the address the client uses."""
    meta = _meta(provider)
    rows_now, index = _locate(provider, slot, expect_tail, expect_id)
    value = rows_now[index]["key"]
    spec = meta["probe"]
    url = spec["url"].format(key=value)
    headers = {name: text.format(key=value) for name, text in (spec.get("headers") or {}).items()}
    try:
        with httpx.Client(timeout=config.KEY_PROBE_TIMEOUT) as client:
            response = client.request(
                spec["method"], url, headers=headers or None,
                params=spec.get("params"), json=spec.get("json"),
            )
        level, detail = _classify(response.status_code)
        if level == "good":
            # 200 isn't always enough: JSON-RPC answers 200 with an `error` inside.
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error") not in (None, False):
                level, detail = "bad", _redact(str(body["error"])[:120], value)
        status: int | None = response.status_code
    except httpx.TimeoutException:
        level, detail, status = "warn", f"timeout after {config.KEY_PROBE_TIMEOUT:g}s — no response", None
    except httpx.HTTPError as exc:
        level, detail, status = "warn", _redact(f"{type(exc).__name__}", value), None

    result = {
        "level": level,
        "detail": detail,
        "status": status,
        "at": datetime.now(UTC).isoformat(),
    }
    _PROBES[(provider, _key_id(value))] = result
    return result
