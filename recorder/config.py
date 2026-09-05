"""Configuration for the historical data recorder.

The recorder is a separate project beside `api/` but reuses the `api` code
(FomoClient, CredentialStore, config) by adding `api/src` to sys.path. It does not
duplicate any access logic that already exists.

All paths are absolute and derived from this file's location, so the recorder works
regardless of the current working directory (important for the scheduled task).
"""
from __future__ import annotations

import os
import sys

# --- Paths ---
HERE = os.path.dirname(os.path.abspath(__file__))          # .../aoi/recorder
ROOT = os.path.dirname(HERE)                               # .../aoi
API_DIR = os.path.join(ROOT, "api")                        # .../aoi/api
API_SRC = os.path.join(API_DIR, "src")                     # .../aoi/api/src

# We add api/src so we can import fomo_api. Executed at import because config/db need it.
if API_SRC not in sys.path:
    sys.path.insert(0, API_SRC)

# The database sits beside this file.
DB_PATH = os.path.join(HERE, "recorder.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

# The log file (the scheduled task hides stderr).
LOG_PATH = os.path.join(HERE, "recorder.log")

# tradingActivity head collector. Separate from the /feed cycle because this path has
# its own pagination, and it walks up to the first overlap with stored events without touching the history pointer.
ACTIVITY_HEAD_INTERVAL_SECONDS = 300
ACTIVITY_HEAD_MAX_PAGES = 3

# Credential state file (session_token). The fomo_api default is relative
# (.privy_state.json) and it lives inside api/, so resolve it against api/ unless already absolute.
def credential_state_path() -> str:
    from fomo_api.config import settings  # noqa: E402  (after the sys.path insert)

    raw = settings.credential_state_file
    if os.path.isabs(raw):
        return raw
    return os.path.join(API_DIR, raw)


# --- Epoch boundary: forward (live) vs retroactive (scattered) ---
# The model is trained on the live set alone (project decision): retroactive rows
# structurally lack the instantaneous families (market_ticks, token_social snapshots,
# leader rank, macro), so they create an absence pattern that matches the epoch
# exactly — the model learns it instead of the signal (epoch leakage).
# The value = the timestamp of the first live signal (signal_events); structurally
# confirmed: zero of 861 retroactive rows has a market_tick near t0, vs 89% of live
# rows. An `entry_ts` at or above this boundary => `is_live=1`. We do not delete the
# retroactive set — it stays stored and labeled for analysis, but it is excluded
# from the training set by the `is_live=1` filter.
LIVE_START_TS = 1785018927         # 2026-07-25T22:35:27Z (first live signal)

# --- Cadences and limits (in seconds/hours) ---
CYCLE_SECONDS = 60                 # one cycle per minute
# The cycle budget — the real constraint on collection is not the source's limits
# but the cycle time. The source does not rate-limit calls: zero 429 responses in the
# project's entire history, and `errors: 0` in 274 of the last 375 cycles. But the
# "be kind to the source" pacing was eating **27.5s of every 60** (46%) with real
# work at ~31.5s — so the median cycle was 59s and **32% of cycles exceeded 61s**,
# i.e. with no headroom. And the `run_forever` loop sleeps
# `CYCLE_SECONDS - elapsed`, so a cycle that exceeds the budget stretches the same
# duration: raising caps without cutting pacing slows the sweep down instead of
# speeding it up. So pacing was cut to ~11s and the savings were spent on the caps.
# --- The cycle breaker when the source blocks us ---
# A "dead" cycle = errors and not one productive row. Three in a row open the
# breaker, so the cycle becomes a single probe (`GET /feed` with limit 1) instead of
# ~190 requests, and the wait doubles 60->120->240->480->900s. The reason is measured
# 2026-08-19: every data path returned 403 (`/feed`, `/proxy`, `/v2`, `/hodlers`,
# `/trades`), so the recorder kept throwing ~52 requests a minute at a wall and
# writing nothing, for 67 consecutive cycles. And the seven-day precedent: ten
# outages, all <= two cycles, so a threshold of three touches none of them.
#
# **And the first diagnosis was wrong, and it is recorded here as wrong**: it was
# said "our address, not our fingerprint" because eight fingerprints from chrome124
# to safari260 all returned 403. The truth (measured 2026-08-20): the block is **on
# the identity** — another account from the same address and the same fingerprint
# returned 200 on all three paths. Changing the fingerprint does not help because
# the fingerprint is not what is blocked, and changing the network does not help
# because it is not the address. And the difference is known with two calls: a
# garbage token returns **401** while our token returns **403** => the request
# arrived and the identity was rejected, so do not chase the network for an hour
# when it is healthy. (The dashboard's "check" button does this, and the switch is
# from `data health` or `py api/switch_recorder_account.py`.)
# --- The volume cap: what was cut after the 2026-08-19 block, and at what cost ---
# What exactly opened the block is not known for certain — no 429 in the project's
# entire history and no reason message from the source — but sustained volume is the
# most likely explanation: ~52 calls/minute = 3,150/hour = 75k/day from a single
# identity, week after week. So volume was cut by two-thirds as a precaution, and
# the cost is **entirely in the cadence of the slow features, not in the data
# core**: `market_ticks` stays every minute, signals are recorded the moment they
# arrive, and candles (the labeling source) are untouched.
#
# The measurement the cut was built on (2026-08-20, 189 active watches):
#
#     Block        calls/coin  before          after           saved
#     ─────────────────────────────────────────────────────────
#     Holders          2      13 => 26 calls  8 => 16 calls   -10
#     Social           1      7               5               -2
#     Traders          1      4               1               -3   <- one batch, not per-id
#     Candles          1      9               9                0   <- untouched
#     Lists+gap-fill   -      ~6              ~6               0
#     ─────────────────────────────────────────────────────────
#     Total                   ~52/minute      ~37/minute      -29%
#
# And the traders row was revised the same day after the death of `/v2/users/{id}`
# was discovered: it became a single call carrying fifty traders instead of two calls
# carrying two — fewer requests and wider coverage at the same time, which is the
# difference between cutting volume and cutting collection. The details sit at the constant.
#
# And for every cap that was cut, its cadence was raised by the same ratio, or the
# request becomes larger than the cap and a permanent deficit accumulates: the sweep
# stays **under** the refresh window, not above it (the arithmetic sits at each
# constant). And reverting is two lines: restore the numbers, nothing else needs changing.
UPSTREAM_BREAKER_AFTER = 3         # consecutive dead cycles before the breaker opens
UPSTREAM_BREAKER_MAX_SECONDS = 900 # cap on the wait between probes (15 minutes)

WATCH_HOURS = 48                   # how long each coin is watched
WATCHLIST_CAP = 150                # cap on simultaneous active coins
# The age gate at signal time: we do not watch a coin younger than this at the moment of the signal.
#
# Measured on independent signals in the database: coins younger than two days have
# a **29x** higher collapse rate, and the median return is -46.2% vs -5.1% for older
# ones — and the cost is only 4% of training rows. The purpose is explicit: no bot
# is built on a coin under two days old, so collecting its data is spending a cycle
# on what will not be traded.
#
# **The rejection is not final and is not recorded as a ban**: the rejected coin
# never enters the list at all, so if another signal arrives when it is older than
# two days it enters like any other coin. Each signal is judged by its age **at that
# moment**, not by a prior verdict.
#
# Zero disables the gate.
MIN_TOKEN_AGE_DAYS = 2.0
# The moment the age gate was published on the branch (commit 5307ed7). Cleanup of
# operational watches is limited to what was admitted after this moment; we do not re-interpret older history with a later policy.
AGE_GATE_ENABLED_AT = "2026-08-22T18:03:31+00:00"
# A lookup result with no date is not a reason to repeat the same call every minute.
# We re-check it periodically because the source may fix its data, but with a long
# gap; transport errors are retried faster.
AGE_MISSING_RETRY_SECONDS = 6 * 3600
AGE_ERROR_RETRY_SECONDS = 300
# An independent age fallback (2026-08-27): when filterTokens cannot find the coin
# we ask GeckoTerminal for the oldest pool_created_at — measured: it resolves 17/18
# of fomo's unknowns, one call per new unknown coin, no key, with a Chrome UA like the fomo line itself.
AGE_GECKO_FALLBACK = True
# The upper age cap for EVM admission (2026-08-29): the minimum-age gate alone was
# admitting coins a year to two years old, and filling their holders ledger drops
# the whole network into admission pause (the 08-29 Robinhood queue: 43 coins, 38 of
# them above 10M blocks remaining). Measured on 4663+8453 admissions since 22/8: a
# 60-day cap would have rejected 34/123 (28%) at the cost of nearly empty rows — the
# explosion in EVM is almost entirely under 60 days of age (0 of 37 labeled coins
# aged 30-60 days exploded, and every one above that is old without exception).
# Zero disables the cap. Not applied to Solana: its mints are pulled from a
# different source and there is no fill queue there at all.
EVM_MAX_TOKEN_AGE_DAYS = 60
# Keep the EVM ledger when reactivating a watch (2026-08-29): upsert_watch used to
# wipe balances/backfill/replay on every reactivation, so the queues were built from
# scratch forever. If the gap since the previous window ended is shorter than this
# cap the ledger stays and only to_block is extended; a longer one rebuilds from
# genesis (a long gap may have missed transfers that the old ledger cannot compensate for).
EVM_REACTIVATION_KEEP_LEDGER_SECONDS = 48 * 3600
# The pre-signal runup gate (2026-08-27): a signal on a coin that rose more than
# this (log-return, 1.5 = +150%) in the last 24 hours before the signal does not
# open a watch. Measured on 2,968 labeled signals: the 48h final for this class is
# -36% with rug at 7.1% — they are late joiners (whale exit fuel), not the start of
# a rise. The signal is always saved; only opening the watch is rejected. 0 disables the gate.
MAX_PRE_SIGNAL_RUNUP = 1.5
# Backoff for the rejection verdict (2026-08-28): a rising coin receives 5-10
# signals a day and each one used to re-query the candles. The rejecting verdict is
# read from memory for this duration and then re-evaluated — the runup may subside,
# making the later signal genuinely early. One hour balances computation efficiency
# against a serious chance of re-evaluation.
RUNUP_RETRY_SECONDS = 3600
# The definition of an explosion (fv15, owner's decision 2026-08-28 after measuring
# 2,378 explosions): peak >= +100% within 48 hours, with half of it (+50%) reachable
# within the first 24 hours. Measured: it captures 85.5% of explosions and 96% of
# giants (10x+), and the missed one has a median of only +26% available in the first
# day (marginal hunting). The positive rate is 13.7% — a healthy balance for
# classification without severe imbalance.
EXPLOSIVE_MIN_PEAK = 1.0
EXPLOSIVE_HALF_AT_24H = 0.5
# The +20% strength-announcement hook: a candidate-dropping filter only —
# **never an entry rule** (owner's decision 2026-08-28: entering after +20% is a
# net loss of -1% to -11% across every window). Measured on the full history
# after the fill: explosive coins reach +20% in a median of 259 minutes vs 816 for
# non-explosive — a coin that has not announced its strength within ~4 hours =>
# dropping it from the watch saves with no loss of selection.
PLUS20_THRESHOLD = 0.20
# DEX Screener socials enrichment (fv16): one call per new coin at admission
# — stored in token_static and never asked again. The only measured added value
# from the source (full inventory 2026-08-29: the rest is duplicate or below what we already hold).
DEX_SCREENER_SOCIALS = True
# An unknown age is rejected.
# measured) has no date at all. And it is measured that ignorance **carries no news** about age:
# age-unknown coins were < two days old in 39.8% of cases vs 42.4% for the known
# ones — meaning accepting them admits the young at the population's own rate, so the gate collapses on a third of entrants.
# Admission of new EVM coins adapts to the holders-ledger fill capacity. Current
# coins are not deleted, and signals stay saved even when the watch is deferred.
# The resume band prevents oscillation near the pause cap: we stop at 45 and do not resume until the queue drops below 20.
EVM_ADMISSION_FULL_BELOW = 15
EVM_ADMISSION_REDUCED_BELOW = 30
EVM_ADMISSION_PAUSE_AT = 45
EVM_ADMISSION_RESUME_BELOW = 20
LEADERBOARD_REFRESH_SECONDS = 3600 # refresh the leaderboard every hour
# The source's cap is **50 leaders**, confirmed by a live probe (2026-08-10): nine
# pagination forms (limit/offset/skip/page/pageNumber/start/from/pageSize/take) are
# silently ignored — the id lists are byte-identical. So the value here does not
# raise the count; it stays because it is the parameter the source accepts and may honor one day.
LEADERBOARD_SIZE = 200             # number of leaders requested (the source caps it at 50)
# The workaround for the cap: the leaderboard itself is split by period, and each
# period returns its own independent list — and measured on the archived raw
# (2026-08-10): the main one 50 while **every period returns 100**, so the union of
# the four is 214 distinct traders (164 unknown to the main one). On 7,200 real buy
# events from three days: the fifty match 265 (3.68%) vs 1,099 (15.26%) for the union, i.e. x4.15.
# "all" = the main path (totalPnL); the rest come from upstream_leaderboard_period_paths.
# **Each period's rank is an independent measurement**: rank 4 in 24h is not rank 4
# in totalPnL, so separate maps are kept and never merged into one number.
LEADERBOARD_PERIODS = ("all", "24h", "7d", "30d")
LEADERBOARD_PACING_SECONDS = 0.5   # gap between period calls (was 1.5 — the cycle budget)

# --- OHLCV candles (getBarsNew) ---
# The price source of truth for labeling. The trending/verified lists are not
# enough: a quarter of watched coins never appeared in them and stayed without any price at all.
BARS_RESOLUTION = "5"              # 5 minutes: 48 hours = 576 candles, under the 900 cap
BARS_PER_CYCLE = 9                 # coins per cycle — raised from 6 to absorb the control group
BARS_REFRESH_SECONDS = 900         # each coin's candles are refreshed every ~15 minutes
BARS_PACING_SECONDS = 0.3          # was 1.5: the single biggest waster (12s of 27.5) — see CYCLE_SECONDS
BARS_PRE_SIGNAL_HOURS = 2          # we also pull context from before the signal
BARS_MAX_SPAN_HOURS = 72           # cap on the requested window (below the 900-candle cap)
BARS_COUNT_BACK = 900              # the most fomo returns in a single call
# Automatic 1-minute candle archiving (2026-08-29): a follow-up step in the hourly
# builder cycle — every coin whose window matures (48h) and gets built has its 1m
# window archived in the same cycle with no manual intervention. A cap of 3
# coins/cycle matches the pace (~20 new coins/day) and does not compete with the
# build; and a partial failure is retried automatically in later cycles.
BARS_1M_ARCHIVE_PER_CYCLE = 3
# A coin that fomo answers with `no_data` this many times is excluded: it has no series.
BARS_MAX_NO_DATA_ATTEMPTS = 3

# --- Candle integrity: impossible wicks from the source ---
# fomo sometimes returns a physically impossible `h` (or `l`): h=2,626,092 was seen
# for a candle whose close was 0.0219 (a 119-million multiple!) — confirmed by a
# live re-pull on 2026-07-30, meaning it is a permanent distortion in the source,
# not a transport error on our side. Its impact on the label is catastrophic:
# max_gain became +3.78 billion % in 4 rows, and the dashboard showed
# +62,570,743,609%.
# The 10x threshold is measured on 650,679 candles: only 132 candles exceed 2x, 39
# exceed 10x, and 22 exceed 1000x — meaning legitimate tails practically end below
# 2x and everything beyond is distortion. At 10x we flag 0.009% of candles and protect every label.
# The flagged wick is **never deleted or fixed** (the raw is sacred): it is excluded
# from the peak/trough computation only, and the rest of the candle (o/c) is intact and used.
BAR_WICK_MAX_RATIO = 10.0
# A full day allows genuine pumps and crashes larger than 10x. The comparison with
# 5-minute candles showed 28 confirmed false flags at 10x, and zero at 1000x.
DAILY_BAR_WICK_MAX_RATIO = 1000.0

# --- The "major asset" threshold ---
# The archive is not meme coins only: measured 2026-07-30 there are 116 signals on
# BTC ($1.27T), ETH, SOL (103 signals), USDT, XRP, BNB, HYPE, and the tokenized
# SNDK stock — including 21 labeled outcomes. A trillion-dollar asset does not
# behave like a half-million coin, so mixing them corrupts any conclusion about
# "the market-cap effect". This threshold is the single reference for the `is_major`
# flag in analysis and the feature extractor (PLAN §2.3-c) — classification, not exclusion: the coin stays recorded.
MAJOR_ASSET_MARKET_CAP_USD = 1_000_000_000.0

# --- Asset classification (asset_class) ---
# Market cap alone **is not enough**: tokenized stocks measured with very small
# market caps (AAPL at $1.36M · DRAM $1.18M · NET $780k) because the tokenized
# share is a tiny fraction of the stock — so they look like "memes" to any candidate
# that relies on cap. **Price is the strongest discriminator**: a meme coin
# practically never trades above a few dollars (measured distribution: 267 of 297 coins below $0.1).
ASSET_MEME_MAX_PRICE_USD = 5.0        # a higher price => not a meme (stock/commodity/major asset)
ASSET_STABLE_PRICE_BAND = (0.95, 1.05)  # all observations inside the band => a stablecoin
# A market-cap plausibility ceiling: above it we do not trust the number ($69T was
# seen for a coin at $0.0888 — price x mythical supply). We do not judge by it; we **ignore** it and judge by price.
ASSET_MAX_CREDIBLE_MARKET_CAP_USD = 5e12
# A name-trust floor: meme coins forge the majors' tickers (measured: "BTC" with a
# $3.5M cap and "SOL" at $4.9M). So the name list applies only with a credible market cap.
ASSET_SYMBOL_TRUST_MIN_MARKET_CAP_USD = 1e8
# A name-based safety net for assets whose price may fall below the threshold
# (XRP ~$1) — a short explicit list is more honest than fragile inference, and every
# name in it was actually observed in our archive.
ASSET_NON_MEME_SYMBOLS = frozenset({
    "BTC", "WBTC", "CBBTC", "ETH", "WETH", "SOL", "WSOL", "BNB", "WBNB", "XRP",
    "LTC", "DOGE", "ADA", "AVAX", "LINK", "UNI", "AAVE", "HYPE",
    "USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "PAXG", "XAUT",
})

# The Solana network id. One source of truth: the interpretation of the mint/freeze
# authorities depends on it (Solana alone has both; on EVM it is unmeasured, not "safe").
# extract._authority_to_int.
SOLANA_NETWORK_ID = "1399811149"

# --- Whole-market (macro) candles ---
# The market-regime reference: a coin +40% during a general pump is not the same as
# +40% on a dead day. The control group is a partial substitute (~100 noisy coins);
# the reference candles are clean and free via getBarsNew (confirmed live
# 2026-07-28). They are stored in token_bars at hourly resolution so they never
# collide with the watched coins' candles (5 minutes), and the labeler does not read them (they have no watch).
MACRO_BARS = (
    ("SOL",  "So11111111111111111111111111111111111111112", "1399811149"),
    ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "1"),
    ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "1"),
)
MACRO_BARS_RESOLUTION = "60"          # hourly — enough for regime context, and light on the cycle
MACRO_BARS_REFRESH_SECONDS = 3600     # one pull per hour
MACRO_BARS_SPAN_HOURS = 24 * 14       # two weeks per pull (the full pull heals the gaps)

