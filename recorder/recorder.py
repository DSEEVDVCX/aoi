"""Main loop of the historical data recorder.


Reads raw data from fomo via FomoClient._get/_post directly (before any mapping,
since mapping drops the most valuable fields) and writes to recorder.db. Every
upstream call sits inside try/except: failure (502...) is logged in meta and
skipped — the loop never dies.


read-only (FR-012): every GET/POST call is read-only (feed, trending, verified,
leaderboard). No call writes account state or trades.

"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import config
import extract
from db import RecorderDB, utcnow_iso
from features import epoch_of
from leaderboard_cache import LeaderboardCache

_evm_admission_paused_runtime = False


@dataclass(frozen=True)
class EVMNetworkAdmission:
    """Admission state for one EVM network and its current RPC budget."""

    network: str
    backlog: int
    work_units: int
    capacity_units: int
    retry_count: int
    rpc_healthy: bool
    numerator: int
    denominator: int
    paused: bool
    reason: str

    @property
    def percent(self) -> int:
        return 100 * self.numerator // self.denominator


def _fail_closed_evm_admission(networks: frozenset[str]) -> EVMAdmissionPolicy:
    global _evm_admission_paused_runtime

    _evm_admission_paused_runtime = True
    return EVMAdmissionPolicy(networks, -1, 0, 1, True)


@dataclass(frozen=True)
class EVMAdmissionPolicy:
    """Deterministic admission decision for new tokens; non-EVM networks are unaffected."""


    networks: frozenset[str]
    backlog: int
    numerator: int
    denominator: int
    paused: bool
    by_network: Mapping[str, EVMNetworkAdmission] | None = None

    @property
    def percent(self) -> int:
        if self.by_network:
            return min(state.percent for state in self.by_network.values())
        return 100 * self.numerator // self.denominator

    def network(self, network: str) -> EVMNetworkAdmission | None:
        return self.by_network.get(str(network)) if self.by_network else None

    def network_percent(self, network: str) -> int:
        state = self.network(network)
        return state.percent if state is not None else 100

    def allows(self, kind: str, token: str, network: str, key: str) -> bool:
        if str(network) not in self.networks:
            return True
        state = self.network(network)
        numerator = state.numerator if state is not None else self.numerator
        denominator = state.denominator if state is not None else self.denominator
        if numerator <= 0:
            return False
        if numerator >= denominator:
            return True
        # All admission paths must make the same decision for one token. The
        # unused kind/key parameters remain part of the call contract only.
        digest = hashlib.sha256(f"{network!s}:{token.lower()}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % denominator < numerator

    def score(self, kind: str, token: str, network: str) -> int:
        digest = hashlib.sha256(f"{network!s}:{token.lower()}".encode()).digest()
        return int.from_bytes(digest[:8], "big")


def _hypersync_covered_networks(db: RecorderDB) -> frozenset[str]:
    """Networks the EVM worker routed through HyperSync in a **fresh** cycle.

    The stamp is written by `evm_layer.run_evm_cycle` every cycle (60s), so a
    stamp older than `EVM_HYPERSYNC_STAMP_FRESH_SECONDS` means the worker is
    dead or the key vanished — and the admission gate must then fall back to
    the public-node math on its own, with no operator involved. The same guard
    rejects a stamp from the future beyond the window: a clock skew backwards
    must not make one stamp live forever.
    """
    raw = db.get_meta("evm_hypersync_covered")
    if not raw:
        return frozenset()
    try:
        decoded = json.loads(raw)
        at = datetime.fromisoformat(str(decoded["at"]))
        networks = {str(net) for net in decoded["networks"]}
    except (KeyError, TypeError, ValueError):
        return frozenset()
    fresh = float(getattr(config, "EVM_HYPERSYNC_STAMP_FRESH_SECONDS", 900))
    age = (datetime.fromisoformat(utcnow_iso()) - at).total_seconds()
    if not -fresh <= age <= fresh:
        return frozenset()
    return frozenset(networks)


def evm_admission_policy(db: RecorderDB) -> EVMAdmissionPolicy:
    """Build independent admission budgets from each network's RPC limits.

    ``backlog`` remains as a compatibility aggregate for the dashboard, but
    ``allows`` consumes ``by_network``. A slow/failed RPC therefore throttles
    only its own network.
    """
    networks = tuple(str(network) for network in config.EVM_NETWORKS)
    if not networks:
        return EVMAdmissionPolicy(frozenset(), 0, 1, 1, False, {})

    previous: dict[str, Any] = {}
    raw_previous = db.get_meta("evm_admission_network_state")
    if raw_previous:
        try:
            decoded = json.loads(raw_previous)
            if isinstance(decoded, dict):
                previous = decoded
        except (TypeError, ValueError):
            previous = {}

    states: dict[str, EVMNetworkAdmission] = {}
    hypersync = _hypersync_covered_networks(db)
    for network in networks:
        rows = db._conn.execute(
            """SELECT b.status, b.from_block, b.to_block
                 FROM watchlist w
                 LEFT JOIN evm_backfill_state b
                   ON b.network_id=w.network_id
                  AND lower(b.token_address)=lower(w.token_address)
                WHERE w.active=1 AND w.network_id=?""",
            (network,),
        ).fetchall()
        cursor = db._conn.execute(
            """SELECT last_status, last_run_at, last_block
                 FROM evm_block_cursor WHERE network_id=?""",
            (network,),
        ).fetchone()

        batch_size = max(1, int(config.EVM_BATCH_SIZE.get(network, 1)))
        rpc_limit = int(getattr(config, "EVM_RPC_SUBREQUEST_LIMIT", {}).get(network, 0))
        if rpc_limit:
            batch_size = min(batch_size, rpc_limit)
        range_hint = int(config.EVM_LOG_RANGE_HINT.get(network, 0)) or 10_000
        # The fast lane: a network the worker actually routed through HyperSync
        # this cycle is not spending the public node's budget on backfill at
        # all, so the public node's ration (pacing, call cap, 10K-block work
        # units) is the wrong yardstick — under it every fresh Base token
        # looked like ~3,400 units against a capacity of 72, and the network
        # re-paused the moment it opened (10,948 coins deferred, 2026-09-04).
        # The stamp decides, not the config: no key ⇒ no stamp ⇒ old math.
        fast = network in hypersync
        pacing = (
            config.EVM_HYPERSYNC_PACING_SECONDS if fast
            else (
                config.EVM_BATCH_PACING_SECONDS
                if batch_size > 1 else config.EVM_PACING_SECONDS
            )
        )
        requests_by_time = max(
            1,
            int(
                getattr(config, "EVM_BACKFILL_BUDGET_SECONDS_BY_NETWORK", {}).get(
                    network, config.EVM_BACKFILL_BUDGET_SECONDS
                ) / max(pacing, 0.1)
            ),
        )
        request_capacity = min(
            int(
                config.EVM_HYPERSYNC_MAX_CALLS if fast
                else config.EVM_BACKFILL_MAX_CALLS
            ),
            requests_by_time,
        )
        capacity_units = max(
            1,
            request_capacity
            # The public batch multiplier is the multi-address filter (10
            # addresses per call on Base); the HyperSync query is one address
            # per request, so the fast lane must not borrow it — an inflated
            # capacity is an admission promise the lane cannot keep.
            * (1 if fast else batch_size)
            * max(1, int(config.EVM_BACKFILL_TOKENS_PER_CYCLE)),
        )

        pending_rows = [
            row for row in rows if str(row["status"] or "") != "done"
        ]
        backlog = len(pending_rows)
        retry_count = sum(str(row["status"] or "") == "retry" for row in pending_rows)
        # One work unit = one backfill call, and a call covers the measured range
        # in `EVM_BACKFILL_BLOCKS_PER_CALL`, not the range cap (range_hint). The old
        # cap-based estimate inflated the work ×25 on Robinhood (a range cap not in
        # effect there anyway), keeping the gate paused forever — measurement
        # 2026-08-29: a full token with 79,894 transfers completed in 33 calls.

        blocks_per_call = max(
            1,
            int(
                # The fast lane's calibrated figure, not the range cap — see
                # `EVM_HYPERSYNC_BLOCKS_PER_CALL` in config for why it is the
                # deliberately pessimistic side of the measurement.
                config.EVM_HYPERSYNC_BLOCKS_PER_CALL if fast
                else config.EVM_BACKFILL_BLOCKS_PER_CALL.get(network, range_hint)
            ),
        )
        work_units = 0
        head = int(cursor["last_block"]) if cursor and cursor["last_block"] else None
        for row in pending_rows:
            start = row["from_block"]
            end = row["to_block"]
            if start is None or end is None or head is None:
                work_units += max(1, capacity_units // max(1, int(config.EVM_BACKFILL_TOKENS_PER_CYCLE)))
                continue
            remaining = max(0, int(end) - int(start) + 1)
            work_units += max(1, (remaining + blocks_per_call - 1) // blocks_per_call)

        rpc_healthy = not pending_rows or bool(
            cursor and str(cursor["last_status"] or "") in ("", "ok")
        )
        reason = "ok"
        if retry_count:
            reason = "active_retry"
        elif not rpc_healthy:
            reason = "rpc_unhealthy_or_uninitialized"

        utilization = work_units / capacity_units
        old_paused = bool((previous.get(network) or {}).get("paused"))
        # The dedicated-provider exemption (2026-09-06): a network whose
        # historical backfill runs through HyperSync (fresh routing stamp)
        # is not spending the public node's budget on that backlog at all.
        # Inheriting the public-node pause math there re-paused Monad for a
        # ~24× "utilization" computed entirely from a HyperSync lane the
        # stamp had just certified — the mechanism was built before the
        # dedicated provider existed. The exemption opens **admission**
        # only; it never clears an `active_retry` or an unhealthy cursor,
        # and a stale stamp (dead worker / removed key) returns the network
        # to the full public-node math on its own.
        routed = fast
        if routed and rpc_healthy and not retry_count:
            paused = False
            fraction = (1, 1)
            reason = "hypersync_routed"
        elif not rpc_healthy or retry_count:
            paused = True
            fraction = (0, 1)
        elif old_paused and utilization > 0.5:
            paused = True
            fraction = (0, 1)
            reason = "hysteresis_high_water"
        elif utilization >= 4.0:
            paused = True
            fraction = (0, 1)
            reason = "rpc_work_budget_exhausted"
        elif utilization >= 2.0:
            paused = False
            fraction = (1, 2)
            reason = "rpc_work_budget_reduced"
        elif utilization >= 1.0:
            paused = False
            fraction = (3, 4)
            reason = "rpc_work_budget_guard"
        else:
            paused = False
            fraction = (1, 1)

        states[network] = EVMNetworkAdmission(
            network=network,
            backlog=backlog,
            work_units=work_units,
            capacity_units=capacity_units,
            retry_count=retry_count,
            rpc_healthy=rpc_healthy,
            numerator=fraction[0],
            denominator=fraction[1],
            paused=paused,
            reason=reason,
        )

    snapshot = {
        network: {
            "backlog": state.backlog,
            "work_units": state.work_units,
            "capacity_units": state.capacity_units,
            "retry_count": state.retry_count,
            "rpc_healthy": state.rpc_healthy,
            "percent": state.percent,
            "paused": state.paused,
            "reason": state.reason,
        }
        for network, state in states.items()
    }
    db.note_error("evm_admission_network_state", json.dumps(snapshot, sort_keys=True))
    db.note_error(
        "evm_admission_paused_networks",
        json.dumps(sorted(network for network, state in states.items() if state.paused)),
    )
    db.note_error(
        "evm_admission_paused",
        "1" if any(state.paused for state in states.values()) else "0",
    )

    total_backlog = sum(state.backlog for state in states.values())
    aggregate = min(states.values(), key=lambda state: state.percent)
    return EVMAdmissionPolicy(
        frozenset(networks),
        total_backlog,
        aggregate.numerator,
        aggregate.denominator,
        any(state.paused for state in states.values()),
        states,
    )


def _load_access_token() -> str:
    """Reads the session_token (access_token) from the CredentialStore on disk.


    Never prints the token value (FR-013). Raises a clear error if the credential is missing.
    The disk is the source of truth: the api server (TokenRefresher) writes a fresh
    token here before it expires, and the recorder picks it up every cycle with no manual login.

    """
    from fomo_api.auth.credential_store import CredentialStore

    creds = CredentialStore(config.credential_state_path()).load()
    if creds is None or not creds.access_token:
        raise RuntimeError(
            "No valid credential in the state file — run the api service first to generate one."

        )
    return creds.access_token


def _build_client(access_token: str) -> Any:
    """Builds a FomoClient from a given token (no disk read, no token printing)."""

    from fomo_api.clients.fomo_client import FomoClient

    return FomoClient(session_token=access_token)


def _load_client() -> Any:
    """Loads the session_token from disk and builds a FomoClient.


    Never prints the token value (FR-013). Raises a clear error if the credential is missing.

    """
    return _build_client(_load_access_token())


def _exc_note(exc: Exception) -> str:
    """The exception's name and message **plus its status code** — the message alone sometimes lies.


    Measured 2026-08-19T14:54Z: the platform blocked the account with 403 on all
    twelve paths, yet the recorder wrote "UpstreamUnavailableError: fomo.family is
    currently unreachable" — the very same default class message a real network
    outage writes. So the diagnosis chased DNS and ping and TLS while the origin
    replied in 20ms; the 403 code sat in `ApiError.details` the whole time and was
    never printed because the formatter takes `str(exc)` alone. The EVM layer
    already includes the code — a test asking for `"503" in last_error_evm`
    attests to it — so this unifies an existing convention, not invents a new one.


    The truncation is deliberate: `details["reason"]` carries the full transport
    exception text, and this note lives in a `meta` row read on the dashboard, not
    an open-ended debug log.

    """
    text = f"{type(exc).__name__}: {exc}"
    details = getattr(exc, "details", None)
    if not isinstance(details, dict) or not details:
        return text
    pairs = ", ".join(f"{k}={str(v)[:120]}" for k, v in sorted(details.items()))
    return f"{text} ({pairs})"


def _note_shutout(
    db: RecorderDB, block: str, recorded_at: str, asked: int, produced: int
) -> int:
    """Makes the shutout audible: a block that asks and produces nothing, cycle after cycle.


    The lesson of 2026-08-19: `/v2/users/{id}` died and returned 404 for every id,
    and 404 and "account deleted" are one and the same path — so `empty` was
    written for every trader, `traders_rows: 0` on every log line, and `errors: 0`
    as well. 21 hours without a single error word, because "no data for this item"
    is a **legitimate** answer for a single item. An individual zero is normal; a
    repeated collective zero can only be an outage.


    Hence the counter lives in `meta`, not in memory: were the recorder restarted
    every hour (it happened), memory would start from zero and the shutout would
    stay as silent as before. It resets at the first row that lands — the
    diagnosis is about "now", not about yesterday.


    It is called only where a block-wide zero is **impossible**, not merely rare:
    50 traders all deleted is not an event (measured: 7 real absences out of 500).
    Candles can be legitimately missing, and signals sit empty in a quiet minute —
    those blocks are not wired to this one.


    Returns the streak length after the update.

    """
    key = f"shutout_{block}"
    if produced > 0 or asked <= 0:
        if db.get_meta(key) not in (None, "0"):
            db.note_error(key, "0")
        return 0
    previous = db.get_meta(key)
    streak = (int(previous) if previous and previous.isdigit() else 0) + 1
    db.note_error(key, str(streak))
    if streak >= config.SHUTOUT_STREAK_ALERT:
        db.note_error(
            f"last_error_{block}",
            f"{recorded_at}: ShutoutSuspected: asked {asked} and not a single row landed "
            f"across {streak} consecutive cycles — most likely the path itself broke, "
            f"not that every item is empty",

        )
    return streak


async def _fetch_feed_raw(client: Any) -> Any:
    """Raw GET /feed — we bypass get_feed (it maps and drops topTraders/the full body)."""

    from fomo_api.config import settings

    params = {"feedTypes": list(config.FEED_TYPES), "limit": config.FEED_LIMIT}
    return await client._get(settings.upstream_feed_path, params)


async def _fetch_trending_raw(client: Any) -> Any:
    from fomo_api.config import settings

    return await client._post(settings.upstream_trending_tokens_path, {})


async def _fetch_verified_raw(client: Any) -> Any:
    from fomo_api.config import settings

    return await client._get(settings.upstream_verified_tokens_path)


async def _fetch_most_held_raw(client: Any) -> Any:
    """Raw POST /proxy/mostHeld — a third discovery list.


    Measured live: `[200]`, 25 items, intersecting the trending extractor in 18
    keys => `extract_market_tick` suffices unchanged. 19 of the 25 were already
    watched here and **6 brand new** — that is, it sees tokens the other two lists
    do not.

    """
    from fomo_api.config import settings

    return await client._post(settings.upstream_most_held_path, {})


async def _fetch_filter_tokens_raw(client: Any, symbols: list[str]) -> Any:
    """Raw POST /proxy/filterTokens — the body is an **array** of `"address:networkId"`.


    Measured live: 150 addresses in one call return 150/150 (326 KB), and a dead
    address is **silently dropped while the batch survives** (5 of 6 came back,
    `[200]`) — so a token delisted mid-window does not blind the rest of the batch.

    """
    from fomo_api.config import settings

    return await client._post(settings.upstream_filter_tokens_path, symbols)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


async def _fetch_traders_raw(client: Any, trader_ids: list[str]) -> Any:
    """Raw GET /v2/users?userIds=…&userIds=… — a batch of trader profiles.


    We bypass `get_trader_profiles` because it maps and drops fields; we want the
    full envelope for the archive. Measured live: 26 keys per user.


    And the batch is not an optional nicety: `/v2/users/{id}` has answered 404
    "User not found" to every well-formed id since 2026-08-19T14:53Z — and ids
    that that very moment came out of a 200 on `/v2/leaderboard` also get 404, so
    it is neither our ids nor our account. This is the first time a trader has
    been fetched since then (3,775 rows, the last at 14:53:51Z, then 21 hours of
    silent `empty`).


    The 100-per-call limit is declared by the source itself in the validation error.

    """
    from fomo_api.config import settings

    return await client._get(
        settings.upstream_traders_batch_path, {"userIds": trader_ids}
    )


async def _fetch_bars_raw(
    client: Any, token_address: str, network_id: str, from_ts: int, to_ts: int,
    resolution: str | None = None,
) -> Any:
    """Raw POST /proxy/getBarsNew.


    We bypass get_token_bars because it maps; we want the raw envelope to extract
    ourselves. symbol = "address:networkId" and from/to are both mandatory — each
    confirmed live (bare address → 502, missing from/to → 400).

    """
    from fomo_api.config import settings

    body = {
        "symbol": f"{token_address}:{network_id}",
        "resolution": resolution or config.BARS_RESOLUTION,
        "from": from_ts,
        "to": to_ts,
        "countBack": config.BARS_COUNT_BACK,
    }
    return await client._post(settings.upstream_get_bars_path, body)


async def run_bars_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """Pulls candles for a slice of the watched tokens (round-robin scheduling).


    Each token on its own inside try: one failure does not block the rest or sink
    the cycle. We always request the full window from (first seen - context), not
    from the last candle: the latest candle is still forming and gets re-reviewed,
    and the full pull heals any earlier gap.

    """
    stats = {"bars_tokens": 0, "bars_rows": 0, "bars_no_data": 0, "bars_errors": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (now_dt - timedelta(seconds=config.BARS_REFRESH_SECONDS)).isoformat()
    due = db.bars_fetch_due(
        limit=config.BARS_PER_CYCLE,
        stale_before_iso=stale_before,
        max_no_data_attempts=config.BARS_MAX_NO_DATA_ATTEMPTS,
    )
    to_ts = int(now_dt.timestamp())

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        try:
            first_seen = datetime.fromisoformat(w["first_seen_at"])
            from_dt = first_seen - timedelta(hours=config.BARS_PRE_SIGNAL_HOURS)
            # Never exceed the window cap (avoid requesting a wider range than fomo returns anyway).

            earliest = now_dt - timedelta(hours=config.BARS_MAX_SPAN_HOURS)
            from_ts = int(max(from_dt, earliest).timestamp())

            raw = await _fetch_bars_raw(client, addr, net, from_ts, to_ts)
            status = extract.bars_status(raw) or "no_data"
            rows = extract.extract_bars(
                raw, addr, net, config.BARS_RESOLUTION, recorded_at
            )
            if rows:
                stats["bars_rows"] += db.insert_bars(rows)
                # The distortion verdict needs the neighbor of the last candle — recomputed after every insert.

                db.recompute_bar_flags(addr, net, config.BARS_RESOLUTION)
                stats["bars_tokens"] += 1
            else:
                stats["bars_no_data"] += 1
            db.set_bars_state(addr, net, status if rows else "no_data", len(rows), recorded_at)
        except Exception as exc:  # noqa: BLE001 — one token must not sink the slice

            stats["bars_errors"] += 1
            try:
                db.set_bars_state(addr, net, "error", 0, recorded_at)
            except Exception:  # noqa: BLE001 — rescheduling, not measuring; and if it threw
                pass                        # the seal below would never be reached anyway

            db.note_error("last_error_bars", f"{recorded_at}: {_exc_note(exc)}")
        if i + 1 < len(due):
            await sleep(config.BARS_PACING_SECONDS)
    return stats


def admit_control_sample(
    db: RecorderDB,
    candidates: Sequence[tuple],
    recorded_at: str,
    rng: random.Random | None = None,
    policy: EVMAdmissionPolicy | None = None,
    stats: dict[str, int] | None = None,
    static_items: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> int:
    """Admits control tokens chosen **at random** from the same token universe. Returns how many were admitted.


    Comparison-soundness conditions (all deliberate):

    - **Random, not rank-ordered**: taking from the head of the trending list picks
      the highest volume, so the gap versus the signalled ones becomes a volume gap, not a signal gap.

    - **No peeking at the future**: selection depends only on what is known now; no
      later performance is consulted (otherwise it is outright leakage).

    - **Everything signalled is excluded**, even if it never entered the watch — otherwise it is no longer a control.

    - **In installments** (`CONTROL_PER_CYCLE`): taking all forty at once makes them
      a sample of a single market moment, mixing the signal effect with that moment's effect.

    - **The same age gate** applied to the signalled ones (added 2026-08-22):
      otherwise the two arms differ in the strongest variable. Measured on the day
      the gate was added: 1.5% of signal windows were for a token under two days
      old versus **50%** of control windows — a 48-point gap in a variable whose
      rug rate is 29x higher and whose median return is −46.2% versus −5.1%. Any
      "signal effect" measured on arms like these is an age effect dressed up as a
      signal. Unknown age is rejected here exactly as it is rejected there — the
      same rule, not a gentler one — and its price is 12 tokens out of 1,212 (0.99%).

    """
    need = config.CONTROL_GROUP_SIZE - db.active_watch_count(is_control=1)
    if need <= 0 or not candidates:
        return 0

    known = db.known_tokens()
    signalled = db.signalled_tokens()
    normalized: dict[tuple[str, str], tuple[float | None, str | None, Any]] = {}
    strict_comparison = any(len(candidate) >= 4 for candidate in candidates)
    for candidate in candidates:
        if len(candidate) == 2:
            addr, net = candidate
            price_usd = None
            admission_source = None
            has_price = False
            created_at = None
        elif len(candidate) == 3:
            addr, net, price_usd = candidate
            admission_source = None
            has_price = True
            created_at = None
        elif len(candidate) == 4:
            addr, net, price_usd, admission_source = candidate
            has_price = True
            created_at = None
        else:
            addr, net, price_usd, admission_source, created_at = candidate
            has_price = True
        # Production candidates carry the current tick price. A missing price
        # is not a valid entry point; legacy callers without a price remain
        # supported for deterministic tests and offline tooling.
        if has_price:
            try:
                price_usd = float(price_usd) if price_usd is not None else None
            except (TypeError, ValueError):
                continue
            if price_usd is None or not math.isfinite(price_usd) or price_usd <= 0:
                continue
        normalized[(addr, net)] = (price_usd, admission_source, created_at)

    # The gate **before** weighting, not after: the weights match the control
    # group's network mix to the signal network mix, so if the small ones were
    # dropped after selection their network's share would shrink with no
    # re-weighting — and the two arms would differ in network instead of age.

    age_rejected = 0

    def _age_ok(key: tuple[str, str]) -> bool:
        nonlocal age_rejected
        if not config.MIN_TOKEN_AGE_DAYS:
            return True
        if static_items and key in static_items:
            _write_filter_static(
                db,
                static_items[key],
                key[0],
                str(key[1] or ""),
                recorded_at,
                replace_invalid=True,
            )
        _stored, _observed = stored_age(db, key[0], str(key[1] or ""))
        candidate_created = normalized[key][2]
        verdict = stored_age_verdict(db, key[0], str(key[1] or ""), recorded_at)
        if verdict == AGE_UNKNOWN and candidate_created is not None:
            verdict = age_verdict(candidate_created, recorded_at, recorded_at)
        if verdict == AGE_OK:
            return True
        age_rejected += 1
        return False

    pool = sorted(
        key for key in normalized
        if key not in known and key[0] not in signalled
        and (
            policy is None
            or policy.allows("control", key[0], str(key[1] or ""), recorded_at)
        )
        and _age_ok(key)
    )
    if age_rejected:
        db.bump_counter("control_age_rejected_total", age_rejected)
        if stats is not None:
            stats["control_age_rejected"] = (
                stats.get("control_age_rejected", 0) + age_rejected
            )
    if not pool:
        return 0

    rng = rng or random.Random()
    pick_count = min(len(pool), config.CONTROL_PER_CYCLE, need)
    if strict_comparison:
        signal_networks = db._conn.execute(
            """SELECT network_id, admission_source, COUNT(*) AS n
                 FROM watch_windows
                WHERE design_version=? AND is_control=0
                GROUP BY network_id, admission_source""",
            (config.CONTROL_DESIGN_VERSION,),
        ).fetchall()
        weights = {
            (
                str(row["network_id"] or ""),
                str(row["admission_source"] or ""),
            ): int(row["n"])
            for row in signal_networks
        }
        if not weights:
            return 0
    else:
        signal_networks = db._conn.execute(
            """SELECT network_id, COUNT(*) AS n FROM watchlist
               WHERE active=1 AND is_control=0 GROUP BY network_id"""
        ).fetchall()
        weights = {
            (str(row["network_id"] or ""), ""): int(row["n"])
            for row in signal_networks
        }
    eligible_by_network = {}
    for key in pool:
        source = normalized[key][1] if strict_comparison else ""
        source = source or ""
        eligible_by_network.setdefault((str(key[1] or ""), source), []).append(key)

    if weights and any(group in weights for group in eligible_by_network):
        eligible_networks = [group for group in eligible_by_network if group in weights]
        total_weight = sum(weights[group] for group in eligible_networks)
        if strict_comparison:
            existing_rows = db._conn.execute(
                """SELECT network_id, admission_source, COUNT(*) AS n
                     FROM watch_windows
                    WHERE design_version=? AND is_control=1
                    GROUP BY network_id, admission_source""",
                (config.CONTROL_DESIGN_VERSION,),
            )
            existing = {
                (
                    str(row["network_id"] or ""),
                    str(row["admission_source"] or ""),
                ): int(row["n"])
                for row in existing_rows
            }
        else:
            existing = {
                (str(row["network_id"] or ""), ""): int(row["n"])
                for row in db._conn.execute(
                    """SELECT network_id, COUNT(*) AS n FROM watchlist
                       WHERE active=1 AND is_control=1 GROUP BY network_id"""
                )
            }
        picks = []
        for _ in range(pick_count):
            available = [network for network in eligible_networks
                         if eligible_by_network[network]]
            if not available:
                break
            target_total = sum(existing.values()) + 1
            network = max(
                available,
                key=lambda item: (
                    target_total * weights[item] / total_weight - existing.get(item, 0),
                    weights[item],
                ),
            )
            choices = eligible_by_network[network]
            pick = (
                min(choices, key=lambda item: policy.score(
                    "control", item[0], str(item[1] or "")
                ))
                if policy is not None else rng.choice(choices)
            )
            eligible_by_network[network].remove(pick)
            picks.append(pick)
            existing[network] = existing.get(network, 0) + 1
    else:
        picks = (
            sorted(pool, key=lambda item: policy.score(
                "control", item[0], str(item[1] or "")
            ))[:pick_count]
            if policy is not None else rng.sample(pool, pick_count)
        )
    added = 0
    for addr, net in picks:
        if db.admit_control(
            addr, net, config.CONTROL_WATCH_HOURS, recorded_at,
            admission_price_usd=normalized[(addr, net)][0],
            design_version=config.CONTROL_DESIGN_VERSION,
            admission_source=normalized[(addr, net)][1],
        ):
            added += 1
    return added


def admit_signal_comparison_windows(
    db: RecorderDB,
    candidates: Sequence[tuple],
    recorded_at: str,
    policy: EVMAdmissionPolicy | None = None,
    admitted_signals: set[str] | None = None,
    stats: dict[str, int] | None = None,
    static_items: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> int:
    """Admits v3 signals only when they appear in the same candidate list used for the control group."""
    candidate_prices: dict[tuple[str, str], tuple[float, str, Any]] = {}
    for candidate in candidates:
        addr, net, price, admission_source = candidate[:4]
        candidate_created = candidate[4] if len(candidate) >= 5 else None
        try:
            value = float(price) if price is not None else None
        except (TypeError, ValueError):
            continue
        if value is not None and math.isfinite(value) and value > 0:
            candidate_prices[(addr, str(net or ""))] = (
                value, admission_source, candidate_created,
            )

    added = 0
    rows = db._conn.execute(
        """SELECT s.id, s.token_address, s.network_id, s.signal_type
             FROM signal_events s
            WHERE s.signal_type IN (?, ?)
              AND s.recorded_at = ?""",
        (*config.TRIGGER_SIGNAL_TYPES, recorded_at),
    ).fetchall()
    for row in rows:
        key = (row["token_address"], str(row["network_id"] or ""))
        admission = candidate_prices.get(key)
        if admission is None:
            continue
        _stored, _observed = stored_age(db, *key)
        candidate_created = admission[2]
        if static_items and key in static_items:
            _write_filter_static(
                db, static_items[key], key[0], str(key[1] or ""), recorded_at,
                replace_invalid=True,
            )
            _stored, _observed = stored_age(db, *key)
        verdict = stored_age_verdict(db, *key, recorded_at)
        if verdict == AGE_UNKNOWN and candidate_created is not None:
            verdict = age_verdict(candidate_created, recorded_at, recorded_at)
        if verdict != AGE_OK:
            if stats is not None:
                stats["comparison_age_rejected"] = (
                    stats.get("comparison_age_rejected", 0) + 1
                )
            continue
        if policy is not None:
            existing = db._conn.execute(
                "SELECT active FROM watchlist WHERE token_address=? AND network_id=?",
                key,
            ).fetchone()
            if admitted_signals is not None and existing is None and str(row["id"]) not in admitted_signals:
                continue
            if existing is None and not policy.allows(
                "signal", key[0], key[1], str(row["id"]),
            ):
                continue
        price, admission_source, _candidate_created = admission
        if db.add_signal_comparison_window(
            key[0], key[1], row["signal_type"], row["id"],
            config.WATCH_HOURS, recorded_at, price,
            config.CONTROL_DESIGN_VERSION, admission_source,
        ):
            added += 1
    return added


AGE_OK = "ok"
AGE_TOO_YOUNG = "too_young"
AGE_UNKNOWN = "unknown"


def token_age_days(created_at: Any, now_iso: str) -> float | None:
    """Token age in days as of `now_iso`, or None if it cannot be determined.


    `token_created_at` is **seconds-since-epoch stored as text**, not a readable
    date, so `julianday()` on it silently returns NULL — which is why it is
    computed here in Python, not in SQL.


    A negative age (creation date in the future) is not an age: it returns None
    and is treated as unknown, not as old — otherwise a clock skew becomes an
    open gate.

    """
    created = _created_epoch(created_at)
    if created is None:
        return None
    now = _created_epoch(now_iso)
    if now is None:
        return None
    age = (now - created) / 86400.0
    return age if age >= 0 else None


def _created_epoch(value: Any) -> float | None:
    """Normalize upstream creation timestamps to Unix seconds.

    One parser for every consumer: admission, labeler, and the EVM gate all
    read through ``features.epoch_of`` so a format one accepts, all accept.
    Epoch ``0``/negative is not a creation date — unknown, never "old".
    """
    epoch = epoch_of(value)
    return float(epoch) if epoch is not None else None


def age_verdict(
    created_at: Any, now_iso: str, observed_at: Any = None,
) -> str:
    """Age-gate verdict with an optional stamp for when the date became known."""

    if not config.MIN_TOKEN_AGE_DAYS:
        return AGE_OK
    if observed_at is not None:
        observed = _created_epoch(observed_at)
        now = _created_epoch(now_iso)
        if observed is None or now is None or observed > now:
            return AGE_UNKNOWN
    age = token_age_days(created_at, now_iso)
    if age is None:
        return AGE_UNKNOWN
    return AGE_OK if age >= config.MIN_TOKEN_AGE_DAYS else AGE_TOO_YOUNG


def quarantine_active_age_violations(
    db: RecorderDB, recorded_at: str, stats: dict[str, int],
) -> int:
    """Stop post-gate invalid active watches without deleting their history."""
    quarantined = db.quarantine_age_invalid_active(
        recorded_at,
        config.MIN_TOKEN_AGE_DAYS,
        since_iso=config.AGE_GATE_ENABLED_AT,
    )
    stats["age_active_quarantined"] = quarantined
    if quarantined:
        db.bump_counter("age_gate_active_quarantined_total", quarantined)
        db.set_meta("age_gate_active_quarantined_last_at", recorded_at)
    return quarantined


def _token_lookup_key(value: Any, network_id: Any = None) -> str | None:
    """Canonical address key: EVM is case-insensitive, Solana is not."""
    return extract.canonical_token_address(value, network_id)


def stored_age(db: RecorderDB, addr: str, net: str) -> tuple[str | None, str | None]:
    key = _token_lookup_key(addr, net)
    row = db._conn.execute(
        """SELECT token_created_at, token_created_at_observed_at
            FROM token_static WHERE token_address=? AND network_id=?""",
        (key, net),
    ).fetchone()
    if row is None:
        return None, None
    return row["token_created_at"] or None, row["token_created_at_observed_at"] or None


def _item_with_network(item: Mapping[str, Any], network_id: str) -> Mapping[str, Any]:
    """Supply a requested network only when the upstream item omitted it."""
    if extract.token_list_network(item):
        return item
    out = dict(item)
    token = item.get("token")
    if isinstance(token, Mapping):
        token_copy = dict(token)
        token_copy["networkId"] = str(network_id)
        out["token"] = token_copy
    else:
        out["networkId"] = str(network_id)
    return out


def stored_age_verdict(db: RecorderDB, addr: str, net: str, recorded_at: str) -> str:
    created, observed = stored_age(db, addr, net)
    if not config.MIN_TOKEN_AGE_DAYS:
        return AGE_OK
    if created is None:
        return AGE_UNKNOWN
    if observed is None and _created_epoch(recorded_at) >= _created_epoch(
        config.AGE_GATE_ENABLED_AT
    ):
        return AGE_UNKNOWN
    return age_verdict(created, recorded_at, observed)


def stored_created_at(db: RecorderDB, addr: str, net: str) -> str | None:
    return stored_age(db, addr, net)[0]


def _age_value_is_valid(value: Any, recorded_at: str) -> bool:
    created = _created_epoch(value)
    now = _created_epoch(recorded_at)
    return created is not None and now is not None and created <= now


async def resolve_ages(
    client: Any,
    db: RecorderDB,
    keys: Sequence[tuple[str, str]],
    recorded_at: str,
    stats: dict[str, int],
    gecko_fallback: Any | None = None,
) -> None:
    """Fetches the creation date of unknown candidates and pins it in `token_static`.


    The signal itself **carries no token age**: the feed event holds
    `tokenAddress` and `networkId` and the post's own `createdAt` — no token
    object at all. So the age gate needs a source, and `filterTokens` asks for an
    address by name with no popularity requirement and carries `token.createdAt`.


    The cost is at most one call per cycle: new candidates are **1.11 tokens per
    cycle** (4 at the observed maximum) and the batch carries 150 — against nine
    candle calls made in the same cycle. And the result is stored, so the same
    token is never asked again: a later signal on a rejected token is judged from
    the stored date with no network.


    `gecko_fallback` (2026-08-27): when filterTokens cannot find the token at all
    ("missing", not "error") we ask GeckoTerminal for the earliest
    `pool_created_at`. Measured: it resolves 17/18 of fomo's unknowns. The two
    sources are independent, so if one is blocked the other fills in. GT failure
    is silent with no failure counter: it is a fallback, not a primary path — only
    its success is counted (`age_gecko_resolved`), and its worst outage leaves the
    state as it was.

    """
    if not keys:
        return
    due_keys = [
        key for key in keys
        if db.age_lookup_due(
            key[0], key[1], recorded_at,
            config.AGE_MISSING_RETRY_SECONDS,
            config.AGE_ERROR_RETRY_SECONDS,
        )
    ]
    if not due_keys:
        return
    symbols = [f"{addr}:{net}" for addr, net in due_keys]
    try:
        raw = await _fetch_filter_tokens_raw(client, symbols)
    except Exception as exc:  # noqa: BLE001 — the signal is already durable

        stats["age_lookup_failed"] += len(due_keys)
        db.note_error("last_error_age_lookup", f"{recorded_at}: {_exc_note(exc)}")
        db.bump_counter("age_lookup_failed_streak")
        db.bump_counter("age_lookup_failed_total", len(due_keys))
        with db.batch():
            for addr, net in due_keys:
                db.set_age_lookup_state(addr, net, "error", recorded_at)
        return
    by_key: dict[tuple[str, str], Any] = {}
    by_address: dict[str, list[Any]] = {}
    for item in extract.unwrap_token_list(raw):
        a = extract.token_list_address(item)
        if a:
            net = extract.token_list_network(item)
            by_key[(a.lower(), net)] = item
            by_address.setdefault(a.lower(), []).append(item)
    ambiguous_addresses: set[str] = set()
    requested_counts = {}
    for addr, _net in due_keys:
        requested_counts[addr.lower()] = requested_counts.get(addr.lower(), 0) + 1
    with db.batch():
        for addr, net in due_keys:
            item = by_key.get((addr.lower(), str(net or "")))
            candidates = by_address.get(addr.lower(), [])
            if item is None and len(candidates) == 1:
                candidate = candidates[0]
                if (
                    requested_counts.get(addr.lower()) == 1
                    and extract.token_list_network(candidate) == ""
                ):
                    item = candidate
                elif candidates:
                    ambiguous_addresses.add(addr.lower())
            elif item is None and candidates:
                ambiguous_addresses.add(addr.lower())
            if item is None:
                db.set_age_lookup_state(addr, net, "missing", recorded_at)
                if gecko_fallback is not None:
                    gecko_created = await _gecko_resolve_age(
                        gecko_fallback, db, addr, net, recorded_at,
                    )
                    if gecko_created is not None:
                        stats["age_gecko_resolved"] = (
                            stats.get("age_gecko_resolved", 0) + 1
                        )
                        stats["age_resolved"] += 1
                continue
            tok = item.get("token") if isinstance(item.get("token"), Mapping) else {}
            created = tok.get("createdAt") or item.get("createdAt")
            item = _item_with_network(item, str(net))
            if created in (None, "") or not _age_value_is_valid(created, recorded_at):
                db.set_age_lookup_state(addr, net, "missing", recorded_at)
                if gecko_fallback is not None:
                    gecko_created = await _gecko_resolve_age(
                        gecko_fallback, db, addr, net, recorded_at,
                    )
                    if gecko_created is not None:
                        stats["age_gecko_resolved"] = (
                            stats.get("age_gecko_resolved", 0) + 1
                        )
                        stats["age_resolved"] += 1
                continue
            if _write_filter_static(db, item, addr, net, recorded_at, replace_invalid=True):
                stats["age_resolved"] += 1
            db.set_age_lookup_state(addr, net, "ok", recorded_at)
    if ambiguous_addresses:
        schema_drift = len(ambiguous_addresses)
        db.bump_counter("age_lookup_schema_drift_total", schema_drift)
        db.note_error(
            "last_error_age_lookup_schema",
            f"{recorded_at}: missing networkId for {schema_drift} ambiguous age items",
        )
    db.set_meta("age_lookup_last_ok_at", recorded_at)
    db.set_meta("age_lookup_failed_streak", "0")


async def _gecko_resolve_age(
    gecko: Any, db: RecorderDB, addr: str, net: str, recorded_at: str,
) -> str | None:
    """Asks GeckoTerminal for the age and writes it if valid. Returns the value or None.


    The same validity rule applied to fomo (`_age_value_is_valid`): a future or
    malformed value is rejected — we do not write a corrupt age from an alternate
    source. The write goes through `set_static_created_at` with an explicit
    observation stamp, so the gate can tell a documented age from one with no
    provenance.

    """
    canonical = extract.canonical_token_address(addr)
    current, _observed = stored_age(db, canonical, str(net or ""))
    if current not in (None, ""):
        return None                      # an age already exists — not our business

    try:
        created = await gecko.pool_created_at(addr, str(net or ""))
    except Exception:  # noqa: BLE001 — the fallback never raises

        return None
    if created in (None, "") or not _age_value_is_valid(created, recorded_at):
        return None
    db.set_static_created_at(addr, str(net or ""), str(created), recorded_at)
    return str(created)


def _opens_window(existing: Any) -> bool:
    """Will `upsert_watch` open a new window for this row?


    Its update clause is `WHERE watchlist.active=0 OR watchlist.is_control=1`, so
    three cases open a window: no row at all, an expired row, or a control row
    being converted to a signalled one. Only the active non-control row gets no
    window — its window is already running.

    """
    if existing is None:
        return True
    return not existing["active"] or bool(existing["is_control"])


def _evm_age_exceeds_cap(
    db: RecorderDB, token: str, network: str, recorded_at: str,
) -> bool:
    """Does an EVM token's age exceed the upper admission cap?


    It reads only the stored date (no network call): the cap acts at the moment
    the window opens, and the stored date was just judged by the minimum age gate,
    whose unknowns were already rejected there. Returns False when the cap or the
    date is missing — a "not above the cap" verdict here opens no door: the
    minimum gate rejects the unknown.

    """
    cap = float(config.EVM_MAX_TOKEN_AGE_DAYS or 0)
    if cap <= 0:
        return False
    created, _observed = stored_age(db, token, network)
    if created is None:
        return False
    age = token_age_days(created, recorded_at)
    return age is not None and age > cap


def _evm_admission_networks() -> frozenset[str]:
    """Networks subject to the upper age cap: the enabled EVM networks.


    Read from settings, not from the policy object: the cap is a fixed admission
    rule that does not change with congestion, and a gate that depends on "is
    there even a policy" is a hole.

    """
    return frozenset(str(net) for net in config.EVM_NETWORKS)


async def record_feed(
    db: RecorderDB,
    raw_feed: Any,
    recorded_at: str,
    rank_lookup,
    rank_lookups,
    policy: EVMAdmissionPolicy,
    stats: dict[str, int],
    client: Any = None,
) -> set[str]:
    """Saves the signals first, then attempts the watch without tying the two fates together."""

    events = extract.unwrap_feed(raw_feed)
    rows = []
    inserted: set[str] = set()
    with db.batch():
        db.insert_snapshot("feed", raw_feed, recorded_at)
        newest = max(
            (str(event.get("createdAt")) for event in events if event.get("createdAt")),
            default=None,
        )
        if newest:
            db.set_meta("last_feed_event_at", newest)
        for event in events:
            row = extract.extract_signal_event(
                event, recorded_at, rank_lookup, rank_lookups,
            )
            if row is None:
                continue
            rows.append(row)
            if db.insert_signal(row):
                inserted.add(str(row["id"]))
                stats["signals"] += 1

    triggers = [r for r in rows if r["signal_type"] in config.TRIGGER_SIGNAL_TYPES]
    # List state per trigger fetched once: read by candidate sorting and the admission loop.

    watch_state: dict[tuple[str, str], Any] = {}
    for row in triggers:
        key = (str(row["token_address"]), str(row["network_id"] or ""))
        if key not in watch_state:
            watch_state[key] = db._conn.execute(
                """SELECT active, is_control FROM watchlist
                    WHERE token_address=? AND network_id=?""",
                key,
            ).fetchone()

    # Age of the candidates that will get a window opened, before the verdict.
    # The stored value usually suffices, so no call for it; only the unknown goes
    # to the network, and one call carries 150.

    if client is not None and config.MIN_TOKEN_AGE_DAYS:
        unknown = [
            key for key, existing in watch_state.items()
            if _opens_window(existing)
            and stored_age_verdict(db, key[0], key[1], recorded_at) == AGE_UNKNOWN
        ]
        gecko = None
        if config.AGE_GECKO_FALLBACK:
            # A client per cycle, not a shared one: the session is tied to the
            # asyncio loop, and the recorder runs one long-lived loop so there is
            # no problem, but the tests run each test in a fresh loop — a shared
            # client then wakes the curl_cffi timer from a dead loop (measured:
            # PytestUnraisableExceptionWarning via filterwarnings=error). The
            # creation cost is zero: the session is built on the first real call,
            # and cycles with no unknowns touch the network not at all.

            from gecko_terminal import GeckoTerminalClient

            gecko = GeckoTerminalClient()
        await resolve_ages(client, db, unknown, recorded_at, stats,
                           gecko_fallback=gecko)

    admitted: set[str] = set()
    policy_networks = set(policy.networks) if policy is not None else set()
    for row in triggers:
        token = str(row["token_address"])
        network = str(row["network_id"] or "")
        existing = watch_state[(token, network)]
        # The gate rules every window that opens — the new ones **and
        # reactivations**: `upsert_watch` revives an expired row (active=0) or a
        # control row with a new window, so had it been confined to the new, the
        # young ones would slip in through revival.

        if _opens_window(existing):
            verdict = stored_age_verdict(db, token, network, recorded_at)
            if verdict != AGE_OK:
                stats["age_rejected"] += 1
                if verdict == AGE_UNKNOWN:
                    stats["age_unknown"] += 1
                continue
            # Upper age cap for EVM admission (2026-08-29): the minimum age gate
            # alone admits two-year-old tokens and drops the network into
            # admission pause (measured 08-29: 26 admissions over a year old out
            # of 8453). Not applied to Solana nor to unknown age (the minimum
            # gate handles that).

            if (
                config.EVM_MAX_TOKEN_AGE_DAYS
                and network in _evm_admission_networks()
                and _evm_age_exceeds_cap(db, token, network, recorded_at)
            ):
                stats["evm_max_age_rejected"] = (
                    stats.get("evm_max_age_rejected", 0) + 1
                )
                continue
            # Pre-signal runup gate: applied to the new/reactivated window just
            # like the age gate itself. The signal itself is always saved above;
            # what is rejected here is only opening the watch (otherwise it
            # returns through revival).

            row_ts = row.get("ts")
            t0 = int(row_ts) if str(row_ts or "").isdigit() else None
            if t0 is not None and runup_verdict(db, token, network, t0) == RUNUP_LATE:
                stats["runup_rejected"] = (
                    stats.get("runup_rejected", 0) + 1
                )
                continue
        # The EVM admission gate applies to every window about to open — new ones
        # and reactivations alike (2026-08-29): its check used to be confined to
        # the new, so reactivation bypassed the pause entirely and fed the queue
        # through a back door.

        if (
            _opens_window(existing)
            and network in policy_networks
            and not policy.allows("signal", token, network, str(row["id"]))
        ):
            if str(row["id"]) in inserted:
                stats["evm_admission_deferred"] += 1
            continue
        if (
            existing is None
            and db.active_watch_count(is_control=0) >= config.WATCHLIST_CAP
        ):
            continue
        try:
            added = db.upsert_watch(
                token_address=token,
                network_id=network,
                source=row["signal_type"],
                entry_signal_id=row["id"],
                watch_hours=config.WATCH_HOURS,
                now_iso=recorded_at,
                admission_price_usd=row.get("price_usd"),
            )
            admitted.add(str(row["id"]))
            if added:
                stats["watch_added"] += 1
                watch_state[(token, network)] = {"active": 1, "is_control": 0}
                # (fv16) socials from DEX Screener for a token just admitted —
                # one call per token in its lifetime with us: stored in
                # token_static (written once), never asked again no matter how
                # many signals repeat. Failure is fully silent: the layer is
                # supporting, and its absence leaves the two columns NULL (not
                # measured).

                if config.DEX_SCREENER_SOCIALS:
                    try:
                        await _enrich_dex_socials(db, token, network)
                    except Exception:  # noqa: BLE001 — enrichment must not sink the admission

                        pass
        except Exception as exc:  # noqa: BLE001 - signal is already durable
            stats["errors"] += 1
            db.note_error("last_error_watch_admission", f"{recorded_at}: {_exc_note(exc)}")
    if stats["evm_admission_deferred"]:
        db.bump_counter("evm_admission_deferred_total", stats["evm_admission_deferred"])
    if stats.get("evm_max_age_rejected"):
        db.bump_counter("evm_max_age_rejected_total", stats["evm_max_age_rejected"])
    if stats["age_rejected"]:
        db.bump_counter("age_rejected_total", stats["age_rejected"])
    if stats.get("age_gecko_resolved"):
        db.bump_counter("age_gecko_resolved_total", stats["age_gecko_resolved"])
    _persist_runup_rejection(db, stats)
    return admitted


async def _enrich_dex_socials(db: RecorderDB, token: str, network: str) -> None:
    """Asks DEX Screener for the social channels and stores them in token_static.


    One call per token in its lifetime with us — once the column has been written
    (even an explicit NULL from a failed attempt) it is never asked again:
    enrichment is an opportunity, not an obligation. `social_match_fomo_dex`
    compares the two sources' visibility: both see socials or neither does = 1
    (agreement); exactly one does = 0 (conflict — a forged-profile pattern). A
    failed call writes nothing (NULL = not measured).

    """
    from dex_screener import DexScreenerClient

    net = str(network or "")
    row = db._conn.execute(
        """SELECT twitter, telegram, discord, social_channels_dex
             FROM token_static
            WHERE token_address=? AND network_id=? ORDER BY recorded_at DESC
            LIMIT 1""",
        (token, net),
    ).fetchone()
    if row is None or row["social_channels_dex"] is not None:
        return                       # asked before (successfully or with a documented failure)

    client = DexScreenerClient()
    try:
        channels = await client.social_channels(token, net)
    finally:
        await client.aclose()
    if channels is None:
        return                       # not indexed / broken — stays NULL, ask again later?
                                     # No: the column stays NULL but we do not make a
                                     # habit of asking every cycle — a fuzzy failure burns calls.

    fomo_has = 1 if (row["twitter"] or row["telegram"] or row["discord"]) else 0
    dex_has = 1 if channels > 0 else 0
    match = 1 if fomo_has == dex_has else 0
    with db.batch():
        db._conn.execute(
            """UPDATE token_static
                  SET social_channels_dex=?, social_match_fomo_dex=?
                WHERE token_address=? AND network_id=?""",
            (channels, match, token, net),
        )


async def _fetch_thesis_raw(client: Any, token_address: str, network_id: str) -> Any:
    """Raw GET /feed/token/thesis — we bypass get_token_thesis_feed because it maps."""

    from fomo_api.config import settings

    params = {
        "tokenAddress": token_address,
        "networkId": int(network_id) if str(network_id).isdigit() else network_id,
        "threshold": config.SOCIAL_THRESHOLD,
    }
    return await client._get(settings.upstream_feed_token_thesis_path, params)


async def _fetch_token_details_raw(client: Any, token_address: str, network_id: str) -> Any:
    """Raw POST /proxy/tokenDetails — carries top10HoldersPercent and the holder count.


    `tokenId` **must** be "address:networkId" as in getBarsNew; the bare address
    makes the server throw a 502 from Cloudflare (looks like an outage, is a malformed request).

    """
    from fomo_api.config import settings

    body = {"tokenId": f"{token_address}:{network_id}" if network_id else token_address}
    return await client._post(settings.upstream_token_details_path, body)


async def _fetch_hodlers_raw(client: Any, token_address: str, network_id: str) -> Any:
    """Raw GET /hodlers/top — top-holders detail (also yields top1).


    The `tokens` parameter is a JSON string of a list of objects, the source's shape, not a single address.

    """
    from fomo_api.config import settings

    net: Any = int(network_id) if str(network_id).isdigit() else network_id
    params = {"tokens": json.dumps([{"address": token_address, "networkId": net}])}
    return await client._get(settings.upstream_hodlers_top_path, params)


async def refresh_leaderboard(
    lb: LeaderboardCache, db: RecorderDB, now_mono: float, recorded_at: str
) -> None:
    """Refreshes the leaderboard when due, archives the raw, and logs failure explicitly.


    Without archiving, every leader's trajectory is lost forever — the rank used
    to be read and thrown away every hour. And without failure logging, a stale
    map (failed refresh or empty envelope ⇒ the previous map) stays silent
    forever: `last_error_leaderboard` is shown on the dashboard like the other
    sources.


    **Raw per period in an independent source** (`leaderboard` /
    `leaderboard_24h` …): merging them into one source mixes four different lists
    into an archive that cannot be untangled. `raw_by_period` returns only the
    fresh periods (the last attempt) — archiving an old envelope under a new
    timestamp would put in the archive a leaderboard we never fetched. A period
    that failed while its sisters succeeded does not fail the cycle, but it is
    logged so the partial outage is not silent.

    """
    was_stale = lb.is_stale(now_mono)
    refreshed = await lb.maybe_refresh(now_mono)
    if refreshed:
        for period, raw in lb.raw_by_period.items():
            if raw is None:
                continue
            source = "leaderboard" if period == "all" else f"leaderboard_{period}"
            db.insert_snapshot(source, raw, recorded_at)
        missing = lb.failed_periods()
        if missing:
            db.set_meta(
                "last_error_leaderboard",
                f"{recorded_at}: periods without a lookup: {', '.join(missing)}",
            )
    elif was_stale and not refreshed:
        db.set_meta(
            "last_error_leaderboard",
            f"{recorded_at}: refresh failed or empty — keeping previous lookup",
        )


async def run_macro_bars_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """Pulls whole-market candles (SOL/WETH/WBTC) once an hour.


    The market-regime reference: any token's return read in isolation from the
    market makes the signal effect look like an up-day effect. Not driven by the
    watchlist — fixed assets in config.MACRO_BARS. Stored in token_bars at hourly
    resolution so it never collides with the watch candles (5 minutes), and the
    labeler never touches it (no signal, no watch on them). The stamp in meta
    drives the cadence; a cycle within the hour exits immediately with no network
    call.

    """
    stats = {"macro_rows": 0, "macro_errors": 0, "macro_no_data": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    last = db.get_meta("last_macro_bars_at")
    if last is not None:
        elapsed = (now_dt - datetime.fromisoformat(last)).total_seconds()
        if elapsed < config.MACRO_BARS_REFRESH_SECONDS:
            return stats

    to_ts = int(now_dt.timestamp())
    from_ts = to_ts - config.MACRO_BARS_SPAN_HOURS * 3600
    for i, (label, addr, net) in enumerate(config.MACRO_BARS):
        try:
            raw = await _fetch_bars_raw(
                client, addr, net, from_ts, to_ts, resolution=config.MACRO_BARS_RESOLUTION
            )
            rows = extract.extract_bars(
                raw, addr, net, config.MACRO_BARS_RESOLUTION, recorded_at
            )
            # "Success with no candles" is not success: were it permanent (bad
            # configuration) it would become eternal silence — the same documented
            # no_data patrol as in the candle cycle.

            if not rows:
                stats["macro_no_data"] += 1
            stats["macro_rows"] += db.insert_bars(rows)
            if rows:
                db.recompute_bar_flags(addr, net, config.MACRO_BARS_RESOLUTION)
        except Exception as exc:  # noqa: BLE001 — one asset must not sink the rest

            stats["macro_errors"] += 1
            db.note_error("last_error_macro", f"{recorded_at}: {label}: {_exc_note(exc)}")
        if i + 1 < len(config.MACRO_BARS):
            await sleep(config.BARS_PACING_SECONDS)
    # Stamp only with rows actually written: a total failure (fomo outage) or
    # total emptiness (responses with no candles) is retried next cycle like the
    # other sources — not postponed a full hour, and total emptiness is logged as
    # an error so it does not pass silently.

    if stats["macro_rows"] == 0:
        if stats["macro_errors"] == 0:
            db.set_meta(
                "last_error_macro",
                f"{recorded_at}: all {len(config.MACRO_BARS)} assets returned no data",
            )
        return stats
    db.set_meta("last_macro_bars_at", recorded_at)
    return stats


async def run_social_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """Captures the social layer for a slice of the watches (round-robin like the candles).


    A token with no discussion is recorded with zeros, not skipped: **silence is
    a signal**, and the run of zeros followed by a sudden spike is exactly what we want to capture.

    """
    stats = {"social_tokens": 0, "social_items": 0, "thesis_rows": 0, "social_errors": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (now_dt - timedelta(seconds=config.SOCIAL_REFRESH_SECONDS)).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.SOCIAL_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.social_fetch_due(
        limit=config.SOCIAL_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
    )

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        try:
            raw = await _fetch_thesis_raw(client, addr, net)
            row = extract.extract_social(raw, addr, net, recorded_at)
            db.insert_social(row)
            thesis_rows = extract.extract_thesis_items(raw, addr, net, recorded_at)
            stats["thesis_rows"] += db.insert_thesis_items(thesis_rows)
            stats["social_tokens"] += 1
            stats["social_items"] += row["thesis_total"]
            db.set_social_state(
                addr, net, "ok" if row["thesis_sampled"] else "empty",
                row["thesis_total"], recorded_at,
            )
        except Exception as exc:  # noqa: BLE001 — one token must not sink the slice

            stats["social_errors"] += 1
            try:
                db.set_social_state(addr, net, "error", 0, recorded_at)
            except Exception:  # noqa: BLE001 — as in bars: do not block the seal below

                pass
            db.note_error("last_error_social", f"{recorded_at}: {_exc_note(exc)}")
        if i + 1 < len(due):
            await sleep(config.SOCIAL_PACING_SECONDS)
    return stats


# The two holder sources are a module constant, not inline in the loop, so their
# length is known and the sleep after the last call is guarded: pacing is a gap
# **between** calls, and a sleep after the last one cuts cycle time for nothing
# (same guard as `if i + 1 < len(due)` in the candle and social cycles).

_HOLDERS_SOURCES: tuple[tuple[str, Any, Any], ...] = (
    ("token_details", _fetch_token_details_raw, extract.extract_token_details_holders),
    ("hodlers_top", _fetch_hodlers_raw, extract.extract_platform_holders),
)


async def run_holders_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """Measures ownership concentration and crowd positioning over time from two complementary sources.


    `market_ticks.top10_holders_pct` is dead (zero of 1,430,475): the
    trending/verified lists do not carry the key at all. And the on-chain check
    covers Solana only (2,929 of 3,044) and is completely silent on EVM (zero of
    2,378) — so concentration is unknown for every Ethereum token we have. The
    two sources here work on both networks and measure two different things
    (confirmed live 2026-08-09, not assumed):


    - `tokenDetails`: on-chain concentration — `top10HoldersPercent` + `holders`
      across the whole chain (83.6%, 90.1%, and 21.9% observed). Works on both
      EVM and Solana — the direct fix for the dead column.

    - `hodlers/top`: crowd positioning — fomo users who hold (276 of 947; 118 of
      14,371) with no share of supply at all, but with each position's cost,
      unrealized profit, holding duration, and the `isDev` flag. "Are the
      platform's holders underwater?" is a different question from "is ownership
      concentrated?". A first measurement: 50 of 50 holders losing on one token,
      versus 12 of 49 on another.


    Each is stored in its own row (`source` inside the primary key) so neither
    blinds the other's measurement, and we fabricate no absent value (FR-007).
    One source failing does not sink the second.


    And the `tokenDetails` response is extracted **twice**: ownership
    concentration into `token_holders`, and buy/sell flow into `token_flow` —
    **one fetch, two extractors, two tables**. The response carries (100% across
    300 archived responses) the buy/sell split and a full 5-minute layer, and we
    used to throw it all away. Zero extra calls. One extractor failing does not
    sink the other: the flow sits in its own `try`.

    """
    stats = {
        "holders_tokens": 0, "holders_details": 0,
        "holders_top": 0, "holders_errors": 0, "flow_rows": 0,
    }
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (now_dt - timedelta(seconds=config.HOLDERS_REFRESH_SECONDS)).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.HOLDERS_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.holders_fetch_due(
        limit=config.HOLDERS_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
    )

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        first_seen = w["first_seen_at"]
        sig = w.get("entry_signal_id")
        is_control = int(w.get("is_control") or 0)
        top10: float | None = None
        got = 0

        for j, (source, fetch_fn, extract_fn) in enumerate(_HOLDERS_SOURCES):
            raw: Any = None  # stays None if the fetch raised — read in the flow branch below

            try:
                raw = await fetch_fn(client, addr, net)
                row = extract_fn(raw, addr, net, recorded_at, first_seen, sig, is_control)
                if row is not None:
                    db.insert_holders(row)
                    got += 1
                    if source == "token_details":
                        stats["holders_details"] += 1
                    else:
                        stats["holders_top"] += 1
                    if top10 is None:
                        top10 = row["top10_pct"]
            except Exception as exc:  # noqa: BLE001 — one source must not sink the rest

                stats["holders_errors"] += 1
                db.note_error(
                    "last_error_holders",
                    f"{recorded_at}: {source}: {_exc_note(exc)}",
                )
            # The flow from the **same** response: a second extractor on an
            # envelope already in hand, with no extra call. An independent `try`
            # so neither table sinks the other, and deliberately **outside** the
            # holders branch: the holders extractor returns None when ownership
            # shares are absent, so had the flow hung off it, it would have been
            # swallowed with them — it is present 100% of the time while they are
            # not.

            if source == "token_details" and raw is not None:
                try:
                    frow = extract.extract_token_flow(
                        raw, addr, net, recorded_at, first_seen, sig, is_control
                    )
                    if frow is not None:
                        db.insert_flow(frow)
                        stats["flow_rows"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["holders_errors"] += 1
                    db.note_error(
                        "last_error_holders",
                        f"{recorded_at}: flow: {_exc_note(exc)}",
                    )
            # A gap **between** calls, not after the last one.

            if i + 1 < len(due) or j + 1 < len(_HOLDERS_SOURCES):
                await sleep(config.HOLDERS_PACING_SECONDS)

        if got:
            stats["holders_tokens"] += 1
        db.set_holders_state(addr, net, "ok" if got else "error", top10, recorded_at)
    return stats


def _write_filter_static(
    db: RecorderDB, item: Any, addr: str, net: str, recorded_at: str,
    *, replace_invalid: bool = False,
) -> bool:
    """Fills the age hole from a filterTokens item. Returns True if it wrote something.


    The creation date used to come from the public lists alone, and a public
    list's condition is **popularity, not age**: 406 tokens appeared in
    trending/verified/most_held and so have a statics row, while 177 never
    appeared and have no row at all — a perfect 100% split, measured. So
    "unknown age" was not a class of new tokens but a class of unpopular ones
    (39.8% of them were < two days old at first retrieval, versus 42.4% among
    the knowns — no difference).


    And the fix costs not a single call: filterTokens is asked about **our own
    watches** every cycle, and its item shape **matches** the trending item shape
    (the top-level keys and the keys inside `token` are identical) so it carries
    `token.createdAt` — measured coverage 215 of 216 active watches, with zero
    retries. The item was in our hands in this very loop and we were discarding
    it.

    """
    net = str(net or "")
    if not db.static_exists(addr, net):
        st = extract.extract_token_static(item, recorded_at)
        if st is None:
            return False
        if not _age_value_is_valid(st.get("token_created_at"), recorded_at):
            st["token_created_at"] = None
            st["token_created_at_observed_at"] = None
        db.upsert_static(st)
        return True
    # The row exists and the age is missing (the source omitted it): the first answer carrying it fills the hole.
    tok = item.get("token") if isinstance(item.get("token"), Mapping) else {}
    created = tok.get("createdAt") or item.get("createdAt")
    if created in (None, "") or not _age_value_is_valid(created, recorded_at):
        return False
    canonical = extract.canonical_token_address(addr)
    current, observed = stored_age(db, canonical, net)
    if replace_invalid and current not in (None, ""):
        if _age_value_is_valid(current, recorded_at):
            if observed is None:
                current_epoch = _created_epoch(current)
                candidate_epoch = _created_epoch(created)
                if current_epoch == candidate_epoch:
                    return db.observe_static_created_at(
                        addr, net, current, recorded_at,
                    )
                return db.replace_unobserved_static_created_at(
                    addr, net, current, str(created), recorded_at,
                )
            return False
        return db.replace_static_created_at(
            addr, net, str(created), recorded_at,
        )
    return db.set_static_created_at(addr, net, str(created), recorded_at)


# The filterTokens slice counter: starts at -1 so the first cycle takes slice 0.
# Tests reset it (fixture) to guarantee determinism.

_FILTER_STRIDE_TURN = -1

# ----------------------------------------------------------------------------
# Pre-signal runup gate (2026-08-27): a signal on a token that already ran up a
# lot before it = latecomers, not the start of a run. Measured on 2,968 labelled
# signals: the median peak after the signal is similar across all stages (~28%),
# but the 48h final flips more negative the later the signal: -2.1% for the
# early, -14.4% for the late, -36.0% for the very late (>150% prior runup), and
# rug jumps 0.0%→7.1%. A late signal is a liquidity trap, not an opportunity:
# whoever bought "mid-run" is the whales' exit fuel.

# ----------------------------------------------------------------------------
RUNUP_OK = "ok"
RUNUP_LATE = "late"

# Cache of "late" verdicts (2026-08-28): a running token receives 5-10 signals a
# day and each one was re-querying the candles. The rejecting verdict is kept in
# memory for `RUNUP_RETRY_SECONDS` — the same pattern as
# `token_age_lookup_state`: a negative result is re-read, not recomputed, and
# expires when the window does (the run may calm down and a later signal may
# then be genuinely early). An "accepted" verdict is never stored: runup is
# fast-moving and every row deserves a fresh measurement.

_RUNUP_STATE: dict[tuple[str, str], tuple[int, str]] = {}

# Prior-runup measurement window: 24 hours of 5-minute candles (the same context
# window the market uses); 12 candles minimum to reject noise, and anything less
# = a new token with no history = an early signal by definition.

_RUNUP_WINDOW_BARS = 288
_RUNUP_MIN_BARS = 12


def pre_signal_runup(
    db: RecorderDB, token: str, network: str, t0: int,
) -> float | None:
    """log(signal price / oldest price in the 24h window before it) — runup before t0.


    The point-in-time law is preserved structurally: the query requires
    `ts <= t0` so no candle after the signal ever enters. Returns None when
    there is not enough history (a new token) — absence is neither runup nor
    decline.

    """
    rows = db._conn.execute(
        """SELECT c FROM token_bars
            WHERE token_address=? AND network_id=? AND resolution='5'
              AND c_suspect=0 AND ts <= ?
            ORDER BY ts DESC LIMIT ?""",
        (token, str(network or ""), int(t0), _RUNUP_WINDOW_BARS),
    ).fetchall()
    if len(rows) < _RUNUP_MIN_BARS:
        return None
    closes = [float(r[0]) for r in rows if r[0] is not None and float(r[0]) > 0]
    if len(closes) < _RUNUP_MIN_BARS or not closes[-1]:
        return None
    import math

    return math.log(closes[0] / closes[-1])   # rows descending: [0]=newest



def runup_verdict(
    db: RecorderDB, token: str, network: str, t0: int,
) -> str:
    """Runup gate verdict: RUNUP_OK or RUNUP_LATE (or OK if the filter is disabled).


    Rejecting verdicts are read from the cache inside the `RUNUP_RETRY_SECONDS`
    window — the same backoff philosophy as the age gate, keyed by address+network.

    """
    limit = float(getattr(config, "MAX_PRE_SIGNAL_RUNUP", 0) or 0)
    if limit <= 0:
        return RUNUP_OK                       # disabled — pre-filter behavior

    key = (token, str(network or ""))
    cached = _RUNUP_STATE.get(key)
    if cached is not None and t0 - cached[0] < int(
        getattr(config, "RUNUP_RETRY_SECONDS", 3600) or 3600
    ):
        return cached[1]                      # inside the truce window: from memory

    runup = pre_signal_runup(db, token, network, t0)
    if runup is None:
        return RUNUP_OK                       # no history = early by definition

    verdict = RUNUP_LATE if runup > limit else RUNUP_OK
    if verdict == RUNUP_LATE:
        # Only the rejection is stored: acceptance is fast-moving and deserves a fresh measurement every time.

        _RUNUP_STATE[key] = (t0, verdict)
    return verdict


def _persist_runup_rejection(db: RecorderDB, stats: dict[str, int]) -> None:
    """Persists the runup gate's rejections cumulatively in meta — a silent gate is blind.


    Discovered 2026-08-28: the gate was working (the rejected did not enter) but
    the counter was never written, so there was no way to know its effect or its
    failure. Zero rejections write nothing — no write noise in meta.

    """
    n = int(stats.get("runup_rejected", 0) or 0)
    if n:
        db.bump_counter("runup_rejected_total", n)


async def run_filter_tokens_cycle(
    client: Any,
    db: RecorderDB,
    recorded_at: str,
    watched: set[tuple[str, str]],
    captured: set[tuple[str, str]],
    sleep=asyncio.sleep,
) -> dict[str, int]:
    """Measures the watches that trending/verified **did not capture** in this cycle.


    The two public lists measure what is trending, while we watch what the
    signal pointed at — and the two sets drift apart quickly. Measured on the
    live database: 53 of 189 active watches with no tick for two hours, and 35
    never measured at all. `filterTokens` asks for our addresses by name and
    returns them regardless of their popularity.


    Constraints measured live, not assumed:

    - **Join by address, not by order**: every item carries `token.address`, and
      the source **silently drops the dead** (5 of 6 came back) — so the index slides and the order lies.

    - **No `insert_snapshot`**: we asked for our own watches, so every item
      becomes a row carrying its own `raw_json`; the snapshot is pure duplication
      (~94 MB/day for nothing). Unlike trending/verified, where the snapshot
      also preserves the unwatched.

    - The 18 count/unique columns stay `NULL` from this source — which is the
      correct thing (FR-007), and merging sources in
      `features.market_features` is what keeps this gap from masking a richer
      measurement that came from trending.

    """
    stats = {"filter_requested": 0, "filter_ticks": 0, "filter_errors": 0,
             "filter_static": 0}
    missing = sorted(watched - captured)
    if not missing:
        return stats

    # Alternating slices (2026-08-27): the same address keeps its position in
    # `missing` across cycles because the `sorted` order is stable, so picking
    # `idx % stride == turn` measures every token every N cycles. N=1 restores
    # the old behavior literally. An empty slice (nothing in its turn) costs no
    # call at all.

    stride = max(1, int(config.FILTER_TOKENS_STRIDE))
    if stride > 1:
        # A cycle counter pinned in the recorder module itself (bump_counter
        # returns no value). `sorted(missing)` is order-stable across cycles,
        # so an address's position in it is stable, and the `idx % stride ==
        # turn` slice makes every token measured every N cycles.

        global _FILTER_STRIDE_TURN
        _FILTER_STRIDE_TURN += 1
        cycle_turn = _FILTER_STRIDE_TURN % stride
        missing = [tok for i, tok in enumerate(missing)
                   if i % stride == cycle_turn]
        if not missing:
            return stats

    for start in range(0, len(missing), config.FILTER_TOKENS_BATCH):
        batch = missing[start : start + config.FILTER_TOKENS_BATCH]
        symbols = [f"{addr}:{net}" for addr, net in batch]
        stats["filter_requested"] += len(symbols)
        try:
            raw = await _fetch_filter_tokens_raw(client, symbols)
            items = extract.unwrap_token_list(raw)
            # The address alone is not enough: the same address can exist on
            # two networks. EVM casing may differ, so we lowercase the address and keep network_id in the key.

            by_key: dict[tuple[str, str], Any] = {}
            for item in items:
                a = extract.token_list_address(item)
                if a:
                    by_key[(a.lower(), extract.token_list_network(item))] = item
            with db.batch():
                for addr, _net in batch:
                    item = by_key.get((addr.lower(), str(_net or "")))
                    if item is None:
                        continue  # silently dropped (a delisted token) — does not break the batch

                    tick = extract.extract_market_tick(item, recorded_at, "filter")
                    if tick is None:
                        continue
                    if db.insert_tick(tick):
                        stats["filter_ticks"] += 1
                    # `dex_protocol` is entirely absent from trending raw (0 of
                    # 3,000) and this is its only source — we fill it only when the column is empty.

                    proto = extract.filter_item_protocol(item)
                    if proto:
                        db.set_static_protocol(
                            tick["token_address"],
                            str(tick["network_id"] or ""),
                            proto,
                        )
                    if _write_filter_static(
                        db,
                        item,
                        tick["token_address"],
                        str(tick["network_id"] or ""),
                        recorded_at,
                        replace_invalid=True,
                    ):
                        stats["filter_static"] += 1
        except Exception as exc:  # noqa: BLE001 — one batch must not sink the rest

            stats["filter_errors"] += 1
            db.note_error(
                "last_error_filter",
                f"{recorded_at}: {_exc_note(exc)}",
            )
        # A gap **between** batches, not after the last one. And in the common
        # case (≈53 addresses ⇒ one batch) this whole sleep was wasted with no call after it.

        if start + config.FILTER_TOKENS_BATCH < len(missing):
            await sleep(config.FILTER_TOKENS_PACING_SECONDS)
    return stats


async def run_traders_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """Builds profiles of the buyers whose names recur in our signals.


    `signal_events.buyer_id` has been stored since day one and there is no
    traders table in the database: 5,572 distinct ids, **3,202 of them with ≥3
    events**. So the question "is this buyer skilled or does he buy everything?"
    stayed unanswered even though the answer was one call away.


    The scheduling is round-robin like the holders cycle: never-fetched first,
    then the longest since fetched, then the most events. `INSERT OR REPLACE`
    on purpose (unlike every other table): the profile **changes** — followers
    and holding duration are not constants.

    """
    stats = {
        "traders_fetched": 0, "traders_rows": 0, "traders_errors": 0,
        "traders_missing": 0, "traders_malformed": 0,
    }
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.TRADERS_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.TRADERS_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.traders_fetch_due(
        limit=config.TRADERS_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        min_events=config.TRADERS_MIN_EVENTS,
    )

    def _seal() -> dict[str, int]:
        """The end-of-cycle seal: the shutout guard, then the success stamp. Two exits share it.


        Some were asked and no row came down: acceptable once, an outage if it
        repeats — `_note_shutout`. And this is not charged against errors: an
        error writes its own line, while a shutout is silent by nature.


        And the success stamp requires **a row to land** (or that nobody was
        asked), unlike the `chain_last_ok_at` rule "zero errors ⇒ stamp".
        Because a shutout is precisely zero errors with zero rows: stamping here
        would declare the shutout error "recovered" a minute later, killing the
        guard with the same false health that created it. And "nobody was
        asked" is a genuine success, not idleness — nothing deserved fetching so
        nothing failed — and without a stamp for it a quiet queue would stay red
        forever over an error long since healed.

        """
        if not stats["traders_errors"]:
            streak = _note_shutout(
                db, "traders", recorded_at,
                stats["traders_fetched"], stats["traders_rows"],
            )
            if streak == 0:
                db.set_meta("traders_last_ok_at", recorded_at)
        return stats

    # One malformed id kills the whole batch (400 on that one bad item alone),
    # so a single rotten id must not cost the remaining ninety-nine. And
    # `unsupported` not `error`, because retrying cannot fix it: the due-query
    # excludes it for good.

    wanted, malformed = [], []
    for w in due:
        (wanted if _UUID_RE.match(str(w["trader_id"] or "")) else malformed).append(
            str(w["trader_id"])
        )
    for tid in malformed:
        db.set_trader_state(tid, "unsupported", recorded_at)
        stats["traders_malformed"] += 1
    if not wanted:
        return _seal()

    cap = max(1, config.TRADERS_BATCH_MAX)
    for start in range(0, len(wanted), cap):
        chunk = wanted[start : start + cap]
        try:
            raw = await _fetch_traders_raw(client, chunk)
            stats["traders_fetched"] += len(chunk)
            rows = extract.extract_traders(raw, recorded_at) if raw is not None else {}
            for tid in chunk:
                row = rows.get(tid)
                if row is None:
                    # Absent from the response = no user with this id. The
                    # source deletes the unknown silently and does not error on it, so this is an answer, not a failure.

                    db.set_trader_state(tid, "empty", recorded_at)
                    stats["traders_missing"] += 1
                    continue
                db.upsert_trader(row)
                stats["traders_rows"] += 1
                db.set_trader_state(tid, "ok", recorded_at)
        except Exception as exc:  # noqa: BLE001 — one batch must not sink the cycle

            stats["traders_errors"] += 1
            db.note_error("last_error_traders", f"{recorded_at}: {_exc_note(exc)}")
            # And no state is written for those whose batch fell: state means
            # "we asked and this is the answer", and writing `error` here would
            # push them into the long wait timeout for the network's sins.

        if start + cap < len(wanted):
            await sleep(config.TRADERS_PACING_SECONDS)
    return _seal()


async def run_cycle(
    client: Any,
    db: RecorderDB,
    lb: LeaderboardCache,
    now_mono: float | None = None,
) -> dict[str, int]:
    """One cycle. Returns summarized counters. Swallows each source's errors separately."""

    now_mono = now_mono if now_mono is not None else time.monotonic()
    recorded_at = utcnow_iso()
    stats = {
        "signals": 0, "watch_added": 0, "comparison_signal_added": 0,
        "control_added": 0, "ticks": 0, "static": 0,
        "bars_tokens": 0, "bars_rows": 0, "social_tokens": 0, "social_items": 0,
        "thesis_rows": 0,
        "holders_tokens": 0, "holders_details": 0, "holders_top": 0,
        "flow_rows": 0, "filter_requested": 0, "filter_ticks": 0,
        "filter_static": 0,
        "traders_rows": 0,
        "evm_admission_backlog": 0, "evm_admission_percent": 100,
        "evm_admission_network_state": "{}",
        "evm_admission_paused": 0, "evm_admission_deferred": 0,
        "age_rejected": 0, "age_unknown": 0, "age_resolved": 0,
        "control_age_rejected": 0, "comparison_age_rejected": 0,
        "age_active_quarantined": 0,
        "age_lookup_failed": 0,
        "macro_rows": 0, "macro_no_data": 0, "errors": 0,
    }

    def _fail(where: str, exc: Exception) -> None:
        stats["errors"] += 1
        # **The error handler needs the database that is the failing
        # resource.** Measured 2026-08-17: `database is locked` in
        # `insert_holders` raised here too, taking down `_fail`, then the
        # loop's shield, so the process exited with code 1 and the task sat
        # `Ready` for three silent hours. Counting in memory is enough to
        # finish and report the cycle; losing a line in `meta` is cheaper than
        # losing the recorder for hours.

        try:
            db.bump_counter("errors_total")
        except Exception:  # noqa: BLE001 — a counter, not a measurement

            pass
        note = f"{recorded_at}: {_exc_note(exc)}"
        if not db.note_error(f"last_error_{where}", note):
            # Not fully silent either: the log is the last thing standing when
            # the database is locked, and `_log` itself is guarded so it cannot sink the cycle.

            _log(f"note_error failed for {where}: the database is not responding to writes")


    # First stop any watch admitted after the gate was enabled with a
    # young/unknown age. The historical window stays, but the row leaves the
    # admission count and the costly collection requests.

    try:
        quarantine_active_age_violations(db, recorded_at, stats)
    except Exception as exc:  # noqa: BLE001 — cleanup must not sink the recorder

        _fail("age_cleanup", exc)

    try:
        admission_policy = evm_admission_policy(db)
        stats["evm_admission_backlog"] = admission_policy.backlog
        stats["evm_admission_percent"] = admission_policy.percent
        stats["evm_admission_paused"] = 1 if admission_policy.paused else 0
        stats["evm_admission_network_state"] = json.dumps({
            network: {
                "backlog": state.backlog,
                "work_units": state.work_units,
                "capacity_units": state.capacity_units,
                "retry_count": state.retry_count,
                "rpc_healthy": state.rpc_healthy,
                "percent": state.percent,
                "paused": state.paused,
                "reason": state.reason,
            }
            for network, state in (admission_policy.by_network or {}).items()
        }, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 - admission must fail closed for EVM
        _fail_policy_networks = frozenset(str(n) for n in config.EVM_NETWORKS)
        admission_policy = _fail_closed_evm_admission(_fail_policy_networks)
        stats["evm_admission_backlog"] = -1
        stats["evm_admission_percent"] = 0
        stats["evm_admission_paused"] = 1
        stats["evm_admission_network_state"] = json.dumps({
            network: {"paused": True, "percent": 0, "reason": "policy_error"}
            for network in _fail_policy_networks
        }, sort_keys=True)
        db.note_error("last_error_evm_admission", f"{recorded_at}: {_exc_note(exc)}")

    # 0) Refresh the leaderboard (hourly) + archive the raw + log the failure.

    try:
        await refresh_leaderboard(lb, db, now_mono, recorded_at)
    except Exception as exc:  # noqa: BLE001 — do not fail the cycle

        _fail("leaderboard", exc)

    # 1) Raw feed → signal_events + watchlist for the triggers.

    admitted_signals: set[str] = set()
    try:
        raw_feed = await _fetch_feed_raw(client)
        if raw_feed is not None:
            admitted_signals = await record_feed(
                db, raw_feed, recorded_at, lb.lookup, lb.lookups,
                admission_policy, stats, client,
            )
    except Exception as exc:  # noqa: BLE001
        _fail("feed", exc)

    # 2) Raw trending + verified + mostHeld → snapshots + market_ticks + token_static.
    # `mostHeld` is a third discovery list: measured to give 25 items of which
    # **6 we never saw** in the other two lists, and it intersects them in 18
    # keys ⇒ the same extractor suffices, and it enters candidates, control,
    # and token_static automatically with no new code.

    watched = {(w["token_address"], str(w["network_id"] or "")) for w in db.active_watches()}
    # Keys captured in this cycle — whatever remains of them is covered by the filterTokens cycle.

    captured: set[tuple[str, str]] = set()
    # Control-group candidates: every token we see in this cycle that has not
    # been admitted before. Collected here for free — the data is already in hand, so no extra network call.

    control_candidates: list[tuple[str, str, float | None, str]] = []
    static_items: dict[tuple[str, str], Mapping[str, Any]] = {}
    for source, fetch in (
        ("trending", _fetch_trending_raw),
        ("verified", _fetch_verified_raw),
        ("most_held", _fetch_most_held_raw),
    ):
        try:
            raw = await fetch(client)
            if raw is None:
                continue
            with db.batch():  # ~65 ticks per cycle → one commit instead of 65

                db.insert_snapshot(source, raw, recorded_at)
                items = extract.unwrap_token_list(raw)
                for item in items:
                    tick = extract.extract_market_tick(item, recorded_at, source)
                    if tick is None:
                        continue
                    key = (tick["token_address"], str(tick["network_id"] or ""))
                    static_items[key] = item
                    control_candidates.append(
                        (key[0], key[1], tick.get("price_usd"), source,
                         extract.token_list_created_at(item))
                    )
                    # We record a tick for every watched token (the primary
                    # source of the time series). We also record statics for
                    # every first-seen token when it is watched.

                    if key in watched:
                        captured.add(key)
                        if db.insert_tick(tick):
                            stats["ticks"] += 1
                        if not db.static_exists(
                            tick["token_address"], str(tick["network_id"] or "")
                        ):
                            st = extract.extract_token_static(item, recorded_at)
                            if st is not None:
                                db.upsert_static(st)
                                stats["static"] += 1
        except Exception as exc:  # noqa: BLE001
            _fail(source, exc)

    # 2.3) Close the measurement gap: the watches no list captured in this
    # cycle. Without this step a token stops being measured the moment it falls
    # off the public lists — while still inside the 48-hour window we claim to
    # measure (53 of 189).

    try:
        filt = await run_filter_tokens_cycle(client, db, recorded_at, watched, captured)
        stats["filter_requested"] = filt["filter_requested"]
        stats["filter_ticks"] = filt["filter_ticks"]
        stats["filter_static"] = filt["filter_static"]
        if filt["filter_errors"]:
            stats["errors"] += filt["filter_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("filter", exc)

    # 2.2) Signal windows from the same universe and the same market price used for the control group.

    try:
        stats["comparison_signal_added"] = admit_signal_comparison_windows(
            db, control_candidates, recorded_at, policy=admission_policy,
            admitted_signals=admitted_signals, stats=stats,
            static_items=static_items,
        )
    except Exception as exc:  # noqa: BLE001
        _fail("comparison_signal", exc)

    # 2.25) The control group is admitted only in the same cycle that admitted
    # a comparison signal. Allowing 40 controls to be filled after one old
    # signal reintroduces the timing imbalance we are trying to prevent.

    if stats["comparison_signal_added"]:
        try:
            stats["control_added"] = admit_control_sample(
                db, control_candidates, recorded_at, policy=admission_policy,
                stats=stats, static_items=static_items,
            )
        except Exception as exc:  # noqa: BLE001 — the control group is an addition, it must not sink the cycle

            _fail("control", exc)

    # 2.42) Ownership concentration from the two sources — high concentration =
    # dump risk, and it is unknown today for every EVM token we have.

    try:
        hold = await run_holders_cycle(client, db, recorded_at)
        for key in ("holders_tokens", "holders_details", "holders_top", "flow_rows"):
            stats[key] = hold[key]
        if hold["holders_errors"]:
            stats["errors"] += hold["holders_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("holders", exc)

    # 2.45) Profiles of repeat buyers — "who bought?" was a question without
    # an answer even though buyer_id has been stored in every event since day one.

    try:
        trd = await run_traders_cycle(client, db, recorded_at)
        stats["traders_rows"] = trd["traders_rows"]
        if trd["traders_errors"]:
            stats["errors"] += trd["traders_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("traders", exc)

    # 2.5) OHLCV candles for a slice of the watches (the price source of truth for labelling).

    try:
        bars = await run_bars_cycle(client, db, recorded_at)
        stats["bars_tokens"] = bars["bars_tokens"]
        stats["bars_rows"] = bars["bars_rows"]
        if bars["bars_errors"]:
            stats["errors"] += bars["bars_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("bars", exc)

    # 2.75) The social layer for a slice of the watches.

    try:
        soc = await run_social_cycle(client, db, recorded_at)
        stats["social_tokens"] = soc["social_tokens"]
        stats["social_items"] = soc["social_items"]
        stats["thesis_rows"] = soc["thesis_rows"]
        if soc["social_errors"]:
            stats["errors"] += soc["social_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("social", exc)

    # 2.9) Whole-market candles (the market-regime reference — once an hour).

    try:
        mac = await run_macro_bars_cycle(client, db, recorded_at)
        stats["macro_rows"] = mac["macro_rows"]
        stats["macro_no_data"] = mac["macro_no_data"]
        if mac["macro_errors"]:
            stats["errors"] += mac["macro_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("macro", exc)

    # 3) Watchlist cleanup: deactivate what passed 48 hours.

    try:
        db.deactivate_expired(recorded_at)
        # Delete old snapshots — disabled by default (0 = keep forever).

        if config.SNAPSHOT_RETENTION_DAYS > 0:
            cutoff = (
                datetime.fromisoformat(recorded_at)
                - timedelta(days=config.SNAPSHOT_RETENTION_DAYS)
            ).isoformat()
            pruned = db.prune_snapshots(cutoff)
            if pruned:
                db.bump_counter("snapshots_pruned_total", pruned)
    except Exception as exc:  # noqa: BLE001
        _fail("cleanup", exc)

    db.set_meta("last_cycle_at", recorded_at)
    db.set_meta("evm_admission_last_state", json.dumps({
        "at": recorded_at,
        "backlog": stats["evm_admission_backlog"],
        "percent": stats["evm_admission_percent"],
        "paused": bool(stats["evm_admission_paused"]),
        "deferred": stats["evm_admission_deferred"],
        "networks": json.loads(stats["evm_admission_network_state"]),
    }, sort_keys=True))
    db.set_meta("last_cycle_stats", str(stats))
    # A fully successful cycle (no source errors at all) → a stamp that invalidates older meta errors on the dashboard.

    if stats["errors"] == 0:
        db.set_meta("last_ok_cycle_at", recorded_at)
    db.bump_counter("cycles_total")
    return stats


async def _maybe_rotate_client(
    client: Any, current_token: str, lb: LeaderboardCache, db: RecorderDB
) -> tuple[Any, str]:
    """Picks up the renewed token from disk every cycle.


    A fomo token lives 60 minutes; the api server writes a fresh token to disk
    before it expires. We read the disk, and if the token changed we rebuild
    the FomoClient (closing the old one) and point the cache at the new client.
    A read failure does not sink the cycle — we continue with the current
    client. No token value is ever printed (FR-013).


    Returns the (client, token) pair to use for the next cycle.

    """
    try:
        disk_token = _load_access_token()
    except Exception as exc:  # noqa: BLE001 — disk read failed; continue with the current one

        db.note_error("last_error_token_reload", f"{utcnow_iso()}: {type(exc).__name__}")
        return client, current_token
    if disk_token == current_token:
        return client, current_token
    # Rotate the token: build a new client and close the old one cleanly.

    new_client = _build_client(disk_token)
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 — closing the old client must not sink the recorder

        pass
    lb.set_client(new_client)
    try:
        db.set_meta("last_token_refresh_at", utcnow_iso())
    except Exception:  # noqa: BLE001 — the rotation already succeeded; don't waste it for a stamp

        pass
    _log("token rotated → client rebuilt")  # no secret value whatsoever

    return new_client, disk_token


# The productive keys: rows actually written. `filter_requested` is not one of
# them — it counts attempts and rises 108→190 exactly during the block
# (measured 2026-08-19T14:55); counted as produce it would blind the breaker to
# the very block it was built for.

_PRODUCTIVE_KEYS = (
    "signals", "ticks", "filter_ticks", "bars_rows", "social_items",
    "holders_details", "holders_top", "flow_rows", "traders_rows", "macro_rows",
)


def _cycle_is_dead(stats: dict[str, int]) -> bool:
    """A cycle that errored and wrote not a single row — not a "weak cycle" and not a "cycle with errors".


    Both conditions together are deliberate: errors with no produce means the
    source gave nothing, while produce alongside errors is the state of 274 of
    375 healthy cycles and must not be allowed to slow collection.

    """
    return stats.get("errors", 0) > 0 and not any(
        stats.get(key, 0) for key in _PRODUCTIVE_KEYS
    )


def _breaker_wait(dead_streak: int) -> float:
    """The wait between probes: doubles from a single cycle up to the cap.


    Capped twice — by seconds and by the exponent itself: `2.0 ** 1024` raises
    OverflowError, and a week-long block reaches that exponent, so the shield
    would crash every cycle forever. And the base is scaled by
    `CYCLE_SECONDS`, so a test that zeroes it cancels the wait on its own.

    """
    return min(
        float(config.UPSTREAM_BREAKER_MAX_SECONDS),
        config.CYCLE_SECONDS
        * 2.0 ** min(dead_streak - config.UPSTREAM_BREAKER_AFTER, 16),
    )


async def _upstream_alive(client: Any, reason: list[str] | None = None) -> bool:
    """A single probe: does the origin answer at all? It never raises, never writes, never counts.


    `limit=1` because what is wanted is the road's condition, not its payload —
    while `feedTypes` is a mandatory condition: measured 2026-08-20T00:24Z with
    a sound account that `/feed?limit=1` alone returns **400** "Invalid input:
    query.feedTypes - Required", and `_get` translates every ≥400 into
    "unavailable". So the probe was reading the road as dead while it was
    alive, meaning the breaker closed and never reopened even after the account
    was unblocked — and the log witnessed it: eleven closures and not one
    resumption except by restart.


    Hence two verdicts, not one: the call carries its condition, and then
    **the application's answer is itself life** — a 4xx code other than 403 and
    401 means the request arrived and was examined, which is all the breaker
    asks. So if the source adds another condition tomorrow, the shield will not
    kill collection a second time. And 403 alone is an identity block, 401 a
    bad token, and neither is fixed by running the cycle.


    And `reason` is an optional output, not a return value: "blocked" and
    "disconnected" are equal in the wait decision and utterly different in the
    remedy — a 403 on identity is solved with an account, an outage with
    patience. Whoever returns the exception as text here has the loop write it
    to `meta` for the dashboard to read, and whoever makes it a return value
    breaks `is False` in the test.

    """
    from fomo_api.config import settings

    params = {"feedTypes": list(config.FEED_TYPES), "limit": 1}
    try:
        await client._get(settings.upstream_feed_path, params)
        return True
    except Exception as exc:  # noqa: BLE001 — the failure **is** the requested answer

        details = getattr(exc, "details", None)
        status = details.get("upstream_status") if isinstance(details, dict) else None
        if isinstance(status, int) and 400 <= status < 500 and status not in (401, 403):
            return True                    # the application's answer is life, not death

        if reason is not None:
            reason.append(_exc_note(exc))
        return False


async def main_loop(cycles: int | None = None) -> None:
    """Runs the loop forever (cycles=None) or a set number of cycles (for verification)."""

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    current_token = _load_access_token()
    client = _build_client(current_token)
    lb = LeaderboardCache(
        client,
        size=config.LEADERBOARD_SIZE,
        refresh_seconds=config.LEADERBOARD_REFRESH_SECONDS,
        periods=config.LEADERBOARD_PERIODS,
        pacing_seconds=config.LEADERBOARD_PACING_SECONDS,
    )
    db.set_meta("schema_version", "1")
    # Documents that raw_json is written compressed — any later reader goes through db.decode_raw.

    db.set_meta("raw_encoding", "zlib")
    db.set_meta("started_at", utcnow_iso())
    n = 0
    dead_streak = 0
    try:
        while cycles is None or n < cycles:
            started = time.monotonic()
            try:
                # Pick up the renewed token on disk before the cycle (prevents
                # a 401 after the hour). **Inside** the shield: it used to sit
                # outside it while writing to the database, so a lock there
                # escaped the whole loop with no handling at all.

                client, current_token = await _maybe_rotate_client(
                    client, current_token, lb, db
                )
                # The block breaker — **after** rotation: the token stays
                # fresh even if we skip a single cycle, so a long block does
                # not bequeath a 401 when it ends. One probe instead of a full
                # cycle, and a wait that doubles; the first success puts
                # everything back at once, so a one-minute outage stays a
                # one-minute outage.

                if dead_streak >= config.UPSTREAM_BREAKER_AFTER:
                    why: list[str] = []
                    if await _upstream_alive(client, why):
                        _log(f"upstream back after {dead_streak} dead cycles")
                        dead_streak = 0
                    else:
                        wait = _breaker_wait(dead_streak)
                        # The rejection reason on the same line: this line is
                        # what gets read for the whole block, and a 403 block resembles an outage in nothing but the wait.

                        cause = why[0] if why else "no answer"

                        _log(
                            f"upstream blocked ({dead_streak} dead cycles) — "
                            f"{cause} — skipping cycle, next probe in {wait:.0f}s"
                        )
                        db.note_error(
                            "last_error_upstream_blocked",
                            f"{utcnow_iso()}: /feed probe rejected — {cause} — "
                            f"{dead_streak} dead cycles, waiting {wait:.0f}s",

                        )
                        dead_streak += 1
                        n += 1
                        await asyncio.sleep(wait)
                        continue
                stats = await run_cycle(client, db, lb, now_mono=started)
                # The drought is counted here, not in the shield: a cycle that
                # collapsed entirely is the shield's business, and this one **succeeded** in running and failed in collecting.

                dead_streak = dead_streak + 1 if _cycle_is_dead(stats) else 0
                _log(f"cycle {n}: {stats}")
            except Exception:  # noqa: BLE001 — a final shield around the whole cycle

                _log("cycle crashed:\n" + traceback.format_exc())
                # One connection for the process's lifetime: if it sticks on
                # an open transaction or a superseded read snapshot, every
                # following cycle would crash just as this one did — 22 minutes
                # and 40 seconds of silence on 2026-08-19 until a manual
                # restart. So the rescue goes here: after the crash, before
                # its counter and before the next cycle.

                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
                # And the shield must not die by its own hand: this very line
                # exited the process with code 1 at 2026-08-17T16:27 because the database was locked.

                try:
                    db.bump_counter("cycle_crashes")
                except Exception as meta_exc:  # noqa: BLE001
                    _log(f"cycle_crashes write failed: {type(meta_exc).__name__}")
            n += 1
            if cycles is not None and n >= cycles:
                break
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, config.CYCLE_SECONDS - elapsed))
    finally:
        await client.aclose()
        db.close()


def _log(msg: str) -> None:
    """Writes a line to the log with a timestamp. A write failure does not sink the recorder.


    It rotates the file past LOG_MAX_BYTES (a line a minute means eternal
    growth without it); we keep a single `.1` copy — the log is diagnostic,
    not an archive.

    """
    line = f"{utcnow_iso()} {msg}\n"
    try:
        import os

        if config.LOG_MAX_BYTES > 0 and os.path.getsize(config.LOG_PATH) > config.LOG_MAX_BYTES:
            os.replace(config.LOG_PATH, config.LOG_PATH + ".1")
    except OSError:
        pass  # file not there yet or locked — the write below handles it

    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — writing to the log must not sink what it logs
        pass


if __name__ == "__main__":
    import sys

    _cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        _cycles = int(sys.argv[1])
    asyncio.run(main_loop(cycles=_cycles))
