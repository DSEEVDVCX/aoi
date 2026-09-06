# Integrated implementation plan: completing FOMO and blockchain data collection

## 1. Objective

This plan aims to transform the current recording system from a broad data archive into a data
platform capable of building and testing a realistic trading model, while preserving three conditions
that may not be traded away:

1. Every entry feature must be known at or before the decision moment `t0`.
2. Missing data stays `NULL` and does not become a fabricated zero.
3. Collection success or a high `AUC` does not mean a winning model exists; approval requires a
   time-based test on new tokens and a net profit after a realistic execution cost.

This document is an execution annex to `docs/PLAN.md`. It does not cancel the `v3` control-group gate or the
live-data-only training decision, and it does not automatically open real trading.

## 2. Reference state at the start of the plan

State measured on 2026-08-19:

| Item | Approximate state |
|---|---:|
| `recorder/recorder.db` size | 16.8 GB |
| `signal_events` | 97 thousand |
| `token_bars` | 2.05 million candles |
| `market_ticks` | 3.25 million snapshots |
| `training_rows` | 94 thousand |
| Clean independent live model rows | 11.2 thousand |
| Distinct tokens in model rows | 611 |
| `traders` | 4,588 profiles |
| `token_flow` | ~77 thousand snapshots |
| `token_holders` | ~173 thousand snapshots |
| `chain_concentration` | ~283 thousand snapshots |
| `evm_balances` | ~2.05 million balances |
| `activity_events` | only 190, a clear gap |

Reference methodological state:

- The previous evaluation gave an `AUC` for new tokens near 0.555, with no sufficient evidence of a
  generalizable profit.
- Trader-profile data is collected, but it does not yet enter `FEATURE_COLUMNS` or
  `training_rows`.
- `flow_*` and `onchain_*` collection exists, but a large part of it is recent in time or its EVM
  backfill is still partial.
- `evm_training_rebuild_started=0`; the final EVM rows must not be considered complete before
  the ledger repair is finished and then rebuilt.
- Realistic execution data, especially DEX reserves and slippage and liquidity add/remove,
  is not a complete layer yet.

## 3. Plan scope

### In scope

- Quality and coverage audit of every existing source.
- Finishing EVM ledger integrity and rebuilding its rows.
- Exploiting the existing trader profiles without time leakage.
- Building signal-cluster and FOMO-list-appearance-path features.
- Completing `tradingActivity` as a separate analytical universe.
- Keeping trader-profile history instead of only the latest copy.
- Adding a DEX event, liquidity, and execution layer incrementally per protocol.
- Adding wallet, whale, and developer measurements from historical transfers.
- Updating export, reports, monitoring, and point-in-time tests.
- Running an `ablation` study and time-based test and paper trading after the data matures.

### Out of scope in this cycle

- Executing real trading or storing trading wallet keys.
- Using private data or bypassing source terms.
- Analyzing thesis texts with a language model before aggregate, identity, and time integrity is established.
- Adding a neural network merely to increase complexity.
- Treating incomplete retro data as a substitute for live measurements.
- Opening the current `test` set repeatedly for feature selection.

## 4. Architecture principles

### 4.1 Raw data is irreplaceable

- `raw_json` stays stored for every available source.
- Extracted columns are rebuildable; raw is never edited.
- Any derived migration runs in batches, is resumable, and has a `--dry-run` mode.
- Any deletion of EVM derivatives goes through `repair_evm_ledger.py` only and after a verified
  backup; no destructive manual SQL commands.

### 4.2 Separating measurement from derivation

Every family follows this path:

```text
External source
  -> a dated, source-stamped raw row
  -> directly extracted columns
  -> point-in-time features
  -> a training row with a feature_version
  -> time-based evaluation and paper trading
```

The collection process must not compute a `label`, and the entry-feature extractor must not read a row
after `t0`.

### 4.3 Separating populations and sources

- On-chain holders are not equal to FOMO holders.
- A FOMO trade is not equal to an on-chain `Swap` event without a documented join.
- A live snapshot is not equal to a historically reconstructed snapshot, even if the number is correct.
- Live `signal_events` data is not merged with retro `activity_events` in approval training
  without an independent bias report.
- Every row must carry `source`, `recorded_at`, and a measurement-method indicator where needed, such as
  `is_replay`.