# --- The labeler (labeler — the separate FomoLabeler process) ---
# Separate from the recorder by design: the recorder records raw only (preventing
# future leakage), and labeling happens only after the window completes.
LABEL_WINDOW_HOURS = 48            # the outcome window — matches the watch window
LABEL_MARGIN_SECONDS = 900         # margin after the window: the last candle closes and the sweep catches it
LABEL_INTERVAL_SECONDS = 900       # the labeler's cycle (every 15 minutes)
LABEL_BATCH = 500                  # the labeling cap per cycle
LABEL_ENTRY_MAX_LAG_SECONDS = 1800 # the entry candle's max lag, else status=no_entry
LABEL_INDEPENDENCE_GAP_SECONDS = 1800  # the gap for a signal to count as independent (69% < 5 minutes!)
LABEL_RUG_THRESHOLD = -0.90        # final return <= -90% => is_rug=1
LABEL_LOG_PATH = os.path.join(HERE, "labeler.log")

# --- Training rows build (build rows — the separate FomoBuildRows process) ---
# Stage three: flattening the labeled outcomes into a single features row.
# Incremental and a no-op when repeated (INSERT OR REPLACE on (kind,key)), so
# scheduling it is safe. The cycle is an hour, not 15 minutes: the labeler produces
# no new rows until a 48-hour window closes, so a faster build scans a table with nothing new in it.
BUILD_ROWS_INTERVAL_SECONDS = 3600
BUILD_ROWS_BATCH = 500             # the single batch size (write, then release memory)
# The rows cap per cycle. It matters when FEATURE_VERSION is raised: every row
# becomes pending at once (~53k), and the cap makes them get bitten across cycles
# instead of locking the database for an hour and a half against the recorder and the labeler.
BUILD_ROWS_MAX_PER_CYCLE = 4000
BUILD_ROWS_LOG_PATH = os.path.join(HERE, "build_rows.log")

