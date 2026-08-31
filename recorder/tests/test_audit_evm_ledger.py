"""Tests for the ledger auditor: what it catches, and what it refuses to
call a match.

All the value here is in the verdicts. An audit tool that calls a truncated
ledger a "match" is worse than no tool at all: the trap would stay hidden,
and with it a *certificate of health*. So what is examined, precisely: the
boundary block is found with real timestamps, a shortfall is called a
shortfall, slight drift is not screamed about, and the key never appears in
any error message.
"""
import json
import os

import audit_evm_ledger as audit
import pytest
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NET = "8453"
TOK = "0xaaaa000000000000000000000000000000000001"
# 2026-08-15T18:15:00+00:00 = 1786** — computed, not written by hand, so
# the two cannot drift apart.
STAMP = "2026-08-15T18:15:00+00:00"
WHALES = [f"0x{index:040x}" for index in range(1, 6)]


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


class FakeRPC:
    """A fake archive node: one block per second, balances fixed regardless
    of the block.

    The balance constancy is deliberate: these tests examine the auditor's
    **verdict**, not the chain math, so any dependence on the block would
    mix a boundary-search failure into a comparison failure.
    """

    def __init__(self, *, supply, balances, burned=0, genesis=1_000_000_000,
                 head=2_000, fail_at=None):
        self.supply, self.balances, self.burned = supply, balances, burned
        self.genesis, self.head, self.fail_at = genesis, head, fail_at
        self.calls = 0
        self.asked = []

    def block_number(self):
        self.calls += 1
        return self.head

    def block_timestamp(self, block):
        self.calls += 1
        if block > self.head:
            raise audit.ArchiveError(f"no block at {block}")
        return self.genesis + int(block)

    def total_supply(self, token, block):
        self.calls += 1
        if self.fail_at == "supply":
            raise audit.ArchiveError("eth_call: archive unavailable")
        return self.supply

    def balance_of(self, token, holder, block):
        self.calls += 1
        self.asked.append((holder, block))
        if holder in audit.BURN_ADDRESSES:
            return self.burned if holder.endswith("dead") else 0
        return self.balances.get(holder, 0)


def _seed(db, *, balances, supply_base=None, pct=None, stamp=STAMP):
    """A single replay snapshot with known balances — the denominator is
    their sum unless stated otherwise."""
    total = supply_base if supply_base is not None else sum(balances.values())
    top = sorted(balances.items(), key=lambda item: -item[1])
    shares = pct or {
        f"top{n}_pct": sum(v for _, v in top[:n]) / total * 100 for n in (1, 5, 10, 20)
    }
    db.upsert_watch(TOK, NET, "trending", "sig", 48, stamp)
    assert db.insert_chain_concentration({
        "token_address": TOK, "network_id": NET, "recorded_at": stamp,
        "watch_first_seen_at": stamp, "is_control": 0, "is_replay": 1,
        "holder_count": len(balances), "top_accounts": len(top),
        "decimals": 18, "supply": float(total),
        **shares,
        "raw_json": {
            "source": "evm_ledger", "supply_base": str(total),
            "holder_count": len(balances),
            "top": [[address, str(value)] for address, value in top],
        },
    })
    return dict(db._conn.execute(
        "SELECT * FROM chain_concentration WHERE recorded_at = ?", (stamp,),
    ).fetchone())


def test_a_complete_ledger_is_called_matching(db):
    """The healthy case: the sum equals the supply and every holder
    matches ⇒ "match"."""
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)
    rpc = FakeRPC(supply=500, balances=balances)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "match", record
    assert record["holders_matched"] == 5
    assert record["coverage_pct"] == pytest.approx(100.0)


def test_a_truncated_ledger_is_called_deficient_not_matching(db):
    """The very trap: every holder we know of is correct, and the ledger is
    still incomplete.

    This is exactly what a response truncated at the first page leaves
    behind: the addresses that reached us have intact balances, and the
    missing ones are missing without a trace. If the verdict went by the
    holders alone it would say "match" — and the identity against
    `totalSupply()` is what exposes it.
    """
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)                 # the ledger knows 500
    rpc = FakeRPC(supply=1_000, balances=balances)     # the chain says 1000
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "incomplete", record
    assert record["holders_matched"] == 5              # not one holder wrong
    assert record["coverage_pct"] == pytest.approx(50.0)
    assert "not read" in record["note"]


