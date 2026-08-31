# aoi — fomo.family data collection and labeling system

A system that collects data from the social trading platform [fomo.family](https://fomo.family)
moment by moment, and saves the **outcome** of each signal after 48 hours — with the goal
of building a model that predicts which coin will rise after a buy signal.

> **Status** (2026-08-29): collection, labeling, **and training-row building** are all
> automatic (eight permanent services + the Bars1m and backup tasks, §2). The current
> feature extractor is **fv16 = 171 features** and running.
>
> **The phase-1 gate was run twice — 2026-08-02 and 2026-08-07 — and not passed.** In
> 08-02 the signal's median return was −28.9% versus −2.2% for the control; in 08-07 the
> difference vanished (median −0.1% for both, `p`=0.25 on n=89/10). The structural cause
> is not just a weak effect: **the control design itself was broken** (`no_entry` 30.8%
> versus 2.3%, two mismatched universes, network-distribution imbalance 0.36). So the old
> control was deleted and redesigned as `v3` (`CONTROL_DESIGN_VERSION = 3`).
>
> Mature `v3` control count is now **426**: the exploratory review threshold (100) is
> **open**, and the binding threshold for accepting/rejecting the thesis (500) has not
> been reached (74 remaining). **No approved entry-model training** before it. The number
> is live; refer to `/api/labeling` instead of copying this snapshot. The detailed
> post-collection plan: **[docs/PLAN.md](docs/PLAN.md)**.

---

## 1. Why this project exists

fomo.family shows the market state **now** but keeps no history. You cannot ask:
"the coins bought by top traders yesterday — what happened to them?" This system closes
the gap: it captures the **decision moment (t=0)** then follows the price for 48 hours,
making the answer possible.

**The past cannot be recorded** (except partially — see §9). Every day of delay = lost data.

---

## 2. Architecture

**Eight independent permanent services**, plus the Bars1m archiving task and the daily
backup. All are **Windows scheduled tasks** that survive reboots, running under
`pythonw.exe` with no window.

```
        ┌──────────────────┐                    ┌──────────────────────────┐
        │  fomo.family     │                    │  blockchain nodes         │
        │  (no official API)│                   │  Helius · official RPC    │
        └────────┬─────────┘                    └─────────────┬────────────┘
                 │ curl_cffi (Cloudflare bypass)             │ chain_keys.json
    ┌────────────▼─────────┐                                  │  (key pools)
    │  FomoApiServer :8080 │  self-renewing Privy auth         │
    └────────────┬─────────┘                                  │
                 │ .privy_state.json (token on disk)          │
   ┌─────────────┴──────────────┐              ┌──────────────┴─────────────┐
   │ FomoRecorder    every 60s  │              │ FomoChain       every 60s  │
   │ FomoLabeler     every 15m  │              │ FomoEVMReplay   every 60s  │
   │ FomoBuildRows   hourly     │              └──────────────┬─────────────┘
   └─────────────┬──────────────┘                             │
                 └──────────────┬─────────────────────────────┘
                                │  recorder.db (SQLite WAL — five writers)
                 ┌──────────────┴───────────────┐
      ┌──────────▼─────────┐        ┌───────────▼───────────┐
      │ FomoDashboard :8090│        │ FomoBackup            │
      │ read-only mode=ro  │        │ daily 03:15 → OneDrive│
      └────────────────────┘        └───────────────────────┘
```

| Service | Directory | Role | Frequency |
|---|---|---|---|
| `FomoApiServer` | `api/` | Privy auth + REST wrapper for fomo | permanent :8080 |
| `FomoRecorder` | `recorder/` | collect and store raw data | every 60 seconds |
| `FomoLabeler` | `recorder/` | compute outcomes after the window matures | every 15 minutes |
| `FomoChain` | `recorder/` | **ownership concentration from the blockchain**: Solana (Helius) + mint authorities + the EVM layer, contract safety, and BSC in the same process | 60s cycle; a snapshot per coin every 5 minutes, authorities hourly |
| `FomoEVMReplay` | `recorder/` | **historical replay** of EVM balances from `Transfer` events | every 60 seconds, one coin/cycle |
| `FomoBuildRows` | `recorder/` | **build `training_rows`** incrementally (phase 3, previously manual) | hourly, cap 4,000 rows/cycle |
| `FomoActivityHead` | `recorder/` | plug the head of `tradingActivity` up to the first local overlap | every 5 minutes |
| `FomoDashboard` | `dashboard/` | live monitoring (read-only) | permanent :8090 |
| `FomoBars1m` | `recorder/` | archiving round of 1-minute candles for the eligible queue | hourly |
| `FomoBackup` | `recorder/` | consistent, verified SQLite copy to OneDrive | daily 03:15 |

**Why the separation**: the recorder records raw data only. Computing any value derived
from the future at record time is a **structural leak** that corrupts the model. The
labeler is a separate process that touches no row before its window completes.

**And why the chain layer is a separate process, not a step in the cycle**: the cycle
budget (60 seconds) is full, and a second external source inside it would let its
slowness delay fomo collection itself. The separation is what makes the five-minute
cadence **per coin** possible without harming that: the layer cycles every 60 seconds and
takes 20 due coins (`CHAIN_PER_CYCLE`), completing the round before
`CHAIN_REFRESH_SECONDS = 300` expires, at a cost of 20 calls per minute against **224
calls per minute measured on the key with zero failures** — about ~9% of the comfortable
capacity. (`chain_layer.py`, `run_chain.py`)

**Providers and key pools**: Solana on Helius, EVM on **official RPCs** (Robinhood 4663 ·
Base 8453 · Monad 143) because they measured best, and NodeReal for BSC (56) holders —
the only source that gives an exact ERC-20 holder count. And **no keyed provider is on
the replay path**: GoldRush was deleted from the code on 2026-08-19 — the account had
been returning 402 since 2026-08-17, and its path had spent 41,386 calls on Base for
**zero snapshots** (trap #29). All replay now runs on official RPCs, and any keyed
provider's return starts from `PROVIDERS` in `key_file.py`, not from a saved converter.
Monad (143) is **collected live and never replayed** (`EVM_REPLAY_NETWORKS = 4663 ·
8453`): six signals in the whole archive, and it was taking a third of replay cycles for
zero snapshots. Keys are read from local files (`chain_keys.json` and the like) via
`provider_keys.py` and managed in **rotating pools**: only the owning process holds the
pool, and it writes a report with no key values at all to `meta` (`pool_report()`), which
the dashboard shows as one dot per key. (Trap #26)

---

## 3. Data flow — one recorder cycle

```
0)    refresh the leaderboard head + archive its raw form in snapshots   (hourly)
1)    GET /feed          → signal_events + watchlist + last_feed_event_at
2)    POST /proxy/trendingTokens ┐
      GET  /proxy/verifiedTokens ├→ snapshots + market_ticks + token_static
      GET  /proxy/mostHeld       ┘
2.3)  POST /proxy/filterTokens → close the measurement gap: watchlist entries not
      captured by any list in this cycle  (150 addresses/call — measured)
2.2)  comparison signal windows from the same universe and the same market price used
      for the control
2.25) pick random control coins  (target 40 active · 2/cycle · only in a cycle that
      accepted a comparison signal — so universe and price are identical)
2.42) tokenDetails + /hodlers/top → token_holders + token_flow  (13 coins/cycle)
2.45) GET /v2/users/{id}          → traders                     (4 traders/cycle)
2.5)  POST /proxy/getBarsNew      → token_bars                  (9 coins/cycle)
2.75) GET /feed/token/thesis      → token_social + token_thesis  (7 coins/cycle)
2.9)  POST /proxy/getBarsNew      → hourly SOL/WETH/WBTC candles   (hourly)
3)    disable what passed 48 hours  (`SNAPSHOT_RETENTION_DAYS = 0` ⇒ no pruning:
      the raw archive is kept in full)
```

**The rotation is deliberate, not neglect**: each rich source scans a slice per cycle, so
full-scan time = number of watchlist entries ÷ slice, in minutes. The numbers above are
not arbitrary but the result of buying time from pacing (`*_PACING_SECONDS`) after
measuring that the upstream has no call limit and that the real limit is the sixty
seconds themselves (trap #27).

Every call sits inside its own `try/except`: a source failure is logged to
`meta.last_error_<src>` and skipped. **The loop never dies** — with a last-resort shield
around the whole cycle.

> ⚠️ Writing the bookkeeping itself is a trap: use `db.note_error`, not bare `set_meta`.
> An error handler once wrote to the locked database and the recorder died for 3h14m —
> the guard itself became the cause of death (trap #25).

### The chain-layer cycle (`FomoChain` — separate process, 60-second cycle)

```
1) Solana: oldest due watchlist entries (20/cycle) → one call each
   (getTokenLargestAccounts + getTokenSupply in a single HTTP request)
   → chain_concentration: top1/5/10/20 together — measured 230ms
2) Solana hourly (6/cycle): mint/freeze authorities and mutable → chain_authority
3) EVM in the same process under its own guard: a single eth_getLogs covering **all**
   the network's coins (4663 · 8453 · 143) → evm_balances (a balance ledger built from
   Transfer events), from which a concentration snapshot per coin every 5 minutes
4) contract safety straight from the byte-code (Base only, 2/cycle) → evm_contract
5) BSC (56) via NodeReal (2/cycle, every 15 minutes) → exact holder count and top 20
```

**Coverage is not uniform, and this is measured, not overlooked**: `EVM_ADDRESS_BATCH`
is the provider's limit, not our choice (Robinhood and Base 60 addresses per filter,
Monad 40, BSC only **5** — hence BSC on NodeReal rather than on the transfer ledger).
Contract safety is Base-only because that is the network where measurements diverged; on
4663 sizes repeat in six identical templates so the column is nearly constant. And
Robinhood returns `blockTimestamp: '0x0'` in every record (measured 2026-08-13), so it
needs a time↔block anchor table (`evm_block_time`) that Base and BSC do not need.

Three states, not two, for every measurement — `ok` (a measurement arrived), `empty`
(the source replied with no measurement: the address is not a token), and `error` — so
the next retry cadence differs between them.

**Why this layer when fomo gives us `token_holders`?** Because fomo provides only
`top10HoldersPercent`, and only every ~25 minutes (measured median gap 25.0m over 19,440
pairs): no top1 — no answer to "one whale or ten distributed holders?", which are two
different risks — and no cadence that can keep up with a dump unfolding in minutes.

**And Solana alone in concentration is not neglect**: the ERC-20 standard carries no
holder list on-chain, so no node call gives EVM top holders at all. The only route to an
exact number is replaying every `Transfer` event and saving the resulting balance — which
is what `FomoEVMReplay` does, and it is measured **cheap, not expensive**: one
`eth_getLogs` call with a filter carrying all the network's addresses (Robinhood 57
addresses in 0.5s), and backfilling a quiet coin's history is a single call (1,814
transfers across 1.71 million blocks, 0.7s).

Signals split **42.5% Solana / 57.5% EVM** (Solana 40,848 of 96,106) — so any on-chain
work is **two implementations, not one**. The network distribution decides where effort
goes: Robinhood (4663) **31.6%** · BSC (56) **22.0%** · Base (8453) 3.9% · Monad (143)
six signals. That is, the second-largest network in the archive is precisely the one the
transfer ledger **does not work for** (filter cap of 5 addresses), so it runs on NodeReal
with a different measure — a methodological divergence forced by provider limits, not by
choice.

---

## 4. Data model (`recorder/recorder.db`)

### Raw from fomo

| Table | Purpose | Critical note |
|---|---|---|
| `signal_events` | the decision moment t=0, **45 columns** | `raw_json` compressed · size and likes fields recovered |
| `watchlist` | what we watch now + when it ends | operational state, **upgradable** — not a source of truth |
| `watch_windows` | **immutable record of every window** | source of truth for comparison and labeling; the separation prevents upgrading a control to a signal by erasing its window |
| `token_bars` | OHLCV candles — **the price source of truth** | 5-minute for watchlist entries · hourly for SOL/WETH/WBTC (market reference, since 2026-07-28) · daily for deep history (ATH) · `h/l/c_suspect` flags impossible upstream values (since 2026-07-30) |
| `market_ticks` | market snapshot from trending/verified/mostHeld/filterTokens | partial coverage — candles beat it. And its `top10_holders_pct` is a **dead column**: 0 of 1,430,475 rows (trap #28) |
| `token_static` | coin constants (mint/freeze/socials) | once, on entry |
| `token_social` | time series of social momentum | `thesis_total`, not `thesis_count` |
| `token_thesis` | one row per thesis with its write timestamp | enables **historical** counting |
| `token_holders` | ownership concentration **from two sources** | one row per `source`: `tokenDetails` = on-chain concentration, `/hodlers/top` = fomo users' positioning. **Two different measurements, not two copies** (§3) |
| `token_flow` | buy/sell flow with 5m/1h/24h layers | the ratios are the information, not absolute values; no 12h layer upstream ⇒ no column for it |
| `traders` | profile of every recurring trader (17 columns) | matches the live-measured `/v2/users/{id}` response — **no `win_rate`, no `realized_pnl`** (absent from the response) |
| `snapshots` | full raw archive per source/cycle | includes the **raw leaderboard hourly** (since 2026-07-28); `SNAPSHOT_RETENTION_DAYS = 0` ⇒ no pruning |
| `activity_events` | retrospective `tradingActivity` history | a separate table to keep forward collection clean; **excluded from training** (`is_live=0`) |

### From the blockchain (`FomoChain` / `FomoEVMReplay` layers)

| Table | Purpose | Critical note |
|---|---|---|
| `chain_concentration` | top1/5/10/20 + supply — **Solana** | a standalone table, not columns on `token_holders`: different source and cadence. `top10_pct` agreement between the two sources is a **free cross-check** |
| `chain_authority` | mint/freeze authorities and mutable | one row per measurement, not an updated row: an authority left **is an event** that happens mid-window |
| `evm_balances` | balance ledger built from `Transfer` events | the balance is a **64-wide hex string**, not a number: uint256 exceeds SQLite's limit, and the zero padding makes `ORDER BY balance_hex DESC` numerically correct |
| `evm_contract` | contract safety from byte-code (`eth_getCode`) | Base only; 100% coverage, not 10% — every verification provider measures below it |
| `evm_block_time` | time↔block anchors | needed **for Robinhood alone**: its RPC returns `blockTimestamp: '0x0'` |
| `evm_block_cursor` | how far transfer application has reached | one row **per network**, not per coin: the call itself is one per network |

### Derived (rebuilt without the network)

| Table | Purpose | Critical note |
|---|---|---|
| `outcomes` | **the results (labels)** | filled exclusively by the labeler |
| `phase1_watch_outcomes` | View: eligible comparison windows | `analysis_eligible=1 AND design_version>=3` — the old control never reappears |
| `token_class` | asset classification (meme/major/priced/stable) | `classify_tokens.py` |
| `training_rows` | **the training table**: 204 physical columns; current contract = 13 meta + **171 features at t=0** + 10 labels (plus dead archival columns kept but not exported) | `build_training_rows.py` — **incremental, overwriting the same row. Do not use `--rebuild`** (below) |
| `model_training_rows` | **View: the safe model interface** | enforces `signal + is_live=1 + meme + ok + independent + feature_version≥2` and removes coincident signals. **Train on the view, not the table** |

### Operational state

| Table | Purpose |
|---|---|
| `bars_fetch_state` · `social_fetch_state` · `holders_fetch_state` · `traders_fetch_state` · `chain_fetch_state` · `chain_auth_state` · `evm_contract_state` | rotation state per source — drives the round-robin scheduling |
| `evm_replay_state` · `evm_backfill_state` · `historical_bars_state` · `activity_bars_state` | backfill progress (resumable) |
| `meta` | counters and operational state: `last_error_*` · `last_feed_event_at` · `*_last_run_at` · key-pool reports |

### `--rebuild` is not a preparation step — it is deletion

`build_training_rows.py --rebuild` **deletes `training_rows` entirely** and then builds
from scratch. Incremental building overwrites the same row, so after any recovery of
past data (theses · constants · candles · corruption flags · classification · ATH) or any
`FEATURE_VERSION` bump, letting `FomoBuildRows` cycle is enough — **no `--rebuild`**.
That is why [recorder/run_build_rows.py](recorder/run_build_rows.py) blocks it
explicitly: a scheduled deletion means a window where 93 thousand rows do not exist.

**Status snapshot** (2026-08-19, at the moment this line was written — the database grows
every minute): **96,106 signals** · 750 classified coins (716 meme · 17 major · 14
priced · 3 stable) · 190 active watchlist entries · **2.05 million candles** · 3.20
million market ticks · 43,408 theses · 76,144 raw snapshots · **105,509 labeled
outcomes** · **93,698 training rows** (76,385 live · 17,313 retrospective, excluded) ·
**305 mature `v3` controls** · 16.4 GB.

**And the single governing number: 9,496.** That is what remains in
`model_training_rows` — **10% of training rows** and **0.01× the signal count**. The
funnel is not a defect but the correct filter (live · meme · `status='ok'` ·
**independent** · `feature_version>=2` · no coincident-signal duplication), and
`is_independent` is its harshest cut. When you ask "how much data do we have?", the
answer is 9,496, not 96,106 — **count the view, not the table**.

> Regenerate these numbers yourself instead of trusting them:
> ```sql
> SELECT COUNT(*) FROM signal_events;
> SELECT COUNT(*) FROM outcomes;
> SELECT is_live, COUNT(*) FROM training_rows GROUP BY 1;
> SELECT COUNT(*) FROM model_training_rows;                      -- the governing number (slow)
> SELECT COUNT(*) FROM phase1_watch_outcomes WHERE is_control=1 AND status='ok';
> ```
> `model_training_rows` is **slow** on a 16 GB database (minutes). Do not put it in a
> loop or a dashboard path.
> For full analysis: `py recorder/pattern_analysis.py` and `py recorder/phase1_analysis.py`.

### Governing principles

1. **Always raw** — every row saves `raw_json` beside the extracted fields. This is what
   made later recovery of forgotten fields possible (§9).
2. **No fabrication (FR-007)** — an absent field is `NULL`, not zero. `False` is stored
   explicitly (`freezable=0` means "safe", distinct from `NULL` "unknown").
3. **No future leakage** — the recorder never computes a label.
4. **Survivorship-bias resistance** — losing and dead coins are recorded exactly like
   winners.
5. **Read-only (FR-012)** — no call writes account or trading state.
6. **No filtering at collection** — record everything, filter at modeling (§7).
7. **Train on live data only (`is_live=1`)** — retrospective rows structurally lack the
   real-time families (`market_ticks` · `token_social` snapshots · leaderboard rank ·
   macro candles), so their missingness pattern **matches the era**, which the model
   learns instead of the signal — a whole-era leak. The retrospective set is kept for
   exploratory analysis and excluded from training by the `model_training_rows` view,
   not by the query author's memory.

---

## 5. Watchlist rules

| Signal | Enters the watchlist? |
|---|---|
| `large_buy` | ✅ |
| `multi_user_buy` | ✅ |
| `multi_user_sell` | ❌ context only |
| `large_sell` | ❌ context only (whale dump — recorded and labeled whenever candles exist) |

- The window is **48 hours** from first sighting. Cap of 150 coins (applies to the
  referenced coin alone).
- An active coin is **not extended** by a new signal (first sighting is the reference).
- An expired coin **returns** with a fresh window if a later signal references it.
- **No condition on size, market cap, or momentum** — deliberately (§7).

### The control group (the negative class)

Coins that enter **by random selection, not by signal** (`is_control=1`). Without this
class a model can learn "any referenced coin rises more", but it can **never** answer
"does the signal mean anything at all".

Comparison validity conditions — all deliberate and tested: random, not ordinal · with
no look-ahead · everything referenced is excluded · in installments (two coins/cycle,
target 40 active) · one-directional upgrade · a separate cap.

**And the current design is `v3` (`CONTROL_DESIGN_VERSION = 3`) because the first one
was broken.** The first design drew controls from `trending/verified` and signals from
`/feed`, so the two universes differed and both sides' prices came from different
sources — resulting in `no_entry` at 30.8% for the control versus 2.3% for the signal
and a network-imbalance distance of 0.36, meaning the comparison measured
**priceability**, not return (§8).

`v3` unifies them **by constraining the signal, not by moving the control**: a signal
enters the comparison only if it itself appeared in `trending/verified` in its acceptance
cycle, and both sides' prices come from **the same market snapshot** (§3, steps 2.2 and
2.25) — and no control is accepted except in a cycle that accepted a comparison signal.
Old-control eligibility was not carried over to `v3`; and `phase1_watch_outcomes`
requires `design_version>=3` explicitly so the old one never returns to the analysis by
mistake.

---

## 6. The labeler — outcome definitions

It runs only after `entry + 48h + 15min`. Fields in `outcomes`:

| Field | Definition |
|---|---|
| `entry_px` | close of the **first candle at/after** the signal (delay ≤30m, else `no_entry`) |
| `max_gain_1h/4h/24h/48h` | max `high` in the candles **after the entry candle** ÷ entry − 1 |
| `max_drawdown_48h` | lowest `low` ÷ entry − 1 |
| `final_return_48h` | close of the last candle ÷ entry − 1 |
| `time_to_peak_h` | hours until the peak |
| `is_rug` | final return ≤ −90% |
| `bars_truncated` | the series ended early by >1h — **a coin's death is a signal, not a gap** |
| `is_independent` | first signal or a gap ≥30m (69% of signals <5m = false repetition) |
| `split` | train/val/test by **coin address** partitioning |
| `status` | `ok` / `no_entry` / `no_bars` — the reason values are absent is documented |

**No-leak guarantees**: the entry price never precedes the signal · the entry candle's
own high is not counted as gain (it may precede execution) · no labeling before maturity.

---

## 7. Why we don't filter at collection

A recurring question: "why do we treat a $995 trade and a $172,169 trade alike?"

Because if you filtered at collection — "only watch above $50,000" — you **lose forever**
the ability to know whether the threshold was right. Not one small trade would remain in
your data to compare against. Filtering at collection **executes the negative class**.

The correct order: record everything indiscriminately → discover the threshold from the
data at modeling.

**Scenarios are tested as feature combinations on the archived raw data, not as entry
conditions.**

---

## 8. What the data says so far

> ⚠️ **Every number in this section is a snapshot with a date, and the database grows
> every minute.** Do not base a decision on any of them: regenerate with
> `py recorder/pattern_analysis.py` and `py recorder/phase1_analysis.py`. The binding
> thresholds live in [docs/PLAN.md](docs/PLAN.md), not here.

### The governing result: the phase-1 gate was not passed (2026-08-02, then 08-07)

This is the most important thing the data says, and it precedes everything after it:
**it has not been shown that the signal beats a random coin.**

| Run | Signal | Control | Verdict |
|---|---|---|---|
| 2026-08-02 | median −28.9% · win 19.9% | median −2.2% · win 33.3% | ❌ signal **worse** |
| 2026-08-07 | median −0.1% · win 48.3% (n=89) | median −0.1% · win 50.0% (n=10) | ❌ no difference (`p`=0.25) |

More important, the **difference was not causally interpretable** in the first place, for
three structural reasons that were measured, not guessed: the control was drawn from
`trending/verified` and the signal from `/feed`, so the universes differed; the
network-imbalance distance was 0.36 (the guard rejects above 0.10); and `no_entry` was
30.8% for the control versus 2.3% for the signal in 08-02 — meaning `status='ok'` was
comparing two different subsets in **priceability**, not in return.

⇒ So the old control was deleted and redesigned as `v3`, and the two universes were
unified **by constraining the signal, not by moving the control**: a signal enters the
comparison only if it itself appeared in `trending/verified` in its acceptance cycle,
both sides' prices come from **the same market snapshot**, and no control is accepted
except in a cycle that accepted a comparison signal (§3, steps 2.2 and 2.25, and §5).
Old-control eligibility was not carried over to the new design. The two reports:
[phase1-2026-08-02.md](docs/phase1-2026-08-02.md) (the record the decision was built on)
and [phase1-2026-08-07.md](docs/phase1-2026-08-07.md) (the newer one).

### First reading on ongoing windows (2026-07-28)

> ⚠️ **Preliminary** results on **ongoing, incomplete** windows, with no mature control
> comparison. Not final results and not trading advice.

Across 89 coins (first signal per coin):

- **Win rate 41.6% · median −3.2% · mean +1.4%**
- The mean is positive while the median is negative: **one outlier winner (+1092%)
  carries the entire net**.
- The only statistically strong pattern — **market cap**, and it is monotonic:

| Market cap | n | Win | Median | Peak median |
|---|---|---|---|---|
| < 1M | 28 | 32% | −21.4% | +35.4% |
| 1–10M | 38 | 50% | −0.1% | +14.0% |
| 10M+ | 23 | 61% | +0.6% | +5.4% |

- **The project's own thesis** ("a top trader bought"): n=6 only, median −20.7%. No
  evidence it helps — and no sufficient evidence it doesn't.
- **Time to peak**: the raw median of 3.3 hours is **misleading** (right truncation).
  After restricting to matured coins: **11.4 hours**, and 47% peak after 12 hours.
  ⇒ The 48-hour window is justified.
- **Signal rate**: `large_buy` ~1138/day · `multi_user_buy` ~14/day
  (⇒ the original thesis needs ~71 days to reach 1,000 samples).
- **False repetition**: 14.2 signals/coin, median gap **1.1 minutes**, and 69% <5 minutes.

### First retrospective reading (2026-07-28) — the central thesis over 9 months

> ⚠️ **Exploratory on incomplete data**: the results below come from `activity_events`
> (retrospective tradingActivity) **and not from the fully featured forward pipelines**.
> Beyond the statistical caveats (no retrospective control, 2.3% survivorship bias, 9%
> without entry candles, mixed eras), the retrospective set **lacks most of the
> discriminative dimensions** we currently collect — read the table below before reading
> any number.

**What the retrospective event has**: the kind, the coin, the time, price/market cap at
that moment, `unique_traders`/`top_trader_ids`/`total_volume`/`minutes` (for multi), and
`usd_amount` (for single swaps).

**What the retrospective lacks versus the forward event — every gap documented with its
reason**:

| Dimension absent retroactively | Available when | Effect on results |
|---|---|---|
| **Leaderboard rank at event time** (`buyers_best_rank`, `top_trader_match_count`) | forward only (leaderboard archive since 2026-07-28) | the "buyer quality" thesis is not measurable retroactively |
| **Social momentum series** (`token_social` every 30m) | forward since watchlist entry | retroactively only the historical thesis count exists, not its real-time acceleration |
| **Thesis likes at event time** | ❌ lost forever, forward and retro alike | no social-consensus feature |
| **Real-time market context** (`market_ticks`: liquidity, holders, buy/sell counters) | forward only | no liquidity/concentration discrimination |
| **Macro candles** (SOL/WETH/WBTC) | since 2026-07-28 | only partial isolation of market-system effect (across eras) |
| **`large_buy`/`large_sell` flood** (84% of our forward data) | forward exclusively | results are about collective blocks only, **not about the large-buy thesis** |
| **The control group** | forward exclusively | no causal verdict: "does the signal beat random?" stays open |

⇒ Read the numbers below as "the shape of the world for the multi_user branch with
impoverished features"; the model trained on the rich forward data may partition these
results fundamentally differently.

From `backfill_activity` (819 `multi_user_buy` events / 408 coins, 472 labeled
independent):

- **Win 18.9% · median −58.7% · mean −18.0% · rug 23.1%** — holding for 48 hours loses
  heavily at the median.
- **Median peak within 24h: +46.2%** — the "pump then dump" pattern: most coins jump,
  then die. The thesis's real question is the **exit rule, not the entry**.
- The exit simulator on it (382 first-per-coin trades, 2% cost): hold-to-end median
  **−72.3%** · take-profit +10% median **+8%** with an 86% win rate but mean **−3.7%**
  (the rare catastrophic failure eats the small clipped gains) · break-even versus cost
  **~0%** for every fixed rule.
- **Era variance is sharp**: 2026-06 win 42%/mean +100% versus 2025-12 win 9%/mean −64%
  — justifying the mandatory walk-forward (PLAN §2.4).
- **The model's role based on this**: blind play is decisively median-losing; the
  model's job is **filtering signals** (don't play them all), and its measured baseline
  to beat = "play everything with a +10% take-profit" (PLAN §3.1).

### Asset classification (2026-07-30) — the archive is not a meme market

fomo is a **multi-asset** platform. The latest classification (`token_class`, derived
from all sightings, last run 2026-07-30) covers **750 coins**; the numbers below are from
that same run, so re-run `py recorder/classify_tokens.py` before relying on them:

| Class | Coins | Note |
|---|---|---|
| `meme` | **716** | the overwhelming majority in signals and outcomes |
| `major` (value > $1B) | 17 | BTC · ETH · SOL · BNB … |
| `priced` (price > $5: tokenized stocks/commodities) | 14 | AAPL · MU · PAXG … |
| `stable` (dollar-pegged price) | 3 | — |

> The counts change between runs because the classification is derived from **all**
> sightings available at the time: the older run (cited in the retrospective reading
> above and in trap #23) gave 725 coins (686/21/14/4). This is not a defect but the
> nature of a derived measure — which is why classification must be re-run **before**
> any analysis, not quoted from a document.

Actually sighted: BTC · ETH · SOL (103 signals) · BNB · XRP · TRX · DOGE · HYPE · LTC ·
JUP · MORPHO · TRUMP · Cake · PAXG gold · and tokenized stocks: AAPL ($339) · SNDK
($1013) · MU ($958) · MSTR · HOOD · INTC · META · NET · DRAM.

**Market cap does not reveal them**: AAPL at a market cap of only $1.36M (the tokenized
fraction is a sliver of the share) — so price is the discriminator, and a meme coin does
not trade above a few dollars (267 of 297 coins under $0.1). And the impersonation case
was guarded: a meme coin named "BTC" at $3.5M and another "SOL" at $4.9M are **not**
classified as major assets (a substantial market-cap condition applies).

### Re-examining results after classification — both claims held

1. **The central thesis numbers were unaffected**: only 5 of 487 rows are non-meme.
   Memes only: win **18.0%** · median **−59.7%** · rug **20.5%** · median peak
   **+45.4%** — practically identical to the published numbers (18.3% / −59.6% / 20.5%).
2. **The market-cap effect is real within memes alone** (481 samples) — a clean
   monotonic gradient, no asset-mixing effect:

| Market cap (memes only) | n | Win | Median | Rug |
|---|---|---|---|---|
| < $1M | 135 | 10.4% | −79.9% | **33.3%** |
| $1–10M | 234 | 17.9% | −65.9% | 20.9% |
| $10–100M | 89 | 24.7% | −29.4% | 4.5% |
| > $100M | 23 | **39.1%** | **−9.4%** | **0%** |

⇒ The difference between the smallest and largest bins: win ×3.8 and rug from 33% to
zero. This is the **strongest measured gradient in the project**, and it survived the
asset-classification control.

### The composite signature (2026-07-28, binary cuts — the strongest effect measured so far)

| | Total <$50k | Total ≥$50k |
|---|---|---|
| **Small crowd** (per-head <$3.1k) | n=155 · win 12.9% · median **−78.2%** · rug **31.6%** | n=84 · win 15.5% · median −67.6% · rug 22.6% |
| **Big elite** (per-head ≥$3.1k) | n=0 (nearly nonexistent in the universe) | n=239 · win **23.4%** · median **−37.8%** · rug **12.1%** |

- "Few big buyers + huge total" beats "small crowd + smaller total": win ×1.8, median
  halved, rug cut to a third — a real one-directional structure, not noise (but it
  **filters, it doesn't flip**: the best cell's median is −37.8%).
- The block universe starts at ~11 buyers and ~$20k — neither "three people" nor a
  "small block" in it.
- `are_top_traders` = 0 across all retrospective data (a useless flag retroactively).
- Forward (very small n): `large_buy` ≥$10k + a classified buyer = win 42.9% (n=7) — a
  consistent direction, not a verdict.

### What these numbers claim and what they don't

They describe **blind play with fixed rules** — not a verdict on "the thesis". Three
structural facts that aggregation hides if read alone:

1. **Median ≠ distribution**: a real right tail exists — MarsCoin (network 56, captured
   2026-07-27 20:17 with 8 theses) reached **+1962% at peak and +1214% at close so
   far**. But it is one row of 819; against it stands the median −58.7%. Both are real
   data.
2. **Fixed rules crush exactly the tail**: on MarsCoin itself from the actual entry —
   take-profit +10% exited after **24 minutes** and missed +1900%, and the 25% trailing
   stop exited **−21.8% after 12 minutes** (a candle wick) before a 20× climb. So "the
   best rule" on the big winner and on the median are structurally opposite — the
   question "which fixed rule" is simply wrong.
3. **The promising exit direction is momentum-based, not numeric**: MarsCoin exploded
   socially (8→393 theses/71 writers in `token_social`) and top trader #2 entered with
   $39k then sold at #42 later — **momentum death and the top trader's dump are recorded
   by us in real time**, and that is the real candidate for an exit rule (PLAN §4.2). A
   note of honesty: the coin is still inside its window and has fallen 36% from its peak
   — even the winning story is read after the window closes, not during it.

---

## 9. What was recovered retroactively and what is lost forever

The "always raw" principle is what made recovery possible: the fields were not lost, just
**not extracted**.

| Data | Status | Tool |
|---|---|---|
| Trade size fields | ✅ 100% (1485/1485) | `backfill_sizes.py` |
| Candles | ✅ deeper than the archive (back to 2025-11) | automatic |
| Historical thesis count | ✅ 30,182 theses, oldest 2025-08 | `backfill_thesis.py` |
| **multi_user signal history** | ✅ **1,151 events back to 2025-10-22** (of which 819 collective buys / 408 coins) — `/feed/tradingActivity` scrapes backward | `backfill_activity.py` |
| A control covering the signal period | ✅ 60 coins from `snapshots` | `seed_control_retro.py` |
| **Likes/replies at signal time** | ❌ **lost forever** | — |
| **Leaderboard rank history before 2026-07-28** | ❌ **lost forever** — it was read and discarded hourly with no archive | raw archived in `snapshots` since 2026-07-28 |
| **Historical `large_buy` flood** | ❌ `/feed` is newest-only and its pagination is ignored (proven) | forward collection since 2026-07-25 |

**The one loss**: fomo gives only the **current** likes counter. So
`token_thesis.fetched_at` is recorded: it is the time the likes were measured, and
`num_likes` must not be read as the value at writing time. `token_social` resolves this
for the future.

> ⛔ **And the recovery succeeded technically but failed in use.** Retrospective rows
> (`is_live=0`) are **excluded from training** and never enter `model_training_rows`:
> because they structurally lack the real-time families (`market_ticks` ·
> `token_social` snapshots · leaderboard rank · macro candles), their missingness pattern
> **matches the era**, which the model learns instead of the signal. They are kept for
> exploratory analysis alone. So do not run `backfill_activity.py` or
> `backfill_activity_bars.py` intending to increase training data — they do not increase
> it, and they increase the chance of an era leaking into where a feature should be
> (§4, principle 7).

---

## 10. Discovered traps — read before any change

Each one cost time or nearly corrupted the data.

### fomo data and API

1. **`getBarsNew` needs `"address:networkId"` and `from`/`to`** — a bare address throws
   a **Cloudflare 502** that looks like a fomo outage but is a malformed request. Same
   for `tokenDetails`. (`_pair_id` in `fomo_client.py`)
2. **OHLCV availability decays** — a coin returned 192 candles then `404 No OHLCV data`
   15 minutes later. Do not postpone pulls on the assumption history persists.
3. **The thesis counter saturates at 100** — the response returns 100 items while
   `responseObject.count` can reach **10,551**. Use `thesis_total`.
4. **`currentSizeUsd` was being wasted** — the "large buy" median is only **$3,448** and
   extends to $172,169. Without this field every trade looks the same.
5. **The feed can freeze** — observed frozen for 3 hours while the recorder ran without
   error. `meta.last_feed_event_at` distinguishes "quiet market" from "dead source".
6. **`/proxy/verifiedTokens` sometimes throws 502** from fomo's side — outside our
   control.
7. **`large_sell` was a missing valid feed type** — a 2026-07-25 probe confirmed only
   three types; the re-scan (2026-07-28) revealed it. Lesson: the confirmed type list is
   not closed — re-scan candidate values periodically. Its shape = `large_buy` in the
   opposite direction (`inHumanAmount` = the sold coin).

### Storage

8. **`raw_json` columns are zlib-compressed BLOBs, not text** — always use
   `db.decode_raw()`. (Compression cut growth from ~1.3 GB/day to ~280 MB.)
9. **`CREATE TABLE IF NOT EXISTS` does not touch an existing table** — any new column
   needs `_COLUMN_MIGRATIONS`, **and the migration precedes the schema** (indexes on the
   new columns fail before it).
10. **The old `outcomes` key `(token, entry_ts)` collides** — 201 signals for one coin.
    The key is now `(kind, key)`.
11. **`upsert_watch` with `INSERT OR IGNORE`** was swallowing every later signal after a
    watch ended — a silent bleed of the list down to zero.

### SQL and Python

12. **Three-valued SQL logic** — `NOT (NULL AND NULL) = NULL`, so rows are silently
    excluded. Use `COALESCE` in `LEFT JOIN` conditions.
13. **Infinite loop in backfill** — `WHERE col IS NULL` returns the same rows if they
    stay NULL after processing. Advance with a `rowid` cursor.
14. **A missing `asyncio_mode` ⇒ silent skipping** — 8 async tests were seen skipped
    with the suite "green". `pytest.ini` raises it to an error.

### Operations and deployment

15. **`Stop-ScheduledTask` does not kill the process immediately** — the state returns
    `Ready` while `pythonw` is alive. The script guards check the **process**, not the
    task state.
16. **`log_config=None` is required under pythonw** (uvicorn's formatter calls
    `isatty()`) — **but it means zero logging handlers**. `serve.py` installs a rotating
    file handler.
17. **The dashboard is served with `Cache-Control: no-cache`** — without it the browser
    keeps a stale version forever, including after a bug fix.
18. **`Element.append()` returns `undefined`** — do not chain `.textContent` on it.

### Analysis

19. **Right truncation misleads** — "no peaks after 24 hours" was a false conclusion;
    the cause was that the maximum follow-up available was 22 hours. Always adjust
    before concluding.
20. **`errors_total` is a lifetime counter** — its ratio to cycles is not "current
    health".
21. **A separate test schema drifts** — a query that failed in production passed in
    test. The drift guard builds the database from the real `schema.sql`.
22. **fomo candles contain impossible values** (2026-07-30) — `h = 2,626,092` for a
    candle whose close is 0.0219 (×119 million), and `c = 12,052.5` between closes ≈
    0.0004. **Confirmed by a live re-pull ⇒ permanent upstream corruption, not a
    transport error**. Impact: `max_gain` +3.78 billion % in 4 rows, and the dashboard
    showed **+62,570,743,609%**. The fix: `bar_context_flags` — price is continuous in
    the aggregate, so a value exceeding **both** its neighbors by ×10 and not persisting
    is corruption, not a price. They are flagged (`h/l/c_suspect`) and excluded from
    computation, **not fixed and not deleted** (raw is sacred). A general lesson: any
    column derived from an external source needs a physical-plausibility check before it
    becomes a label.
23. **The archive is not meme coins only** (2026-07-30) — major assets, tokenized stocks
    and commodities, and stablecoins inside the archive (BTC $1.27T · SOL 103 signals ·
    AAPL · PAXG …). **Resolved**: the `token_class` table classifies coins via
    `classify_tokens.py` (last run 2026-07-30: **750 coins** = 716 meme · 17 major · 14
    priced · 3 stable; and the count changes with every run because it is derived from
    all sightings available at the time, so do not quote it from here). Market cap alone
    is **not enough** for detection (AAPL at $1.36M) and price is the discriminator,
    with a guard against ticker impersonation. Any analysis or training must constrain to
    `asset_class='meme'` or separate the classes explicitly.
24. **`market_cap` misleads in the tail and is not flagged corrupt** — it is merely price
    × supply, so a coin with 7.8×10¹⁴ tokens reads "$69 trillion" (SMILE) with no real
    dollar in it. **The check proved it is not a defect**: no coin's value varies >×50
    between sightings except by real price movement (BONK and MarsCoin). The remedy is
    featurization: log/bins, and prefer `liquidity`. Flagging it "corrupt" would have
    been a judgment no measurement supports.

### Hard limits: time · keys · columns

25. **The error handler needed the locked database and died with it** (2026-08-17) — the
    `last_error_*` lines are written from **inside** the exception handler; so when the
    failing resource was the database itself (`database is locked`), `set_meta` raised in
    turn, taking down the handler, the rest of the cycle, and then the loop's shield.
    The result: the recorder exited with code 1 and the task sat `Ready` for **three
    hours and a quarter, silent**, without a single line in the periodic log. **The
    fix**: `db.note_error()` — it attempts the write and returns `False` without
    raising. The rule: any **bookkeeping** stamp (a description of a failure that already
    happened) goes through `note_error`; bare `set_meta` stays for depended-on state
    (`schema_version`, cycle stamps, the EVM ledger generation) where its failure **must**
    be heard. Losing a descriptive line is cheaper than losing the cycle — and the
    measurement is never written through that route.
26. **The key pool lives in one process's memory, and the dashboard is another
    process** — without a written stamp there is no way to answer "is there even a
    second key?" or "is one of them rejected right now?" except by reading a text log.
    **The fix**: `KeyPool.stats()` and `pool_report()` write to `meta` **counts and
    indicators only — with no key values or fragments**: the count cannot be
    reconstructed into a key, and the fingerprint is matched, never published. The
    reason is that whatever is written to `meta` gets backed up and read in logs. The
    dashboard has a **separate second route** (`keystore` reads the file directly) that
    shows the account name and last four characters on request, computed at that moment
    and written neither to `meta` nor to logs. Do not mix the two routes: what may be
    **displayed** is not necessarily what may be **logged**. (And a stopped key stays in
    the file for the dashboard to see and re-enable, and never enters the pool so it is
    never called.)
27. **The constraint on collection is not the source's limits but cycle time** — the
    source does not rate-limit calls: **zero 429 responses in the project's entire
    history**, and `errors: 0` in 274 of the last 375 cycles. But "courtesy to the
    source" pacing was eating **27.5s of every 60** (46%) while real work took ~31.5s,
    so the cycle median was 59s and **32% of cycles exceeded 61s** — with no slack at
    all. And `run_forever` sleeps `CYCLE_SECONDS - elapsed`, so a cycle exceeding its
    budget **extends the same period**: raising caps without cutting pacing slows the
    scan down instead of speeding it up. **The fix**: pacing was cut to ~11s and the
    savings spent on caps (candles were the single biggest waster: 12s of the 27.5).
    The rule: buy time from pacing **before** raising any per-cycle cap.
28. **A column for a field the source never sends stays NULL forever — and passes the
    schema check** — `market_ticks.top10_holders_pct`: **zero of 1,430,475 rows**,
    because the `trending/verified` lists never carry the key at all. The column exists,
    the schema is sound, the checks are green — and there is no data. **The fix**:
    concentration is fetched from two sources that work on both networks
    (`tokenDetails` gives top10 and the holder count, and `/hodlers/top` gives the
    detail from which we derive top1: a single whale is a different risk from ten
    distributed holders), and on-chain concentration fixes it from above. And the
    derived rule: **a column in `training_rows` is added only with a key in
    `ROW_COLUMNS` that writes it** — which is why `dex_protocol` and `is_top_trader_tagged`
    are not in it (they are collection columns on `token_static` and `signal_events`,
    not produced by `build_features`).
29. **A successful but truncated response is more dangerous than a failed one — and with
    a cumulative ledger it never shows** — GoldRush's event endpoint was **paginated**,
    and `_chunk` read `data.items` once without following `links`, while the requested
    range was `GOLDRUSH_BLOCK_CHUNK = 2,000,000` blocks — multiples of a single page for
    any active coin. No 429, no 500, no message: a 200 response and a list of events.
    And the impact is that the balance is **cumulative**, so a missing transfer does not
    decrement a row but flips the balance negative or gives a false concentration after
    thousands of calls: on Base, 41,386 calls · zero snapshots · 76 coins `error` after
    credit exhaustion · and two coins that spent 15,270 and 11,665 calls and ended
    `negative`. **The fix that was**: truncation is detected from three signs
    (`pagination.has_more` · `total_count > len(items)` · `links.prev`/`next`) and raised
    **as a range limit**, so the range shrinks and refetches whole, and at the
    single-block range it raises an explicit error — a half response is never accepted.
    And the converter itself was deleted on 2026-08-19 (the cost was the reason, not the
    code), but the rule remains for the first paginating provider to come
    (`alchemy_getAssetTransfers`, for example): **a paginating provider is asked "is
    there another page?" before its response is read**, otherwise a green check is worse
    than a red one. And `eth_getLogs` on the official RPCs does not paginate — it
    rejects the range with an explicit message — which is half the reason replay stays
    on them.

---

## 11. Operation and maintenance

### The healthy state

```powershell
Get-ScheduledTask -TaskName Fomo* | Select-Object TaskName,State
# Seven tasks must all be Running:
#   FomoApiServer · FomoRecorder · FomoLabeler · FomoChain
#   FomoEVMReplay · FomoBuildRows · FomoDashboard
# FomoBackup = Ready usually, and Running only during the daily copy
```

> **`Running` is not proof of life, and `Ready` is not proof of death.** A task whose
> process died returns `Ready` while nothing complains (trap #15), and a task that
> failed to boot writes not a character to its periodic log. The real check is two
> lines: `<name>_boot.log` and `meta.*_last_run_at` — or the dashboard's top bar, which
> reads them for you.

| What to check | Where |
|---|---|
| Live dashboard | http://127.0.0.1:8090/ — its top bar shows each service's life separately (the labeler's death is silent without it) |
| API health | http://127.0.0.1:8080/health |
| Recorder log | `recorder/recorder.log` (per-cycle stats) |
| Labeler log | `recorder/labeler.log` (written only when labeling) |
| Chain-layer log | `recorder/chain.log` (Solana + EVM + BSC in one process) and `recorder/evm.log` |
| EVM replay log | `recorder/evm_replay.log` + `recorder/evm_replay_run.log` |
| Row-building log | `recorder/build_rows.log` (hourly) |
| Server log | `api/server.log` (rotated 5MB×3) |
| Backups | `recorder/backup.log` + freshness/disk-space status on the dashboard |
| Boot errors | `*_boot.log` beside every service — **check it first** |

### Installing dependencies

```powershell
py -m pip install -r requirements-dev.txt
```

`requirements.txt` covers running the whole workspace, and `requirements-dev.txt` adds
the testing, lint, and type-check tools. There are no machine-specific implicit
dependencies.

### Tests

```powershell
.\run_tests.ps1
```
Eight steps, with no auto-fixing and no file modification — and all must pass:

1. An interpreter pre-check (`pytest`/`ruff`/`mypy` installed for **this** exact
   interpreter).
2. `pytest` for the API suite.
3. `pytest` for the recorder suite.
4. `pytest` for the dashboard suite.
5. `ruff check src/ tests/`.
6. `ruff check recorder/ dashboard/`.
7. `node dashboard/tools/check_key_render.mjs` — the dashboard's JavaScript rendering is
   not touched by `pytest`; the page functions are extracted and run on a payload
   covering every state. **Skipped without node** rather than failed.
8. `mypy src/fomo_api` (full strictness — the API only, for now).

These include future-leak guards, candle-integrity, asset classification, consistent
backup, and schema-drift tests.

**The same script is run by CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))
on `windows-latest` with Python 3.11 on every `push` and `pull_request`.

> ⚠️ **The interpreter check is not pedantry.** `py` on `windows-latest` resolves to 3.14
> where no dependencies exist, so **three red batches passed** (2026-08-11 twice, and
> 2026-08-17) with zero tests run and text implying everything failed. And do not read a
> CI result from `gh run watch` output through a pipe — the pipe's zero hides a failed
> run; read `conclusion` explicitly.

### Backup

`FomoBackup` uses `VACUUM INTO` to a local staging file, so it takes a consistent
snapshot while WAL and the recorder keep running with no restart per write. The copy is
verified with `PRAGMA quick_check` before an atomic publish, and the last three copies
are kept in `%OneDrive%\aoi-backups` outside the repository. To reconfigure the
destination or time:

```powershell
.\recorder\setup_backup_task.ps1 -Destination "D:\aoi-backups" -At "03:15" -Keep 3
Start-ScheduledTask -TaskName FomoBackup   # an immediate copy when needed
```

`AOI_BACKUP_DIR` can point to an external disk or another synced folder. The dashboard
warns if no copy exists, or it is older than 36 hours, or free disk space drops below
25 GB.

### Restarting a service safely

```powershell
Stop-ScheduledTask -TaskName FomoRecorder
Start-Sleep -Seconds 5
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*run_recorder.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }   # trap #15
Start-ScheduledTask -TaskName FomoRecorder
```

### Maintenance scripts (`recorder/`)

They are grouped by what they do, because mixing them is the root of incidents:
**migration** touches raw data, **backfill** rebuilds a column from saved raw data,
**derivation** builds a layer on top of the labeled data, and **analysis** writes
nothing.

#### Migration and backup (touches raw data or files)

| Script | Purpose | Requires stopping the recorder |
|---|---|---|
| `migrate_compress.py` | compress old `raw_json` + VACUUM | yes |
| `backup_db.py` | consistent, verified SQLite copy + keep last 3 | no |
| `reset_evm_replay.py` | return **specific** replay coins to the queue after a path wrote their state — deletion *is* the revert (`evm_replay_state` is fully derived). Display-only without `--apply`, and it refuses `done` because that is finished work, not a stuck state | no |
| `repair_evm_ledger.py` | zero out derived EVM ledger data after fixing its integrity — **it does not touch** raw data, windows, prices, or labels. Read-only by default; `--apply` executes, and `--finalize-training` deletes EVM training rows alone so the builder rebuilds them from corrected snapshots | no |

#### Backfill (rebuilds a column — brings no new truth except where authorized)

| Script | Purpose | Network |
|---|---|---|
| `backfill_extracted_fields.py` | fill columns extracted late from saved raw data (likes · views · num_replies · pinned …) — **they were in `raw_json` from day one** and the extractor never read them, so they were lost to analysis alone, not to the archive | no |
| `backfill_bar_flags.py` | recompute candle corruption flags (trap #22) | no |
| `backfill_sizes.py` | backfill the size fields | yes (stops the recorder) |
| `backfill_thesis.py` | recover thesis history | yes |
| `backfill_bars.py` | the daily price history per coin for a trustworthy ATH. The endpoint truncates at ~900 candles, so we pull at `1D` resolution and page backward by the oldest timestamp returned. The write is idempotent and `historical_bars_state` makes it resumable — **and the series counts for features only when `last_status='ok'`** | yes |
| `backfill_training_ath.py` | upgrade old training rows to the daily ATH feature without rebuilding every feature (version 3 changed the ATH family alone) | no |
| `backfill_activity.py` | recover tradingActivity history (back to 2025-10-22) — ⛔ **do not run it**: the source of the retrospective rows excluded from training (§4, principle 7) | no |
| `backfill_activity_bars.py` | candles for the retrospective events' times (to label them) — same warning | no |

#### Derivation (a layer above the labeled data — no network)

| Script | Purpose |
|---|---|
| `classify_tokens.py` | asset classification: meme/major/priced/stable (trap #23) |
| `build_training_rows.py` | build `training_rows` from labeled outcomes — and `FomoBuildRows` runs it hourly, automatically |
| `relabel_suspect.py` | delete outcomes whose window is corrupted so the labeler recomputes them (requires stopping the recorder) |
| `seed_control_retro.py` | retrospective control from snapshots (requires stopping the recorder) — a sensitivity analysis, not a primary result |

#### Analysis and training (read-only)

| Script | Purpose |
|---|---|
| `phase1_analysis.py` | the phase-1 gate: do signal windows beat the control? generates the `docs/phase1-*.md` report |
| `pattern_analysis.py` | interpretable pattern analysis with per-coin uncertainty — explicitly separates "touched +20% within 24h" from "profitable at the end of 48h" |
| `run_exit_sim.py` | exit-rule comparison + break-even points |
| `train_pipeline.py` | the final training pipeline: time-based walk-forward + coin leakage prevention across folds + a simulation-derived target. Runs on `model_training_rows` and evaluates on new coins alone in addition to all signals, **and refuses the conclusion when a target-derived leak is found in the features** |
| `backtest_strategy.py` | backtest the inferred strategy on the whole archive (train + val + test), with thresholds from `train` alone |
| `export_dataset.py` | export an external analysis bundle (rows + candles + entry price together — the target is the output of a simulation that walks the candles, so whoever holds the rows without the candles cannot rebuild it) |

Migration scripts support `--dry-run`.

> **The ledger's current state**: `meta.evm_ledger_rebuild_required = 1` and
> `evm_ledger_generation = 5` — meaning a fix to EVM ledger integrity has occurred and
> the derived EVM data needs zeroing and rebuilding via `repair_evm_ledger.py`. Draw no
> conclusions from EVM features before this flag drops to 0.

Migration scripts support `--dry-run`.

**Before any training** (in this order): `classify_tokens.py` then
`build_training_rows.py` — **without `--rebuild`**. That flag **deletes** the table
first (see §4); incremental building overwrites the existing row, so on any
`FEATURE_VERSION` bump `FomoBuildRows` alone rebuilds what changed. The reason for the
order is that any recovery of **past** data after a row was built (theses · constants ·
candles · corruption flags · classification) changes its feature values, so rows built
at different times silently become inconsistent — see [docs/PLAN.md](docs/PLAN.md),
phase 3.

### The exit-rule simulator

```powershell
cd recorder; py run_exit_sim.py --cost 0.02
cd recorder; py run_exit_sim.py --source activity --cost 0.02   # on the retrospective set
```

A tool for testing **when do we exit?** — it simulates take-profit, stop-loss,
trailing-stop, and time-limit rules on the candles, and computes the break-even point
against cost.

⚠️ **A tool, not a result.** Current runs are on truncated windows and a sample below
the maturity threshold, so its outputs are exploratory and not a basis for decisions.
Its details and methodology are in
[recorder/README.md](recorder/README.md#exit-rule-simulator-exit_simpy).

---

## 12. Security

- **Credentials**: `api/.privy_state.json` (the rotating renewal token) and
  `.privy_state.json.consumer_key`. Written 0600, **never printed**, and covered by
  `.gitignore`. Delete the file for a full revoke.
- **Auto-renewal**: `TokenRefresher` every 60 seconds without a browser. The fomo token
  lives exactly 60 minutes; the recorder re-reads the disk every cycle (it used to
  freeze it and die 401 once an hour).
- **Local-only ports**: 8080 and 8090 on `127.0.0.1`.
- **The dashboard is read-only**: it opens the database `mode=ro` — no writes to SQLite,
  no disabling of the recorder, and no write path in the server.
- **Read-only toward fomo**: no call writes account or trading state.
- **The dashboard's two guards stay** despite the absence of write paths: a `Host`
  guard against DNS rebinding on every route, and CSRF (session token + `Origin/Referer`)
  for any state-changing request — cheaper than remembering to re-add them at the first
  write route.
- **No Redis** — the system runs on in-memory `FakeRedis`. Sufficient locally; sessions
  are wiped on restart and boot regenerates them.

---

## 13. Detailed documentation

### Standing references

| Document | Contents |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | **What we do after data collection — in detail.** The sole binding reference for modeling and paper-trading thresholds, for the gate decision and the conditions to overturn it |
| [`recorder/README.md`](recorder/README.md) | The recorder and labeler: candles, social, control, compression |
| [`api/README.md`](api/README.md) | The server: authentication, alerts, logs |
| [`dashboard/README.md`](dashboard/README.md) | The dashboard: metrics, design decisions |
| `specs/001-fomo-family-api/` | The original specifications (spec, plan, contracts) |

### Dated reports — **snapshots, not references**

Every file here carries only its publication day's numbers; the database grows every
minute, so every count in them is smaller than today's reality. No number from them is
quoted into a decision — regenerate first.

| Report | Status | Regenerate with |
|---|---|---|
| [`docs/phase1-2026-08-07.md`](docs/phase1-2026-08-07.md) | **The newest** phase-1 run (n=89 signals versus 10 controls · median −0.1%/−0.1% · `U=503`, `p=0.2522` · network imbalance 0.360) — **gate not passed** | `py recorder/phase1_analysis.py` |
| [`docs/phase1-2026-08-02.md`](docs/phase1-2026-08-02.md) | **A record kept on purpose**: the report the "not passed" decision in `PLAN.md` was built on; it stays so the decision can be reviewed at its source | same |
| [`docs/pattern-analysis-2026-08-08.md`](docs/pattern-analysis-2026-08-08.md) | **The newest** pattern analysis (4,097 valid independent signals · 346 coins) | `py recorder/pattern_analysis.py` |
| [`docs/pattern-analysis-2026-08-07.md`](docs/pattern-analysis-2026-08-07.md) | **Superseded** by the next one; kept so the two snapshots can be compared, not to be read alone | same |

> Why keep a superseded snapshot at all? Because the difference between two consecutive
> snapshots is the only measurement of a result's stability: the 08-07 analysis covered
> **4,215** signals and **322** coins, and the 08-08 analysis covered **4,097** and
> **346** — the signal count **fell** while coins matured by a day. A number that moves
> like that between two days is not a basis for a decision, and keeping the old snapshot
> is what shows it.
