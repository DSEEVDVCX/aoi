"""Chain-layer cycle: ownership concentration measured from the blockchain, not from FOMO.

Deliberately separate from `recorder.py`, for two measured reasons:

1. **The cycle's budget is full.** The recorder cycle is 60 seconds, and
   politeness pacing toward the source was eating 27.5s of it. A second
   external source inside it means its slowness delays FOMO collection
   itself.
2. **The separation is what makes a 5-minute cadence possible.** 73 active
   Solana tokens ÷ 5 minutes = 15 calls per minute. The whole cycle can
   afford 13 calls for everything — whereas here the measured rate on the
   key is **224 calls per minute without a single failure** (concurrency 3,
   median 216ms), so we need only 7% of the comfortable capacity.

And why this layer at all when we have `token_holders`? Because FOMO gives
`top10` alone and only every ~25 minutes (measured gap median 25.0 over
19,440 pairs), so there is no top1 — i.e. no answer to "one whale or ten
distributed holders?", two different dangers — and no cadence that can keep
up with a dump unfolding in minutes. A single on-chain call yields
top1/5/10/20 together (measured 230ms).

**Solana only, not an oversight**: the ERC-20 standard carries no on-chain
holder list, so no node call can return the largest holders on EVM at all —
not here, and not from another provider without a paid per-network indexer.
Solana = 45.2% of our signals (30,931 of 68,491).

Read-only (FR-012), and the key is never printed nor logged (FR-013).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import config
import extract
from db import RecorderDB
from solana_rpc import ChainKeyMissing


async def run_chain_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """One cycle: the oldest due watches, one call each, one concentration row each.

    Three states, not two — and that is deliberate:
      - `ok`    — a measurement arrived and a row was written ⇒ next refresh
        after `CHAIN_REFRESH_SECONDS`.
      - `empty` — the source replied with no measurement (an address that is
        not a token, or has no accounts) ⇒ a normal cadence, not a fast one:
        retrying every two minutes an address that will never have a
        measurement burns the budget.
      - `error` — a call failed ⇒ fast retry (`CHAIN_ERROR_RETRY_SECONDS`),
        because the unknown is not safe.

    `ChainKeyMissing` **is raised outside the cycle** and is never stamped on
    the tokens: the fault is ours, not theirs, and marking the whole queue
    `error` over a missing key file would wreck correct scheduling.
    """
    stats = {
        "chain_due": 0, "chain_rows": 0, "chain_empty": 0,
        "chain_unsupported": 0, "chain_errors": 0,
    }
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.chain_fetch_due(
        limit=config.CHAIN_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        networks=config.CHAIN_NETWORKS,
    )
    stats["chain_due"] = len(due)

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        first_seen = w["first_seen_at"]
        sig = w.get("entry_signal_id")
        is_control = int(w.get("is_control") or 0)
        status = "error"
        top1: float | None = None

        try:
            if str(addr).lower() in config.CHAIN_UNSUPPORTED_TOKENS:
                # Some reference mints have millions of token accounts. The
                # Solana RPC rejects largest-account enumeration for them;
                # keep the token out of the retry loop instead of logging the
                # same permanent error every few minutes.
                status = "unsupported"
                stats["chain_unsupported"] += 1
            else:
                raw = await rpc.fetch_concentration_raw(addr)
                row = extract.extract_chain_concentration(
                    raw, addr, net, recorded_at, first_seen, sig, is_control
                )
                if row is None:
                    status = "empty"
                    stats["chain_empty"] += 1
                else:
                    db.insert_chain_concentration(row)
                    status = "ok"
                    top1 = row["top1_pct"]
                    stats["chain_rows"] += 1
        except ChainKeyMissing:
            raise  # a setup fault, not a token fault — never stamped on the token
        except Exception as exc:  # noqa: BLE001 — one token must not sink the cycle
            stats["chain_errors"] += 1
            # The message is scrubbed of the key inside `solana_rpc` before it
            # reaches here (FR-013), and this field is displayed by the
            # dashboard. And `note_error`, not `set_meta`: taking the DB lock
            # while **handling** one token's error would escape this guard and
            # sink the rest of the queue for the entire cycle.
            db.note_error(
                "last_error_chain",
                f"{recorded_at}: {addr}: {type(exc).__name__}: {exc}",
            )

        db.set_chain_state(addr, net, status, top1, recorded_at)
        # A pause **between** calls, not after the last one (same guard as the other cycles).
        if i + 1 < len(due):
            await sleep(config.CHAIN_PACING_SECONDS)
    return stats


async def run_chain_auth_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """The slow layer: mint authority, mutability, and developer holdings.

    An hourly cadence, not a five-minute one: mint authority is revoked once
    in a token's lifetime if it is ever revoked, so asking about it 12 times
    an hour wastes budget we need for moving concentration. And a separate
    queue (`chain_auth_state`), so one layer's refresh cannot hide the
    other's delay.

    Two calls, not one, and the second is **conditional**: the developer's
    address is known only from the first call's reply, so it cannot be folded
    into the same batch. If the second call alone fails, the row is written
    without `dev_holding_pct` — losing a column, not a measurement.
    """
    stats = {"auth_due": 0, "auth_rows": 0, "auth_empty": 0, "auth_errors": 0,
             "auth_dev": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_AUTH_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_AUTH_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.chain_auth_due(
        limit=config.CHAIN_AUTH_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        networks=config.CHAIN_NETWORKS,
    )
    stats["auth_due"] = len(due)

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        first_seen = w["first_seen_at"]
        sig = w.get("entry_signal_id")
        is_control = int(w.get("is_control") or 0)
        status = "error"

        try:
            raw = await rpc.fetch_authority_raw(addr)
            owner = extract.pick_dev_owner(raw)
            if owner:
                await sleep(config.CHAIN_PACING_SECONDS)
                try:
                    raw = dict(raw)
                    raw["dev_owner"] = owner
                    raw["owner_accounts"] = await rpc.fetch_owner_token_balance_raw(
                        owner, addr
                    )
                    stats["auth_dev"] += 1
                except ChainKeyMissing:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # One column falls, the row survives: `dev_owner` stays
                    # saved, so we know whom we were measuring when we read
                    # the error.
                    raw["owner_accounts"] = None
                    raw["dev_error"] = f"{type(exc).__name__}: {exc}"
            row = extract.extract_chain_authority(
                raw, addr, net, recorded_at, first_seen, sig, is_control
            )
            if row is None:
                status = "empty"
                stats["auth_empty"] += 1
            else:
                db.insert_chain_authority(row)
                status = "ok"
                stats["auth_rows"] += 1
        except ChainKeyMissing:
            raise
        except Exception as exc:  # noqa: BLE001
            stats["auth_errors"] += 1
            db.note_error(
                "last_error_chain_auth",
                f"{recorded_at}: {addr}: {type(exc).__name__}: {exc}",
            )

        db.set_chain_auth_state(addr, net, status, recorded_at)
        if i + 1 < len(due):
            await sleep(config.CHAIN_PACING_SECONDS)
    return stats
