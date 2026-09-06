"""Auditing the EVM ledger against the chain's truth — not against itself.

The balances ledger we build from `Transfer` logs is **cumulative**: every
snapshot depends on everything before it, so one lost log quietly corrupts
all the snapshots that follow and leaves no trace in any row or log. And
that is the trap a deleted provider caught us in (a successful response,
truncated at the first page with no error marker at all). Only a question
to the chain itself exposes it: **what is this holder's balance at that
block?**

That question needs **archive state**, and our public nodes do not have it:
the Robinhood node is ~128 blocks deep (`metadata is not found`), and the
public BSC node requires a token (`Archive requests require a personal
token`). That, and that alone, is why provider keys are used here: not to
extend the ledger but to audit it. Measured 2026-08-19: the Alchemy archive
serves all four networks, and its `eth_getLogs` cap is ten blocks — so it is
unfit for building the ledger, and perfectly fit for `eth_call` at a past
block.

The live EVM layer stays keyless (FR-012): this is a separate manual tool,
it does not write a single character to the database, and it does not import
`evm_rpc` so no key can sneak into a path designed to carry none.

Three questions per snapshot, each exposing a different defect:

1. **The block** — the highest block whose timestamp is ≤ `recorded_at`.
   Found with the chain's timestamps, not with an anchor of ours (anchors
   are a free initial bracket, then the search runs inside it with fresh
   timestamps), so its correctness is **proven**, not assumed: we see with
   our own eyes that the one after it is newer than the time.

2. **Completeness** — and this is the one the trap is caught with.
   `supply` in the ledger is the sum of live balances, not `totalSupply()`
   from the contract (a deliberate definition: percentages over what can be
   sold). If the ledger has seen **every** transfer, the identity holds:
       `supply_base + burn-address balance == totalSupply()`
   A truncated ledger shrinks the left side and never touches the right —
   so the gap is exactly what was lost.

3. **The balances** — `balanceOf(h)` for the top K holders, in base units
   with no division by `decimals` (no rounding error mixed into a ledger
   error), then `top1/5/10/20` are recomputed and compared with the stored
   values under the same denominator they were computed with.

And what is explicitly **not** audited: `holder_count`. The call tells us
the balance of an address we ask about, and has no way to reach an address
our ledger never knew in the first place. But the completeness question
closes that gap from the money side: an unknown holder carrying a balance
shows up as a gap in the identity, and an unknown holder with a zero
balance changes no percentage and no concentration.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import db as db_module  # noqa: E402
import evm_contract  # noqa: E402
from provider_keys import read_keys  # noqa: E402

# The archive route per network, ordered by preference. **Measured
# 2026-08-19, not assumed**:
#   Alchemy: a working archive on all four networks (`eth_call` at head−3m ✓).
#   dRPC   : Base archive ✓, and its BSC archive returned an internal error
#            twice ⇒ do not rely on it there, and its Robinhood slice
#            answers `eth_chainId` then refuses everything after.
# So no provider is listed that "should" serve a network: the fallback sits
# only where it was measured to work.
ARCHIVE_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "4663": (("alchemy", "https://robinhood-mainnet.g.alchemy.com/v2/{key}"),),
    "8453": (
        ("alchemy", "https://base-mainnet.g.alchemy.com/v2/{key}"),
        ("drpc", "https://lb.drpc.org/ogrpc?network=base&dkey={key}"),
    ),
    "143": (("alchemy", "https://monad-mainnet.g.alchemy.com/v2/{key}"),),
    "56": (("alchemy", "https://bnb-mainnet.g.alchemy.com/v2/{key}"),),
}
# The three field names per provider exactly as in `key_file.PROVIDERS` —
# the guard test matches them by text, so any divergence here breaks there,
# not in production.
PROVIDER_FIELDS = {
    "alchemy": ("alchemy_api_keys", "alchemy_api_key", "ALCHEMY_API_KEY"),
    "drpc": ("drpc_api_keys", "drpc_api_key", "DRPC_API_KEY"),
}
TOTAL_SUPPLY = evm_contract.selector("totalSupply()")
BALANCE_OF = evm_contract.selector("balanceOf(address)")
BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)
# A one-block shift at the boundary moves a balance, so not every difference
# is a ledger defect. And the threshold is in percentage points of supply
# because that is the unit of what we actually store (`top1_pct`): below a
# tenth of a point it is called drift, above it a mismatch worth a look.
DRIFT_POINTS = 0.1


class ArchiveError(RuntimeError):
    """A failed archive call — with a message scrubbed of the key before
    it is raised."""


class ArchiveRPC:
    """A small client for a keyed archive node — read-only, and it never
    prints a key.

    `evm_rpc.EVMRPC` is deliberately not reused: it is keyless by design,
    and passing it a URL carrying a key would let the secret flow through
    its logs, its timeouts, and its throttle messages — all places that
    were not written with a secret in mind. The separation here is a line
    of defense, not duplication, and it costs thirty lines.
    """

    def __init__(self, url: str, secret: str, timeout: float = 30.0) -> None:
        self._url = url
        self._secret = secret
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ArchiveRPC:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def hide(self, text: object) -> str:
        """Every piece of text leaving here passes through it.

        An error message usually carries the URL, and the URL carries the
        key: so redaction happens at **construction**, not at printing, so
        there is no second path it could slip through.
        """
        clean = str(text).replace(self._secret, "<key>")
        return " ".join(clean.split())[:160]

    def _call(self, method: str, params: list) -> object:
        self.calls += 1
        try:
            response = self._client.post(
                self._url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ArchiveError(self.hide(f"{method}: {type(exc).__name__}")) from None
        if not isinstance(body, dict):
            raise ArchiveError(self.hide(f"{method}: response is not an object"))
        if body.get("error"):
            raise ArchiveError(self.hide(f"{method}: {body['error']}"))
        return body.get("result")

    def block_number(self) -> int:
        return int(str(self._call("eth_blockNumber", [])), 16)

    def block_timestamp(self, block: int) -> int:
        result = self._call("eth_getBlockByNumber", [hex(int(block)), False])
        if not isinstance(result, dict) or "timestamp" not in result:
            raise ArchiveError(f"eth_getBlockByNumber: no block at {block}")
        return int(str(result["timestamp"]), 16)

    def read_uint(self, to: str, data: str, block: int) -> int:
        """`eth_call` returns a number. A revert here is an **error**, not an
        answer.

        In the live layer a revert of `owner()` is an answer ("no owner"),
        but here we are asking an ERC-20 contract we know to be one: a
        revert means the contract did not exist at that block, or that the
        node has no archive for that depth. Both are audit results to be
        reported, not swallowed into a zero that would read as a "match".
        """
        result = self._call("eth_call", [{"to": to, "data": data}, hex(int(block))])
        text = str(result or "")
        if not text.startswith("0x") or len(text) < 3:
            raise ArchiveError(f"eth_call: non-numeric response at block {block}")
        return int(text, 16)

    def balance_of(self, token: str, holder: str, block: int) -> int:
        padded = holder.lower().removeprefix("0x").rjust(64, "0")
        return self.read_uint(token, BALANCE_OF + padded, block)

    def total_supply(self, token: str, block: int) -> int:
        return self.read_uint(token, TOTAL_SUPPLY, block)


def open_route(
    network_id: str, provider: str | None = None, timeout: float = 30.0,
) -> tuple[str, ArchiveRPC]:
    """(provider name, an open client) for the first route with a key on
    disk.

    The order in `ARCHIVE_ROUTES` is precedence, not preference: the first
    is the most reliable as measured on that network, the second a fallback
    if the first's key is missing or its service is down. Keys are read
    through `read_keys` alone (`enabled` filtering + environment
    precedence), so a key the operator disabled from the dashboard is not
    called here either.
    """
    routes = ARCHIVE_ROUTES.get(str(network_id), ())
    if not routes:
        raise ArchiveError(f"no known archive route for network {network_id}")
    wanted = [(name, url) for name, url in routes if not provider or name == provider]
    if not wanted:
        raise ArchiveError(f"provider {provider} is not a route for network {network_id}")
    for name, pattern in wanted:
        plural, singular, env = PROVIDER_FIELDS[name]
        keys = read_keys(plural, singular, env)
        if keys:
            return name, ArchiveRPC(pattern.format(key=keys[0]), keys[0], timeout)
    names = ", ".join(name for name, _ in wanted)
    raise ArchiveError(
        f"no enabled key for network {network_id} — one of these is required: {names}"
    )


def bracket_from_anchors(
    db: db_module.RecorderDB, network_id: str, target_ts: int, head: int,
) -> tuple[int, int]:
    """A first bracket [low, high] from our stored anchors — free, and it
    shortens the search.

    An anchor is a real block timestamp read from the chain and stored
    (`evm_block_time`), so it is stored truth, not an estimate. But they are
    sparse (every 18,000 blocks) so they cannot answer alone: they give the
    bracket, and the search runs inside it with fresh timestamps. If no
    suitable anchor exists, the bracket is the whole chain — slower by a
    handful of calls, and just as correct. And a bracket is never narrowed
    by an anchor without verifying it later: the search reads both of its
    ends from the chain before trusting them.
    """
    low, high = 0, int(head)
    for block, stamp in db.block_anchors(str(network_id)):
        if int(stamp) <= target_ts and int(block) > low:
            low = int(block)
        elif int(stamp) > target_ts and int(block) < high:
            high = int(block)
    return low, max(high, low + 1)


def boundary_block(rpc: ArchiveRPC, target_ts: int, low: int, high: int) -> int:
    """The highest block whose timestamp is ≤ the wanted time, inside the
    given bracket.

    Extrapolation then bisection: block time is nearly constant, so the
    extrapolation lands close within two or three calls, but it can get
    stuck (probing the same block twice), so bisection guarantees
    termination. And the answer is exact, not approximate: we exit when the
    two ends touch, meaning we have read ourselves that the block after it
    is newer than wanted.
    """
    # The low end is read even when it is block zero: genesis is a real
    # block with a real timestamp, and assuming zero in its place corrupts
    # the extrapolation by the age of the whole epoch — dropping the search
    # to pure bisection (twenty-five calls instead of three).
    low_ts = rpc.block_timestamp(low)
    if low_ts > target_ts:  # a false anchor ⇒ drop to genesis and ask again
        low = 0
        low_ts = rpc.block_timestamp(low)
        if low_ts > target_ts:
            return 0  # a time before the chain's genesis: no block to ask for
    high_ts = rpc.block_timestamp(high)
    if high_ts <= target_ts:
        return high
    tries, seen = 0, {low, high}
    while high - low > 1:
        probe = 0
        if tries < 4 and high_ts > low_ts:
            span = (target_ts - low_ts) * (high - low) / (high_ts - low_ts)
            probe = min(max(low + max(1, int(span)), low + 1), high - 1)
        if not probe or probe in seen:
            probe = (low + high) // 2
        seen.add(probe)
        tries += 1
        stamp = rpc.block_timestamp(probe)
        if stamp <= target_ts:
            low, low_ts = probe, stamp
        else:
            high, high_ts = probe, stamp
    return low


def _percentages(balances: list[int], supply: int) -> dict[str, float | None]:
    """Percentages of the top 1/5/10/20 — with the ledger's own definition
    and its own denominator.

    The denominator is `supply_base`, not `totalSupply()`: we are comparing
    a stored number with a recomputed one, so the denominators must not
    diverge, otherwise the difference becomes a difference of definition,
    not of data.
    """
    if supply <= 0:
        return {f"top{n}_pct": None for n in (1, 5, 10, 20)}
    ordered = sorted(balances, reverse=True)
    return {
        f"top{n}_pct": sum(ordered[:n]) / supply * 100 for n in (1, 5, 10, 20)
    }


def sample_rows(
    db: db_module.RecorderDB, network_id: str, tokens: int, per_token: int,
    only: tuple[str, ...] = (),
) -> list[dict]:
    """Snapshots spread across each token's lifetime, with the latest always
    among them.

    The latest is not just one of several: an error in a cumulative ledger
    accumulates, so the maximum possible deviation sits in the last
    snapshot. The rest are spread evenly, not randomly, so two runs on the
    same database are comparable — an audit tool that gives a different
    answer every run is not one to base a decision on.
    """
    net = str(network_id)
    if only:
        addresses = [token.lower() for token in only]
    else:
        addresses = [
            row["token_address"] for row in db._conn.execute(
                """SELECT token_address, COUNT(*) AS n FROM chain_concentration
                    WHERE network_id = ? AND is_replay = 1
                    GROUP BY token_address ORDER BY n DESC, token_address LIMIT ?""",
                (net, max(1, int(tokens))),
            ).fetchall()
        ]
    picked: list[dict] = []
    for address in addresses:
        rows = db._conn.execute(
            """SELECT token_address, network_id, recorded_at, supply, decimals,
                      top1_pct, top5_pct, top10_pct, top20_pct, holder_count,
                      top_accounts, raw_json
                 FROM chain_concentration
                WHERE token_address = ? AND network_id = ? AND is_replay = 1
                ORDER BY recorded_at""",
            (address, net),
        ).fetchall()
        if not rows:
            continue
        count = max(1, min(int(per_token), len(rows)))
        step = (len(rows) - 1) / (count - 1) if count > 1 else 0
        wanted = sorted({round(i * step) for i in range(count)} | {len(rows) - 1})
        picked.extend(dict(rows[i]) for i in wanted)
    return picked


def _epoch(stamp: object) -> int:
    """`recorded_at` as ISO text → seconds since the epoch, UTC if unstated."""
    text = str(stamp).replace("Z", "+00:00")
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def audit_row(
    rpc: ArchiveRPC, db: db_module.RecorderDB, row: dict, holders: int,
    head: int | None = None,
) -> dict:
    """Audit one snapshot → a self-describing record. No printing, no
    database writes."""
    payload = db_module.decode_raw(row["raw_json"])
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    payload = payload or {}
    top = [(str(a).lower(), int(b)) for a, b in (payload.get("top") or [])]
    stored_supply = int(payload.get("supply_base") or 0)
    token, net = str(row["token_address"]), str(row["network_id"])
    out: dict = {
        "token": token, "network_id": net, "recorded_at": row["recorded_at"],
        "verdict": "unverifiable", "note": "", "block": None,
        "supply_stored": stored_supply, "supply_chain": None, "burned": None,
        "coverage_pct": None, "holder_count_stored": row["holder_count"],
        "holders_checked": 0, "holders_matched": 0, "worst_holder": None,
        "stored_pct": {f"top{n}_pct": row[f"top{n}_pct"] for n in (1, 5, 10, 20)},
        "chain_pct": {}, "delta_points": None, "calls": 0,
    }
    started = rpc.calls
    try:
        target = _epoch(row["recorded_at"])
        low, high = bracket_from_anchors(db, net, target, head or rpc.block_number())
        block = boundary_block(rpc, target, low, high)
        out["block"] = block
        if not block:
            out["note"] = "no block before this time on this network"
            return out

        # 1) Completeness: `supply_base + burned` must equal `totalSupply()`
        #    exactly.
        supply_chain = rpc.total_supply(token, block)
        burned = sum(rpc.balance_of(token, address, block) for address in BURN_ADDRESSES)
        out["supply_chain"], out["burned"] = supply_chain, burned
        sellable = supply_chain - burned
        out["coverage_pct"] = stored_supply / sellable * 100 if sellable > 0 else None

        # 2) Balances: the top K holders in the snapshot, an exact match in
        #    base units.
        checked = top[: max(1, int(holders))]
        measured = [
            (address, stored, rpc.balance_of(token, address, block))
            for address, stored in checked
        ]
        out["holders_checked"] = len(measured)
        out["holders_matched"] = sum(1 for _, s, c in measured if s == c)
        off = [item for item in measured if item[1] != item[2]]
        if off and stored_supply > 0:
            worst = max(off, key=lambda item: abs(item[1] - item[2]))
            out["worst_holder"] = {
                "address": worst[0], "stored": str(worst[1]), "chain": str(worst[2]),
                "points": abs(worst[1] - worst[2]) / stored_supply * 100,
            }

        # 3) The derived percentages — compared only where enough holders
        #    were checked: checking five says nothing about `top20_pct`,
        #    and including it here would be a manufactured difference.
        out["chain_pct"] = _percentages([c for _, _, c in measured], stored_supply)
        deltas = [
            abs(out["stored_pct"][f"top{n}_pct"] - out["chain_pct"][f"top{n}_pct"])
            for n in (1, 5, 10, 20)
            if n <= len(measured)
            and out["stored_pct"][f"top{n}_pct"] is not None
            and out["chain_pct"][f"top{n}_pct"] is not None
        ]
        out["delta_points"] = max(deltas, default=None)

        # The verdict: incompleteness first. An incomplete ledger is wrong in
        # the denominator, so every percentage built on it is wrong even if
        # every checked holder matches — and the converse does not hold.
        gap = sellable - stored_supply
        if sellable <= 0:
            out["verdict"] = "unverifiable"
            out["note"] = "sellable supply is zero or negative ⇒ no denominator for percentages"
        elif abs(gap) * 10_000 > sellable:  # more than one basis point
            out["verdict"] = "incomplete" if gap > 0 else "excess"
            cause = "transfer logs not read" if gap > 0 else "balances counted twice"
            out["note"] = (
                f"the ledger holds {out['coverage_pct']:.4f}% of sellable supply"
                f" — a gap of {abs(gap) / sellable * 100:.4f}% ⇒ {cause}"
            )
        elif out["holders_matched"] == out["holders_checked"]:
            out["verdict"] = "match"
        elif (out["delta_points"] or 0) <= DRIFT_POINTS:
            out["verdict"] = "drift"
            out["note"] = "a difference under a tenth of a point ⇒ a boundary block, not a ledger defect"
        else:
            out["verdict"] = "mismatch"
    except ArchiveError as exc:
        out["note"] = str(exc)
    finally:
        out["calls"] = rpc.calls - started
    return out


_MARK = {"match": "✓", "drift": "≈", "incomplete": "✗", "excess": "✗",
         "mismatch": "✗", "unverifiable": "?"}
_FAIL = ("incomplete", "excess", "mismatch")


def _line(record: dict) -> str:
    block = f"{record['block']:,}" if record["block"] else "—"
    cover = (
        f"{record['coverage_pct']:.4f}%" if record["coverage_pct"] is not None else "—"
    )
    delta = (
        f"{record['delta_points']:.4f}pp" if record["delta_points"] is not None else "—"
    )
    text = (
        f"   {_MARK.get(record['verdict'], '?')} {record['token'][:14]}… · "
        f"{str(record['recorded_at'])[:16]} · block {block} · "
        f"coverage {cover} · holders {record['holders_matched']}/"
        f"{record['holders_checked']} · delta {delta} · {record['calls']} calls"
    )
    if record["note"]:
        text += f"\n       {record['note']}"
    worst = record["worst_holder"]
    if worst:
        text += (
            f"\n       worst holder {worst['address'][:12]}… "
            f"stored {worst['stored']} · chain {worst['chain']} "
            f"({worst['points']:.4f}pp)"
        )
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit EVM ledger snapshots against a keyed archive — read-only.",
    )
    parser.add_argument("--networks", nargs="+", default=list(config.EVM_NETWORKS))
    parser.add_argument("--tokens", type=int, default=3, help="tokens per network")
    parser.add_argument("--token", action="append", default=[], help="a specific token")
    parser.add_argument("--rows", type=int, default=3, help="snapshots per token")
    parser.add_argument("--holders", type=int, default=5, help="how many top holders to ask")
    parser.add_argument("--provider", choices=sorted(PROVIDER_FIELDS), default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", dest="json_path", default="", help="path for a JSON report")
    args = parser.parse_args(argv)

    db = db_module.RecorderDB(config.DB_PATH, os.path.join(HERE, "schema.sql"))
    records: list[dict] = []
    try:
        for network in [str(n) for n in args.networks]:
            rows = sample_rows(
                db, network, args.tokens, args.rows, tuple(args.token),
            )
            if not rows:
                print(f"── network {network}: no replay rows to audit")
                continue
            try:
                provider, rpc = open_route(network, args.provider, args.timeout)
            except ArchiveError as exc:
                print(f"── network {network}: {exc}")
                continue
            tokens = len({row["token_address"] for row in rows})
            print(
                f"── network {network} · archive {provider} · {tokens} tokens · "
                f"{len(rows)} snapshots"
            )
            started = time.monotonic()
            with rpc:
                # The chain head is read once for the whole network: it is
                # only the bracket's ceiling, and all our snapshots are days
                # in the past — reading it per row is a call paid for nothing.
                try:
                    head = rpc.block_number()
                except ArchiveError as exc:
                    print(f"   ? could not read the head: {exc}")
                    continue
                for row in rows:
                    record = audit_row(rpc, db, row, args.holders, head)
                    record["provider"] = provider
                    records.append(record)
                    print(_line(record))
            print(f"   {rpc.calls} calls in {time.monotonic() - started:.1f}s\n")
    finally:
        db.close()

    if not records:
        print("nothing audited.")
        return 0
    tally: dict[str, int] = {}
    for record in records:
        tally[record["verdict"]] = tally.get(record["verdict"], 0) + 1
    print("tally: " + " · ".join(
        f"{_MARK.get(name, '?')} {name} {count}" for name, count in tally.items()
    ))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        print(f"written to {args.json_path}")
    # One bad verdict is enough for a non-zero exit code: this is a checking
    # tool, not a report, and its silent success in the face of an
    # incomplete ledger is exactly what it was built to prevent.
    return 1 if any(tally.get(name) for name in _FAIL) else 0


if __name__ == "__main__":
    raise SystemExit(main())
