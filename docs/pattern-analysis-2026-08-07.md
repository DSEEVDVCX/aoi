# Signal Pattern Analysis — 2026-08-07

> **A superseded snapshot.** [`pattern-analysis-2026-08-08.md`](pattern-analysis-2026-08-08.md)
> came after it, so read that instead. This one is kept so the two snapshots can be compared, not to be
> read alone. For current numbers: `py recorder/pattern_analysis.py`. For the binding thresholds:
> [`PLAN.md`](PLAN.md).

> Created `2026-08-07T14:21:04+00:00` from `C:\Users\rr\Desktop\aoi\recorder\recorder.db`. It covers 4,215 valid independent signals,
> 322 tokens, from `2026-07-28T10:31:01+00:00` to `2026-08-05T13:31:26+00:00`.
> Thresholds are derived from `train` only, and the split is fixed by token address to prevent a token crossing between parts.

## Summary

- **There is no proof that the general signal beats the control group.** After removing duplicates, 89
  tokens with signals remained versus only 10 live controls. The 48-hour return median difference is
  -0.0%, with a confidence interval of
  [-6.6%,
  +20.2%], and `p=0.252`.
- **The strongest upside pattern is momentum continuation:** `ret_24h_before` > 64.9% and `vol_24h_before` > 3.3%.
  In the test set it touched +20% within 24 hours in **79.2%** of
  101 signals across 21 tokens, versus
  47.0% for the whole test set. The direction of improvement held on every qualifying day.
- **This is not a holding pattern.** The pattern's median return at the end of 48 hours is
  -29.6% and the median drawdown is
  -57.5%; the typical move is a spike followed by a reversal/dump.
- In a conservative simulation: +20% target, −30% stop, 24-hour exit, and 2% round-trip cost, the momentum pattern
  hit the target in **70.3%** and was stopped out in
  **28.7%**; the net mean was
  **+3.2%** and the median
  **+18.0%** on the test set. But it produced a negative mean
  in training, so it is not adopted as a complete financial strategy.
- No stable pattern was found for a final +20% rise after 48 hours. Prior calm raised the probability of merely
  closing positive to 51.7%, but the median return was only
  +0.2% and the rate of closing above +20% was
  13.6%. The confidence interval for the positive-close difference versus the rest of the test set is
  [+11.2%, +38.8%], but
  the return size itself is tiny, meaning fees and slippage likely erase the edge.

## Patterns for reaching +20% within 24 hours

Baseline: train 53.4%, val
59.2%, test 47.0%.
The `rows/tokens` column is for the test set. The confidence interval is the rate difference versus the rest of the
test set, bootstrapped at the token level, not the row level.

| Pattern | Rule fixed from train | train | val | test | rows/tokens | Day stability | 95% CI of the difference |
|---|---|---:|---:|---:|---:|---:|---:|
| Strong momentum with realized volatility | `ret_24h_before` > 64.9% and `vol_24h_before` > 3.3% | 73.9% | 77.4% | 79.2% | 101 / 21 | + 9/9 | [+20.8%, +53.7%] |
| Relatively old token | `token_age_h` > 37.4 days | 33.8% | 23.9% | 19.9% | 246 / 23 | − 9/9 | [-53.0%, -22.0%] |
| Weak turnover relative to liquidity | `volume_to_liquidity` <= 2.27 | 23.0% | 31.8% | 25.8% | 89 / 21 | − 9/9 | [-43.9%, -6.3%] |
| Weak prior volatility | `vol_24h_before` <= 1.7% | 28.5% | 43.6% | 24.7% | 259 / 28 | − 9/9 | [-47.1%, -13.3%] |
| Few daily transactions | `tick_txn_24h` <= 2,646 | 34.2% | 39.8% | 22.9% | 96 / 22 | − 9/9 | [-45.7%, -11.2%] |

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
| train | 3028 | -0.7% | 449 / 87 | 60.6% | 37.9% | -1.4% | +18.0% |
| val | 444 | +0.2% | 93 / 11 | 68.8% | 30.1% | +2.5% | +18.0% |
| test | 743 | -2.5% | 101 / 21 | 70.3% | 28.7% | +3.2% | +18.0% |

On unseen tokens, the confidence interval for the pattern's net mean outperforming the rest of the
test set was [+0.1%,
+13.2%]. The maximum round-trip cost
at which the **test mean** stays non-negative is roughly
5.2%, but it is unstable: in training it was
only 0.6%.

## What does not count as evidence

Event type alone does not distinguish upside in the test set:

| Type | Rows | Tokens | Reached +20% |
|---|---:|---:|---:|
| `large_buy` | 373 | 65 | 48.0% |
| `large_sell` | 370 | 57 | 45.9% |

Likewise, a leaderboard-trader match or the trade being a first buy did not show a stable effect stronger
than the market. And the following fields are 100% empty in the sample, so they were barred from any inference:
`unique_traders, num_trades, minutes, price_change_pct, total_volume, volume_per_trader, are_top_traders, top_trader_match_ratio, mintable, freezable, thesis_accel, top10_holders_pct`.

## Limits of inference and the next step

- The period is short (9 days) and the meme market changes regime quickly.
- The qualifying live control group is small and network-unbalanced; therefore there is no causal verdict that the
  signal itself creates an edge over picking a similar random token.
- `val` was used to choose the formulation, then `test` was examined in this report; **the current test set
  is now consumed** and the rules may not be modified and renamed an independent test.
- Freeze the rules above now, collect 7–14 new days, then test them over time without changes.
  A proposed acceptance criterion for the momentum pattern: ≥100 signals and ≥30 new tokens, the target rate staying above
  baseline, a token-aggregated confidence interval above zero, and a positive net mean at a 2–5% cost.

This is a historical probabilistic analysis, not a price guarantee and not investment advice.