# --- Archive retention ---
# Delete raw snapshots older than this many days. 0 = no deletion (the default):
# the snapshots are the re-derivation archive, so deleting them is the owner's
# explicit decision. With zlib compression the database grows ~280 MB/day instead
# of ~1.3 GB, so keeping them forever is reasonable.
SNAPSHOT_RETENTION_DAYS = 0
# Rotate recorder.log when it exceeds this size (bytes). 0 = no rotation.
LOG_MAX_BYTES = 5 * 1024 * 1024

# --- The social layer (/feed/token/thesis) ---
# Meme coins are moved by crowds; we were recording price and volume but not the
# discussion around them. A slower cycle than candles: social momentum changes over hours, not minutes.
SOCIAL_PER_CYCLE = 5               # was 7 (and 4 before it) — cut in the volume cut, see
                                   # the "volume cap" above. 189/5 = 37.8min per sweep, under
                                   # the new 40min window. The reason is that social
                                   # momentum changes over hours, not minutes, so a
                                   # tenfold coarser cadence is the cheapest price in the whole file.
SOCIAL_REFRESH_SECONDS = 2400      # was 1800 — raised with the cap so a deficit does not accumulate
SOCIAL_ERROR_RETRY_SECONDS = 300   # a transient error is retried within 5 minutes, not 30
SOCIAL_PACING_SECONDS = 0.3        # was 1.0 — the cycle budget
SOCIAL_THRESHOLD = 0               # 0 = no minimum for an author's share (we want everyone)

