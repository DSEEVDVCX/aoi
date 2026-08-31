# Task list: completing FOMO and blockchain data

This list executes `tasks/plan.md`. Every task must end with tests and a verification report before
the next task is moved to `completed`.

## Phase 0 - Baseline and audit

### T001 - Readiness report contract [completed 2026-08-19]

**Description:** Define a stable JSON schema for a per-source health and coverage report.

**Acceptance criteria:**

- [x] The schema distinguishes `ok`, `empty`, `missing`, `error`, `stale`, and `unsupported`.
- [x] It includes coverage by day, network, and feature family.
- [x] It contains no secrets or raw payloads.

**Verification:** Schema tests on complete and incomplete fixtures.

**Dependencies:** none.

**Expected files:** `recorder/data_readiness.py`, `recorder/tests/test_data_readiness.py`.

### T002 - Implement the readiness reader [completed 2026-08-19]

**Description:** Build a read-only database tool that produces JSON and a text summary.

**Acceptance criteria:**

- [x] Opens SQLite in `mode=ro`.
- [x] Measures counts, timestamps, states, coverage, and feature versions.
- [x] Executes no `INSERT`, `UPDATE`, or migration.

**Verification:** `py -3 -m pytest recorder/tests/test_data_readiness.py`.

**Dependencies:** T001.

### T003 - Add data-integrity checks [completed 2026-08-19]

**Description:** Add uniqueness, model-view stability, timestamps, and EVM states.

**Acceptance criteria:**

- [x] Detects duplicate decision rows.
- [x] Detects a successful replay with negative balances or stale training rows.
- [x] Displays failures without modifying the database.

**Verification:** Fixtures with deliberate faults.

**Dependencies:** T002.

### T004 - Freeze the reference report [completed 2026-08-19]

**Description:** Run the readiness tool and save an auto-generated dated report.

**Acceptance criteria:**

- [x] The report states the measurement time, database path, and size.
- [x] Numbers for every current family are present.
- [x] No manual, non-reproducible numbers.

**Verification:** Re-run and schema comparison.

**Dependencies:** T003.

## Checkpoint A [completed 2026-08-19]

- [x] Full unit tests pass: `786 passed`.
- [x] The reference report is saved in `docs/data-readiness-2026-08-19.json` and `.md`.
- [x] `recorder/recorder.db` size and stamp are identical before and after the report.

## Phase 1 - EVM integrity

### T005 - Verified backup [completed 2026-08-19]

**Description:** Take a consistent snapshot before any finalize or derivative reset.

**Acceptance criteria:**

- [x] `backup_db.py` finishes successfully.
- [x] `PRAGMA quick_check` on the backup returns `ok`.
- [x] The backup is stored outside the project in `C:\Users\rr\OneDrive\aoi-backups`.

**Verification:** A backup report exists.

**Dependencies:** T004.

### T006 - EVM repair status report [completed 2026-08-19]

**Description:** Run `repair_evm_ledger.py` in inspect mode for the allowed networks.

**Acceptance criteria:**

- [x] Logs active pending and replay pending per network.
- [x] Separates `partial`, `negative`, `budget`, `empty`, `done`.
- [x] No finalize while pending is above zero.
- [x] An EVM writer-contention defect was found and removed by making `run_evm_replay.py` the sole owner.

**Verification:** Compare the report with `evm_replay_state` and `evm_backfill_state`.

**Dependencies:** T005.

### T007 - Finish the live backfill [in progress via FomoEVMReplay]

**Description:** Process active tokens' ledgers to a correct final state.

**Acceptance criteria:**

- [ ] `active_pending=0` for the target networks.
- [ ] The cursor does not advance when a batch fails.
- [ ] No negative balance for a regular address.

**Verification:** EVM tests and a dated progress log.

**Dependencies:** T006.

### T008 - Finish replay for mature windows [in progress via FomoEVMReplay]

**Description:** Complete `4663` and `8453` respecting call budgets and final states.

**Acceptance criteria:**

- [ ] `replay_pending=0` for the required mature windows.
- [ ] Non-completable cases end with an explicit reason and are not retried forever.
- [ ] No concentration row is written for a negative state.

**Verification:** A before/after report and sample states.

**Dependencies:** T007.

