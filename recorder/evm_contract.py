"""EVM contract safety — straight from the bytecode, with no verification provider.

**Why from the bytecode rather than the source?** Because every verification provider,
measured, falls short: Etherscan V2 refuses without a key, V1 is deprecated, and
Sourcify knows 1 in 10 tokens. `eth_getCode`, by contrast, is free and guaranteed =>
coverage is 100%, not 10%.

**And the check runs on Base alone**, which is a measurement outcome, not neglect
(2026-08-13, live watch): on BSC 21 of 27 are an identical minimal proxy (EIP-1167)
pointing to just **two implementation contracts**, 20 of them to one, ownership is
renounced in both, and neither has a pause, fee, or limit selector — so the column is
constant and carries no information. On Robinhood 51 of 57 are full contracts, but
their sizes repeat across six identical templates and `owner` is present in 5 of 51.
Base, though: 19 of 22 are full contracts with sizes 135B–14.8KB, `owner` in 7 of 19
and `mint` in 2 => the variation is real, so the column discriminates.

The selectors are computed here with keccak-256 **via an internal implementation**:
neither `pycryptodome` nor `eth-hash` is in the environment, and `hashlib.sha3_256` is
standard SHA3, not original Keccak (the padding differs), so it cannot serve as a
substitute.

Read-only (FR-012).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import config
from db import RecorderDB
from evm_rpc import EVMRateLimit

# ---------------------------------------------------------------------------
# keccak-256 (Keccak-f[1600], rate 136) — the original implementation, not standard SHA3.
# ---------------------------------------------------------------------------
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_ROT = (
    (0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56), (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK if n else x


def _keccak_f(a: list[list[int]]) -> None:
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y] & _MASK)
        a[0][0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    """keccak-256 of bytes. Padding is `0x01` (not `0x06` as in standard SHA3)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    out = bytearray()
    for i in range(4):
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


