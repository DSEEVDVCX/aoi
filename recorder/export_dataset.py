"""Export an external analysis bundle: everything an outside analyst needs to
propose a better training method.

Why a bundle and not a single table: the target we train on is not a column in
the database but the outcome of a trade simulation
(`exit_sim.simulate_trade`) that walks the bars. Whoever holds the rows
without the bars cannot rebuild the target, and whoever holds the bars without
the entry price does not know where to measure from. All three together or
nothing.

Read-only (`mode=ro`) inside a single transaction: the recorder and the
labeler write every minute, and without a consistent snapshot `bars.csv`
could point at rows that are not in `features.csv`.

Usage:
    py export_dataset.py                      # to C:\\Users\\rr\\Desktop\\data
    py export_dataset.py --out D:\\somewhere
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import UTC

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_OUT = r"C:\Users\rr\Desktop\data"
BAR_RESOLUTION = "5"          # the only complete resolution; 1D over 48h is two bars, useless
WINDOW_H = 48                 # outcome window

# Code files that describe the current method. No `config.py`: it holds paths
# and connection settings the analyst has no business with.
CODE_FILES = (
    "features.py",            # the point-in-time law + the 147-feature computation
    "exit_sim.py",            # the real target definition (trade simulation)
    "train_pipeline.py",      # current training and evaluation
    "build_training_rows.py", # how a row became a row
    "labeler.py",             # how an outcome became an outcome
    "schema.sql",             # the full shape, with its comments
)


def _log(msg: str) -> None:
    print(msg, flush=True)


# The bundle's guide. Rules live here and counts live in MANIFEST.json — so
# this text never goes stale between exports. Written on every run so no
# bundle ships without its rules.
README_TEXT = """# Token-signal dataset — analysis bundle

You are asked one question: **what is the best way to train a model on this data,
and is a generalizable edge present at all?** Everything needed to answer it is here,
including the code that produced it (`code/`) and the current training method
(`code/train_pipeline.py`) for you to critique or replace.

Live row counts, file sizes and distributions for THIS export: **`MANIFEST.json`**.
Column roles: **`columns.json`**. Per-feature fill rates: **`feature_coverage.csv`**.

## Files

| file | what it is |
|---|---|
| `features.csv` | one row per signal at its decision moment. The inputs. |
| `outcomes.csv` | same keys: entry price, bar-quality flags, realized outcome columns. |
| `bars.csv` | 5-minute OHLCV inside each row's 48h window. Needed to rebuild the target. |
| `control_features.csv` | same features for tokens watched **without** a signal — the negative class. |
| `control_group.csv` | watch metadata + outcome for those control tokens. |
| `feature_coverage.csv` | fill rate of every feature. Read before calling a feature dead. |
| `columns.json` | which columns are features, which are targets, which must never be inputs. |
| `code/` | the real feature builder, target simulator, labeler, schema and current pipeline. |

Join key across all files: `(kind, key)`. `key` is
`token_address:network_id:entry_ts`-style and unique per row.

## Rule 1 — the point-in-time law (the one that matters most)

Every value in `features.csv` was computed from data recorded **at or before**
`entry_ts`. That is enforced in code (`code/features.py`) with `t0` bound into every
`WHERE` clause. It is the reason this dataset is worth anything.

So: **never** build a feature from `outcomes.csv`, from `bars.csv` at `ts > entry_ts`,
or from any aggregate over the whole token's history. If you engineer new features,
they must obey the same law or your score is fiction.

## Rule 2 — the target is not a column

There is no ready-made label. The real target is the outcome of a simulated trade,
computed by `code/exit_sim.py::simulate_trade` walking `bars.csv` forward from
`entry_ts` under `ExitRule(take_profit=0.20, stop_loss=0.30, trail=None,
timeout_h=24, cost=0.02)` → `net_return`, and `win_trade = net_return > 0`.

