"""Measure whether HyperSync can feed the replay log walk — read-only.

The question this answers before any routing change: **does the paid
provider (Envio HyperSync) return exactly the same transfer logs as the
public node for the same token and block range?** Replay's history walk is
the 2-4 week stage of the EVM repair (public node, ~5-10 tokens/day
measured 2026-09-01..06, Base holding 102 of the 124 pending); routing it
through HyperSync is only legal if the two sources provably agree — the
GoldRush trap (#29) was a provider that silently truncated pages while
looking healthy.

Per token, on the same (from, to) window a replay segment would walk:
  1. `EVMRPC.get_logs_paged` (public) vs `EnvioHyperSync.get_logs_paged`
     (paid): compare completion, counts, and the full multiset of
     (block, logIndex, topics, data, address) — a dropped or invented log
     shows up as a diff, not as a count that happens to match.
  2. Block timestamps: can HyperSync's query API return them at all (a log
     field_selection entry, and a dedicated block query), and do the values
     agree with the public node's `eth_getBlockByNumber`? Replay needs
     block times for its snapshot grid; today they come from the public
     node's anchor calls.

Read-only: no database connection, no writes, no task interaction. Keys
stay inside `EnvioHyperSync`'s own disk-per-call pool — never printed,
logged, or echoed (FR-013). Public-node volume is deliberately small
(≈ one replay cycle's worth of requests).

Usage:
    python probe_hypersync_replay.py
    python probe_hypersync_replay.py --output report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import envio_hypersync  # noqa: E402
import evm_rpc  # noqa: E402

# Real queue items (`evm_replay_state`, read 2026-09-06): the heaviest Base
# replay partials plus one Robinhood partial — the shapes the 2-4 week drain
# is actually made of. `from_block` is each token's live replay checkpoint.
TOKENS: tuple[tuple[str, str, int], ...] = (
    ("8453", "0xcb585250f852c6c6bf90434ab21a00f02833a4af", 35_818_252),
    ("8453", "0x3e12b9d6a4d12cd9b4a6d613872d0eb32f68b380", 35_964_608),
    ("8453", "0xa99f6e6785da0f5d6fb42495fe424bce029eeb3e", 31_667_379),
    ("4663", "0x57c0e45cb534413d1c20a4240955d6bb250bb4f1", 53_251_272),
)
# Window sizes: enough transfers for a strong identity check, small enough
# that the public walk stays in a handful of requests. Robinhood blocks are
# 0.1s each and its public node throttles, so its window is a fifth of Base's.
SPAN = {"8453": 50_000, "4663": 10_000}
# Headroom for the public walk's adaptive splits on dense ranges.
PUB_MAX_CALLS = 60


def _identity(log: dict[str, Any]) -> tuple[Any, ...]:
    """Every field the ledger depends on: two sources agreeing here is the
    whole point of the probe. `logIndex` makes the multiset a true set even
    when one block carries many transfers."""
    def _num(value: Any) -> int:
        try:
            return int(str(value), 16)
        except (TypeError, ValueError):
            return 0

    return (
        _num(log.get("blockNumber")),
        _num(log.get("logIndex")),
        tuple(log.get("topics") or ()),
        str(log.get("data") or ""),
        str(log.get("address") or "").lower(),
    )


def _diff_summary(
    pub: Counter, paid: Counter,
) -> dict[str, Any]:
    missing = pub - paid      # logs the paid provider dropped
    invented = paid - pub     # logs the paid provider fabricated/re-dated
    sample: list[str] = []
    for (block, log_index, *_rest) in list(missing) + list(invented):
        sample.append(f"block={block} logIndex={log_index}")
        if len(sample) >= 5:
            break
    return {
        "dropped_by_paid": sum(missing.values()),
        "invented_by_paid": sum(invented.values()),
        "first_divergences": sample,
    }


async def compare_token(
    rpc: evm_rpc.EVMRPC, hyper: envio_hypersync.EnvioHyperSync,
    network: str, token: str, from_block: int,
) -> dict[str, Any]:
    span = SPAN.get(network, 20_000)
    to_block = from_block + span - 1
    row: dict[str, Any] = {
        "network": network, "token": token,
        "from_block": from_block, "to_block": to_block,
    }

    started = time.monotonic()
    pub_logs, pub_calls, pub_complete, pub_resume = await rpc.get_logs_paged(
        network, [token], from_block, to_block, max_calls=PUB_MAX_CALLS,
    )
    row["public"] = {
        "logs": len(pub_logs), "requests": pub_calls,
        "complete": pub_complete, "resume": pub_resume,
        "seconds": round(time.monotonic() - started, 1),
    }

    started = time.monotonic()
    try:
        paid_logs, paid_calls, paid_complete, _paid_resume = (
            await hyper.get_logs_paged(
                network, [token], from_block, to_block, max_calls=100,
            )
        )
    except envio_hypersync.EnvioUnavailable as exc:
        row["paid"] = {"verdict": "unavailable", "reason": str(exc)[:120]}
        return row
    row["paid"] = {
        "logs": len(paid_logs), "requests": paid_calls,
        "complete": paid_complete,
        "seconds": round(time.monotonic() - started, 1),
    }

    # If the public walk fell short, compare only the region it covered —
    # a prefix mismatch is still a finding; a prefix match on an honest
    # resume point is still evidence.
    compared_to = to_block if pub_complete else pub_resume - 1
    pub_set = Counter(
        _identity(log) for log in pub_logs
        if int(str(log["blockNumber"]), 16) <= compared_to
    )
    paid_set = Counter(
        _identity(log) for log in paid_logs
        if int(str(log["blockNumber"]), 16) <= compared_to
    )
    row["comparison"] = {
        "compared_through_block": compared_to,
        "logs_compared": sum(pub_set.values()),
        **_diff_summary(pub_set, paid_set),
    }
    row["verdict"] = (
        "match" if not (pub_set - paid_set) and not (paid_set - pub_set)
        else "mismatch"
    )
    if not pub_complete:
        row["verdict"] += "_public_incomplete"
    # Block order, the never-broken contract both callers rely on.
    pub_blocks = [int(str(log["blockNumber"]), 16) for log in pub_logs]
    paid_blocks = [int(str(log["blockNumber"]), 16) for log in paid_logs]
    row["comparison"]["public_ascending"] = pub_blocks == sorted(pub_blocks)
    row["comparison"]["paid_ascending"] = paid_blocks == sorted(paid_blocks)
    return row


async def probe_timestamps(
    rpc: evm_rpc.EVMRPC, hyper: envio_hypersync.EnvioHyperSync,
    network: str, token: str, from_block: int,
) -> list[dict[str, Any]]:
    """The two shapes a block time could take from HyperSync, checked against
    the public node's `eth_getBlockByNumber` on the same blocks."""
    rows: list[dict[str, Any]] = []
    sample_blocks = sorted({
        from_block, from_block + SPAN.get(network, 20_000) // 2,
    })
    public_times: dict[int, int | None] = {}
    for block in sample_blocks:
        try:
            public_times[block] = await rpc.block_timestamp(network, block)
        except evm_rpc.EVMRPCError as exc:
            public_times[block] = None
            rows.append({"experiment": "public_block_timestamp",
                         "block": block, "error": str(exc)[:100]})

    # A: a `block_timestamp` field on the log selection itself.
    try:
        body = {
            "from_block": from_block,
            "to_block": from_block + SPAN.get(network, 20_000) - 1,
            "logs": [{"address": [token.lower()],
                      "topics": [[evm_rpc.TRANSFER_TOPIC]]}],
            "field_selection": {"log": ["block_number", "block_timestamp"]},
        }
        page = await hyper._query(network, body)  # probe-only use of the key plumbing
        data = page.get("data")
        entries = [
            row for group in (data if isinstance(data, list) else [])
            if isinstance(group, dict) for row in (group.get("logs") or [])
        ]
        with_ts = [e for e in entries if isinstance(e, dict)
                   and e.get("block_timestamp") is not None]
        rows.append({
            "experiment": "log_field_block_timestamp",
            "verdict": "ok" if with_ts else "no_timestamp_in_response",
            "sample": {
                str(e.get("block_number")): e.get("block_timestamp")
                for e in with_ts[:2]
            },
        })
    except Exception as exc:  # noqa: BLE001 — a rejected experiment is a result, not a crash
        rows.append({"experiment": "log_field_block_timestamp",
                     "verdict": "rejected", "reason": str(exc)[:120]})

    # B: a dedicated block query, one block at a time.
    try:
        checked = 0
        agreed = 0
        for block in sample_blocks:
            body = {
                "from_block": block, "to_block": block,
                "field_selection": {"block": ["number", "timestamp"]},
            }
            page = await hyper._query(network, body)  # same probe-only plumbing
            blocks = page.get("data")
            ts = None
            if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
                ts = blocks[0].get("timestamp")
            rows.append({
                "experiment": "block_query_timestamp",
                "block": block,
                "paid_timestamp": ts,
                "public_timestamp": public_times.get(block),
            })
            checked += 1
            if ts is not None and public_times.get(block) is not None:
                agreed += int(int(ts) == int(public_times[block]))  # type: ignore[arg-type]
        rows.append({"experiment": "block_query_timestamp_summary",
                     "checked": checked, "agreed": agreed})
    except Exception as exc:  # noqa: BLE001 — same: a rejected experiment is a result
        rows.append({"experiment": "block_query_timestamp",
                     "verdict": "rejected", "reason": str(exc)[:120]})
    return rows


async def main_async(output: str | None) -> int:
    rpc = evm_rpc.EVMRPC()
    hyper = envio_hypersync.EnvioHyperSync()
    rows: list[dict[str, Any]] = []
    timestamp_rows: list[dict[str, Any]] = []
    try:
        for network, token, from_block in TOKENS:
            rows.append(await compare_token(rpc, hyper, network, token, from_block))
        # Timestamp experiments on the first Base token only — the answer is
        # a property of the service, not of the token.
        timestamp_rows = await probe_timestamps(
            rpc, hyper, TOKENS[0][0], TOKENS[0][1], TOKENS[0][2],
        )
    finally:
        await rpc.aclose()
        await hyper.aclose()
    report = {
        "probe": "hypersync-replay-v1",
        "tokens": rows,
        "timestamp_experiments": timestamp_rows,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=1)
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
    print(rendered)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=None, help="write JSON to a file")
    args = parser.parse_args()
    return asyncio.run(main_async(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
