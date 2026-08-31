-- Schema of the historical data recorder (recorder.db)
-- Governing principle: we always keep the raw payload (raw_json) alongside the extracted fields, so we can
-- re-derive any feature later from the archive without losing the past. The recorder computes no
-- label (preventing future leakage) — it only records raw data with a precise timestamp.
-- All timestamps are ISO-8601 UTC text (recorded_at) to avoid ambiguity.
--
-- Warning about raw_json columns: their value is a **zlib-compressed BLOB**, not text. SQLite
-- is dynamically typed and accepts that in a TEXT column. Do not read them with json.loads directly —
-- use db.decode_raw(), which decompresses and also accepts the old rows written
-- as plain text before compression was enabled. Reason for compression: a full snapshot every minute was growing the DB
-- ~1.3 GB per day; lossless compression cuts that ~4.7x. The key meta.raw_encoding
-- documents the encoding in effect.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- What we watch and when monitoring ends (48 hours per token).
CREATE TABLE IF NOT EXISTS watchlist (
    token_address    TEXT    NOT NULL,
    network_id       TEXT    NOT NULL,
    first_seen_at    TEXT    NOT NULL,           -- ISO UTC time of first entry
    source           TEXT    NOT NULL,           -- the signal that admitted it, or 'control'
    watch_until      TEXT    NOT NULL,           -- ISO UTC = first_seen_at + 48h
    entry_signal_id  TEXT,                       -- signal_events.id that admitted it (NULL for the control group)
    active           INTEGER NOT NULL DEFAULT 1, -- 1 active, 0 window expired
    -- 1 = **control** token: admitted by random selection, not by a signal.
    -- Without this negative class the model cannot know whether the signal means anything at all:
    -- every token has a price series that had been admitted by a signal, so there is no reference to compare against.
    is_control       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist (active, watch_until);
CREATE INDEX IF NOT EXISTS idx_watchlist_control ON watchlist (is_control, active);

-- Immutable record of every monitoring window. `watchlist` is runtime state that can be upgraded and
-- reactivated, whereas this table is the source of truth for comparison and labeling, and its window is never
-- updated after insert. The separation prevents promoting a control to a signal from erasing its original window.
CREATE TABLE IF NOT EXISTS watch_windows (
    token_address    TEXT    NOT NULL,
    network_id       TEXT    NOT NULL,
    first_seen_at    TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    watch_until      TEXT    NOT NULL,
    entry_signal_id  TEXT,
    is_control       INTEGER NOT NULL DEFAULT 0,
    admission_price_usd REAL,
    admission_source TEXT,
    design_version   INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY (token_address, network_id, first_seen_at)
);
CREATE INDEX IF NOT EXISTS idx_watch_windows_pending
    ON watch_windows (first_seen_at, source, is_control);

-- The decision moment t=0: a signal event from the feed (multi-buy / large buy).
CREATE TABLE IF NOT EXISTS signal_events (
    id                     TEXT PRIMARY KEY,     -- feed event id (idempotent)
    token_address          TEXT NOT NULL,
    network_id             TEXT,
    ts                     TEXT,                 -- original createdAt from fomo
    recorded_at            TEXT NOT NULL,        -- when we recorded it (ISO UTC)
    signal_type            TEXT NOT NULL,        -- type from the feed
    ticker                 TEXT,
    price_usd              REAL,
    fdv                    REAL,
    market_cap             REAL,
    num_trades             INTEGER,
    unique_traders         INTEGER,
    minutes                INTEGER,
    price_change_pct       REAL,
    total_volume           REAL,
    are_top_traders        INTEGER,              -- fomo's own verdict (bool)
    top_trader_ids_json    TEXT,                 -- topTraders[].id as a JSON array
    top_trader_match_count INTEGER,              -- how many of them are in our leaderboard (the match)
    buyers_best_rank       INTEGER,              -- best (smallest) rank among the buyers
    -- Period leaderboards: the source caps the base leaderboard at 50 and silently ignores every form of pagination,
    -- but /24h, /7d, and /30d each return 100 — the union is 214 distinct traders
    -- measured on 7,200 buy events: the match rate went 3.68% → 15.26% (×4.15). They stay separate, not
    -- merged: rank 7 in 24h is not rank 7 in totalPnL, and merging mixes two different measurements.
    -- NULL in all rows predating the column addition — absent ≠ zero (FR-007).
    top_trader_match_count_24h INTEGER,
    buyers_best_rank_24h   INTEGER,
    top_trader_match_count_7d  INTEGER,
    buyers_best_rank_7d    INTEGER,
    top_trader_match_count_30d INTEGER,
    buyers_best_rank_30d   INTEGER,
    top_trader_periods_matched INTEGER,          -- in how many leaderboards (0-4) a buyer appeared
    -- Single-buyer fields (large_buy): one buyer instead of topTraders[].
    buyer_id               TEXT,                 -- buyer's userId (also matched against the leaderboard)
    buyer_handle           TEXT,
    num_swaps              INTEGER,              -- number of swaps by this buyer
    is_first_buy           INTEGER,              -- is it his first buy of this token?
    buyer_pnl_pct          REAL,                 -- buyer's profit/loss at event time
    avg_cost               REAL,                 -- buyer's average cost
    -- Trade size: it was missing entirely, so a $1,000 trade and a $141,000 one
    -- looked completely identical to the model even though the 'large buy' median is only $3,448.
    size_usd               REAL,                 -- currentSizeUsd: his position size after the buy
    in_amount              REAL,                 -- inHumanAmount: what he actually paid
    in_token_address       TEXT,                 -- what he paid with (USDC/another token)
    out_amount             REAL,                 -- outHumanAmount: what he received
    -- What he received. Measured on 51,066 events: the counterparty is USDC in 100% of them, and the direction
    -- is determined by signal_type alone — no variance, so no feature from it. We capture it to catch the source
    -- starting to route token↔token (non-USDC) pairs, at which point it becomes useful.
    out_token_address      TEXT,
    token_amount           REAL,                 -- humanTokenAmount: total he holds
    realized_pnl_usd       REAL,                 -- realizedPnlUsd at event time
    -- Social engagement on the event itself — available in 100% of feed events and
    -- entirely wasted. `likes`/`views` measure the signal's spread, not its financial force:
    -- a $3,000 buy seen by 40,000 users outweighs a $30,000 buy nobody saw.
    -- `pinned` is an editorial decision by fomo (pinning the event) = deliberate amplification of reach.
    likes                  INTEGER,              -- likes (event top level)
    views                  INTEGER,              -- views (event top level)
    num_replies            INTEGER,              -- numReplies if present
    pinned                 INTEGER,              -- pinned by fomo (bool)
    -- fomo's tag for the event (body.tag). Measured on 4,000 events: a **single** value,
    -- 'Top Trader', in 3.4% of them — so the information is the tag's presence, not its text, and we store it as a boolean
    -- rather than as text. (If other values appear later, the raw payload keeps them.)
    is_top_trader_tagged   INTEGER,
    raw_json               TEXT NOT NULL         -- the full raw event
);
CREATE INDEX IF NOT EXISTS idx_signal_token ON signal_events (token_address, network_id);

-- A full market snapshot for every watched token, every minute. The primary source for time series.
CREATE TABLE IF NOT EXISTS market_ticks (
    token_address      TEXT NOT NULL,
    network_id         TEXT,
    recorded_at        TEXT NOT NULL,            -- ISO UTC time of the snapshot
    source             TEXT NOT NULL,            -- trending / verified / getBars ...
    price_usd          REAL,
    liquidity          REAL,
    market_cap         REAL,
    holders            INTEGER,
    top10_holders_pct  REAL,
    change_5m          REAL,
    change_1h          REAL,
    change_4h          REAL,
    change_12h         REAL,
    change_24h         REAL,
    volume_5m          REAL,
    volume_1h          REAL,
    volume_4h          REAL,
    volume_12h         REAL,
    volume_24h         REAL,
    txn_count_1h       INTEGER,
    txn_count_4h       INTEGER,
    txn_count_12h      INTEGER,
    txn_count_24h      INTEGER,
    buy_count_1h       INTEGER,
    buy_count_4h       INTEGER,
    buy_count_12h      INTEGER,
    buy_count_24h      INTEGER,
    sell_count_1h      INTEGER,
    sell_count_4h      INTEGER,
    sell_count_12h     INTEGER,
    sell_count_24h     INTEGER,
    unique_buys_1h     INTEGER,
    unique_buys_4h     INTEGER,
    unique_buys_12h    INTEGER,
    unique_buys_24h    INTEGER,
    unique_sells_1h    INTEGER,
    unique_sells_4h    INTEGER,
    unique_sells_12h   INTEGER,
    unique_sells_24h   INTEGER,
    circulating_supply REAL,
    total_supply       REAL,
    raw_json           TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at, source)
);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts ON market_ticks (token_address, recorded_at);