### 4.4 Versions and eras

- Every change in a feature's meaning bumps `FEATURE_VERSION`.
- Adding a recent source does not permit training the model on it immediately; Train and Val/Test must be
  covered in time so the model does not learn the source's operating history.
- Every new family carries a coverage report by day, network, and signal type.
- If presence alone reveals a row's date, the family is temporarily excluded from the approval model.

## 5. Dependency map

```text
P0 baseline and audit
  |
  +--> P1 EVM integrity and rebuild
  |
  +--> P2 trader history preservation
  |      |
  |      +--> P3 trader quality features
  |
  +--> P4 signal clusters and FOMO list path
  |
  +--> P5 tradingActivity completion
  |
  +--> P6 DEX contracts and pool discovery
         |
         +--> P7 Swap and liquidity events
                |
                +--> P8 execution and wallet features

P1 + P3 + P4 + P5 + P8
  -> P9 rebuild, export, and leakage audit
  -> P10 time-based evaluation and feature ablation
  -> P11 paper trading and the approval gate
```

P2, P4, and P5 can run in parallel after P0. P1 must stay sequential because of the shared EVM
ledger state. Do not start P7 before P6's contracts are established, so we do not rewrite tables for every
protocol.

## 6. Implementation phases

## P0 - Freeze the baseline and build an automated audit

### Purpose

Create a reference number that every change can be compared against, and prevent collection improving
while another layer silently degrades.

### Work

- Create a read-only tool `recorder/data_readiness.py` that produces comparable JSON.
- Measure row counts, latest stamp, error ratio, coverage at `t0`, coverage by day
  and network, distinct values, and the `NULL` ratio per family.
- Log scheduled-task states and important meta without secrets.
- Add safety checks:
  - `PRAGMA quick_check` on a backup, not on the live writer.
  - No negative EVM balances in a successful replay.
  - No future candles entering `t0` features.
  - No duplicate rows in `model_training_rows`.
  - No token crossing between the time parts used in evaluation.
- Write the first dated reference report at `docs/data-readiness-YYYY-MM-DD.md` by the tool
  only, no manual numbers.

### Acceptance gate

- The tool finishes in reasonable time on the operating database without writing.
- The outputs cover every existing source and distinguish `missing`, `empty`, `error`, and `stale`.
- Two consecutive runs without a structural change produce compatible JSON schemas.
- There are tests on a miniaturized database for every counter and state.

## P1 - Finish EVM integrity and rebuild its derivatives

### Purpose

Prevent the model from training on incomplete holder concentration or a balance that started after the
first transfer.

### Work

1. Take a consistent backup via `backup_db.py` and verify it with `quick_check`.
2. Run `repair_evm_ledger.py` in inspect-only mode and store the status report.
3. Finish the live backfill for enabled networks.
4. Finish replay for mature windows on `4663` and `8453`.
5. Classify final states explicitly: `done`, `empty`, `negative`, `budget`, `error`.
6. Audit a sample from each network against `totalSupply` and `balanceOf` via
   `audit_evm_ledger.py`.
7. Do not re-admit networks or tokens marked `negative` into ownership features.
8. Only after `active_pending=0` and `replay_pending=0`, run
   `--finalize-training` and then let the builder rebuild the EVM rows.
9. Compare row counts, coverage, and values before and after the repair.

### BSC decision

- Instantaneous measurement via NodeReal remains a separate, trusted source for the current state.
- History is not fabricated from a non-archive RPC.
- If no documented archive provider is available within a clear budget, BSC's historical replay features
  stay `NULL` with a coverage flag added, and that is not a failure.

### Writer ownership

- `run_evm_replay.py` is the sole writer of all EVM layers: live application, backfill,
  snapshots, BSC, contract checks, then historical replay.
- `run_chain.py` owns Solana and Solana authority only; it does not run EVM or BSC.
- The reason is operational, not analytical: SQLite allows concurrent readers but serializes writes, and the
  two long EVM cycles were contending on `evm_balances` and `chain_concentration` and causing
  `database is locked`.
- These layers must not be split back into two writer tasks unless the state is moved to a single
  writer or a standalone write queue and tested under real load.

### Acceptance gate

- `repair_evm_ledger.inspect()` returns `active_pending=0` and `replay_pending=0` for the required
  networks before finalize.
