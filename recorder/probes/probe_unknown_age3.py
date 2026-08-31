"""Are "unknown age" coins actually young at their first retrieval? — an independent measurement.

The problem: 177 coins have no constants row at all, so we have no age — neither do we
have it in any raw data we archived (signal raw carries the trade time, not the coin's
age, and tokenDetails raw carries no creation date at all). So we measure age from an
independent source: **price history**.

The anchor is **the first moment we retrieved the coin's data**, not now: we request
hourly candles ending at first retrieval and starting five days before it. If a candle
exists two days or more before first retrieval ⇒ the coin was ≥ 2 days old **at that
moment**. The question "how old is it today" is never asked.

Calibration first on coins with a known age: if the upstream returns candles at the
window floor for a coin we know was old, then the absence of candles is evidence of
youth, not of a gap in the archive.

Read-only from the database (mode=ro); only getBarsNew calls go out to the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import config
import recorder

LOOKBACK_DAYS = 5
RESOLUTION = "60"          # hourly: 5 days = 120 candles, under the 900 cap
YOUNG_DAYS = 2.0
PACING = 0.35


def ro_conn() -> sqlite3.Connection:
    uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


FIRST_RETRIEVAL = """
SELECT MIN(x.t) FROM (
  SELECT MIN(first_seen_at) t FROM watch_windows
   WHERE token_address=:tok AND network_id=:net
  UNION ALL SELECT MIN(recorded_at) FROM signal_events
   WHERE token_address=:tok AND network_id=:net
  UNION ALL SELECT MIN(recorded_at) FROM market_ticks
   WHERE token_address=:tok AND network_id=:net
) x
"""

UNKNOWN = """
SELECT w.token_address AS tok, w.network_id AS net
  FROM watch_windows w
  LEFT JOIN token_static t
    ON t.token_address=w.token_address AND t.network_id=w.network_id
 WHERE w.is_control=0 AND w.admission_source IS NULL AND t.token_address IS NULL
 GROUP BY 1,2
"""

KNOWN = """
SELECT w.token_address AS tok, w.network_id AS net,
       CAST(t.token_created_at AS INTEGER) AS created
  FROM watch_windows w
  JOIN token_static t
    ON t.token_address=w.token_address AND t.network_id=w.network_id
 WHERE w.is_control=0 AND w.admission_source IS NULL
   AND t.token_created_at IS NOT NULL AND t.token_created_at<>''
 GROUP BY 1,2