-- Token constants: captured once at admission (mint/freeze/creator/socials...).
CREATE TABLE IF NOT EXISTS token_static (
    token_address      TEXT NOT NULL,
    network_id         TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    name               TEXT,
    symbol             TEXT,
    decimals           INTEGER,
    mintable           INTEGER,
    freezable          INTEGER,
    is_scam            INTEGER,
    creator_address    TEXT,
    launchpad_name     TEXT,
    migrated           INTEGER,
    graduation_percent REAL,
    twitter            TEXT,
    telegram           TEXT,
    website            TEXT,
    discord            TEXT,
    token_created_at   TEXT,                     -- the token's createdAt from fomo
    token_created_at_observed_at TEXT,           -- when we actually learned createdAt
    -- External legitimacy signals: they were in the raw payload but never extracted. A token listed on
    -- CoinMarketCap or traded across several platforms is not a token launched an hour ago.
    -- We store the names, not just the count: 'Uniswap' is not 'PumpSwap'.
    exchanges_count    INTEGER,                  -- how many platforms trade it
    exchanges_json     TEXT,                     -- platform names (JSON array)
    cmc_id             TEXT,                     -- CoinMarketCap id (external listing)
    -- Description and images: their **presence** is the serious signal, not their content. Measured after the backfill
    -- on 517 tokens (not on the first 45 sample): a description in 217 (42%) and a banner in 181
    -- (35%) — variance usable for learning. As for `has_image`, it is constant: 1 in 517 of 517, so it
    -- carries no discrimination ⇒ archived, never a feature (like `out_token_address`).
    description        TEXT,
    description_len    INTEGER,                  -- description length (0 = no description)
    has_banner         INTEGER,                  -- imageBannerUrl present (bool)
    has_image          INTEGER,                  -- any image of the token present (bool)
    -- Pool protocol (PumpAmm/Uniswap...). Measured as **entirely absent from the trending
    -- raw payload** (zero of 3,000 items); it comes from filterTokens alone — at the item's
    -- top level, not under token. The launcher's pool is not a migrated pool.
    dex_protocol       TEXT,
    -- (fv16) socials from DEX Screener — asked once at admission and stored here:
    social_channels_dex INTEGER,          -- number of social channels at DEX
    social_match_fomo_dex INTEGER,        -- 1=the two sources agree, 0=conflict (forged-profile pattern)
    raw_json           TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id)
);

-- Last attempt to fetch the token's age, keyed by address+network. A reply without createdAt
-- is rejected at admission, but it does not consume a fresh call every cycle.
CREATE TABLE IF NOT EXISTS token_age_lookup_state (
    token_address TEXT NOT NULL,
    network_id TEXT NOT NULL,
    last_lookup_at TEXT NOT NULL,
    last_status TEXT NOT NULL, -- ok / missing / error
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);
CREATE INDEX IF NOT EXISTS idx_age_lookup_due
    ON token_age_lookup_state (last_status, last_lookup_at);

-- Periodic raw archive of every source in full (for future re-derivation).
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    source      TEXT NOT NULL,                   -- trending / verified / feed ...
    raw_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source_ts ON snapshots (source, recorded_at);

-- OHLCV candles for every watched token — the price source of truth for later labeling.
--
-- Why a table separate from market_ticks: the latter is fed by the trending/verified lists, which
-- are curated lists of ~46/~37 tokens, not a price digest. A quarter of the watched tokens never got
-- a single price point, and the rest were covered for only 52-75% of the minutes. getBarsNew, however,
-- gives a true price series for any token, with history preceding the signal.
--
-- ts is the candle's open in epoch seconds (as fomo returns it) — not ISO text, because numeric
-- comparisons sit at the heart of the max_gain/drawdown computation.
-- The newest candle may still be forming and is re-fetched on the next pull, so the insert is
-- OR REPLACE, not OR IGNORE: the newer value for the same timestamp is the correct one.
CREATE TABLE IF NOT EXISTS token_bars (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    ts            INTEGER NOT NULL,       -- candle timestamp (epoch seconds, UTC)
    o             REAL,
    h             REAL,
    l             REAL,
    c             REAL,
    v             REAL,
    -- Upstream corruption: a value exceeding its neighbors by ×10 that does not persist (see bar_context_flags).
    -- Excluded from computation only — the raw value stays as it arrived: raw data is never fixed.
    h_suspect     INTEGER NOT NULL DEFAULT 0,   -- impossible high (seen ×119 million)
    l_suspect     INTEGER NOT NULL DEFAULT 0,   -- impossible low
    c_suspect     INTEGER NOT NULL DEFAULT 0,   -- the close itself corrupted (seen 12,052.5)
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, resolution, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_token_ts ON token_bars (token_address, ts);

-- Candle-fetch state per token: the rotating scheduler is driven by it (we pull the oldest first).
-- last_status distinguishes 'no data at fomo' (no_data — usually final) from 'error'
-- (transient), so we do not waste attempts on dead tokens.
CREATE TABLE IF NOT EXISTS bars_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                    -- ok / no_data / error
    candles       INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- Long-history retrieval at daily resolution to compute the true ATH before t0.
-- The state stays `partial` until we reach the start of the series; the extractor must not use
-- the daily candles before `ok`, because the oldest (and possibly highest) part may not have been fetched yet.
CREATE TABLE IF NOT EXISTS historical_bars_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    cursor_to     INTEGER,
    oldest_ts     INTEGER,
    last_status   TEXT NOT NULL,       -- partial / ok / empty_retry / no_data / error
    candles       INTEGER NOT NULL DEFAULT 0,
    calls         INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, resolution)
);

