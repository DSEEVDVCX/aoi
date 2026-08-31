"""Tests for EVM contract safety: keccak, selectors, the proxy, and the cycle (no network).

keccak is written by hand (no `pycryptodome` and no `eth-hash` in the
environment, and `hashlib.sha3_256` has different padding) ⇒ it is tested
against known vectors before anything else: a wrong selector makes every
danger column a silent false zero with no visible trace.
"""
import os

import config
import evm_contract
import pytest
from db import RecorderDB, decode_raw
from evm_rpc import EVMRateLimit

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
BASE = "8453"
TOK = "0xbbbb000000000000000000000000000000000002"
ZERO = "0x0000000000000000000000000000000000000000"
OWNER = "0x00000000000000000000000000000000000000ff"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token=TOK, *, network=BASE, when=NOW):
    db.upsert_watch(token, network, "large_buy", f"sig-{token[:8]}", 48, when)


def _code(*signatures, extra=b""):
    """Handmade bytecode: `PUSH4 <selector>` for each signature, as a dispatcher table does."""
    body = b""
    for sig in signatures:
        body += b"\x63" + bytes.fromhex(evm_contract.selector(sig)[2:])
    return "0x" + (body + extra).hex()


def _word(addr):
    return "0x" + "0" * 24 + addr[2:]


# ---------------------------------------------------------------------------
# keccak and selectors
# ---------------------------------------------------------------------------
def test_keccak_matches_known_vectors():
    assert evm_contract.keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert evm_contract.keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_keccak_spans_multiple_blocks():
    """Longer than 136 bytes (the rate) ⇒ it passes through more than one compression round."""
    assert evm_contract.keccak256(b"a" * 200).hex() == evm_contract.keccak256(
        bytes(bytearray(b"a" * 200))
    ).hex()
    assert len(evm_contract.keccak256(b"a" * 200)) == 32


def test_selector_matches_known_signatures():
    assert evm_contract.selector("owner()") == "0x8da5cb5b"
    assert evm_contract.selector("transfer(address,uint256)") == "0xa9059cbb"
    assert evm_contract.selector("decimals()") == "0x313ce567"
    assert evm_contract.selector("balanceOf(address)") == "0x70a08231"


def test_transfer_topic_is_the_hashed_signature():
    """The signature stored in `evm_rpc` must equal a keccak computation, not a transcribed copy."""
    import evm_rpc

    computed = "0x" + evm_contract.keccak256(
        b"Transfer(address,address,uint256)"
    ).hex()
    assert computed == evm_rpc.TRANSFER_TOPIC


# ---------------------------------------------------------------------------
# Reading the bytecode
# ---------------------------------------------------------------------------
def test_code_selectors_skips_push_immediates():
    """A 0x63 byte **inside** other push immediates is not an instruction —
    reading it invents phantom selectors that make every contract look like
    it carries everything."""
    # PUSH5 carries 0x63 and the four bytes after it: if it were counted as
    # an instruction, a phantom selector would appear.
    body = "64" + "63aabbccdd"
    assert evm_contract._code_selectors("0x" + body) == set()


def test_code_selectors_finds_real_push4():
    sels = evm_contract._code_selectors(_code("owner()", "mint(uint256)"))
    assert evm_contract.selector("owner()") in sels
    assert len(sels) == 2


def test_analyze_flags_only_present_selectors():
    out = evm_contract.analyze_code(_code("mint(address,uint256)", "pause()"))
    assert out["has_mint"] == 1
    assert out["has_pause"] == 1
    assert out["has_blacklist"] == 0
    assert out["has_fee_setter"] == 0
    assert out["function_count"] == 2
    assert out["is_proxy"] == 0
    assert out["code_size"] == 10
    assert out["code_hash"].startswith("0x") and len(out["code_hash"]) == 66


def test_analyze_empty_code_is_a_measurement_not_a_gap():
    """`0x` = not a contract (a wallet, or a contract that was deleted) — and
    zero is a measurement, so it is written."""
    out = evm_contract.analyze_code("0x")
    assert out["code_size"] == 0
    assert out["code_hash"] is None
    assert out["function_count"] is None
    assert "has_mint" not in out          # not measured ⇒ stays NULL, not zero


