# Data Readiness Report

Generated: `2026-08-20T15:22:31.242377+00:00`
Database: `C:\Users\rr\Desktop\aoi\recorder\recorder.db` (17493483520 bytes, `ro`)

## Sources

| source | status | rows | latest | age seconds | states |
|---|---:|---:|---|---:|---|
| feed | ok | 23585 | 2026-08-20T15:21:40.770153+00:00 | 50.472 |  |
| trending | ok | 23565 | 2026-08-20T15:21:40.770153+00:00 | 50.472 |  |
| verified | ok | 23561 | 2026-08-20T15:21:40.770153+00:00 | 50.472 |  |
| most_held | ok | 8076 | 2026-08-20T15:21:40.770153+00:00 | 50.472 |  |
| market | ok | 3396964 | 2026-08-20T15:21:40.770153+00:00 | 50.472 |  |
| bars | degraded | 2152771 | 2026-08-20T15:21:40.770153+00:00 | 50.472 | error:2, no_data:30, ok:1086 |
| social | degraded | 95926 | 2026-08-20T15:21:40.770153+00:00 | 50.472 | empty:50, error:28, ok:1040 |
| holders | degraded | 184117 | 2026-08-20T15:21:40.770153+00:00 | 50.472 | error:10, ok:707 |
| flow | ok | 83158 | 2026-08-20T15:21:40.770153+00:00 | 50.472 |  |
| traders | ok | 4701 | 2026-08-20T15:21:40.770153+00:00 | 50.472 | empty:48, ok:4668 |
| chain | degraded | 371798 | 2026-08-20T15:22:13.535896+00:00 | 17.706 | error:2, ok:439, unsupported:1 |
| chain_authority | ok | 8318 | 2026-08-20T15:22:09.199949+00:00 | 22.042 | ok:200 |
| evm_contract | ok | 2353 | 2026-08-20T15:21:30.433229+00:00 | 60.809 | ok:68 |
| activity | stale | 190 | 2026-07-28T13:26:53.311157+00:00 | 1994137.931 |  |

## Model

Rows: **8518**. Tokens: **573**. Feature versions: `{"12": 8518}`.

| family | status | rows present | coverage | columns |
|---|---:|---:|---:|---|
| event | ok | 8514 | 100.0% | size_usd |
| social | ok | 7964 | 93.5% | social_thesis_total |
| price_history | ok | 7914 | 92.9% | ret_24h_before |
| market | ok | 7603 | 89.3% | liquidity |
| holders | ok | 2909 | 34.2% | chain_holder_count |
| onchain_concentration | ok | 1432 | 16.8% | onchain_top1_pct |
| onchain_authority | ok | 989 | 11.6% | onchain_has_mint_authority |
| evm_contract | empty | 0 | 0.0% | onchain_code_size |
| flow | ok | 2372 | 27.8% | flow_net_volume_5m |
| macro | ok | 8518 | 100.0% | sol_ret_24h |
| density | ok | 8518 | 100.0% | prior_signals_token |
| trader | missing | 0 | 0.0% |  |

## Integrity Checks

| check | status | value | detail |
|---|---:|---:|---|
| model_duplicate_decisions | pass | 0 | Extra rows sharing token, network, and decision timestamp. |
| model_null_labels | pass | 0 | Eligible model rows with missing mature outcome labels. |
| model_feature_versions | pass | 1 | The model view must expose exactly one current feature version. |
| model_view_contract | pass | 0 | Required eligibility guards absent from the model view definition. |
| phase1_incomplete_ok_outcomes | pass | 0 | Eligible phase-one ok outcomes with missing required metrics. |
| phase1_incomplete_outcomes | warn | 2 | Incomplete outcomes are quarantined and excluded from phase-one analysis. |
| phase1_no_bars_outcomes | warn | 59 | Phase-one no_bars outcomes are retained in missingness analysis. |
| evm_done_balance_checks | pass | 0 | Completed replay rows whose balance check is not ok. |
| evm_pending_backfills | warn | 89 | EVM token ledgers not yet in a terminal live-backfill state. |