-- The social layer for every watched token (POST/GET /feed/token/thesis).
--
-- Meme tokens are moved by crowds, not fundamentals, and this dimension was entirely missing: we record
-- price, volume, and holders, but not how many people are talking about the token or how many like
-- what they say. And we distinguish **is he shilling his own bag?** from the author's own position in the envelope.
-- A time series: the difference between two snapshots yields the **acceleration** of social momentum, which matters more
-- than the absolute level.
CREATE TABLE IF NOT EXISTS token_social (
    token_address    TEXT NOT NULL,
    network_id       TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    -- The **true** count from the envelope (responseObject.count). The response returns at most 100
    -- items while count can reach the thousands (seen 3111), so counting items
    -- alone saturates and loses the strongest discriminator in this layer.
    thesis_total     INTEGER,
    thesis_sampled   INTEGER,                    -- how many items we actually saw (≤100)
    has_next_page    INTEGER,                    -- are there pages left (truncated sample)
    thesis_count     INTEGER,                    -- = thesis_sampled (legacy compatibility)
    -- What follows is computed on the **sample** (latest 100), not on the total:
    thesis_likes     INTEGER,                    -- sum of likes
    thesis_replies   INTEGER,                    -- sum of replies (dead upstream: always 0)
    thesis_authors   INTEGER,                    -- distinct authors (no duplicates)
    -- of those, who holds a positive quantity right now (authorTrade.humanTokenAmount > 0).
    -- **Not** `equity`: that field exists in the envelope and is zero in 28,186/28,186
    -- measured theses, so this column was pinned at 0 until 2026-08-09.
    holder_authors   INTEGER,
    newest_thesis_at TEXT,                       -- newest thesis (freshness of the discussion)
    raw_json         TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_social_token_ts ON token_social (token_address, recorded_at);

-- One row per thesis — enables rebuilding the **historical count**: how many theses
-- existed at the moment of the signal? (`SELECT COUNT(*) ... WHERE created_at <= :t`).
-- This is what makes retrieving the past possible: every thesis carries its writing timestamp.
--
-- A fundamental caveat: `num_likes`/`num_replies` are the **value at fetch time**, not at
-- writing time. A two-day-old thesis has 50 likes today — how many did it have at signal time?
-- fomo keeps no record of that. Hence `fetched_at` is recorded: it is when the likes were measured.
CREATE TABLE IF NOT EXISTS token_thesis (
    id            TEXT PRIMARY KEY,             -- thesis id (idempotent)
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,                -- writing time — genuinely historical
    user_handle   TEXT,
    user_id       TEXT,
    num_likes     INTEGER,                      -- value at fetch time, not writing time
    num_replies   INTEGER,
    equity        REAL,
    trade_id      TEXT,
    comment       TEXT,
    fetched_at    TEXT NOT NULL,                -- when the likes were measured
    raw_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thesis_token_created
    ON token_thesis (token_address, created_at);

-- Historical activity events from GET /feed/tradingActivity (walking lastId backwards
-- in time — verified 2026-07-28; the forward /feed is 'newest only' and its pagination is ignored).
-- A source independent of signal_events: it collects the same multi_user_* events (match verified
-- live by id: 32/42) **and** the individual swap_buy/swap_sell events with usd_amount
-- that /feed never shows at all (0/42 match). A separate table so that the forward collection
-- stays clean; it is filled retroactively via backfill_activity.py only.
-- Two shapes: flat (swap_*/thesis: usdAmount/marketCap/price at the top) and nested
-- (multi_user_*: body with the same fields as /feed). The top trader's rank at event time is not
-- available historically — no top_trader_match_count here (derived later from the leaderboard archive).
CREATE TABLE IF NOT EXISTS activity_events (
    id                  TEXT PRIMARY KEY,       -- event id (idempotent)
    event_type          TEXT NOT NULL,          -- swap_buy/swap_sell/multi_user_buy/thesis/...
    token_address       TEXT,
    network_id          TEXT,
    ts                  TEXT,                   -- original createdAt (ISO UTC)
    recorded_at         TEXT NOT NULL,          -- when we fetched it (ISO UTC)
    user_id             TEXT,
    user_handle         TEXT,
    trade_id            TEXT,
    usd_amount          REAL,                   -- usdAmount (flat events)
    price_usd           REAL,                   -- from the top level or from body, depending on the shape
    market_cap          REAL,
    fdv                 REAL,
    equity              REAL,
    num_trades          INTEGER,                -- body fields for multi_user_* events
    unique_traders      INTEGER,
    minutes             INTEGER,
    price_change_pct    REAL,
    total_volume        REAL,
    are_top_traders     INTEGER,
    top_trader_ids_json TEXT,
    ticker              TEXT,
    raw_json            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_token_ts ON activity_events (token_address, ts);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_events (ts);

-- Asset classification per token: fomo is a multi-asset platform, not a meme-only market.
-- Measured 2026-07-30: BTC/ETH/SOL/USDT, PAXG gold, and tokenized stocks (AAPL/MSTR/HOOD/
-- INTC/META/SNDK/MU) are inside our archive. A trillion-dollar asset or an Apple share does not behave like a token
-- two hours old, so mixing them corrupts training and any conclusion about market-cap effects.
-- Fully derived from observations (extract.classify_asset) ⇒ it can be rebuilt any time
-- via classify_tokens.py, and the raw values do not change.
CREATE TABLE IF NOT EXISTS token_class (
    token_address  TEXT NOT NULL,
    network_id     TEXT NOT NULL,
    asset_class    TEXT NOT NULL,   -- meme | major | stable | priced
    reason         TEXT,            -- the rule that decided (for auditing, not for cosmetics)
    symbol         TEXT,
    price_min      REAL,
    price_max      REAL,
    market_cap_max REAL,
    observations   INTEGER,
    classified_at  TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id)
);
CREATE INDEX IF NOT EXISTS idx_token_class_class ON token_class (asset_class);

-- Retroactive candle-fetch state for activity_events (same pattern as bars_fetch_state).
-- Written by backfill_activity_bars.py only. The labeler does not label an activity event until
-- its token's status='ok' — otherwise it writes a permanent no_entry before the candles arrive (labeling
-- is idempotent and never revised). no_data after MAX_ATTEMPTS = the token has no series at
-- fomo (dead/decayed) — it is measured as a survival-bias rate in the retro set, not hidden.
CREATE TABLE IF NOT EXISTS activity_bars_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                    -- ok / no_data / error
    candles       INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- Social-layer fetch state (same pattern as bars_fetch_state, on a slower cycle).
CREATE TABLE IF NOT EXISTS social_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                          -- ok / empty / error
    items         INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- Outcomes (labels): filled by the labeler (the separate FomoLabeler process), not by the recorder
-- — the recorder records raw data only (preventing future leakage), and labeling happens only after
-- the 48-hour window closes.
--
-- The key is (kind, key), not (token, entry_ts): the old one was bound to collide — 201
-- signals on a single token. Two kinds get labeled:
--   kind='signal': key = signal_events.id — one training row per signal.
--   kind='watch' : key = token:network:first_seen — for every watch entry (including
--                  the control group) — to compare signal vs control on completed windows.
-- The same token appears in both kinds on purpose; each serves its own purpose.
CREATE TABLE IF NOT EXISTS outcomes (
    kind             TEXT NOT NULL,             -- 'signal' | 'watch'
    key              TEXT NOT NULL,
    token_address    TEXT NOT NULL,
    network_id       TEXT,
    signal_type      TEXT,                      -- the signal type, or 'control*'
    is_control       INTEGER NOT NULL DEFAULT 0,
    -- 1 = the token's first signal, or a signal that follows its predecessor by a gap of ≥30 minutes. 69% of signals
    -- are <5 minutes apart (false duplication) — train on the independent ones, do not delete the rest.
    is_independent   INTEGER,                   -- NULL for watch rows
    entry_ts         INTEGER NOT NULL,          -- epoch of the signal/entry moment
    entry_px         REAL,                      -- close of the first candle at/after entry
    entry_lag_s      INTEGER,                   -- how much the entry candle lags the moment
    max_gain_1h      REAL,                      -- peak gain within the window (ratio)
    max_gain_4h      REAL,
    max_gain_24h     REAL,
    max_gain_48h     REAL,
    max_drawdown_48h REAL,                      -- lowest trough (ratio, negative)
    final_return_48h REAL,                      -- window-end close / entry - 1
    time_to_peak_h   REAL,                      -- hours until the peak
    candles_48h      INTEGER,                   -- candles observed inside the window
    suspect_bars     INTEGER,                   -- candles with an impossible tail inside the window
                                                --   (their tails excluded from the peak/trough)
    last_bar_lag_h   REAL,                      -- how long before the window's end the series stopped
    bars_truncated   INTEGER,                   -- 1 = the series ended early (>1h)
                                                --   usually the token dying — a signal, not a gap!
    is_rug           INTEGER,                   -- 1 = final return ≤ -90%
    is_explosive     INTEGER,                   -- (fv15) 1 = peak ≥2x with half of it reached within 24h
    time_to_plus20_min REAL,                    -- (fv15) minutes until the first close ≥ +20%
    split            TEXT,                      -- train/val/test (fixed split per token)
    status           TEXT NOT NULL,             -- ok | no_entry | no_bars | incomplete
    labeled_at       TEXT NOT NULL,
    design_version   INTEGER NOT NULL DEFAULT 1,
    analysis_eligible INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_token ON outcomes (token_address);
CREATE INDEX IF NOT EXISTS idx_outcomes_split ON outcomes (split, kind, status);

-- The safe reading interface for phase 1: hides the old archive and the ineligible rows.
DROP VIEW IF EXISTS phase1_watch_outcomes;
CREATE VIEW phase1_watch_outcomes AS
SELECT o.*
  FROM outcomes o
 WHERE o.kind = 'watch'
   AND o.analysis_eligible = 1
   AND o.design_version >= 3;

-- The training table: one row per decision, one column per feature known **at t=0 or earlier**,
-- with the label attached from outcomes. Built by features.py via build_training_rows.py.
--
-- Fully derived ⇒ it can be dropped and rebuilt at any time with no network calls. The columns are text
-- or numeric per their nature, and the absent is NULL (FR-007: no fabrication — absence is information).
--
-- ⚠️ Do not write to it manually and do not add a column computed from after t=0: every column here must
-- answer 'was it known at the decision moment?' with yes. The tests plant post-t0 data
-- and verify that it does not show up.
CREATE TABLE IF NOT EXISTS training_rows (
    -- Row labeling
    kind             TEXT NOT NULL,      -- signal | activity | watch
    key              TEXT NOT NULL,      -- the decision key (= outcomes.key)
    token_address    TEXT NOT NULL,
    network_id       TEXT,
    entry_ts         INTEGER NOT NULL,   -- t=0 (epoch)
    asset_class      TEXT,               -- meme | major | priced | stable
    split            TEXT,               -- train/val/test (by token split)
    is_independent   INTEGER,
    is_live          INTEGER NOT NULL DEFAULT 0,  -- 1 = live (≥LIVE_START_TS), 0 = retroactive
    status           TEXT,
    suspect_bars     INTEGER,
    feature_version  INTEGER NOT NULL DEFAULT 1,
    built_at         TEXT NOT NULL,
    -- a) The triggering event
    signal_type      TEXT,
    size_usd         REAL,
    in_amount        REAL,
    out_amount       REAL,
    token_amount     REAL,
    avg_cost         REAL,
    price_to_avg_cost REAL,
    realized_pnl_usd REAL,
    num_swaps        INTEGER,
    is_first_buy     INTEGER,
    buyer_pnl_pct    REAL,
    market_cap       REAL,
    fdv              REAL,
    price_usd        REAL,
    log_market_cap   REAL,
    log_size_usd     REAL,
    size_to_mcap     REAL,
    unique_traders   INTEGER,
    num_trades       INTEGER,
    minutes          INTEGER,
    price_change_pct REAL,
    total_volume     REAL,
    volume_per_trader REAL,
    are_top_traders  INTEGER,
    top_trader_match_count INTEGER,
    top_traders_listed INTEGER,
    top_trader_match_ratio REAL,
    buyers_best_rank INTEGER,
    rank_le_10       INTEGER,
    rank_le_50       INTEGER,
    -- Period-leaderboard family (v7): each period's rank is an independent measurement, and the presence
    -- span (periods_matched) distinguishes a leader in all four from a leader in 24h alone.
    -- All NULL in rows built before the join was enabled (FR-007: 'not measured' is not 'zero').
    top_trader_match_count_24h INTEGER,
    buyers_best_rank_24h INTEGER,
    top_trader_match_count_7d INTEGER,
    buyers_best_rank_7d INTEGER,
    top_trader_match_count_30d INTEGER,
    buyers_best_rank_30d INTEGER,
    top_trader_periods_matched INTEGER,
    top_trader_any_period INTEGER,
    best_rank_any_period INTEGER,
    ticker_len       INTEGER,
    ticker_has_digit INTEGER,
    ticker_non_ascii INTEGER,
    hour_utc         INTEGER,
    dow              INTEGER,
    -- b) Token constants and the creator's fingerprint
    token_age_h      REAL,
    launchpad_name   TEXT,
    migrated         INTEGER,
    graduation_percent REAL,
    is_scam          INTEGER,
    mintable         INTEGER,
    freezable        INTEGER,
    socials_count    INTEGER,
    has_twitter      INTEGER,
    creator_prior_tokens INTEGER,
    name_len         INTEGER,
    name_non_ascii   INTEGER,
    decimals         INTEGER,
    -- External legitimacy: a centralized listing and a CMC id are not things a launcher can grant itself at the press of a button
    exchanges_count  INTEGER,
    listed_on_exchange INTEGER,
    has_cmc_id       INTEGER,
    description_len  INTEGER,
    has_banner       INTEGER,
    -- c) Social momentum: a historical count (sample) + a real snapshot before t0
    -- (without thesis likes — forbidden: their value is at fetch time, not writing time)
    thesis_counted   INTEGER,
    thesis_counted_capped INTEGER,
    thesis_authors_before INTEGER,
    thesis_1h        INTEGER,
    thesis_24h       INTEGER,
    thesis_accel     REAL,
    hours_since_last_thesis REAL,
    thesis_history_days REAL,
    social_thesis_total INTEGER,
    social_thesis_authors INTEGER,
    social_holder_authors INTEGER,
    social_holder_ratio REAL,
    social_replies   INTEGER,
    social_snapshot_age_min REAL,
    social_total_delta_1h INTEGER,
    social_total_growth_1h REAL,
    social_authors_delta_1h INTEGER,
    -- d) Price and volume path before the signal
    ret_1h_before    REAL,
    ret_4h_before    REAL,
    ret_24h_before   REAL,
    ret_7d_before    REAL,
    vol_24h_before   REAL,
    flat_ratio_24h   REAL,
    up_candle_ratio_24h REAL,
    pre_signal_runup REAL,              -- fv13: log-runup over the last 24h before t0
    dist_from_ath    REAL,
    ath_history_complete INTEGER,
    ath_history_days REAL,
    bars_history_h   REAL,
    bars_count_24h   INTEGER,
    bar_vol_1h       REAL,
    bar_vol_24h      REAL,
    vol_surge_1h     REAL,
    -- e) The last market snapshot before t0
    liquidity        REAL,
    holders          INTEGER,
    top10_holders_pct REAL,
    volume_24h       REAL,
    buy_count_24h    INTEGER,
    sell_count_24h   INTEGER,
    buy_sell_ratio_24h REAL,
    unique_buys_24h  INTEGER,
    unique_sells_24h INTEGER,
    tick_age_min     REAL,
    tick_change_1h   REAL,
    tick_change_4h   REAL,
    tick_change_24h  REAL,
    tick_volume_1h   REAL,
    tick_volume_4h   REAL,
    tick_txn_1h      INTEGER,
    tick_txn_24h     INTEGER,
    volume_to_liquidity REAL,
    liquidity_to_mcap REAL,
    float_ratio      REAL,
    -- Freshness of the counters alone: after merging sources, they may come from a row older than the newest,
    -- and without this the model would treat a two-hour-old counter as fresh as a one-minute-old price.
    tick_rich_age_min REAL,
    -- e2) Ownership: on-chain concentration (fixes the dead top10_holders_pct) and crowd positioning
    chain_top10_pct  REAL,                 -- top 10 as % of supply (token_details)
    chain_holder_count INTEGER,            -- total on-chain holders
    chain_holders_delta_1h INTEGER,        -- change in on-chain holders over an hour
    chain_holders_growth_1h REAL,          -- the change as a ratio (comparable across sizes)
    chain_holders_span_min REAL,           -- the span actually measured (≥60min, not =60min)
    holders_age_min  REAL,                 -- freshness of the latest holding measurement
    platform_holders INTEGER,              -- fomo holders (hodlers/top)
    platform_penetration REAL,             -- platform holders ÷ on-chain holders
    platform_underwater_ratio REAL,        -- share of losing positions (an extra view)
    platform_value_usd REAL,               -- total value of platform positions
    platform_median_hold_h REAL,           -- median holding duration (hours)
    platform_dev_holding INTEGER,          -- 1 = the developer is among the holders
    -- e2-b) Ownership measured **from the blockchain**, not from FOMO (chain_concentration).
    -- What this opens beyond the family above: top1 is a measurement, not a derivation (a single whale is a
    -- different risk from ten spread-out holders), and a 5-minute cadence where it used to be 25. **Both networks together**
    -- since feature version 12: the evm_layer builds a balance ledger from Transfer logs
    -- (ERC-20 carries no on-chain holder list, and the ledger is the only way)
    -- and writes into the **same** table ⇒ these columns cover EVM with no new column.
    onchain_top1_pct  REAL,                -- largest single account as % of supply
    onchain_top5_pct  REAL,
    onchain_top10_pct REAL,                -- mirrors chain_top10_pct ⇒ cross-check
    onchain_top20_pct REAL,
    onchain_top_accounts INTEGER,          -- the source returns 20 at most: a token
                                           -- with 7 holders returns 7, and its top20 = all of them
    onchain_age_min   REAL,                -- freshness of the on-chain measurement
    onchain_top1_delta_5m  REAL,           -- movement of the largest whale over ~5 minutes
    onchain_top10_delta_5m REAL,
    onchain_delta_span_min REAL,           -- the span actually measured (≥4min, not =5min)
    -- The holder count **exact** from the ledger, with no rank cap, and a five-minute window on it
    -- that no provider offers (they are all a snapshot with no entry history). EVM only: on Solana
    -- getTokenLargestAccounts returns 20 accounts at most and does not know the total ⇒ NULL.
    onchain_holder_count     INTEGER,
    onchain_holders_delta_5m INTEGER,      -- new holders − leavers over ~5 minutes
    -- e2-c) Structural risk from the chain (chain_authority, hourly cadence): who can
    -- mint new supply or freeze your sale. Measured: mint authority live in 3/48 and freeze in 1/48
    -- — rare, therefore discriminating. Developer holding from the chain covers ~15% (Token-2022 returns
    -- empty creators), and that is a measured absence, not zero.
    onchain_has_mint_authority   INTEGER,  -- 1 = an open minting door
    onchain_has_freeze_authority INTEGER,  -- 1 = can freeze your wallet
    onchain_is_mutable           INTEGER,  -- metadata changeable after the sale
    onchain_is_token2022         INTEGER,  -- a broader risk surface (extensions)
    onchain_dev_holding_pct      REAL,     -- from the chain, ≠ platform_dev_holding
    onchain_auth_age_min         REAL,
    -- e2-d) Its EVM counterpart (evm_contract, hourly cadence): the contract's shape and permissions from
    -- the **bytecode**, not from a ready-made field — ERC-20 carries no declared permissions, and the
    -- identifier's presence in the dispatch table is the proof. eth_getCode is free ⇒ 100% coverage versus
    -- 10% for any verification provider. **Base alone** — and this is a measurement: on BSC, 21/27 small proxies
    -- point to just two implementation contracts with no danger identifier at all, and on Robinhood six identical
    -- templates, while Base has sizes 135B–14.8KB, owner in 7/19, and mint in 2 ⇒ variance.
    onchain_code_size            INTEGER,  -- 0 = not a contract (a measurement, not a blank)
    onchain_function_count       INTEGER,  -- number of PUSH4 identifiers in the dispatch table
    onchain_is_proxy             INTEGER,  -- an EIP-1167 proxy ⇒ every flag below means
                                           -- nothing (the logic lives in another, swappable contract)
    onchain_owner_renounced      INTEGER,  -- NULL = no owner function ≠ 1 = renounced
    onchain_has_mint_fn          INTEGER,  -- can mint new supply
    onchain_has_pause_fn         INTEGER,  -- can halt transfers
    onchain_has_blacklist_fn     INTEGER,  -- can block your address alone
    onchain_has_fee_setter       INTEGER,  -- can raise the fee after you buy
    onchain_has_limit_setter     INTEGER,  -- can cap your sell size
    onchain_has_trading_switch   INTEGER,  -- trading is gated by a switch he owns
    onchain_contract_age_min     REAL,     -- the cadence is hourly ⇒ 55min is perfectly normal
    -- e3) Flow (token_flow): who buys and who sells — every other volume we have is an **aggregate**,
    -- and a 5-minute layer we never had anything like (our shortest window was an hour, blind to the turns).
    flow_age_min     REAL,
    flow_buy_volume_5m  REAL,
    flow_sell_volume_5m REAL,
    flow_net_volume_5m  REAL,              -- buy − sell (absent does not become zero)
    flow_net_volume_1h  REAL,
    flow_net_volume_24h REAL,
    flow_buy_sell_volume_ratio_5m  REAL,   -- the ratio is the information, not the absolute value
    flow_buy_sell_volume_ratio_1h  REAL,
    flow_buy_sell_volume_ratio_24h REAL,
    flow_buy_count_5m   INTEGER,
    flow_sell_count_5m  INTEGER,
    flow_unique_buys_5m  INTEGER,
    flow_unique_sells_5m INTEGER,
    flow_buy_sell_count_ratio_5m REAL,
    flow_unique_ratio_5m REAL,             -- unique ÷ trades: low = wash/bot
    flow_trade_size_5m   REAL,             -- a few whales or a small crowd?
    flow_is_low_fees     INTEGER,
    -- f) The market system
    sol_ret_4h       REAL,
    sol_ret_24h      REAL,
    eth_ret_24h      REAL,
    -- g) Signal density
    prior_signals_token INTEGER,
    minutes_since_prior_signal REAL,
    global_signals_1h INTEGER,
    -- The label (from outcomes — post-t0, a target, not a feature)
    final_return_48h REAL,
    max_gain_1h      REAL,
    max_gain_4h      REAL,
    max_gain_24h     REAL,
    max_gain_48h     REAL,
    max_drawdown_48h REAL,
    time_to_peak_h   REAL,
    is_rug           INTEGER,
    is_explosive     INTEGER,          -- (fv15) the explosion label — from outcomes
    time_to_plus20_min REAL,           -- (fv15) the early-entry hook — from outcomes
    social_channels_dex INTEGER,       -- (fv16) social channels at DEX Screener
    social_match_fomo_dex INTEGER,     -- (fv16) socials agreement between the two sources
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_training_split
    ON training_rows (asset_class, split, status, is_independent);