def test_analyze_detects_eip1167_proxy_and_implementation():
    impl = "1234567890abcdef1234567890abcdef12345678"
    body = evm_contract._PROXY_PREFIX + impl + evm_contract._PROXY_SUFFIX
    out = evm_contract.analyze_code("0x" + body)
    assert len(body) // 2 == 45
    assert out["is_proxy"] == 1
    assert out["impl_address"] == "0x" + impl


def test_analyze_rejects_45_bytes_that_are_not_a_proxy():
    """Matching is on both ends, not the length: a 45-byte contract with other bytes is not a proxy."""
    out = evm_contract.analyze_code("0x" + "ab" * 45)
    assert out["is_proxy"] == 0
    assert out["impl_address"] is None


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
class _RPC:
    """Fake client: `code` is bytecode per address, and `owner` is the `eth_call` answer."""

    def __init__(self, code=None, owner=None, fail=(), throttle=()):
        self.code = code or {}
        self.owner = owner or {}
        self.fail = set(fail)
        self.throttle = set(throttle)
        self.code_calls = []
        self.eth_calls = []

    async def get_code(self, network_id, address):
        self.code_calls.append((str(network_id), address))
        if address in self.throttle:
            raise EVMRateLimit("eth_getCode [8453] HTTP 429")
        if address in self.fail:
            raise RuntimeError("eth_getCode HTTP 503")
        return self.code.get(address, "0x")

    async def eth_call(self, network_id, to, data, block="latest"):
        self.eth_calls.append((to, data))
        want = self.owner.get(to)
        if want is None:
            return None                   # every ownership form reverts
        if data != evm_contract.selector(want[0]):
            return None                   # this form does not exist in the contract
        return _word(want[1])


async def _noop(_seconds):
    pass


async def test_cycle_writes_row_and_state(db):
    _watch(db)
    rpc = _RPC(
        code={TOK: _code("mint(address,uint256)", "setMaxTxAmount(uint256)")},
        owner={TOK: ("owner()", OWNER)},
    )

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats == {
        "evm_contract_due": 1, "evm_contract_rows": 1,
        "evm_contract_errors": 0, "evm_contract_proxies": 0,
        "evm_contract_throttled": 0,
    }
    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["network_id"] == BASE
    assert row["owner_address"] == OWNER
    assert row["is_ownership_renounced"] == 0
    assert row["has_mint"] == 1
    assert row["has_limit_setter"] == 1
    assert row["has_pause"] == 0
    assert decode_raw(row["raw_json"])["owner"] == OWNER
    state = db._conn.execute(
        "SELECT last_status, attempts FROM evm_contract_state"
    ).fetchone()
    assert state["last_status"] == "ok"
    assert state["attempts"] == 1


async def test_zero_owner_is_renounced_not_missing(db):
    _watch(db)
    rpc = _RPC(code={TOK: _code("owner()")}, owner={TOK: ("owner()", ZERO)})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["owner_address"] == ZERO
    assert row["is_ownership_renounced"] == 1


async def test_no_owner_function_stays_null(db):
    """"No owner function" is not "ownership renounced" — merging them makes
    the column a lie."""
    _watch(db)
    rpc = _RPC(code={TOK: _code("transfer(address,uint256)")})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["owner_address"] is None
    assert row["is_ownership_renounced"] is None
    # Every form was tried before giving up.
    assert len(rpc.eth_calls) == len(evm_contract._OWNER_CALLS)


async def test_owner_probes_are_paced_not_back_to_back(db):
    """Three back-to-back ownership probes throttled the Base node with 429
    in a live cycle.

    And throttling on `eth_call` is no longer swallowed after the fix ⇒ the
    whole row is cancelled and retried. So there is pacing between each form
    and its sister, and its cost is fractions of a second per hourly cycle.
    """
    _watch(db)
    rpc = _RPC(code={TOK: _code("transfer(address,uint256)")})
    slept = []

    async def _record(seconds):
        slept.append(seconds)

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_record)

    assert len(rpc.eth_calls) == len(evm_contract._OWNER_CALLS)
    # One sleep before the first (after `eth_getCode`) and one between every two forms.
    assert len(slept) >= len(evm_contract._OWNER_CALLS)
    assert all(s == config.EVM_PACING_SECONDS for s in slept)


