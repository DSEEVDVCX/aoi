"""Asset classification tests — fomo is a multi-asset platform, not a meme market.

Every case below was **actually observed** in our archive (2026-07-30): major
assets, stablecoins, tokenized stocks and commodities, and memes impersonating
the majors' symbols.
"""
import config
from extract import classify_asset


def cls(symbol=None, px_min=None, px_max=None, mc=None):
    return classify_asset(symbol, px_min, px_max, mc)[0]


# --- Memes: the majority and the target ---
def test_typical_meme():
    assert cls("PEPE2", 1e-7, 3.5e-6, 420_000) == "meme"


def test_meme_with_high_market_cap_but_below_major():
    assert cls("WOJAK", 0.0004, 0.02, 40_000_000) == "meme"


# --- Major assets ---
def test_btc_by_market_cap():
    assert cls("BTC", 63967.9, 63967.9, 1.2692e12) == "major"


def test_xrp_by_symbol_when_price_low_but_cap_big():
    assert cls("XRP", 1.06, 1.09, 6.65e10) == "major"


def test_known_symbol_without_market_cap_is_major():
    assert cls("ETH", 1900.0, 1926.0, None) in {"major", "priced"}


# --- Symbol impersonation: the most dangerous case ---
def test_meme_impersonating_btc_is_not_major():
    """Measured: a "BTC" token at $0.0035 with a $3.5M cap — a meme, not Bitcoin."""
    assert cls("BTC", 0.0009, 0.00354, 3_536_767) == "meme"


def test_meme_impersonating_sol_is_not_major():
    assert cls("SOL", 0.002, 0.0059, 4_927_224) == "meme"


# --- Untrustworthy market caps ---
def test_absurd_market_cap_is_ignored_not_trusted():
    """Measured: SMILE at $0.0888 with a "$69 trillion" market cap — ignored, judged by price."""
    assert cls("SMILE", 0.0888, 0.0888, 6.9e13) == "meme"


def test_absurd_cap_with_high_price_still_non_meme():
    assert cls("WEIRD", 900.0, 958.0, 9e13) == "priced"


# --- Tokenized stocks and commodities: market cap does not reveal them, price does ---
def test_tokenized_apple_by_price_despite_tiny_cap():
    """Measured: AAPL with a market cap of only $1.36M (the tokenized part is
    a tiny fraction) and a price of $339."""
    assert cls("AAPL", 339.0, 339.25, 1_358_127) == "priced"


def test_tokenized_gold():
    assert cls("PAXG", 4100.0, 4106.65, 1_109_289) == "priced"


def test_tokenized_stock_just_above_threshold():
    assert cls("META", 5.6, 7.17, 162_621_784) == "priced"


# --- Stablecoins ---
def test_stablecoin_pinned_band():
    assert cls("USDT", 0.9985, 0.9987, 9.16e9) == "stable"


def test_dollar_priced_meme_is_not_stable_if_it_moved():
    """A coin that touched the dollar but moved outside the band => not stable."""
    assert cls("HYPEDMEME", 0.4, 1.02, 5_000_000) == "meme"


# --- Boundaries and missing values ---
def test_missing_everything_defaults_to_meme():
    assert cls(None, None, None, None) == "meme"


def test_zero_price_does_not_crash_or_stabilize():
    assert cls("X", 0.0, 0.0, None) == "meme"


def test_reason_is_recorded_for_audit():
    _, reason = classify_asset("BTC", 63967.9, 63967.9, 1.2692e12)
    assert "market_cap" in reason


def test_thresholds_come_from_config_single_source():
    assert config.ASSET_MEME_MAX_PRICE_USD == 5.0
    assert config.MAJOR_ASSET_MARKET_CAP_USD == 1e9