-- The model's safe interface: no need to remember six filters at every training run.
DROP VIEW IF EXISTS model_training_rows;
CREATE VIEW model_training_rows AS
SELECT * FROM training_rows
 WHERE kind = 'signal'
   AND is_live = 1
   AND asset_class = 'meme'
   AND status = 'ok'
   AND is_independent = 1
   AND feature_version = CAST(COALESCE(
       (SELECT value FROM meta WHERE key = 'current_feature_version'), '0'
   ) AS INTEGER)
   AND NOT EXISTS (
       SELECT 1
         FROM signal_events current_event
         JOIN signal_events earlier_event
           ON earlier_event.token_address = current_event.token_address
          AND COALESCE(earlier_event.network_id, '') =
              COALESCE(current_event.network_id, '')
          AND earlier_event.ts = current_event.ts
          AND earlier_event.signal_type = current_event.signal_type
          AND earlier_event.id < current_event.id
        WHERE current_event.id = training_rows.key
   )
   -- Simultaneous signals for the same token and moment count as duplicates: we keep only the smallest key.
   AND training_rows.key = (
       SELECT MIN(t2.key)
         FROM training_rows t2
        WHERE t2.kind = 'signal'
          AND t2.is_live = 1
          AND t2.asset_class = 'meme'
          AND t2.status = 'ok'
          AND t2.is_independent = 1
          AND t2.feature_version = CAST(COALESCE(
              (SELECT value FROM meta WHERE key = 'current_feature_version'), '0'
          ) AS INTEGER)
          AND t2.token_address = training_rows.token_address
          AND COALESCE(t2.network_id, '') =
              COALESCE(training_rows.network_id, '')
          AND t2.entry_ts = training_rows.entry_ts
   );

