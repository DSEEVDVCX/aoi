# Historical data recorder (recorder)

> Part of the **aoi** project — see the [main document](../README.md) for the full
> architecture and the [post-collection plan](../docs/PLAN.md). This folder holds the
> **recorder** (`FomoRecorder`), the **labeler** (`FomoLabeler`), and the **training rows builder**
> (`FomoBuildRows`).

It collects fomo.family data over time to train a model that predicts which coin will
rise. The system reads fomo live but does not **persist** state over time; this
recorder fills that gap: it captures **the state at the moment of the signal (t=0)**
and then **follows the market every minute for 48 hours**, so the outcome (did it
rise?) can be derived later in the labeling stage.

**The recorder and labeler are running now** (`FomoRecorder` + `FomoLabeler`). The
exploratory v1 model and exploratory paper trading start at 100 mature v3 controls;
the production model and the new paper-trading run wait for 500 controls, 14 days,
and passing the phase-1 gate — the detailed plan is in
[docs/PLAN.md](../docs/PLAN.md). The past cannot be recorded (except partially, see
below) — every day of delay = data lost.

## Why it reads the raw payload directly

`FomoClient._map_trending_token` drops the most valuable fields (change5m…24,
volume…, txnCount, buy/sellCount, uniqueBuys, holders, top10HoldersPercent, mintable,
freezable, creatorAddress, socialLinks, circulating/totalSupply). So the recorder
calls raw `_get`/`_post` **before any mapping** and extracts the fields itself in
`extract.py`.

## Governing principles

- **Raw, always**: every row stores `raw_json` beside the extracted fields — any
  feature we discover later is re-derived from the archive without losing the past.
  The raw payload is **zlib-compressed** (see "Raw storage" below) — it is never read
  with `json.loads` directly.
- **No future leakage**: the recorder computes no label — it records raw data with a timestamp only.
- **Survivorship-bias resistance**: every coin that entered via a signal is watched
  for 48 hours regardless of outcome — we record losers the same as winners.
- **No fabrication (FR-007)**: a missing field = NULL, not an assumed zero. False is
  stored explicitly (freezable=0 means "safe", distinct from NULL "unknown").
- **Read-only (FR-012)**: all GET/POST calls are reads (feed, trending, verified,
  leaderboard). No call writes account state or trades.
- **Crash survival**: any failed call (502…) is recorded in `meta` and skipped; the
  loop does not die. A final shield around the whole cycle catches any unexpected crash.

## Signals (triggers)

`multi_user_buy` and `large_buy` are what **admit** a coin into monitoring.
`multi_user_sell` and `large_sell` are recorded as context but trigger no monitoring
(the latter confirmed live on 2026-07-28 — a single whale distributing with the same
shape as `large_buy`, in the opposite direction). Hype/warnings are features, not
triggers. The labeler labels **all** signals whenever candles are available, so a
"whale/leader sold → what happened to the price" record accumulates for free —
input for the exit model later (see [docs/PLAN.md](../docs/PLAN.md) §2.3-k).

**"More than one leader bought" signal**: for each event we match `topTraders[].id`
against the leaderboard (loaded hourly) and record `top_trader_match_count` (how many
leaders bought) and `buyers_best_rank` (best rank) — a stronger signal than generic
multi-buy.

**Four leaderboards, not one** (since 2026-08-10): the source caps `/v2/leaderboard`
at **50** leaders and silently ignores every pagination form — nine forms probed live
(`limit`/`offset`/`skip`/`page`/`pageNumber`/`start`/`from`/`pageSize`/`take`),
and the id lists are byte-identical, so `LEADERBOARD_SIZE = 200` had no effect since
it was written. But `/24h` and `/7d` and `/30d` each return **100** (measured on the
archived raw): the union is **214** distinct traders, 164 of them unknown to the main
leaderboard. Measured on 7,200 real buy events from three days: the fifty match 265
events (3.68%) and the union 1,099 (15.26%) — **×4.15**. Ranks are **separated by
period** (`buyers_best_rank_24h/_7d/_30d`), not merged into one number: rank 7 in
`24h` is not rank 7 in `totalPnL`, and merging the two mixes two different
measurements. `top_trader_periods_matched` is added too (in how many of the four
leaderboards a buyer appeared) — a leader in all four is a record; a leader in `24h`
alone may be a lucky hit. And `best_rank_any_period` ranges 1-100, not 1-50, so
`rank_le_50` regains its meaning on the new family.

⚠️ **The past is not backfilled**: the archive saved only `totalPnL`, so there is no
history for period ranks. The v7 family stays NULL in every earlier row and is
measured in the new. No working around this; a workaround is retroactive filling,
which is rejected (`is_live=1` only).

## Tables (`schema.sql`)

- `watchlist` — what we monitor, the entry source, and when it ends (48 hours). An
  active coin is not extended by a new signal (first sighting is the reference), but
  an **expired coin returns** with a fresh window if a later signal points at it —
  without that the list would bleed to zero.
- `signal_events` — the decision moment t=0 with leaderboard matching and the raw payload.
- `market_ticks` — a full market snapshot, but **only for coins appearing in trending/verified**.
- `token_bars` — 5-minute OHLCV candles for every watched coin (the price source of
  truth for labeling), **and hourly candles for the SOL/WETH/WBTC macro benchmarks**
  (pulled hourly since 2026-07-28 — the system's market reference; they have no
  watch, so the labeler does not touch them). The `h_suspect`/`l_suspect`/`c_suspect`
  columns flag impossible values from the source (see "Candle integrity" below) —
  the raw value stays exactly as received.