async def test_alternate_owner_signature_answers(db):
    """`getOwner()` is a common form on BSC; the first revert is an answer, not an error."""
    _watch(db)
    rpc = _RPC(code={TOK: _code("getOwner()")}, owner={TOK: ("getOwner()", OWNER)})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    row = db._conn.execute("SELECT owner_address FROM evm_contract").fetchone()
    assert row["owner_address"] == OWNER


async def test_non_contract_address_skips_owner_call(db):
    """`0x` ⇒ no contract, so asking `owner()` is meaningless; the row is
    written as a measurement (zero size)."""
    _watch(db)
    rpc = _RPC()

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_rows"] == 1
    assert rpc.eth_calls == []
    row = db._conn.execute("SELECT * FROM evm_contract").fetchone()
    assert row["code_size"] == 0
    assert row["has_mint"] is None            # not measured, not "absent"
    assert row["function_count"] is None


async def test_proxy_is_counted(db):
    _watch(db)
    impl = "1234567890abcdef1234567890abcdef12345678"
    body = evm_contract._PROXY_PREFIX + impl + evm_contract._PROXY_SUFFIX
    rpc = _RPC(code={TOK: "0x" + body})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_proxies"] == 1
    row = db._conn.execute("SELECT is_proxy, impl_address FROM evm_contract").fetchone()
    assert row["is_proxy"] == 1
    assert row["impl_address"] == "0x" + impl


async def test_failure_marks_state_error_and_meta(db):
    _watch(db)
    rpc = _RPC(fail={TOK})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_errors"] == 1
    assert stats["evm_contract_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM evm_contract").fetchone()[0] == 0
    state = db._conn.execute(
        "SELECT last_status FROM evm_contract_state"
    ).fetchone()
    assert state["last_status"] == "error"
    assert "503" in (db.get_meta("last_error_evm_contract") or "")


async def test_only_configured_networks_are_scanned(db):
    """The gate reads `EVM_CONTRACT_NETWORKS`, not "is the network EVM".

    Monad (143) is an EVM network and watched, but it is outside the scan
    list — if the condition were "any EVM", it would be scanned. Base, BSC,
    and Robinhood are all inside it as measured on 2026-08-22.
    """
    _watch(db, "0xmonad", network="143")
    _watch(db, TOK, network=BASE)
    rpc = _RPC(code={TOK: _code("owner()")})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert [a for _, a in rpc.code_calls] == [TOK]


async def test_bsc_and_robinhood_are_scanned_after_the_widening(db):
    """`is_proxy` at 26 of 40 on BSC is the strongest separating column we
    measured — restricting the scan to Base was wasting it along with 40% of
    the model's rows."""
    _watch(db, "0xbsc", network="56")
    _watch(db, "0xrh", network="4663")
    rpc = _RPC(code={"0xbsc": _code("mint(address,uint256)"),
                     "0xrh": _code("pause()")})

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert sorted(a for _, a in rpc.code_calls) == ["0xbsc", "0xrh"]
    nets = {r[0] for r in db._conn.execute(
        "SELECT network_id FROM evm_contract"
    )}
    assert nets == {"56", "4663"}


async def test_row_per_measurement_not_updated_in_place(db):
    """Ownership renunciation is an **event** mid-window; a row overwritten
    in place erases that it happened."""
    from datetime import datetime, timedelta

    _watch(db)
    rpc = _RPC(code={TOK: _code("owner()")}, owner={TOK: ("owner()", OWNER)})
    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    later = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.EVM_CONTRACT_REFRESH_SECONDS + 1)
    ).isoformat()
    rpc.owner = {TOK: ("owner()", ZERO)}          # ownership was renounced between the two measurements
    await evm_contract.run_evm_contract_cycle(rpc, db, later, sleep=_noop)

    rows = db._conn.execute(
        "SELECT recorded_at, is_ownership_renounced FROM evm_contract "
        "ORDER BY recorded_at"
    ).fetchall()
    assert [r["is_ownership_renounced"] for r in rows] == [0, 1]