This matters because the pre-computed columns in `outcomes.csv` (`max_gain_48h`,
`max_drawdown_48h`, …) give **magnitudes without order**. They cannot tell you whether
the +20% came before or after the −30%. In this data **27.4% of rows have both** a
≥20% peak and a ≤−30% drawdown, so a label built from those columns alone is wrong for
about a quarter of the set. Only the bars have the ordering. Rebuild the target from
bars; that is why `bars.csv` is in this bundle.

Also: peaks in those columns come from wicks. One live row shows a claimed +28% peak
while no 5-minute *close* exceeded +12.7%. A wick you could not have sold into is not
a return.

## Rule 3 — absent is not zero

A `NULL`/empty field means *not measured at that moment*, never *measured as zero*.
`fillna(0)` silently converts "we did not know" into "there was none", which is a
different and false statement — and the model will learn the collection schedule
instead of the market. Use a learner with native missing-value support (the current
pipeline uses `HistGradientBoostingClassifier`, which handles NaN natively), or add an
explicit `_was_missing` indicator column. Do not impute with 0.

## Rule 4 — do not re-split randomly

`split` (`train`/`val`/`test`) is a **token-hash** split: every row of a token lands in
the same fold. A random row-level split leaks, because the same token appears many
times — often minutes apart. Respect the given split, or split by token *and* by time.

The current pipeline goes further and evaluates walk-forward by day, reporting AUC
restricted to tokens never seen in training (`auc_new`). That number is the honest one.
An AUC computed over all rows is inflated by token repetition.

## Rule 5 — `is_independent` is the decisive cut

69% of signals fire within 5 minutes of another signal on the same token. Those are
near-duplicates of one event. `is_independent = 1` marks signals separated by at least
30 minutes. The main training set therefore is:

```sql
kind='signal' AND is_live=1 AND is_independent=1 AND asset_class='meme' AND status='ok'
```

That is a small fraction of `features.csv` — see `main_training_set_rows` in
`MANIFEST.json`. Count *that*, not the file's line count, when you reason about
statistical power. `is_live=1` is also mandatory: rows from the live bot only, no
backfilled history.

## Rule 6 — `bars_truncated` is a signal, not missing data

`bars_truncated = 1` means the price series stopped inside the 48h window. That is
usually the token dying — which is exactly the outcome you want to predict. Dropping
those rows deletes the worst cases and inflates every metric.

Distinguish it from `suspect_bars` / `h_suspect` / `l_suspect` / `c_suspect`, which
flag upstream distortion. Raw values are never repaired in this dataset (by design);
the flags let you exclude a distorted wick from a computation without losing the row.
The current pipeline trains on `suspect_bars = 0` only.

## Known data gaps — read before declaring a feature useless

`feature_coverage.csv` gives the fill rate of all features in the main training set.
Some are near-empty, and the reason is **collection start date, not a bug**: labeling
lags collection by 48h, so a source switched on recently has almost no coverage among
already-labeled rows. Specifically, as measured 2026-08-12:

- **`flow_*` — 0% at `t0`.** The `token_flow` table was created 2026-08-10, after
  nearly every labeled signal. Self-healing with time; not repairable retroactively.
- **holders snapshots — 9.8% at `t0`.** Capacity: 6 tokens/minute against ~190 watched
  meant a 31.7-minute sweep versus a 15-minute target, so many tokens fired their
  signal before their first-ever snapshot.
- **social — present for 93.4% but fresh within 15 min for only 32.3%.** Same cause:
  4 tokens/minute → a 47.5-minute sweep.

The last two were fixed on 2026-08-12 (holders 6→13/min, social 4→7/min), so coverage
improves going forward but **cannot be back-filled** — the past was never measured.
Treat low-coverage features as "too early to judge", not "no signal". Judge them again
on a later export.

## Two holder populations — do not conflate them

