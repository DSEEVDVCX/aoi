"""Configuration for the recorder dashboard.

A separate project alongside recorder/ and api/. It reads recorder.db
read-only and checks the local API's health. All paths are absolute and
derived from this file's location so the dashboard works regardless of the
working directory (which matters for the scheduled task).
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))          # .../aoi/dashboard
ROOT = os.path.dirname(HERE)                               # .../aoi
RECORDER_DIR = os.path.join(ROOT, "recorder")              # .../aoi/recorder

# The recorder database — opened read-only (mode=ro) so it never blocks the recorder's writes.
DB_PATH = os.path.join(RECORDER_DIR, "recorder.db")

# Local API health (the fomo_api server on 8080).
API_HEALTH_URL = "http://127.0.0.1:8080/health"
API_HEALTH_TIMEOUT = 3.0  # seconds

# The dashboard's own port (localhost only).
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8090

# Log files (the scheduled task hides stderr via pythonw).
LOG_PATH = os.path.join(HERE, "dashboard.log")
BOOT_LOG_PATH = os.path.join(HERE, "dashboard_boot.log")

STATIC_DIR = os.path.join(HERE, "static")

# The live epoch boundary: the first signal the bot collected in real time
# (2026-07-25T22:35:27Z). The dashboard shows **bot data only**; anything before
# this stamp is retro (backfill) data lacking the live families, which was
# excluded from training and had its rows deleted from the database. Only the
# token_bars table still carries retro candles (price history predating the
# signal), so we cap the displayed candle count at this boundary so retro data
# doesn't mix with bot data.
# The value matches recorder/config.py:LIVE_START_TS (a single epoch boundary for the whole project).
LIVE_START_TS = 1785018927

# "Alive" = the last recorder cycle within this window (the cycle runs every 60s, so we allow double + margin).
RECORDER_ALIVE_WINDOW_SECONDS = 150

# "Alive" for the labeler = a last cycle within this window (its cycle runs every 900s, so we allow double + margin).
# Its death is completely silent — no errors, no crash — while the results stop accumulating.
LABELER_ALIVE_WINDOW_SECONDS = 2000

# Control gates v3 as fixed in docs/PLAN.md: 100 preliminary checks, 500 core decisions.
CONTROL_PRELIMINARY_TARGET = 100
CONTROL_DECISION_TARGET = 500
CONTROL_DESIGN_VERSION = 3

# Storage and backup. Matches the recorder/backup_db.py default: a synced
# destination outside the repo, with an alert if more than a day and a half
# passes without a healthy backup.
BACKUP_DIR = os.environ.get("AOI_BACKUP_DIR") or (
    os.path.join(os.environ["OneDrive"], "aoi-backups")  # noqa: SIM112 — on Windows the name is spelled this way, not capitalized
    if os.environ.get("OneDrive")  # noqa: SIM112 — on Windows the name is spelled this way, not capitalized
    else None
)
BACKUP_MAX_AGE_HOURS = 36.0
DISK_FREE_WARN_GB = 25.0

# Recorder sources whose latest error we display (they match the meta keys: last_error_<src>).
# The list covers everything the recorder actually writes; bars/social used to be logged without being displayed.
RECORDER_SOURCES = (
    "feed", "trending", "verified", "most_held", "leaderboard", "control",
    "bars", "social", "holders", "filter", "traders", "macro", "cleanup",
    # The tradingActivity collector is an independent, long-lived worker. The
    # main recorder's success says nothing about it; it was getting 401s every
    # five minutes for two days while the task still looked Running.
    "activity_head",
    # "chain" is not part of the recorder's cycle but a separate process
    # (FomoChain/run_chain.py), yet it writes last_error_chain into the same
    # meta table ⇒ displayed like the other sources.
    # And "chain_auth" is a second queue inside the same process with an hourly
    # cadence: its error is separate because the focus layer succeeding doesn't
    # mean the credentials layer succeeded.
    "chain", "chain_auth",
    # And "evm" and "evm_contract" are two more queues in the same process, and
    # they run keyless, so their failure reason is completely different (a
    # public node stumbling, not an expired key) ⇒ two separate columns:
    # Solana's success says nothing about the EVM ledger.
    "evm", "evm_contract",
    # And "bsc_nodereal" is the NodeReal queue for BSC: `bsc_layer` used to
    # write last_error_bsc_nodereal with nobody displaying it ⇒ a written
    # failure that was invisible.
    "bsc_nodereal",
    # And "evm_replay" is a sixth process (FomoEVMReplay) writing into the same table.
    "evm_replay",
)

# The staleness boundary per source: any success stamp voids an error older than it.
#
# The boundary used to be one shared value: `started_at` and
# `last_ok_cycle_at`, which only `recorder.py` writes. So the recorder dying —
# or merely its cycles stumbling — froze the boundary, and the chain/evm badges
# stayed red forever no matter how cleanly `FomoChain` completed its cycles;
# which is what was seen on 2026-08-17: three transient errors displayed as if
# current long after they healed.
# Hence: each queue has its own stamp from its own writer. A source's presence
# in this map is a fail-closed contract: if its stamp doesn't exist yet, it is
# not healed by the recorder's general stamp (a first-run failure is not a success).
SOURCE_OK_STAMPS = {
    "chain": ("chain_last_ok_at",),
    "chain_auth": ("chain_auth_last_ok_at",),
    "evm": ("evm_last_ok_at",),
    "evm_contract": ("evm_contract_last_ok_at",),
    "bsc_nodereal": ("bsc_nodereal_last_ok_at",),
    "evm_replay": ("evm_replay_last_ok_at",),
    "activity_head": ("activity_head_last_run_at",),
    # And "traders" is part of the recorder's own cycle, yet it has its own
    # stamp: the recorder's boundary advances every minute with `errors: 0`
    # even while this path is blocked (measured: 21 hours), so without a
    # dedicated stamp the shutdown error would be declared "recovered" a minute
    # after being written. And its stamp is written only when a row lands or
    # nobody is asked — not merely because the cycle had no errors.
    "traders": ("traders_last_ok_at",),
}

# The recorder's boundary: the basis for the sources of its cycle that do **not**
# have an independent entry above. It is not used as a substitute for a missing
# independent stamp; a missing one means that source hasn't succeeded yet.
RECORDER_OK_STAMPS = ("started_at", "last_ok_cycle_at")

# --- External provider keys ---
# Pools live in each process's memory, and the dashboard is a different process
# ⇒ each process stamps its `provider_keys_<owner>` row into meta: **counts and
# gauges, not values** (FR-013). No key value, nor any fragment of one, passes
# through here, and it couldn't anyway — the value is never written to the database.
PROVIDER_KEY_META_PREFIX = "provider_keys_"

# A single key means "rotation exists in code but there's no fallback on rejection" ⇒ a warning, not an error.
PROVIDER_KEY_MIN_KEYS = 2

# A report older than this means the owning process didn't complete a cycle:
# the slowest cadence among owners is the EVM replay (900s), so we allow double and a half.
PROVIDER_KEY_STALE_SECONDS = 2400

# The key file: a second route to the same truth, and the one route where the
# dashboard **writes** (`keystore.py`). The database stays `mode=ro`; all
# writing happens in this file. Account names and the last four characters are
# displayed from it — the only permitted exception to FR-013, requested by the
# user to tell two keys apart, and confined to `key_file.tail`: computed at
# request time and never written to a log, to meta, or to the pools report.
CHAIN_KEYS_PATH = os.path.join(RECORDER_DIR, "chain_keys.json")

# The "test now" timeout: one cheap call. Deliberately shorter than the real
# client's timeout — the user is waiting at the button, and a timed-out probe
# is a valid answer ("no response"), not a probe failure.
KEY_PROBE_TIMEOUT = 6.0

# --- The fomo account: the third route where the dashboard **writes** (`account.py`) ---
# The Privy credential file the recorder reads every cycle and the api server
# refreshes. Switching the account from the dashboard writes to this file
# alone — not to the database, not to a scheduled task, and no shell command
# is executed. Its name comes from `settings.credential_state_file`.
API_DIR = os.path.join(ROOT, "api")                        # .../aoi/api
PRIVY_STATE_PATH = os.path.join(API_DIR, ".privy_state.json")

# The account probe: is the identity blocked? Three cheap calls to the same
# upstream. Its timeout is longer than the key probe because `curl_cffi`
# negotiates a full TLS handshake with a Chrome fingerprint, which is slower
# than an ordinary call.
FOMO_UPSTREAM_BASE = "https://prod-api.fomo.family"
FOMO_APP_ORIGIN = "https://fomo.family"
ACCOUNT_PROBE_TIMEOUT = 12.0

# --- Cache TTLs for the heavy queries (`cache.py`) ---
# Three queries re-count the whole history on every refresh, and their cost
# grows linearly with recording. They can't be fixed with an index because an
# index means touching the database ⇒ they're computed once per period, the
# stale value is served instantly while the refresh happens in the background.
# The TTLs are chosen for what **changes the meaning of the number**, not for
# how often it changes: these are numbers displayed compressed (1.5M).

# Network summary: 21.5 seconds measured at 5.0M market_ticks (2026-08-29);
# the heaviest thing in the dashboard, and the slowest to change in meaning —
# coverage counts and ratios. The latest market snapshot is refreshed live on
# top of it on every request, so ten minutes doesn't make the pulse stale, and
# it cuts the full archive sweep from 30 times/hour to just 6.
NETWORK_SUMMARY_TTL_SECONDS = 600.0

# Row counts per table: 400 ms, of which 357 in `market_ticks` alone. They're
# displayed compressed on the cards, so a minute of lag is invisible anyway.
TABLE_COUNTS_TTL_SECONDS = 60.0

# Ticks summary: 1520 ms, and the page doesn't call it today — but it's a
# public path that costs a second and a half for whoever calls it, so it's
# cached so it doesn't stay a trap for whoever displays it later.
TICKS_SUMMARY_TTL_SECONDS = 120.0

# Labeling and outcomes summary: 1.2 seconds measured (the signal median alone
# scans 34 thousand rows). Final historical numbers that only change every
# quarter hour (the labeler's cadence), so a five-minute TTL is cheaper than
# re-scanning on every refresh.
LABELING_TTL_SECONDS = 300.0

# A busy database drops a refresh; without backoff every request becomes a fresh failed attempt.
CACHE_ERROR_BACKOFF_SECONDS = 15.0
