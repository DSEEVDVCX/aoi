# Signal Pattern Analysis — 2026-08-08

> **A snapshot, not a reference.** This file holds the numbers of its publication day only; the database
> grows every minute, so every number here is smaller than today's reality. It is the latest analysis of
> this kind, and latest does not mean current: regenerate with `py recorder/pattern_analysis.py` before
> relying on any number from it. For the binding thresholds: [`PLAN.md`](PLAN.md).

> Created `2026-08-08T11:52:46+00:00` from `C:\Users\rr\Desktop\aoi\recorder\recorder.db`. It covers 4,097 valid independent signals,
> 346 tokens, from `2026-07-28T10:31:01+00:00` to `2026-08-06T00:35:28+00:00`.
> Thresholds are derived from `train` only, and the split is fixed by token address to prevent a token crossing between parts.

## Summary

- **There is no proof that the general signal beats the control group.** After removing duplicates, 97
  tokens with signals remained versus only 10 live controls. The 48-hour return median difference is
  -1.3%, with a confidence interval of
  [-8.4%,
  +19.2%], and `p=0.332`.
- **The strongest upside pattern is momentum continuation:** `ret_24h_before` > 48.5% and `vol_24h_before` > 3.3%.
  In the test set it touched +20% within 24 hours in **74.8%** of
  119 signals across 26 tokens, versus
  46.8% for the whole test set. The direction of improvement held on every qualifying day.
- **This is not a holding pattern.** The pattern's median return at the end of 48 hours is
  -28.2% and the median drawdown is
  -56.5%; the typical move is a spike followed by a reversal/dump.
- In a conservative simulation: +20% target, −30% stop, 24-hour exit, and 2% round-trip cost, the momentum pattern
  hit the target in **67.2%** and was stopped out in
  **29.4%**; the net mean was
  **+1.9%** and the median
  **+18.0%** on the test set. But it produced a negative mean
  in training, so it is not adopted as a complete financial strategy.
- No stable pattern was found for a final +20% rise after 48 hours. Prior calm raised the probability of merely
  closing positive to 53.9%, but the median return was only
  +0.6% and the rate of closing above +20% was
  13.8%. The confidence interval for the positive-close difference versus the rest of the test set is
  [+8.2%, +39.4%], but
  the return size itself is tiny, meaning fees and slippage likely erase the edge.
- **The machine-learning trial (GBM + walk-forward) failed to generalize to new
  tokens**: AUC ≈ 0.5 on tokens never seen in training, and a negative net mean
  for the top 20% on 3 of 4 test days. The apparent edge over all signals comes from
  previously known tokens (successful repetition), not new-token discovery. The
  systematic training pipeline is ready (see the "machine-learning trial" section), and it
  includes a mandatory leakage check — after a derived-target leak produced a spurious 100% in the top 20%.

## Patterns for reaching +20% within 24 hours

Baseline: train 52.3%, val
58.4%, test 46.8%.
The `rows/tokens` column is for the test set. The confidence interval is the rate difference versus the rest of the
test set, bootstrapped at the token level, not the row level.

| Pattern | Rule fixed from train | train | val | test | rows/tokens | Day stability | 95% CI of the difference |
|---|---|---:|---:|---:|---:|---:|---:|
| Strong momentum with realized volatility | `ret_24h_before` > 48.5% and `vol_24h_before` > 3.3% | 67.9% | 75.8% | 74.8% | 119 / 26 | + 9/9 | [+21.0%, +48.3%] |
| Relatively old token | `token_age_h` > 41.3 days | 31.4% | 22.7% | 18.8% | 239 / 24 | − 9/9 | [-54.8%, -22.8%] |
| Weak turnover relative to liquidity | `volume_to_liquidity` <= 2.18 | 24.5% | 28.4% | 24.4% | 86 / 21 | − 9/9 | [-43.9%, -6.8%] |
| Weak prior volatility | `vol_24h_before` <= 1.7% | 28.2% | 41.4% | 23.4% | 244 / 29 | − 9/9 | [-48.7%, -11.9%] |
| Few daily transactions | `tick_txn_24h` <= 2,535 | 35.6% | 37.5% | 21.3% | 94 / 22 | − 9/9 | [-44.1%, -12.4%] |