- All rows labeled `done` pass the non-negative check.
- An audit of the top five holders in the sample matches the chain or documents a difference under the
  threshold.
- No EVM training rows remain on the previous version after the rebuild.
- The tests `test_repair_evm_ledger.py`, `test_evm_replay.py`, and `test_evm_layer.py`
  pass in full.

## P2 - Turn trader profiles into a time-series record

### Purpose

The current `traders` table overwrites the previous snapshot with the latest. This prevents knowing what was known
about a trader at an old signal.

### Design

Add an append-only table:

```sql
CREATE TABLE trader_snapshots (
    trader_id         TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    followers_count   INTEGER,
    following_count   INTEGER,
    swap_count        INTEGER,
    num_trades        INTEGER,
    total_volume_usd  REAL,
    avg_hold_seconds  REAL,
    is_restricted     INTEGER,
    is_private        INTEGER,
    has_twitter       INTEGER,
    account_created_at TEXT,
    raw_json          BLOB NOT NULL,
    PRIMARY KEY (trader_id, recorded_at)
);
```

- `traders` remains the latest-state table for the dashboard and fast operations.
- Every valid fetch writes to `trader_snapshots` and then updates `traders` in a single transaction.
- Duplication is prevented via the key, and a new snapshot is not recorded if the content is unchanged except
  after a documented daily heartbeat, to reduce size.
- No fake retroactive filling: the current version can only be inserted with the current measurement date.
- Add `trader_fetch_method` or a source version if the endpoint's shape changes.

### Scheduling improvements

- Buyers linked to a new signal get priority.
- Then the most frequent repeaters.
- The trader profile is requested before or immediately after processing the signal, with the real
  `recorded_at` preserved.
- The rate limit stays tunable, and `buyer_id -> trader_snapshot <= t0` coverage is displayed.

### Acceptance gate

- Every successful fetch writes a historical snapshot and never erases an old one.
- A test proves that an old signal's feature reads a snapshot from before `t0`, not the latest snapshot.
- The table does not grow daily from unlimited identical copies.
- New-buyer coverage is measured and shown in the readiness report.

## P3 - Build trader-quality features without leakage

### Purpose

Exploit the 4,588 trader profiles and the existing signal archive instead of settling for the leaderboard
rank.

### Feature families

#### A. Profile snapshot at `t0`

- `buyer_followers`
- `buyer_following`
- `buyer_follow_ratio`
- `buyer_swap_count`
- `buyer_num_trades`
- `buyer_total_volume_usd`
- `buyer_volume_per_trade`
- `buyer_avg_hold_h`
- `buyer_account_age_days`
- `buyer_has_twitter`
- `buyer_is_private`
- `buyer_is_restricted`
- `buyer_profile_age_min`

#### B. Prior behavior inside our archive

- `buyer_prior_signals`
- `buyer_prior_tokens`
- `buyer_prior_networks`
- `buyer_signals_24h`
- `buyer_minutes_since_signal`
- `buyer_prior_win_rate`
- `buyer_prior_net_return_mean`
- `buyer_prior_net_return_median`
- `buyer_prior_rug_rate`
- `buyer_prior_up20_rate`
- `buyer_history_complete_rows`

An outcome enters only if the prior trade's `watch_until` or horizon ended before `t0`.
The existence of a prior row that entered before `t0` but has not matured yet does not permit using its
outcome.

#### C. Multiple traders

For `multi_user_buy`, aggregates over the `top_trader_ids_json` list are computed:

- Number of trader profiles available.
- Best and median historical volume.
- Best and median prior success rate.
- Number of traders with sufficient history.
- Quality dispersion, so one strong trader cannot hide a weak group.

### Overfitting protection

- No `buyer_id`, handle, wallet, or any high-cardinality identifier enters as a direct feature.
- A minimum count of prior outcomes before computing a rate; below the threshold the ratio stays `NULL` and
  `buyer_history_complete_rows` is shown.
- Pre-specified smoothing is used, not tuned after seeing `test`.
- Tests are written specifically to keep the current row's outcome out of the buyer's history.

### Acceptance gate

- Every feature has a documented SQL or Python definition and a point-in-time test.
- The coverage report shows Train, Val, and new-period coverage separately.
- An ablation trial on Val compares baseline versus `+trader`, without opening Test.
- Acceptance does not rest on any single individual's identity.

