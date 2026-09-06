"""Feature extractor — the scattered archive ⇒ one training table (stage 2).

**The one law that governs this file**: every number in a row must be knowable
**at t=0 or earlier**. Every query here is explicitly time-bounded, and the tests
plant data after t0 and verify it never shows up. One mistake here silently poisons
the model and produces a fake accuracy that is only discovered with money.

**One piece for training and live**: the same `build_row` is called to build a
historical row and, later, a live signal row — a train/serve skew in the
computation destroys the model without showing up in any metric.

Missing columns stay None (FR-007: no fabricated zeros — absence itself is
information LightGBM learns).
"""
from __future__ import annotations

import json
import math
import statistics as st
from datetime import UTC, datetime
from typing import Any

import config
from db import RecorderDB
from extract import classify_asset

# Cap of the stored thesis sample: retroactive pagination fetches pages with a
# hard limit (100/page), so counting stops at ~400 for a coin that may carry
# 29,595 real theses. Rows at the cap are flagged via `thesis_counted_capped`
# so the model does not read a saturated count as if it were a measurement.
_THESIS_PAGE_CAP = 380
# 4: The ownership family (chain concentration + platform crowd positioning) and
#    external legitimacy (exchanges/CMC/description/banner) — training queries
#    require feature_version >= 2, and old rows stay valid with NULL in the new
#    columns (absent ≠ zero).
# 5: mintable/freezable fix. Both were NULL in 100% of rows: the source returns
#    the **address** of the mint/freeze authority, not a boolean, and the previous
#    transformer dropped every string to None. After the fix: Solana 312/312
#    measured (56 with a live mint authority, 19 with a freeze authority), and EVM
#    legitimately stays NULL — there is no authority in that sense there, so a
#    zero would fabricate "safe" for an unmeasured coin (FR-007).
# 6: holder_authors fix ⇒ social_holder_authors and social_holder_ratio. Both
#    were stuck at zero across 46,040 rows because the extractor read `equity`,
#    which is genuinely zero in 28,186/28,186 measured theses (a dead field from
#    upstream). The real seat is `authorTrade.humanTokenAmount`: after the
#    retroactive backfill, 99 distinct values over the 0-100 range, and 95.3% of
#    snapshots have at least one holder. Two information-free columns became
#    measurements — and the ratio is truly distributed (peak 30-60%, with a tail
#    of 906 snapshots whose every author is an owner).
# 7: The period-leaderboard family. The source caps at 50 leaders in the main
#    leaderboard and every pagination variant is silently ignored (measured: nine
#    variants, byte-identical lists), but /24h, /7d, and /30d each return **100**,
#    so the union of the four is 214 traders (164 unknown to the main board) — on
#    7,200 real buy events over three days: matching went from 265 (3.68%) to
#    1,099 (15.26%), i.e. ×4.15. Ranks are **split by period**, not merged (rank
#    7 in 24h is not rank 7 in totalPnL), and their range here is 1-100, not 1-50,
#    so `rank_le_50` regains variance on `best_rank_any_period`. The past is not
#    backfilled: the archive stored only totalPnL, so there is no history of
#    period ranks, and old rows legitimately stay NULL.
# 8: Snapshot source merge + the flow family. The first item is a **fix of a live
#    defect**, not an addition: market_features read the newest row in
#    market_ticks without distinguishing the source, and the two sources are not
#    peers — measured over 200k rows, `verified` carries price and liquidity 100%
#    of the time and **zero%** of the buy/sell counters, uniques, and holders,
#    while `trending` carries all of them. Result: **for 50% of coins the newest
#    row is poor**, and 14 coins in a single day had a rich measurement a median
#    of 105 minutes older that went entirely unused. Now we merge
#    column-by-column over the last 12 rows (newest non-empty wins), and
#    `tick_rich_age_min` measures the freshness of the counters alone so that a
#    two-hour-old reading is not counted as fresh. The second item is the `flow_*`
#    family from `token_flow`: the buy/sell split (every other volume we have is
#    a sum, so flow direction was unknown) and a **5-minute** tier we never had —
#    our shortest was one hour. The source is the same tokenDetails response
#    already fetched for the holders cycle: zero extra calls, 100% presence in
#    300 archived responses. The new family will be dropped from training as an
#    era marker until its coverage reaches both halves of the window — as
#    happened with the holders family, which is healthy behavior.
# 9: `chain_holders_delta_1h` / `_growth_1h` / `_span_min` — change in holders
#    of the **whole chain** (`tokenDetails.holders`, not fomo holders: measured,
#    platform users are 0.0-2.5% of chain holders, a mean of 1,970 versus 75,121).
#    The source gives no delta, so it is measured from two of our snapshots on
#    the pattern of `social_total_delta_1h`. Measured over 19,440 pairs: the
#    count changes in 84.9% of them ⇒ a signal, not a zero. And `_span_min` is
#    an explicit column because the query guarantees ≥60min, not =60min (cadence
#    25min ⇒ median 75.1min).
# 10: The `onchain_*` family — the first measurement in the project that **does
#    not come from FOMO at all** but straight from a Solana node. It opens what
#    was closed by two hard limits: FOMO gives only `top10`, so `top1` (the
#    single whale, a different risk from ten distributed holders) was completely
#    unknown; and its cadence is ~25min (measured: median gap 25.0 over 19,440
#    pairs, with **zero% ≤5min**) so there is no five-minute window. A single
#    node call returns all four together — measured live 2026-08-13 on 20
#    watched coins: 19 succeeded in 16.3s, top1 from 2.11% to 32.21% and top20
#    from 25.49% to 60.99% ⇒ real variance, not a constant column. And running
#    it in a dedicated process is what made the cadence possible: 73 Solana
#    coins ÷ 20 per cycle = a full sweep every 3.6min, against the 13 calls the
#    whole recorder cycle can afford. `onchain_top10_pct` deliberately mirrors
#    `chain_top10_pct` (two independent sources for the same thing =
#    cross-checking) and they are not merged. `_delta_span_min` is an explicit
#    column for the same reason as `chain_holders_span_min`. **Solana-only by
#    design**: the ERC-20 standard carries no on-chain holder list, so EVM rows
#    (55% of signals) legitimately stay NULL — "cannot be measured", not "zero".
#    The family will be dropped from training as an era marker until its coverage
#    reaches both halves of the window, as happened with the holders and flow
#    families.
# 11: `onchain_*authority*` — the slow tier from the same node: who can **mint
#    new supply** (`mint_authority`) or **freeze your sale**
#    (`freeze_authority`), plus `mutable`, `token2022`, and developer holding
#    from the chain. Structural risk, not market motion, and we had none of it at
#    all — FOMO does not expose contract authorities. Measured on 48 coins from
#    our live watchlist (2026-08-13) before writing any column: minting live in
#    3, freezing in 1, `mutable` 31 yes/17 no, and token-2022 27/48 — every
#    column varies. `burnt`, `ownership.frozen`, and `interface` were dropped
#    because they measured **constant** across the whole sample (0/48, 0/48,
#    48/48), so there is no information in them. `dev_holding_pct` covers ~15%
#    alone (Token-2022 returns `creators: []`), and that is measured absence, not
#    zero. Bumping the version is as cheap as it gets: the v10 sweep has not yet
#    reached 3,000 of 69,734 rows, so rebuilding costs almost nothing.
# 12: **EVM joins the `onchain_*` family** — it had been Solana-only by design
#    (comment 10), so the rows of 55% of signals were legitimately NULL. The fix
#    was not a provider but a balance ledger we build from `Transfer` logs
#    (`evm_layer.py`) that writes into the **same** `chain_concentration`
#    ⇒ the ten existing columns cover EVM with no new column. And the additions
#    are two columns the ledger gives and no provider does:
#    `onchain_holder_count` **exact**, with no rank cap (it stays NULL on Solana:
#    `getTokenLargestAccounts` returns at most 20 accounts and does not know the
#    total), and `onchain_holders_delta_5m` — the first five-minute window on
#    holder count in the project (the FOMO tier's cadence is 25min). And the
#    `onchain_contract_*` family: contract shape and authorities straight from
#    the byte-code (`eth_getCode` is free ⇒ 100% coverage versus 10% for
#    Sourcify). **On Base only**, and that is a measurement, not neglect
#    (2026-08-13): on BSC, 21 of 27 are tiny proxies pointing to just two
#    implementation contracts, with ownership left in both and no pause or fee
#    identifier ⇒ a constant column with no information; on Robinhood, six
#    templates of identical sizes; whereas Base has 19 of 22 full contracts sized
#    135B–14.8KB with `owner` in 7 and `mint` in 2 ⇒ the variance is real, so the
#    column discriminates. And both families will be dropped from training as era
#    markers until their coverage reaches both halves of the window — healthy
#    behavior, not a defect.
# 13: **`pre_signal_runup`** — the signal's position on the run-up curve as a
#     continuous measurement. The gates see hard cutoffs, and the model deserves
#     the full gradient: log(t0 close / oldest close in the last 24h). Measured
#     on 2,968 labeled signals: this value alone separates the population more
#     strongly than any existing family — the 48h final return drops from -2.1%
#     (early) to -36.0% (very late, >+150% prior run-up), and rug jumps
#     0.0%→7.1%. Without this feature the model treats a row on the reject
#     boundary (140%) the same as a perfectly quiet one (0%) — and that is a
#     difference of fate we do not hide from the model.
# 14: **8 dead columns removed from the feature list** (owner decision
#     2026-08-28): raw feed-format fields the source stopped sending (filled in
#     91 rows of 102,963 = 0.09% of all history, all between Aug 6-23 from an old
#     era). Keeping them in the export misleads the external analyst (assumes the
#     columns are alive, tries fillna, distorts the result) — so they are lifted
#     from FEATURE_COLUMNS, dropping out of every future export and training, and
#     kept in the table as an honest archive (no migration over 102k rows).
#     Removed: unique_traders, num_trades, minutes, price_change_pct,
#     total_volume, volume_per_trader, are_top_traders, top_trader_match_ratio.
#     `onchain_holders_delta_5m` was kept deliberately: its deadness was
#     cadence-driven (it needs two snapshots ≤15 minutes apart) and the
#     2026-08-27 backfill fix revived it — 93% of EVM gaps now fall inside the
#     window (measured), so the column is rising, not dead.
# 15: **`is_scam` removed** (owner decision 2026-08-28): a column with no
#     variance — only 36 rows in all history, all zero; the value 1 never
#     appeared even once. No variance, no information, and the safety filters
#     (age/run-up gates + the upcoming p_danger) cover the role. The raw field
#     is still extracted into token_static as an archive; only the feature was
#     lifted.
# 16: **socials from DEX Screener** — two columns from a source independent of
#     fomo: `social_channels_dex` (count of social channels; measured coverage
#     92% of active coins) and `social_match_fomo_dex` (agreement between the
#     two sources — their disagreement is a spoofed-profile pattern). A full
#     inventory of the remaining DEX fields showed them duplicated or inferior
#     to our stock (our flow is deeper: 5 minutes with uniques versus a bare m5
#     count), so the tier is restricted to socials. One call per **new** coin
#     at acceptance — stored in token_static (written once), no ongoing loop.
FEATURE_VERSION = 16


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def epoch_of(value: Any) -> int | None:
    """Timestamp → epoch. Accepts ISO (fomo's Z form or the recorder's +00:00)
    **and** a numeric epoch (seconds or milliseconds).

    Necessary because fomo does not unify formats: `token_static.token_created_at`
    is stored as a numeric epoch (1784983617) while `signal_events.ts` is ISO
    text — assuming ISO alone left `token_age_h` empty 100% until the coverage
    report caught it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip()
        try:
            num = float(text)
        except (ValueError, TypeError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                num = parsed.timestamp()
            except (ValueError, TypeError):
                return None
    # Milliseconds (13 digits) or seconds (10) — the threshold tells them apart unambiguously
    if not math.isfinite(num) or num <= 0:
        return None
    if num > 1e11:
        num /= 1000.0
    return int(num) if num > 0 else None


def _log1p(v: Any) -> float | None:
    """Safe logarithm for volumes (measured range $10⁵→$10¹³ — the raw scale swamps the division)."""
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    return math.log1p(float(v))


def _div(a: Any, b: Any) -> float | None:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or not b:
        return None
    return float(a) / float(b)


def _min_or_none(*values: Any) -> int | float | None:
    """Smallest existing numeric value, or None if all are absent.

    Needed to aggregate ranks across periods: `min()` on a list containing None
    raises TypeError, and `min(x or inf ...)` fabricates a number where we did not measure (FR-007).
    """
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return min(nums) if nums else None


def _ret(new: Any, old: Any) -> float | None:
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)) or old <= 0:
        return None
    return float(new) / float(old) - 1.0


def _json_list(text: Any) -> list[Any] | None:
    """JSON text of an array → list. Malformed/absent → None (no fabricated empty list)."""
    if not text:
        return None
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, list) else None


# ---------------------------------------------------------------------------
# Family A — the event itself (signal_events or activity_events)
# ---------------------------------------------------------------------------
def event_features(row: dict[str, Any], t0: int) -> dict[str, Any]:
    """Features of the triggering event. Works on both shapes (forward and
    retroactive) without fabrication: fields missing in the retroactive source stay None."""
    dt = datetime.fromtimestamp(t0, UTC)
    rank = row.get("buyers_best_rank")
    # Zero is a **measured** value, not absence: 1,640 events with size 0.0 (a full
    # position exit). `a or b` used to turn them into None, costing the model a
    # correct piece of information — hence this explicit check, not falsiness.
    size = row.get("size_usd")
    if size is None:
        size = row.get("usd_amount")
    top_ids = _json_list(row.get("top_trader_ids_json"))
    ticker = row.get("ticker")
    # Presence across leaderboards. Stored on the event (the extractor computes it
    # at capture time, where the maps are present); older rows of the family stay None, unfabricated.
    periods_matched = row.get("top_trader_periods_matched")
    return {
        "signal_type": row.get("signal_type") or row.get("event_type"),
        "size_usd": size,
        "in_amount": row.get("in_amount"),
        "out_amount": row.get("out_amount"),
        "token_amount": row.get("token_amount"),
        "avg_cost": row.get("avg_cost"),
        # Is the buyer paying above their average cost (conviction) or below (averaging down)?
        "price_to_avg_cost": _div(row.get("price_usd"), row.get("avg_cost")),
        "realized_pnl_usd": row.get("realized_pnl_usd"),
        "num_swaps": row.get("num_swaps"),
        "is_first_buy": row.get("is_first_buy"),
        "buyer_pnl_pct": row.get("buyer_pnl_pct"),
        "market_cap": row.get("market_cap"),
        "fdv": row.get("fdv"),
        "price_usd": row.get("price_usd"),
        "log_market_cap": _log1p(row.get("market_cap")),
        "log_size_usd": _log1p(size),
        "size_to_mcap": _div(size, row.get("market_cap")),
        # (fv14) The eight dead columns were lifted from the feature list; the raw
        # fields are still extracted into signal_events (archive) but no feature is computed from them anymore.
        "top_trader_match_count": row.get("top_trader_match_count"),
        # How many leaders fomo announced versus how many we matched with our
        # leaderboards: the ratio tells an "elite block" from a "nominal block".
        "top_traders_listed": len(top_ids) if top_ids is not None else None,
        "buyers_best_rank": rank,
        "rank_le_10": (1 if rank <= 10 else 0) if isinstance(rank, int) else None,
        "rank_le_50": (1 if rank <= 50 else 0) if isinstance(rank, int) else None,
        # Period leaderboards: each rank is an independent measurement — fourth in
        # 24h is not fourth in totalPnL. Passed through as-is; rows built before the merge ran stay NULL.
        "top_trader_match_count_24h": row.get("top_trader_match_count_24h"),
        "buyers_best_rank_24h": row.get("buyers_best_rank_24h"),
        "top_trader_match_count_7d": row.get("top_trader_match_count_7d"),
        "buyers_best_rank_7d": row.get("buyers_best_rank_7d"),
        "top_trader_match_count_30d": row.get("top_trader_match_count_30d"),
        "buyers_best_rank_30d": row.get("buyers_best_rank_30d"),
        # Presence breadth: in how many leaderboards (0-4) a buyer appeared. A top
        # performer in all four is a different animal from a 24h-only one — the first is a track record, the second may be a lucky strike.
        "top_trader_periods_matched": periods_matched,
        "top_trader_any_period": (
            (1 if periods_matched > 0 else 0)
            if isinstance(periods_matched, int) else None
        ),
        # Best rank across all periods: the strongest "elite bought" evidence with
        # the widest coverage, while the detailed ranks remain for the model to separate the periods if it wants.
        "best_rank_any_period": _min_or_none(
            rank,
            row.get("buyers_best_rank_24h"),
            row.get("buyers_best_rank_7d"),
            row.get("buyers_best_rank_30d"),
        ),
        # Ticker text: measured in similar projects as a quality/impersonation signal
        "ticker_len": len(ticker) if isinstance(ticker, str) else None,
        "ticker_has_digit": (
            1 if isinstance(ticker, str) and any(ch.isdigit() for ch in ticker) else
            (0 if isinstance(ticker, str) else None)
        ),
        "ticker_non_ascii": (
            1 if isinstance(ticker, str) and any(ord(ch) > 127 for ch in ticker) else
            (0 if isinstance(ticker, str) else None)
        ),
        "hour_utc": dt.hour,
        "dow": dt.weekday(),
    }


# ---------------------------------------------------------------------------
# Family B — token constants + creator fingerprint
# ---------------------------------------------------------------------------
def static_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    row = db._conn.execute(
        """SELECT * FROM token_static
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY recorded_at DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    out: dict[str, Any] = {
        "token_age_h": None, "launchpad_name": None, "migrated": None,
        "graduation_percent": None, "mintable": None,
        "freezable": None, "socials_count": None, "has_twitter": None,
        "creator_prior_tokens": None, "decimals": None, "name_len": None,
        "name_non_ascii": None,
        # External legitimacy — present in the raw data since day one, never extracted
        "exchanges_count": None, "listed_on_exchange": None, "has_cmc_id": None,
        "description_len": None, "has_banner": None,
        # (fv16) socials from DEX Screener: channels independent of fomo, and the
        # agreement between the two sources — their disagreement (fomo sees a
        # Twitter handle, DEX sees nothing) is a common spoofed-profile pattern. NULL = DEX not asked yet (absence, not zero).
        "social_channels_dex": None, "social_match_fomo_dex": None,
    }
    if row is None:
        return out
    created = epoch_of(row["token_created_at"])
    socials = [row["twitter"], row["telegram"], row["website"], row["discord"]]
    name = row["name"]
    # A negative age is physically impossible (a signal before the coin was
    # created): one measured row at −177 hours — fomo's stamp refers to
    # listing/re-listing, not creation. The value is untrustworthy
    # ⇒ None, not a negative number the model would learn as if it meant something.
    age_h = (t0 - created) / 3600 if created else None
    if age_h is not None and age_h < 0:
        age_h = None
    out.update({
        "token_age_h": age_h,
        "launchpad_name": row["launchpad_name"],
        "migrated": row["migrated"],
        "graduation_percent": row["graduation_percent"],
        # (fv15) is_scam was lifted from the features — extracted in the raw as an archive only.
        "mintable": row["mintable"],
        "freezable": row["freezable"],
        "socials_count": sum(1 for s in socials if s),
        "has_twitter": 1 if row["twitter"] else 0,
        # (fv16) DEX Screener socials — if this coin was ever asked:
        # its channel count, and its agreement with fomo (both see socials or neither does).
        "social_channels_dex": row["social_channels_dex"],
        "social_match_fomo_dex": row["social_match_fomo_dex"],
        "decimals": row["decimals"],
        # Name text: deceptive characters / abnormal length are a measured quality signal in similar projects
        "name_len": len(name) if isinstance(name, str) else None,
        "name_non_ascii": (
            1 if isinstance(name, str) and any(ord(ch) > 127 for ch in name) else
            (0 if isinstance(name, str) else None)
        ),
        # External legitimacy: a centralized-exchange listing or a CoinMarketCap
        # id is not something any coin grants itself at the press of a button —
        # unlike Twitter and a website. Measured on 516 coins: exchanges_count
        # available in 516 (up to 8 exchanges), cmc_id in 123, a description in
        # 217, a banner in 181. All were sitting in raw_json, unread.
        "exchanges_count": row["exchanges_count"],
        "listed_on_exchange": (
            1 if (row["exchanges_count"] or 0) > 0
            else (0 if row["exchanges_count"] is not None else None)
        ),
        "has_cmc_id": 1 if row["cmc_id"] else 0,
        # Description length: zero means a project that wrote not one line about itself (effort is a signal)
        "description_len": row["description_len"],
        "has_banner": row["has_banner"],
    })
    # Creator fingerprint: how many of their other coins **appeared before t0**
    # (a serial creator = a rug pattern). The time constraint is on the other
    # coin's first appearance, or the future leaks. The comparison happens in
    # Python, not SQL, because `token_created_at` may be a numeric epoch or ISO
    # depending on what fomo returned — strftime fails silently on the former.
    creator = row["creator_address"]
    if creator:
        others = db._conn.execute(
            """SELECT token_created_at FROM token_static
                WHERE creator_address = ? AND token_address != ?
                  AND network_id = ?
                  AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?""",
            (creator, token, network, t0),
        ).fetchall()
        out["creator_prior_tokens"] = sum(
            1 for o in others
            if (e := epoch_of(o["token_created_at"])) is not None and e <= t0
        )
    return out


