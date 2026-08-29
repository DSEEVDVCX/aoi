"""How much COMPLETE data exists, and how fast does it grow?

Complete = no entirely-NULL family among the 9 families that have no documented
single-network restriction. The onchain_auth (Solana-only) and
onchain_contract_evm (Base/BSC/RH-only) families are excluded from the test,
because a Solana coin missing an EVM-only family is not incomplete.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

import config

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect(
    f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro", uri=True, timeout=180
)
con.row_factory = sqlite3.Row

FAMILIES = {
    "token_static": ["token_age_h", "decimals", "socials_count"],
    "market_snap": ["liquidity", "volume_24h", "tick_age_min"],
    "social": ["social_thesis_total", "thesis_counted"],
    "price_path": ["ret_24h_before", "bars_history_h"],
    "regime": ["sol_ret_24h"],
    "density": ["prior_signals_token"],
    "chain_own": ["chain_holder_count", "platform_holders"],
    "flow": ["flow_net_volume_5m", "flow_age_min"],
    "onchain_conc": ["onchain_top10_pct"],
}
FV = ("(SELECT CAST(value AS INTEGER) FROM meta "
      "WHERE key='current_feature_version')")
POP = (f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")

present = ",\n  ".join(
    "(" + " OR ".join(f"{c} IS NOT NULL" for c in cols) + f") AS has_{name}"
    for name, cols in FAMILIES.items()
)
rows = con.execute(
    f"SELECT entry_ts, token_address, network_id,\n  {present}\n"
    f"  FROM training_rows WHERE {POP}"
).fetchall()

keys = [f"has_{n}" for n in FAMILIES]
day = lambda ts: dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")

tot = len(rows)
complete = [r for r in rows if all(r[k] for k in keys)]
print(f"model population              : {tot} rows, "
      f"{len({(r['token_address'], r['network_id']) for r in rows})} coins")
print(f"COMPLETE on all 9 families    : {len(complete)} rows "
      f"({100.0*len(complete)/tot:.1f}%), "
      f"{len({(r['token_address'], r['network_id']) for r in complete})} coins")

print("\nper entry-day: rows / complete / %complete")
by_day: dict[str, list] = {}
for r in rows:
    by_day.setdefault(day(r["entry_ts"]), []).append(r)
for d in sorted(by_day):
    sub = by_day[d]
    c = sum(1 for r in sub if all(r[k] for k in keys))
    print(f"  {d}  {len(sub):>5} / {c:>5} / {100.0*c/len(sub):5.1f}%")

print("\nmissing-family frequency among INCOMPLETE rows:")
inc = [r for r in rows if not all(r[k] for k in keys)]
for name in FAMILIES:
    n = sum(1 for r in inc if not r[f"has_{name}"])
    print(f"  {name:14} missing in {n:>6} of {len(inc)} incomplete "
          f"({100.0*n/max(len(inc),1):5.1f}%)")
con.close()