def test_burned_supply_is_added_back_before_judging_completeness(db):
    """A token half of which was burned is not an incomplete ledger — the
    difference is the burn-address balance."""
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)
    rpc = FakeRPC(supply=1_000, balances=balances, burned=500)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "match", record
    assert record["burned"] == 500
    assert record["coverage_pct"] == pytest.approx(100.0)


def test_a_transfer_missed_between_two_known_holders_is_caught_by_balances(db):
    """The sum is sound and the distribution is not — a defect the
    completeness question never catches.

    A transfer lost **between two holders we know of** changes no sum: what
    left one landed in the other. So the identity against `totalSupply()`
    passes with honors while the concentration — the very thing we measure
    — is wrong. That is why neither question suffices without the other.
    """
    stored = {address: 100 for address in WHALES}
    row = _seed(db, balances=stored)
    chain = {**stored, WHALES[0]: 40, WHALES[1]: 160}   # sixty moved with no log
    rpc = FakeRPC(supply=500, balances=chain)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "mismatch", record
    assert record["coverage_pct"] == pytest.approx(100.0)   # completeness passes
    assert record["holders_matched"] == 3
    assert record["delta_points"] == pytest.approx(12.0)    # top1: 20% ← 32%
    assert record["worst_holder"]["address"] in (WHALES[0], WHALES[1])


def test_a_hair_of_drift_is_not_screamed_about(db):
    """A difference under a tenth of a point is called drift: a boundary
    block moved a balance, and that is to be expected.

    Without this distinction the tool becomes useless by sheer alarm
    volume — and a tool whose alarm is ignored warns of nothing.
    """
    stored = {address: 1_000_000 for address in WHALES}
    row = _seed(db, balances=stored)
    chain = {**stored, WHALES[0]: 1_000_001}
    rpc = FakeRPC(supply=5_000_001, balances=chain)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "drift", record
    assert record["delta_points"] < audit.DRIFT_POINTS


def test_the_boundary_block_is_the_last_one_at_or_before_the_stamp():
    """The answer is exact, not approximate: the block after it is newer
    than the time, and was actually read."""
    rpc = FakeRPC(supply=1, balances={}, genesis=1_000_000_000, head=50_000)
    target = 1_000_000_000 + 37_411
    assert audit.boundary_block(rpc, target, 0, 50_000) == 37_411
    assert rpc.block_timestamp(37_411) <= target < rpc.block_timestamp(37_412)


def test_the_search_costs_four_calls_across_forty_million_blocks():
    """Extrapolation is what makes the audit feasible — and the bound here
    is tight so its regression gets caught.

    Pure bisection over this range costs twenty-five calls; extrapolation
    costs four. And if one end's timestamp ever went missing in the future
    (which is what used to happen to block zero), performance would sink
    back to bisection without a single test failing — so a loose bound here
    means a silent regression.
    """
    rpc = FakeRPC(supply=1, balances={}, head=40_000_000)
    assert audit.boundary_block(rpc, rpc.genesis + 31_415_926, 0, 40_000_000)         == 31_415_926
    assert rpc.calls <= 6, rpc.calls


def test_anchors_narrow_the_bracket_without_being_trusted(db):
    """The anchor shortens the search and is never believed: both bracket
    ends are read from the chain before being trusted.

    That is why a **false** anchor is tested: if the search believed it, it
    would answer above it without a second look, when the correct behavior
    is to read its timestamp, see it is after the wanted time, and drop
    below it.
    """
    target = 1_000_000_000 + 5_000
    for block, stamp in ((2_000, 1_000_002_000), (8_000, 1_000_008_000)):
        db.add_block_anchor(NET, block, stamp, STAMP)
    low, high = audit.bracket_from_anchors(db, NET, target, 40_000)
    assert (low, high) == (2_000, 8_000)
    rpc = FakeRPC(supply=1, balances={}, head=40_000)
    assert audit.boundary_block(rpc, target, low, high) == 5_000
    # and a false anchor (its timestamp precedes its block) yields no false
    # answer:
    assert audit.boundary_block(rpc, target, 9_999, 40_000) == 5_000