Practical interpretation:

- High momentum with real volatility is a **short-spike filter**.
- Age above the training threshold, or weak volume/liquidity turnover, or weak volatility, or few
  transactions are **low-upside noise filters**. Not necessarily collapses; many of them
  move near zero, meaning the signal adds no movement worth the risk.

## Price-path simulation of the strongest pattern

The simulation walks the candles in order. If the price touches the target and the stop within the same
candle, it assumes the stop first. The assumed cost is 2% per round trip, and there is no separate model for
execution rejection.

| Part | All signals | Their net mean | Momentum pattern rows/tokens | Target | Stop | Pattern mean | Pattern median |
|---|---:|---:|---:|---:|---:|---:|---:|
| train | 2905 | -0.9% | 467 / 97 | 53.5% | 43.9% | -4.7% | +18.0% |
| val | 442 | +0.5% | 99 / 14 | 68.7% | 28.3% | +2.6% | +18.0% |
| test | 750 | -2.8% | 119 / 26 | 67.2% | 29.4% | +1.9% | +18.0% |

On unseen tokens, the confidence interval for the pattern's net mean outperforming the rest of the
test set was [+0.3%,
+11.0%]. The maximum round-trip cost
at which the **test mean** stays non-negative is roughly
3.9%, but it is unstable: in training it was
only -2.7%.

## Early price path (15/30/60 minutes) does not separate the real moonshot from the fake one

Definition of a moonshot here: touched +100% within 48 hours. A "real" moonshot
closes above +100% (197 signals); a fake one touches it and then collapses (578). The question: does
keeping the price above the entry price after 15/30/60 minutes tell them apart early? The answer from
`test` (out of training): **no, and the difference flips**.

| Check point | Real above entry | Fake above entry | Difference |
|---|---:|---:|---:|
| After 15 minutes | 58.8% | 53.1% | +5.7 points |
| After 30 minutes | 47.1% | 52.1% | −5.0 points |
| After 60 minutes | 47.1% | 54.2% | −7.1 points |

A delayed-entry simulation (buying at the first candle after 15/30/60 minutes, only if the price is
above the signal price) did not improve reaching +100% — it shrank the capture of real moonshots:

| Entry | Trades | Touched +100% | Real moonshot captured |
|---|---:|---:|---:|
| Immediate (all signals) | 877 | 13.0% | 17/17 |
| +60 minutes, conditionally above entry | 378 | 9.8% | 8/17 |
| Immediate with score≥4 filter | 59 | 23.7% | 3/3 |
| +60 minutes conditional, score≥4 | 25 | 4.0% | 0/3 |

The reason: the "above entry" condition forces you to buy at a higher price, and a real moonshot usually
advances in a single spike that cannot be chased after an hour. Any price "confirmation" within the first
hour adds no out-of-training information.

## What does not count as evidence

Event type alone does not distinguish upside in the test set:

| Type | Rows | Tokens | Reached +20% |
|---|---:|---:|---:|
| `large_buy` | 371 | 70 | 47.7% |
| `large_sell` | 379 | 57 | 45.9% |

Likewise, a leaderboard-trader match or the trade being a first buy did not show a stable effect stronger
than the market. And the following fields are 100% empty in the sample, so they were barred from any inference:
`unique_traders, num_trades, minutes, price_change_pct, total_volume, volume_per_trader, are_top_traders, top_trader_match_ratio, mintable, freezable, thesis_accel, top10_holders_pct`.

## Machine-learning trial (Gradient Boosting + Walk-Forward)

