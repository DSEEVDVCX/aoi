"""أنبوب التدريب النهائي: walk-forward زمني + منع انتقال العملة + هدف من المحاكاة.

يعمل على `model_training_rows` (نظيف، بلا تكرار) ويقيّم على العملات الجديدة فقط
بالإضافة إلى كل الإشارات. يرفض الاستنتاج عند تسريب هدف مشتق في الميزات.

الاستعمال:
    py train_pipeline.py                 # تشغيل كامل
    py train_pipeline.py --target up20   # هدف لمس +20% بدل الربح الصافي
    py train_pipeline.py --min-train-days 5
"""

import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from db import RecorderDB
import config

FORBIDDEN = {
    "key", "token_address", "network_id", "entry_ts", "asset_class", "split",
    "kind", "status", "is_independent", "built_at", "is_live", "feature_version",
    "suspect_bars", "final_return_48h", "max_gain_1h", "max_gain_4h", "max_gain_24h",
    "max_gain_48h", "max_drawdown_48h", "time_to_peak_h", "is_rug", "net_return",
    "win_trade", "up20_48h", "up100", "up200", "up300", "up500", "up1000", "up2000",
}

# دون هذا الحدّ يصير نصف الإطار أصغر من أن يميّز «ثابت بحكم الحِقبة» عن «ثابت
# بالصدفة»، فنكتفي بفلتر التباين وحده.
MIN_HALF_FOR_EPOCH = 200


def prune_dead_features(frame: pd.DataFrame, feat_cols: list[str]) -> list[str]:
    """يُسقط الأعمدة التي لا تحمل معلومة، ويكشف علامات الحِقبة.

    `FORBIDDEN` قائمةُ **منعٍ** يدوية: كل عمود لا يُذكر فيها صراحةً يصير ميزةً.
    فالحقول التي ترسلها fomo بقيمة ميتة — `social_replies` و`are_top_traders`
    و`is_scam` و`platform_dev_holding` — تدخل النموذج اليوم بلا أن يقصدها أحد.
    وحذفُها بيدٍ من القائمة يحلّ اليوم ويصنع مشكلة الغد: إن بدأت المنصّة تملأ
    أحدها فلن ينتبه أحد لإعادته. القرار هنا بالتباين المقيس لا بالاسم، فيسقط
    الميت الآن ويُقبل تلقائياً يوم يحمل قيماً — والسطر المطبوع هو الإشعار.

    وفي «يوم يحمل قيماً» فخّ: عمود ثابت في كل الصفوف القديمة ومتغيّر في الجديدة
    ليس ميزة بل **ساعة** — يقرأ منه النموذج حِقبة الصفّ فيتنبّأ بمعدّل الأساس لا
    بالعملة، وAUC يرتفع بلا أفضلية حقيقية. هذا بعينه ما رُفض سابقاً في
    `config.py:43` (تسريب حِقبة)، فنكشفه هنا بدل أن ينزلق مع أول تصحيح للمنصّة
    أو مع أول مصدر جديد لم ينضج بعدُ عبر نافذة الـ48 ساعة.

    يُنادى **بعد** ترقيم الأعمدة النصّية: `factorize` يعطي NaN القيمة −1، فيبقى
    تمييز «غائب/حاضر» قائماً وهو جوهر كشف الحِقبة.
    """
    kept: list[str] = []
    dead: list[str] = []
    epoch: list[str] = []
    mid = len(frame) // 2
    for c in feat_cols:
        col = frame[c]
        if col.nunique(dropna=False) <= 1:
            dead.append(c)  # ثابت أو NULL بالكامل — لا معلومة أصلاً
            continue
        # الإطار مرتَّب زمنياً في `load_frame`، فالنصف الأول هو الماضي.
        if mid >= MIN_HALF_FOR_EPOCH:
            early, late = col.iloc[:mid], col.iloc[mid:]
            const_half = None
            if early.nunique(dropna=False) <= 1:
                const_half, other = early, late
            elif late.nunique(dropna=False) <= 1:
                const_half, other = late, early
            if const_half is not None:
                v = const_half.iloc[0]
                shared = other.isna() if pd.isna(v) else (other == v)
                # قيمة النصف الثابت لا تظهر في النصف الآخر ولو مرّة ⇒ العمود
                # يحدّد الحِقبة يقيناً لنصف الصفوف. ذلك تأريخ لا قياس.
                if not shared.any():
                    epoch.append(c)
                    continue
        kept.append(c)

    if dead:
        print(f"أُسقط {len(dead)} عموداً بلا تباين: {', '.join(sorted(dead))}")
    if epoch:
        print(f"أُسقط {len(epoch)} عموداً كعلامة حِقبة: {', '.join(sorted(epoch))}")
        print("  (قيمة نصفه الثابت غائبة تماماً عن النصف الآخر — النموذج يقرأ "
              "منه التاريخ. إن كان إشارة جديدة صحيحة فانتظر تغطيتها النصف "
              "المبكّر قبل إدخالها.)")
    return kept


