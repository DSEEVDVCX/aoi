"""The on-disk format of the provider keys file — for both reading and writing — **with no local dependencies**.

This module imports neither `config` nor any other module of the project, and that is
deliberate: the dashboard is a separate project with its own `config.py`, so adding
`recorder/` to `sys.path` would make `import config` inside a shared module resolve to
one file or the other depending on order — a breakage that appears late and in only
one process. And because it has no dependencies, the dashboard loads it by explicit
path (`importlib`) without ever touching `sys.path`, so the file format stays
**defined in one place** that the recorder reads and the dashboard writes, not in two
places that quietly drift apart.

The format accepts three shapes per provider, because the existing file was written by hand before there were multiple keys:

    {"helius_api_key": "..."}                        the old singular form
    {"helius_api_keys": ["...", "..."]}              plural
    {"helius_api_keys": [{"key": "...", "label": "main account", "enabled": true}]}

The third shape is what the dashboard writes: `label` is the name of the account the
key was taken from — the only human identifier allowed, since the value itself is
never shown (FR-013). And `enabled` is a temporary off switch: the key stays in the
file but never enters the pool, so a suspect key can be switched off without losing
its value.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

# The three providers that have keys on this machine. `plural`/`singular` are the
# field names in the file (they must match the `provider_keys.read_keys` calls), and
# `env` is the environment variable that **takes precedence over the file** — when it
# is set, editing the file from the dashboard is pointless, and the dashboard must say
# so explicitly instead of writing into a void.
#
# `probe` is the cheapest verification call per provider, and its URL matches what the
# client actually uses (`config.SOLANA_RPC_URL`, `nodereal_rpc._call`) — probing some
# other URL would report "healthy" for a key that fails where it is actually used.
# Guarded by
# `tests/test_key_file.py::test_probe_endpoints_match_the_clients`.
PROVIDERS: dict[str, dict[str, Any]] = {
    "helius": {
        "plural": "helius_api_keys",
        "singular": "helius_api_key",
        "env": "HELIUS_API_KEY",
        "title": "Helius · Solana",
        "probe": {
            "method": "POST",
            "url": "https://mainnet.helius-rpc.com/?api-key={key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        },
    },
    "nodereal": {
        "plural": "nodereal_api_keys",
        "singular": "nodereal_api_key",
        "env": "NODEREAL_API_KEY",
        "title": "NodeReal · BSC",
        "probe": {
            "method": "POST",
            "url": "https://bsc-mainnet.nodereal.io/v1/{key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        },
    },
    # The next two providers build no blocks: they audit the EVM ledger in archive
    # mode (`audit_evm_ledger.py`) because our public endpoints have no archive — the
    # Robinhood node is only ~128 blocks deep. The live layer stays keyless (FR-012).
    "alchemy": {
        "plural": "alchemy_api_keys",
        "singular": "alchemy_api_key",
        "env": "ALCHEMY_API_KEY",
        "title": "Alchemy · EVM archive",
        "probe": {
            "method": "POST",
            # The probe hits Base, not Robinhood: this key serves both, and Base is a
            # stable public slice — so a failed probe means the key, not the network.
            "url": "https://base-mainnet.g.alchemy.com/v2/{key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        },
    },
    "drpc": {
        "plural": "drpc_api_keys",
        "singular": "drpc_api_key",
        "env": "DRPC_API_KEY",
        "title": "dRPC · Base archive",
        "probe": {
            "method": "POST",
            "url": "https://lb.drpc.org/ogrpc?network=base&dkey={key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        },
    },
}

# The shortest key whose tail may be shown. For a short key (≤11) the four-character
# tail is a meaningful part of its substance, so none of it is shown at all: the
# distinguishability is dropped, and the secret stays.
_MIN_LENGTH_FOR_TAIL = 12
TAIL_LENGTH = 4


def tail(key: str) -> str:
    """The last four characters, to tell two keys apart by eye — nothing more, and not a fingerprint.

    Showing a fragment of the secret is a deliberate relaxation of FR-013 that the
    user requested for distinguishability, and it is confined to this one function:
    the tail is never written to a log, to `meta`, or to the pools report — it is
    computed at the dashboard's request and lives inside a single response.
    """
    text = key.strip()
    if len(text) < _MIN_LENGTH_FOR_TAIL:
        return ""
    return text[-TAIL_LENGTH:]


def load(path: str) -> dict[str, Any]:
    """The file's content as-is. Missing and broken are the same thing: an empty dict, not an exception.

    Absence is a normal state (a machine with no keys yet), and breakage must not take
    down the dashboard or the recorder — the writer is the one who should trip, not
    the reader.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def entries(data: dict[str, Any], plural: str, singular: str) -> list[dict[str, Any]]:
    """One provider's rows, normalized: `{key, label, enabled}` — **disabled ones included**.

    The plural shadows the singular even when it is empty: an empty list means "the
    last key was deleted", not "fall back to the old value" — otherwise the deleted
    key would rise from its grave on the very first read.

    The disabled ones are listed here because the dashboard needs to show them in order
    to re-enable them; the pool builder (`provider_keys.read_keys`) filters them out.
    """
    raw = data.get(plural)
    candidates = raw if isinstance(raw, list) else [data.get(singular)]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            key, label, enabled = item.strip(), "", True
        elif isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            label = str(item.get("label") or "").strip()
            # Absence means enabled: a row a human typed by hand without `enabled` is a working key.
            enabled = item.get("enabled", True) is not False
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "label": label, "enabled": enabled})
    return out


def load_entries(path: str, plural: str, singular: str) -> list[dict[str, Any]]:
    return entries(load(path), plural, singular)


def save_entries(
    path: str, plural: str, singular: str, rows: list[dict[str, Any]],
) -> None:
    """Writes one provider's rows and leaves everything else in the file untouched.

    Three precautions, because the recorder reads this file **on every call**:

    1. `os.replace` on a temp file in the same folder — an atomic swap. Writing over
       the original directly leaves a window where the reader sees a half-written file
       and every key disappears: a total outage of collection over a one-key edit.
    2. A check before the swap: re-parse what we wrote and match the enabled keys
       against the intent. A truncated serialization is caught before it becomes the file.
    3. The old singular is deleted on the first write: keeping it beside the plural
       means two sources of truth, and since the plural shadows it, one of them could
       be edited with nothing changing.
    """
    data = load(path)
    normalized = [
        {
            "key": str(row["key"]).strip(),
            "label": str(row.get("label") or "").strip(),
            "enabled": bool(row.get("enabled", True)),
        }
        for row in rows
        if str(row.get("key") or "").strip()
    ]
    data[plural] = normalized
    data.pop(singular, None)

    blob = json.dumps(data, ensure_ascii=False, indent=1)
    check = entries(json.loads(blob), plural, singular)
    if [row["key"] for row in check] != [row["key"] for row in normalized]:
        raise ValueError("write verification failed: the serialized file does not yield the same keys")

    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".keys-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