# --- Ownership concentration (/proxy/tokenDetails + /hodlers/top) ---
# The column `market_ticks.top10_holders_pct` is dead: zero of 1,430,475 rows,
# because the trending/verified lists do not carry the key at all. These two sources
# work on both networks: tokenDetails gives top10 and the holder count, and
# hodlers/top gives the detail, so we derive top1 (a single dangerous whale is
# different from ten distributed ones).
HOLDERS_PER_CYCLE = 8               # was 13 (and 6 before it). **The single largest
                                    # block in the cycle**: two calls per coin
                                    # (tokenDetails + hodlers/top), so 13 was 26 calls
                                    # = half the recorder's entire load. So it is the
                                    # first thing cut and the biggest saving: 8 => 16
                                    # calls, i.e. -10 of -14. 189/8 = 23.6min per
                                    # sweep, under the new 25min window. And the cost
                                    # is real and must be known: the concentration
                                    # snapshot at t0 was present in 9.8% of training
                                    # rows before the 6->13 raise, and this cut walks
                                    # it back toward that direction, not to its value.
                                    # If coverage at t0 collapses, this is the first
                                    # constant to restore.
HOLDERS_REFRESH_SECONDS = 1500      # was 900 — raised with the cap so a deficit does not accumulate
HOLDERS_ERROR_RETRY_SECONDS = 300
HOLDERS_PACING_SECONDS = 0.2        # was 0.5 — the cycle budget

# --- Closing the measurement gap (/proxy/filterTokens) ---
# `market_ticks` used to be filled from two public lists only, so a coin that fell
# out of them stopped being measured while still inside its 48-hour window:
# measured on the live database, 53 of 189 active watches had no snapshot for two
# hours, and 35 had never been measured even once. That is, we were losing exactly what we claim to measure.
#
# `filterTokens` asks for our addresses by name, so it returns all of them
# regardless of popularity. **Gap-fill mode, not a full sweep**: we ask only for
# what trending/verified did not pick up this cycle (about 53 addresses => one
# call, 0.12 GB/day). A full sweep costs three times as much and adds nothing:
# this source is as poor in counters as verified, so it cannot substitute for a coin trending already covered.
FILTER_TOKENS_BATCH = 150           # measured: 150 addresses/call return 150/150
FILTER_TOKENS_PACING_SECONDS = 0.5  # between batches when the gap exceeds one batch
# Splitting the filterTokens gap into alternating slices (2026-08-27): every
# change_*/volume_* arrives **ready-made** inside each tick and the features read
# only the newest tick (market_features), so per-minute density is excess
# freshness, not a time series. Live measurement: 79 addresses outside the public
# lists measured every cycle = ~17% of fomo's daily calls. Slicing measures each
# coin every N cycles with no column loss at all; N=2 halves the gap (freshness <=
# two minutes instead of one) — and that is enough, because the trending/verified
# ticks of the public lists themselves refresh at a slower rate.
FILTER_TOKENS_STRIDE = 2            # 1 = the old behavior (every cycle)

# --- Trader profiles (/v2/users/{id}) ---
# `signal_events.buyer_id` has been stored since day one and there is no traders
# table in the database: 5,572 distinct ids, **3,202 of them with >=3 events**. So
# the question "is this buyer skilled or does he buy everything?" had no answer
# even though the answer is one call away. Whoever appears once has no behavior for
# us to learn => only the repeaters are fetched.
# **And the whole accounting above became meaningless on 2026-08-20**: the per-id
# call `/v2/users/{id}` died at the source (404 "User not found" for every
# well-formed id, including ids that had just come out of a 200 on
# `/v2/leaderboard`), so the recorder spent 21 hours writing `empty` for every
# trader: the last successful row 2026-08-19T14:53:51Z, then silence. And nothing
# alerted, because 404 and "deleted account" were a single case in the code.
#
# The replacement is a batch: `/v2/users?userIds=...&userIds=...` returns 100
# profiles in **one call** (the source declares the limit in a validation error). So the cap no longer means calls:
#
#     Method        calls/cycle  coverage/cycle  sweep of 4,685 repeaters
#     per-id (4)         4          4             19.5 hours
#     per-id (2)         2          2             39.0 hours   <- and all of them 404
#     batch (50)         1         50             94 minutes
#
# I.e.: **fewer calls than the reduced state, and coverage twenty-five times
# faster**. And after the first catch-up (94 minutes) the demand shrinks on its
# own: only whoever crossed the window still qualifies, and that is ~3.3 traders a
# minute, so the batch becomes small with no new cap.
TRADERS_PER_CYCLE = 50              # now "how many traders", not "how many calls": one call.
TRADERS_BATCH_MAX = 100             # the source's declared limit. 50 is half of it on purpose:
                                    # the request is one line in the URL (~45 chars per id),
                                    # and there is no reason to touch any URL-length limit
                                    # for the profit of half a call per cycle.
TRADERS_MIN_EVENTS = 3              # the threshold that makes a trader worth fetching
TRADERS_REFRESH_SECONDS = 86400     # back to one day from two: the "sweep under the
                                    # window" rule is now met with a huge margin (94
                                    # minutes under 24 hours) instead of being broken by 26.7 hours.
TRADERS_ERROR_RETRY_SECONDS = 3600
TRADERS_PACING_SECONDS = 0.2        # was 0.5 — the cycle budget

# How many consecutive cycles of "we asked and not one row came down" before an
# error line is written that shows up in the dashboard. Three, because the traders
# call is one per cycle and asks for 50: all fifty deleted three times in a row is
# not a possibility (measured: 7 real absences out of 500), so the third time is
# certainty, not doubt. And a bigger number would have cost hours of silence for
# nothing: the outage that forced this counter lasted 21 hours, and three minutes would have been enough to catch it.
SHUTOUT_STREAK_ALERT = 3

