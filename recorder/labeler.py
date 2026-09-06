"""The labeler: computes outcomes (labels) from candles once the window completes.

The separation from the recorder is principled, not organizational: the
recorder records raw data with a timestamp only, and computing any value
derived from the future at recording time is leakage. The labeler runs in a
separate process (FomoLabeler) and never touches a signal until after
`entry + 48h + margin` — by then the "future" has become archived past.

No-leakage guarantees inside the computation itself:
- Entry price = the close of the **first candle at or after** the signal
  moment (never before it — that would be reverse leakage), and with a lag
  of at most 30 minutes, otherwise `status=no_entry`.
- Peaks and troughs come from candles **strictly after the entry candle**:
  the entry candle's own peak may have occurred before our notional
  execution, so it is not counted as a gain.
- `bars_truncated` flags a series that ended early instead of dropping it:
  a dead coin is **a signal, not missing data** — excluding it introduces
  survivorship bias (documented in fomo-getbars).

The split is deterministic via a hash of the token address — all signals for a
single token always land in the same partition, otherwise the model would
memorize tokens (14.2 signals/token) and validation would become an illusion.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import config
from db import RecorderDB, utcnow_iso
from features import epoch_of

# Partial-gain windows (in hours) — the max_gain_* columns
_GAIN_WINDOWS = (1, 4, 24, 48)


def assign_split(token_address: str) -> str:
    """Deterministic train/val/test from a hash of the address (70/10/20).

- **Per token, not per row**: all of a token's signals go to one partition —
  prevents memorization leakage.
- **Deterministic and stateless**: the same address maps to the same
  partition in any run and on any machine, with no table to lose or
  regenerate.
- lower() normalizes EVM addresses with mixed casing; Solana addresses are
  case-sensitive, but normalizing them here is harmless (worst case: two
  distinct tokens end up in one partition).
    """
    digest = hashlib.sha1(
        token_address.strip().lower().encode(), usedforsecurity=False
    ).hexdigest()
    bucket = int(digest[:8], 16) % 10
    if bucket <= 6:
        return "train"
    if bucket == 7:
        return "val"
    return "test"


def compute_labels(
    bars: Sequence[Mapping[str, Any]],
    entry_ts: int,
    window_h: int | None = None,
    admission_price_usd: float | None = None,
) -> dict[str, Any]:
    """Candles (ascending) + an entry moment → an outcomes dict.

