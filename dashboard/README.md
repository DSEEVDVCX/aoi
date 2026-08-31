# Recorder Dashboard — Fomo Recorder Dashboard

> Part of the **aoi** project — see the [main document](../README.md) for the
> full architecture and the [post-collection plan](../docs/PLAN.md).

A live (read-only) window onto what `FomoRecorder` collects, and onto the
local API's health. The dashboard is split into independent views through a
sidebar: overview, signals, watchlist, performance, data health, and networks.
A standalone project alongside `api/` and `recorder/`.
**It modifies no code in either of them.**
The `recorder.db` database stays read-only — **purely read, no exceptions**.

## What it shows
- **Sidebar**: navigate between workspaces instead of one long page; status and
  options stay in the top bar on desktop, and turn into a scrollable bar on mobile.
- **Top bar**: is the recorder alive (last cycle within 150s), is the API
  connected/degraded/disconnected, plus a pause for refresh and a theme toggle
  (dark/light/auto).
- **One headline number**: the count of recorded signals + its accumulation
  line over the last 24 hours.
- **Cards**: active watches, candle coverage, OHLCV candles, market snapshots,
  archive size and growth rate, and API latency.
- **Signal performance since entry** ← the most important panel: for every
  watched coin, how much its price rose after its signal (`peak_pct`), where
  it is now, and how far it has fallen from its peak, with a sparkline.
  Its columns are **sortable** (a click on the header flips the direction) and
  the display limit is changeable.
- **The profit-and-loss tally** above the table: winner/loser count, win rate,
  mean return **and median** together, gross gains and gross losses and net,
  and the best and worst.
- **Signal flow**: stacked columns, signals per hour by type (last 24 hours).
- **Collection health**: candle coverage + last cycle + archive size/span +
  disk space and backup freshness.
- **Watchlist**: the 48-hour remaining gauge for each coin.
- **Latest signals**: with a "top trader bought" highlight
  (top_trader_match_count>0 or rank ≤50).
- **Errors by source**: the active one in red, the recovered one (older than
  the last successful cycle) dimmed.
- **Networks**: the number of watched coins, market snapshots, `Top 1/5/10/20`
  measurements, and holder counts per network, showing the measurement source
  and the last actual update.

The page refreshes itself every 10 seconds via fetch to the JSON endpoints.

## Design notes (for whoever edits it later)

- **The colors are verified automatically, not chosen by taste.** The three
  categorical colors (signal type) pass color-blindness separation and contrast
  gates on both the dark and light surfaces. Don't swap a color on intuition —
  run the dashboard's color checker first.
- **The color follows the signal type, not its rank** (`TYPE_META`): otherwise
  filtering would repaint the remaining series and confuse whoever learned
  "blue = multi-buy".
- **Sorting happens on the server, not in the browser.** The server sorts the
  full set, then truncates. If it were sorted in the browser after truncation,
  descending order would have given "the top N inverted" instead of the actual
  worst — a silent mistake that looks completely correct. The sort keys are
  whitelisted (`_SORT_KEYS`), and a missing value stays at the tail in both
  directions.
- **The tally is computed over all coins, not the displayed slice** —
  otherwise the numbers would change whenever the user changed the display
  limit. And it's built on `change_pct`, not `peak_pct`: a peak is only
  realized by selling at that exact moment. **And the median is shown next to
  the mean** because a single outlier winner drags the mean by itself
  (currently: mean +4.9% versus median −4.7%).
- **The links to fomo.family** are built from the site's own route table, not
  by guessing: the coin page `/tokens/:chain/:tokenAddress` and the trader
  page `/u/:handle`. And `:chain` is a **short slug, not a numeric id** — the
  site's code reads it as a key in a map, so the map is copied verbatim from
  its bundle (`chains-*.js`) into `CHAIN_SLUG`:

  | networkId | slug | | networkId | slug |
  |---|---|---|---|---|
  | 1399811149 | `solana` | | 1 | `ethereum` |
  | 8453 | `base` | | 1337 | `hyperliquid` |
  | 143 | `monad` | | 4663 | `robinhood` |
  | 56 | `bnb` | | | |

  A network not in the table ⇒ **no link** (plain text) instead of a guessed
  link leading to a broken page. If fomo adds a new network, add it here. All
  links are `target="_blank" rel="noopener noreferrer"`.