# ---------------------------------------------------------------------------
# Family C — historical social momentum (created_at <= t0 only)
# ---------------------------------------------------------------------------
def social_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """Social momentum from two complementary sources:

    1. `token_thesis` — individual theses stamped with their write time ⇒ a
       **historical count** for any moment, but it is **a sample, not the truth**:
       pagination stopped at ~400/coin, so 30% of rows are
       saturated at the cap (`thesis_counted_capped` teaches the model that).
    2. `token_social` — a snapshot every ~30 minutes carrying the **real**
       `thesis_total` from the envelope (29,595 seen versus 400 stored), plus
       `thesis_authors` and **`holder_authors`** (authors who actually own a
       positive amount = skin in the game; its source is `authorTrade`, not
       `equity` — see the FEATURE_VERSION 6 comment).

    The snapshot is read **at/before t0 only**, so it is not a leak: the ban was
    on using today's value for a past event (README §9), not on a value measured before the decision.

    ⚠️ `num_likes` from `token_thesis` is forbidden forever (its value is from fetch time, not write time).
    """
    q = """SELECT COUNT(*) n, COUNT(DISTINCT user_id) authors, MAX(e) last_e, MIN(e) first_e
             FROM (SELECT user_id, CAST(strftime('%s', created_at) AS INTEGER) e
                     FROM token_thesis
                    WHERE token_address = ? AND network_id = ?)
             WHERE e <= ?"""
    r = db._conn.execute(q, (token, network, t0)).fetchone()
    n_all, authors, last_e, first_e = r["n"], r["authors"], r["last_e"], r["first_e"]

    def _count(since: int) -> int:
        return db._conn.execute(
            """SELECT COUNT(*) FROM token_thesis
                WHERE token_address = ?
                  AND network_id = ?
                  AND CAST(strftime('%s', created_at) AS INTEGER) <= ?
                  AND CAST(strftime('%s', created_at) AS INTEGER) > ?""",
            (token, network, t0, since),
        ).fetchone()[0]

    n_1h = _count(t0 - 3600) if n_all else 0
    n_24h = _count(t0 - 86400) if n_all else 0
    out: dict[str, Any] = {
        "thesis_counted": n_all,
        "thesis_counted_capped": 1 if n_all >= _THESIS_PAGE_CAP else 0,
        "thesis_authors_before": authors,
        "thesis_1h": n_1h,
        "thesis_24h": n_24h,
        # Discussion acceleration: the last hour's share of the day (near 1 = on fire now)
        "thesis_accel": _div(n_1h, n_24h),
        "hours_since_last_thesis": (t0 - last_e) / 3600 if last_e else None,
        # "Discovered late": age of the discussion before the signal (PLAN §2.3-F-C)
        "thesis_history_days": (t0 - first_e) / 86400 if first_e else None,
        # From token_social (stays None when there is no snapshot before t0)
        "social_thesis_total": None, "social_thesis_authors": None,
        "social_holder_authors": None, "social_holder_ratio": None,
        "social_replies": None, "social_snapshot_age_min": None,
        "social_total_delta_1h": None, "social_total_growth_1h": None,
        "social_authors_delta_1h": None,
    }

    snap = db._conn.execute(
        """SELECT thesis_total, thesis_authors, holder_authors, thesis_replies,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_social
            WHERE token_address = ? AND network_id = ?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    if snap is None:
        return out
    out.update({
        "social_thesis_total": snap["thesis_total"],
        "social_thesis_authors": snap["thesis_authors"],
        "social_holder_authors": snap["holder_authors"],
        # Share of authors who are owners: enthusiasm that holds versus enthusiasm that only talks
        "social_holder_ratio": _div(snap["holder_authors"], snap["thesis_authors"]),
        "social_replies": snap["thesis_replies"],
        "social_snapshot_age_min": (t0 - snap["e"]) / 60,
    })
    # Momentum delta: a snapshot at least an hour older ⇒ a measured acceleration, not an inferred one
    prev = db._conn.execute(
        """SELECT thesis_total, thesis_authors,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_social
            WHERE token_address = ? AND network_id = ?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, snap["e"] - 3600),
    ).fetchone()
    if prev is not None:
        out["social_total_delta_1h"] = (
            snap["thesis_total"] - prev["thesis_total"]
            if snap["thesis_total"] is not None and prev["thesis_total"] is not None
            else None
        )
        out["social_total_growth_1h"] = _ret(snap["thesis_total"], prev["thesis_total"])
        out["social_authors_delta_1h"] = (
            snap["thesis_authors"] - prev["thesis_authors"]
            if snap["thesis_authors"] is not None and prev["thesis_authors"] is not None
            else None
        )
    return out


