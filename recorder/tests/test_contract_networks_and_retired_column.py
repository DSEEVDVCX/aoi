"""What the measurement overturned: the three networks, and the column retired on purpose.

The first measurement (2026-08-13) restricted contract checking to Base
because BSC and Robinhood showed up as "repeated templates". A re-measurement
(2026-08-22) showed the repetition was measured in `code_size` alone, and
that `is_proxy` **is itself the information**: 26 of 40 on BSC versus 3 of
40 on Base. These two tests prevent going back to the restriction without
a new measurement.
"""
import config
import features


def test_contract_scan_covers_the_three_measured_networks():
    """Restricting the check to Base loses 40% of the model's rows with no measured reason."""
    assert set(config.EVM_CONTRACT_NETWORKS) == {"8453", "56", "4663"}


def test_every_scanned_network_has_an_rpc_endpoint():
    """A network checked without a node = an error every cycle, not an empty column."""
    for network in config.EVM_CONTRACT_NETWORKS:
        assert config.EVM_RPC_URLS.get(str(network)), network


def test_contract_batch_stays_under_the_measured_base_quota():
    """`mainnet.base.org` has a quota of nine calls per window, and a coin costs four ⇒ two.

    Widening the networks does not justify raising the batch: the number is
    one node's quota, not the layer's capacity, and the node was in fact
    throttled at four in two consecutive live cycles.
    """
    assert config.EVM_CONTRACT_PER_CYCLE * 4 < 9


def test_retired_dead_column_is_out_of_the_feature_list():
    """`top10_holders_pct` is zero across 3.8 million rows — the source never sends the key."""
    assert "top10_holders_pct" not in features.FEATURE_COLUMNS
    assert "top10_holders_pct" not in features.ROW_COLUMNS


def test_its_two_live_replacements_are_still_features():
    """Retirement is only permitted while the replacement stands — otherwise we lose concentration, not the dead column."""
    assert "chain_top10_pct" in features.FEATURE_COLUMNS
    assert "onchain_top10_pct" in features.FEATURE_COLUMNS