- **Every chart has a table equivalent** (a "show as table" button): the value
  isn't hidden behind a tooltip.
- **A legend is always present** for two or more series; the direction
  (up/down) is paired with a `+/-` sign and text, so color never carries the
  meaning alone.
- **Every panel renders in isolation** via `paint()`: a failure in one empties
  that one alone. (Without this isolation one error emptied four panels at once.)
- **The page is served with `Cache-Control: no-cache`**: without it the browser
  stays on an old version of the dashboard forever after any update — including
  after a bug fix.
- Wide tables scroll inside `.tablewrap` alone; the page body never scrolls
  horizontally.
- No external libraries: the charts are hand-written SVG (they work offline).

## Security
- `recorder.db` is opened via a URI with **`mode=ro`** — no writes at all, so
  it never blocks the recorder's writes (WAL).
- Listening on **127.0.0.1** only (localhost, not exposed).
- **Both guards stay even though the dashboard is now purely read-only**: the
  `Host` guard blocks DNS rebinding on every route, and the CSRF guard (a
  random session token + `Origin/Referer` matching) rejects any
  state-changing request. Cheaper than remembering to add them back when the
  first write route appears.

## Files
- `config.py` — paths and ports (recorder.db, API health, port 8090).
- `dao.py` — pure read-only query functions, testable.
- `app.py` — FastAPI: JSON endpoints + serving the page.
- `static/index.html` — the page (inline HTML+CSS+JS, English/LTR).
- `serve_dashboard.py` — the launch point (chdir + boot log + uvicorn).
- `tests/test_dao.py` — dao tests against a temporary database.
- `tests/test_app_guards.py` — tests for the `Host` and CSRF guards.

## endpoints
| Route | Description |
|---|---|
| `GET /` | The page |
| `GET /api/status` | Recorder status (alive, cycle count, last cycle…) **and labeler status** — the top bar shows them separately because the labeler's death is silent (no errors, no cycle crashes) |
| `GET /api/api-health` | Checks `http://127.0.0.1:8080/health` |
| `GET /api/signals?limit=50` | Latest signals |
| `GET /api/watchlist` | Active watched coins |
| `GET /api/ticks-summary` | Market snapshot summary |
| `GET /api/counts` | Row counts for every table |
| `GET /api/errors` | Last error per source |
| `GET /api/bars` | Candle coverage (how many watched coins have a price series) |
| `GET /api/storage` | Archive size and growth, disk space, and backup freshness |
| `GET /api/performance?limit=12` | Every coin's performance since its signal + sparkline |
| `GET /api/signal-timeline?hours=24` | Signals per hour by type |
| `GET /api/networks` | Market, concentration, and holder coverage per network |

## Running manually
```powershell
& "C:\Users\rr\AppData\Local\Programs\Python\Python311\python.exe" `
  "c:\Users\rr\Desktop\aoi\dashboard\serve_dashboard.py"
# then open: http://127.0.0.1:8090/
```

## Tests
```powershell
cd c:\Users\rr\Desktop\aoi\dashboard
& "C:\Users\rr\AppData\Local\Programs\Python\Python311\python.exe" -m pytest tests -q
```

## Scheduled task
`FomoDashboard` is installed (the `FomoRecorder` pattern):
`pythonw serve_dashboard.py`, starts at logon, survives a reboot. Manage it:
```powershell
Start-ScheduledTask -TaskName FomoDashboard
Stop-ScheduledTask  -TaskName FomoDashboard
Get-ScheduledTaskInfo -TaskName FomoDashboard
```