# ---------------------------------------------------------------------------
# Family D — the price path **before** the signal (clean candles only)
# ---------------------------------------------------------------------------
def price_history_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    bars = db._conn.execute(
        """SELECT ts, h, l, c, v, h_suspect FROM token_bars
            WHERE token_address=? AND network_id=? AND resolution=?
              AND ts + CAST(resolution AS INTEGER) * 60 <= ?
              AND c_suspect = 0
            ORDER BY ts""",
        (token, network, config.BARS_RESOLUTION, t0),
    ).fetchall()
    out: dict[str, Any] = {
        "ret_1h_before": None, "ret_4h_before": None, "ret_24h_before": None,
        "ret_7d_before": None, "vol_24h_before": None, "flat_ratio_24h": None,
        "dist_from_ath": None, "ath_history_complete": 0,
        "ath_history_days": None, "bars_history_h": None, "bars_count_24h": None,
        "bar_vol_1h": None, "bar_vol_24h": None, "vol_surge_1h": None,
        "up_candle_ratio_24h": None,
        # fv13: the signal's position on the run-up curve — log(t0 close / the
        # oldest close in a 24h window). Measured on 2,968 signals: the strongest
        # population separator in the project (rug 0.0% for early → 7.1% for very
        # late, and the 48h final -2.1% → -36%).
        "pre_signal_runup": None,
    }
    if not bars:
        return out
    last_c = bars[-1]["c"]
    first_ts = bars[0]["ts"]
    out["bars_history_h"] = (t0 - first_ts) / 3600

    def _close_at_or_before(target: int) -> float | None:
        prev = None
        for b in bars:
            if b["ts"] <= target:
                prev = b["c"]
            else:
                break
        return prev

    for label, secs in (("1h", 3600), ("4h", 14400), ("24h", 86400), ("7d", 604800)):
        ref = _close_at_or_before(t0 - secs)
        out[f"ret_{label}_before"] = _ret(last_c, ref)

    window = [b for b in bars if b["ts"] > t0 - 86400]
    out["bars_count_24h"] = len(window)

    # fv13 — pre-signal run-up: the last 288 candles (24h) before t0. `bars`
    # enforces `ts + bar_duration <= t0`, so a candle opened before the signal and
    # closing after it never enters at all — the point-in-time law is structural, not audited.
    runup_window = bars[-288:]
    if len(runup_window) >= 12:
        first_c = runup_window[0]["c"]
        if (isinstance(first_c, (int, float)) and first_c > 0
                and isinstance(last_c, (int, float)) and last_c > 0):
            out["pre_signal_runup"] = math.log(last_c / first_c)
    if len(window) >= 3:
        rets = [
            _ret(window[i]["c"], window[i - 1]["c"]) for i in range(1, len(window))
        ]
        rets = [r for r in rets if r is not None]
        if len(rets) >= 2:
            out["vol_24h_before"] = st.pstdev(rets)
            # "Calm before the explosion": share of nearly flat candles (PLAN §2.3-G)
            out["flat_ratio_24h"] = sum(1 for r in rets if abs(r) < 0.005) / len(rets)
            out["up_candle_ratio_24h"] = sum(1 for r in rets if r > 0) / len(rets)
    # Trading volume before the signal: a volume burst usually precedes the price.
    # `v` is 100% covered and was entirely ignored.
    vols_24h = [b["v"] for b in window if isinstance(b["v"], (int, float))]
    hour = [b for b in bars if b["ts"] > t0 - 3600]
    vols_1h = [b["v"] for b in hour if isinstance(b["v"], (int, float))]
    if vols_24h:
        out["bar_vol_24h"] = sum(vols_24h)
    if vols_1h:
        out["bar_vol_1h"] = sum(vols_1h)
    # Last hour's share of the day's volume: 1/24 = normal, toward 1 = on fire now
    out["vol_surge_1h"] = _div(out["bar_vol_1h"], out["bar_vol_24h"])
    # All-time high: **distorted tails are excluded** — a candle whose close is
    # clean but whose high is corrupt (39 measured candles) used to make dist_from_ath = −0.99999997 on 49 rows.
    highs = [
        b["h"] for b in bars
        if isinstance(b["h"], (int, float)) and not b["h_suspect"]
    ]
    # 1D candles are pulled backwards until the source's history is exhausted. We
    # do not use the partial page: a missing older section could silently lower
    # the ATH and turn the feature into a "local peak".
    daily = db._conn.execute(
        """SELECT MIN(b.ts) AS first_ts, MAX(b.h) AS ath
             FROM token_bars b
             JOIN historical_bars_state s
               ON s.token_address=b.token_address
              AND s.network_id=b.network_id
              AND s.resolution=b.resolution
              AND s.last_status='ok'
            WHERE b.token_address=? AND b.network_id=? AND b.resolution='1D'
              AND b.ts + 86400 <= ? AND b.h_suspect=0""",
        (token, network, t0),
    ).fetchone()
    if daily and isinstance(daily["ath"], (int, float)):
        highs.append(daily["ath"])
        out["ath_history_complete"] = 1
        if daily["first_ts"] is not None:
            out["ath_history_days"] = (t0 - daily["first_ts"]) / 86400
    if highs and last_c:
        ath = max(highs)
        out["dist_from_ath"] = _ret(last_c, ath)  # negative = below the peak
    return out