async def test_fresh_row_is_not_refetched(db):
    _watch(db)
    rpc = _RPC(code={TOK: _code("owner()")})
    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.code_calls) == 1

    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.code_calls) == 1               # still fresh


async def test_empty_networks_tuple_scans_nothing(db, monkeypatch):
    _watch(db)
    monkeypatch.setattr(config, "EVM_CONTRACT_NETWORKS", ())
    rpc = _RPC(code={TOK: _code("owner()")})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.code_calls == []
    assert stats["evm_contract_due"] == 0


# ---------------------------------------------------------------------------
# Throttling: quota exhausted, not a token failure
# ---------------------------------------------------------------------------
OLD = "2026-08-13T11:00:00+00:00"
TOK2 = "0xbbbb000000000000000000000000000000000009"


async def test_throttle_stops_the_step_and_writes_no_state(db, monkeypatch):
    """429 = "our quota is used up", not "this token is broken".

    The `mainnet.base.org` quota is measured by count (nine calls), not by
    spacing ⇒ everything after the first throttle is pre-throttled. So the
    step is cut short and no state is written: `error` would push the token
    a quarter hour back for no fault of its own, and with no state it stays
    first in line for the next cycle.
    """
    monkeypatch.setattr(config, "EVM_CONTRACT_PER_CYCLE", 2)
    _watch(db, TOK, when=NOW)                     # the newer ⇒ first
    _watch(db, TOK2, when=OLD)
    rpc = _RPC(code={TOK: _code("owner()")}, throttle={TOK2})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_due"] == 2
    assert stats["evm_contract_rows"] == 1
    assert stats["evm_contract_errors"] == 0
    assert stats["evm_contract_throttled"] == 1
    # The throttled one has no state row at all — neither 'ok' nor 'error'.
    rows = db._conn.execute(
        "SELECT token_address, last_status FROM evm_contract_state"
    ).fetchall()
    assert [(r["token_address"], r["last_status"]) for r in rows] == [(TOK, "ok")]
    # And no error message: throttling is not an error, so it does not pollute the error dashboard.
    assert db.get_meta("last_error_evm_contract") is None
    assert db.get_meta("evm_contract_last_throttle_at") == NOW


async def test_throttled_token_is_measured_next_cycle(db, monkeypatch):
    """Progress is guaranteed: the throttled token comes back due, and the cycle does not spin in place."""
    monkeypatch.setattr(config, "EVM_CONTRACT_PER_CYCLE", 2)
    _watch(db, TOK, when=NOW)
    _watch(db, TOK2, when=OLD)
    rpc = _RPC(code={TOK: _code("owner()")}, throttle={TOK2})
    await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    rpc.throttle = set()                          # the quota has refilled
    rpc.code[TOK2] = _code("pause()")
    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_due"] == 1          # the first one is still fresh
    assert stats["evm_contract_rows"] == 1
    assert stats["evm_contract_throttled"] == 0
    row = db._conn.execute(
        "SELECT token_address, has_pause FROM evm_contract "
        "WHERE token_address=?", (TOK2,)
    ).fetchone()
    assert row["has_pause"] == 1


async def test_throttle_on_owner_probe_cancels_the_row_entirely(db, monkeypatch):
    """Throttling amid the ownership forms is not swallowed: a bogus "no
    owner" is a false measurement that would be stored."""
    monkeypatch.setattr(config, "EVM_CONTRACT_PER_CYCLE", 2)
    _watch(db, TOK)

    class _Throttling(_RPC):
        async def eth_call(self, network_id, to, data, block="latest"):
            self.eth_calls.append((to, data))
            raise EVMRateLimit("eth_call [8453] HTTP 429")

    rpc = _Throttling(code={TOK: _code("owner()")})

    stats = await evm_contract.run_evm_contract_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_contract_rows"] == 0
    assert stats["evm_contract_throttled"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM evm_contract").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM evm_contract_state"
    ).fetchone()[0] == 0