-- Holding concentration and crowd positioning — a rug-risk measure, and both were entirely missing.
-- `market_ticks.top10_holders_pct` is dead: the source never provides the key at all
-- (0 of 1,430,475 rows). The on-chain check covers Solana only and is silent about all of EVM.
-- The two sources here work on both networks but measure two different things (verified live):
-- tokenDetails gives on-chain concentration (top10HoldersPercent+holders), and /hodlers/top
-- gives fomo users' positions with no share of supply — crowd positioning, not concentration.
-- Each in its own row (source is part of the key), so neither measurement is taken for the other.
CREATE TABLE IF NOT EXISTS token_holders (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL,               -- token_details / hodlers_top
    -- On-chain concentration — from token_details only; NULL from the other source
    top10_pct           REAL,                        -- top 10 as % of supply
    holder_count        INTEGER,                     -- total holders on the chain
    -- Platform crowd positioning — from hodlers_top only; NULL from the other
    platform_holders             INTEGER,            -- totalHolders on the platform
    platform_holders_listed      INTEGER,            -- those itemized in the response (~50)
    platform_value_usd           REAL,               -- total value of the positions
    platform_underwater          INTEGER,            -- number of losing positions
    platform_median_hold_seconds REAL,               -- median holding duration
    platform_dev_holding         INTEGER,            -- 1 = the developer is among the holders
    top_holders_json    TEXT,                        -- summarized positions (without the user block)
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at, source)
);
CREATE INDEX IF NOT EXISTS idx_holders_token_ts
    ON token_holders (token_address, network_id, recorded_at);