# ---------------------------------------------------------------------------
# Family E — the last market snapshot before t0 (partial coverage ⇒ deliberate NULLs)
# ---------------------------------------------------------------------------
# Columns we read from market_ticks, merged across sources. Order does not matter.
_TICK_MERGE_COLUMNS: tuple[str, ...] = (
    "liquidity", "market_cap", "holders", "top10_holders_pct",
    "change_1h", "change_4h", "change_24h",
    "volume_1h", "volume_4h", "volume_24h",
    "txn_count_1h", "txn_count_24h",
    "buy_count_24h", "sell_count_24h", "unique_buys_24h", "unique_sells_24h",
    "circulating_supply", "total_supply",
)
# Columns carried by the rich source alone — their freshness is measured
# separately because they may come from an older row than the newest. (Measured: verified 0% of them, trending 100%.)
_TICK_RICH_COLUMNS: frozenset[str] = frozenset(
    {"buy_count_24h", "sell_count_24h", "unique_buys_24h", "unique_sells_24h",
     "holders", "top10_holders_pct"}
)
# How many rows we read looking for a non-empty value. There are three sources
# (trending/verified/filter) and each may write a row per minute, so 12 covers several cycles without scanning the table.
_TICK_MERGE_LOOKBACK = 12


def market_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """Newest market snapshot **at/before t0**, merged across sources column by column.

    The sources are **not field-equivalent**, measured over 200k rows: `verified`
    carries price, liquidity, and volume 100% of the time but carries **zero%** of
    the buy/sell counters, uniques, and holders, while `trending` carries all of
    them 100%. Reading "the newest row" alone inherited the poverty of whichever
    source happened to write last: **for 50% of coins the newest row was poor**,
    and 14 coins in a single day had a rich measurement a median of 105 minutes
    older sitting ignored in the table.

    So the merge here: we walk the rows from newest to oldest and fill each column
    with the first non-empty value we find. Not a relaxation for a new source's gaps but **a fix for a loss that exists today**.

    Freshness stays honest with two stamps, not one: `tick_age_min` is the age of
    the newest row (price and liquidity), and `tick_rich_age_min` the age of the
    row the counters came from — without the second, the model reads a two-hour-old counter as fresh.

    A zero value is a **measurement**, not absence: the check is `is None` explicitly, not `or` (FR-007).
    """
    empty = {
        "liquidity": None, "holders": None, "top10_holders_pct": None,
        "volume_24h": None, "buy_count_24h": None, "sell_count_24h": None,
        "buy_sell_ratio_24h": None, "unique_buys_24h": None,
        "unique_sells_24h": None, "tick_age_min": None, "tick_rich_age_min": None,
        "tick_change_1h": None, "tick_change_4h": None, "tick_change_24h": None,
        "tick_volume_1h": None, "tick_volume_4h": None,
        "tick_txn_1h": None, "tick_txn_24h": None,
        "volume_to_liquidity": None, "liquidity_to_mcap": None,
        "float_ratio": None,
    }
    rows = db._conn.execute(
        """SELECT *, CAST(strftime('%s', recorded_at) AS INTEGER) e FROM market_ticks
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT ?""",
        (token, network, t0, _TICK_MERGE_LOOKBACK),
    ).fetchall()
    if not rows:
        return empty

    m: dict[str, Any] = dict.fromkeys(_TICK_MERGE_COLUMNS)
    rich_e: int | None = None
    for r in rows:  # newest to oldest: the first non-empty value wins
        for col in _TICK_MERGE_COLUMNS:
            if m[col] is None:
                v = r[col]
                if v is not None:
                    m[col] = v
                    if rich_e is None and col in _TICK_RICH_COLUMNS:
                        rich_e = r["e"]

    return {
        "liquidity": m["liquidity"],
        "holders": m["holders"],
        "top10_holders_pct": m["top10_holders_pct"],
        "volume_24h": m["volume_24h"],
        "buy_count_24h": m["buy_count_24h"],
        "sell_count_24h": m["sell_count_24h"],
        "buy_sell_ratio_24h": _div(m["buy_count_24h"], m["sell_count_24h"]),
        "unique_buys_24h": m["unique_buys_24h"],
        "unique_sells_24h": m["unique_sells_24h"],
        # Two stamps: the first for price/liquidity (newest row), the second for the merged counters.
        "tick_age_min": (t0 - rows[0]["e"]) / 60,
        "tick_rich_age_min": (t0 - rich_e) / 60 if rich_e is not None else None,
        # Short windows: momentum closer to the decision moment than a 24-hour window
        "tick_change_1h": m["change_1h"],
        "tick_change_4h": m["change_4h"],
        "tick_change_24h": m["change_24h"],
        "tick_volume_1h": m["volume_1h"],
        "tick_volume_4h": m["volume_4h"],
        "tick_txn_1h": m["txn_count_1h"],
        "tick_txn_24h": m["txn_count_24h"],
        # Pool turnover: large volume on shallow liquidity = fast pumping and deadly slippage
        "volume_to_liquidity": _div(m["volume_24h"], m["liquidity"]),
        "liquidity_to_mcap": _div(m["liquidity"], m["market_cap"]),
        # Float ratio: circulating supply ÷ total — a tiny float = dump risk
        "float_ratio": _div(m["circulating_supply"], m["total_supply"]),
    }


