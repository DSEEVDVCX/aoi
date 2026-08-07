"""اختبارات محلّل السلسلة بدوال RPC مزيّفة؛ لا شبكة ولا أسرار."""
from typing import Any

import chain_security as cs


class FakeRpc:
    def __init__(self, handler):
        self.handler = handler
        self.trace = []

    async def call(self, method: str, params: list[Any]):
        value = self.handler(method, params)
        if isinstance(value, Exception):
            raise value
        return value


def _sol_account(extensions=None, *, mint_authority=None, freeze_authority=None):
    return {
        "value": {
            "owner": cs.TOKEN_2022_PROGRAM,
            "space": 300,
            "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6,
                "supply": "1000000",
                "isInitialized": True,
                "mintAuthority": mint_authority,
                "freezeAuthority": freeze_authority,
                "extensions": extensions or [],
            }}},
        }
    }


async def test_solana_non_transferable_is_blocked():
    def handler(method, _params):
        if method == "getAccountInfo":
            return _sol_account([{"extension": "nonTransferable", "state": {}}])
        if method == "getTokenLargestAccounts":
            return {"value": []}
        return cs.RpcError("unsupported")

    result = await cs.scan_solana("mint", FakeRpc(handler))

    assert result["gate_status"] == "blocked"
    assert "non_transferable" in result["reason_codes"]
    assert result["token_standard"] == "token-2022"


async def test_solana_transfer_hook_or_authority_requires_review():
    extension = {
        "extension": "transferHook",
        "state": {"authority": "authority", "programId": None},
    }

    def handler(method, _params):
        if method == "getAccountInfo":
            return _sol_account([extension])
        if method == "getTokenLargestAccounts":
            return {"value": []}
        return cs.RpcError("unsupported")

    result = await cs.scan_solana("mint", FakeRpc(handler))

    assert result["gate_status"] == "review"
    assert "transfer_hook_configurable" in result["reason_codes"]
    assert result["dangerous_capabilities"] == ["transferHook"]


def _evm_handler(
    *, code="0x6000", paused=False, chain=56, owner=None, logs=True,
    transfer_succeeds=True,
):
    holder = "0x" + "1" * 40

    def handler(method, params):
        if method == "eth_chainId":
            return hex(chain)
        if method == "eth_getCode":
            return code
        if method == "eth_getStorageAt":
            return "0x" + "0" * 64
        if method == "eth_blockNumber":
            return "0x1000"
        if method == "eth_getLogs":
            if not logs:
                return []
            return [{"topics": [cs.TRANSFER_TOPIC, "0x" + "0" * 64,
                                "0x" + "0" * 24 + holder[2:]]}]
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x18160ddd":
                return "0x" + hex(1_000_000)[2:].rjust(64, "0")
            if data == "0x313ce567":
                return "0x" + hex(18)[2:].rjust(64, "0")
            if data in {"0x8da5cb5b", "0x893d20e8"}:
                address = owner or cs.ZERO_EVM_ADDRESS
                return "0x" + cs._address_arg(address)
            if data == "0x5c975abb":
                return "0x" + ("1" if paused else "0").rjust(64, "0")
            if data.startswith("0x70a08231"):
                return "0x" + "64".rjust(64, "0")
            if data.startswith("0xa9059cbb"):
                return "0x" + ("1" if transfer_succeeds else "0").rjust(64, "0")
            return None
        raise AssertionError(method)

    return handler


async def test_evm_plain_renounced_contract_with_holder_transfer_passes():
    result = await cs.scan_evm("0x" + "a" * 40, FakeRpc(_evm_handler()), 56)

    assert result["gate_status"] == "pass"
    assert result["owner_renounced"] == 1
    assert result["transfer_simulation_status"] == "success"


async def test_evm_paused_contract_is_blocked():
    result = await cs.scan_evm(
        "0x" + "a" * 40, FakeRpc(_evm_handler(paused=True)), 56
    )

    assert result["gate_status"] == "blocked"
    assert "contract_paused" in result["reason_codes"]


async def test_evm_failed_holder_transfer_requires_review_not_global_block():
    result = await cs.scan_evm(
        "0x" + "a" * 40,
        FakeRpc(_evm_handler(transfer_succeeds=False)),
        56,
    )

    assert result["gate_status"] == "review"
    assert result["transfer_simulation_status"] == "failed"
    assert "holder_transfer_rejected" in result["reason_codes"]


async def test_evm_minimal_proxy_and_active_owner_require_review():
    implementation = "b" * 40
    proxy = "0x363d3d373d3d3d363d73" + implementation + "5af43d82803e903d91602b57fd5bf3"

    def handler(method, params):
        if method == "eth_getCode" and params[0] == "0x" + implementation:
            return "0x600040c10f19"
        return _evm_handler(code=proxy, owner="0x" + "2" * 40)(method, params)

    result = await cs.scan_evm("0x" + "a" * 40, FakeRpc(handler), 56)

    assert result["gate_status"] == "review"
    assert result["upgradeable"] == 1
    assert result["program_or_implementation"] == "0x" + implementation
    assert "mint(address,uint256)" in result["dangerous_capabilities"]
    assert "owner_authority_active" in result["reason_codes"]


async def test_evm_chain_mismatch_is_unknown():
    result = await cs.scan_evm(
        "0x" + "a" * 40, FakeRpc(_evm_handler(chain=8453)), 56
    )

    assert result["gate_status"] == "unknown"
    assert result["reason_codes"] == ["rpc_chain_id_mismatch"]


async def test_evm_missing_contract_is_blocked():
    result = await cs.scan_evm(
        "0x" + "a" * 40, FakeRpc(_evm_handler(code="0x")), 56
    )

    assert result["gate_status"] == "blocked"
    assert result["reason_codes"] == ["contract_not_found"]


def test_fomo_hyperliquid_id_maps_to_hyperevm_chain_999():
    spec = cs.network_spec("1337")
    assert spec is not None
    assert spec.rpc_chain_id == 999


def test_solana_transfer_transaction_is_structurally_serialized():
    tx = cs._build_solana_transfer_checked(
        "11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
        "11111111111111111111111111111111",
        "11111111111111111111111111111111",
        cs.SPL_TOKEN_PROGRAM,
        9,
    )
    assert tx[0] == 1       # توقيع واحد
    assert tx[65:69] == bytes([1, 0, 2, 5])  # message header + عدد المفاتيح
    assert tx[-10] == 12    # TransferChecked instruction