## P4 - Signal clusters and the token discovery path in FOMO

### Purpose

Turn the periodic archive of `feed`, `trending`, `verified`, and `most_held` into time context
instead of merely separate market rows.

### Design

Add a derived table, rebuildable from `snapshots`:

```sql
CREATE TABLE token_source_presence (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_rank   INTEGER,
    price_usd     REAL,
    market_cap    REAL,
    raw_item_hash TEXT,
    PRIMARY KEY (token_address, network_id, observed_at, source)
);
```

- The item's rank is extracted from its position in the list, if the rank has a stable meaning.
- If a list's ordering is not proven meaningful, `source_rank` stays NULL.
- `filterTokens` does not enter as a popularity list; it is used only as a measurement-continuity flag.
- It is rebuilt from old raw snapshots without any network.

### Source-path features

- `first_seen_age_min`
- `first_seen_source`
- `sources_seen_before_t0`
- `in_trending_at_t0`
- `in_verified_at_t0`
- `in_most_held_at_t0`
- `trending_appearances_1h`
- `most_held_appearances_1h`
- `source_transition_count_24h`
- `minutes_since_last_trending`
- `rank_change_15m` only if the rank's meaning is proven.

### Signal-cluster features

- `token_signals_5m`, `15m`, `30m`, `1h`
- `token_unique_buyers_15m`
- `token_buy_events_15m`
- `token_sell_events_15m`
- `token_buy_sell_event_ratio_15m`
- `signal_cluster_age_min`
- `signal_cluster_size`
- `same_trade_duplicate_count`

`tradeId`, `swapId`, and `transferId` are used for joining and deduplication only after the meaning of each
identifier is proven on raw samples. Missing identifiers are not treated as equal.

### Acceptance gate

- Rebuilding from the snapshots is idempotent.
- A test proves that no appearance or signal after `t0` enters.
- A report shows the share of events removed as duplicates and the removal reason.
- The database does not blow up from repeated raw copies; the derived table does not re-save the envelope.

## P5 - Complete `tradingActivity` as a separate universe

### Purpose

Obtain `swap_buy`, `swap_sell`, and events that `/feed` does not show, without polluting
live training with instantaneously incomplete retro data.

### Work

- Diagnose why `activity_events` is stuck at 190 rows: pagination stopping, authorization, endpoint,
  time limit, or a non-running task state.
- Add a clear checkpoint: last `id`, oldest `createdAt`, page count, stop reason.
- Prevent a transient empty page from being treated as the end of history without a controlled retry.
- Log new event types before writing a guessing extractor.
- Complete the backfill gradually with a time and call budget and a resumable state.
- Pull candles for the new rows and label them in a separate path.
- Produce a bias report comparing live versus retro across network, type, market cap, missing
  fields, and `t0` feature coverage.

### Usage rule

- `activity_events` is used first for buyer/seller behavior analysis and universe size.
- It does not enter the current approval model because `is_live=0` and the instantaneous families are
  incomplete.
- A separate retro model can be built for comparison, with a completely separate name, version, and results.

### Acceptance gate

- Progress shows via the oldest stamp, not just row count.
- Restarting does not duplicate events.
- Every stop state has a recorded reason.
- The bias report explicitly decides whether the source is valid for any training; it does not assume it.

## P6 - DEX contracts and pool discovery

### Purpose

Establish the pool, protocol, and asset identity before collecting `Swap` or liquidity, because an event
without a documented pool identity can be interpreted in the wrong direction.

### Data model

```sql
CREATE TABLE dex_pools (
    network_id       TEXT NOT NULL,
    pool_address     TEXT NOT NULL,
    token_address    TEXT NOT NULL,
    quote_address    TEXT,
    protocol         TEXT,
    pool_version     TEXT,
    fee_bps          REAL,
    created_block    INTEGER,
    created_at       TEXT,
    discovery_source TEXT NOT NULL,
    verified_at      TEXT,
    raw_json         BLOB,
    PRIMARY KEY (network_id, pool_address)
);
```

### Protocol order

1. Inventory the `dex_protocol` and `pair` values already in FOMO by network and frequency.
2. Choose one protocol and one network representing the largest measurable share.
3. Build a standalone adapter for it, with tests on real raw samples and no secrets.
4. Add a second protocol only after the first passes the match gate.