# ---------------------------------------------------------------------------
# Family E2 — ownership: chain concentration + platform crowd positioning
# ---------------------------------------------------------------------------
def holders_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """Newest holding measurement **at/before t0** from two sources that measure two different things.

    `market_ticks.top10_holders_pct` above is dead (zero out of 1,430,475): the
    trending/verified lists never carry the key at all. And the chain check covers
    Solana only and is silent on all of EVM. The holders cycle fixes both via:

    - `token_details` ⇒ **chain concentration**: top 10 % of supply + the total
      holder count, on EVM and Solana alike (measured live: 83.2% for one coin,
      21.9% for another). The single strongest rug indicator.
    - `hodlers/top` ⇒ **crowd positioning**: fomo users who actually hold (274 of
      937, and 118 of 14,371) with each position's cost, unrealized profit, and
      holding duration. No supply percentages here at all, so we neither derive
      concentration from it nor guess it.

    `platform_penetration` is the derivation no single source gives: the
    platform's share of chain holders. High = a move led by the fomo crowd
    (reversible when they exit), low = broader external demand.

    `platform_underwater_ratio` is potential excess supply: losing holders who
    sell at the first recovery. Measured live: 49 of 49 underwater in one coin, versus 6 of 50 in another.

    All rows before 2026-08-09 will be None here (the cycle is new) — and that is
    deliberate: NULL means "we did not measure", not "zero" (FR-007).
    """
    out: dict[str, Any] = {
        "chain_top10_pct": None, "chain_holder_count": None,
        "chain_holders_delta_1h": None, "chain_holders_growth_1h": None,
        "chain_holders_span_min": None,
        "holders_age_min": None, "platform_holders": None,
        "platform_penetration": None, "platform_underwater_ratio": None,
        "platform_value_usd": None, "platform_median_hold_h": None,
        "platform_dev_holding": None,
    }
    q = """SELECT *, CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_holders
            WHERE token_address=? AND network_id=? AND source=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1"""
    det = db._conn.execute(q, (token, network, "token_details", t0)).fetchone()
    plat = db._conn.execute(q, (token, network, "hodlers_top", t0)).fetchone()
    if det is None and plat is None:
        return out

    if det is not None:
        out["chain_top10_pct"] = det["top10_pct"]
        out["chain_holder_count"] = det["holder_count"]
        # Change in holders of the **whole chain** over an hour: are people
        # entering or leaving. The source does not give it, so it is measured from
        # two of our snapshots. Measured on 19,440 consecutive pairs: the count
        # actually changes in 84.9% of them (median difference 5 holders, mean 23,
        # max 5,921) ⇒ a signal, not a zero.
        # A 5-minute tier is **impossible**, not merely postponed: the median gap
        # between snapshots is 25.0min (p25=25.0, p75=25.1 — a steady cadence, not
        # a jittering one) and **zero% of gaps are ≤5min**; shortening it to 5
        # would take 190/5 = 38 calls/minute against the 13 we can afford.
        prev = db._conn.execute(q, (token, network, "token_details",
                                    det["e"] - 3600)).fetchone()
        if prev is not None:
            span = (det["e"] - prev["e"]) / 60
            # The actual span is an explicit column: the query guarantees ≥60min,
            # not =60min, and the measured median is 75.1min (p25=74.4, p75=77.0)
            # because the 25min cadence skips three steps. Without the column, a
            # 75-minute difference is read as an hour (same logic as
            # `tick_rich_age_min`). Above 100min the older snapshot is from
            # another era — the tail stretches to 4,834min — ⇒ None, not a
            # misleading number. The cutoff keeps 89.1%.
            if span <= 100:
                out["chain_holders_span_min"] = span
                new, old = det["holder_count"], prev["holder_count"]
                if isinstance(new, int) and isinstance(old, int):
                    out["chain_holders_delta_1h"] = new - old
                out["chain_holders_growth_1h"] = _ret(new, old)
    if plat is not None:
        out["platform_holders"] = plat["platform_holders"]
        out["platform_value_usd"] = plat["platform_value_usd"]
        out["platform_dev_holding"] = plat["platform_dev_holding"]
        out["platform_underwater_ratio"] = _div(
            plat["platform_underwater"], plat["platform_holders_listed"]
        )
        secs = plat["platform_median_hold_seconds"]
        out["platform_median_hold_h"] = secs / 3600 if secs is not None else None
        if det is not None:
            out["platform_penetration"] = _div(
                plat["platform_holders"], det["holder_count"]
            )
    # Measurement freshness: the newest stamp of the two sources (each is refreshed by its own cycle)
    stamps = [r["e"] for r in (det, plat) if r is not None]
    out["holders_age_min"] = (t0 - max(stamps)) / 60 if stamps else None
    return out