### T009 - On-chain audit of an EVM sample

**Description:** Compare supply and top-holder balances at selected blocks.

**Acceptance criteria:**

- [ ] A sample from every supported network.
- [ ] The top five holders match, or their difference is under the documented threshold.
- [ ] Any mismatch is classified, not hidden.

**Verification:** `audit_evm_ledger.py` and a JSON report.

**Dependencies:** T008.

### T010 - Finalize and rebuild EVM

**Description:** Delete only the EVM training derivatives and rebuild them from the corrected snapshots.

**Acceptance criteria:**

- [ ] Finalize refuses if pending returns.
- [ ] `training_missing=0` after the build.
- [ ] `evm_ledger_rebuild_required=0` at the end.

**Verification:** Repair tests, then a new readiness report.

**Dependencies:** T009.

## Checkpoint B [waiting for T007 and T008]

- [ ] All EVM tests pass.
- [ ] An audit report is saved.
- [ ] EVM rows are built on the current version only.

## Phase 2 - Trader-profile history

### T011 - Design and migrate `trader_snapshots`

**Description:** Add an append-only table with its indexes without breaking `traders`.

**Acceptance criteria:**

- [ ] Idempotent migration.
- [ ] The key `(trader_id, recorded_at)` prevents duplicates.
- [ ] `raw_json` is compressed through the unified DB path.

**Verification:** Schema and migration tests.

**Dependencies:** T004.

### T012 - Dual write for profile and snapshot

**Description:** Update the traders cycle to write the snapshot and latest state in one transaction.

**Acceptance criteria:**

- [ ] A failure in either insert leaves no half state.
- [ ] An old snapshot is never erased.
- [ ] The latest table stays consistent with the leaderboard.

**Verification:** Tests for the traders cycle and rollback on error.

**Dependencies:** T011.

### T013 - Prevent unlimited identical copies

**Description:** Compare a hash or the key fields before writing a new snapshot.

**Acceptance criteria:**

- [ ] An unchanged profile is not written every cycle.
- [ ] A daily heartbeat preserves measurement continuity.
- [ ] A changed profile is written immediately.

**Verification:** A three-cycle test: identical, identical, changed.

**Dependencies:** T012.

### T014 - New-buyer priority

**Description:** Prioritize a buyer who appears in a new signal over the long periodic sweep.

**Acceptance criteria:**

- [ ] Old traders are not starved forever.
- [ ] The time from signal to first snapshot is measured.
- [ ] The cycle limit stays within 60 seconds.

**Verification:** A mixed-queue test and a latency metric.

**Dependencies:** T012.

## Phase 3 - Trader features

### T015 - Trader-profile feature contract

**Description:** Fix the names and definitions of point-in-time profile features.

**Acceptance criteria:**

- [ ] No identifiers or handles in the features.
- [ ] Every ratio has a documented denominator and missing state.
- [ ] `buyer_profile_age_min` exists.

**Verification:** Contract review and computational tests.

**Dependencies:** T013.

### T016 - Implement profile features at `t0`

**Description:** Read the latest `trader_snapshot.recorded_at <= t0`.

**Acceptance criteria:**

- [ ] A row after `t0` does not change the features.
- [ ] The absence of a snapshot returns NULL, not the current latest profile.
- [ ] Logarithmic computations are safe.

**Verification:** A mandatory future-row test.

**Dependencies:** T015.

### T017 - Prior trader-history features

**Description:** Aggregate the trader's prior signals and mature outcomes.

**Acceptance criteria:**

- [ ] The current row is excluded.
- [ ] A prior outcome does not enter unless its horizon ended before `t0`.
- [ ] The minimum sample and smoothing are documented.

**Verification:** Tests on overlapping time rows.

**Dependencies:** T016.

### T018 - Multi-trader quality aggregation

**Description:** Derive topTraders group features without revealing identity.

**Acceptance criteria:**

- [ ] median/best/coverage are computed on the measured only.
- [ ] An empty list differs from a list whose profiles were never measured.
- [ ] A single row does not carry the weight of a whole group without a coverage flag.

**Verification:** Mixed-list tests.

**Dependencies:** T017.

### T019 - Merge trader features into training

**Description:** Update the schema, `FEATURE_COLUMNS`, the builder, and the version.