A full training pipeline was built as the foundation for the final model: a target from the **actual exit
simulation** (+20% take-profit / −30% stop / ≤24 hours / 2% cost — the trade is net profitable) instead of a
bare touch, **time-based walk-forward** evaluation (train on everything before the day, test one day,
4 test days), 69 instantaneous features, and `HistGradientBoostingClassifier` with parameters
chosen on a single val day only.

**The most important methodological lesson:** the derived target column (`max_gain_48h >= 20%`) later
leaked into the feature set in a draft of the analysis, producing a "great" AUC (0.83–0.85) and 100% in
the top 20%. When the leak was removed, everything returned to reality — any test showing an extreme
edge over the entry price must be checked immediately for leakage.

**The honest result (no leakage):**

| Day | AUC (all signals) | AUC (new tokens only) |
|---|---:|---:|
| 20667 | 0.592 | 0.610 |
| 20668 | 0.507 | 0.474 |
| 20669 | 0.578 | 0.422 |
| 20670 | 0.609 | 0.646 |

- On **all signals**: AUC ≈ 0.58 — a weak, unstable edge (one day at 0.51).
- On **new tokens only** (never seen in training — the fair test of token
  discovery): AUC ≈ 0.54, and the net mean of the top 20% is negative on 3 of 4 days
  (−5.8%, −12.2%, −14.9%). **There is no generalizable edge.**
- The weak edge on "all signals" comes from previously known tokens
  (`prior_signals_token`), and it is unstable: on day 20668 the model was worse than
  baseline. Relying on it is a repetition strategy, not discovery.

**The recommended final training methodology (ready to apply as soon as longer data is available):**
time-based walk-forward + preventing token crossing between parts + a target from the exit simulation +
measuring AUC on new tokens only + a mandatory leakage check before any inference.

### Final result of the official training pipeline (`recorder/train_pipeline.py`)

It was rebuilt as a permanent file in the project with the same methodology (walk-forward, 107 features,
token separation, token-level bootstrap). The approved result on the four test days
(2007 signals, 491 new tokens):

- **Net-profit target** (`win_trade`): new-token AUC = **0.555**
  (95% CI: 0.504–0.595) — a marginal edge barely touching zero.
- **Touch +20% target** (`up20`): a high AUC (0.71) appeared in an early draft, but
  hard verification revealed it does not translate into profit: the top 20% on new tokens gave a net mean
  of **−6.5%** versus −5.4% for the baseline, and the same negative result at every
  cost level of 2–5%. Any edge measured on a bare target (a touch) without an exit
  simulation is not profit.
- The only stable positive edge in any draft appeared on "all signals"
  (including known ones), and most of it vanished when new tokens were isolated — **its source is
  repeating previously successful tokens, not new-token discovery.**

The final verdict: at this data volume, the model **does not generalize** to tokens
never seen in training; the pipeline is ready as a strict accept/reject tool for every future training round,
and its acceptance criterion is: new-token AUC with a token-level confidence interval away from 0.5
and net profit positive after cost.

## Limits of inference and the next step

- The period is short (10 days) and the meme market changes regime quickly.
- `model_training_rows` was fixed as of the report date: concurrent signals (same token + network + moment)
  were being counted multiple times (498 excess rows). The view now keeps only the smallest `key` per
  (token, network, moment). All numbers in this report come from the clean version.
- The qualifying live control group is small and network-unbalanced; therefore there is no causal verdict that the
  signal itself creates an edge over picking a similar random token.
- `val` was used to choose the formulation, then `test` was examined in this report; **the current test set
  is now consumed** and the rules may not be modified and renamed an independent test.
- Freeze the rules above now, collect 7–14 new days, then test them over time without changes.
  A proposed acceptance criterion for the momentum pattern: ≥100 signals and ≥30 new tokens, the target rate staying above
  baseline, a token-aggregated confidence interval above zero, and a positive net mean at a 2–5% cost.

This is a historical probabilistic analysis, not a price guarantee and not investment advice.
