# Verification report — EVM admission-gate fix (2026-08-29)

## Summary

The cause of the EVM admission-gate stoppage (Robinhood Chain 4663 and Base 8453) was diagnosed
on the live database. Four compounding defects were found, not one, and all four were fixed
together. The full suite **passed 990 tests** after the fix (including 11 new tests).

## Diagnosis (measured on the live database, 2026-08-29)

| Measurement | Value |
|---|---|
| Gate 4663 status | `paused` — work_units 116,610 versus capacity 72 (×1,619) |
| Gate 8453 status | `paused/hysteresis_high_water` — work 14,622 versus 480 (×30) |
| RPC | healthy on all three networks (`rpc_healthy: true`) — no provider defect |
| 4663 queue | 43 tokens, 38 of them above 10M remaining blocks, all admitted within 3 days |
| Admission cause | 43 tokens, including reactivations of expired watches, bypassed the gate |

## The four defects and their fixes

### 1. Work units were estimated in the wrong unit (the core one)
`evm_admission_policy` computed a work unit = the range cap `range_hint` (10,000 blocks),
while the actual fill measurement (evm.log): a single call covers **~500,000 blocks** on
Robinhood (there is no range cap there at all — adaptive halving), and a full token with
79,894 transfers completed in 33 calls. The wrong estimate inflated the work ×50 and kept
the gate paused **forever**.

**Fix:** the constant `EVM_BACKFILL_BLOCKS_PER_CALL` in config.py (measured per network),
and `evm_admission_policy` now uses it instead of `range_hint`.

### 2. No upper age cap
The age gate (a two-day minimum only) was admitting tokens **one to two years old**
(26 admissions over a year old on 8453 since 22/8), whose ledger fill requires scanning
10–47 million blocks.

**Fix:** `EVM_MAX_TOKEN_AGE_DAYS = 60` — applied to EVM networks only at the moment a window
opens (new or reactivation). The justifying measurement: on 4663 admissions since 22/8 it
rejects only 4% (3/82); on 8453 it rejects 7/11 of today's queue (work drops from 14,552 to 44);
and 0 of 39 EVM tokens tagged at 30–60+ days old ever rugged (all rug events happened within
<60 days).

### 3. The policy check was not applied to reactivation
`policy.allows` was checked only when `existing is None` (a new token), so reactivating an
expired watch bypassed the pause entirely — the back door that fed the queue.
A comment in the old code claimed the gate governs "new and reactivation," which was false.

**Fix:** the check now runs on every `_opens_window(existing)` — a new or reactivated window,
and the counter `evm_admission_deferred` counts both cases.

### 4. Reactivation was deleting all fill progress
`upsert_watch` erased `evm_balances` + `evm_backfill_state` + `evm_replay_state`
on every reactivation, so a token that had completed most of its fill went back to zero.
This is why 43 tokens stayed in the queue spanning 20–47M blocks even though they had entered
the watchlist repeatedly.

**Fix:** `EVM_REACTIVATION_KEEP_LEDGER_SECONDS = 48h` — a gap ≤48h since the previous window
closed preserves the ledger and the resume point and flips `done`→`partial` to catch up the gap;
a longer gap rebuilds from genesis (the old safety behavior).

## Verification

- **New tests:** `recorder/tests/test_evm_admission_gate_fix.py` — 11 tests
  covering the four defects + the boundaries (cap disabled, unknown age, short/long gap).
- **Updated tests:** `test_db.py::test_evm_readmission_invalidates...` (documented the new
  short-gap behavior), and `test_evm_admission.py::test_feed_signal_survives...`
  (aged the token so the cap would not block it — its subject is isolating admission failure).
- **Full suite:** `py -3 -m pytest tests/ -q` → **990 passed in 64.39s**.
- **Simulation on live-database data (read-only):** after fixes 1+2:
  - 4663: work drops from 116,610 to 2,381 (still constrained ×33 but drainable
    — each cycle pushes up to 72 units, meaning days, not hundreds of years).
  - 8453: work drops from 14,622 to **44** — the gate opens automatically at the first
    following cycle (below the caution threshold ×1).

## Expected operational impact

- The Base gate opens automatically within the coming hours (44 units < 480 capacity).
- The Robinhood queue drains without a rebuild: renewed admission is now governed by the
  policy, the upper cap blocks millions of new blocks from entering, and reactivation no
  longer zeroes progress.
- Solana is completely unaffected (the cap and the policy are EVM-only).

## Modified files

| File | Change |
|---|---|
| `recorder/config.py` | +`EVM_MAX_TOKEN_AGE_DAYS`, +`EVM_REACTIVATION_KEEP_LEDGER_SECONDS`, +`EVM_BACKFILL_BLOCKS_PER_CALL` |
| `recorder/recorder.py` | Work units by actual measurement; age cap; policy on reactivation; `_evm_age_exceeds_cap`, `_evm_admission_networks`; `evm_max_age_rejected` counter |
| `recorder/db.py` | `upsert_watch` preserves the ledger on a short gap; `_reactivation_gap_exceeds_keep` |
| `recorder/tests/test_evm_admission_gate_fix.py` | New — 11 tests |
| `recorder/tests/test_db.py`, `test_evm_admission.py` | Updated for the new gate behavior |

**Not committed** on the `recorder-age-gate` branch, within the pending age-gate change package.
