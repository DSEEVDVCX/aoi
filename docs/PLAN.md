# What to Do After Data Collection — The Detailed Plan

> This document details every step after collection is complete, in order, with its criteria and traps.
> General reference: [`../README.md`](../README.md).

**Current status** (updated 2026-08-29): collection, labeling, and row building run
automatically. The current feature extractor is **fv16 = 171 features**. Control-group gate v3:
**426/500** mature; the preliminary check is open, and the admission decision remains closed (74 remaining,
rough ETA about 5 days at the current rate). These numbers change live; the binding reference
for count and freshness is the "Labeling & Results" page and the `/api/labeling` route, and for source health
it is the generated `recorder/data_readiness.py` report — not the historical numbers below.

**Data decision:** train on live data only — retro rows are structurally missing the instantaneous families
(market_ticks, token_social snapshots, leaderboard rank, macro), which creates an absence pattern tied to
the era, and the model learns that instead of the signal (era leakage). The retro set **was deleted from the live
database** (preserved in a `.bak` copy) and is excluded with `is_live=1`. **The historical maturity gate
for the old control group completed (2026-08-02): all conditions were met at the time,
and the old control group reached 123/100; its eligibility does not carry over to v3.** Phase 1 was run on
2026-08-02 and **did not pass the gate**: on the cleaned core sample (live memes, live
control group, one token per group) the median signal return was −28.9% versus −2.2% for the control,
and the win rate was 19.9% versus 33.3%. On top of that, the network mismatch and the elevated `no_entry`
in the control (30.8% versus 2.3%) prevent interpreting the difference causally. **No approved entry-model
training begins** before the control-group design is fixed or a new sub-hypothesis is confirmed; the limited
exploratory training at 100 mature v3 controls is documented below and does not count as approval. The report:
[`phase1-2026-08-02.md`](phase1-2026-08-02.md).

---

## Phase 0 — Maturity gate (where we are now)

**Do not start any modeling before these conditions are met.** Building a model on 50 samples is not
moving faster — it is wasting time: the result is noise indistinguishable from chance.

| Condition | Threshold | Status/Decision |
|---|---|---|
| Independent labeled samples (live memes) | ≥ 1000 | ✅ **1,925** (1447+133+345) |
| Of which in `test` | ≥ 200 | ✅ **345** |
| `v3` control group mature for the preliminary check | ≥ 100 | ✅ **426/100** as of 2026-08-29; preliminary check available, not an admission gate |
| `v3` control group mature for the core decision | **≥ 500** | ⏳ **426/500**; the binding threshold before accepting/rejecting the thesis |
| `status='ok'` ratio | ≥ 85% | ✅ ~99% |
| Live calendar days | ≥ 5 | ✅ **7.7** (2026-07-25 – 08-02) |
| **Feature extractor** | Built and tested | ✅ **fv16** (171 features · readiness report: model=ok) |

⇒ **The signal data maturity gate is open, but the v3 control-group gate is still closed.**
The old control group was deleted, and its eligibility does not carry over to the new design. v3 must be
collected per the thresholds fixed below before the decision is revisited.

**Note — retro data**: The table measures **live only** (`is_live=1`), which is what the
model is trained on. An additional 1,144 retro rows (multi_user_buy/sell from
October 2025) are labeled and stored but **excluded from training** by project decision —
the structural gap in market_ticks and the instantaneous families creates full era leakage.
The retro set remains for exploratory analysis only.