`chain_holder_count` / `chain_top10_pct` describe **the whole token on-chain**;
`platform_holders` and everything else prefixed `platform_` describes **fomo.family
users only**. Measured: on-chain averages 75,121 holders against 1,970 platform users,
and platform users are 0.0%–2.5% of the chain. So `chain_top10_pct` is the ownership
concentration measure; nothing derived from the platform side is. `platform_penetration`
is deliberately the ratio of the two — high means a crowd-driven move that can reverse
when the crowd exits.

`chain_holders_delta_1h` (with `_growth_1h`) is the change in on-chain holders, computed
from two of our snapshots because the upstream exposes no deltas. **Read
`chain_holders_span_min` alongside it**: snapshot cadence is a rigid 25 minutes, so the
"1 hour" comparison actually spans a measured median of 75 minutes, and spans above 100
minutes are NULL rather than misleading. There is no 5-minute equivalent and there
cannot be — 0% of measured snapshot gaps are ≤5 min.

## The current method, for you to beat

`code/train_pipeline.py`: `HistGradientBoostingClassifier`, small 3-config grid picked
on a validation day, walk-forward by day, AUC reported both overall and on unseen
tokens, then a 1000-iteration bootstrap **resampled by token** for a confidence
interval. It asserts that no derived-target column reached the feature matrix, and
prints an explicit "no generalizable edge" verdict when `auc_new < 0.55`.

Judge that design and tell us what to change: target definition, class balance,
feature selection, model family, evaluation protocol, or the conclusion that a
tradeable edge does or does not exist here.

## Re-exporting

From the recorder directory:

```
py export_dataset.py                 # rewrites this folder in place
py export_dataset.py --out D:\other  # somewhere else
```

Read-only on the database, takes well under a minute, safe to run while the recorder
and labeler are running. Every run overwrites the files, so numbers in `MANIFEST.json`
always match the CSVs beside it.
"""


def _writer(path: str):
    fh = open(path, "w", encoding="utf-8", newline="")  # noqa: SIM115 — a factory returning the handle; the caller closes it
    # Explicit lineterminator: without it Windows writes \r\n, bloating the
    # file and confusing some tools' readers. None values are written as empty
    # fields automatically — which is what we want: absent is not zero.
    return fh, csv.writer(fh, lineterminator="\n")


def export_table(con, out_dir: str, name: str, sql: str, params=()) -> dict:
    """Export a query's result to CSV and return row count and size."""
    path = os.path.join(out_dir, name)
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    fh, w = _writer(path)
    n = 0
    try:
        w.writerow(cols)
        while True:
            chunk = cur.fetchmany(5000)
            if not chunk:
                break
            w.writerows(chunk)
            n += len(chunk)
    finally:
        fh.close()
    size = os.path.getsize(path)
    _log(f"   {name:<22} {n:>9,} rows · {size/1024/1024:.1f} MB")
    return {"file": name, "rows": n, "bytes": size, "columns": cols}


def export_bars(con, out_dir: str, windows: dict) -> dict:
    """Bars inside the 48-hour windows only.

    One query per token over [oldest entry, newest entry + 48h], then filtering
    in Python down to the actual windows. The alternative — one query per row —
    is tens of thousands of queries; the other alternative — a correlated
    EXISTS over 1.4 million bars — is orders of magnitude slower.
    """
    path = os.path.join(out_dir, "bars.csv")
    cols = ["token_address", "network_id", "resolution", "ts", "o", "h", "l", "c",
            "v", "h_suspect", "l_suspect", "c_suspect"]
    fh, w = _writer(path)
    n = 0
    tokens_with_bars = 0
    try:
        w.writerow(cols)
        for tok, spans in windows.items():
            lo = min(a for a, _ in spans)
            hi = max(b for _, b in spans)
            rows = con.execute(
                f"""SELECT {','.join(cols)} FROM token_bars
                     WHERE token_address = ? AND resolution = ?
                       AND ts BETWEEN ? AND ?
                     ORDER BY ts""",
                (tok, BAR_RESOLUTION, lo, hi),
            ).fetchall()
            keep = [r for r in rows
                    if any(a <= r[3] <= b for a, b in spans)]
            if keep:
                w.writerows(keep)
                n += len(keep)
                tokens_with_bars += 1
    finally:
        fh.close()
    size = os.path.getsize(path)
    _log(f"   {'bars.csv':<22} {n:>9,} bars · {size/1024/1024:.1f} MB "
         f"· {tokens_with_bars:,} tokens")
    return {"file": "bars.csv", "rows": n, "bytes": size, "columns": cols,
            "resolution": BAR_RESOLUTION, "tokens_with_bars": tokens_with_bars}