# --- The chain layer (Solana RPC via Helius) — a separate process, not a step in the cycle ---
# Why separate: the recorder cycle is 60 seconds and its budget is full (pacing was
# eating 27.5s of the 60). The chain layer has its own rhythm and its own speed
# limit, and mixing it into the cycle makes a slow external source delay the
# collection of FOMO itself. And because it is separate, a 5-minute cadence became
# **possible** after being impossible inside the cycle: 73 active Solana coins / 5
# = 15 calls a minute, vs 13 that the whole cycle tolerates for everything.
#
# The speed limit is measured on the key, not assumed: 24 of 24 succeeded at
# parallelism 3 => **224 calls a minute without a single failure** (median 216ms).
# And at parallelism 10 it degrades: 32 of 40 with -32603/-32600 errors and one
# call waiting 17 seconds. => we pace, we do not crowd; our need of 15/min = 7% of
# the comfortable capacity, so there is no reason to approach the limit at all.
CHAIN_INTERVAL_SECONDS = 60         # the layer's cycle, every minute
CHAIN_PER_CYCLE = 20                # 73 active / 20 = a full sweep every ~3.7 minutes
CHAIN_REFRESH_SECONDS = 300         # the declared target: a concentration snapshot every 5 minutes
CHAIN_ERROR_RETRY_SECONDS = 120     # unknown != safe: errors are retried fast
CHAIN_PACING_SECONDS = 0.4          # the gap **between** calls (parallelism 1, not 3)
CHAIN_TIMEOUT_SECONDS = 20.0
# **A transient failure is not key rejection**, and the two classes were once
# mixed: `HTTP 522` (a Cloudflare timeout to the origin, measured 2026-08-17), 5xx,
# and `ReadTimeout` are failures of seconds that the key has nothing to do with =>
# rotating it does not cure them and cools a healthy key for no reason; the right
# move is to wait and retry the **same** key. And the retry budget used to be
# derived from the number of keys (`attempts = len(keys)`), so with one key it
# became a single attempt with no wait — for a class whose definition is transient
# => separated here. And the value is bound by the cycle budget, not by
# preference: one extra attempt costs one second for the failing coin, so even if
# all twenty fail (a total outage) the minute stays intact — unlike two attempts at one and a half seconds.
CHAIN_TRANSIENT_RETRIES = 1
CHAIN_TRANSIENT_BACKOFF_SECONDS = 1.0
# The batch request is one, but the node may answer each method at a near slot, not
# an identical one. A larger gap puts the numerator and denominator at two different moments, so the measurement is rejected instead of a misleading ratio.
CHAIN_MAX_SLOT_LAG = 32
# The slow layer (mint authority, mutability, developer holding). An hourly rhythm
# because what it measures changes once in a coin's lifetime if at all — and that
# "once" is an event we must catch, so we neither drop the layer nor ask it every
# five minutes. 6 coins a minute x 60 = a full sweep of 72 active in ~12 minutes,
# so each coin is measured 5 times an hour at the `REFRESH` cap — and budget-wise
# two calls per coin (the third is conditional), so the cost is ~0.2 calls/second on top of the fast layer.
CHAIN_AUTH_PER_CYCLE = 6
CHAIN_AUTH_REFRESH_SECONDS = 3600
CHAIN_AUTH_ERROR_RETRY_SECONDS = 900
# The networks this layer accepts. Solana alone **for this layer**: the ERC-20
# standard does not carry an on-chain holder list, so there is no
# `getTokenLargestAccounts` equivalent on EVM. EVM networks are measured by another
# layer (`evm_layer.py`) that builds the balances ledger from transfers.
CHAIN_NETWORKS = ("1399811149",)
SOLANA_RPC_URL = "https://mainnet.helius-rpc.com/"
# Wrapped SOL is a macro/reference asset as well as a possible FOMO signal.
# Helius cannot answer getTokenLargestAccounts for it because the mint has
# millions of token accounts; excluding it keeps the chain queue error-free.
CHAIN_UNSUPPORTED_TOKENS = frozenset({
    "so11111111111111111111111111111111111111112",
})
CHAIN_LOG_PATH = os.path.join(HERE, "chain.log")

