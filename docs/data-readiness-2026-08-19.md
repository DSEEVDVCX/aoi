# Data Readiness Report

Generated: `2026-08-19T14:08:23.243027+00:00`
Database: `C:\Users\rr\Desktop\aoi\recorder\recorder.db` (16936574976 bytes, `ro`)

## Sources

| source | status | rows | latest | age seconds | states |
|---|---:|---:|---|---:|---|
| feed | ok | 23086 | 2026-08-19T14:08:11.333792+00:00 | 11.909 |  |
| trending | ok | 23066 | 2026-08-19T14:08:11.333792+00:00 | 11.909 |  |
| verified | ok | 23062 | 2026-08-19T14:08:11.333792+00:00 | 11.909 |  |
| most_held | ok | 7576 | 2026-08-19T14:07:11.341487+00:00 | 71.902 |  |
| market | ok | 3287507 | 2026-08-19T14:08:11.333792+00:00 | 11.909 |  |
| bars | error | 2074801 | 2026-08-19T14:07:11.341487+00:00 | 71.902 | error:2, no_data:32, ok:1041 |
| social | error | 93560 | 2026-08-19T14:07:11.341487+00:00 | 71.902 | empty:46, error:10, ok:1019 |
| holders | error | 176630 | 2026-08-19T14:07:11.341487+00:00 | 71.902 | error:1, ok:666 |
| flow | ok | 79415 | 2026-08-19T14:07:11.341487+00:00 | 71.902 |  |
| traders | error | 4614 | 2026-08-19T14:07:11.341487+00:00 | 71.902 | empty:40, error:528, ok:4076 |
| chain | error | 292114 | 2026-08-19T14:08:08.783155+00:00 | 14.46 | error:2, ok:396, unsupported:1 |
| chain_authority | ok | 6922 | 2026-08-19T14:06:55.769450+00:00 | 87.474 | ok:184 |
| evm_contract | ok | 1986 | 2026-08-19T14:08:21.382033+00:00 | 1.861 | ok:61 |
| activity | ok | 190 | 2026-07-28T13:26:53.311157+00:00 | 1903289.932 |  |

## Model

Rows: **9690**. Tokens: **613**. Feature versions: `{"12": 8212, "8": 1478}`.

| family | status | rows present | coverage | columns |
|---|---:|---:|---:|---|
| event | ok | 9686 | 100.0% | size_usd |
| social | ok | 9116 | 94.1% | social_thesis_total |
| price_history | ok | 9033 | 93.2% | ret_24h_before |
| market | ok | 8590 | 88.6% | liquidity |
| holders | ok | 2931 | 30.2% | chain_holder_count |
| onchain_concentration | ok | 1147 | 11.8% | onchain_top1_pct |
| onchain_authority | ok | 795 | 8.2% | onchain_has_mint_authority |
| evm_contract | empty | 0 | 0.0% | onchain_code_size |
| flow | ok | 2072 | 21.4% | flow_net_volume_5m |
| macro | ok | 9690 | 100.0% | sol_ret_24h |
| density | ok | 9690 | 100.0% | prior_signals_token |
| trader | missing | 0 | 0.0% |  |

## Integrity Checks

| check | status | value | detail |
|---|---:|---:|---|
| model_duplicate_decisions | pass | 0 | Extra rows sharing token, network, and decision timestamp. |
| model_null_labels | pass | 0 | Eligible model rows with missing mature outcome labels. |
| model_feature_versions | warn | 2 | Distinct feature versions exposed by the model view. |
| model_view_contract | pass | 0 | Required eligibility guards absent from the model view definition. |
| evm_done_balance_checks | pass | 0 | Completed replay rows whose balance check is not ok. |
| evm_pending_backfills | warn | 73 | EVM token ledgers not yet in a terminal live-backfill state. |