def main() -> None:
    import config
    import features

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument(
        "--since", default=None, metavar="YYYY-MM-DD",
        help="Focused export: only signals from this date onward (entry_ts). "
             "Used to gather recent feature families (onchain/flow) whose "
             "coverage is thin before their collection start date — absent "
             "stays NULL, not zero, in both cases.",
    )
    args = ap.parse_args()
    since_cut: int | None = None
    if args.since:
        from datetime import datetime
        dt = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
        since_cut = int(dt.timestamp())
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "code"), exist_ok=True)

    started = time.time()
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True,
                          isolation_level=None)
    con.execute("PRAGMA busy_timeout = 30000")
    # One consistent snapshot for all files (WAL allows a reader that does not
    # block the writers).
    con.execute("BEGIN")
    manifest: dict = {"feature_version": features.FEATURE_VERSION,
                      "window_hours": WINDOW_H, "files": []}
    live_where = "is_live = 1"
    if since_cut is not None:
        # The cutoff applies to entry_ts (the decision moment), not
        # recorded_at — otherwise old decision rows slip in through late
        # archiving.
        live_where += f" AND entry_ts >= {since_cut}"
        manifest["since"] = args.since
    try:
        _log("1) Training rows (is_live=1 only — live data only)")
        skipped = con.execute(
            "SELECT COUNT(*) FROM training_rows WHERE COALESCE(is_live,0) <> 1"
        ).fetchone()[0]
        manifest["rows_excluded_not_live"] = skipped
        if skipped:
            _log(f"   Excluded {skipped:,} non-live rows (hard rule: live-only training)")
        manifest["files"].append(export_table(
            con, out_dir, "features.csv",
            f"SELECT * FROM training_rows WHERE {live_where} ORDER BY entry_ts"))

        _log("\n2) Outcomes (entry price and bar quality — not all in features.csv)")
        manifest["files"].append(export_table(
            con, out_dir, "outcomes.csv",
            f"""SELECT o.* FROM outcomes o
                WHERE EXISTS (SELECT 1 FROM training_rows t
                               WHERE t.kind = o.kind AND t.key = o.key
                                 AND t.is_live = 1{'' if since_cut is None else f' AND t.entry_ts >= {since_cut}'})
                ORDER BY o.entry_ts"""))

        _log("\n3) Bars inside the windows")
        windows: dict = {}
        # Signals + control. We exclude kind='activity' alone: it is
        # retroactive, and training is live-only.
        for tok, ts in con.execute(
            f"""SELECT token_address, entry_ts FROM training_rows
                WHERE kind IN ('signal', 'watch') AND entry_ts IS NOT NULL
                  AND {live_where}"""
        ):
            windows.setdefault(tok, []).append((ts, ts + WINDOW_H * 3600))
        _log(f"   {len(windows):,} tokens · {sum(len(v) for v in windows.values()):,} windows")
        manifest["files"].append(export_bars(con, out_dir, windows))

        _log("\n4) Control group (kind='watch' — for comparison, not for training)")
        manifest["files"].append(export_table(
            con, out_dir, "control_group.csv",
            """SELECT w.token_address, w.network_id, w.first_seen_at, w.is_control,
                      w.source, w.watch_until, w.active, w.entry_signal_id,
                      o.kind, o.key, o.entry_ts, o.entry_px,
                      o.status, o.max_gain_48h, o.max_drawdown_48h,
                      o.final_return_48h, o.is_rug, o.candles_48h, o.bars_truncated
                 FROM watchlist w
                 LEFT JOIN outcomes o
                        ON o.kind = 'watch'
                       AND o.key = w.token_address || ':' || w.network_id
                                || ':' || w.first_seen_at
                ORDER BY w.first_seen_at"""))

        # Control features in a separate file, not inside features.csv.
        # Reason: `is_live` is a signal-only flag — every watch row has
        # is_live=0, so filtering features.csv by is_live=1 (which the rule
        # requires) would drop the entire control group. But the control group
        # is the negative class; without it there is no telling whether a
        # signal means anything. Hence a second, explicit file: train from the
        # first, compare from the second.
        _log("\n4b) Control features (same 147 features, same point-in-time law)")
        manifest["files"].append(export_table(
            con, out_dir, "control_features.csv",
            f"""SELECT r.*, w.is_control, w.source AS watch_source
                 FROM training_rows r
                 LEFT JOIN watchlist w
                        ON r.key = w.token_address || ':' || w.network_id
                                || ':' || w.first_seen_at
                WHERE r.kind = 'watch' AND {live_where}
                ORDER BY r.entry_ts"""))

        _log("\n5) Reference distributions (so the analyst does not re-split randomly)")
        dist: dict = {}
        for label, sql in (
            ("by_split", "SELECT split, COUNT(*) FROM training_rows "
                          f"WHERE {live_where} AND kind='signal' GROUP BY 1"),
            ("by_kind", "SELECT kind, COUNT(*) FROM training_rows "
                        f"WHERE {live_where} GROUP BY 1"),
            ("by_status", "SELECT status, COUNT(*) FROM training_rows "
                          f"WHERE {live_where} GROUP BY 1"),
            ("by_is_independent", "SELECT is_independent, COUNT(*) FROM training_rows "
                                  f"WHERE {live_where} AND kind='signal' GROUP BY 1"),
            ("by_asset_class", "SELECT asset_class, COUNT(*) FROM training_rows "
                               f"WHERE {live_where} GROUP BY 1"),
            ("by_feature_version", "SELECT feature_version, COUNT(*) FROM training_rows "
                                   f"WHERE {live_where} GROUP BY 1"),
        ):
            dist[label] = {str(k): v for k, v in con.execute(sql)}
            _log(f"   {label:<20} {dist[label]}")
        manifest["distributions"] = dist

        n_model = con.execute(
            f"""SELECT COUNT(*) FROM training_rows
                WHERE kind='signal' AND {live_where.replace('is_live = 1', 'is_live=1')}
                  AND is_independent=1
                  AND asset_class='meme' AND status='ok'"""
        ).fetchone()[0]
        manifest["main_training_set_rows"] = n_model
        _log(f"   Main training set (by the spec's criteria) = {n_model:,}")

        _log("\n5b) Per-feature coverage (feature_coverage.csv)")
        # A third of the features are empty in the current window not because
        # of a defect but because their source started collecting after the
        # last labeled outcome (labeling lags collection by 48h). Without this
        # file the analyst pronounces a healthy feature dead.
        feat_cols = list(features.FEATURE_COLUMNS)
        sel = ", ".join(f"SUM({c} IS NOT NULL)" for c in feat_cols)
        since_clause = "" if since_cut is None else f" AND entry_ts >= {since_cut}"
        MAIN = ("kind='signal' AND is_live=1 AND is_independent=1 "
                "AND asset_class='meme' AND status='ok'" + since_clause)
        r_main = con.execute(
            f"SELECT {sel} FROM training_rows WHERE {MAIN}").fetchone()
        r_all = con.execute(
            f"SELECT {sel} FROM training_rows WHERE kind='signal' AND is_live=1{since_clause}").fetchone()
        n_all = con.execute(
            "SELECT COUNT(*) FROM training_rows "
            "WHERE kind='signal' AND is_live=1" + since_clause
        ).fetchone()[0]
        cov_path = os.path.join(out_dir, "feature_coverage.csv")
        fh, w = _writer(cov_path)
        try:
            w.writerow(["feature", "filled_main", "rows_main", "fill_rate_main",
                        "filled_all_signals", "rows_all_signals",
                        "fill_rate_all_signals", "usable_at_50pct"])
            for i, c in enumerate(feat_cols):
                fm, fa = r_main[i] or 0, r_all[i] or 0
                rm = fm / max(n_model, 1)
                w.writerow([c, fm, n_model, round(rm, 4), fa, n_all,
                            round(fa / max(n_all, 1), 4), int(rm >= 0.50)])
        finally:
            fh.close()
        usable = sum(1 for i in range(len(feat_cols))
                     if (r_main[i] or 0) / max(n_model, 1) >= 0.50)
        empty = sum(1 for i in range(len(feat_cols)) if not r_main[i])
        manifest["feature_coverage"] = {
            "usable_at_50pct": usable, "empty_in_main_set": empty,
            "total_features": len(feat_cols)}
        _log(f"   {usable} usable features (≥50%) · {empty} fully empty "
             f"· of {len(feat_cols)}")
        manifest["files"].append(
            {"file": "feature_coverage.csv", "rows": len(feat_cols),
             "bytes": os.path.getsize(cov_path)})
    finally:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        con.close()

    _log("\n6) Separating features from targets (columns.json)")
    import train_pipeline
    feat = list(features.FEATURE_COLUMNS)
    all_cols = list(features.ROW_COLUMNS)
    targets = ["final_return_48h", "max_gain_1h", "max_gain_4h", "max_gain_24h",
               "max_gain_48h", "max_drawdown_48h", "time_to_peak_h", "is_rug"]
    ident = [c for c in all_cols if c not in feat and c not in targets]
    cols_doc = {
        "feature_columns": feat,
        "target_columns_never_use_as_feature": targets,
        "identifier_and_meta_columns": ident,
        "forbidden_as_feature": sorted(train_pipeline.FORBIDDEN),
        "note": ("Any column in target_columns_* or forbidden_as_feature describes "
                 "the FUTURE relative to entry_ts. Using one as an input produces a "
                 "meaningless AUC near 1.0."),
    }
    with open(os.path.join(out_dir, "columns.json"), "w", encoding="utf-8") as fh:
        json.dump(cols_doc, fh, indent=2, ensure_ascii=False)
    _log(f"   {len(feat)} features · {len(targets)} targets · {len(ident)} identifier/meta")
    manifest["counts"] = {"features": len(feat), "targets": len(targets),
                          "identifiers": len(ident)}

    _log("\n7) The code describing the current method")
    copied = []
    for name in CODE_FILES:
        src = os.path.join(HERE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out_dir, "code", name))
            copied.append(name)
            _log(f"   code/{name}")
        else:
            _log(f"   code/{name} — not found, skipped")
    manifest["code_files"] = copied
    manifest["export_seconds"] = round(time.time() - started, 1)

    # The rules that prevent wrong inference live in README_TEXT above, but the
    # owner asked that no README.md ship in the export bundle (decision
    # 2026-08-25) — so it is deleted if left over from a previous export, and
    # no new one is written.
    _log("\n8) Reading guide (README.md) — disabled at the owner's request")
    stale_readme = os.path.join(out_dir, "README.md")
    if os.path.exists(stale_readme):
        os.remove(stale_readme)
        _log("   Removed stale README.md")

    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    total = sum(f["bytes"] for f in manifest["files"])
    _log(f"\nDone in {manifest['export_seconds']}s · "
         f"{total/1024/1024:.0f} MB · {out_dir}")


if __name__ == "__main__":
    main()