def test_a_missing_archive_is_reported_not_swallowed_as_zero(db):
    """An `eth_call` revert or a missing archive ⇒ "unverifiable". A silent
    zero would read as a "match"."""
    balances = {address: 100 for address in WHALES}
    row = _seed(db, balances=balances)
    rpc = FakeRPC(supply=500, balances=balances, fail_at="supply")
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "unverifiable"
    assert "archive unavailable" in record["note"]
    assert record["holders_checked"] == 0


def test_the_key_never_reaches_an_error_message():
    """The error message carries the URL, and the URL carries the key — so
    redaction happens at construction."""
    secret = "alch_notARealKeyJustForTheTest"
    rpc = audit.ArchiveRPC(f"https://x.invalid/v2/{secret}", secret)
    try:
        with pytest.raises(audit.ArchiveError) as caught:
            rpc.block_number()
        assert secret not in str(caught.value)
        assert secret not in rpc.hide(f"boom https://x.invalid/v2/{secret} boom")
        assert "<key>" in rpc.hide(secret)
    finally:
        rpc.close()


def test_every_declared_route_has_a_key_field_triplet():
    """A route without key fields fails at run time, not at test time — so
    it is checked here."""
    for network, routes in audit.ARCHIVE_ROUTES.items():
        assert routes, network
        for name, pattern in routes:
            assert name in audit.PROVIDER_FIELDS, (network, name)
            assert "{key}" in pattern, (network, name)
            assert len(audit.PROVIDER_FIELDS[name]) == 3


def test_sampling_always_includes_the_last_snapshot(db):
    """The maximum deviation in a cumulative ledger sits in the last
    snapshot, so it is never left to luck."""
    stamps = [f"2026-08-15T18:{minute:02d}:00+00:00" for minute in range(0, 40, 5)]
    for stamp in stamps:
        _seed(db, balances={address: 100 for address in WHALES}, stamp=stamp)
    picked = audit.sample_rows(db, NET, tokens=1, per_token=3)
    assert len(picked) == 3
    assert picked[-1]["recorded_at"] == stamps[-1]
    assert picked[0]["recorded_at"] == stamps[0]
    # and two runs on the same database give the same snapshots — a flaky
    # tool is not one to build on.
    again = audit.sample_rows(db, NET, tokens=1, per_token=3)
    assert [row["recorded_at"] for row in again] == [
        row["recorded_at"] for row in picked
    ]


def test_percentages_use_the_ledgers_own_denominator():
    """The denominator is `supply_base`, not `totalSupply()`: a difference
    of definition must not be read as a difference of data."""
    out = audit._percentages([50, 30, 20], 100)
    assert out["top1_pct"] == pytest.approx(50.0)
    assert out["top5_pct"] == pytest.approx(100.0)
    assert audit._percentages([1], 0)["top1_pct"] is None


def test_a_row_that_predates_the_chain_is_reported_not_audited(db):
    """A time before the first block ⇒ no block to ask for, and that is
    said plainly rather than compared against a fiction."""
    row = _seed(db, balances={address: 100 for address in WHALES})
    rpc = FakeRPC(supply=500, balances={}, genesis=9_000_000_000, head=10)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    assert record["verdict"] == "unverifiable"
    assert record["block"] == 0
    assert "no block before" in record["note"]


def test_json_report_survives_a_round_trip(db):
    """The report is read back by machines later, so whale balances are
    strings, not floats."""
    balances = {address: 10 ** 30 for address in WHALES}
    row = _seed(db, balances=balances)
    chain = {**balances, WHALES[0]: 10 ** 29}
    rpc = FakeRPC(supply=sum(chain.values()), balances=chain)
    record = audit.audit_row(rpc, db, row, 5, head=rpc.head)
    revived = json.loads(json.dumps(record, ensure_ascii=False))
    assert int(revived["worst_holder"]["chain"]) == 10 ** 29