def onchain_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """Ownership concentration measured **from the blockchain** at/before t0 — not from FOMO.

    The `chain_*` family above comes from FOMO and is bounded by two limits that
    cannot be lifted from there: it gives **top10 only** (so no answer to "one
    whale or ten distributed holders?", two entirely different risks), and its
    cadence is ~25 minutes (measured median gap 25.0 over 19,440 pairs, with
    **zero% of gaps ≤5min**), making it blind to a dump that plays out in minutes.

    A single Solana node call returns top1/5/10/20 together (measured 230ms), at
    a sweep cadence of ~3.6 minutes. So this family is not a repeat of the one
    before it but what that one could not do: `onchain_top1_pct` is a
    measurement, not a derivation, and `onchain_top1_delta_5m` is the first
    five-minute window on whale movement in the whole project.

    And `onchain_top10_pct` deliberately mirrors `chain_top10_pct`: two measures
    of the same thing from independent sources ⇒ free cross-checking, and they are not merged into one column.

    **Both networks since 2026-08-13** (it used to be Solana-only): the
    `evm_layer` tier builds a balance ledger from `Transfer` logs and writes into
    the **same table**, so this family covers EVM (55% of signals) with no new
    feature code. The only difference is `holder_count`: exact on EVM (the ledger
    knows every address) and still NULL on Solana, since
    `getTokenLargestAccounts` returns at most 20 accounts and does not know the
    total.

    And the holder count is read from the table's **snapshots**, not from the
    ledger directly: the ledger holds only the current state with no history, so
    reading it at build time would import information from the future and break
    the point-in-time law (file preamble).
    """
    out: dict[str, Any] = {
        "onchain_top1_pct": None, "onchain_top5_pct": None,
        "onchain_top10_pct": None, "onchain_top20_pct": None,
        "onchain_top_accounts": None, "onchain_age_min": None,
        "onchain_top1_delta_5m": None, "onchain_top10_delta_5m": None,
        "onchain_delta_span_min": None,
        "onchain_holder_count": None, "onchain_holders_delta_5m": None,
    }
    q = """SELECT top1_pct, top5_pct, top10_pct, top20_pct, top_accounts,
                  holder_count,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM chain_concentration
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1"""
    cur = db._conn.execute(q, (token, network, t0)).fetchone()
    if cur is None:
        return out

    out["onchain_top1_pct"] = cur["top1_pct"]
    out["onchain_top5_pct"] = cur["top5_pct"]
    out["onchain_top10_pct"] = cur["top10_pct"]
    out["onchain_top20_pct"] = cur["top20_pct"]
    out["onchain_top_accounts"] = cur["top_accounts"]
    out["onchain_holder_count"] = cur["holder_count"]
    out["onchain_age_min"] = (t0 - cur["e"]) / 60

    # The previous snapshot: 240s, not 300, deliberately — the target cadence is
    # 300s so the actual gap oscillates around it, and asking for ≥300 jumps over
    # the neighboring snapshot to the one before it, turning a "5-minute
    # difference" into an eleven-minute one. And `_span_min` is an explicit
    # column for the same reason as `chain_holders_span_min`: the query
    # guarantees ≥4min, not =5min, and without the column a 12-minute difference
    # is read as a five-minute one. Above 15min the older snapshot is from
    # another window ⇒ None, not a misleading number (the coin was outside the
    # sweep, or the cycle stumbled).
    prev = db._conn.execute(q, (token, network, cur["e"] - 240)).fetchone()
    if prev is not None:
        span = (cur["e"] - prev["e"]) / 60
        if span <= 15:
            out["onchain_delta_span_min"] = span
            for col, src in (("onchain_top1_delta_5m", "top1_pct"),
                             ("onchain_top10_delta_5m", "top10_pct"),
                             ("onchain_holders_delta_5m", "holder_count")):
                new, old = cur[src], prev[src]
                if new is not None and old is not None:
                    out[col] = new - old
    return out