**Acceptance criteria:**

- [ ] migrations and ROW_COLUMNS match.
- [ ] The coverage report shows the family.
- [ ] The model view accepts no old-version row.

**Verification:** `test_features.py`, `test_build_rows_cycle.py`.

**Dependencies:** T018.

## Checkpoint C

- [ ] Trader-snapshot writing works live.
- [ ] Future-row tests pass.
- [ ] A coverage report states the family's validity over time.

## Phase 4 - Source presence and signal clusters

### T020 - Migrate `token_source_presence`

**Description:** Add a light derived table with point-in-time indexes.

**Acceptance criteria:**

- [ ] No duplicated raw envelope in the table.
- [ ] source and observed_at are mandatory.
- [ ] rank is nullable.

**Verification:** Schema tests.

**Dependencies:** T004.

### T021 - Rebuild presence from snapshots

**Description:** Extract historical trending/verified/most_held in resumable batches.

**Acceptance criteria:**

- [ ] Idempotent.
- [ ] Joins by address and network, not index alone.
- [ ] A checkpoint prevents a full rescan on every run.

**Verification:** Fixtures for the deleted item and order changes.

**Dependencies:** T020.

### T022 - Measure the meaning of rank

**Description:** Test whether each list's ordering carries stable meaning or is just response order.

**Acceptance criteria:**

- [ ] A report per source.
- [ ] rank stays NULL for an unproven source.
- [ ] No rank features are built before the decision.

**Verification:** Replayed requests and documented time samples.

**Dependencies:** T021.

### T023 - Source-path features

**Description:** Add first seen, duration, appearances, transitions, and source multiplicity.

**Acceptance criteria:**

- [ ] Every query is constrained by `observed_at <= t0`.
- [ ] filterTokens is not treated as a popularity list.
- [ ] The source categorical is handled consistently between train and serve.

**Verification:** Future-row and transition tests.

**Dependencies:** T022.

### T024 - Audit trade/swap/transfer identifiers

**Description:** Measure the meaning and coverage of the identifiers before using them for dedup.

**Acceptance criteria:**

- [ ] A uniqueness and cross-event-match report.
- [ ] NULLs do not match each other.
- [ ] The dedup rule is proven before implementation.

**Verification:** A manual raw sample and an automated test.

**Dependencies:** T004.

### T025 - Signal-cluster features

**Description:** Compute 5/15/30/60-minute windows, buyers, and event direction.

**Acceptance criteria:**

- [ ] The current event is excluded where required.
- [ ] Dedup applies the T024 rule.
- [ ] A matching signal/activity is not counted twice.

**Verification:** Window-boundary and duplication tests.

**Dependencies:** T024.

### T026 - Merge the source and cluster families

**Description:** Update rows, version, and export.

**Acceptance criteria:**

- [ ] Coverage by source and day.
- [ ] No recognizable collection-start era without a clear flag.
- [ ] The live row uses the same function as training.

**Verification:** Build/export tests.

**Dependencies:** T023, T025.

## Phase 5 - tradingActivity

### T027 - Diagnose the backfill stoppage

**Description:** Determine why only 190 events exist.

**Acceptance criteria:**

- [ ] The cause is supported by a log or an actual response.
- [ ] It determines whether the problem is the endpoint, pagination, or the task.
- [ ] No modification before a diagnosis exists.

**Verification:** A limited read-only run and a report.

**Dependencies:** T004.

### T028 - Fix checkpoint and resume

**Description:** Make pagination resumable and resistant to transient emptiness.

**Acceptance criteria:**

- [ ] The checkpoint saves the id and oldest timestamp.
- [ ] Emptiness is retried before being treated as the end.
- [ ] A terminal error type is documented.

**Verification:** Tests for repeated, empty, and mid-chain error pages.

**Dependencies:** T027.

### T029 - Inventory new activity types

**Description:** Record the types and shapes before extending the extractor.

**Acceptance criteria:**

- [ ] unknown is stored raw and does not drop the task.
- [ ] Every supported type has a fixture.
- [ ] No fabricated fields between the flat and nested shapes.

**Verification:** Tests per type.

**Dependencies:** T028.

### T030 - Complete bars/labels for activity

