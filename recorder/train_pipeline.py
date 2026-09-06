"""Training pipeline: the explosive-coins prediction model (fv15).

**Methodology change 2026-08-28 (owner decision after measurement)**: the old target
was `win_trade` from a TP+20%/SL−30% simulation — a measurement of 14,870 rows proved
that strategy marginal (+0.14%/trade, eaten by fees) because it clips the winners'
wings (16% of signals climb +100% while TP exits at +20) and lets the losses walk.
The new method: **predict the catchable explosion** directly — the `is_explosive`
label (peak ≥2x with half of it within 24h), born from that same measurement, plus
`time_to_plus20_min` (a strength-announcement hook) as an analysis variable and a
filter that drops candidates — **never an entry rule** (owner measurement
2026-08-28: entering after +20% is a net loser in every window, -1% to -11% versus
+0.74% at the signal).

Evaluation keeps its three safety legs: time-ordered walk-forward, AUC on coins never
seen during training, and a per-coin bootstrap — but the **economic metric** is now
"of the top 20% by the model, how many are explosions" instead of the mean net_return
of an abandoned simulation.

Usage:
    py train_pipeline.py                      # the explosive target (the new default)
    py train_pipeline.py --target explosive   # explicit (same thing)
    py train_pipeline.py --target win_trade   # the old target — reference only
    py train_pipeline.py --min-train-days 5
"""

import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Deliberately after the silencing: importing db/config drags in warnings from packages that are none of our business.
import config  # noqa: E402
from db import RecorderDB  # noqa: E402

FORBIDDEN = {
    "key", "token_address", "network_id", "entry_ts", "asset_class", "split",
    "kind", "status", "is_independent", "built_at", "is_live", "feature_version",
    "suspect_bars", "final_return_48h", "max_gain_1h", "max_gain_4h",
    "max_gain_24h", "max_gain_48h", "max_drawdown_48h", "time_to_peak_h",
    "is_rug", "net_return", "win_trade", "up20_48h", "up100", "up200",
    "up300", "up500", "up1000", "up2000",
    # (fv15) two new labels — pure future, never to be touched as features.
    "is_explosive", "time_to_plus20_min",
}

# Approved targets: explosive is the new default; the old one is kept for reference.
TARGETS = ("explosive", "win_trade")


DEFAULT_PARAMS = {"lr": 0.05, "depth": 3, "leaf": 40, "l2": 1.0, "iters": 300}
PARAM_GRID = [
    {"lr": 0.03, "depth": 2, "leaf": 80, "l2": 2.0, "iters": 400},
    {"lr": 0.05, "depth": 2, "leaf": 60, "l2": 1.0, "iters": 300},
    {"lr": 0.05, "depth": 3, "leaf": 40, "l2": 1.0, "iters": 300},
]