- `bars_fetch_state` — state of the last candle pull per coin (drives the rotating schedule).
- `token_static` — coin constants (mint/freeze/creator/socials), once at entry.
- `snapshots` — periodic raw archive per source (feed/trending/verified/**leaderboard**
  — the raw leaderboard hourly since 2026-07-28; before that it was read and
  discarded, losing the leaders' path forever). And since 2026-08-10 each period has its own source
  (`leaderboard` · `leaderboard_24h` · `leaderboard_7d` · `leaderboard_30d`) —
  merging them into one source mixes four lists into an archive that cannot be
  untangled. **The archive is fresh-only**: a period that fails in a given hour does
  not get its old envelope re-stamped with that hour's timestamp, or the archive
  would contain a leaderboard never fetched at its time — a contamination that is
  never detected later.
- `token_social` — a time series of social momentum per watched coin.
- `token_thesis` — one row per thesis with its write timestamp (enables **historical** counting).
- `activity_events` — historical tradingActivity events (backfilled to 2025-10-22):
  `multi_user_*` events **with the same ids as /feed** + individual
  `swap_buy/swap_sell` events with a `usd_amount` the feed does not show. Filled only
  by `backfill_activity.py` (not the periodic recorder), so the forward-collection
  set in `signal_events` stays pure.
- `token_class` — the asset class per coin (`meme`/`major`/`priced`/`stable`): fomo
  is a multi-asset platform, and the archive holds BTC, ETH, SOL, gold, and tokenized
  stocks (AAPL/MSTR/HOOD/INTC/META/SNDK/MU). Derived entirely from observations via
  `classify_tokens.py` ⇒ rebuilt whenever we want. **Any analysis or training
  restricts to `asset_class='meme'`** (PLAN §2.1) — the classification is for
  separation, not deletion.
- `social_fetch_state` — state of the last social pull (drives rotation).
- `token_holders` — ownership concentration from **two sources measuring different
  things** (do not mix them): `source='token_details'` gives `top10_pct`/
  `holder_count` from the chain, and `source='hodlers_top'` gives the positioning of
  **the fomo platform crowd** (value · who is underwater · median holding time ·
  developer presence) with no supply ratio at all. One row per (coin, source, timestamp).
- `holders_fetch_state` — state of the last holders pull (drives rotation, step 2.42).
- `outcomes` — the outcomes/labels: filled by the **labeler** after the window matures (see below).
- `phase1_watch_outcomes` — **the safe analysis view**: shows only watch outcomes with
  `design_version>=3` and `analysis_eligible=1`. The old control was deleted from the
  running database; do not use `outcomes WHERE kind='watch'` directly in any new comparison.
- `meta` — run state, counters, last error per source, feed freshness.

The current comparison design `v3` requires one universe for both sides: a signal
window is accepted into the comparison only if the coin appeared in
`trending/verified` in its admission cycle, and `admission_source` and the very
listing price are saved. The control is admitted only in cycles that admitted a
comparison signal, and matched cumulatively on network and source. The earlier `v2`
data is quarantined and ineligible.

## The social layer

Meme coins are moved by crowds, and we were recording price, volume, and holders but
not **who was talking about them**. The `token_social` table captures
`GET /feed/token/thesis` for every watched coin every ~30 minutes
(`SOCIAL_PER_CYCLE`, `SOCIAL_REFRESH_SECONDS`).

### The saturation trap (fixed — do not reintroduce it)

The response returns **at most 100 items** while `responseObject.count` carries the
real total — **10,549** theses have been seen for a single coin. Counting items alone
saturates, so a coin with 10,549 theses looks identical to one with exactly 100 —
a waste of the strongest discriminator in this layer. Therefore:

| Column | Meaning |
|---|---|
| `thesis_total` | The **real** count from the envelope — use this for comparisons |
| `thesis_sampled` | How many items we actually saw (≤100) |
| `has_next_page` | Whether the sample is truncated |
| `thesis_likes/replies/authors/holder_authors` | Computed on the **sample**, not the whole |

Do not compare `thesis_likes` with `thesis_total` as if they were on the same scale.

`thesis_authors` counts distinct authors, not theses: ten theses from one person is
not social momentum. And `holder_authors` is who actually holds a stake (`equity>0`).

**Silence is recorded, not skipped**: a coin with no discussion is written with
zeros, and a run of zeros followed by a sudden spike is exactly what we want to
capture. That is why a silent coin is not excluded from the cycle (unlike candles,
where repeated `no_data` excludes it).

## Trade size fields

`large_buy` was broken: the size was never extracted at all, so a $995 trade and a
$172,169 one looked identical to the model — even though **the median of what fomo
calls a "large buy" is just $3,448**. The columns now: `size_usd` (position size
after the buy), `in_amount` (what was actually paid), `in_token_address`,
`out_amount`, `token_amount`, `realized_pnl_usd`. The difference between `size_usd`
and `in_amount` distinguishes "added 3k to a 42k position" from "entered with 45k
in one go".

The whole archive was migrated retroactively from `raw_json` via `backfill_sizes.py`.

## The labeler — the separate FomoLabeler process

It fills the `outcomes` table from candles **after the 48-hour window closes + a
15-minute margin**. A separate process from the recorder by design: the recorder
records raw only, and computing any value derived from the future at record time is
leakage. Files: `labeler.py` (pure, testable logic) + `run_labeler.py` (a
15-minute loop, the run_recorder pattern).

### The key (kind, key) — why it changed

The old `(token, entry_ts)` would inevitably collide: 201 signals on a single coin. Now:
- `kind='signal'`, key = the signal id — **one training row per signal**.
- `kind='watch'`, key = `token:network:first_seen` — for every monitoring entry
  **including the control** — to compare signal/control on completed windows, not running ones.

### Definitions (all inside `compute_labels` — a pure function)

| Field | Definition |
|---|---|
| `entry_px` | Close of the **first candle at/after** the signal moment (delay ≤30min, else `no_entry`) |
| `max_gain_Xh` | Max `high` over the candles **strictly after the entry candle** ÷ entry −1 |
| `max_drawdown_48h` | Min `low` in the window ÷ entry −1 |
| `final_return_48h` | Close of the last candle in the window ÷ entry −1 |
| `is_rug` | Final return ≤ −90% |
| `bars_truncated` | The series ended more than an hour before the window's end — **the coin dying is a signal, not a gap** |
| `is_independent` | First signal for the coin, or ≥30min after the previous one (69% of signals are <5min apart — false repetition, taught not deleted) |
| `split` | train/val/test (70/10/20) split by **coin address** — all of a coin's signals in one part, or the model memorizes coins and validation becomes an illusion |

The entry candle's own peak **does not count as gain** (it may precede a hypothetical
fill), and candles before the signal never enter the calculation at all (reverse leakage).

### Operations

```powershell
Get-ScheduledTask FomoLabeler | Select State
Get-Content recorder\labeler.log -Tail 5      # written only when it labels something
py run_labeler.py 1                            # one manual cycle
```

## The feature extractor (`features.py` + `build_training_rows.py`)

It turns the scattered archive into **one training table**: a row per decision, a
column per feature known **at t=0 or earlier**, with the label attached from `outcomes`.

**The one rule**: every query is explicitly time-bounded. Tests plant data after t0
(thesis · candle · market tick · signal · new coin for the deployer · **a holders
snapshot** · **an exchange listing**) and assert that the row **does not change** —
these tests are the only line of defense against illusory accuracy that is discovered
only with money. With them a structural guard: no label column among the features,
and no feature named likes.

**120 features in 9 families**: the event (size · market cap · leader rank · block
composition · buyer cost average · ticker text) · coin constants and the deployer's
fingerprint **plus its external legitimacy** (number of platforms · listings · CMC
presence · description length · banner) · **ownership concentration and platform-crowd
positioning** (`chain_top10_pct` · `chain_holder_count` · `platform_penetration`,
the platform's reach among holders · `platform_underwater_ratio`, who is underwater ·
median holding time · developer presence) · **social momentum from two sources**
(a historical count from `token_thesis` + **the real count, owning authors, and
momentum deltas** from a `token_social` snapshot before t0) · **the price and volume
path before the signal** (1h/4h/24h/7d returns · volatility · `flat_ratio_24h` for
calm before the explosion · volume burst `vol_surge_1h` · distance from the peak) ·
**the market snapshot with its short windows** (change/volume/txn at 1h/4h + pool
turnover and float ratio) · the market regime (SOL/ETH) · event density from both sources.

**The holders family is empty now, and that is correct, not broken**: holders
collection started 2026-08-09, and the 48-hour maturity window means the newest
mature outcome at t0 = 2026-08-07 — that is, **before** the first holders snapshot.
So the time bound rightly returns NULL, and "fixing" it by pasting today's snapshot
onto yesterday's decision is not allowed (that is explicit future leakage). The
family starts filling automatically as post-2026-08-11 outcomes mature. The features
themselves are verified on live data (6 coins · 12 rows · zero errors).

**One piece for training and serving**: the same `build_features` will compute the
live signal row — a computation difference between training and serving
(train/serve skew) breaks the model silently.

The safe training interface is `model_training_rows`, not raw `training_rows`. The
former enforces automatically: `kind='signal'`, `is_live=1`, `asset_class='meme'`,
`status='ok'`, `is_independent=1`, and `feature_version>=2`. The second version
rejects the candle still open at decision time and the open hourly macro, and rejects
`token_static` recorded after `t0`.

**Running automatically since 2026-08-11** via the scheduled task `FomoBuildRows`
(`run_build_rows.py`, AtLogOn, an hourly loop from `BUILD_ROWS_INTERVAL_SECONDS`).
This was the pipeline's only manual stage, so `training_rows` used to freeze on an
old feature version with no warning at all while labeled outcomes piled up.

The cycle calls the same incremental build in batches up to `BUILD_ROWS_MAX_PER_CYCLE`
(4000) — the cap is deliberate: raising `FEATURE_VERSION` makes ~53k rows pending at
once, and biting across cycles avoids competing with the recorder and labeler for the
write lock for ~90 minutes. **Never `--rebuild` in the scheduled path** (an automatic
delete with no human hand).

```powershell
py run_build_rows.py --check-config      # verify before registering
py run_build_rows.py 1                   # one cycle manually
Get-ScheduledTask FomoBuildRows | Select State
Start-ScheduledTask -TaskName FomoBuildRows
```

Its logs are `build_rows.log` (written only on an actual build) and
`build_rows_boot.log`, and its pulse is `build_rows_last_run_at` /
`build_rows_last_stats` in `meta`.

Manual runs remain available for reports and special cases:

```powershell
py classify_tokens.py                   # update the classification first (asset_class for the row)
py build_training_rows.py --dry-run     # coverage report without writing
py build_training_rows.py               # incremental build
py build_training_rows.py --model-candidates-only  # independent live only (quick analysis)
py build_training_rows.py --rebuild     # wipes the table first — you do not need it for a version bump
```

**Raising `feature_version` needs no `--rebuild`**: the primary key is `(kind, key)`
and the insert is `INSERT OR REPLACE`, so the ordinary incremental build picks up
older-version rows and replaces them **in place** — measured: the count held at
20,303 rows while the v4 counter rose. As for `--rebuild`, it starts with
`DELETE FROM training_rows`, so save it for one case: evicting rows that are no
longer eligible. The difference is not cosmetic — wiping the table and then stumbling
mid-build leaves the database poorer than it started.

**Read the coverage report after every build**: a 100% empty column is either source
emptiness or a defect on our side. Three lessons measured from the audit:

1. `token_age_h` was completely empty because fomo stores `token_created_at` as a
   numeric **epoch**, not ISO (fixed; coverage 96.9%).
2. **A filled column can still be wrong**: we were using the buy/sell counters
   (24% coverage) and neglecting `change_*`/`volume_*`/`txn_*` (**100%** coverage
   over 588k ticks).
3. **A correct column can be saturated**: `token_thesis` is a sample that stops at
   ~400, while the truth is in `token_social.thesis_total` (reached 27,559) — so the
   count is now flagged with `thesis_counted_capped` alongside the real total.

And `mintable`/`freezable`/`top10_holders_pct` are genuine source emptiness, not a
defect. And an impossible negative age (a listing timestamp, not creation) stays
None, not a negative number for the model to learn.

## Uptrend and noise pattern analysis (`pattern_analysis.py`)

It extracts interpretable rules from `train` only, then measures them on `val` and
`test` and on days separately, and computes confidence intervals by bootstrapping at
the **coin** level. It separates touching +20% within 24 hours, the final close
after 48 hours, and an exit simulation that walks the candles in order (+20% target,
−30% stop, 24-hour exit, 2% round-trip cost) — an analysis tool only; the official
training target since 2026-08-28 is `is_explosive` (see above).

```powershell
py pattern_analysis.py
# output: docs/pattern-analysis-YYYY-MM-DD.md
```

After reading the test report, the current `test` sample is spent; do not tweak the
rules and then call it an independent test again. Freeze the rules and wait for new
data in time.

## Asset classification (`classify_tokens.py`)

fomo is **multi-asset**: the archive holds BTC ($1.27T), ETH, SOL (103 signals),
PAXG gold, and tokenized stocks (AAPL $339 · SNDK $1013 · MU $958 · MSTR · HOOD ·
INTC · META) plus stablecoins. The measured classification: **686 meme · 21 major ·
14 priced · 4 stable** out of 725 coins.

The rules in order (`extract.classify_asset`, its thresholds in config):

1. `stable` — all observations inside [0.95, 1.05].
2. `major` — market cap > $1B, **subject to credibility**: above $5T it is ignored
   ($69T was seen for a coin at $0.0888 = price × mythical supply) and price decides.
3. `priced` — price > $5: tokenized stocks and commodities. **Market cap does not
   reveal them** (AAPL at just $1.36M because the tokenized share is a tiny fraction
   of the stock) — price alone reveals them.
4. A known name (BTC/ETH/XRP…) **subject to a credible value** — memes forge tickers
   (measured: "BTC" at $3.5M and "SOL" at $4.9M — they stay memes).
5. `meme` — the rest, and it is the project's target.

```powershell
py classify_tokens.py --dry-run   # the distribution + every non-meme with its reasons
py classify_tokens.py             # write/update token_class
```

**Mandatory usage**: any analysis/training restricts to `asset_class='meme'`
(PLAN §2.1). The audit proved the retroactive numbers were unaffected (5 rows of
487) but the forward collection is more contaminated (SOL alone is 22 outcomes).

## Candle integrity — impossible values from the source

fomo sometimes returns impossible prices in `getBarsNew`. Two measured cases
(2026-07-30, **confirmed by a live re-pull ⇒ a permanent defect in the source, not a
transport error on our side**):

| Field | Value | Context |
|---|---|---|
| `h` | **2,626,092.02** | candle close 0.0219, neighbors 0.0040/0.0202 (×119 million) |
| `c` | **12,052.5** | between two closes ≈0.0004 — then it returns at once |

The impact before the fix: `max_gain` reached **+3.78 billion %** in 4 outcome rows,
and the dashboard showed **+62,570,743,609%** for a coin whose real rise was +1000%.

**The verdict: price is continuous across the aggregate** (`extract.bar_context_flags`)
— a candle's close is the next one's open, so a value exceeding **both** neighbors by
×10 (`BAR_WICK_MAX_RATIO`) that does not persist = distortion, not price. The
neighbor is the reference, not the body, because distortion hits the close itself,
stretching the body so the tail looks plausible. And requiring neighbors on **both
sides** prevents flagging the start of a real trend (a jump that persisted = price ✅
· a collapse that persisted = a correct rug ✅).

- `h_suspect`/`l_suspect`/`c_suspect` are flagged and excluded from computation only
  — **never fixed or deleted** (the raw is sacred, and the flag is always recomputable from it).
- Labeler: the peak/trough come from clean candles, and the final return and entry
  price from a **clean close** (a distorted entry close is skipped to the first clean
  one), and `outcomes.suspect_bars` counts the distorted candles in the window so
  the affected rows are known and not read as pure.
- The verdict needs the next candle, so `db.recompute_bar_flags` is called after
  every insert (the candle cycle, the macro, the retroactive candle backfill).
- The ×10 threshold is measured on 652,342 candles: only 132 exceed ×2, 39 exceed
  ×10, and 22 exceed ×1000 — legitimate tails practically end below ×2.

```powershell
py backfill_bar_flags.py --dry-run   # recompute the flags for old candles (no network)
py backfill_bar_flags.py
py relabel_suspect.py --dry-run      # outcomes whose window is distorted (deleted to be recomputed)
py relabel_suspect.py                # + a copy of the old values in a JSON file
py run_labeler.py 1                  # recompute immediately instead of waiting for the cycle
```

## Activity history backfill (`backfill_activity.py`)

The forward `/feed` is "latest only" (five pagination parameters ignored — proven
live), but `GET /feed/tradingActivity?lastId=` — the one the SPA itself uses — pages
back to the **end of history (2025-10-22)**. The first pull (2026-07-28) fetched the
whole universe: **1151 events**, of which **819 `multi_user_buy` across 408 coins
over 9 months** — the central thesis sample retroactively instead of waiting 71
days. The overlap with `signal_events` (43 events) proves it is the same universe;
merge by id when modeling.

**What it does not cover**: the `large_buy` flood (~1138/day) is not on this path —
it stays exclusive to forward collection. And individual `swap_*` events go back
only two days. And the leader's rank at event time is historically missing
(derivable from the leaderboard archive for the future only).

A resumable scraper (checkpoint in `meta`), ×3 retry on transient errors, idempotent:

```powershell
py backfill_activity.py --pages 30              # stops automatically at the end of history
py backfill_activity.py --until 2026-06-01      # or until a date
py backfill_activity.py --pages 5 --dry-run     # measure without writing
py backfill_activity.py --reset                 # from the newest again
```

**Retroactive labeling** (implemented): `backfill_activity_bars.py` pulls 5m candles
around each coin's event times ([ts−1h, ts+48h+margin] in merged clusters ≤72h/call),
and records its state in `activity_bars_state`. The labeler labels `activity_events`
with the same `compute_labels` and the same independence flag — but **only after its
coin reaches status='ok'** (a gate in `activities_pending_label`) so no eternal
no_entry is written before the candles arrive. A coin with no series at fomo is
labeled `no_data` and excluded after 3 attempts — **its share is the survivorship
bias of the retroactive set** (dead coins = the surely-missed rug cases); read it in
any conclusion, do not hide it. The outcomes enter `outcomes` with
`kind='activity'` (and no retroactive control is possible: the un-signaled
historical universe is unknown — a constraint on any control comparison that
includes them).

## Exit rule simulator (`exit_sim.py`)

> **Abandoned as a training target (2026-08-28).** The decision: a measurement of
> 14,870 rows proved that the TP+20%/SL−30% strategy is marginal (+0.14%/trade,
> eaten by fees and slippage) — it cuts the winners' wings (16% of signals rise
> +100% while the target exits at +20) and lets the losses walk on.
> **The training target is now `is_explosive`** (predicting the catchable
> explosion: peak ≥2x with half of it within 24h) in `train_pipeline.py`, and
> compared with an alternative window (buy everything, sell at 2x): +2.57% vs
> +0.14% — 18x better. The simulator remains a reference analysis tool
> (`--target win_trade` in the pipeline), not a basis for decisions.

**The hypothesis it was testing** (a hypothesis, not a result): catching the peak is
impossible in advance, so perhaps a modest fixed target was closer to attainable. — The hypothesis was measured and answered by the history above.

> ⚠️ **Do not build a decision on its current outputs.** Runs so far are on a sample
> of tens of trades with truncated follow-up, **without a single completed 48-hour
> window and without a control comparison**. (The tool prints its actual maturity
> state before every table — read it there, not here.) Far targets are structurally
> under-counted (the time has not passed), and the observed difference may be a
> property of those hours' market, not of the strategy. The numbers change on every run.

**When the outputs become meaningful**: after the windows complete (phase 0) and the
control group matures (phase 1). Run the simulator **on both groups and compare** —
if the fixed profit also beats the control, the effect is real; otherwise it is a
market property, not a strategy.

### Why we walk the candles instead of reading `outcomes` columns

`max_gain_48h` and `max_drawdown_48h` **do not carry their order**. A coin fell
−40% then rose +50%: with a −30% stop you are out at a loss, without a stop you are
a winner — and the two columns are identical in both cases. **The path is the
outcome**, which is why the simulator walks the candles (pinned by a test: two paths
with the same peak and trough and two opposite results).

### The conservative assumption inside a candle

A candle gives `high` and `low` with no time order. If the target and the stop are
both touched within the same candle, we assume **the stop first** — the worse of the
two possibilities, so the results are a floor, not an exaggeration.
(`optimistic_same_candle=True` flips it, to measure the effect's size only, not for reporting.)

### The decisive test: cost

A theoretical edge means nothing if slippage swallows it. `breakeven_cost` computes
the highest round-trip cost at which the rule still profits — and the current result
is **1.9% – 3.2%** depending on the rule, while **a third of the coins have
liquidity under $50k**. A rule that breaks even only below ~3% is not practically
executable no matter how pretty it looks on paper.

### Usage

```powershell
py run_exit_sim.py                # the default set (forward collection) + breakeven points
py run_exit_sim.py --cost 0.02    # with a 2% cost per round trip
py run_exit_sim.py --all-signals  # all signals, not the first signal per coin
py run_exit_sim.py --source activity --cost 0.02
                                  # retroactive: multi_user_buy from activity_events
                                  # (382 first-per-coin trades — no control possible)
```

It only reads — safe alongside the running recorder. By default **the first signal
per coin** (14.2 signals per coin and a median gap of 1.1 minutes; simulating them as
separate trades inflates the result meaninglessly).

⚠️ The numbers above are on **incomplete windows** (longest follow-up ~25 of 48
hours), so far targets (+50% and up) are **under-counted**. And no control comparison yet.

## What was recovered retroactively and what is lost forever

The "raw, always" principle in the original design is what made recovery possible:
the fields were not lost, merely **unextracted**.

| Data | Status | Tool |
|---|---|---|
| Trade size fields | ✅ 100% recovered | `backfill_sizes.py` |
| Candles | ✅ deeper than the archive itself (back to 2025-11) | automatic |
| Historical thesis count | ✅ recovered via pagination | `backfill_thesis.py` |
| A control group for the signals period | ✅ built from `snapshots` | `seed_control_retro.py` |
| **Likes/replies at signal time** | ❌ **lost forever** | — |

**The only real loss**: fomo gives only the **current** like count, with no
historical record. A thesis with 50 likes today — how many did it have at the moment
of the signal? No way to know. That is why `token_thesis.fetched_at` is recorded: it
is when the likes were measured, and `num_likes` must not be read as a value from
write time. From now on the time series in `token_social` solves the problem for the
future.

`token_thesis` (one row per thesis with its write timestamp) makes "how many theses
existed at the signal moment" a simple query — `db.thesis_count_before(token, at)`.

## Source feed freshness

`meta.last_feed_event_at` holds the newest event's timestamp. **The recorder can run
without errors while fomo's feed is frozen** — it was seen frozen for 3 hours while
every cycle succeeded. Without this stamp the archive looks like a "quiet market"
when it is really a source outage, and the difference is decisive for any time-based
analysis. The dashboard shows it in the top bar.

## The control group (the negative class)

**The problem**: every coin with a price series was entering via a signal (90 of
90). So the model can learn "any signaled coin rises more", but it can **never**
answer "does the signal mean anything at all". You may find 41% of your signals
profitable, then discover 41% of the market was profitable in that period — the
signal adds nothing, and there is no way to find out.

**The solution**: coins enter monitoring **by random selection, not by signal**,
with the column `watchlist.is_control = 1` and `source = 'control'`, recorded with
the same window (48h), the same candles, and the same ticks. Tuning in `config.py`:
`CONTROL_GROUP_SIZE` (40), `CONTROL_PER_CYCLE` (2), `CONTROL_WATCH_HOURS` (48).

### Validity conditions of the comparison (all deliberate, in `admit_control_sample`)

- **Random, not ordinal**: taking from the head of the trending list picks the
  highest volume, so the difference from the signaled coins becomes a **volume difference, not a signal difference**.
- **No peeking at the future**: selection depends only on what is known at the moment.
- **Everything ever signaled is excluded**, even if it never entered monitoring
  (`signalled_tokens`) — otherwise it stops being a control.
- **In installments**: two coins per cycle. Taking all forty at once makes them a
  sample of a single market moment, mixing the signal's effect with that moment's effect.
- **One-way promotion**: a control coin that receives a signal becomes a signaled
  coin with a fresh window; the reverse is forbidden (`admit_control` never touches an existing row).
- **A separate cap**: `WATCHLIST_CAP` applies to the signaled coins alone, so the control does not crowd them.

The dashboard shows the comparison in "signal vs control group" with an explicit
warning as long as the sample is under 20 per side.

## Candles (OHLCV) — the price source of truth

`market_ticks` **is not enough for labeling**. Its sources are the
`trending`/`verified` lists, which are two curated lists of ~46 and ~37 coins, not a
price digest for every coin. Measured on the live archive: **21 of 84 watched coins
(25%) never got a single price point at all**, and the rest had coverage of only
52–75% of minutes. That is, a quarter of the sample was impossible to label.

`token_bars` fills the gap via `POST /proxy/getBarsNew`. Measured after activation:
**~100% coverage** of the five-minute grid, and history extending to **11 days
before** the recorder itself started.

### Critical details confirmed live (2026-07-26)

- `symbol` **must** be `"<address>:<networkId>"`. A bare address makes fomo's server
  throw a **502 from Cloudflare** — it looks like a fomo outage when it is really a malformed request.
- `from` and `to` **are mandatory** (without them `400 "body.from - Required"`).
- A single call caps at **900 candles**; at 5 minutes = 75 hours, covering a 48-hour window.
- **Candle availability decays**: a coin returned 192 candles, then 15 minutes later
  `404 No OHLCV data available`. So **do not delay the pull** assuming history will
  remain available. A coin that returns `no_data` three times is excluded
  (`BARS_MAX_NO_DATA_ATTEMPTS`).

### The rotating schedule

Pulling 84 coins every minute would burden the loop and flood fomo. Instead, each
cycle takes a slice of `BARS_PER_CYCLE` (=6) of the **stalest-pulled-first** coins,
so the sweep completes over ~14 cycles (~14 minutes) and every coin refreshes every
`BARS_REFRESH_SECONDS` (=900). A coin never pulled goes before everyone, so the
entry backfill happens immediately.

We always request the full window from (first sighting − two hours of context), not
from the last candle: the last candle is still forming and gets revised (insert with
`OR REPLACE`), and the full pull heals any earlier gap automatically.

## Raw storage (zlib compression)

The `raw_json` columns hold a **zlib-compressed BLOB**, not text. Always read them
through `db.decode_raw()` — it decompresses and also accepts the old rows written as text:

```python
from db import decode_raw
raw = decode_raw(row["raw_json"])   # <- never json.loads(row["raw_json"])
```

**Why**: a full snapshot of `feed`+`trending`+`verified` is written every minute
(~310 KB each), so the database reached 720 MB within 20 hours, growing ~1.3 GB
daily, with `snapshots` alone 80% of it. Compression is **lossless** and brings that
down to ~220 MB daily. The key `meta.raw_encoding = "zlib"` documents the active encoding.

The existing archive was migrated once via `migrate_compress.py` (739 MB → 184 MB).
The migration verifies every row before writing and is resumable; it requires
stopping the recorder:

```powershell
Stop-ScheduledTask -TaskName FomoRecorder
py migrate_compress.py --dry-run   # estimate the gain only
py migrate_compress.py             # migrate + VACUUM
Start-ScheduledTask -TaskName FomoRecorder
```

### Live backup

`backup_db.py` uses `VACUUM INTO` to a local staging file and does not need the
recorder stopped. This pins a snapshot instead of re-copying 5.5 GB on every WAL
write, then checks it with `PRAGMA quick_check`, publishes it atomically, and keeps
only the last three copies. The daily task `FomoBackup` is created via:

```powershell
.\setup_backup_task.ps1   # default: %OneDrive%\aoi-backups, daily 03:15
```

The destination must be outside the project; set `AOI_BACKUP_DIR` for an external drive or a synced folder.

**Retention**: `config.SNAPSHOT_RETENTION_DAYS = 0` (no deletion) by default —
snapshots are the re-derivation archive, so deleting them is an explicit decision.
Set it to a number of days to enable periodic pruning. The dashboard shows "database
size" and "daily growth" to monitor this.

## Files

| File | Role |
|-----|------|
| `config.py` | paths, cadences (60s), watch duration (48h), the cap (150), macro assets, the `api/src` link |
| `db.py` | schema + WAL + idempotent inserts + raw compression + candle scheduling (no network) |
| `migrate_compress.py` | one-time migration: compress the existing `raw_json` + VACUUM |
| `backup_db.py` / `setup_backup_task.ps1` | a verified live copy + a daily task outside the repository |
| `extract.py` | pure raw → row transforms (testable without network) + the candle integrity verdict (`bar_context_flags`) + asset classification (`classify_asset`) |
| `leaderboard_cache.py` | the **raw** leaderboards for four periods (all/24h/7d/30d): a separate id→rank map for each + raw of each for the hourly archiving into snapshots |
| `recorder.py` | the main loop (`run_cycle` callable once or repeatedly) |
| `labeler.py` / `run_labeler.py` | outcome computation (pure logic) + the 15-minute labeling service |
| **`features.py`** | **the feature extractor: 120 features bounded by t=0 — one piece for training and serving** |
| `build_training_rows.py` / `run_build_rows.py` | build `training_rows` from labeled outcomes + the coverage report · the hourly build service (`FomoBuildRows`) |
| `pattern_analysis.py` | interpreted rise/noise rules + coin- and day-level validation + exit simulation |
| `backtest_strategy.py` | a full backtest for any threshold rule (momentum pattern by default) over the whole archive + candle simulation + cost robustness |
| `train_pipeline.py` | the training pipeline: **the explosive-coin prediction model** (since 2026-08-28) — target `is_explosive` (peak ≥2x with half of it within 24h, measured on 2,378 explosions) + time-based walk-forward + coin separation + per-coin AUC/bootstrap + economic lift (explosion rate in the top 20%) + a mandatory leakage check. The old target `win_trade` (TP20/SL30) remains an abandoned reference option — its measurement: +0.14%/trade, eaten by fees. ⚠️ `time_to_plus20_min` is a candidate-dropping filter only — **never an entry rule** (entering after +20% is a net loss of -1% to -11%) |
| `classify_tokens.py` | asset classification → `token_class` (meme/major/priced/stable) |
| `backfill_bar_flags.py` | recompute the candle distortion flags from the raw |
| `backfill_extracted_fields.py` | derive late-added columns from the saved `raw_json` — **zero network calls**, and it calls the same `extract` functions so the two definitions cannot drift |
| `relabel_suspect.py` | delete outcomes whose window is distorted so the labeler recomputes them (with a backup copy) |
| `backfill_activity.py` / `_bars.py` | tradingActivity history backfill + candles for its times |
| `backfill_bars.py` | resumable 1D candle backfill for all coins to compute the full ATH |
| `backfill_training_ath.py` | upgrade ATH in old training rows after the daily history completes |
| `exit_sim.py` / `run_exit_sim.py` | the exit rule simulator (`--source activity` for the retroactive set) |
| `run_recorder.py` | the scheduled task's launch point (`serve.py` pattern) |
| `tests/` | tests on real raw shapes and db logic (no network) — including **63 guard tests** |

## Running

It depends on a valid credential in the api state file (`api/.privy_state.json`) —
generated by running the api service. The recorder reads `session_token` from it directly and never prints its value.

```powershell
python run_recorder.py        # an endless loop (every 60 seconds)
python run_recorder.py 1      # a single cycle (a live check)
```

## Tests

```powershell
python -m pytest recorder/tests -q      # 371 tests, no network
```

The most important for modeling: `test_features.py` (**63 guard tests** — plants
data after t0 and pins that it does not appear, including the new-snapshot guards:
holders and exchange listing), `test_holders_cycle.py` (source separation · the
refresh window · raw archiving), `test_bar_integrity.py` (18 — the source's
distortion with its real numbers), `test_asset_class.py` (18 — ticker forgery and
tokenized stocks).

## Empty or constant columns — a measured classification (2026-08-09)

A full inventory over **48,287 training rows × 120 features**. 33 columns looked
"broken", and they are not one category. **Do not fix a column from these tables
before reading its reason**: three of them are correct and must not be filled, and
only the fourth was a defect.

### a — Uncollectible: the source sends the field and its value is dead

The field exists in the raw envelope, and we read it, but fomo always puts the same
value in it. There is nothing to fix here — and nothing for the model to learn from it.

| Column | Measurement on the raw | Verdict |
| --- | --- | --- |
| `social_replies` | `numReplies = 0` in 28,104/28,104 theses, and every `comment.parentId` empty | replies do not arrive at this endpoint at all |
| `are_top_traders` | `areTopTraders: false` literally in the raw | not emptiness but the source's answer |
| `is_scam` | 0 in every row (39 measured) | no coin is flagged as scam in our archive |
| `platform_dev_holding` | `isDev = false` in 5,195/5,195 holders | the flag is not enabled upstream |

And two dead fields **we do not read** (documented in `extract.py` so they are not
used later): `equity` is zero in 28,186/28,186, and
`comment.reactions.counts.likeCount` is always zero because it is the **reader's**
state, not the public count — the public count is `comment.numLikes` (non-zero in
46.7%), which is what we actually read.

### b — No variance by definition (no defect)

- `listed_on_exchange` = 1 in 45,857 rows: fomo expresses "no platforms" by the
  key's absence, not by zero, so the `else 0` branch in `features.py` is
  **unreachable**. The count itself (`exchanges_count`) carries the information; the flag does not.
- `rank_le_50` = 1 always: **the source's cap is 50** (not 200 as was once written
  here — `LEADERBOARD_SIZE` is ignored by the source), so `buyers_best_rank` never
  exceeds 50 by construction, not by chance. `rank_le_10` is the live discriminator
  (distinct=2). The period leaderboards **raise this cap**: each returns 100, so
  `buyers_best_rank_24h/_7d/_30d` and `best_rank_any_period` range 1-100 — and
  `rank_le_50` regains its variance if computed on `best_rank_any_period` rather
  than on `buyers_best_rank` alone.
- `minutes` = 15 always: the `multi_user_buy` window is fixed at the source.

### c — Time-blocked: collected now, cannot appear until later

Ten holders-family columns (`top10_holders_pct` · `chain_top10_pct` ·
`chain_holder_count` · `holders_age_min` · `platform_holders` ·
`platform_penetration` · `platform_underwater_ratio` · `platform_value_usd` ·
`platform_median_hold_h` · `platform_dev_holding`) having zero is **not a defect**:

- The collection is sound: `token_holders` has 5,196 rows · 219 coins · and 219/219
  in state `ok`. Coverage within each source is complete (`top10_pct` in
  2,604/2,604 `token_details` rows, and the platform family in 2,582/2,604 from
  `hodlers_top` ≈ 99%).
- But the oldest holders snapshot is `2026-08-09T10:20`, and the newest **mature**
  t0 is `2026-08-07T20:32` — a gap of ~38 hours. The snapshot is read at/before t0
  only, so no mature row today can find a snapshot preceding it.
- The first rows carrying these features mature around **2026-08-11** (t0 ≥ the
  first snapshot, plus the 48h window and margin). **Filling them before that is
  future leakage** — the only number available was measured after the window closed.

The same logic explains the partial coverage of `mintable`/`freezable` (3.3%):
measured 100% on Solana where they mean something, and rightly NULL on EVM (no
authority in this sense — FR-007).

### d — A real defect, fixed

Only two of the 33 were a programming error, both of the same kind: **the extractor
reads a field the source does not send in the expected shape**, so it silently falls to a uniform value.

1. `mintable`/`freezable` — were NULL in 100%: the source returns the authority's
   **address**, not a boolean. After the fix: 312/312 measured on Solana (56 with a listed mint authority).
2. `social_holder_authors`/`social_holder_ratio` — were 0 in 46,040 rows: the
   extractor read `equity` (the dead field above) instead of
   `authorTrade.humanTokenAmount`. After the fix and the retroactive fill (44,763
   snapshots, **no network calls**): 99 distinct values over 0–100, at least one
   holder in 95.2% of snapshots, and the ratio actually distributed (peak 30–60%,
   and a tail of ~900 snapshots where every author is an owner).

**The lesson that generalizes**: a column constant at 0 is more dangerous than a
NULL column. NULL declares its absence, while zero looks like a measurement and
enters training with weight. When a column looks "filled but without variance",
inspect the **raw**, not the table: both defects showed up the moment the real envelope's shape was printed.

### Why are category (a) columns not deleted?

The question is fair: if the column is dead, why keep it? Because "keep" means
three different things, each with a different cost calculation:

| Layer | Decision | Reason |
| --- | --- | --- |
| The raw (`raw_json`) | **never deleted** | it is what allowed fixing `mintable` and `holder_authors` retroactively with zero network calls. What is deleted from it is recoverable only by an impossible re-collection (the time windows have passed). |
| The table column | **stays** | it is the probe. A deleted column is not measured, so if fomo started sending a real `numReplies` we would never know. Keeping it costs bytes and buys detection. |
| The model's feature set | **all of them enter** | `FORBIDDEN` is a manual **block**list, and every column not explicitly listed there becomes a feature. No automatic filtering after that. |

**An explicit decision (2026-08-11): no automatic column filtering, ever.**
`prune_dead_features` used to drop every column without variance, and drop every
column whose value in one half was constant and entirely absent from the other half
as an **epoch marker**. It was deleted at the project owner's request and will not
return. The only way to exclude a column today is `FORBIDDEN`, by hand.

The accepted cost of this decision — stated because it actually happens, not as a hypothetical:

- A column that starts empty then fills (everything collected recently: the flow
  family, source merging, period leaderboards) is NULL in old rows and measured in
  new ones. So the model reads the row's **epoch**, not the coin's state, and AUC
  rises without a real edge. This is the same pattern already rejected and
  documented in `config.py:43` (`LIVE_START_TS`).
- A completely dead column (`social_replies`, `are_top_traders`, `is_scam`,
  `platform_dev_holding`) enters the model with no information — a cost, not a harm.

So the guarding is now **by eye, not by code**: any AUC that jumps after adding a
new family is suspected first as epoch leakage, and verified by comparing the
column's coverage across the two halves of the time frame before relying on it.

## Later (not blocking now)

- **The window decision (t0+Δ)**: decision rows at +10min/+30min labeled from their
  own entry price (PLAN §2.3-b) — the extractor is ready, waiting for a second outcomes row keyed `id:d30`.
- **A thesis text classifier** with a small local model (PLAN §2.3-w-b):
  {substantive · shell · bait · frequency} — a valid retroactive feature because the text is fixed from write time.