def selector(signature: str) -> str:
    """`owner()` → `0x8da5cb5b` — the first four bytes of the signature's keccak."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


# The ownership functions we actually call (one call per form; a revert is an answer, not an error).
_OWNER_CALLS = ("owner()", "getOwner()", "_owner()")

# The selectors we look for in the bytecode dispatch table. The set is measured: these
# are exactly the ones whose presence varied on Base; the rest appeared in every
# contract or in none of them.
_RISK_SELECTORS: dict[str, tuple[str, ...]] = {
    "has_mint": ("mint(address,uint256)", "mint(uint256)"),
    "has_pause": ("pause()", "unpause()", "setPaused(bool)"),
    "has_blacklist": (
        "blacklist(address)", "setBlacklist(address,bool)", "addBlackList(address)",
        "isBlacklisted(address)",
    ),
    "has_fee_setter": (
        "setFees(uint256,uint256)", "setFee(uint256)", "setTaxes(uint256,uint256)",
        "setBuyTax(uint256)", "setSellTax(uint256)",
    ),
    "has_limit_setter": (
        "setMaxTxAmount(uint256)", "setMaxWallet(uint256)",
        "setMaxWalletAmount(uint256)", "removeLimits()",
    ),
    "has_trading_switch": (
        "enableTrading()", "openTrading()", "setTradingEnabled(bool)",
        "startTrading()",
    ),
}

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# EIP-1167: a minimal proxy exactly 45 bytes long — a prefix, then a 20-byte address, then a suffix.
# Match both ends, not the length alone: a 45-byte contract with different bytes is not a proxy.
_PROXY_PREFIX = "363d3d373d3d3d363d73"
_PROXY_SUFFIX = "5af43d82803e903d91602b57fd5bf3"


def _code_selectors(code_hex: str) -> set[str]:
    """The function selectors visible in the bytecode.

    The dispatch table compares four bytes against a `PUSH4` before every branch, so a
    contract's selectors are practically every `PUSH4` immediate. Skipping immediates
    is mandatory: a byte with value `0x63` **inside** some other push's data is not an
    opcode, and reading it as one produces phantom selectors that make every contract
    look like it carries everything.
    """
    body = code_hex[2:] if code_hex.startswith("0x") else code_hex
    try:
        code = bytes.fromhex(body)
    except ValueError:
        return set()
    out: set[str] = set()
    i = 0
    n = len(code)
    while i < n:
        op = code[i]
        if op == 0x63 and i + 5 <= n:                  # PUSH4
            out.add("0x" + code[i + 1:i + 5].hex())
            i += 5
            continue
        if 0x60 <= op <= 0x7F:                         # PUSH1..PUSH32
            i += 1 + (op - 0x5F)
            continue
        i += 1
    return out


def analyze_code(code_hex: str) -> dict[str, Any]:
    """Bytecode → the computed `evm_contract` columns (with no extra network call)."""
    body = (code_hex or "0x")[2:] if (code_hex or "0x").startswith("0x") else code_hex
    body = (body or "").lower()
    size = len(body) // 2
    out: dict[str, Any] = {
        "code_size": size,
        "is_proxy": 0,
        "impl_address": None,
        "code_hash": None,
        "function_count": None,
    }
    if size == 0:
        # Not a contract. Not an error and not an absence: the address may be a wallet
        # or a deleted contract (SELFDESTRUCT) — and the zero here is a **measurement**,
        # so it is written.
        return out
    out["code_hash"] = "0x" + keccak256(bytes.fromhex(body)).hex()
    if size == 45 and body.startswith(_PROXY_PREFIX) and body.endswith(_PROXY_SUFFIX):
        out["is_proxy"] = 1
        out["impl_address"] = "0x" + body[20:60]
    sels = _code_selectors(body)
    out["function_count"] = len(sels)
    for col, sigs in _RISK_SELECTORS.items():
        out[col] = 1 if any(selector(s) in sels for s in sigs) else 0
    return out


async def _read_owner(
    rpc: Any, network_id: str, address: str, sleep=asyncio.sleep,
) -> str | None:
    """The first ownership form that answers. A revert is an answer ("no such function"), not an error.

    The zero address is a valid answer, not an absence — it is exactly what "ownership renounced" means.

    And pacing between forms rather than back-to-back calls. But pacing is **not** what
    prevents throttling: a 2026-08-13 measurement against `mainnet.base.org` gave nine
    successful calls then 429, identically at 0.4s and 1.0s spacing => the quota is
    **by count** in a time window, not by spacing. The real protection is
    `EVM_CONTRACT_PER_CYCLE` (two tokens = eight calls) and cutting the step short at
    the first throttle; pacing stays because three attempts in one instant is a
    pointless burst.
    """
    for i, sig in enumerate(_OWNER_CALLS):
        if i:
            await sleep(config.EVM_PACING_SECONDS)
        result = await rpc.eth_call(network_id, address, selector(sig))
        if isinstance(result, str) and len(result) >= 66:
            return "0x" + result[-40:].lower()
    return None


async def run_evm_contract_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep,
) -> dict[str, int]:
    """The slow layer on EVM: contract shape and permissions, catching up over time, on Base alone.

    One row per measurement, not one updated row: `renounceOwnership()` is an **event**
    that lands mid-window, and a single row overwritten onto itself erases that it
    happened. The same defect as `chain_authority`.

    And throttling (429) cuts the whole step short, not just the one token: the public
    node's quota is by count in a window, so everything after the first throttle is
    throttled already — and calls we know will be rejected are paid for with tokens
    wrongly marked as errors.
    """
    stats = {"evm_contract_due": 0, "evm_contract_rows": 0,
             "evm_contract_errors": 0, "evm_contract_proxies": 0,
             # Tokens deferred because the node throttled the quota — not a failure, and no state written.
             "evm_contract_throttled": 0}
    networks = [str(n) for n in config.EVM_CONTRACT_NETWORKS]
    if not networks:
        return stats
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.EVM_CONTRACT_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.EVM_CONTRACT_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.evm_contract_due(
        limit=config.EVM_CONTRACT_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        networks=networks,
    )
    stats["evm_contract_due"] = len(due)

    for i, w in enumerate(due):
        addr = str(w["token_address"])
        net = str(w["network_id"] or "")
        status = "error"
        try:
            code = await rpc.get_code(net, addr)
            shape = analyze_code(code)
            owner = None
            if shape["code_size"] > 0:
                await sleep(config.EVM_PACING_SECONDS)
                owner = await _read_owner(rpc, net, addr, sleep)
            row = {
                "token_address": addr,
                "network_id": net,
                "recorded_at": recorded_at,
                "watch_first_seen_at": w["first_seen_at"],
                "entry_signal_id": w.get("entry_signal_id"),
                "is_control": int(w.get("is_control") or 0),
                "owner_address": owner,
                # No owner => NULL, not 1: "no ownership function" and "ownership
                # renounced" are two different states, and merging them makes the
                # column a lie (FR-007).
                "is_ownership_renounced": (
                    None if owner is None else (1 if owner == _ZERO_ADDRESS else 0)
                ),
                "raw_json": {
                    "code_size": shape["code_size"],
                    "code_hash": shape["code_hash"],
                    "is_proxy": shape["is_proxy"],
                    "impl_address": shape["impl_address"],
                    "owner": owner,
                },
                **{k: v for k, v in shape.items() if k != "raw_json"},
            }
            db.insert_evm_contract(row)
            status = "ok"
            stats["evm_contract_rows"] += 1
            if shape["is_proxy"]:
                stats["evm_contract_proxies"] += 1
        except EVMRateLimit:
            # Throttling is not this token's failure: it is **the end of our quota** in
            # this window, and what follows will be throttled too (measured: nine calls
            # then 429 on `mainnet.base.org` regardless of pacing). So no state is
            # written — writing `error` would push the token behind
            # `EVM_CONTRACT_ERROR_RETRY_SECONDS` (15 minutes) though it is innocent,
            # while with no state it stays first among those due in the next cycle =>
            # guaranteed progress with no loop.
            stats["evm_contract_throttled"] = len(due) - i
            db.note_error("evm_contract_last_throttle_at", recorded_at)
            break
        except Exception as exc:  # noqa: BLE001 — one token does not sink the cycle
            stats["evm_contract_errors"] += 1
            db.note_error(
                "last_error_evm_contract",
                f"{recorded_at}: {addr}: {type(exc).__name__}: {exc}"[:400],
            )
        db.set_evm_contract_state(addr, net, status, recorded_at)
        if i + 1 < len(due):
            await sleep(config.EVM_PACING_SECONDS)
    return stats