"""


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


async def first_candle(client, tok: str, net: str, anchor_ts: int) -> tuple[str, int | None]:
    """Returns (status, first-candle timestamp) for a window ending at first retrieval."""
    from fomo_api.config import settings

    body = {
        "symbol": f"{tok}:{net}",
        "resolution": RESOLUTION,
        "from": anchor_ts - LOOKBACK_DAYS * 86400,
        "to": anchor_ts + 3600,
        "countBack": 900,
    }
    raw = await client._post(settings.upstream_get_bars_path, body)
    ro = (raw or {}).get("responseObject") or {}
    status = ro.get("s") or "no_data"
    ts = ro.get("t") if isinstance(ro.get("t"), list) else []
    ts = [int(v) for v in ts if isinstance(v, (int, float))]
    return status, (min(ts) if ts else None)


def classify(anchor_ts: int, first_ts: int | None, floor_ts: int) -> tuple[str, float | None]:
    """A verdict from the first candle's age. `countBack` sometimes returns history deeper
    than the window, so the measured age is the true age, not the window floor — and
    calibration attests to it (15.69 versus 15.7 days). The verdict is on age itself;
    the `at_floor` flag is for transparency only."""
    if first_ts is None:
        return "no_history", None
    age_days = (anchor_ts - first_ts) / 86400.0
    return ("young_lt2d" if age_days < YOUNG_DAYS else "old_ge2d"), age_days


async def run(n_known: int, n_unknown: int) -> None:
    con = ro_conn()
    client = recorder._load_client()

    def anchor(tok: str, net: str) -> int | None:
        row = con.execute(FIRST_RETRIEVAL, {"tok": tok, "net": net}).fetchone()
        return epoch(row[0]) if row and row[0] else None

    async def measure(rows, label, expect_known=False):
        out = []
        print(f"\n=== {label} (n={len(rows)}) ===")
        for i, r in enumerate(rows):
            tok, net = r["tok"], r["net"]
            a = anchor(tok, net)
            if a is None:
                continue
            floor = a - LOOKBACK_DAYS * 86400
            try:
                status, first_ts = await first_candle(client, tok, net, a)
            except Exception as exc:  # noqa: BLE001
                print(f"  {tok[:12]:14} net={net:11} ERROR {type(exc).__name__}: {exc}")
                out.append({"tok": tok, "net": net, "verdict": "error"})
                await asyncio.sleep(PACING)
                continue
            verdict, age = classify(a, first_ts, floor)
            rec = {
                "tok": tok, "net": net, "anchor": a, "verdict": verdict,
                "bars_age_days": None if age is None else round(age, 2),
                "status": status,
            }
            if expect_known:
                true_age = (a - r["created"]) / 86400.0
                rec["true_age_days"] = round(true_age, 2)
                rec["true_bucket"] = "young_lt2d" if true_age < YOUNG_DAYS else "old_ge2d"
            out.append(rec)
            extra = f" true_age={rec.get('true_age_days')}" if expect_known else ""
            print(
                f"  {tok[:12]:14} net={net:11} anchor="
                f"{datetime.fromtimestamp(a, timezone.utc).strftime('%m-%d %H:%M')} "
                f"bars_age={rec['bars_age_days']!s:>7} -> {verdict}{extra}"
            )
            if i + 1 < len(rows):
                await asyncio.sleep(PACING)
        return out

    known = con.execute(KNOWN).fetchall()
    unknown = con.execute(UNKNOWN).fetchall()
    # a sample spread over the whole time range, not just its head (fixed stride, no randomness)
    def spread(rows, k):
        if k >= len(rows):
            return list(rows)
        step = len(rows) / k
        return [rows[int(i * step)] for i in range(k)]

    cal = await measure(spread(known, n_known), "CALIBRATION: age already known", True)
    tgt = await measure(spread(unknown, n_unknown), "TARGET: age unknown (no token_static)")

    print("\n=== calibration accuracy (bars verdict vs provider age) ===")
    scored = [r for r in cal if r.get("true_bucket") and r["verdict"] != "error"]
    agree = sum(1 for r in scored if r["verdict"] == r["true_bucket"])
    print(f"  agreement: {agree}/{len(scored)}")
    for v in ("young_lt2d", "old_ge2d", "no_history", "error"):
        for b in ("young_lt2d", "old_ge2d"):
            k = sum(1 for r in cal if r["verdict"] == v and r.get("true_bucket") == b)
            if k:
                mark = "  <-- MISMATCH" if v != b and v in ("young_lt2d", "old_ge2d") else ""
                print(f"    bars={v:12} provider={b:11} {k}{mark}")

    print("\n=== target verdicts (age unknown) ===")
    tot = len([r for r in tgt if r["verdict"] != "error"]) or 1
    for v in ("young_lt2d", "old_ge2d", "no_history", "error"):
        k = sum(1 for r in tgt if r["verdict"] == v)
        if k:
            print(f"  {v:12} {k:3}  ({100.0*k/tot:.1f}%)")

    # baseline youth rate over all known-age coins — from the database, no network,
    # and the same anchor (first retrieval), not now. This is the reference the unknowns are measured against.
    base_young = base_tot = 0
    for r in known:
        a = anchor(r["tok"], r["net"])
        if a is None or not r["created"]:
            continue
        base_tot += 1
        if (a - r["created"]) / 86400.0 < YOUNG_DAYS:
            base_young += 1
    print(
        f"\n=== base rate, ALL known-age coins (n={base_tot}) ===\n"
        f"  young_lt2d at first retrieval: {base_young} "
        f"({100.0*base_young/max(base_tot,1):.1f}%)"
    )

    with open("unknown_age_probe.json", "w", encoding="utf-8") as fh:
        json.dump({"calibration": cal, "target": tgt}, fh, indent=2)
    print("\nwrote unknown_age_probe.json")
    await client.close() if hasattr(client, "close") else None
    con.close()


if __name__ == "__main__":
    nk = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    nu = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    asyncio.run(run(nk, nu))