### Verification

- `token0/token1` or mint A/B matches the expected token.
- Identify the quote asset and do not always assume USDC.
- Verify decimals, address, and network.
- Do not accept a pool just because FOMO mentioned its name; on-chain verification is required.

### Acceptance gate

- ≥95% of accepted pools have asset addresses matching the token and network.
- Every pool carries a discovery source and a verification method.
- Unsupported cases stay `unsupported`, not an `error` repeating forever.

## P7 - Collecting Swap and liquidity events

### Purpose

Obtain real on-chain price, execution, and flow instead of FOMO summaries only.

### Proposed tables

```sql
CREATE TABLE dex_swaps (
    network_id    TEXT NOT NULL,
    pool_address  TEXT NOT NULL,
    block_number  INTEGER NOT NULL,
    tx_hash       TEXT NOT NULL,
    event_index   INTEGER NOT NULL,
    block_ts      INTEGER NOT NULL,
    trader        TEXT,
    token_address TEXT NOT NULL,
    side          TEXT,
    token_amount  REAL,
    quote_amount  REAL,
    price_usd     REAL,
    protocol      TEXT NOT NULL,
    raw_json      BLOB NOT NULL,
    PRIMARY KEY (network_id, tx_hash, event_index)
);

CREATE TABLE dex_liquidity_events (
    network_id    TEXT NOT NULL,
    pool_address  TEXT NOT NULL,
    block_number  INTEGER NOT NULL,
    tx_hash       TEXT NOT NULL,
    event_index   INTEGER NOT NULL,
    block_ts      INTEGER NOT NULL,
    event_type    TEXT NOT NULL,
    token_amount  REAL,
    quote_amount  REAL,
    actor         TEXT,
    raw_json      BLOB NOT NULL,
    PRIMARY KEY (network_id, tx_hash, event_index)
);

CREATE TABLE dex_pool_snapshots (
    network_id       TEXT NOT NULL,
    pool_address     TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    block_number     INTEGER,
    token_reserve    REAL,
    quote_reserve    REAL,
    liquidity_usd    REAL,
    executable_buy_usd  REAL,
    executable_sell_usd REAL,
    price_impact_1k  REAL,
    price_impact_5k  REAL,
    price_impact_10k REAL,
    raw_json         BLOB NOT NULL,
    PRIMARY KEY (network_id, pool_address, recorded_at)
);
```

### Execution strategy

- Start with a process independent of `run_recorder.py` so it does not delay the FOMO cycle.
- Use a cursor per network and protocol, with appropriate confirmation delays.
- Store raw events once and re-derive price and direction locally.
- Build the backfill from the pool's creation block to the end of the watch windows only, not an
  unnecessary sweep of the whole network.
- Compare aggregated `Swap` volume against `token_flow` and OHLCV; a large difference triggers an alert.

### Acceptance gate

- The buy/sell direction is proven with manually known transactions for each protocol.
- The extracted price falls within a FOMO candle's range in most of the match sample, with fees and
  timing differences explained.
- No cursor gaps and no duplicate application after restart.
- `Swap` and Transfer are not mixed; a transfer is not a trade except within the protocol's decoder.

## P8 - Execution, wallet, and whale features

### A. Execution and liquidity features

- `pool_liquidity_usd_t0`
- `pool_count`
- `dominant_pool_share`
- `liquidity_change_15m`
- `liquidity_removed_1h`
- `price_impact_1k`, `5k`, `10k`
- `max_executable_buy_usd`
- `max_executable_sell_usd`
- `expected_entry_slippage`
- `expected_exit_slippage`
- `pool_age_h`
- `fee_bps`

### B. DEX flow

- Buy and sell volume over 5, 15, and 60 minutes.
- Unique buyer and seller counts.
- Median and mean trade size.
- Largest trade's share of volume.
- Top five traders' volume share.
- Number of trades in the same block.
- Demand breadth: uniques over trade count.

### C. Wallets and whales

- New and exiting holders over 5, 15, and 60 minutes.
- Movement of the largest holder and the largest ten.
- Developer transfers before the signal.
- Share of supply recently moved.
- Wallets funded from a single address, where a documented path exists.
- Early-buyer concentration.

### D. Governance hazards