**Description:** Gradually cover prices and outcomes for the new events.

**Acceptance criteria:**

- [ ] no_data is visible, not hidden.
- [ ] No duplication with signal_events.
- [ ] Progress and coverage appear in the readiness report.

**Verification:** Integration backfill -> bars -> label.

**Dependencies:** T029.

### T031 - Live-versus-retro bias report

**Description:** A report deciding the allowed use of activity.

**Acceptance criteria:**

- [ ] A comparison of networks, types, coverage, and eras.
- [ ] An explicit decision: analysis only, or separate training.
- [ ] No automatic merge into the model view.

**Verification:** A re-runnable report.

**Dependencies:** T030.

## Checkpoint D

- [ ] Activity advances or has a documented terminal-stop cause.
- [ ] No dependency-training change without the bias report.
- [ ] New FOMO families have coverage and time tests.

## Phase 6 - DEX discovery

### T032 - Inventory protocols and pools

**Description:** Analyze `dex_protocol`, `pair`, the networks, and coverage.

**Acceptance criteria:**

- [ ] A frequency table by network.
- [ ] A justified first network/protocol choice based on the data.
- [ ] A list of raw pair shapes saved as sanitized fixtures.

**Verification:** An inventory report.

**Dependencies:** T004.

### T033 - DEX adapter contract

**Description:** Define a unified interface for pool discovery and swap/liquidity decoding.

**Acceptance criteria:**

- [ ] No protocol details in features.py.
- [ ] side, amounts, decimals, block, and event index are clearly defined.
- [ ] unsupported is a normal state.

**Verification:** Mock interface tests.

**Dependencies:** T032.

### T034 - Migrate DEX tables

**Description:** Add pools, swaps, liquidity events, snapshots, states.

**Acceptance criteria:**

- [ ] Idempotent migrations.
- [ ] The keys prevent reorg/retry duplicates.
- [ ] Point-in-time and hot-query indexes exist.

**Verification:** Basic schema/query-plan tests.

**Dependencies:** T033.

### T035 - Pool discovery and verification for the first protocol

**Description:** Link the FOMO pair on-chain and verify the assets.

**Acceptance criteria:**

- [ ] token/quote match.
- [ ] decimals documented.
- [ ] discovery source and verified_at exist.

**Verification:** ≥20 real pools, or all available if fewer.

**Dependencies:** T034.

## Phase 7 - Swap and liquidity

### T036 - Swap decoder for the first protocol

**Description:** Decode buy and sell events and the actual price.

**Acceptance criteria:**

- [ ] The trade direction is proven with known buy and sell samples.
- [ ] Decoding is deterministic.
- [ ] A transfer alone does not become a swap.

**Verification:** On-chain fixtures and unit tests.

**Dependencies:** T035.

### T037 - Event cursor and live collection

**Description:** A standalone process that collects swaps with confirmations and resume.

**Acceptance criteria:**

- [ ] No gaps when a batch fails.
- [ ] No duplication on restart.
- [ ] A documented reorg policy.

**Verification:** Integration with a mocked RPC and mid-batch failure.

**Dependencies:** T036.

### T038 - Backfill pool windows

**Description:** Fill the history of watched tokens' windows only.

**Acceptance criteria:**

- [ ] Starts at pool creation or a documented boundary.
- [ ] Ends at the watch window, not an eternal head.
- [ ] budget/cursor are resumable.

**Verification:** A blocks/events/calls report.

**Dependencies:** T037.

### T039 - Liquidity-event decoder

**Description:** Extract mint/burn/add/remove according to the protocol.

**Acceptance criteria:**

- [ ] actor, amounts, and event type are correct.
- [ ] remove liquidity is not confused with a token burn.
- [ ] raw is preserved.

**Verification:** Known-transaction samples.

**Dependencies:** T036.

### T040 - Reserve and slippage snapshots

**Description:** Compute reserves, liquidity USD, price impact, and executable capacity.

**Acceptance criteria:**

- [ ] The computation accounts for fee and protocol.
- [ ] No future price is used to value the quote.
- [ ] Results are compared against actual transactions.

**Verification:** Computational tests and an on-chain cross-check.

**Dependencies:** T039.

### T041 - FOMO/DEX cross-check

