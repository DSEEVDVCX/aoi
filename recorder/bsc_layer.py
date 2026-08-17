# -*- coding: utf-8 -*-
"""طبقة قياس BSC اللحظي عبر NodeReal.

تطلب NodeReal أعلى 20 رصيداً مرتبة، وعدد الحائزين، بينما يُقرأ totalSupply
من العقد نفسه. لا نستخدم هذه الطبقة لبناء دفتر Transfer: NodeReal هو المصدر
المفهرس للحالة الحالية، وRPC القياسي يبقى مسار التحويلات للشبكات التي اكتمل
تاريخها.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from typing import Any, Sequence

import config
import evm_rpc
from db import RecorderDB
from nodereal_rpc import NodeRealRateLimit

_BURN = {address.lower() for address in evm_rpc.BURN_ADDRESSES}
_TIERS = ((1, "top1_pct"), (5, "top5_pct"), (10, "top10_pct"), (20, "top20_pct"))


def build_bsc_concentration_row(
    *, token: str, recorded_at: str, watch_first_seen_at: str,
    entry_signal_id: str | None, is_control: int, holder_count: int | None,
    total_supply: int, top: Sequence[tuple[str, int]],
    burn_balances: Sequence[tuple[str, int]] = (),
) -> dict[str, Any] | None:
    """يبني صف التركّز من قيم uint256، مع استبعاد الحرق من النسب."""
    burn_supply = sum(int(balance) for _, balance in burn_balances)
    supply = max(0, int(total_supply) - burn_supply)
    amounts = [int(balance) for address, balance in top if address.lower() not in _BURN and int(balance) > 0]
    live_holder_count = (
        None if holder_count is None else max(
            0, int(holder_count) - sum(1 for _, balance in burn_balances if int(balance) > 0)
        )
    )
    if supply <= 0 or not amounts:
        return None
    row: dict[str, Any] = {
        "token_address": token.lower(),
        "network_id": config.BSC_NODEREAL_NETWORK,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": int(bool(is_control)),
        "supply": float(supply),
        "decimals": None,
        "holder_count": live_holder_count,
        "top_accounts": len(amounts),
        "is_replay": 0,
        "raw_json": {
            "source": "nodereal_bsc",
            "supply_base": str(supply),
            "total_supply_base": str(total_supply),
            "holder_count": live_holder_count,
            "provider_holder_count": holder_count,
            "burn_balances": [[address, str(balance)] for address, balance in burn_balances],
            "top": [[address.lower(), str(balance)] for address, balance in top],
        },
    }
    for n, column in _TIERS:
        row[column] = 100.0 * sum(amounts[:n]) / supply
    return row


async def _maybe_sleep(sleep, seconds: float) -> None:
    result = sleep(seconds)
    if inspect.isawaitable(result):
        await result


async def run_bsc_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep,
) -> dict[str, int]:
    """يحدّث شريحة BSC المستحقة ويحفظ لقطة لكل عملة نجحت."""
    stats = {"bsc_due": 0, "bsc_rows": 0, "bsc_errors": 0, "bsc_rate_limits": 0}
    now = datetime.fromisoformat(recorded_at)
    stale_before = (now - timedelta(seconds=config.BSC_NODEREAL_REFRESH_SECONDS)).isoformat()
    retry_before = (now - timedelta(seconds=config.BSC_NODEREAL_ERROR_RETRY_SECONDS)).isoformat()
    due = db.chain_fetch_due(
        limit=config.BSC_NODEREAL_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=retry_before,
        networks=(config.BSC_NODEREAL_NETWORK,),
    )
    stats["bsc_due"] = len(due)
    for index, watch in enumerate(due):
        token = str(watch["token_address"]).lower()
        try:
            holder_count = await rpc.holder_count(token)
            await _maybe_sleep(sleep, config.BSC_NODEREAL_CALL_PACING_SECONDS)
            total_supply = await rpc.total_supply(token)
            await _maybe_sleep(sleep, config.BSC_NODEREAL_CALL_PACING_SECONDS)
            # نطلب مقعدين إضافيين: إن دخل عنوانا الحرق أعلى القائمة يبقى لدينا
            # أعلى 20 حائزاً فعلياً بعد استبعادهما.
            top = await rpc.top_holders(token, top_n=20 + len(evm_rpc.BURN_ADDRESSES))
            burn_balances = []
            for address in evm_rpc.BURN_ADDRESSES:
                await _maybe_sleep(sleep, config.BSC_NODEREAL_CALL_PACING_SECONDS)
                burn_balances.append((address, await rpc.balance_of(token, address)))
            row = build_bsc_concentration_row(
                token=token,
                recorded_at=recorded_at,
                watch_first_seen_at=watch["first_seen_at"],
                entry_signal_id=watch.get("entry_signal_id"),
                is_control=int(watch.get("is_control") or 0),
                holder_count=holder_count,
                total_supply=total_supply,
                top=top,
                burn_balances=burn_balances,
            )
            if row is None:
                db.set_chain_state(token, config.BSC_NODEREAL_NETWORK, "empty", None, recorded_at)
                continue
            if db.insert_chain_concentration(row):
                stats["bsc_rows"] += 1
            db.set_chain_state(token, config.BSC_NODEREAL_NETWORK, "ok", row["top1_pct"], recorded_at)
        except NodeRealRateLimit as exc:
            stats["bsc_rate_limits"] += 1
            db.set_meta("last_error_bsc_nodereal", f"{recorded_at}: {type(exc).__name__}")
            db.set_chain_state(token, config.BSC_NODEREAL_NETWORK, "error", None, recorded_at)
        except Exception as exc:  # noqa: BLE001 — عملة واحدة لا تسقط الشريحة
            stats["bsc_errors"] += 1
            db.set_meta("last_error_bsc_nodereal", f"{recorded_at}: {type(exc).__name__}: {exc}")
            db.set_chain_state(token, config.BSC_NODEREAL_NETWORK, "error", None, recorded_at)
        if index + 1 < len(due):
            await _maybe_sleep(sleep, config.BSC_NODEREAL_PACING_SECONDS)
    return stats
