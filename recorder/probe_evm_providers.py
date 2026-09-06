"""Measure EVM provider limits read-only: chainId, head, eth_getLogs range cap, latency.

Answers one question before any routing change: **which provider actually lifts
which network's `eth_getLogs` limit?** The queue on 8453/4663 exists because of
measured public-node caps (10k-block Base range, response size, Robinhood
429s/timeout) — a new provider only helps if its own measured cap is larger.

Secrets: keys are read from `chain_keys.json` via `provider_keys.read_keys`
and never printed, logged, or echoed. URLs are built with the key inlined and
every outgoing message passes through `hide()` which strips the key first —
the same construction-time redaction contract as `audit_evm_ledger.ArchiveRPC`.

Read-only: no database connection, no writes, no task interaction.

Usage:
    python probe_evm_providers.py                # all networks, all providers
    python probe_evm_providers.py --network 8453 # one network
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

HERE = __file__.rsplit("\\", 1)[0] if "\\" in __file__ else __file__.rsplit("/", 1)[0]
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from provider_keys import read_keys  # noqa: E402

# Per-network provider routes: (provider, url template). Same measured-not-assumed
# rule as `audit_evm_ledger.ARCHIVE_ROUTES`; providers not listed for a network
# are simply not probed there — a route is earned by a measurement, not by a hope.
ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "4663": (
        ("public", config.EVM_RPC_URLS["4663"]),
        ("alchemy", "https://robinhood-mainnet.g.alchemy.com/v2/{key}"),
    ),
    "8453": (
        ("public", config.EVM_RPC_URLS["8453"]),
        ("alchemy", "https://base-mainnet.g.alchemy.com/v2/{key}"),
        ("drpc", "https://lb.drpc.org/ogrpc?network=base&dkey={key}"),
    ),
    "143": (
        ("public", config.EVM_RPC_URLS["143"]),
        ("alchemy", "https://monad-mainnet.g.alchemy.com/v2/{key}"),
    ),
    "56": (
        ("public", config.EVM_RPC_URLS["56"]),
        ("alchemy", "https://bnb-mainnet.g.alchemy.com/v2/{key}"),
    ),
}
PROVIDER_FIELDS = {
    "alchemy": ("alchemy_api_keys", "alchemy_api_key", "ALCHEMY_API_KEY"),
    "drpc": ("drpc_api_keys", "drpc_api_key", "DRPC_API_KEY"),
}
# The known transfer-heavy token to probe logs against — read from the live
# backfill state if possible (a real queue item), else per-network fallbacks.
FALLBACK_TOKENS = {
    "4663": "0x9ca1cc0c90d97b4f36c5e2232d4fbd705a73c65d",
    "8453": "0xb200000000000000000000503d889fdcbe48b801",
    "143": "0xb0a788d09e7da1582012721bea1d16d2aafb7777",
    "56": "0x0000000000000000000000000000000000000000",
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TIMEOUT = 20.0


class ProviderProbe:
    """One provider endpoint, with construction-time key redaction."""

    def __init__(self, name: str, url: str, secret: str = "") -> None:
        self.name = name
        self._url = url
        self._secret = secret
        self._client = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ProviderProbe:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def hide(self, text: object) -> str:
        clean = str(text).replace(self._secret, "<key>") if self._secret else str(text)
        return " ".join(clean.split())[:160]

    def call(self, method: str, params: list[Any]) -> tuple[bool, Any, float]:
        self.calls += 1
        started = time.monotonic()
        try:
            response = self._client.post(
                self._url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            elapsed = time.monotonic() - started
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}", time.monotonic() - started
        try:
            body = response.json()
        except ValueError:
            return False, f"HTTP {response.status_code} non-JSON", elapsed
        if body.get("error"):
            return False, self.hide(body["error"]), elapsed
        if "result" not in body:
            return False, "no result", elapsed
        return True, body["result"], elapsed

    def head(self) -> tuple[bool, int, float]:
        ok, result, elapsed = self.call("eth_blockNumber", [])
        if not ok:
            return False, 0, elapsed
        try:
            return True, int(str(result), 16), elapsed
        except (TypeError, ValueError):
            return False, 0, elapsed

    def logs(
        self, token: str, from_block: int, to_block: int,
    ) -> tuple[str, int, float]:
        """One range probe. Returns (verdict, count, seconds).

        verdict: ok · empty · range_limited · error:<msg>
        """
        params = [{
            "address": token,
            "topics": [TRANSFER_TOPIC],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }]
        ok, result, elapsed = self.call("eth_getLogs", params)
        if not ok:
            text = str(result)
            low = text.lower()
            if "limit" in low or "range" in low or "10,000" in low or "-32614" in low:
                return "range_limited", 0, elapsed
            return f"error:{text[:80]}", 0, elapsed
        if not isinstance(result, list):
            return "error:not-a-list", 0, elapsed
        return "ok", len(result), elapsed


def _keys() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for provider, (plural, singular, env) in PROVIDER_FIELDS.items():
        out[provider] = read_keys(plural, singular, env)
    return out


def probe_network(network: str) -> list[dict[str, Any]]:
    keys = _keys()
    results: list[dict[str, Any]] = []
    # The head from the public node anchors the ranges: probing the same block
    # window on every provider makes the counts comparable.
    pub_url = config.EVM_RPC_URLS.get(network)
    anchor_head = 0
    if pub_url:
        with ProviderProbe("public", pub_url) as probe:
            ok, head, elapsed = probe.head()
            results.append({
                "network": network, "provider": "public", "url": "public",
                "chain_check": "ok" if ok else "unreachable",
                "head": head, "head_seconds": round(elapsed, 3),
            })
            anchor_head = head if ok else 0
    token = FALLBACK_TOKENS.get(network, "")
    for provider, template in ROUTES.get(network, ()):
        if provider == "public":
            continue
        pool = keys.get(provider) or []
        if not pool:
            results.append({
                "network": network, "provider": provider,
                "note": "no key in chain_keys.json",
            })
            continue
        url = template.replace("{key}", pool[0])
        with ProviderProbe(provider, url, secret=pool[0]) as probe:
            ok, head, elapsed = probe.head()
            row = {
                "network": network, "provider": provider,
                "chain_check": "ok" if ok else "unreachable",
                "head": head, "head_seconds": round(elapsed, 3),
            }
            if ok and anchor_head and head:
                # A wildly different head means the route answers a different
                # chain — reported, not trusted.
                row["head_gap_vs_public"] = head - anchor_head
            results.append(row)
            if not ok or not head:
                continue
            # Range probe, doubling from the known public cap upward until the
            # provider refuses: the largest accepted range is the measurement.
            base = int(config.EVM_LOG_RANGE_HINT.get(network, 0) or 10_000)
            accepted = 0
            for span in (base, base * 2, base * 4, 500_000):
                lo = max(0, head - span)
                verdict, count, secs = probe.logs(token, lo, head)
                results.append({
                    "network": network, "provider": provider,
                    "range": span, "verdict": verdict,
                    "logs": count, "seconds": round(secs, 3),
                })
                if verdict.startswith("error") or verdict == "range_limited":
                    break
                accepted = span
            results.append({
                "network": network, "provider": provider,
                "max_accepted_range": accepted,
            })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default=None, help="probe one network id")
    parser.add_argument("--output", default=None, help="write JSON to a file")
    args = parser.parse_args()
    networks = [str(args.network)] if args.network else list(ROUTES)
    all_rows: list[dict[str, Any]] = []
    for network in networks:
        all_rows.extend(probe_network(network))
    rendered = json.dumps(
        {"probe": "evm-providers-v1", "rows": all_rows}, ensure_ascii=False, indent=1,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