**Description:** Compare OHLCV and token_flow with the collected swaps.

**Acceptance criteria:**

- [ ] Price and volume differences by a documented window.
- [ ] Outliers are named, not silently deleted.
- [ ] A pool quality standard is defined.

**Verification:** A match report.

**Dependencies:** T038, T040.

## Checkpoint E

- [ ] One protocol works end-to-end.
- [ ] Swap direction, price, and liquidity are audited.
- [ ] The new process does not slow the FOMO recorder.

## Phase 8 - DEX and wallet features

### T042 - Liquidity and execution features at `t0`

**Description:** Build liquidity, price impact, and executable capacity.

**Acceptance criteria:**

- [ ] Every query ends at `t0`.
- [ ] The dominant-pool choice is documented.
- [ ] Pool absence is NULL/unsupported, not zero.

**Verification:** Future-row tests.

**Dependencies:** T041.

### T043 - DEX flow features

**Description:** 5/15/60-minute windows for volumes, unique traders, and largest trades.

**Acceptance criteria:**

- [ ] Dedup by tx/event.
- [ ] Documented quote normalization.
- [ ] A wash proxy is not called certain wash; it is described as an indicator only.

**Verification:** Window and aggregation tests.

**Dependencies:** T041.

### T044 - A considered optional Transfer-events table

**Description:** Evaluate the cost of storing the transfers needed for precise historical features.

**Acceptance criteria:**

- [ ] Measure per-day/per-network volume before a full migration.
- [ ] A justified decision: full storage or summary.
- [ ] No duplication of what evm_balances already suffices for.

**Verification:** A benchmark and a decision report.

**Dependencies:** T010.

### T045 - New-holder and exit features

**Description:** Add 5/15/60-minute windows from the correct snapshots/events.

**Acceptance criteria:**

- [ ] EVM and Solana are separated by source capability.
- [ ] first seen in an incomplete ledger is not treated as the first historical ownership.
- [ ] A complete-history flag exists.

**Verification:** Tests on complete and incomplete ledgers.

**Dependencies:** T044.

### T046 - Developer and whale features

**Description:** The developer's and top holders' movement before the decision.

**Acceptance criteria:**

- [ ] The developer identity has provenance.
- [ ] An unknown developer is NULL.
- [ ] Movements after `t0` do not enter.

**Verification:** Future-row and multi-wallet fixtures.

**Dependencies:** T045.

### T047 - Authority and admin events

**Description:** Track ownership/mint/freeze/fee/limits when events or state are available.

**Acceptance criteria:**

- [ ] The change is dated by block.
- [ ] No definitive inference from a selector alone without describing it as a risk flag.
- [ ] proxy logic is handled separately.

**Verification:** Contract fixtures and transition tests.

**Dependencies:** T035.

## Phase 9 - Build and export

### T048 - Update schema and feature version

**Description:** Merge the accepted families into a single version package.

**Acceptance criteria:**

- [ ] schema/db/features match.
- [ ] No training column without a producer in build_features.
- [ ] The model view requires the new version.

**Verification:** Schema parity tests.

**Dependencies:** T019, T026, T042, T043, T045, T046.

### T049 - Remove N+1 from training and simulation

**Description:** Load the required bars efficiently instead of a query per row.

**Acceptance criteria:**

- [ ] Results match the old method on a sample.
- [ ] The full evaluation finishes within a set time budget.
- [ ] Memory stays within an acceptable limit.

**Verification:** A benchmark and a match test.

**Dependencies:** T048.

### T050 - A resumable full rebuild

**Description:** Build the rows with the new version in batches.

**Acceptance criteria:**

- [ ] No mixed row in the model view.
- [ ] A restart continues instead of redoing everything.
- [ ] A coverage report after every batch.

**Verification:** An interrupt/resume test and a final report.

**Dependencies:** T049.

### T051 - Update the export package and provenance

**Description:** Add the new files, families, and column roles.

**Acceptance criteria:**

- [ ] columns.json blocks identifiers/targets.
- [ ] Coverage by day and network.
- [ ] Provenance for every feature.

**Verification:** Rebuilding the target and rows from the export.

**Dependencies:** T050.

### T052 - A comprehensive leakage test suite

**Description:** Generate future rows in every new table.

