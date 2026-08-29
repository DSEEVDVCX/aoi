# Data audit — 2026-08-22

Six-domain audit of `recorder.db` (18.96 GB) with every finding adversarially
re-measured by an independent verifier. **Three of the six domain auditors and
nine of 27 agents died on API quota**, so this report is partial — see BLIND
SPOTS. 18 agents completed, 4.64M subagent tokens, ~2.5 h.

Population at audit time: `training_rows` 108,441–108,685 rows (live growth
during the run), model population 10,820–10,856, `current_feature_version` = 12.

## Resolved after the audit, same day

- **The 11 EVM contract columns**: cause was scope, not a broken collector — only
  Base was scanned (1.0% of model rows) and Base's newest model row (Aug 4)
  predates the collector's start (Aug 13), so overlap was zero. Re-measured BSC
  and Robinhood bytecode: **BSC `is_proxy` = 26/40**, the most discriminating
  column on any network, and Robinhood varies on `has_pause` where Base is
  constant. The Aug 13 "template label ⇒ no information" conclusion read variance
  through `code_size` alone and was wrong. `EVM_CONTRACT_NETWORKS` widened to
  `("8453","56","4663")` — 41% of model rows. BSC and Robinhood wrote their first
  rows live at 21:40:02Z. `EVM_CONTRACT_PER_CYCLE` deliberately left at 2: the
  Base node's measured quota is 9 calls per ~15 s window at 4 calls per token.
- **`top10_holders_pct`**: unfixable — the upstream key is absent from all 240
  sampled payloads (0 non-null in 3,837,466 `market_ticks` rows). Retired from
  `FEATURE_COLUMNS`; replacements `chain_top10_pct` (4,506 non-null / 3,002
  distinct) and `onchain_top10_pct` (2,608 / 1,956) already carry it. Column kept
  in the table, no `feature_version` bump — it is NULL in every existing row, so
  dropping it changes no row's data. `ROW_COLUMNS` is now 198 against 199 table
  columns, documented in place.
- Both pinned by `tests/test_contract_networks_and_retired_column.py`; suite 880.

## Two throughput levers measured and rejected, same day

Both were my own proposals for speeding up the EVM backlog. Both are wrong, and
the measurements are recorded so they are not re-proposed.

- **Raise `EVM_BACKFILL_BUDGET_SECONDS` (25 s).** Its comment justified the cap
  by "the fast Solana layer in the same process", and that reason is stale —
  `chain_layer`'s only production caller is `run_chain.py:123` (FomoChain), a
  different process. But the cap protects *more* now, not less: the four steps
  that share the replay process and the same cycle, all **after** backfill in
  `run_evm_replay.run_cycle`, are the EVM concentration snapshots (step 3 of
  `run_evm_cycle`), `bsc_layer`, `evm_contract` — three networks since today,
  not one — and `evm_replay` with its own 120 s budget, against
  `EVM_REPLAY_INTERVAL_SECONDS = 60`. Comment corrected in both `config.py` and
  `evm_layer.py`; value unchanged. A stale justification on a correct number is
  exactly what kept `EVM_CONTRACT_NETWORKS` on Base for nine days.
- **Add Robinhood to `EVM_BACKFILL_ASSIST_NETWORKS` (`("8453",)`).** Refuted by
  `config.py:622-624` and by measurement: Robinhood's backfill is 66 partial /
  66 done, Base is **51 partial / 19 done / 1 retry**. Base is the deeper hole,
  and it already has the assist. Giving Robinhood a share would take budget from
  the network that needs it.

## New problem found while closing those two questions

### LOW — two Monad backfills retry forever and have never produced a row

Both Monad (143) tokens sit at `status='retry'` with `from_block`, `to_block`,
`transfers` and `calls` all NULL — they have never completed a single call.
Both failed on the same cause, QuickNode's `50/second request limit`, at
2026-08-22 17:55Z and 19:17Z; the second is also the newest value of
`meta.last_error_evm`. `chain_concentration` has **no row on 143 at all**
(4663: 286,557 · 8453: 11,136 · 56: 9,349), and 143 contributes **1 of 10,916**
model rows.

