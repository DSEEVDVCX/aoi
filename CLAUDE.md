# aoi — working instructions

Data collection and labeling for fomo.family signals. **[README.md](README.md) explains
what the system is and why**; each folder has its own README with detail. This file is
only the operating rules — the things that are not visible in the code and that have
already cost real time.

## Environment

Windows 10 + PowerShell 5.1. No `&&` chaining — use `;` or separate calls. Python 3.11.

## Tests

`powershell -ExecutionPolicy Bypass -File run_tests.ps1` is the single gate: the api,
recorder and dashboard suites, ruff over all three, four node render checks, then mypy
strict on `api/src/fomo_api`. Run it from the repo root.

**It leaves your shell in `api/`.** The script uses `Set-Location` per suite and never
returns. Any `git` or relative-path work you do afterwards silently resolves against
`api/` instead of the root — `cd` back before continuing.

Do not use the `py` launcher to run tests directly: it resolves to the newest *registered*
Python, not the 3.11 the suite targets. Three CI batches passed green in 2026-08 without a
single test executing because of this. `run_tests.ps1` already picks the interpreter
correctly — go through it.

## Lint

Two configs, and the nearest one wins: [ruff.toml](ruff.toml) covers `recorder/` and
`dashboard/`, `api/pyproject.toml` covers `api/`. Both select `E,F,I,UP,B,SIM,RUF` (root
adds `BLE`). Every broad `except` needs a `# noqa: BLE001` with a written reason beside it.

`recorder/probes/*` is fully exempt — a 295-file archive of finished field measurements.
Do not fix, lint or revive them in place; copy a script to `recorder/` if you need it again.

The `[lint.per-file-ignores]` block is partly **temporary** — several entries exist only
because another agent has uncommitted work in those files. Read the comment above a line
before assuming it is permanent.

## Concurrent writer — never `git add -A`

Kilo Code ([kilo.jsonc](kilo.jsonc)) writes into this repo while you work. Stage by
explicit path, always. `git add -A` sweeps up someone else's half-finished edits and
commits them under your name.

## Long-running services

Eight permanent Windows scheduled tasks: `FomoApiServer`, `FomoRecorder`, `FomoChain`,
`FomoLabeler`, `FomoEVMReplay`, `FomoBuildRows`, `FomoActivityHead`, `FomoDashboard`,
plus `FomoBars1m` and `FomoBackup`.

**Editing `recorder.py` or `features.py` changes nothing until the task restarts.**
`FomoRecorder` and `FomoBuildRows` both hold the module in memory for their whole lifetime.
A code change you verified by reading the file is not a change the system is running.

A dead task writes nothing to its periodic log — that log just stops. To tell "dead" from
"idle", check `<name>_boot.log` and the `meta.*_last_run_at` row, not the main log.

Restarting `FomoDashboard` with `/End` then `/Run` back to back loses the race for port
8090 and exits 1, with a boot log that looks normal. Confirm with a 200 from
`http://127.0.0.1:8090/`, not with the task's status field.

## Database

`recorder.db`. The `raw_json` columns are **zlib-compressed BLOBs** — read them through
[`recorder/db.py:53`](recorder/db.py#L53) `decode_raw()`. `json.loads` on them raises or,
worse, silently mis-parses.

For error bookkeeping use [`recorder/db.py:360`](recorder/db.py#L360) `note_error()`, never
a bare `set_meta`. The recorder once died for 3h14m because its error handler tried to
write to the database that was already locked — `note_error` is the path that survives that.

Adding a column to `training_rows` means touching **four** places, and
[`recorder/features.py:1333`](recorder/features.py#L1333) `ROW_COLUMNS` is the one that gets
forgotten. A migration-only column is `NULL` forever and still passes the schema check.

Bumping `feature_version` does **not** need `--rebuild`. That flag `DELETE`s the table
first; the incremental build overwrites rows in place, which is what you want.

## Training data

Live bot data only (`is_live=1`). The retrospective set was rejected, archived and deleted
on purpose — **do not run `recorder/backfill_activity.py`**, despite it still being present.

`model_training_rows` is a view over `training_rows`; `is_independent` is the decisive cut
and takes ~20.3k rows down to ~4.8k. Count the view, never the table.

## Upstream API

`getBarsNew` needs `"address:networkId"`, not a bare address — a bare address returns a
Cloudflare 502 that reads like an outage. OHLCV availability decays with age.

There is no rate limit upstream; the 60-second cycle is the real cap. Buy time by pacing
before raising any per-cycle cap.

`holder_count` (whole chain, ~75k avg) and `platform_holders` (fomo users only, ~2k) are
different numbers from different sources. Do not treat one as a proxy for the other.

## CI

Never pipe `gh run watch --exit-status` — the pipe's exit 0 hides a failed run. Read the
run's `conclusion` field instead.

The CI runner resolves much newer dependencies than production. A failure that only
appears in CI is dependency drift until proven otherwise.