# --- The EVM layer: a balances ledger we build from Transfer logs ---
# Why a ledger of our own and not a provider: measured on the live watch 2026-08-13.
#   · Etherscan V2 refuses without a key, old V1 is deprecated, and Sourcify knows 1 of 10.
#   · Blockscout is free without a key but takes **5.6 seconds per coin** (no 429 —
#     just slow) => sweeping 57 Robinhood coins = 5.5 minutes, and only the top 50 holders.
#   · NodeReal works on BSC alone (27 of 27, 1.07s per coin) and its free tier is
#     enough for an hourly rhythm and no more.
# Against that: the official node is free and keyless, and **one `eth_getLogs`
# call covers every coin of the network together** (measured: Robinhood 57
# addresses in 0.5s, Base 22 in 0.4s). So the ledger is cheaper, more precise, and
# faster in rhythm: an **exact** holder count (no free provider gives it on EVM,
# and `token_holders.holder_count` from FOMO is empty for every EVM coin) and
# any-count-top, not top 50.
EVM_INTERVAL_SECONDS = 60
# Each network's node URL. **Measured**, not assumed:
#   4663 Robinhood: the official node, 57 addresses in one filter ✅ · block 0.101s
#   8453 Base    : the official node, 22 addresses ✅ · block 2.00s
#                  (publicnode drops above 5 addresses with 403 => not an option)
#   56   BSC     : publicnode with a **5-address** filter cap => split into batches
#                  (bsc-dataseed rejects block ranges outright)
EVM_RPC_URLS = {
    "4663": "https://rpc.mainnet.chain.robinhood.com",
    "8453": "https://mainnet.base.org",
    "56": "https://bsc-rpc.publicnode.com",
    "143": "https://rpc.monad.xyz",
}
# Monad publishes a separate official service for historical state. We use it only
# for the binary search of the contract's creation block; `eth_getLogs` stays on
# the normal RPC because the service does not widen its limit.
EVM_HISTORICAL_RPC_URLS = {"143": "https://rpc-mainnet.monadinfra.com"}
# Both nodes support historical `eth_getCode` enough to search for the first block
# where the contract appeared. Starting from the creation block safely skips an empty history.
EVM_CREATION_BLOCK_NETWORKS = ("8453", "143")
# The most addresses in one filter per network — the provider's limit, not our choice.
EVM_ADDRESS_BATCH = {"4663": 60, "8453": 60, "143": 40, "56": 5}
# The measured cap on the `eth_getLogs` range. Zero means adapt by halving. The
# two values stop us from wasting most of the cycle cap on rejecting ranges known in advance, before the actual reading begins.
EVM_LOG_RANGE_HINT = {"8453": 10_000, "143": 100}
# How many `eth_getLogs` calls are packed into one HTTP request (a JSON-RPC
# batch). **Every number is measured live on 2026-08-19 on a real watched coin;
# not one of them is an estimate**:
#   8453 Base : ten as the cap, but the real limit is **the response size, not
#               the count** — `-32020 backend response too large` drops the
#               **whole batch atomically**, and the largest accepted batch differs
#               per coin: 10 and 5 and 3 and 2 and 1 were measured for five coins.
#               So the cap is adaptively reduced on rejection, never fixed.
#               And the gain is real: 5x10,000 blocks in 1.16s vs 0.88s for the single call.
#   143 Monad : 40 — under QuickNode's measured 50/second limit for subrequests, with margin.
#   4663 Robinhood: **one**, i.e. no batching. 4x100,000 blocks was tried and
#               worked individually (400k blocks in 1.45s), but the node throttles
#               with HTTP 429 on the **cost** of the request, not its count:
#               eight consecutive requests at a 1.5s rhythm gave 18 subrequests of
#               32 (54,572 blocks/s), and at a 2.0s rhythm 24 of 32 (67,516
#               blocks/s) — and both are **less** than the 71,000 blocks/s of the
#               single call. So batching here buys throttling, not speed.
#   56   BSC  : one — the network is outside `EVM_NETWORKS` anyway (no free archive node).
EVM_BATCH_SIZE = {"8453": 10, "143": 40, "4663": 1, "56": 1}
# And a pacing of its own for batches: a single request now carries the work of
# ten, so the 0.4s gap suited to a single call invites throttling. Measured on
# Base: nine requests in a ~15s window => 1.5s.
EVM_BATCH_PACING_SECONDS = 1.5
# The networks where the contract's creation block is found by a **mint scan**:
# one `eth_getLogs` call over the range `0->head` with two topics `[Transfer,
# 0x0]` (a mint from the zero address). This is the only way on Robinhood: its
# node has no archive (~128 blocks deep), so the binary search on `eth_getCode`
# is impossible — which is why the replay used to start from **block zero**. And
# the 2026-08-19 measurement says how expensive that was: four watched coins were
# minted at 0.2%, 67.7%, 88.6%, and 93.6% of the chain, meaning three of them
# walked through 27-37 **million** empty blocks before the first transfer. And
# the mint scan answers in **one 0.17s call**. And Base/Monad are excluded, not
# forgotten: their range cap (10,000 and 100 blocks) forbids a `0->head` query
# outright (HTTP 413), and they have the archive => `EVM_CREATION_BLOCK_NETWORKS`.
EVM_MINT_SCAN_NETWORKS = ("4663",)
# The networks actually enabled. Widened after verifying on the first one, not before.
EVM_NETWORKS = ("4663", "8453", "143")
# Confirmation delay in blocks: we do not process a block that may be replaced.
# Robinhood and Base are layer-2s with a sequencer (reordering is nearly zero) and
# BSC reorders ~3 blocks => a margin of 12 is enough for all three, and it costs
# Robinhood only 1.2 seconds of freshness.
EVM_CONFIRMATIONS = 12
# The cap on log records in one response at the source. Measured live: Robinhood
# refuses with `logs matched by query exceeds limit of 10000` => we halve the range and retry.
EVM_LOG_LIMIT = 10000
EVM_BACKFILL_MAX_CALLS = 24         # cap on the initial backfill calls for a single coin
# How many blocks one backfill call actually covers — for measuring the queue's
# work in the admission gate (`evm_admission_policy`). Measured from the live
# evm.log (2026-08-29): the backfill covers hundreds of thousands of blocks per
# call — 1,714 transfers in one call on Base, and a whole Robinhood coin (79,894
# transfers) in only 33 calls. The old estimate (unit = the 10K range cap)
# inflated the work x25 and kept the gate paused forever despite the real backfill progress.
#   4663: no range cap (adaptive halving), and the measurement is ~500K blocks/call for the empty range.
#   8453: a 10K result cap; the effective value per call = the range cap itself.
#   143:  a 100-block/call cap — it never exceeds it.
EVM_BACKFILL_BLOCKS_PER_CALL = {"4663": 500_000, "8453": 10_000, "143": 100, "56": 10_000}
EVM_BACKFILL_TOKENS_PER_CYCLE = 3   # new coins backfilled in one cycle
# And a cap **in time** on top of the count cap, and it is the one that actually
# protects the period: a single call can hang for up to `EVM_TIMEOUT_SECONDS`, so
# "24 calls" may be two seconds or eight minutes. Measured on the second live
# cycle: 72 calls (3 coins x 24) consumed **118 seconds** against a period of 60.
# And the backfill is the only thing that gets cut: it resumes from its checkpoint
# in the next cycle with no lost records. And a value of 25 keeps the cycle at
# ~50s in the worst case (25 budget + one hung call), not 118.
# **And what it protects is not Solana** — the 08-13 wording said "the fast layer
# on Solana in the same process", and `chain_layer` runs in another process
# (`run_chain.py` / FomoChain, whose only production entry is `run_chain.py:123`).
# What shares this process and this cycle is the four steps **after** the backfill
# in `run_evm_replay.run_cycle`: the EVM concentration snapshots (step 3 of
# `run_evm_cycle`), then `bsc_layer`, then `evm_contract` (three networks since
# 08-22, not one), then `evm_replay` with its 120s budget. So the cap protects
# more today than when it was written, not less => **do not raise it to speed up the backfill**.
# (Measured 08-22: an outdated reason inside a numerically correct comment is the
# same thing that kept `EVM_CONTRACT_NETWORKS` stuck on Base for nine days.)
EVM_BACKFILL_BUDGET_SECONDS = 25.0
# Per-network budgets feed admission decisions independently. A provider/RPC
# slowdown on one network must not consume the acceptance budget of another.
EVM_BACKFILL_BUDGET_SECONDS_BY_NETWORK = {
    "4663": 25.0,
    "8453": 25.0,
    "143": 25.0,
}
# QuickNode counts JSON-RPC subrequests inside a batch.
EVM_RPC_SUBREQUEST_LIMIT = {"143": 50}
# The backfill starts from **block zero**, not from the 48-hour window: a balance
# is not a delta but an accumulation, so one missed old transfer leaves every
# holder short forever with no visible error. And the cost is practically zero —
# a filter without a range returns the coin's whole history in one call if it is
# under the cap (measured: 1,814 transfers in 0.7 seconds), and the halving handles whatever exceeds it.
EVM_BACKFILL_FROM_BLOCK = 0
EVM_PACING_SECONDS = 0.4            # public nodes set the pace (a 429 was seen)
# And when they do throttle (429): a longer wait, then retry the **same** range.
# Measured on the first live cycle: a heavy backfill ended in a query timeout, so
# the node throttled the two calls after it. And the value is not a one-way guess
# — the call cap (`EVM_BACKFILL_MAX_CALLS`) is what stops the wait from eating
# the cycle, so two seconds are cheaper than a permanent hole in the ledger.
EVM_RATE_LIMIT_BACKOFF_SECONDS = 2.0
# `eth_blockNumber` anchors the whole cycle: its failure drops the entire network
# sweep, not one coin (`evm_layer.py`), and it was seen failing with
# `ReadTimeout` on Robinhood (4663 — 37% of signals) on 2026-08-17. One cheap
# call is not worth that price => it is retried. And the pointer does not advance
# on failure, so there is no hole in the ledger, but the minute is lost.
EVM_HEAD_RETRIES = 2
EVM_TIMEOUT_SECONDS = 25.0
EVM_SNAPSHOT_SECONDS = 300          # a concentration snapshot per coin every 5 minutes
# The cap on the periodic apply calls for a single network. The normal case is
# one call per network (measured: 57 addresses in 0.5 seconds), and the cap is for a noisy minute where ranges get split.
EVM_APPLY_MAX_CALLS = 12
# How many coins get snapshotted in one cycle. The snapshot is a database read
# with no network call, but gathering the balances is computational work => rotated like the other layers.
EVM_SNAPSHOT_PER_CYCLE = 20
EVM_LOG_PATH = os.path.join(HERE, "evm.log")
# Contract safety inspection. **All three networks** — widened 2026-08-22 by a
# measurement that overturned the first one. The verdict "Base alone" used to
# rest on BSC being identical copies of two implementation contracts (21 of 27
# proxies) and Robinhood six repeated templates => "constant, so no information".
# The mistake was that template repetition was measured in `code_size` alone,
# whereas `is_proxy` **is itself the information**:
#   BSC  56   : 26 of 40 proxies => a 65/35 split, the strongest discriminating
#               column we have measured on any network (Base 3 of 40, Robinhood 2
#               of 36). And `code_size` has 8 distinct values and `function_count`
#               7, and mint/limit/trading vary in 1 of 40 each.
#   RH   4663 : `code_size` 20 distinct values and `function_count` 16 — and they
#               vary in `has_pause` (1 of 36) where Base is constant. I.e.
#               **more** variance, not less.
#   Base 8453 : as first measured — 26 distinct size values, mint in 5 of 40.
# What is truly constant across all three: `has_blacklist` and `has_fee_setter`
# (always zero). And the cap is temporal, not quota-based: 106 watches across the
# three networks / 2 per cycle = a full sweep every 53 minutes, under the 3600s
# target. If the watchlist exceeds 120, the sweep would exceed the hour and rows
# go stale — and the fix then is a per-network rhythm, not a bigger batch,
# because the quota below is a single node's quota.
EVM_CONTRACT_NETWORKS = ("8453", "56", "4663")
# **The `mainnet.base.org` quota is measured, not estimated** (2026-08-13): nine
# successful calls then 429, and pacing does not change it — 0.4s and 1.0s both
# gave nine, so it is a count quota in a ~15s window, not a gap between calls.
# And one coin costs four calls (bytecode + three ownership forms) => two coins =
# eight, one call under the quota. It used to be four, and the node throttled the
# fourth in two consecutive live cycles. **So do not raise it to widen the
# networks**: the number is a single node's quota, not the layer's capacity, and
# three coins = twelve calls that Base throttles again. And measured 2026-08-22,
# the Robinhood node is narrower: 429 in 4 of 40 at a 0.4s pace in a single
# batch — and the live cycle sweeps two in sixty seconds, so it never approaches that.
EVM_CONTRACT_PER_CYCLE = 2
EVM_CONTRACT_REFRESH_SECONDS = 3600
EVM_CONTRACT_ERROR_RETRY_SECONDS = 900

