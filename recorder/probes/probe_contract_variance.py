"""Do BSC (56) and Robinhood (4663) contracts vary enough to be features?

Read-only: eth_getCode plus the pure analyze_code(). Writes nothing.
Answers the question schema.sql:1099 leaves open — "to be extended if another network varies".
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

import config
import evm_contract
from evm_rpc import EVMRPC

sys.stdout.reconfigure(encoding="utf-8")
URI = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 40
FLAGS = ("has_mint", "has_pause", "has_blacklist", "has_fee_setter",
         "has_limit_setter", "has_trading_switch")


def watched(net: str) -> list[str]:
    con = sqlite3.connect(URI, uri=True, timeout=120)
    try:
        rows = con.execute(
            """SELECT DISTINCT token_address FROM watch_windows
                WHERE network_id=? ORDER BY first_seen_at DESC LIMIT ?""",
            (net, LIMIT),
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


async def scan(rpc, net, addrs):
    out = []
    for a in addrs:
        try:
            shape = evm_contract.analyze_code(await rpc.get_code(net, a))
        except Exception as exc:
            print(f"    !! {a[:12]} {type(exc).__name__}: {exc}")
            continue
        out.append(shape)
        await asyncio.sleep(config.EVM_PACING_SECONDS)
    return out


async def main():
    rpc = EVMRPC()
    try:
        for net, name in (("56", "BSC"), ("4663", "Robinhood"), ("8453", "Base")):
            addrs = watched(net)
            print(f"\n=== {name} ({net}) — {len(addrs)} most-recent watched tokens ===")
            shapes = await scan(rpc, net, addrs)
            if not shapes:
                print("    no result")
                continue
            n = len(shapes)
            sizes = sorted(s["code_size"] for s in shapes)
            empty = sum(1 for s in shapes if s["code_size"] == 0)
            prox = sum(1 for s in shapes if s.get("is_proxy"))
            fc = [s["function_count"] for s in shapes if s.get("function_count") is not None]
            print(f"  scanned {n}   not-a-contract(size 0) {empty}   proxies {prox}")
            print(f"  code_size   distinct {len(set(sizes))}  min {sizes[0]}  max {sizes[-1]}")
            if fc:
                print(f"  function_count distinct {len(set(fc))}  min {min(fc)}  max {max(fc)}")
            for f in FLAGS:
                vals = [s.get(f) for s in shapes if s.get(f) is not None]
                ones = sum(1 for v in vals if v)
                d = len(set(vals))
                verdict = "VARIES" if d > 1 else "constant"
                print(f"  {f:20} =1 in {ones:>3}/{len(vals):<3} distinct {d}  {verdict}")
    finally:
        await rpc.aclose()


asyncio.run(main())