- Ownership or authority changes.
- mint and burn events after trading started.
- Fee or trading-limit changes.
- Liquidity withdrawal by the developer or an address linked to them.

### Point-in-time law

- Every window ends strictly at `t0` for the entry model.
- Confirmation features at `t0+Delta` are built in a separate decision row with a different `decision_ts`.
- Future exit data does not enter the entry model, but it may feed a separate exit model.

### Acceptance gate

- Future-row tests for every feature function.
- Coverage ≥70% within a supported network/protocol before the family enters that segment's model.
- A single protocol's feature is not generalized to other networks; absence stays `NULL`.
- The execution simulator uses pool reserves, not the summarized `liquidity` alone.

## P9 - Rebuild, export, and dataset quality

### Work

- Bump `FEATURE_VERSION` once per coherent bundle, not per individual column.
- Update `schema.sql`, migrations, `db.py`, `features.py`, and coverage tests.
- Build training rows in batches with the current EVM generation locked.
- Update `model_training_rows` so it remains the single interface for approved training.
- Update `export_dataset.py` to export:
  - trader features.
  - source presence.
  - point-in-time DEX summaries.
  - a coverage file by family, day, and network.
  - a provenance file linking every feature to its table and time law.
- Add an automated forbidden-columns check by name and role, not just a manual list.
- Measure row-build time and remove the N+1 pattern in `train_pipeline.py` before re-evaluation.

### Acceptance gate

- A full, resumable rebuild with no mixed-version rows in the model view.
- `feature_coverage.csv` shows filled values, eras, and networks.
- A generative test plants rows after `t0` in every new table and confirms they do not change the row.
- The export can rebuild the target from candles and events without accessing the live database.

## P10 - Time-based evaluation and the ablation study

### Comparison groups

The following models are trained with the same split, target, and cost:

1. Baseline: market cap, liquidity, and price momentum only.
2. Current: all features as of before this plan.
3. Current + trader.
4. Current + source path/clusters.
5. Current + on-chain ownership.
6. Current + DEX/execution.
7. All accepted families.

### Protocol

- Time-based walk-forward.
- Separate evaluation on tokens never seen in training.
- Bootstrap by token, not by row.
- Classification metrics: ROC-AUC, PR-AUC, calibration, Brier.
- Trading metrics: net return, median, win rate, max drawdown, turnover, and the count of
  unexecutable trades.
- Costs of 2%, 3%, and 5%, plus a pool slippage model.
- A report by day, network, protocol, market cap, and liquidity.
- No feature or threshold selection on Test.

### Feature-family acceptance gate

A family is accepted only if:

- It improves Val over time, not on a random split.
- The improvement does not depend on one day or one unintended network.
- It does not vanish on new tokens.
- Profit does not flip negative after execution costs.
- The confidence interval and sensitivity analysis do not reveal the effect comes from one anomalous
  token.

### Model approval gate

- The `v3` control-group gate in `docs/PLAN.md` is met: 500 mature controls and at least 14 days, or
  the model is described as exploratory only.
- The top segment's net is positive after cost and slippage.
- It beats every pre-frozen baseline.
- New-token `AUC` and the confidence interval support a real edge.
- Calibration is acceptable; a 0.7 probability must not actually mean 0.4.

## P11 - Paper trading and monitoring

### Decision record

Every decision, including rejections, logs:

- `decision_id`, `model_version`, `feature_version`.
- `signal_id`, token, network, `decision_ts`.
- Model probability and threshold.
- The features or the feature-row hash.
- Rejection reasons: liquidity, slippage, coverage, contract risk, or probability.
- Theoretical price and paper execution price.
- Proposed trade size, capped by liquidity.
- Price source and pool and block.
- Execution, exit, and actual simulated cost outcome.

### Operating rules

- A frozen model for the whole round.
- No threshold adjustments after seeing daily profit.
- A duration of at least 14 days, preferably 30 days and two market regimes.
- A new wallet per version, without deleting the previous version's record.
- If the underlying data is stale or incomplete, the decision is `no_trade`, not a guess.

### Gate for leaving paper

This plan does not permit real trading automatically. A separate decision is required after:

- Stable net paper profit.
- Max drawdown within a pre-set limit.
- Sufficient trades and a sufficient period.
- The result not depending on one token or one day.
- An independent security audit of the execution path and key management and risks.