def load_frame():
    """The clean frame: the main set + the explosion label from outcomes.

    The `is_explosive` label is stamped in `outcomes` (labeling/backfill 2026-08-28)
    — no trade simulation happens here at all: the target is a measured peak/speed,
    not an exit strategy. `model_training_rows` already carries both columns (fv15
    migration), but rows built before the new labeling hold NULL — so we replace them
    from outcomes, the same trusted source, and restrict the frame to judged rows.
    """
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    frame = pd.read_sql_query(
        """SELECT t.*,
                  o.is_explosive     AS explosive_label,
                  o.time_to_plus20_min AS hook_label
             FROM model_training_rows t
             JOIN outcomes o ON o.kind = t.kind AND o.key = t.key
            WHERE t.suspect_bars = 0
              AND o.is_explosive IS NOT NULL""",
        db._conn,
    )
    db.close()
    # The two local t columns (fv15) are replaced with trusted outcome values:
    # one source of truth, and any row built before the labeling equals its row in outcomes.
    frame = frame.drop(columns=["is_explosive", "time_to_plus20_min"],
                       errors="ignore")
    frame = frame.rename(columns={
        "explosive_label": "is_explosive",
        "hook_label": "time_to_plus20_min",
    })
    frame["day"] = frame["entry_ts"] // 86400
    return frame.sort_values("entry_ts").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="explosive",
                        choices=list(TARGETS),
                        help="explosive (new): peak ≥2x with half of it within 24h · "
                             "win_trade (old): TP/SL simulation — reference only")
    parser.add_argument("--min-train-days", type=int, default=4,
                        help="number of training days before the first test fold")
    parser.add_argument("--val-folds", type=int, default=1,
                        help="how many leading folds to use for parameter selection (val)")
    args = parser.parse_args()

    frame = load_frame()
    target = "is_explosive" if args.target == "explosive" else "win_trade"

    if args.target == "win_trade":
        # The old target — kept available strictly for historical comparison.
        print("⚠️  win_trade is an abandoned target (measurement 2026-08-28: +0.14%/trade) — "
              "reference run only.")
        from exit_sim import ExitRule, simulate_trade

        db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
        rule = ExitRule(0.20, 0.30, None, 24, 0.02)
        sims = []
        for r in frame.itertuples(index=False):
            bars = db.bars_for(
                r.token_address, str(r.network_id or ""),
                int(r.entry_ts), int(r.entry_ts) + 48 * 3600,
            )
            s = simulate_trade(bars, int(r.entry_ts), rule) if bars else None
            sims.append(s["net_return"] if s else np.nan)
        db.close()
        frame["net_return"] = sims
        frame["win_trade"] = (frame["net_return"] > 0).astype(float)

    feat_cols = [c for c in frame.columns if c not in FORBIDDEN]
    if target in feat_cols:
        feat_cols.remove(target)
    for c in ("max_gain_48h", "final_return_48h", "is_explosive",
              "time_to_plus20_min"):
        assert c not in feat_cols, f"leak: {c} is among the features!"
    # Low-cardinality text columns → numeric
    for c in feat_cols:
        if isinstance(frame[c].dtype, pd.StringDtype) or frame[c].dtype == object:
            frame[c] = pd.factorize(frame[c], use_na_sentinel=True)[0].astype(float)
    print(f"total: {len(frame)} · features: {len(feat_cols)} · target: {target} "
          f"· positives: {frame[target].mean():.1%}")

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    days = sorted(frame["day"].unique())
    test_days = days[args.min_train_days:]
    val_days = test_days[: args.val_folds]
    test_days = test_days[args.val_folds:]
    print(f"days: {len(days)} · val: {val_days} · test: {test_days}")

    def fit(tr, params):
        clf = HistGradientBoostingClassifier(
            learning_rate=params["lr"], max_depth=params["depth"],
            min_samples_leaf=params["leaf"], l2_regularization=params["l2"],
            max_iter=params["iters"], random_state=0,
        )
        clf.fit(tr[feat_cols], tr[target])
        return clf

    # Parameter selection on val only
    best_params, best_auc = DEFAULT_PARAMS, -1.0
    if val_days:
        tr = frame[frame["day"] < val_days[0]]
        te = frame[frame["day"] == val_days[0]]
        for p in PARAM_GRID:
            clf = fit(tr, p)
            auc = roc_auc_score(te[target], clf.predict_proba(te[feat_cols])[:, 1])
            if auc > best_auc:
                best_auc, best_params = auc, p
        print(f"val params: {best_params} (AUC={best_auc:.3f})")

    # walk-forward test
    print("\n=== time-ordered test ===")
    print(f"{'day':<8}{'n':>5}{'AUC all':>9}{'AUC new':>10}"
          f"{'top 20%: explosive rate':>22}")
    all_p, all_te = [], []
    for d in test_days:
        tr = frame[frame["day"] < d]
        te = frame[frame["day"] == d]
        clf = fit(tr, best_params)
        p = clf.predict_proba(te[feat_cols])[:, 1]
        auc = roc_auc_score(te[target], p)
        known = set(tr["token_address"])
        m_new = ~te["token_address"].isin(known)
        auc_new = np.nan
        if m_new.sum() >= 10:
            auc_new = roc_auc_score(te.loc[m_new, target], p[m_new.values])
        # The new economic metric: in the top 20% by the model, how many explosions
        # did we catch versus the day's rate? (lift) — that is the real operational question.
        n = max(1, int(len(te) * 0.20))
        top = te.iloc[np.argsort(p)[::-1][:n]]
        lift = top[target].mean() / max(te[target].mean(), 1e-9)
        print(f"{d:<8}{len(te):>5}{auc:>9.3f}{auc_new:>10.3f}"
              f"{top[target].mean():>13.1%} (×{lift:.1f})")
        all_p.append(p)
        all_te.append(te)

    if not all_te:
        print("no test days — widen --min-train-days or wait for more data.")
        return
    P = np.concatenate(all_p)
    TE = pd.concat(all_te)
    auc_all = roc_auc_score(TE[target], P)
    known_all = set(frame.loc[frame["day"] < test_days[0], "token_address"])
    m_new_all = ~TE["token_address"].isin(known_all)
    auc_new = (roc_auc_score(TE.loc[m_new_all, target], P[m_new_all.values])
               if m_new_all.sum() >= 10 else np.nan)
    # The overall economic bottom line: top 20% across all test days
    n_all = max(1, int(len(TE) * 0.20))
    top_all = TE.iloc[np.argsort(P)[::-1][:n_all]]
    print(f"\nTotal (n={len(TE)}): AUC all={auc_all:.3f} · "
          f"AUC new coins={auc_new:.3f} (n={int(m_new_all.sum())}) · "
          f"baseline={TE[target].mean():.1%}")
    print(f"top 20% by the model: {top_all[target].mean():.1%} explosions "
          f"(lift ×{top_all[target].mean() / max(TE[target].mean(), 1e-9):.1f})")

    # per-coin bootstrap on the new-coins AUC
    if m_new_all.sum() >= 10:
        rng = np.random.default_rng(0)
        b = []
        for _ in range(1000):
            toks = TE.loc[m_new_all, "token_address"].unique()
            samp = rng.choice(toks, size=len(toks), replace=True)
            keep = m_new_all & TE["token_address"].isin(samp)
            if keep.sum() < 10 or TE.loc[keep, target].nunique() < 2:
                continue
            b.append(roc_auc_score(TE.loc[keep, target], P[keep.values]))
        b = np.asarray(b)
        print(f"AUC new coins: CI 95%=[{np.percentile(b, 2.5):.3f}, "
              f"{np.percentile(b, 97.5):.3f}] · P(edge)={(b > 0.5).mean():.1%}")
    if np.isnan(auc_new):
        print("Bottom line: not enough new coins to evaluate.")
    elif auc_new < 0.55:
        print("Bottom line: no advantage that generalizes to new coins — "
              "the model cannot be relied on for discovery.")
    else:
        print("Bottom line: a visible advantage on new coins — evaluate the "
              "economic size (lift) before relying on it.")


if __name__ == "__main__":
    main()