-- Rotating-schedule state for holding concentration. Errors are retried quickly (unknown ≠ safe).
CREATE TABLE IF NOT EXISTS holders_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                              -- ok / empty / error
    top10_pct     REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- Trading flow: the buy/sell split from the **very same** tokenDetails response that the
-- holding cycle fetches — zero extra calls. Only two fields used to be extracted from that response (top10/holders)
-- and the rest thrown away; measured on 300 archived responses, it carries, with 100% presence:
--   * the volume split into buys and sells (the other sources give only the total volume — so a million
--     dollars of selling looked like a million of buying to the model), and
--   * a **full 5-minute layer** that no other source provides (the window closest to the decision moment).
-- A separate table, not columns on token_holders: that one measures ownership concentration, and its extractor returns
-- None when holding data is missing, so it would have swallowed the flow with it. And no rows on
-- market_ticks: there is no price here, so it would pile up sparse rows.
-- There is no 12h layer in this source at all ⇒ no column for it (a permanent absence ≠ a measurement).
CREATE TABLE IF NOT EXISTS token_flow (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    buy_count_5m        INTEGER,
    buy_count_1h        INTEGER,
    buy_count_4h        INTEGER,
    buy_count_24h       INTEGER,
    sell_count_5m       INTEGER,
    sell_count_1h       INTEGER,
    sell_count_4h       INTEGER,
    sell_count_24h      INTEGER,
    -- They arrive **as strings** from the source ('90135') — _num converts them
    buy_volume_5m       REAL,
    buy_volume_1h       REAL,
    buy_volume_4h       REAL,
    buy_volume_24h      REAL,
    sell_volume_5m      REAL,
    sell_volume_1h      REAL,
    sell_volume_4h      REAL,
    sell_volume_24h     REAL,
    unique_buys_5m      INTEGER,
    unique_buys_1h      INTEGER,
    unique_buys_4h      INTEGER,
    unique_buys_24h     INTEGER,
    unique_sells_5m     INTEGER,
    unique_sells_1h     INTEGER,
    unique_sells_4h     INTEGER,
    unique_sells_24h    INTEGER,
    -- It genuinely varies: 11 True out of 800 responses — low fees mean a different pool
    is_low_fees         INTEGER,
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_flow_token_ts
    ON token_flow (token_address, network_id, recorded_at);

-- The trader profile. Measured: 5,572 distinct buyer ids in signal_events, and **3,202 of them
-- are repeat buyers (≥3 events)** — and there is no traders table in the DB at all, so 'who bought?'
-- was a question without an answer even though the id is stored in every event. Only repeat buyers are fetched:
-- someone who shows up once has no behavior for us to learn.
--
-- The columns match the /v2/users/{id} response **measured live 2026-08-10 on two traders**
-- (26 keys). What is not in the response gets no column: **no win_rate and no realized_pnl**
-- — the profile carries no profit at all (that is why a separate endpoint exists,
-- aggregatedSnapshot). Adding a column for a nonexistent field creates a dead column like
-- top10_holders_pct, which cost us 1.43 million empty rows.
CREATE TABLE IF NOT EXISTS traders (
    trader_id        TEXT PRIMARY KEY,
    recorded_at      TEXT NOT NULL,             -- when the profile was last updated
    handle           TEXT,                      -- userHandle
    display_name     TEXT,
    followers_count  INTEGER,                   -- followers: 2,143 and 214,422, measured
    following_count  INTEGER,                   -- following
    swap_count       INTEGER,                   -- swapCount: all of his swaps
    num_trades       INTEGER,                   -- numTrades: always less than swapCount
    total_volume_usd REAL,                      -- totalVolume: 12.99M and 5.45M, measured
    avg_hold_seconds REAL,                      -- averageHoldTimeSeconds: 38k and 170k
    is_restricted    INTEGER,                   -- isRestricted (bool)
    is_private       INTEGER,                   -- private (bool)
    wallet_address   TEXT,                      -- address (Solana)
    evm_address      TEXT,                      -- evmAddress
    twitter_url      TEXT,                      -- his presence or absence is a reputation signal
    created_at       TEXT,                      -- account age
    raw_json         BLOB NOT NULL
);

-- Rotating-schedule state for traders. **Its key is the trader id alone**, not
-- (token, network) like the other state tables — a trader cuts across tokens.
CREATE TABLE IF NOT EXISTS traders_fetch_state (
    trader_id     TEXT PRIMARY KEY,
    last_fetch_at TEXT,
    last_status   TEXT,                         -- ok / empty / error
    attempts      INTEGER NOT NULL DEFAULT 0
);

-- ═══ On-chain layer: measured directly from the blockchain, not from FOMO ═══
-- True ownership concentration. FOMO gives `top10HoldersPercent` alone, and only every 25 minutes
-- (measured: median gap 25.0min, p25=25.0, p75=25.1 over 19,440 pairs) — so no top1, no
-- top5 or top20, and no 5-minute layer. `getTokenLargestAccounts` gives all four
-- in exactly one call (measured 230ms), and a JSON-RPC batch carries `getTokenSupply` with it
-- in **a single HTTP request** (measured 4/4) — so the cost is one call per token per cycle.
-- Measured on live tokens from our watchlist: BABYSHIB top1=32.21% top5=45.60%
-- top10=52.52% top20=60.60% · CHAM top1=11.64% top20=37.90%.
--
-- **Solana only — structurally, not temporarily**: the ERC-20 standard carries no holder list
-- at all, so no node call can return the largest holders on BSC/Robinhood/Base — that needs
-- a paid indexing provider per network. Any EVM token stays without a row here, and this is a measured absence,
-- not zero (FR-007). Solana = 45.2% of our signals (30,931 of 68,491).
--
-- A separate table, not columns on `token_holders`: that one is sourced from FOMO on a 25-minute cadence
-- and already mixes two populations (chain/platform) — adding a third source with a different cadence to it
-- would repeat the same mistake. `top10_pct` here and there measure the same thing from two independent sources,
-- so their agreement is a free cross-check, not duplication.
CREATE TABLE IF NOT EXISTS chain_concentration (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    -- Supply in human units (after dividing by 10^decimals) — the denominator of every ratio
    supply              REAL,
    decimals            INTEGER,
    top1_pct            REAL,                   -- largest single account as % of supply
    top5_pct            REAL,
    top10_pct           REAL,                   -- mirrors the FOMO measurement ⇒ cross-check
    top20_pct           REAL,
    -- The **exact** holder count. Filled from the EVM layer only (a full balance ledger we build
    -- from every transfer); it stays NULL on Solana: `getTokenLargestAccounts` returns 20
    -- accounts at most and does not know the total, and counting every mint account is impractical.
    -- A measured absence, not zero (FR-007).
    holder_count        INTEGER,
    -- How many accounts actually came back. The source returns 20 at most, and a token with 7 holders returns 7 —
    -- so its `top20_pct` = the sum of all of them, not the 'top 20'. Without this column,
    -- the two numbers look equivalent, and they are not.
    top_accounts        INTEGER,
    -- 1 = a **replayed** row, not one measured at its moment: the chain's transfers were re-run up to the block
    -- that was the head at `recorded_at` (`evm_replay.py`). The chain is a dated record that
    -- does not change, so the number is honest for that moment, but the separation is required: without this column there is no way
    -- to evaluate the model on the live-measured rows alone, nor to explain a coverage jump somewhere in the history.
    is_replay           INTEGER NOT NULL DEFAULT 0,
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_chain_conc_token_ts
    ON chain_concentration (token_address, network_id, recorded_at);

-- Rotating-schedule state for the on-chain layer. Same shape as `holders_fetch_state`,
-- and errors are retried quickly (unknown ≠ safe).
CREATE TABLE IF NOT EXISTS chain_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                         -- ok / empty / error
    top1_pct      REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- ═══ The slow layer: mint authorities and mutability (hourly cadence) ═══
-- Why a second table and a different cadence: concentration moves every minute (a whale buys), whereas
-- mint/freeze authority and `mutable` change at most once in a token's lifetime, if at all — so asking
-- every five minutes is waste, and skipping them entirely is a loss. Hourly is an honest middle.
--
-- Every column here has **measured variance** over 48 tokens from our live watchlist (2026-08-13),
-- so there are no dead columns:
--   token_program      : 21 spl-token · 27 spl-token-2022  (strong variance)
--   mint_authority     : live in 3/48 — rare, but it is an **open minting door**
--   freeze_authority   : live in 1/48 — whoever holds it can freeze your wallet
--   is_mutable         : 31 yes · 17 no
--   update_authority   : present in 22/48 (the authority to edit the metadata)
--   creator_address    : only 7/48 — Token-2022 always returns `creators: []`,
--                        so coverage of 'developer holding from the chain' is ~15% at most, and that
--                        is a measured absence, not zero (FR-007).
-- Columns measured as **constant** were dropped, since they carry no information: `burnt` (0/48),
-- `ownership.frozen` (0/48), `interface` (48/48 FungibleToken); and `price_info`
-- (47/48) exists, but we already get the price from FOMO. All of them are kept in `raw_json`
-- anyway, so dropping the column loses no data.
CREATE TABLE IF NOT EXISTS chain_authority (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    token_program       TEXT,                   -- spl-token / spl-token-2022
    mint_authority      TEXT,                   -- NULL = revoked (no new minting)
    freeze_authority    TEXT,                   -- NULL = no freezing possible
    update_authority    TEXT,
    is_mutable          INTEGER,                -- 1/0, and NULL = not measured
    creator_address     TEXT,
    creator_count       INTEGER,
    -- Supply from the **mint account itself**, not from market_ticks: same moment, same source.
    supply              REAL,
    decimals            INTEGER,
    -- Developer holding measured on-chain (a second call, conditional on an address existing).
    -- `dev_owner` says **for whom** we measured; without this column the number cannot be interpreted.
    dev_owner           TEXT,
    dev_holding_pct     REAL,
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_chain_auth_token_ts
    ON chain_authority (token_address, network_id, recorded_at);

-- The hourly schedule state. A state table deliberately separate from `chain_fetch_state`: the two
-- freshness windows differ, and merging them lets one layer's update hide the other's lag.
CREATE TABLE IF NOT EXISTS chain_auth_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                         -- ok / empty / error
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- ═══════════════ EVM layer: a balance ledger built from transfers ═══════════════
-- The ERC-20 standard carries no holder list, so no node call can provide one. The only route to an
-- **exact** number is to replay every `Transfer` event and save the resulting balance. And this is measured
-- to be cheap, not expensive: a single `eth_getLogs` call with a filter carrying **every** watched address on the network
-- (Robinhood 57 addresses in 0.5s, Base 22 in 0.4s), and backfilling a token's full
-- history is one call if it is quiet (measured: 1,814 transfers across 1.71 million blocks, 0.7s).
--
-- And the paid alternative is worse in every way: Blockscout 5.6s per token and only the top 50,
-- Etherscan V2 refuses without a key, and Sourcify knows 1 of 10.
--
-- The balance is a **64-wide hexadecimal string**, not an integer: uint256 exceeds 64 bits
-- (a token with 18 decimals and a billion supply = 10^27 ≫ SQLite's limit), and zero-padding makes
-- the lexicographic order **match** the numeric order, so `ORDER BY balance_hex DESC` is valid.
CREATE TABLE IF NOT EXISTS evm_balances (
    network_id     TEXT NOT NULL,
    token_address  TEXT NOT NULL,               -- always stored lowercase
    holder_address TEXT NOT NULL,               -- always stored lowercase
    balance_hex    TEXT NOT NULL,               -- 64 digits, zero-padded
    -- The first block where this address received the token. It enables 'new holders in 5 minutes',
    -- a question no provider answers: they all give a snapshot with no entry history.
    first_seen_block INTEGER,
    updated_block  INTEGER,
    updated_at     TEXT,
    PRIMARY KEY (network_id, token_address, holder_address)
);
-- Balance in descending order: the top-N query is the hot query in every snapshot.
CREATE INDEX IF NOT EXISTS idx_evm_bal_rank
    ON evm_balances (network_id, token_address, balance_hex DESC);

-- The block cursor per network: how far the application has gotten. One row per network, not per token, because
-- the call is the same single one for the whole network — and a cursor per token would mean either one call per token
-- or cursors drifting apart, with transfers applied twice.
CREATE TABLE IF NOT EXISTS evm_block_cursor (
    network_id     TEXT PRIMARY KEY,
    last_block     INTEGER NOT NULL,            -- the last **applied** block (inclusive)
    last_run_at    TEXT,
    last_status    TEXT,                        -- ok / error
    logs_applied   INTEGER NOT NULL DEFAULT 0,  -- cumulative, for diagnostics
    last_error     TEXT
);

-- Initial backfill state per token. The cursor alone cannot be relied on: a token entering
-- monitoring today has history **before** the cursor, and if we settled for the new transfers, every
-- balance would be short by whatever was missed — a silent error the numbers cannot reveal.
CREATE TABLE IF NOT EXISTS evm_backfill_state (
    network_id    TEXT NOT NULL,
    token_address TEXT NOT NULL,
    status        TEXT,                          -- partial / done / retry / error
                                                  -- retry = a transient failure (timeout, silence)
                                                  -- error = permanent: a single block exceeds
                                                  -- the cap, and no split can save it
    from_block    INTEGER,                       -- start of the filled range
    to_block      INTEGER,                       -- its end = the cursor when application started
    transfers     INTEGER,                       -- how many transfers were applied
    calls         INTEGER,                       -- how many calls it cost (cap diagnostics)
    last_try_at   TEXT,
    last_error    TEXT,
    PRIMARY KEY (network_id, token_address)
);

-- ═══ EVM contract safety — the three networks, one measurement refuting another ═══
-- First measured 2026-08-13 on the live watchlist by inspecting the bytecode (`eth_getCode`)
-- and matching function identifiers against a computed keccak dictionary, so the check was confined to Base:
--   Base 8453 : 19 of 22 full contracts, sizes 135B–14.8KB, `owner` in 7 of 19,
--               `mint` in 2, `limits` in 1 ⇒ **real variance ⇒ information**
--   BSC  56   : 21 of 27 small proxies (EIP-1167) pointing to just two implementation contracts
--               ⇒ it was concluded 'the column is constant, no information in it'
--   RH   4663 : 51 of 57 full contracts, but the sizes repeat across six templates
-- The measurement was repeated on 2026-08-22 and it refuted the conclusion, not the numbers: template repetition was measured in
-- `code_size` alone, while `is_proxy` **is itself the information**, not noise hiding it.
--   BSC  56   : 26 of 40 proxies ⇒ a 65/35 split — the most discriminating column on any network
--               (Base 3 of 40, Robinhood 2 of 36), and `code_size` has 8 distinct values
--   RH   4663 : `code_size` 20 values and `function_count` 16, and `has_pause` varies
--               (1 of 36) where Base is constant ⇒ more variance, not less
-- Truly constant across the three: `has_blacklist` and `has_fee_setter` (zero in 116 contracts).
-- So the check runs on `EVM_CONTRACT_NETWORKS` = the three of them. One row per measurement, not one
-- single row: ownership renunciation is an **event** that lands mid-window, and a row updated in place erases
-- its history (the same defect as `chain_authority`).
CREATE TABLE IF NOT EXISTS evm_contract (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    code_size           INTEGER,                -- bytes. 0 = not a contract
    function_count      INTEGER,                -- unique identifiers in the dispatch table
    is_proxy            INTEGER,                -- an exact EIP-1167 proxy
    impl_address        TEXT,                   -- the implementation contract if it is a proxy
    -- Contracts with the same bytecode share a fingerprint ⇒ 'which factory it came from' in one column,
    -- which turned out to be the real information, not the individual functions.
    code_hash           TEXT,
    owner_address        TEXT,                  -- from `owner()`/`getOwner()`
    is_ownership_renounced INTEGER,             -- 1 = the address is zero
    has_mint            INTEGER,
    has_pause           INTEGER,
    has_blacklist       INTEGER,
    has_fee_setter      INTEGER,
    has_limit_setter    INTEGER,
    has_trading_switch  INTEGER,
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_evm_contract_token_ts
    ON evm_contract (token_address, network_id, recorded_at);

CREATE TABLE IF NOT EXISTS evm_contract_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                         -- ok / empty / error
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- Time↔block anchors. Needed for Robinhood alone: its node returns `blockTimestamp: '0x0'`
-- in every log (measured 2026-08-13), so the log carries no time, whereas Base and BSC return the real
-- timestamp and need no anchor. The anchor is shared by every token on the network ⇒ fetched once,
-- read a thousand times, and Robinhood's block time is constant (0.1002s/0.1003s over 100k and 300k
-- blocks), so interpolating between adjacent anchors errs by seconds.
CREATE TABLE IF NOT EXISTS evm_block_time (
    network_id   TEXT    NOT NULL,
    block_number INTEGER NOT NULL,
    block_ts     INTEGER NOT NULL,              -- seconds since 1970 (from the node)
    fetched_at   TEXT,
    PRIMARY KEY (network_id, block_number)
);

-- Retroactive replay state per token. A task that lasts hours ⇒ the state is what makes resumption
-- free: a completed token is not redone, and a stuck one is known by its failure.
CREATE TABLE IF NOT EXISTS evm_replay_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    status        TEXT,                          -- done / partial / error / empty
    from_block    INTEGER,                       -- the first block actually read
    to_block      INTEGER,                       -- the last block read
    transfers     INTEGER,                       -- how many transfers were replayed
    snapshots     INTEGER,                       -- how many concentration rows were written
    calls         INTEGER,
    -- Was the token's history read from the very start? The check is **not** 'the sum of balances is zero': the sum
    -- of transfers is always zero by construction (each transfer is +v to one and −v to another), so it says
    -- nothing. The real check: **any negative balance for an ordinary address** means the address sent
    -- what we never saw it receive ⇒ we started too late, and the numbers are false, not incomplete. The live layer
    -- clamps negatives to zero (no choice: the column is hexadecimal text) — so here it is not clamped but
    -- exposed, and not a single row is written until it is gone.
    balance_check TEXT,                          -- ok / negative
    last_try_at   TEXT,
    last_error    TEXT,
    -- Balance state at `from_block - 1` and the next network checkpoint. Compressed like the raw payload;
    -- it makes `partial` a true resume instead of redoing the same range or losing the accumulation.
    checkpoint_json BLOB,
    revision       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- Runtime state: last run, counters, getBars status, schema version.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