## 7. Monitoring and operations

### Questions the monitoring must answer

1. Is every source advancing, or returning the same rows?
2. What share of signals has a valid snapshot for every family at `t0`?
3. Is any network's cursor lagging or frozen?
4. Has upstream's shape changed so important columns became empty?
5. Is the cycle time exceeding its budget and delaying other sources?

### Mandatory metrics

- `collector_cycle_duration_seconds` per process.
- Success, failure, and duration of every upstream.
- Age of the last successful row per source and network.
- Rows written, duplicated, empty, and unsupported.
- backlog per backfill/replay state.
- Every family's coverage at `t0` by day.
- Database and WAL growth rate.
- Build failures and pending row count per feature version.

### Practical alerts

- feed or signals with no progress for more than 5 minutes.
- bars/social/holders exceeding twice their target cadence.
- A network's cursor not advancing for two cycles while the head succeeds.
- A source's error rate rising above 10% over 15 minutes.
- A field that covered >80% dropping to <20% on a new day.
- Unexpected database growth beyond twice the average.

No token address or trader id is placed as metric labels; they are used in structured logs only.

## 8. Required tests

### Unit tests

- An extractor for every real raw shape.
- Swap direction, price computation, and reserves.
- Trader and cluster feature computation.
- `NULL` versus zero.
- dedup using event keys.

### Point-in-time tests

For every new table:

1. Build a row at `t0`.
2. Add a stronger measurement after `t0`.
3. Rebuild.
4. The row must remain byte-identical in the family's columns.

### Integration tests

- A miniature collection cycle from a raw fixture to the tables.
- Restarting the cycle does not duplicate events.
- The cursor resumes after a mid-batch failure.
- build rows reads the correct version.
- export reproduces a target matching the local simulation.

### Data tests

- Uniqueness and logical foreign linkage.
- Timestamps not in the future relative to `recorded_at` within the source's bounds.
- Concentration ratios between 0 and 100, with exceptions documented.
- Reserves and volumes non-negative.
- Every `side` has consistent token/quote amounts.
- No high-cardinality feature identifiers.

## 9. Migration and safe rollback

- Every new table is added without modifying existing raw data.
- Migrations are re-runnable.
- Dual writing starts before the read switchover.
- The old read path stays available until the match report succeeds.
- If a new source fails, it is disabled with a config flag and the old operations continue.
- Rollback means stopping reads from the new family, not deleting its data.
- No `git reset`, no database deletion, and no broad manual `DELETE` during execution.

## 10. Stopping points to avoid project bloat

- No new endpoint before measuring a raw sample and its field variance.
- No column proven constant or with near-zero coverage enters without a clear improvement plan.
- No second DEX protocol is supported before the first succeeds end-to-end.
- No sybil and wallet-funding analysis is built before swaps/transfers quality is sufficient.
- No thesis-text data enters before proving that aggregates, identity, and time added value.
- If a family does not improve Val or execution, it stays archived and out of the model; we do not try to
  rescue it with repeated parameter tuning.

## 11. Suggested delivery order

### Release A - Integrity and exploiting what exists

- P0, P1, P2, P3, P4.
- Direct value without a new external provider.
- Produces the first honest comparison of the trader and source-path effects.

### Release B - Completing the historical universe

- P5 and `tradingActivity` coverage and bias reports.
- Does not change the approval model automatically.

### Release C - Experimental DEX execution

- P6 and P7 for one protocol on one network.
- Proves the ability to compute price, liquidity, and slippage from the chain.

### Release D - Execution features and the model

- P8, P9, P10.
- Only families that passed coverage and evaluation are adopted.

### Release E - Paper trading

- P11 after the data and control-group gates.

## 12. Definition of full completion

The plan counts as executed when:

- The final EVM state is audited and the training rows are rebuilt.
- Historical trader features and source clusters enter without leakage.
- `tradingActivity` is either complete to a documented point or stopped for a documented final reason.
- At least one DEX adapter works end-to-end with liquidity and slippage.
- Dataset provenance and full coverage are exported.
- The time-based evaluation and ablation pass without opening Test for development.
- A frozen paper-trading round runs with full decision and execution logging.
- The permitted outcome remains one of three: a proven edge, no edge, or inconclusive.