Cost is bounded, which is why this is LOW: `pending` sorts by `last_try_at`
ascending, so recently-tried Monad tokens sit at the tail behind 117 partials,
and `EVM_REPLAY_NETWORKS` already excludes 143. **Do not drop 143 from
`EVM_NETWORKS`** — `config.py:563` states the intent to re-enable it the moment
an active Monad token appears, and one has (first seen 2026-08-21T00:56Z). The
real defect is the class, not the network: a token whose *first* call is
rate-limited stays `retry` indefinitely with no escalation, so "never started"
and "still walking" look identical in the state table. `EVM_LOG_RANGE_HINT`
gives 143 a 100-block range, so even a successful start would need an enormous
number of calls.


Still open: problem 1 (control arm age gate) and problem 2 (the EVM quarantine,
which is what keeps Robinhood's 17.1% frozen at Aug 4 despite the widening).

## Verdict

The data is structurally sound: no orphans, no split-coin address bugs, no
timestamp corruption, no schema drift, and **zero rows where we admitted a coin
and collected nothing**. The worst problem is not in the stored data at all — it
is that the new ≥2-day age gate was applied to the signal arm only, so the
control arm now differs from it systematically in the one variable most
predictive of outcome. Separately, ~15,975 EVM training rows and ~4,400
model-eligible EVM outcomes are quarantined behind an unfinished ledger repair.

## Answer to the zeros question

**No row of zeros exists.** In the model population (n=10,820) not one row has
every non-trigger feature family NULL. The trigger, social, price-path, regime,
density and label families are 0.0% empty. Non-NULL feature cells per row, out
of 178: min 34, p25 91, median 95, p75 127, max 152.

**12 columns are 100% NULL** in the model population and across all 108,441
rows. Every one is documented measured-absence, not a bug:

| columns | why NULL | verdict |
|---|---|---|
| `top10_holders_pct` | the upstream key is absent from every payload — 0 of 240 sampled `raw_json` bodies carry `top10HoldersPercent`. Doctrine requires NULL; a 0 here would be the bug. Superseded by `chain_top10_pct` / `onchain_top10_pct`. | DOCUMENTED-ABSENCE |
| `onchain_code_size`, `onchain_function_count`, `onchain_is_proxy`, `onchain_owner_renounced`, `onchain_has_mint_fn`, `onchain_has_pause_fn`, `onchain_has_blacklist_fn`, `onchain_has_fee_setter`, `onchain_has_limit_setter`, `onchain_has_trading_switch`, `onchain_contract_age_min` | `config.py:512 EVM_CONTRACT_NETWORKS = ("8453",)` — Base only, which is 1.03% of model rows; and 0 of 685 Base rows have `entry_ts` at or after the collector's start (2026-08-13 18:16:54). Two by-design NULLs stacked. | DOCUMENTED-ABSENCE |

**26 columns hold a single distinct value.** Six are constant *because of the
population filter* (`kind`, `asset_class`, `status`, `is_live`,
`is_independent`, `feature_version`). Twelve are the 100%-NULL set above. Of the
remaining eight genuinely degenerate features, three are documented-correct —
`onchain_has_freeze_authority` is constant 0 here by rarity (schema.sql:666
measured mint authority live in 3/48 and freeze in 1/48, and over the whole
table the column does vary: 14,225 non-null, 2 distinct); `is_scam` is 99.79%
NULL upstream. **One is a real finding: `listed_on_exchange`** — see below.

**9 columns are >90% exactly zero, and every one is a genuine measured zero.**
The verifier settled the strongest candidate: `social_replies` is 0 in 104,862
of 104,862 non-null rows, and `numReplies` is present as a key in 33,537 of
33,537 decoded live thesis items carrying integer 0. The doctrine forbids
fabricating a zero for an unmeasured value; it does not forbid recording a real
one. Same for `suspect_bars` (93.35%), `top_trader_match_count` (95.36%),
`top_traders_listed` (99.95%), `ticker_has_digit` (97.82%).

## Problems

### 1. HIGH — the control arm has no age gate

`age_verdict` and `_opens_window` appear only inside `record_feed`.
`admit_control_sample` never calls them, so control coins are admitted at any
age while signal coins need ≥2 days.

Measured on windows opened since 2026-08-22:

| arm | windows | age ≥2d | age <2d |
|---|---|---|---|
| signal (gated) | 1,142 | 1,125 (98.5%) | 17 (1.5%) |
| control (ungated) | 26 | 13 (50.0%) | **13 (50.0%)** |

The two arms now differ by 48 points in the share of coins under two days old —
the variable measured at a 29× rug rate and a −46.2% vs −5.1% median return.
Any signal-vs-control comparison from here on attributes that age gap to the
signal. 35 control watches are active.

**Fix:** apply the same `age_verdict` check in `admit_control_sample`, or
deliberately decide the control arm should sample the unfiltered universe and
write down why. This is a decision, not just a patch: gating the control arm
makes it match the signal arm's population; leaving it ungated makes it match
the market. Either is defensible, but the current state is neither by choice.

### 2. MEDIUM — EVM training rows quarantined for 10 days

15,975 rows (14.73% of `training_rows`) sit at `feature_version=8`, all on
networks 4663 (12,092) and 8453 (247), invisible to the model view. Roughly
31,592 labeled EVM outcomes have no current-version training row, and 4,392–4,413
of those are model-eligible. The model population contains **no EVM signal newer
than 2026-08-04**.

The verifier confirmed the numbers and corrected the diagnosis: this is not a
stuck latch but the designed hold of an in-flight repair.
`build_training_rows.pending_outcomes()` (lines 60-72) excludes every EVM
network while `meta.evm_ledger_rebuild_required=1` and
`evm_training_rebuild_started=0`, which is exactly the current state.
`repair_evm_ledger.py`'s docstring states the intent: the EVM ledger was found
untrustworthy, derived values were NULLed, and `--finalize-training` removes
only EVM training rows so the builder recreates them from corrected snapshots.
Release is blocked by **44 unfinished cohort backfills and 132 unfinished replay
keys**, counted directly.

**Fix:** finish the backfills and replay keys, then run
`repair_evm_ledger.py --finalize-training`. Until then, treat the model
population as Solana + BSC only.

### 3. LOW — `listed_on_exchange` is a zero-variance feature

1 in all 101,658 non-null rows, NULL in 6,783; no other value ever. The zero
branch of the derivation is unreachable. The NULLs are correct (they come from
`features.py:306` returning the all-None dict when there is no `token_static`
row — 1,270 of 10,820 model rows, 11.74%), so this is hygiene, not a doctrine
violation.

**Fix:** drop it from `FEATURE_COLUMNS` or keep the column and stop shipping it
as a feature. `exchanges_count` already carries the information with variance.

### 4. Measured but never verified (verifier agents died)

- 43 keys collide between `kind='activity'` and `kind='signal'` — the same feed
  event labelled as two decisions. Harmless to the model view, which filters
  `kind='signal'`.
- 4 duplicate `signal_events` for the same `(token, network, ts, signal_type)`.
  The model view's dedup subquery already keeps only the lowest key.
- 1,074 model rows (9.9%) have no market snapshot and 1,270 (11.7%) no
  token-static family. The column-health auditor independently attributes both
  to the collector-rollout curve.

## What is healthy

- **Orphans: 0, in both directions,** across all six core tables.
  `watch_windows` with no `token_static` 0/28,688; `watchlist` with no window
  0/1,170; `outcomes` whose key is not a `signal_events.id` 0/101,370.
- **Address and network normalization is completely clean.** `network_id` is
  TEXT in 100% of rows in all six tables — never integer, never NULL, never
  empty. Every EVM address matched exactly; the 6,384 mixed-case addresses in
  the model population are all Solana base58. Re-running reachability with
  `lower()` recovered 0 extra rows.
- **Split assignment is correct and stable.** 0 coins in more than one split.
  `labeler.assign_split` was recomputed from scratch for all 1,231 distinct
  coins — 0 mismatches. Balance 73.3% train / 7.9% val / 18.7% test against a
  70/10/20 coin-hash target, the expected consequence of hashing by coin.
- **Timestamps are clean.** No timezone mixing (every ISO column carries
  `+00:00` on 100% of rows), no epoch/ms confusion, nothing in the future, no
  `entry_ts` ≤ 0. `token_created_at` is uniformly 10-digit epoch text (1,191 of
  1,191), which `features.epoch_of()` handles.
- **No schema drift.** `PRAGMA table_info(training_rows)` = 199 columns,
  `features.ROW_COLUMNS` = 199, symmetric difference empty both ways. The
  NULL-forever migration-column trap is not present.
- **Labels are complete.** `final_return_48h`, `max_gain_48h`,
  `max_drawdown_48h`, `time_to_peak_h` are 0.0% NULL in the model population
  (9,153 / 8,993 / 9,066 / 8,288 distinct values).
- **Build timing is sound.** `built_at − entry_ts` minimum is 48.3 h — exactly
  the watch window, so no row is built early and no label leaks.
- **The age repair holds.** 0 of 201 active watches missing `token_created_at`;
  0 of the 1,114 windows opened on 2026-08-22 have unknown age. Windows on coins
  under two days old fell 35.8% (08-19) → 16.2% (08-20) → gated.
- **Emptiness is a rollout curve, not an outage.** Mean empty non-trigger
  families per row by entry day: 7.85 (07-25) → 5.76 (08-09) → 3.23 (08-11) →
  1.46–1.97 (08-14..08-20). Collector switch-on dates measured:
  `token_holders` 08-09, `token_flow` 08-10, `chain_authority` 08-13 (Solana
  only), `evm_contract` 08-13 (Base only).
- All pipeline heartbeats fresh at audit time (labeler 20:30Z, build_rows
  19:51Z, age lookup 20:28Z, evm 19:19Z, chain 19:24Z).

## Blind spots

**Three of six domain auditors never produced results** (API quota / gateway
errors). Nothing below has been measured:

1. **outcomes-labels** — label *value* sanity (impossible returns, sign
   convention of `max_drawdown_48h`, `time_to_peak_h` outside [0,48]), closed
   windows with no outcome row, and reverse leakage. Only label *presence* was
   covered, incidentally, by the column-health auditor.
2. **collector-coverage** — per-collector staleness distributions, active
   watches with zero rows in a given collector, and downtime totalled in
   minutes. Related raw signal: 70 of 192 hours from 08-13 to 08-20 contain no
   population row, recurring ~02:00–09:00 UTC (the known sleep pattern), never
   quantified as a percentage.
3. **static-write-paths** — the divide-by-zero / NULL-denominator audit of every
   ratio feature. I checked the obvious coercions by hand beforehand and found
   the code disciplined (`build_training_rows.py:119` inserts missing keys as
   NULL; `evm_layer.py:106` returns `None` for an empty ledger; no production
   `julianday()` on `token_created_at`), but the ratio audit is unfinished.

**Two SQL traps the auditors hit, worth remembering:**

- `signal_events.ts` is TEXT ISO-8601 (`'2026-07-25T23:41:32.799Z'`) while
  `training_rows.entry_ts` is a real integer epoch. Comparing them, or applying
  `datetime(ts,'unixepoch')` to `signal_events`, silently yields NULL or matches
  every row. Two tables, two different time predicates.
- `chain_authority`'s real columns are `freeze_authority`, `mint_authority`,
  `update_authority`, `token_program` — not `has_*`. A query that
  double-quotes a wrong column name makes SQLite treat it as a **string
  literal** and return plausible garbage with no error.

## Recommended order of work

1. Decide and implement the control arm's age policy (problem 1). It is
   corrupting comparisons right now, and it is one function.
2. Finish the EVM backfills (44 cohorts, 132 replay keys), then
   `repair_evm_ledger.py --finalize-training` — this releases 15,975 rows and
   ~4,400 model-eligible EVM outcomes (problem 2).
3. Re-run the three dead auditors when quota allows:
   `Workflow({scriptPath: "recorder/audit_workflow.mjs"})`.
4. Drop `listed_on_exchange` from the feature list (problem 3).
5. Look at the 43 activity/signal key collisions and 4 duplicate signal events.