def onchain_authority_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """Mint authority, mutability, and developer holding — from `chain_authority`.

    These are **structural** hazards, not market motion: a live mint authority
    means its holder can print new supply and dilute the value of what you own,
    and a freeze authority means they can stop you from selling. Measured on 48
    coins from our watchlist (2026-08-13): minting live in 3 and freezing in 1 —
    rare, and that is exactly what makes them information: a flag that is always
    raised does not tell one coin from another.

    `onchain_is_token2022` is not a technicality: 27 of 48 are on the new
    standard that allows extensions (transfer fee, permanent delegate) absent
    from the old one — so it is a proxy for *possible risk surface*, not for the
    coin's age.

    `onchain_dev_holding_pct` is measured on-chain and differs from
    `platform_dev_holding` (which comes from FOMO and is scoped to platform
    users). Its coverage is only ~15% because Token-2022 returns `creators: []`,
    so absence here is frequent and it is measured absence, not zero (FR-007).
    """
    out: dict[str, Any] = {
        "onchain_has_mint_authority": None,
        "onchain_has_freeze_authority": None,
        "onchain_is_mutable": None,
        "onchain_is_token2022": None,
        "onchain_dev_holding_pct": None,
        "onchain_auth_age_min": None,
    }
    row = db._conn.execute(
        """SELECT mint_authority, freeze_authority, is_mutable, token_program,
                  dev_holding_pct,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM chain_authority
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    if row is None:
        return out
    # Address present ⇒ 1, and its absence is a **measured** revocation ⇒ 0, not
    # None: the row itself attests that the measurement happened.
    out["onchain_has_mint_authority"] = 1 if row["mint_authority"] else 0
    out["onchain_has_freeze_authority"] = 1 if row["freeze_authority"] else 0
    out["onchain_is_mutable"] = row["is_mutable"]
    prog = row["token_program"]
    out["onchain_is_token2022"] = None if not prog else int(prog == "spl-token-2022")
    out["onchain_dev_holding_pct"] = row["dev_holding_pct"]
    out["onchain_auth_age_min"] = (t0 - row["e"]) / 60
    return out


def onchain_contract_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """EVM contract shape and authorities — from the byte-code, on Base only.

    The counterpart of `onchain_authority_features` on the other side: that one
    asks "who can mint supply or freeze your sale?" on Solana, and this one asks
    the same on EVM — but the answer here is not read from a ready field (there
    is none in ERC-20) but from **the presence of the identifier in the byte-code
    distribution table**: a contract carrying `mint(address,uint256)` can mint,
    and one carrying `setMaxTxAmount` can restrict your sales.

    `onchain_is_proxy` is not a technicality: an EIP-1167 proxy means all the
    logic lives in another contract that may be swapped, so every risk flag we
    read from the byte-code **means nothing** — and that is exactly what makes
    the column information: zero flags on a proxy ≠ zero on a full contract.

    And `onchain_contract_age_min` is explicit, not derived: the cadence is hourly
    (versus five minutes in the concentration family), so a value 55 minutes old
    is ordinary, not an anomaly — and the model needs to know that.

    Unexamined networks (Solana, BSC, Robinhood) legitimately stay NULL: "not
    measured", not "zero" (FR-007).
    """
    out: dict[str, Any] = {
        "onchain_code_size": None, "onchain_function_count": None,
        "onchain_is_proxy": None, "onchain_owner_renounced": None,
        "onchain_has_mint_fn": None, "onchain_has_pause_fn": None,
        "onchain_has_blacklist_fn": None, "onchain_has_fee_setter": None,
        "onchain_has_limit_setter": None, "onchain_has_trading_switch": None,
        "onchain_contract_age_min": None,
    }
    row = db._conn.execute(
        """SELECT code_size, function_count, is_proxy, is_ownership_renounced,
                  has_mint, has_pause, has_blacklist, has_fee_setter,
                  has_limit_setter, has_trading_switch,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM evm_contract
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    if row is None:
        return out
    out["onchain_code_size"] = row["code_size"]
    out["onchain_function_count"] = row["function_count"]
    out["onchain_is_proxy"] = row["is_proxy"]
    # Stays NULL when no ownership form answered: "no owner function" and
    # "ownership renounced" are two different states (same column rationale as in `evm_contract`).
    out["onchain_owner_renounced"] = row["is_ownership_renounced"]
    for col, src in (
        ("onchain_has_mint_fn", "has_mint"),
        ("onchain_has_pause_fn", "has_pause"),
        ("onchain_has_blacklist_fn", "has_blacklist"),
        ("onchain_has_fee_setter", "has_fee_setter"),
        ("onchain_has_limit_setter", "has_limit_setter"),
        ("onchain_has_trading_switch", "has_trading_switch"),
    ):
        out[col] = row[src]
    out["onchain_contract_age_min"] = (t0 - row["e"]) / 60
    return out


def flow_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """Newest buy/sell flow **at/before t0** from `token_flow`.

    All the volume we knew so far was a sum: `volume_24h` does not say who was
    buying and who was selling. This source (the same `tokenDetails` response
    already fetched for the holders cycle, at no extra call) gives the split, and
    gives a **5-minute tier** we never had at all: our shortest was one hour,
    which is blind to the turn inside the 48-hour window.

    Ratios are the information, not absolute values: a coin with $90k of buy
    volume is not necessarily stronger than one with $9k — what matters is how
    much sell met it. Hence `_div` on every pair. We keep absolute values for the
    5m tier alone: the longer tiers already exist aggregated in `market_ticks`,
    so repeating their absolutes adds nothing.

    All rows from before the merge started will be None here — deliberate: NULL means "we did not measure", not "zero"
    (FR-007).
    """
    out: dict[str, Any] = {
        "flow_age_min": None,
        "flow_buy_volume_5m": None, "flow_sell_volume_5m": None,
        "flow_net_volume_5m": None, "flow_net_volume_1h": None,
        "flow_net_volume_24h": None,
        "flow_buy_sell_volume_ratio_5m": None,
        "flow_buy_sell_volume_ratio_1h": None,
        "flow_buy_sell_volume_ratio_24h": None,
        "flow_buy_count_5m": None, "flow_sell_count_5m": None,
        "flow_unique_buys_5m": None, "flow_unique_sells_5m": None,
        "flow_buy_sell_count_ratio_5m": None,
        "flow_unique_ratio_5m": None,
        "flow_trade_size_5m": None,
        "flow_is_low_fees": None,
    }
    row = db._conn.execute(
        """SELECT *, CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_flow
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    if row is None:
        return out

    out["flow_age_min"] = (t0 - row["e"]) / 60
    out["flow_buy_volume_5m"] = row["buy_volume_5m"]
    out["flow_sell_volume_5m"] = row["sell_volume_5m"]
    out["flow_buy_count_5m"] = row["buy_count_5m"]
    out["flow_sell_count_5m"] = row["sell_count_5m"]
    out["flow_unique_buys_5m"] = row["unique_buys_5m"]
    out["flow_unique_sells_5m"] = row["unique_sells_5m"]
    out["flow_is_low_fees"] = row["is_low_fees"]

    # Net flow: an absent side is not treated as zero — unknown selling is not zero selling.
    for period in ("5m", "1h", "24h"):
        b = row[f"buy_volume_{period}"]
        s = row[f"sell_volume_{period}"]
        if b is not None and s is not None:
            out[f"flow_net_volume_{period}"] = b - s
        out[f"flow_buy_sell_volume_ratio_{period}"] = _div(b, s)

    out["flow_buy_sell_count_ratio_5m"] = _div(
        row["buy_count_5m"], row["sell_count_5m"]
    )
    # Few unique traders with many trades = wash trading or a bot; the reverse is broad demand.
    out["flow_unique_ratio_5m"] = _div(
        row["unique_buys_5m"], row["buy_count_5m"]
    )
    # Average buy trade size: a few whales or a small crowd?
    out["flow_trade_size_5m"] = _div(row["buy_volume_5m"], row["buy_count_5m"])
    return out


# ---------------------------------------------------------------------------
# Family F — the market system (hourly macro candles)
# ---------------------------------------------------------------------------
def macro_features(db: RecorderDB, t0: int) -> dict[str, Any]:
    out: dict[str, Any] = {"sol_ret_4h": None, "sol_ret_24h": None, "eth_ret_24h": None}
    by_label = {label: (addr, net) for label, addr, net in config.MACRO_BARS}
    for key, label, secs in (("sol_ret_4h", "SOL", 14400),
                             ("sol_ret_24h", "SOL", 86400),
                             ("eth_ret_24h", "WETH", 86400)):
        pair = by_label.get(label)
        if not pair:
            continue
        addr, net = pair
        rows = db._conn.execute(
            """SELECT ts, c FROM token_bars
                WHERE token_address=? AND network_id=? AND resolution=?
                  AND ts + CAST(resolution AS INTEGER) * 60 <= ?
                  AND c_suspect = 0 ORDER BY ts""",
            (addr, net, config.MACRO_BARS_RESOLUTION, t0),
        ).fetchall()
        if not rows:
            continue
        last = rows[-1]["c"]
        ref = None
        for b in rows:
            if b["ts"] <= t0 - secs:
                ref = b["c"]
            else:
                break
        out[key] = _ret(last, ref)
    return out


# ---------------------------------------------------------------------------
# Family G — signal density (the "is the market on fire?" context)
# ---------------------------------------------------------------------------
def density_features(
    db: RecorderDB, token: str, network: str, t0: int, exclude_key: str | None
) -> dict[str, Any]:
    """Event density before t0 — **from both sources together**.

    Computing from `signal_events` alone used to give structural zeros for
    retroactive rows (measured: 88% of 2025 rows are zero versus 4% for forward
    ones) — so the column became a marker of "where the row came from" rather
    than of coin activity, and the model learns the spurious effect. Including
    `activity_events` unifies the meaning across all rows.
    """
    prior = db._conn.execute(
        """SELECT COUNT(*) n, MAX(e) last_e FROM (
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM signal_events
                WHERE token_address = ? AND network_id = ? AND ts IS NOT NULL
               UNION
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM activity_events
                WHERE token_address = ? AND network_id = ? AND ts IS NOT NULL)
            WHERE e < ? AND (? IS NULL OR id != ?)""",
        (token, network, token, network, t0, exclude_key, exclude_key),
    ).fetchone()
    glob = db._conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM signal_events
                WHERE ts IS NOT NULL
               UNION
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM activity_events
                WHERE ts IS NOT NULL)
            WHERE e < ? AND e >= ?""",
        (t0, t0 - 3600),
    ).fetchone()[0]
    return {
        "prior_signals_token": prior["n"],
        "minutes_since_prior_signal": (
            (t0 - prior["last_e"]) / 60 if prior["last_e"] else None
        ),
        "global_signals_1h": glob,
    }


# ---------------------------------------------------------------------------
# The full row
# ---------------------------------------------------------------------------
FEATURE_COLUMNS: tuple[str, ...] = (
    # A — the event
    "signal_type", "size_usd", "in_amount", "out_amount", "token_amount",
    "avg_cost", "price_to_avg_cost", "realized_pnl_usd", "num_swaps",
    "is_first_buy", "buyer_pnl_pct", "market_cap", "fdv", "price_usd",
    "log_market_cap", "log_size_usd", "size_to_mcap",
    # (fv14) 8 dead columns were lifted from here: unique_traders, num_trades, minutes,
    # price_change_pct, total_volume, volume_per_trader, are_top_traders,
    # top_trader_match_ratio — the source stopped sending them (0.09% of all history).
    "top_trader_match_count",
    "top_traders_listed",
    "buyers_best_rank", "rank_le_10", "rank_le_50",
    "top_trader_match_count_24h", "buyers_best_rank_24h",
    "top_trader_match_count_7d", "buyers_best_rank_7d",
    "top_trader_match_count_30d", "buyers_best_rank_30d",
    "top_trader_periods_matched", "top_trader_any_period", "best_rank_any_period",
    "ticker_len", "ticker_has_digit", "ticker_non_ascii", "hour_utc", "dow",
    # B — constants and the creator
    # (fv15) is_scam lifted: 36 rows in all history, all 0 — no variance, no information.
    "token_age_h", "launchpad_name", "migrated", "graduation_percent",
    "mintable", "freezable", "socials_count", "has_twitter", "creator_prior_tokens",
    # (fv16) socials from DEX Screener: independent channels + source agreement
    # (their disagreement is a spoofed-profile pattern). Measured: 92% coverage of active coins.
    "social_channels_dex", "social_match_fomo_dex",
    "name_len", "name_non_ascii", "decimals",
    "exchanges_count", "listed_on_exchange", "has_cmc_id", "description_len",
    "has_banner",
    # C — social (historical count + a real snapshot before t0)
    "thesis_counted", "thesis_counted_capped", "thesis_authors_before",
    "thesis_1h", "thesis_24h", "thesis_accel", "hours_since_last_thesis",
    "thesis_history_days",
    "social_thesis_total", "social_thesis_authors", "social_holder_authors",
    "social_holder_ratio", "social_replies", "social_snapshot_age_min",
    "social_total_delta_1h", "social_total_growth_1h", "social_authors_delta_1h",
    # D — price and volume path before the signal
    "ret_1h_before", "ret_4h_before", "ret_24h_before", "ret_7d_before",
    "vol_24h_before", "flat_ratio_24h", "up_candle_ratio_24h", "dist_from_ath",
    "ath_history_complete", "ath_history_days",
    "bars_history_h", "bars_count_24h", "bar_vol_1h", "bar_vol_24h",
    "vol_surge_1h",
    # fv13 — the signal's position on the run-up curve (log-runup of the last
    # 24h): the strongest measured population separator in the project; the acceptance gate sees the hard cutoff, the model sees the gradient.
    "pre_signal_runup",
    # E — the market snapshot
    # `top10_holders_pct` **deliberately kept** (2026-08-22): zero of 3,837,466
    # rows in `market_ticks` and zero of 108,441 training rows, because the source
    # never sends the key at all (zero of 240 raw payloads inspected). Both
    # replacements work: `chain_top10_pct` has 4,506 non-empty values (3,002
    # distinct) and `onchain_top10_pct` 2,608 (1,956). So the column stays in the
    # table — dropping it is a migration over 108k rows for no gain — and leaves
    # the feature list alone. Hence `ROW_COLUMNS` is 198 and the table 199: a
    # deliberate, documented difference, not a forgotten migration drift. And it
    # does not warrant raising `feature_version`: the column is already empty in
    # every existing row, so dropping it changes no row's data.
    "liquidity", "holders", "volume_24h", "buy_count_24h",
    "sell_count_24h", "buy_sell_ratio_24h", "unique_buys_24h", "unique_sells_24h",
    "tick_age_min", "tick_change_1h", "tick_change_4h", "tick_change_24h",
    "tick_volume_1h", "tick_volume_4h", "tick_txn_1h", "tick_txn_24h",
    "volume_to_liquidity", "liquidity_to_mcap", "float_ratio",
    # Freshness of the counters alone: after the source merge they may come from
    # an older row than the newest, and without this the model assumes a two-hour-old counter is as fresh as a one-minute-old price.
    "tick_rich_age_min",
    # E2 — ownership: chain concentration (fixes the dead top10_holders_pct) + crowd positioning
    "chain_top10_pct", "chain_holder_count", "holders_age_min",
    # Whole-chain holder change over an hour + the measured actual span (≥60min, not =60min)
    "chain_holders_delta_1h", "chain_holders_growth_1h", "chain_holders_span_min",
    "platform_holders", "platform_penetration", "platform_underwater_ratio",
    "platform_value_usd", "platform_median_hold_h", "platform_dev_holding",
    # E2-b — ownership measured **from the chain**, not from FOMO: top1 is a
    # measurement, not a derivation (one whale ≠ ten distributed holders), and
    # `_delta_5m` is the first five-minute window on whales. Both networks since
    # v12: a balance ledger built from `Transfer` logs writes the same table.
    "onchain_top1_pct", "onchain_top5_pct", "onchain_top10_pct",
    "onchain_top20_pct", "onchain_top_accounts", "onchain_age_min",
    "onchain_top1_delta_5m", "onchain_top10_delta_5m", "onchain_delta_span_min",
    # Holder count exact from the ledger (EVM only — Solana is capped at 20
    # accounts so it stays NULL), plus a five-minute window on it that no provider gives.
    "onchain_holder_count", "onchain_holders_delta_5m",
    # E2-c — structural risk, not market motion: who can mint new supply or
    # freeze your sale. Measured: minting live in 3/48 and freezing in 1/48 — rare, hence discriminating.
    "onchain_has_mint_authority", "onchain_has_freeze_authority",
    "onchain_is_mutable", "onchain_is_token2022", "onchain_dev_holding_pct",
    "onchain_auth_age_min",
    # E2-d — its counterpart on EVM: from the byte-code, not a ready field (none
    # exists in ERC-20). Base only — on BSC and Robinhood the columns are repeated templates with no variance.
    "onchain_code_size", "onchain_function_count", "onchain_is_proxy",
    "onchain_owner_renounced", "onchain_has_mint_fn", "onchain_has_pause_fn",
    "onchain_has_blacklist_fn", "onchain_has_fee_setter",
    "onchain_has_limit_setter", "onchain_has_trading_switch",
    "onchain_contract_age_min",
    # E3 — flow: who buys and who sells (every other volume we have is a sum),
    # and a 5-minute tier we never had — our shortest was an hour, blind to fast turns.
    "flow_age_min", "flow_buy_volume_5m", "flow_sell_volume_5m",
    "flow_net_volume_5m", "flow_net_volume_1h", "flow_net_volume_24h",
    "flow_buy_sell_volume_ratio_5m", "flow_buy_sell_volume_ratio_1h",
    "flow_buy_sell_volume_ratio_24h",
    "flow_buy_count_5m", "flow_sell_count_5m",
    "flow_unique_buys_5m", "flow_unique_sells_5m",
    "flow_buy_sell_count_ratio_5m", "flow_unique_ratio_5m",
    "flow_trade_size_5m", "flow_is_low_fees",
    # F — macro
    "sol_ret_4h", "sol_ret_24h", "eth_ret_24h",
    # G — density
    "prior_signals_token", "minutes_since_prior_signal", "global_signals_1h",
)

META_COLUMNS: tuple[str, ...] = (
    "kind", "key", "token_address", "network_id", "entry_ts", "asset_class",
    "split", "is_independent", "is_live", "status", "suspect_bars",
    "feature_version", "built_at",
)

LABEL_COLUMNS: tuple[str, ...] = (
    "final_return_48h", "max_gain_1h", "max_gain_4h", "max_gain_24h",
    "max_gain_48h", "max_drawdown_48h", "time_to_peak_h", "is_rug",
    # (fv15) Labels for the catchable explosion and the early-entry snare —
    # two definitions measured on 2,378 explosions (config: EXPLOSIVE_*/PLUS20_*).
    # Leak guard: here only, never in FEATURE_COLUMNS.
    "is_explosive", "time_to_plus20_min",
)

ROW_COLUMNS: tuple[str, ...] = META_COLUMNS + FEATURE_COLUMNS + LABEL_COLUMNS


def build_features(
    db: RecorderDB, event: dict[str, Any], token: str, network: str, t0: int,
    exclude_key: str | None = None,
) -> dict[str, Any]:
    """All features for one decision at t0. **One piece for training and live.**"""
    out: dict[str, Any] = {}
    out.update(event_features(event, t0))
    out.update(static_features(db, token, network, t0))
    out.update(social_features(db, token, network, t0))
    out.update(price_history_features(db, token, network, t0))
    out.update(market_features(db, token, network, t0))
    out.update(holders_features(db, token, network, t0))
    out.update(onchain_features(db, token, network, t0))
    out.update(onchain_authority_features(db, token, network, t0))
    out.update(onchain_contract_features(db, token, network, t0))
    out.update(flow_features(db, token, network, t0))
    out.update(macro_features(db, t0))
    out.update(density_features(db, token, network, t0, exclude_key))
    return {k: out.get(k) for k in FEATURE_COLUMNS}


def source_event(db: RecorderDB, kind: str, key: str) -> dict[str, Any] | None:
    """The triggering event of an outcome row: a forward signal, a retroactive event, or a watch entry."""
    if kind == "signal":
        r = db._conn.execute("SELECT * FROM signal_events WHERE id=?", (key,)).fetchone()
    elif kind == "activity":
        r = db._conn.execute("SELECT * FROM activity_events WHERE id=?", (key,)).fetchone()
    else:  # watch/control — no triggering event (controls are random by design)
        return {}
    return dict(r) if r else None


def build_training_row(db: RecorderDB, outcome: dict[str, Any]) -> dict[str, Any] | None:
    """A labeled outcome row → a full training row (features + labels + marking)."""
    kind, key = outcome["kind"], outcome["key"]
    event = source_event(db, kind, key)
    if event is None:
        return None  # missing event: we do not fabricate a row
    token = outcome["token_address"]
    network = str(outcome["network_id"] or "")
    t0 = int(outcome["entry_ts"])
    price = event.get("price_usd")
    market_cap = event.get("market_cap")
    symbol = event.get("ticker") or event.get("symbol")
    asset_class = None
    if symbol or price is not None or market_cap is not None:
        asset_class = classify_asset(symbol, price, price, market_cap)[0]
    row: dict[str, Any] = {
        "kind": kind, "key": key, "token_address": token, "network_id": network,
        "entry_ts": t0, "asset_class": asset_class,
        "split": outcome["split"], "is_independent": outcome["is_independent"],
        # `activity` is aggregated retroactively even when the event stamp is
        # recent; live means the decision came from the forward signal_events, not merely that entry_ts is after the cutover.
        "is_live": 1 if kind == "signal" and t0 >= config.LIVE_START_TS else 0,
        "status": outcome["status"], "suspect_bars": outcome["suspect_bars"],
        "feature_version": FEATURE_VERSION,
        "built_at": datetime.now(UTC).isoformat(),
    }
    row.update(build_features(
        db, event, token, network, t0, exclude_key=key if kind == "signal" else None
    ))
    for c in LABEL_COLUMNS:
        row[c] = outcome[c]
    return row


def json_safe(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, default=str)