**Acceptance criteria:**

- [ ] The feature row does not change.
- [ ] The maturity guard works for trader outcomes.
- [ ] The `t0+Delta` decision is separate from `t0`.

**Verification:** A standalone suite that fails when any time constraint is removed.

**Dependencies:** T051.

## Checkpoint F

- [ ] Build/export are complete.
- [ ] The train pipeline is fast enough.
- [ ] All leakage and coverage tests pass.

## Phase 10 - Evaluation

### T053 - Freeze splits, targets, and costs

**Description:** Write a manifest before any new training.

**Acceptance criteria:**

- [ ] train/val/test dates/tokens are saved.
- [ ] The target, exit rule, and costs are fixed.
- [ ] Test is not opened during development.

**Verification:** A hash of the manifest and the data.

**Dependencies:** T052.

### T054 - Run baselines

**Description:** Play-all, market cap, liquidity, momentum, and the current model.

**Acceptance criteria:**

- [ ] The same period and cost for every baseline.
- [ ] New-token results are separate.
- [ ] Calibration and net return are present.

**Verification:** A dated baseline report.

**Dependencies:** T053.

### T055 - Ablation study on Val

**Description:** trader/source/onchain/DEX, each family separately then combined.

**Acceptance criteria:**

- [ ] No selection on Test.
- [ ] Bootstrap at the token level.
- [ ] Slices by day, network, and protocol.

**Verification:** A re-runnable ablation report.

**Dependencies:** T054.

### T056 - Frozen model and threshold choice

**Description:** A single choice from the Val results per a pre-written criterion.

**Acceptance criteria:**

- [ ] It beats the baselines on Val after cost.
- [ ] It does not depend on a family with biased era coverage.
- [ ] The model card records the parameters and features.

**Verification:** Retraining with the same seed/hash.

**Dependencies:** T055.

### T057 - Open a fresh test once

**Description:** Evaluate the frozen model on the reserved period/tokens.

**Acceptance criteria:**

- [ ] AUC/PR/calibration and new tokens are reported.
- [ ] Net profit, drawdown, and costs are reported.
- [ ] The result is not re-tuned after being seen.

**Verification:** An immutable final report.

**Dependencies:** T056, and the control-group gate if the description is dependent.

## Phase 11 - Paper trading

### T058 - Paper-decision log schema

**Description:** Record acceptance, rejection, version, and execution.

**Acceptance criteria:**

- [ ] No-trade reasons are structured.
- [ ] feature/model versions are mandatory.
- [ ] No secrets or keys.

**Verification:** Schema tests.

**Dependencies:** T056.

### T059 - Execution simulator with pool liquidity

**Description:** Replace the fixed cost alone with slippage and executable size.

**Acceptance criteria:**

- [ ] A trade is rejected if it exceeds capacity.
- [ ] entry/exit impact are separate.
- [ ] The old fallback is clearly flagged when DEX data is absent.

**Verification:** Deep-liquidity, shallow-liquidity, and liquidity-withdrawal scenarios.

**Dependencies:** T040, T058.

### T060 - A frozen silent live run

**Description:** Run the model without money for the defined round duration.

**Acceptance criteria:**

- [ ] Every signal is logged, including rejected ones.
- [ ] The model and threshold do not change mid-round.
- [ ] Stale data produces no-trade.

**Verification:** Daily monitoring and a completion report.

**Dependencies:** T059.

### T061 - Round report and decision gate

**Description:** Evaluate profit, drawdown, and stability without cherry-picking.

**Acceptance criteria:**

- [ ] All trades and costs are included.
- [ ] A sensitivity analysis of the biggest winner and the top 1%.
- [ ] The verdict: confirmed, rejected, or inconclusive.

**Verification:** A dated report whose results are not re-edited.

**Dependencies:** T060.

## Definition of Done

- [ ] No new source without raw/provenance/timestamp/state.
- [ ] No new feature without a point-in-time test.
- [ ] No family enters the training dependencies without non-era-biased Train/Test coverage.
- [ ] No EVM finalize before zero pending and a sample audit.
- [ ] No Test-shopping or random row split.
- [ ] No profit claim without liquidity, slippage, and fees.
- [ ] No real trading within this plan.