> First reading of it (win rate/median/peaks/exit simulator): [README §8 "First retro
> reading"](../README.md#8-what-the-data-says-so-far) — and the updated role of the model
> in light of it in §3.1-5 below.

### Gate query (run it before Phase 1)

```sql
-- Note: the `asset_class='meme'` constraint is mandatory (§2.1) — the archive contains BTC and tokenized stocks.
SELECT
  COUNT(*)                                                    AS labeled,
  SUM(o.status='ok')                                          AS ok,
  SUM(o.kind IN ('signal','activity') AND o.is_independent=1
      AND o.status='ok' AND COALESCE(t.asset_class,'meme')='meme') AS train_ready,
  SUM(o.kind='watch'  AND o.is_control=1 AND o.status='ok')    AS control_ready,
  SUM(o.split='test'  AND o.is_independent=1)                  AS test_rows,
  COUNT(DISTINCT o.token_address)                              AS tokens,
  ROUND(JULIANDAY(MAX(o.labeled_at)) - JULIANDAY(MIN(o.labeled_at)), 1) AS span_days
FROM outcomes o
LEFT JOIN token_class t
  ON t.token_address = o.token_address AND t.network_id = o.network_id;
```

### While waiting — do not sit idle

- Check the dashboard daily: candle coverage, frozen feed, per-source errors.
- If `status='ok'` drops below 85% ⇒ hunt for a collection gap **now**, not later.
- If `multi_user_buy` stays rare in forward collection (~14/day) ⇒ that no longer means
  waiting: the retro path (`activity_events`, see the evolution above) covers this branch
  back to 2025-10-22 once labeled.

### Staged modeling and paper-trading cycle (binding decision 2026-08-02)

We do not sit idle waiting for 500 controls, and we do not mix an early model with the approved one. The fixed
cycle is:

#### At 100 mature v3 controls

- Run the preliminary safety check: coverage, `no_entry`, network and time balance,
  outliers, and collection stability.
- Run `classify_tokens.py` then `build_training_rows.py --rebuild`.
- Train a frozen exploratory model under a clear version name, e.g.
  `model-v1-100-controls`, on the valid signals per the documented filters.
- Keep `test` closed; use `train/val` for feature and parameter selection, and do not re-split
  by token or try multiple splits to pick the best one.
- Start a separate exploratory paper-trading run under a name like
  `paper-run-v1-model-100`. This test does not prove the thesis is profitable and does not open real
  trading; its purpose is to surface prediction, execution, slippage, and liquidity errors.
- Freeze the model and decision rules during the run. Do not tweak the threshold daily to make
  the results look profitable, and do not cherry-pick a winning window after seeing it.

#### What paper trading v1 logs

- Every signal the model entered and every signal it rejected, with reason, probability, and version.
- Expected price and the default execution price, delay, fees, slippage, and liquidity.
- Profit/loss, drawdown, `rug` rate, and the number of unexecutable trades.
- Fixed baselines for comparison: playing every signal, market cap alone, volume alone,
  and a model with no social features.
- "The model is profitable" is not declared from v1; the correct description: **exploratory paper trial**.

#### At 500 mature v3 controls and 14 days

- Re-run Phase 1 formally on `v3` only, and open `test` a single time for the final evaluation.
- Require median and win rate and effect size and confidence intervals and stability across days
  and networks, plus sensitivity analysis of the biggest winner and the top 1%.
- Run `classify_tokens.py` then `build_training_rows.py --rebuild` again.
- Train a fresh independent model under a name like `model-v2-500-controls`; do not update
  v1's files in place and do not mix the two models' logs.
- Close `paper-run-v1` with a dated report, and create a new paper wallet with a fresh starting
  balance named `paper-run-v2-model-500`. **We reset the paper wallet only; we do not delete v1's log**;
  comparing the two trials is part of the validation.

#### If the verdict is still inconclusive at 500

- We do not cherry-pick a result we like, nor force v2 through; we continue to 1,000 controls or until the
  intervals narrow and the result stabilizes over time.
- The exploratory paper trading can continue, but it must be clearly marked as not approved.

The decisive split: **v1 at 100 for operational learning, v3 at 500 and 14 days for analytical
admission and the new paper trial**. Reaching 100 does not mean the thesis succeeded.

---

## Phase 1 — a question before the model: does the signal beat the market?

> **Result (2026-08-02): not passed.** The signal is descriptively worse in median and
> win rate, but the current control group is not a clean causal counterpart because of universe,
> network, and missing-entry differences. No training of the general entry model before that is fixed.
> Reproducible details: [`phase1-2026-08-02.md`](phase1-2026-08-02.md),
> and the tool: `recorder/phase1_analysis.py`.

### Control-group design fix v3 (applied 2026-08-03)

The structural flaws uncovered by the Phase 1 audit and the leakage audit are closed for **new** data:

- `watch_windows` is an immutable log independent of the operational `watchlist`; promoting a control
  to a signal or reactivating a token no longer erases the previous window.
- Every new window carries `design_version=3` and a documented instantaneous acceptance price from feed or
  trending/verified; that same price is the comparison entry for both sides, so neither side depends on
  a later candle arriving more than the other.
- A control candidate with no bounded positive price is rejected, and selection matches the network mix
  of active signals cumulatively instead of drawing from available networks unadjusted.
- The window is not deactivated or labeled before final candle pulls after `watch_until`; that way the labeler
  no longer writes `no_entry` because of a transient race with the recorder.
- The migration is guarded by a write lock, and an old window's pull state resets on reactivation.
- The old control group was permanently deleted on 2026-08-02 by the owner's decision: its windows,
  outcomes, and operational states, as well as data derived solely from the 194 control tokens that never
  appeared as signals, were removed from the operating database. There is no local backup of this control
  group after the operation. Signals and shared token data were not deleted.
- The View named `phase1_watch_outcomes` remains as the safe interface and exposes only eligible rows; do not
  query raw `outcomes` to produce a comparison.
- A signal window enters the new comparison only if the token appeared in the same `trending/verified`
  cycle the control group is built from, with `admission_source` preserved; and the control group is not
  filled outside a cycle that admitted a comparison signal, to prevent time and universe differences.
- Training-derived leaks were fixed: the candle open at `t0` and the open hourly macro do not enter
  features, `token_static` is dated with `recorded_at <= t0`, all aggregate and density queries are
  network-qualified, and events matched between signal and activity are counted once in density.
- `training_rows` is now rebuilt with `feature_version=2`, and the `model_training_rows` view enforces
  `signal + live + meme + ok + independent`; training must not run from the raw table directly.

**Where things stand**: v3 collection started after the migration was applied on 2026-08-03. At ≥100 mature
controls and ≥5 days, a **preliminary safety check only** runs — not a decision. The Phase 1 decision re-run
waits for ≥500 mature v3 controls and ≥14 v3 calendar days, with the network-mismatch, missing-entry,
and time-stability guards below. This is a wait for fresh valid data, not a wait for code.

**This is the most important test in the entire project, and it precedes any model approval.** The v1 model
at 100 is exploratory only; if the signal does not beat a random token from the same universe, then approving
the model or building it for real trading would be building on sand.

### The test

Compare `kind='watch'` between `is_control=0` and `is_control=1` on **completed windows**:

```sql
SELECT is_control,
       COUNT(*)                                   AS n,
       ROUND(AVG(final_return_48h) * 100, 2)      AS avg_ret,
       ROUND(AVG(max_gain_24h)     * 100, 2)      AS avg_peak,
       ROUND(AVG(final_return_48h > 0) * 100, 1)  AS win_rate,
       ROUND(AVG(is_rug) * 100, 1)                AS rug_rate
FROM outcomes
WHERE kind = 'watch' AND status = 'ok'
GROUP BY is_control;
```

### How to read the result

- Always use the **median** alongside the mean. One anomalous winner (+1092%) flips
  the mean by itself — we have seen it happen.
- A significance test suited to a heavily skewed distribution: **Mann–Whitney U** on `final_return_48h`
  (not a t-test — the distribution is not remotely normal).
- Compute the **effect size**, not just significance: the median difference and the win rate.
- **Compute power in advance** (settled 2026-07-28): with a distribution this skewed, 100 controls
  can only detect a large difference. Fix before running "the smallest median difference worth pursuing"
  (say 5 percentage points) and compute the sample size needed for it — otherwise you get a
  "no difference" result that cannot distinguish "no effect" from "no power". If the available n is below
  the requirement, document that explicitly instead of a false negative verdict.

### Binding methodological decision — control-group size and outlier resistance

This decision was fixed on 2026-08-02 before the new control-group design matured, and is not relaxed after seeing results:

| Mature v3 controls | What is allowed |
|---:|---|
| <100 | No analysis, no inference |
| 100–299 | Safety check + exploratory v1 model and exploratory paper trading only |
| 300–499 | Serious interim analysis, but no final accept/reject and no approved training |
| **≥500** | The minimum for a Phase 1 gate decision |
| ≥1000 | Preferred if automated collection keeps running at no prohibitive operating cost |

And count alone is not enough. The core decision also requires:

- **≥14 v3 calendar days** spanning more than one market regime; a day-five read is diagnostic
  only. The duration must not be shortened just because the count was reached quickly.
- Matching/adjustment by network and time, and by asset-class, market-cap, and liquidity strata as much
  as possible. A source or network difference must not be attributed to the signal.
- Thousands of signals do not compensate for a control shortage: the smaller group is the bottleneck.
- The decision rests on **median and win rate and Mann–Whitney and effect size and confidence intervals**;
  the mean is shown for economics but does not decide on its own.
- A +4000% token is not deleted because it is real, but the report is re-run after excluding the single
  biggest winner and then the top 1% as a **declared sensitivity analysis**. If the verdict flips, the result
  is unstable and does not pass the gate. The core analysis stays all-inclusive and we do not pick the exclusion
  that suits us.
- Report results per day/week and network, not just the aggregate. Success must hold its direction across
  most periods, not be carried by one up day or one anomalous token.
- With repeated signals for a token, thousands of rows are not treated as independent samples. A token does not
  cross between train/test, and a token-level aggregate evaluation is shown alongside the row-level one.
- If the result is close or the confidence intervals are wide at 500, **continue to 1000** and
  do not issue a forced verdict. "Inconclusive" is a valid result.

### The decision

| Result | What to do |
|---|---|
| The signal clearly wins | Proceed to Phase 2 with confidence |
| Minimal/no difference | **Stop.** The model will not conjure an effect that does not exist. Look for a sub-classification (volume? market cap?) that wins internally, or rethink the thesis |
| The signal is worse | A valuable result in itself — maybe `large_buy` is an exit indicator, not an entry one |

**Write the result in a dated file whatever it is.** A negative result saves you from wasting weeks.

---

## Phase 2 — Building the dataset

The model learns from **rows**, not events. This phase turns the scattered archive into
a single training table with strict discipline — one mistake here silently poisons everything after it.

### 2.0 The extractor — one piece for training and live together

**Built and running (2026-07-30)**: `recorder/features.py` + `build_training_rows.py`
→ the `training_rows` table (**5,602 rows**, of which **1,371 trainable rows**:
meme + `status='ok'` + independent · train 947 / val 108 / test 277).

**Run manually**: `py classify_tokens.py` then `py build_training_rows.py`.
And **`--rebuild` is mandatory before any training** — a row is built once, and any later
backfill of past data (theses, constants, candles, suspect flags) changes feature values without
a rebuild.

The extractor walks every labeled outcome and asks: "what was known **at that moment** only?":

- **One row per decision**: `kind='signal'` (3,970) and `activity` (1,047) and `watch` (230),
  with the answer from `outcomes` attached by the same key. Window-decision rows (t0+Δ) are a separate
  kind — see §2.3-b (not built yet).
- **104 features in 7 families**, all via time-bounded queries (literally `<= t0`),
  with missing values as NULL (FR-007: no fabrication; LightGBM learns absence).
- **Dual discipline**: the same `build_features` computes the historical training row and later the live
  signal row — a computation difference between the two (train/serve skew) silently destroys the model,
  so there is one unit, not two copies.
- **Time-lock tests** (20 tests): each family has a test that plants data after
  t0 (a thesis, a candle, a market snapshot, a signal, a new token for the creator) and asserts the row does
  not change. Plus structural guards: no label column sneaks into features, and no feature named likes.

**Coverage measured on forward rows** (4,322 rows) — read before any training:

| Level | Features |
|---|---|
| 100% | Market cap/price/fdv · theses and their authors · macro (SOL/ETH) · density · **24h candle volume** · **symbol text** · **declared leaderboard count** · candle history and flat_ratio and distance-from-peak |
| 91–99% | Trade size and its ratio · **buyer average cost and price ratio to it** · **short market windows (1h/4h) and float ratio and pool turnover** · token name and decimals · last hour volume |
| 48–65% | **the real `token_social` family**: `thesis_total` (the actual count, reached 27,559) · `holder_authors` (owner authors) · momentum deltas between snapshots |
| 1–9% ⚠️ | leaderboard rank and match ratio (rare forward) · creator fingerprint |
| 0% ❌ | `mintable`/`freezable`/`top10_holders_pct` — **the source does not populate them** (not a defect on our side) |

**Four measured defects fixed in the 2026-07-30 audit** (each with a regression test):

1. **`dist_from_ath` poisoned**: the peak used to be computed from `h` without excluding `h_suspect`,
   giving −0.99999997 on 49 rows (a peak of 96,311 instead of 0.0143). Now zero rows below
   −0.999, and the minimum is −0.9957 (a real drawdown).
2. **A measured zero became unknown**: `size_usd or usd_amount` — zero is falsy, and 1,640
   events genuinely had a size of 0.0 (a full exit). The check is now explicit for None.
3. **A column meaning two things depending on row kind**: signal density used to come from `signal_events`
   alone ⇒ 88% zeros for retro versus 4% for forward, so the model inferred "row source" rather than
   token activity. It now comes from both sources (retro zeros 921→363). **A real era gap remains**
   (our archive is denser in 2026) ⇒ do not compare this feature across eras, and walk-forward
   bounds the effect.
4. **`thesis_before` saturated at 400**: `token_thesis` is a sample, not the truth. Now there is
   `thesis_counted` + a `thesis_counted_capped` flag (1,637 capped rows), plus the real
   `social_thesis_total` from a snapshot before t0 (2,949 rows).

**A measured lesson**: the coverage report exposed a real defect — `token_age_h` was 100% empty
because fomo stores `token_created_at` as a numeric **epoch**, not ISO, so the conversion failed silently.
⇒ Read the coverage report after every build; a 100%-empty column is either a source gap or a defect on our side,
and you cannot tell which without checking. And second: **a full column can still be wrong** — the rare
columns (buy/sell at 24%) were in use while the dense ones (change/volume at 100%) were neglected.

### 2.1 Row selection

```sql
SELECT o.* FROM outcomes o
  LEFT JOIN token_class t
    ON t.token_address = o.token_address AND t.network_id = o.network_id
 WHERE o.kind = 'signal' AND o.status = 'ok' AND o.is_independent = 1
   AND COALESCE(t.asset_class, 'meme') = 'meme';   -- mandatory, see below
```

- **`asset_class = 'meme'` is a mandatory condition** (settled 2026-07-30): the archive contains BTC
  and ETH and SOL and gold and tokenized stocks (AAPL/MSTR/HOOD/INTC/META/SNDK/MU) — 62 labeled non-meme
  outcomes. An Apple share does not behave like a two-hour-old coin, and mixing them makes the model learn
  "asset class" instead of the signal. The other classes stay recorded for separate analysis, not for deletion.
  (The measured effect on retro numbers: almost none — 5 rows out of 487; but the forward set is more
  contaminated: SOL alone has 22 outcomes.)
- `kind='signal'` — one row per signal (`watch` is for comparison, not training).
- `status='ok'` — exclude `no_entry` from **training** (a collection gap, not an outcome), but
  **keep it in the report**: its ratio is a collection-quality metric.
- **`no_bars` is not an exclusion** (settled 2026-07-28): the token died within minutes of
  entry — the worst possible outcome. Excluding it trains the model on a world where "everyone survived"
  (hidden survivorship bias). The rule: for upside targets (`up_2x_24h` and the like), `no_bars` enters
  with a hard **0**; for `is_rug` it is reported separately (liquidity death is all but a confirmed −100%
  but not price-proven) and rug is only counted with an actual `final_return ≤ −90%`.
  Only `no_entry` is excluded: no entry candle at all = no trade.
- **`suspect_bars > 0` is a warning, not an exclusion** (settled 2026-07-30): the computation already
  excluded impossible values, but the row may have lost its true peak (or `max_gain` may be missing entirely).
  Include it in training and include the very column as a quality feature, and do not
  build an individual claim on it ("this token rose X%") without checking its candles.
- `is_independent=1` — exclude pseudo-duplication (69% of repeats <5 minutes apart). Excluded
  rows stay in the database; they are not deleted.

### 2.2 The target — pick one and fix it

| Target | Definition | When it fits |
|---|---|---|
| **`up_2x_24h`** (proposed to start) | `max_gain_24h ≥ 1.0` | Clear binary classification, matches real trading behavior (sell at target) |
| `up_50pct_24h` | `max_gain_24h ≥ 0.5` | Larger positive class ⇒ easier learning |
| `final_return_48h` | Regression | Most economically honest but harder and noisier |
| `is_rug` | Classification | An "avoid the disaster" model — may be more commercially useful |

**Start with `up_2x_24h`.** Watch class balance: if the positive class drops below 5%, use a
lower threshold instead of complicated class weights.

⚠️ **Do not use `max_gain` as the sole target and treat it as profit**: the peak is only realized by
selling at that exact moment. Always add `final_return_48h` to the report.

### 2.3 Features — all from `signal_events` at t=0 only

**Strict rule: no feature from after the signal moment.** Every feature below was available at that moment.

**A. Trade size and buyer** (was missing, recovered):
`size_usd` · `in_amount` · `size_usd - in_amount` (prior position?) ·
`log(size_usd)` · `num_swaps` · `is_first_buy` · `buyer_pnl_pct` ·
`avg_cost` · `realized_pnl_usd`

**B. Buyer quality (the original thesis)**:
`buyers_best_rank` · `top_trader_match_count` · `are_top_traders` ·
`rank ≤ 10 / ≤ 50` as binary flags

**C. The token's market at that moment** (strongest measured signal so far):
`market_cap` · `fdv` · `log(market_cap)` · market-cap buckets ·
`price_usd` · `size_usd / market_cap` ← **the impact ratio, a strong candidate**

⚠️ **Measured constraints 2026-07-30 on this family specifically**:
- **Log or buckets mandatory**: the measured range is $10⁵ → $10¹³ (nine orders) — raw values let
  a single row dominate the split.
- **`liquidity` is a more honest candidate** than market cap: the latter is just price × supply,
  so a token with 7.8×10¹⁴ tokens reads as "$69 trillion" (SMILE) without a single real dollar in it.
  (Not corruption — the observations are consistent; trap #19.)
- **The `is_major` flag is mandatory**: 116 signals in the archive on BTC/ETH/SOL/USDT/XRP/BNB
  and a tokenized stock, of which 21 labeled outcomes (trap #18). Exclude or separate them — otherwise
  "market cap is the strongest indicator" might be an asset-mix effect, not a size effect.
  **Resolved**: `token_class.asset_class` (see §2.1) — and the check proved the gradient is
  real within memes alone, so the pattern survived the adjustment rather than falling to it.

**D. Signal momentum** (for `multi_user_buy`):
`unique_traders` · `num_trades` · `minutes` · `price_change_pct` · `total_volume`

**E. Token constants** from `token_static`:
`mintable` · `freezable` · `is_scam` · `launchpad_name` · `migrated` ·
`graduation_percent` · number of social links · token age at signal time

**F. Social momentum** — from `token_thesis` stamped with `created_at` only:
```sql
-- number of theses before the signal
SELECT COUNT(*) FROM token_thesis
WHERE token_address = :tok AND created_at <= :entry_iso;
-- and its acceleration: the last hour's ratio to the last 24 hours
```
⚠️ **Do not use `num_likes` as a historical feature** — its value is from pull time, not write time
(permanently lost for the past, see README §9). For the future, use `token_social` deltas.

**F-b. Thesis text classification (settled 2026-07-28)**: the text is fixed from the moment it is written ⇒
analyzing it later is a t=0 feature **valid retroactively with no leakage** — the only possible
retroactive enrichment. A small open local model (Qwen 2.5 3B / Gemma 2 2B via Ollama,
zero-shot with a strict JSON schema — no training, no cost, no data leaving). **Classification is
a type, not a polarity**: on fomo the writer is an owner (equity) ⇒ polarity is structurally
biased upward and uninformative; the classes: {substantive with numbers, empty shill/hype, news
catalyst, hesitation}. Shill density is a candidate **inverse indicator** (crowd hype = the top).
The process: manual validation on 50–100 theses to measure model agreement before the 30k batch,
then incrementally with inflow. Explicitly rejected: "searching the internet for the reason it pumped" —
leakage (the answer comes after the event) + API cost/compliance + latency (target median 0.2s);
at most a narrative tool for the dashboard later, never a feature.

**F-c. The "discovered late" fingerprint (direction-supported hypothesis 2026-07-28)**: the scenario
"a project with history that people discovered late, so FOMO started on it" breaks down into features:
age of the discussion before the signal (oldest thesis versus t0) · social silence then ignition ·
a long flat price before liftoff (§2.3-g) · `token_static` links (forward only) ·
the second classifier's axis {substance claim / meme hype} — it measures **narrative quality, not project
legitimacy** (every huckster claims utility; on a 48h horizon the credible narrative is the engine).
First cut (20% coverage, thin n): fresh mania win 20.8%/median −50% versus
"30+ days" win 33%/median −34% and zero rugs — **a direction, not a verdict**. To enable it:
extend `backfill_thesis` to the retro 408 tokens then re-cut at sufficient volume.

**G. Price path before the signal** — from `token_bars` (goes back months before t=0, free):
1h/4h/24h returns **before** the signal · realized volatility · distance from the all-time high ·
number of pullbacks. A token up +300% before the signal arrives is not a wiped-out token — and this may be
the most valuable thing in all the features: "momentum before the leaderboard trader entered" versus
"the momentum the leaderboard trader himself brought". Derived from candles stamped ≤ t=0 only
(the same strict rule).

**H. Creator fingerprint** — from `token_static.creator_address` with an internal join:
how many other tokens in your data have the same creator? Age of their first token? A serial creator
(several launches within days) is the classic rug pattern.

**I. Market context** (keeps the model from memorizing "an up day"):
hourly **SOL/WETH/WBTC candles** recorded in `token_bars` (resolution='60') since
2026-07-28 — the reference return over 4h/24h before the signal is the clean context. Complemented by:
number of signals in the previous hour · average return of watched tokens in the same hour ·
hour of day UTC.

**J. Leaderboard trader trajectory** — from the hourly leaderboard archive
(`snapshots` with source `leaderboard`, starting 2026-07-28): the buyer's rank rise/fall over the
previous week ("a rising leaderboard trader" versus "a burned one"). **For the past before that date
there is no archive** — do not fabricate it.

**K. The sell side** (for an exit model inside the window, not an entry model — see §4):
`multi_user_sell` and `large_sell` events (recorded since 2026-07-28) are labeled like the others
once candles are available: "a whale/leaderboard trader dumped → what happened to the price". Inside the
window: the `sell_count_*`/`unique_sells_*` series from `market_ticks`, and the `thesis_total`
path from `token_social` (momentum dying out). All of it is the future relative to t=0 — **forbidden
as an entry model**, legitimate for an exit model with a sliding time.

### 2.3-b The confirmation window — the staged entry decision (measured 2026-07-28)

**The idea**: the rule is not "decide at t=0" but "do not see past the moment of your decision". The
entry decision may shift to t=+Δ: at that point everything in [t0, t0+Δ] is legitimate history —
**no leakage**, rather a two-stage model: the signal filters, and the window confirms or refutes.

**Measured evidence on the retro set (453 independent trades)**:

| | Median price drift in the first 30 minutes |
|---|---|
| Winners (48h return > 0) | **+3.6%** (p75 +33.2%) |
| Losers | **−9.1%** (p25 −33.2%) |

A 12.7-point gap ⇒ early price movement is a real confirmation feature. And the median cost of waiting is
**negative** (−6.7%): you enter cheaper because the median bleeds right after the signal — the cost
concentrates in the fast winners' tail (+33%). Caveat: the two distributions overlap (a quarter of
winners stumble first) ⇒ no blind threshold; the soft version is the model's job.

**The window feature block** (for the decision at t0+Δ — do not mix with the t=0 block):

- Price drift at 15/30/60 minutes (from `token_bars`)
- Number and types of subsequent signals on the same token within the window (`signal_events`)
- Leaderboard trader activity inside it: new buys/sells (`buyers_best_rank` for later events)
- Thesis acceleration (the `token_social` path) — **forward only**: no retro series
- The liquidity/holders path (`market_ticks` inside the window) — forward only

**The discipline**: two rows per decision, not one — the t=0 decision is built from the t=0 block alone,
and the t0+Δ decision is built from both blocks. Mixing them into one row = disguised leakage. And the same
extractor works live: a signal arrives ⇒ a t0 row; after Δ ⇒ a window row by literally the same
logic (no train/serve skew).

**Each decision gets its answer from its own price (the fair-comparison condition)**: the question
"immediate or confirmed?" can only be learned if each row has its own answer computed from its own entry
price: the immediate decision's answer = `compute_labels(bars, entry_ts=t0)`, and the confirmation
decision's answer = `compute_labels(bars, entry_ts=t0+Δ)` over the same evaluation horizon.
Comparing the confirmation decision to an answer computed from the t0 price ignores that the price moved
during Δ — which is the very cost of waiting (the +33% tail on the fast winner). `compute_labels`
accepts entry_ts of any kind ⇒ the structure is ready, and what is needed is a second outcomes row with a
distinct decision key (`key = id + ":d30"` for example). Then the model learns the comparison per
individual signal — not a blanket verdict: "immediate" for signals whose signature is fast (huge size + high
rank + already-burning momentum), "confirmed" for the rest, "skip" when both values are negative.

**Measuring the maturity threshold for the staged decision**: its samples are forward only (no retro for
in-window social) ⇒ it lags behind the t=0 model; build the extractor with both blocks from day one so
samples accumulate automatically.

**The decision-point ladder (settled 2026-07-28 on the measured curve, 466 trades)**:
**t0 / +10m / +30m** — three rungs and no more:

- +5m is noise (a single candle; a meaningless +2.7 gap).
- +10m is quick confirmation: a +7.8 gap (64% of winners positive / 39% of losers) at a −1.7% cost.
- +15m adds nothing over +10m (+6.8) — dropped.
- +30m is the strongest measured separation: a **+18.0** gap (64%/34%) and a negative median cost (−7.7%).
- +60m is deferred: not yet measured precisely; a fourth rung is added **with evidence**, not expanded
  by intuition. The prohibition is methodological: many points = near-duplicate correlated decisions
  (overfitting on timing) + the "best Δ in the sample" trap (the same test-shopping boat).

### 2.4 The split — do not touch it

`outcomes.split` is computed **by token-address hashing** (70/10/20) and is binding:

- **Why by token**: 14.2 signals per token. A random split puts the same token in
  training and test ⇒ the model memorizes tokens and gives spuriously high accuracy.
- **The time barrier is mandatory, not optional** (settled 2026-07-28): splitting by token
  prevents memorization leakage but **does not prevent mixing market regimes** — 1,000 samples accumulate
  over weeks while the meme meta flips within days, so train and test come from the same regime and the
  accuracy is structurally optimistic. The rule: final evaluation is **walk-forward**:
  train on the earliest period, test on the latest, and repeat with a rolling window. The by-token
  `split` stays as an anti-leakage layer inside each time window.
- **Never re-split randomly**, and do not try several splits and pick the best one.

---

## Phase 3 — Baseline model

> **Before any exploratory or approved training — two mandatory steps, in this order**:
> ```powershell
> cd recorder
> py classify_tokens.py            # update asset_class (the mandatory training filter, §2.1)
> py build_training_rows.py --rebuild   # ~6 minutes
> ```
> **Why `--rebuild` and not incremental build**: a row is built once (idempotent
> by key), and its features derive from tables that keep receiving data **about the past** after it is built —
> thesis backfill, token constants, retro candles, suspect-flag recomputation, new asset
> classification. Training on stale rows = training on features poorer than what you actually have, and
> worse: inconsistency between rows built at different times (the same feature computed with different
> information) — noise that shows up in no metric.

### 3.1 The baselines you must beat first

**Do not compare your model to zero; compare it to these.** A model that does not beat them has no value:

1. **Chance**: always predict the majority class.
2. **Market cap alone**: a one-variable rule (`market_cap > 10M`) — the strongest
   measured pattern so far, and it is easy for a sophisticated model to fail to beat it. **Measured within
   memes alone 2026-07-30** (481 retro samples, a clean monotonic gradient):
   <$1M win 10.4%/rug 33.3% · $1–10M win 17.9% · $10–100M win 24.7% ·
   >$100M win **39.1%**/rug **0%**. A model that does not beat this gradient has no value.
3. **Volume alone**: `size_usd > threshold`.
4. **The control group**: does your model on signals beat random selection?
5. **Playing blind (measured 2026-07-28 on the retro set)**: "play every
   `multi_user_buy` signal" = median −58.7% holding / median +8% with a +10% take-profit but a mean
   of −3.7% and roughly 0% net cost. **This is the numeric floor**: a model that filters signals
   must lift the mean above zero after costs, not merely "improve the median" — the rare catastrophic
   failure is what eats the mean, so real filtering is avoiding dead trades, not catching peaks.

### 3.2 The model

- **Start with Gradient Boosting** (LightGBM/XGBoost): suited to tabular data, swallows missing
  values (you have many, deliberately), and needs no normalization.
- **Logistic regression** as an interpretable reference alongside it.
- **No neural networks** at n≈1000 — no benefit, and overfitting hides errors.
- Tune parameters on **`val` only**. `test` is opened **once**, at the end.

### 3.2-b The architecture around the algorithm (settled 2026-07-29)

The algorithm's name is a ~5% decision; the other 95% is what surrounds it:

1. **Unified walk-forward bake-off**: LightGBM versus CatBoost (categoricals:
   launchpad/network) versus Logistic (a detector — if GBDT does not beat it by a margin, the features
   are broken). Same split and metrics, the winner is adopted, and for ties: the faster one.
2. **Two models, not one**: `P(rug)` for disaster avoidance + the opportunity model (classification/regression)
   — the loss structure is asymmetric (rug −100% versus a profit cut at target).
3. **A tail-resistant objective**: quantile (median/quantile) or Huber — a single anomalous winner
   does not drive training. Measured against the plain objective in the same bake-off.
4. **LambdaRank optional** versus classification — it matches the actual use (pick top K).
5. **SHAP every round** — decision interpretation + hidden-leak detection (a feature suddenly topping?).
6. **Rejected for recorded reasons**: deep networks and sequence models (small n = memorization),
   ARIMA/Prophet (a different problem: we are ranking events, not forecasting a series), stacking
   (the meta-model overfits at small n — a simple average of variables is safer).
   The thesis classifier (§2.3-f-b) is a **feature producer** feeding GBDT, not a rival to it.

### 3.3 Metrics

| Metric | Why |
|---|---|
| **PR-AUC** | The class is imbalanced — ROC-AUC is misleadingly optimistic |
| **Precision@K** | What actually matters: of the top 10 recommendations, how many are right |
| **Calibration** | "70% probability" must actually mean 70% |
| Mean and median return of the selected | Economic translation |
| `is_rug` rate among the selected | Disaster avoidance |

---

## Phase 4 — Paper trading

A statistically good model can still lose money. This phase finds that out **without money**.

### 4.1 What must be simulated explicitly

This is the gap between "model accuracy" and "profit", and ignoring it is the most common mistake:

| Factor | Why it matters |
|---|---|
| **Slippage** | A token with market cap <1M: buying $5k moves the price by a meaningful percentage |
| **Fees** | fomo fees + network fees per trade |
| **Execution delay** | `entry_lag_s` is recorded — use it, not an idealized assumption |
| **Liquidity** | `liquidity` from `market_ticks`; do not assume any size is executable |
| **Exit rule** | The peak is not known in advance — define an explicit rule |

### 4.2 Exit rules — an open question, and the tool is ready

**The tool** is built and tested (`recorder/exit_sim.py` + `run_exit_sim.py`, 18
tests) so do not write a new simulator. **The rules themselves are not settled yet** — no rule is
recommended here, and choosing among them is part of this phase, not an input to it.

**Newly documented candidate (2026-07-28) — momentum exit**: the measured evidence on
MarsCoin (+1962% peak) is that fixed rules crush the right tail (a +10% take-profit exited after
24 minutes and missed +1900%; a 25% trailing stop exited at −21.8% on a candle wick and then it rose 20×).
Meanwhile the `token_social` series (8→393 theses) and leaderboard selling (`large_sell` since
2026-07-28) are recorded live. **The hypothesis**: exiting on momentum death (theses dying out /
accelerating, or a leaderboard sell inside the window) beats fixed numbers because it stays in
the tail and exits the median. Test it on the retro set with the same simulator: add "time since last
thesis/leaderboard sell" rules and compare — on the condition that their features are built only from what
was available at the moment (no leakage: `token_social` during the window is legitimate for the
sliding-time exit, forbidden for the entry model).

```powershell
py run_exit_sim.py --cost 0.02
```

**An open question, not a result**: does the exit matter more than token selection?

An early exploratory run (a few dozen trades, truncated follow-through, **not a single completed
window and no control comparison**) suggested fixed take-profit beats holding.
**This is a preliminary signal, not a result**, and no decisions are built on it: the sample is below the
maturity threshold, the windows are truncated so far targets are structurally understated, and the
difference might be a property of those hours' market rather than of the strategy.

**When it becomes answerable**: after Phase 0 (completed windows) and Phase 1
(a mature control group). Then run the simulator on both groups and compare — if fixed take-profit also
beats the control group, the effect is real; otherwise it is a market property.

A measured reference worth attention when designing: the true median time to peak is **11.4
hours** and **47% peak after 12 hours** — so any time limit below that cuts off half
the gains, and dodges half the collapses. The net is not known in advance.

#### The decisive test: the breakeven point

`breakeven_cost` computes the maximum round-trip cost at which the rule still stays profitable. The current
result is only **1.9% – 3.2%**, and **a third of tokens have liquidity under $50k**. Any rule whose
breakeven is below ~3% is not practically executable however pretty it looks.

**This number — not accuracy and not PR-AUC — is what decides the project's viability.**

#### What you must know before trusting the numbers

- **The path is the result**: `max_gain` and `max_drawdown` carry no ordering, so the `outcomes` columns
  are not enough — that is why the simulator walks the candles.
- **The intra-candle assumption is conservative**: when target and stop are touched together, we assume the stop first.
- **The windows are not complete yet** ⇒ far targets are understated. Re-run after Phase 0.
- **No control comparison yet** — a random token might give the same result (Phase 1).

### 4.3 Portfolio metrics

Cumulative return · **max drawdown** · Sharpe/Sortino ·
win rate · average winner ÷ average loser · longest losing streak ·
result sensitivity to trade size

### 4.4 Silent live operation

After the historical simulation succeeds: run the model **live without money** for at least two weeks,
and log its decisions in real time. This exposes hidden leakage that a historical simulation never can.

---

## Phase 5 — Retraining and monitoring

The market changes; a model trained on one week goes stale.

- **Data drift**: monitor the feature distribution monthly against training.
- **Performance drift**: monitor PR-AUC on new data.
- **Retraining**: periodically (monthly) or when a drift threshold is crossed.
- **Version keeping**: every model with its training data and results. Without that, there is no way to know
  why performance degraded.
- **Keeping the archive**: `SNAPSHOT_RETENTION_DAYS = 0` (no deletion). Do not enable
  pruning until the disk is genuinely tight — old data is what allows re-evaluation.

---

## Trap list — review it at every step

Ordered by potential damage.

| # | Trap | Prevention |
|---|---|---|
| 1 | **Future leakage** | No feature from after t=0. The labeler is separate and does not run before maturity |
| 2 | **Leakage through the token** | Respect `outcomes.split` (hashed by token) — no random split |
| 3 | **Pseudo-duplication** | Train on `is_independent=1` only |
| 4 | **Survivorship bias** | `bars_truncated=1` is **a signal, not a gap**; do not exclude `is_rug` |
| 5 | **Right truncation** | Do not infer from incomplete windows (it cost us a false inference) |
| 6 | **The misleading mean** | Always show the median; one anomalous winner flips the mean by itself |
| 7 | **Peak ≠ profit** | `max_gain` is only realized by selling at that moment |
| 8 | **Memorizing market cap** | Adjust for it or stratify within it — the strongest measured pattern |
| 9 | **Test shopping** | `test` is opened once. Tuning on `val` |
| 10 | **Ignoring slippage** | Simulate cost explicitly — it eats small profits |
| 11 | **One day ≠ the market** | ≥5 calendar days before any inference |
| 12 | **`num_likes` historically** | Its value is from pull time, not write time — forbidden as a past feature |
| 13 | **A frozen source** | Check `last_feed_event_at`; a frozen source looks like a quiet market |
| 14 | **Excluding `no_bars` from training** | Hidden survivorship bias — enters with a 0 for upside targets (§2.1) |
| 15 | **train/test from the same market regime** | Splitting by token is not enough — walk-forward is mandatory (§2.4) |
| 16 | **"No difference" without statistical power** | Fix the smallest effect and compute the needed n before Phase 1 |
| 17 | **Impossible source values** | `h/l/c_suspect` are excluded from every computation; a row with `suspect_bars>0` is read with care (README §9-22) |
| 18 | **The universe is not memes only** | Measured 2026-07-30: **116 signals on 8 major assets** (BTC $1.27T · ETH · SOL 103 signals · USDT · XRP · BNB · HYPE · the tokenized SNDK stock), of which **21 labeled outcomes**. A trillion-dollar asset does not behave like a $500k token ⇒ either exclude them explicitly or make a market-cap stratum mandatory in evaluation. **Warning**: the "market cap is the strongest indicator" pattern may be partly an effect of this mix, not a size effect within memes |
| 19 | **`market_cap` is a misleading measure in the tail** | Measured: it is merely price × supply, so a token with 7.8×10¹⁴ tokens reads as **$69 trillion** (SMILE). **Not corruption**: the same token's observations are consistent across sources (the check: no token varies >×50 except with real price movement — BONK and MarsCoin). The fix is featural, not labeling: log or rank buckets, and preferring `liquidity` (real money in the pool) over nominal market cap |

---

## Success and stopping criteria

### Phase 1 success
After ≥500 mature v3 controls and ≥14 days: the signal wins by a meaningful margin in median
and win rate, with acceptable confidence intervals and effect size, and the verdict does not flip in the outlier
analysis or across periods and networks. Reaching 100 controls does not achieve this success.

### Phase 3 success
The model beats **all four** baselines on `test` opened once.

### Phase 4 success
Positive profit after slippage and fees, with a tolerable max drawdown, and stable across multiple
exit rules (not dependent on one over-tuned rule).

### When to stop
- Phase 1 shows no edge ⇒ the thesis is rejected as is.
- The model does not beat "market cap alone" ⇒ no added value for the complexity.
- Profit is positive before costs and negative after ⇒ the effect is real but not investable
  at this size.

**A documented negative result is a project success** — it cost you weeks instead of money.

---

## Order summary

```
0. Maturity gate          ⏳ Signals complete; the control group 426/500
2. Dataset build          ✅ Built and running: training_rows · fv16 · 171 features
1. Signal vs control      ⏳ v3 collection; the preliminary check is open, the admission decision at 500
3. Baseline model         ⏳ exploratory v1 at 100; approved v2 after the Phase 1 gate
4. Paper trading          ⏳ exploratory v1, then a separate v2 after the decision
5. Monitoring & training  ← drift is inevitable
```

**Why Phase 2 ran before Phase 1**: the extractor does not wait on data (pure code over an
existing archive), and the control group matures by time alone — so building it during the wait saved days
and did not violate the methodological order: Phase 1 remains the **admission gate** for everything after it.
The exploratory v1 and the paper run at 100 are allowed under the constraints above, and v2 is not approved
before passing it.