DEFAULT_PARAMS = {"lr": 0.05, "depth": 3, "leaf": 40, "l2": 1.0, "iters": 300}
PARAM_GRID = [
    {"lr": 0.03, "depth": 2, "leaf": 80, "l2": 2.0, "iters": 400},
    {"lr": 0.05, "depth": 2, "leaf": 60, "l2": 1.0, "iters": 300},
    {"lr": 0.05, "depth": 3, "leaf": 40, "l2": 1.0, "iters": 300},
]


def load_frame():
    """الإطار النظيف + المحاكاة الفعلية (TP+20%/SL−30%/24س/تكلفة 2%)."""
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    frame = pd.read_sql_query(
        "SELECT * FROM model_training_rows WHERE suspect_bars = 0", db._conn
    )
    from exit_sim import ExitRule, simulate_trade

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
    frame["day"] = frame["entry_ts"] // 86400
    return frame.sort_values("entry_ts").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="win_trade", choices=["win_trade", "up20"])
    parser.add_argument("--min-train-days", type=int, default=4,
                        help="عدد أيام التدريب قبل أول fold اختبار")
    parser.add_argument("--val-folds", type=int, default=1,
                        help="كم fold أول تستخدم لاختيار المعاملات (val)")
    args = parser.parse_args()

    frame = load_frame()
    target = args.target
    if target == "up20":
        frame[target] = (frame["max_gain_48h"] >= 0.20).astype(float)

    feat_cols = [c for c in frame.columns if c not in FORBIDDEN]
    # عمود الهدف نفسه ممنوع مهما كان اسمه
    if target in feat_cols:
        feat_cols.remove(target)
    for c in ("max_gain_48h", "final_return_48h"):
        assert c not in feat_cols, f"تسريب: {c} ضمن الميزات!"
    # أعمدة نصية قليلة القيم → رقمية
    for c in feat_cols:
        if isinstance(frame[c].dtype, pd.StringDtype) or frame[c].dtype == object:
            frame[c] = pd.factorize(frame[c], use_na_sentinel=True)[0].astype(float)
    raw_n = len(feat_cols)
    feat_cols = prune_dead_features(frame, feat_cols)
    print(f"إجمالي: {len(frame)} · ميزات: {len(feat_cols)}/{raw_n} · هدف: {target}")

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    days = sorted(frame["day"].unique())
    test_days = days[args.min_train_days:]
    val_days = test_days[: args.val_folds]
    test_days = test_days[args.val_folds:]
    print(f"أيام: {len(days)} · val: {val_days} · test: {test_days}")

    def fit(tr, params):
        clf = HistGradientBoostingClassifier(
            learning_rate=params["lr"], max_depth=params["depth"],
            min_samples_leaf=params["leaf"], l2_regularization=params["l2"],
            max_iter=params["iters"], random_state=0,
        )
        clf.fit(tr[feat_cols], tr[target])
        return clf

    # اختيار المعاملات على val فقط
    best_params, best_auc = DEFAULT_PARAMS, -1.0
    if val_days:
        tr = frame[frame["day"] < val_days[0]]
        te = frame[frame["day"] == val_days[0]]
        for p in PARAM_GRID:
            clf = fit(tr, p)
            auc = roc_auc_score(te[target], clf.predict_proba(te[feat_cols])[:, 1])
            if auc > best_auc:
                best_auc, best_params = auc, p
        print(f"معاملات val: {best_params} (AUC={best_auc:.3f})")

    # اختبار walk-forward
    print("\n=== الاختبار الزمني ===")
    print(f"{'يوم':<8}{'n':>5}{'AUC الكل':>9}{'AUC جديد':>10}{'أعلى20% متوسط':>15}")
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
        n = max(1, int(len(te) * 0.20))
        avg20 = te.iloc[np.argsort(p)[::-1][:n]]["net_return"].mean()
        print(f"{d:<8}{len(te):>5}{auc:>9.3f}{auc_new:>10.3f}{avg20:>+15.2%}")
        all_p.append(p)
        all_te.append(te)

    if not all_te:
        print("لا توجد أيام اختبار — وسّع --min-train-days أو انتظر بيانات أطول.")
        return
    P = np.concatenate(all_p)
    TE = pd.concat(all_te)
    auc_all = roc_auc_score(TE[target], P)
    known_all = set(frame.loc[frame["day"] < test_days[0], "token_address"])
    m_new_all = ~TE["token_address"].isin(known_all)
    auc_new = roc_auc_score(TE.loc[m_new_all, target], P[m_new_all.values]) if m_new_all.sum() >= 10 else np.nan
    print(f"\nالمجموع (n={len(TE)}): AUC الكل={auc_all:.3f} · AUC عملات جديدة={auc_new:.3f} "
          f"(n={int(m_new_all.sum())}) · baseline={TE[target].mean():.1%}")

    # bootstrap بالعملة على AUC العملات الجديدة
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
        print(f"AUC عملات جديدة: CI 95%=[{np.percentile(b, 2.5):.3f}, "
              f"{np.percentile(b, 97.5):.3f}] · P(أفضلية)={(b > 0.5).mean():.1%}")
    if auc_new < 0.55:
        print("الخلاصة: لا أفضلية قابلة للتعميم على العملات الجديدة — النموذج لا يُعتمد للاكتشاف.")


if __name__ == "__main__":
    main()