# --- Retroactive replay: concentration rows for a past that has passed (`evm_replay.py`) ---
# The chain is an immutable, block-dated log, so replaying its transfers up to the
# block that was head at an old moment gives **exactly what was known at that
# moment** — no information from the future. And this is completely different from
# backfilling signals retroactively (which is rejected): that fabricates rows the
# bot never saw, while this completes the measurement of a row that exists.
#
# The networks available for replay are **measured, not chosen** (2026-08-13):
#   4663 Robinhood ✅ the node serves `eth_getLogs` from block zero with no range cap.
#   8453 Base     ✅ but with a **10,000-block range cap** (`-32614`, HTTP 413) =
#                    5.5 hours => ~22 calls for a coin's lifetime, and a quota of nine calls per window.
#   56   BSC      ❌ no free node serves history: publicnode refuses anything older
#                    than ~2000 blocks with "Archive requests require a personal
#                    token", bsc-dataseed rejects the filter outright
#                    (`-32005`), 1rpc caps at 50 blocks, and drpc throttles from
#                    the first call. So BSC needs a keyed provider.
#   143  Monad     ❌ **not for inability but for lack of work**: `watchlist` has
#                    zero active coins on 143 (vs 49 Robinhood, 23 Base, 35 BSC,
#                    and 82 Solana, measured 2026-08-19) and three inactive coins.
#                    Yet it was taking a third of the replay cycles and spending
#                    2,950 calls for **zero snapshots**, and its log printed
#                    `blocks 38963051->38963050`, i.e. without one block of progress.
#                    It returns the moment an active Monad coin appears — the whole path is ready.
EVM_REPLAY_NETWORKS = ("4663", "8453")
# The replayed snapshots' cadence = the live cadence itself: the `onchain_*`
# family reads a snapshot at/before t0 **and one 240-900s before it** to compute
# the five-minute deltas, so a network with one snapshot per training row gives
# all-null deltas. And a network on `EVM_SNAPSHOT_SECONDS` matches what the live
# layer would have written had it been running then.
EVM_REPLAY_STEP_SECONDS = EVM_SNAPSHOT_SECONDS
# The cap on log calls for a single coin. Wider than the live backfill cap (24)
# because this is a manual task with no minute period governing it, and Base
# needs ~22 calls for a coin's lifetime because of the range cap.
EVM_REPLAY_MAX_CALLS = 120
# Where we start searching for the coin's history: this far before its first
# appearance in our list. Starting after the first transfer makes balances
# **negative** (someone sent what we never saw them receive) — and that is exactly
# what the zero-sum check below catches, so the range is widened and retried.
EVM_REPLAY_LOOKBACK_SECONDS = 7 * 24 * 3600
# The distance between time<->block anchors, in blocks. Needed for Robinhood
# alone: its node returns `blockTimestamp: '0x0'` in every log (measured), so
# there is no time in the log itself, while Base and BSC return the real
# timestamp. And its block time is constant by a striking measurement (0.1002s
# and 0.1003s over 100k and 300k blocks) => interpolating between two adjacent anchors has an error of seconds.
EVM_REPLAY_ANCHOR_BLOCKS = 18_000
# And the cap on anchor calls for a single coin, **separate** from the log cap:
# one shared cap lets a noisy coin exhaust it on its logs, leaving its blocks
# without time — i.e. calls without rows. And the value is calculated, not
# estimated: a 48-hour window on Robinhood = 1.728 million blocks / 18,000 = 96
# cells in the worst case (a coin that moves in every half hour of the window).
# And the cost collapses after the first coins: an anchor is a property of a
# block, not of a coin, and the windows overlap.
EVM_REPLAY_ANCHOR_MAX_CALLS = 150
# And the retries for an anchor call under throttling. The need is measured, not
# assumed: the first live run died at the first coin with `EVMRateLimit` from
# `eth_getBlockByNumber` — the public Robinhood node throttles after nine calls
# (a quota, not a spacing: the same number at 0.4s and 1.0s). And the log path
# waits and retries the same range, so throttling in the anchor path alone would
# mean dropping a whole coin because of one crowded second. Four attempts x 2s =
# eight seconds at most per block.
EVM_REPLAY_ANCHOR_RETRIES = 4
# And a margin that is added to the interpolated time, never subtracted — the
# direction is deliberate: increasing a log's time pushes a boundary log out of
# the snapshot, and decreasing it pulls in a transfer from the future. So the
# allowed error is a measurement delay (like the block confirmations in the live
# layer), not an anticipation of it.
EVM_REPLAY_TIME_MARGIN_SECONDS = 5
# A window that ended this long ago is replayed up to its end block only; the
# active window follows the current head. This prevents pulling days of logs after a coin's watch has ended.
EVM_REPLAY_HEAD_GRACE_SECONDS = 900
# A time budget for the whole task when run with a cap (0 = no cap). The task
# resumes from its state, so cutting it loses nothing.
EVM_REPLAY_BUDGET_SECONDS = 0.0
# A **lifetime** cap on calls for a single coin in the replay, across all cycles.
# The `EVM_REPLAY_MAX_CALLS` cap is a cycle cap, not a coin cap, so a coin that
# never completes gets its resume retried forever: measured 2026-08-19 on Base,
# two coins spent 15,270 and 11,665 calls and ended `negative` with zero rows,
# and a third 5,660 calls and still `partial`. And the value is calculated: the
# longest real walk is a Base coin from the creation block (~341k) to the head
# (50.1 million) / the 10,000 range cap = ~4,960 calls, so 8,000 leaves a doubled
# margin for transient failures and cuts the drain before it reaches 15k. And
# the stop is recorded with an explicit final state (`budget`), not a state
# retried every cycle: a coin visible in the log is better than a coin silently eating the budget.
EVM_REPLAY_TOKEN_CALL_CAP = 8_000
EVM_REPLAY_LOG_PATH = os.path.join(HERE, "evm_replay.log")
# The scheduled replay worker processes one coin per cycle, then rotates across
# networks. The state is saved in `evm_replay_state`, so stopping loses no progress.
EVM_REPLAY_INTERVAL_SECONDS = 60
EVM_REPLAY_TOKENS_PER_CYCLE = 1
# The cycle budget for the replay. A Base coin needs ~22 calls for its lifetime
# at a 10,000-block range cap, so 120s finishes a meaningful window instead of
# being cut in the middle of one; and the live backfill assist has its own cap so neither eats the other's share.
EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE = 120.0
EVM_BACKFILL_ASSIST_BUDGET_SECONDS = 45.0
EVM_REPLAY_RUN_LOG_PATH = os.path.join(HERE, "evm_replay_run.log")
# A heartbeat line in the log when no work is happening. The worker can pass
# hours healthy without a single line (a cycle with nothing due), so a silent
# log looks exactly like a dead one — measured 2026-08-17: four hours of silence
# while it was working, and the diagnosis needed reading `meta` instead of it.
# 900s => four lines an hour: enough to tell life from death without drowning the rotation.
EVM_REPLAY_HEARTBEAT_SECONDS = 900
# The separate replay worker speeds up the live ledgers of the two networks that
# need it; Robinhood has a mature path and must not compete with Base/Monad for
# the assist budget. (143 is raised with it: zero active coins => the assist would spend budget on nothing)
EVM_BACKFILL_ASSIST_NETWORKS = ("8453",)
# **No keyed provider on the replay path, and that is a decision, not a lack.**
# GoldRush used to index Base and Monad events, and was deleted on 2026-08-19
# after two measurements: its account returns HTTP 402 since 2026-08-17, **and
# the more dangerous part**, its adapter was reading a single page of a
# paginated response and dropping events silently — and the ledger is
# cumulative, so the shortage does not show up as an error but as false
# concentration or a negative balance (trap #29). And the tally in the database:
# the network that never went through it is the only one that produced data
# (Robinhood 133 coins and 92,677 snapshots with 5,309 calls) vs 41,386 calls on
# Base with zero snapshots. So whoever wants a keyed provider here should read
# the trap first: the paginator is asked "is there a next page?" before its response is read.
#
# **Envio HyperSync is the one exception, added 2026-09-04** — after a
# measurement that answered the trap's question first: the paginated walk of a
# queue token returned 239 pages and 31,215 logs in 185s (the whole history,
# where the public-RPC ledger had recorded transfers=0 for weeks). The adapter
# (`envio_hypersync.py`) reads `next_block` on **every** page and exits
# incomplete without it — the GoldRush failure mode cannot occur by
# construction. It serves the backfill history reads of Base alone; live apply
# stays keyless on the public node (FR-012 holds everywhere except this one
# measured route), and a missing/rejected key falls back to the public node,
# never to an error. Alchemy and dRPC were measured on the same day and were
# **not** routed: both are range-capped at or below the public node's own cap
# (see `probe_evm_providers.py` — a route is earned by a measurement).
#
# --- The HyperSync fast lane (2026-09-04) ---
# The three caps below exist because the public node is a shared, rate-limited
# resource; the yearly HyperSync contract is not. They apply **only** to
# networks the EVM worker has stamped as covered this cycle (meta
# `evm_hypersync_covered`, written by `evm_layer.run_evm_cycle`), so a missing
# or rejected key silently returns the network to the public-node caps above —
# the stamp is the fact, not the config.
# No inter-page wait: `EVM_PACING_SECONDS` exists because a public node 429'd
# (measured 2026-08-17); HyperSync on the paid plan has no rate limit, and the
# page latency itself (~0.4s measured) is the real pace. A small courtesy gap
# remains rather than a true 0 — a burst of queries costs the service nothing
# but costs us nothing either to spread slightly.
EVM_HYPERSYNC_PACING_SECONDS = 0.05
# 24 calls per coin per cycle is a public-node ration; on the fast lane the
# coin's real limit is the shared cycle budget (`EVM_BACKFILL_BUDGET_SECONDS`,
# which still applies — it protects the snapshot/contract/replay steps that
# share the 60-second cycle, not the RPC). 100 pages ≈ the measured heavy
# token (239 pages) completes in 2-3 cycles instead of 10.
EVM_HYPERSYNC_MAX_CALLS = 100
# The admission gate's work unit for a covered network. The public value for
# Base (10,000 blocks/call — the range cap) made every fresh Base token look
# like ~3,400 work units against a capacity of 72, so the network re-paused the
# moment it opened: 10,948 coins deferred at admission (2026-09-04). HyperSync
# pages by log count, not blocks, so any blocks→pages figure is a calibration,
# not a law: the measured full-history token was 50M blocks in 239 pages ≈
# 210K blocks/page. 150K is the deliberately pessimistic side — it over-counts
# the work of a dense token (more pages than blocks suggest), which can only
# make the gate pause sooner, never admit beyond its means.
EVM_HYPERSYNC_BLOCKS_PER_CALL = 150_000
# How fresh the coverage stamp must be for the fast lane to apply. The EVM
# worker cycles every 60s and stamps every cycle, so 15 minutes is ~14 missed
# cycles — well past "the worker is dead", which is exactly when the gate must
# fall back to the public-node math.
EVM_HYPERSYNC_STAMP_FRESH_SECONDS = 900