A pure function: no database, no network, no system clock — deterministically
testable. Always returns a dict containing `status`:
  ok         — an entry exists with candles after it; all required fields computed.
  no_entry   — no entry candle within the deadline (a listing gap at signal time).
  no_bars    — an entry candle with no candle after it (the coin died instantly).
  incomplete — candles exist, but a required metric cannot be derived from them.
    """
    window_h = window_h or config.LABEL_WINDOW_HOURS
    window_end = entry_ts + window_h * 3600
    out: dict[str, Any] = {
        "entry_px": None, "entry_lag_s": None,
        **{f"max_gain_{h}h": None for h in _GAIN_WINDOWS},
        "max_drawdown_48h": None, "final_return_48h": None,
        "time_to_peak_h": None, "candles_48h": 0, "suspect_bars": 0,
        "last_bar_lag_h": None, "bars_truncated": None, "is_rug": None,
        # (fv15) Explosive label: a peak ≥2x **and** +50% of it reachable within
        # the first 24h — measurement on 2026-08-28 over 2,378 explosions: 82-96%
        # of true explosions reach half their peak on their first day (the bigger
        # the explosion, the higher the share), and the missed ones (345) had a
        # median of only +26% available within 24h — marginal hunting, so the
        # label separates "catchable" from "a late peak".
        "is_explosive": None,
        # (fv15) Strength-announcement snare: minutes until the first close
        # ≥ +20% — **a candidate-dropping filter only, never an entry rule**
        # (owner decision 2026-08-28 after measuring: every entry after +20%
        # is a net loss of -1% to -11% because you buy at a higher price and
        # keep only a small part of the rise plus all of the fall). The only
        # valid use: a coin that has not reached +20% within ~4 hours is
        # unlikely to explode (median 259 min for exploders vs 816 min for the
        # rest) — drop it from watch.
        "time_to_plus20_min": None,
    }

    # Entry candle: the first candle at/after the moment, within a maximum deadline.
    # A corrupted close is not a valid entry price (a close of 12,052.5 was seen
    # between two closes of ~0.0004), so we skip it to the first clean close —
    # still within the same deadline condition.
    entry_bar = next(
        (b for b in bars if b["ts"] >= entry_ts and not b.get("c_suspect")), None
    )
    use_admission_price = admission_price_usd is not None
    entry_too_late = entry_bar is None or (
        entry_bar["ts"] - entry_ts > config.LABEL_ENTRY_MAX_LAG_SECONDS
    )
    if not use_admission_price and entry_too_late:
        out["status"] = "no_entry"
        return out
    entry_px = float(admission_price_usd) if use_admission_price else float(entry_bar["c"])
    if not math.isfinite(entry_px) or entry_px <= 0:
        # A zero/non-finite price makes every ratio infinite — we fabricate nothing.
        out["status"] = "no_entry"
        return out
    out["entry_px"] = entry_px
    out["entry_lag_s"] = 0 if use_admission_price else int(entry_bar["ts"] - entry_ts)

    # The window: strictly after the entry candle, up to the end of the 48 hours.
    entry_bar_ts = entry_ts if use_admission_price else entry_bar["ts"]
    window = [b for b in bars if entry_bar_ts < b["ts"] <= window_end]
    out["candles_48h"] = len(window)
    if not window:
        out["status"] = "no_bars"
        return out

    # Impossible wicks from upstream are excluded from peak/trough only — the
    # candle is neither repaired nor deleted (its o/c are sound and used).
    # Without this exclusion, a single wick (h = 119 million times its close)
    # would push max_gain into billions of percent.
    hi_ok = [b for b in window if not b.get("h_suspect")]
    lo_ok = [b for b in window if not b.get("l_suspect")]
    close_ok = [b for b in window if not b.get("c_suspect")]
    out["suspect_bars"] = sum(
        1 for b in window
        if b.get("h_suspect") or b.get("l_suspect") or b.get("c_suspect")
    )

    for h in _GAIN_WINDOWS:
        sub = [b for b in hi_ok if b["ts"] <= entry_ts + h * 3600]
        if sub:
            out[f"max_gain_{h}h"] = max(float(b["h"]) for b in sub) / entry_px - 1

    if lo_ok:
        out["max_drawdown_48h"] = min(float(b["l"]) for b in lo_ok) / entry_px - 1
    if hi_ok:
        peak_bar = max(hi_ok, key=lambda b: float(b["h"]))
        out["time_to_peak_h"] = (peak_bar["ts"] - entry_ts) / 3600
    # Final return from the last **clean** close; series freshness from the last
    # observed candle (a candle's presence is proof of life even if its close
    # is corrupted).
    last_bar = window[-1]
    if close_ok:
        out["final_return_48h"] = float(close_ok[-1]["c"]) / entry_px - 1
        out["is_rug"] = 1 if out["final_return_48h"] <= config.LABEL_RUG_THRESHOLD else 0

    # (fv15) The two new labels — from the same candles computed above, at no
    # cost: explosive uses the clean highs (hi_ok) rather than closes, and the
    # snare the opposite. ⚠️ time_to_plus20_min is never an entry rule (owner
    # measurement 2026-08-28): entering after +20% is a net loss in every
    # window — its only use is a candidate-dropping filter (a coin that has
    # not reached +20% within ~4h is unlikely to explode).
    peak_48 = out["max_gain_48h"]
    if peak_48 is not None and out["max_gain_24h"] is not None:
        out["is_explosive"] = 1 if (
            peak_48 >= config.EXPLOSIVE_MIN_PEAK
            and out["max_gain_24h"] >= config.EXPLOSIVE_HALF_AT_24H
        ) else 0
    for b in close_ok:
        if float(b["c"]) >= entry_px * (1.0 + config.PLUS20_THRESHOLD):
            out["time_to_plus20_min"] = (b["ts"] - entry_ts) / 60
            break
    out["last_bar_lag_h"] = (window_end - last_bar["ts"]) / 3600
    out["bars_truncated"] = 1 if out["last_bar_lag_h"] > 1.0 else 0
    required = ("final_return_48h", "max_gain_24h", "is_rug")
    out["status"] = (
        "ok" if all(out[field] is not None for field in required) else "incomplete"
    )
    return out


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def token_age_days(
    created_at: Any, now_iso: str, observed_at: Any = None,
) -> float | None:
    if observed_at is not None:
        observed = epoch_of(observed_at)
        now = epoch_of(now_iso)
        if observed is None or now is None or observed > now:
            return None
    created = epoch_of(created_at)
    now = epoch_of(now_iso)
    if created is None or now is None:
        return None
    age = (now - created) / 86400.0
    return age if age >= 0 else None


def label_pending(
    db: RecorderDB, now_epoch: int, batch: int | None = None
) -> dict[str, int]:
    """Labels everything whose window has matured and is still unlabeled —
    signals, then watch entries, then retroactive activity events (only those
    with candles — see activities_pending_label).

    Incremental and idempotent: every row is labeled exactly once, forever; a
    restart picks up where it stopped. The immature are skipped silently and
    picked up in a later cycle.
    """
    batch = batch or config.LABEL_BATCH
    mature_before = (
        now_epoch - config.LABEL_WINDOW_HOURS * 3600 - config.LABEL_MARGIN_SECONDS
    )
    stats = {"signals": 0, "watches": 0, "activities": 0,
             "ok": 0, "no_entry": 0, "no_bars": 0, "incomplete": 0}
    labeled_at = utcnow_iso()

    for s in db.signals_pending_label(mature_before, batch):
        entry = s["entry_epoch"]
        bars = db.bars_for(
            s["token_address"], str(s["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(bars, entry)
        gap_ok = (
            s["prev_ts"] is None
            or entry - _epoch(s["prev_ts"]) >= config.LABEL_INDEPENDENCE_GAP_SECONDS
        )
        db.insert_outcome({
            "kind": "signal", "key": s["id"],
            "token_address": s["token_address"],
            "network_id": str(s["network_id"] or ""),
            "signal_type": s["signal_type"], "is_control": 0,
            "is_independent": 1 if gap_ok else 0,
            "entry_ts": entry,
            "split": assign_split(s["token_address"]),
            "labeled_at": labeled_at, "design_version": 1,
            "analysis_eligible": 0,
            "exclusion_reason": "signal_outcome_not_phase1_watch",
            **labels,
        })
        stats["signals"] += 1
        stats[labels["status"]] += 1

    for w in db.watches_pending_label(mature_before, batch):
        entry = w["entry_epoch"]
        bars = db.bars_for(
            w["token_address"], str(w["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(
            bars, entry, admission_price_usd=w.get("admission_price_usd")
        )
        age_at_entry = token_age_days(
            w.get("token_created_at"), _iso_from_epoch(w["entry_epoch"]),
            w.get("token_created_at_observed_at"),
        )
        gate_applies = (
            w["design_version"] >= 3
            and _epoch(w["first_seen_at"]) >= _epoch(config.AGE_GATE_ENABLED_AT)
        )
        age_observed_at_entry = (
            not gate_applies
            or (
                epoch_of(w.get("token_created_at_observed_at")) is not None
                and epoch_of(w.get("token_created_at_observed_at")) <= entry
            )
        )
        age_ok = (
            True if not gate_applies or not config.MIN_TOKEN_AGE_DAYS
            else (
                age_observed_at_entry
                and age_at_entry is not None
                and age_at_entry >= config.MIN_TOKEN_AGE_DAYS
            )
        )
        analysis_eligible = (
            w["design_version"] >= 3
            and labels["status"] != "incomplete"
            and age_ok
        )
        db.insert_outcome({
            "kind": "watch", "key": w["key"],
            "token_address": w["token_address"],
            "network_id": str(w["network_id"] or ""),
            "signal_type": w["source"], "is_control": w["is_control"],
            "is_independent": None,  # the independence concept applies to consecutive signals
            "entry_ts": entry,
            "split": assign_split(w["token_address"]),
            "labeled_at": labeled_at, "design_version": w["design_version"],
            "analysis_eligible": 1 if analysis_eligible else 0,
            "exclusion_reason": (
                ("age_gate_at_entry" if (
                    age_observed_at_entry and age_at_entry is not None
                ) else "age_unknown_at_entry")
                if gate_applies and not age_ok else (
                    None if analysis_eligible else (
                        "incomplete_metrics" if labels["status"] == "incomplete"
                        else "superseded_comparison_design"
                    )
                )
            ),
            **labels,
        })
        stats["watches"] += 1
        stats[labels["status"]] += 1

    # Retroactive activity events (backfill_activity): same computation and same
    # independence flag, but is_control=0 always — no retroactive control group
    # is possible (we cannot know the historically unreferenced universe), and
    # this is a documented constraint on any control comparison that includes them.
    for a in db.activities_pending_label(mature_before, batch):
        entry = a["entry_epoch"]
        bars = db.bars_for(
            a["token_address"], str(a["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(bars, entry)
        gap_ok = (
            a["prev_ts"] is None
            or entry - _epoch(a["prev_ts"]) >= config.LABEL_INDEPENDENCE_GAP_SECONDS
        )
        db.insert_outcome({
            "kind": "activity", "key": a["id"],
            "token_address": a["token_address"],
            "network_id": str(a["network_id"] or ""),
            "signal_type": a["event_type"], "is_control": 0,
            "is_independent": 1 if gap_ok else 0,
            "entry_ts": entry,
            "split": assign_split(a["token_address"]),
            "labeled_at": labeled_at, **labels,
        })
        stats["activities"] += 1
        stats[labels["status"]] += 1

    return stats