# --- Live BSC measurement via NodeReal ---
# `nr_getTokenHolders` returns the top balances sorted, and
# `nr_getTokenHolderCount` returns the full count. This is a snapshot path independent of the historical Transfer ledger.
BSC_NODEREAL_NETWORK = "56"
# Five NodeReal calls per coin (count + supply + top 20 + the two burn balances)
# with the conservative-quota pacing; current BSC coins are swept in rotation in about 13 minutes.
BSC_NODEREAL_REFRESH_SECONDS = 900
BSC_NODEREAL_ERROR_RETRY_SECONDS = 300
BSC_NODEREAL_PER_CYCLE = 2
BSC_NODEREAL_PACING_SECONDS = 1.0
# The heaviest call (`nr_getTokenHolders`) consumes 300 CU, which is the entire
# CUPS of the free tier. A gap between every two calls keeps the 50/20 CU from joining it in the same second.
BSC_NODEREAL_CALL_PACING_SECONDS = 1.1



def chain_keys_path() -> str:
    """The chain provider keys file, beside the database and excluded from git.

    It supports both the old singular and the new lists:
    {"helius_api_keys":["...","..."], "nodereal_api_keys":["..."]}.
    And the EVM layer does not need it for the official nodes.
    It is read **from disk every cycle**, not once at launch — the same lesson as
    the session token rotation: a value frozen in a process that is not restarted
    fails after hours with no visible cause. And the key is never printed or logged (FR-013).
    """
    return os.path.join(HERE, "chain_keys.json")


# --- The control group (the negative class) ---
# Coins that enter monitoring **by random selection, not by signal**, recorded the
# same way. Without this class the model can learn "any signaled coin rises
# more", but it can never answer "does the signal mean anything at all" — when
# every coin with a price series entered via a signal, there is no reference to compare against.
CONTROL_GROUP_SIZE = 40            # the targeted number of active controls
CONTROL_PER_CYCLE = 2              # the admission cap per cycle — it spreads the sample over time
                                   # instead of grabbing 40 coins from a single market moment
CONTROL_WATCH_HOURS = 48           # the same window as the signaled coins (a fair comparison)
# v3 unifies the selection universe and the entry price: a signal enters the
# comparison only if it appeared in trending/verified in its admission cycle, and both sides' price comes from the same market snapshot.
CONTROL_DESIGN_VERSION = 3

# The signals that admit a coin into monitoring (the triggers). Hype/warnings are context, not a trigger.
TRIGGER_SIGNAL_TYPES = ("multi_user_buy", "large_buy")
# All the types we request from the feed. Selling is context, not a trigger:
# multi_user_sell (a group sell) and large_sell (a single whale distributing —
# confirmed live 2026-07-28, the same shape as large_buy in the opposite
# direction: inHumanAmount = the coin being sold). The labeler labels them like
# any other whenever candles are available, so a "leader/whale sold -> what
# happened to the price" record accumulates for free — input for the exit model later.
FEED_TYPES = ("multi_user_buy", "large_buy", "multi_user_sell", "large_sell")
FEED_LIMIT = 50
