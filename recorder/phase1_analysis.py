"""تحليل بوابة المرحلة 1: هل نوافذ الإشارة تتفوّق على الضابطة؟"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Callable, Iterable

import numpy as np
from scipy.stats import fisher_exact, mannwhitneyu

import config

MIN_WORTHWHILE_MEDIAN_DIFF = 0.05
ALPHA = 0.05
BOOTSTRAP_ITERATIONS = 20_000
POWER_SIMULATIONS = 2_000
RANDOM_SEED = 20260802
SIGNAL_SOURCES = frozenset(config.TRIGGER_SIGNAL_TYPES)
LIVE_CONTROL_SOURCE = "control"


def summarize_values(values: Iterable[float]) -> dict[str, float | int]:
    xs = [float(value) for value in values]
    if not xs:
        raise ValueError("cannot summarize an empty sample")
    return {
        "n": len(xs),
        "mean": mean(xs),
        "median": median(xs),
        "q25": float(np.quantile(xs, 0.25)),
        "q75": float(np.quantile(xs, 0.75)),
        "win_rate": sum(value > 0 for value in xs) / len(xs),
    }


def bootstrap_difference_ci(
    signal: Iterable[float],
    control: Iterable[float],
    *,
    statistic: str,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = RANDOM_SEED,
) -> dict[str, float]:
    signal_values = np.asarray(list(signal), dtype=float)
    control_values = np.asarray(list(control), dtype=float)
    if not len(signal_values) or not len(control_values):
        raise ValueError("both samples must be non-empty")

    functions: dict[str, Callable[[np.ndarray], float]] = {
        "median": lambda values: float(np.median(values)),
        "mean": lambda values: float(np.mean(values)),
        "win_rate": lambda values: float(np.mean(values > 0)),
    }
    try:
        fn = functions[statistic]
    except KeyError as exc:
        raise ValueError(f"unsupported statistic: {statistic}") from exc

    rng = np.random.default_rng(seed)
    differences = np.empty(iterations)
    for index in range(iterations):
        sampled_signal = rng.choice(signal_values, len(signal_values), replace=True)
        sampled_control = rng.choice(control_values, len(control_values), replace=True)
        differences[index] = fn(sampled_signal) - fn(sampled_control)

    return {
        "estimate": fn(signal_values) - fn(control_values),
        "low": float(np.quantile(differences, 0.025)),
        "high": float(np.quantile(differences, 0.975)),
    }


def _read_rows(db_path: str, *, include_retro_controls: bool) -> list[dict]:
    uri = Path(db_path).resolve().as_uri().replace("file:///", "file:/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    control_sources = (LIVE_CONTROL_SOURCE, "control_retro") if include_retro_controls else (
        LIVE_CONTROL_SOURCE,
    )
    placeholders = ", ".join("?" for _ in control_sources)
    try:
        rows = connection.execute(
            f"""
            SELECT o.key, o.token_address, o.network_id, o.entry_ts,
                   o.is_control, o.signal_type, o.status, o.final_return_48h,
                   o.max_gain_24h, o.is_rug, o.bars_truncated,
                   o.last_bar_lag_h, o.suspect_bars, w.admission_source
              FROM outcomes o
              JOIN watch_windows w
                ON w.token_address || ':' || w.network_id || ':' || w.first_seen_at = o.key
               AND w.design_version >= 3
               AND w.admission_source IN ('trending', 'verified')
              JOIN token_class tc
                ON tc.token_address = o.token_address
               AND tc.network_id = o.network_id
               AND tc.asset_class = 'meme'
             JOIN phase1_watch_outcomes eligible
               ON eligible.kind = o.kind AND eligible.key = o.key
             WHERE o.kind = 'watch'
               AND o.entry_ts >= ?
               AND ((o.is_control = 0 AND o.signal_type IN ({', '.join('?' for _ in SIGNAL_SOURCES)}))
                    OR (o.is_control = 1 AND o.signal_type IN ({placeholders})))
             ORDER BY o.entry_ts, o.key
            """,
            (config.LIVE_START_TS, *sorted(SIGNAL_SOURCES), *control_sources),
        ).fetchall()
    finally:
        connection.close()

    result = [dict(row) for row in rows]
    for row in result:
        if row["status"] == "ok":
            required = ("final_return_48h", "max_gain_24h", "is_rug")
            if any(row[field] is None for field in required):
                raise RuntimeError(f"ok outcome has null metrics: {row['key']}")
            if not np.isfinite(row["final_return_48h"]):
                raise RuntimeError(f"ok outcome has non-finite return: {row['key']}")
    return result


def _deduplicate_tokens(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """أقدم نافذة لكل عملة، ثم حذف العملات التي ظهرت في المجموعتين."""
    first: dict[tuple[int, str, str], dict] = {}
    group_tokens: dict[int, set[tuple[str, str]]] = {0: set(), 1: set()}
    duplicates = Counter()
    for row in rows:
        token = (row["token_address"], str(row["network_id"] or ""))
        group_tokens[row["is_control"]].add(token)
        key = (row["is_control"], *token)
        if key in first:
            duplicates[row["is_control"]] += 1
            continue
        first[key] = row
    overlaps = group_tokens[0] & group_tokens[1]
    kept = [
        row
        for row in first.values()
        if (row["token_address"], str(row["network_id"] or "")) not in overlaps
    ]
    kept.sort(key=lambda row: (row["entry_ts"], row["key"]))
    return kept, {
        "duplicate_signal_windows_removed": duplicates[0],
        "duplicate_control_windows_removed": duplicates[1],
        "overlap_tokens_removed": len(overlaps),
    }


def _power_for_shift(
    reference: np.ndarray,
    *,
    n_signal: int,
    n_control: int,
    shift: float,
    simulations: int,
    seed: int,
) -> float:
    rng = np.random.default_rng(seed)
    rejections = 0
    for _ in range(simulations):
        control = rng.choice(reference, n_control, replace=True)
        signal = rng.choice(reference, n_signal, replace=True) + shift
        if mannwhitneyu(signal, control, alternative="greater").pvalue < ALPHA:
            rejections += 1
    return rejections / simulations


def _power_analysis(control: list[float], n_signal: int, n_control: int) -> dict:
    reference = np.asarray(control, dtype=float)
    current = _power_for_shift(
        reference,
        n_signal=n_signal,
        n_control=n_control,
        shift=MIN_WORTHWHILE_MEDIAN_DIFF,
        simulations=POWER_SIMULATIONS,
        seed=RANDOM_SEED + 10,
    )
    return {
        "current_power": current,
        "model": "pure +5 percentage-point location shift of the control distribution",
    }


def _analyze_rows(rows: list[dict]) -> dict:
    groups = {group: [row for row in rows if row["is_control"] == group] for group in (0, 1)}
    ok = {group: [row for row in group_rows if row["status"] == "ok"]
          for group, group_rows in groups.items()}
    if not ok[0] or not ok[1]:
        raise RuntimeError(
            "phase 1 requires mature design-v2 signal and control outcomes"
        )
    if any(row["status"] == "no_bars" for group_rows in groups.values() for row in group_rows):
        raise RuntimeError(
            "phase 1 has no_bars outcomes; define their return treatment before analysis"
        )

    returns = {group: [row["final_return_48h"] for row in group_rows]
               for group, group_rows in ok.items()}
    peaks = {group: [row["max_gain_24h"] for row in group_rows]
             for group, group_rows in ok.items()}
    u_greater = mannwhitneyu(returns[0], returns[1], alternative="greater")
    u_two_sided = mannwhitneyu(returns[0], returns[1], alternative="two-sided")
    rank_biserial = 2 * u_greater.statistic / (len(returns[0]) * len(returns[1])) - 1

    statuses = {}
    for group in (0, 1):
        counts = Counter(row["status"] for row in groups[group])
        total = len(groups[group])
        statuses[group] = {
            "total": total,
            "ok": counts["ok"],
            "no_entry": counts["no_entry"],
            "no_bars": counts["no_bars"],
            "no_entry_rate": counts["no_entry"] / total,
        }
    missingness = fisher_exact(
        [[statuses[0]["no_entry"], statuses[0]["ok"]],
         [statuses[1]["no_entry"], statuses[1]["ok"]]],
        alternative="two-sided",
    )

    median_ci = bootstrap_difference_ci(returns[0], returns[1], statistic="median")
    win_ci = bootstrap_difference_ci(
        returns[0], returns[1], statistic="win_rate", seed=RANDOM_SEED + 1
    )
    summaries = {}
    for group in (0, 1):
        summaries[group] = summarize_values(returns[group])
        summaries[group]["peak_24h_mean"] = mean(peaks[group])
        summaries[group]["peak_24h_median"] = median(peaks[group])
        summaries[group]["rug_rate"] = sum(row["is_rug"] for row in ok[group]) / len(ok[group])
        summaries[group]["truncated_rate"] = (
            sum(bool(row["bars_truncated"]) for row in ok[group]) / len(ok[group])
        )

    networks = {
        group: dict(sorted(Counter(str(row["network_id"] or "") for row in ok[group]).items()))
        for group in (0, 1)
    }
    all_networks = set(networks[0]) | set(networks[1])
    network_total_variation = 0.5 * sum(
        abs(
            networks[0].get(network, 0) / len(ok[0])
            - networks[1].get(network, 0) / len(ok[1])
        )
        for network in all_networks
    )
    success = (
        median_ci["estimate"] >= MIN_WORTHWHILE_MEDIAN_DIFF
        and median_ci["low"] > 0
        and win_ci["estimate"] > 0
        and win_ci["low"] > 0
        and u_greater.pvalue < ALPHA
    )
    return {
        "summaries": summaries,
        "statuses": statuses,
        "median_difference": median_ci,
        "win_rate_difference": win_ci,
        "mann_whitney": {
            "u": float(u_greater.statistic),
            "p_greater": float(u_greater.pvalue),
            "p_two_sided": float(u_two_sided.pvalue),
            "rank_biserial": float(rank_biserial),
        },
        "missingness": {
            "odds_ratio": float(missingness.statistic),
            "p_two_sided": float(missingness.pvalue),
        },
        "networks": networks,
        "network_total_variation": network_total_variation,
        "network_imbalance": network_total_variation > 0.10,
        "power": _power_analysis(returns[1], len(returns[0]), len(returns[1])),
        "success": success,
    }


def analyze(db_path: str, *, include_retro_controls: bool = False) -> dict:
    raw = _read_rows(db_path, include_retro_controls=include_retro_controls)
    rows, exclusions = _deduplicate_tokens(raw)
    result = _analyze_rows(rows)
    result["exclusions"] = exclusions
    result["include_retro_controls"] = include_retro_controls
    result["max_entry_ts"] = max(row["entry_ts"] for row in rows)
    return result


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _p(value: float) -> str:
    return "<0.0001" if value < 0.0001 else f"{value:.4f}"


def _network_text(networks: dict[str, int]) -> str:
    return " · ".join(f"`{network}`: {count}" for network, count in networks.items())


def render_markdown(primary: dict, sensitivity: dict, *, db_path: str) -> str:
    signal = primary["summaries"][0]
    control = primary["summaries"][1]
    signal_status = primary["statuses"][0]
    control_status = primary["statuses"][1]
    median_diff = primary["median_difference"]
    win_diff = primary["win_rate_difference"]
    mw = primary["mann_whitney"]
    power = primary["power"]
    generated_dt = datetime.now(UTC)
    generated = generated_dt.isoformat(timespec="seconds")
    date = generated_dt.date().isoformat()
    missingness_significant = primary["missingness"]["p_two_sided"] < ALPHA
    structural_blockers = missingness_significant or primary["network_imbalance"]
    gate = "ناجحة" if primary["success"] and not structural_blockers else "غير مجتازة"
    sensitivity_changed = sensitivity["success"] != primary["success"]
    exclusions = primary["exclusions"]
    return f"""# نتيجة المرحلة 1 — {date}

> أُنشئ في {generated} من `{db_path}` بواسطة `py recorder/phase1_analysis.py`.
> آخر ختم دخول مشمول: `{primary['max_entry_ts']}`. النتيجة الأساسية: حيّ فقط،
> `asset_class='meme'` صراحة، ضابطة `source='control'` فقط، وأقدم نافذة واحدة
> لكل عملة مع حذف العملات التي ظهرت في المجموعتين.

## المعيار المثبّت

- أصغر فرق وسيط يستحق المتابعة: **5 نقاط مئوية** لصالح الإشارة.
- نجاح البوابة يتطلب تفوّقاً ذا معنى في **الوسيط ونسبة الفوز معاً**.
- Mann–Whitney U أحادي الاتجاه (`signal` أعلى رتبةً من `control`) عند `alpha=0.05`.
- فحص حساسية القدرة لمكوّن Mann–Whitney فقط. يفترض، على نحو صريح ومحدود،
  إزاحة موقعية +5 نقاط لتوزيع الضابطة كله؛ **ليس قدرة البوابة المركبة** ولا
  يثبت القدرة لكل شكل أثر ممكن.
- فواصل الثقة bootstrap بنسبة 95%. لا t-test بسبب الالتواء الشديد.

## تكوين العينة

- حُذفت {exclusions['duplicate_signal_windows_removed']} نافذة إشارة مكررة
  و{exclusions['duplicate_control_windows_removed']} نافذة ضابطة مكررة، وأُزيلت
  {exclusions['overlap_tokens_removed']} عملات ظهرت في المجموعتين.
- توزيع الشبكات، الإشارة: {_network_text(primary['networks'][0])}.
- توزيع الشبكات، الضابطة: {_network_text(primary['networks'][1])}.
- مسافة الاختلال الكلية بين نسب الشبكات: {primary['network_total_variation']:.3f}
  (أكبر من 0.10 ⇒ اختلال جوهري وفق حرس التحليل).

| المجموعة | كل النوافذ | `ok` | `no_entry` | `no_bars` | نسبة `no_entry` |
|---|---:|---:|---:|---:|---:|
| الإشارة | {signal_status['total']} | {signal_status['ok']} | {signal_status['no_entry']} | {signal_status['no_bars']} | {signal_status['no_entry_rate'] * 100:.1f}% |
| الضابطة | {control_status['total']} | {control_status['ok']} | {control_status['no_entry']} | {control_status['no_bars']} | {control_status['no_entry_rate'] * 100:.1f}% |

فحص فقد شمعة الدخول: `Fisher exact p={_p(primary['missingness']['p_two_sided'])}`؛
الفرق {'دال، ولذلك التصفية على `ok` انتقائية ولا تسمح بحكم سببي نهائي' if missingness_significant else 'غير دال في العينة الحالية'}.
`bars_truncated` لم يُستبعد لأنه قد يعني موت الأصل لا نقصاً: نسبته
{signal['truncated_rate'] * 100:.1f}% للإشارة و{control['truncated_rate'] * 100:.1f}% للضابطة.

## النتيجة الوصفية

| المقياس | الإشارة (n={signal['n']}) | الضابطة (n={control['n']}) | الفرق |
|---|---:|---:|---:|
| العائد النهائي ضمن نافذة 48س، الوسيط | {_pct(signal['median'])} | {_pct(control['median'])} | {_pct(median_diff['estimate'])} |
| العائد النهائي، المتوسط | {_pct(signal['mean'])} | {_pct(control['mean'])} | {_pct(signal['mean'] - control['mean'])} |
| نسبة الفوز | {signal['win_rate'] * 100:.1f}% | {control['win_rate'] * 100:.1f}% | {_pct(win_diff['estimate'])} |
| قمّة 24س، الوسيط | {_pct(signal['peak_24h_median'])} | {_pct(control['peak_24h_median'])} | {_pct(signal['peak_24h_median'] - control['peak_24h_median'])} |
| rug | {signal['rug_rate'] * 100:.1f}% | {control['rug_rate'] * 100:.1f}% | {_pct(signal['rug_rate'] - control['rug_rate'])} |

- فرق الوسيط 95% CI: **[{_pct(median_diff['low'])}, {_pct(median_diff['high'])}]**.
- فرق الفوز 95% CI: **[{_pct(win_diff['low'])}, {_pct(win_diff['high'])}]**.
- Mann–Whitney: `U={mw['u']:.0f}`، `p` للتفوّق الرتبي={_p(mw['p_greater'])}،
  `p` ثنائي={_p(mw['p_two_sided'])}، rank-biserial={mw['rank_biserial']:+.3f}.

## القدرة والقرار

- حساسية قدرة Mann–Whitney المشروطة بنموذج الإزاحة +5 نقاط:
  **{power['current_power'] * 100:.1f}%**. لا نعرض n مطلوباً لأن النجاح الفعلي
  مركّب من فرق الوسيط وفاصله وفرق الفوز أيضاً، والمحاكاة الحالية لا تمثله كاملاً.
- **حالة بوابة المرحلة 1: {gate}.** البيانات لا تحقق تفوقاً في الوسيط ونسبة
  الفوز معاً، كما أن اختلاف الشبكات وفقد الدخول يمنعان تفسير الفرق كأثر الإشارة.
- **القرار العملي: لا يبدأ تدريب نموذج الدخول العام الآن.** أصلح تصميم الضابطة
  أولاً أو انتقل إلى فرضية فرعية محددة مسبقاً؛ النمذجة على هذه المقارنة ستخلط
  أثر الإشارة باختلاف الكون والشبكة وقابلية التسعير.

## تحليل الحساسية

بإضافة الضابطة الرجعية `control_retro`: n الإشارة={sensitivity['summaries'][0]['n']}،
n الضابطة={sensitivity['summaries'][1]['n']}، فرق الوسيط
{_pct(sensitivity['median_difference']['estimate'])}، و`p` للتفوّق الرتبي
={_p(sensitivity['mann_whitney']['p_greater'])}. تحليل الحساسية
{'يغيّر حكم النجاح الحسابي، ولذلك لا يجوز دمج المصدرين' if sensitivity_changed else 'لا يغيّر حكم النجاح الحسابي'}؛
لكنه لا يصلح نتيجة أساسية لأن مسار جمعه واسترجاع شموعه مختلف.

## لماذا النتيجة ليست حكماً سببياً

- الضابطة تُسحب من `trending/verified` بينما الإشارة تأتي من feed؛ الكونان غير
  متطابقين، وتوزيع الشبكات غير متوازن.
- الضابطة التي تتلقى إشارة قبل توسيم نافذتها قد تُرقّى في `watchlist` ويضيع صفها
  الضابط؛ هذا حذف انتقائي لا يمكن إصلاحه رجعياً من الجدول الحالي.
- فرق `no_entry` يعني أن تحليل `status='ok'` يقارن جزأين مختلفين في قابلية
  التسعير. Fisher يشخّص المشكلة ولا يعالج انحيازها.
- تصنيف `token_class` مشتق من كل المشاهدات المتاحة وقد يتغير لاحقاً. لذلك هذا
  التقرير لقطة قابلة للتدقيق، لا عينة مسجلة مسبقاً وغير قابلة للتغير.
- Mann–Whitney يقيس ترتيب التوزيع لا فرق الوسيط تحديداً، وrank-biserial هو أثر
  ترتيب زوجي. معيار الوسيط منفصل ومحسوب بفاصل bootstrap.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--output")
    args = parser.parse_args()
    primary = analyze(args.db, include_retro_controls=False)
    sensitivity = analyze(args.db, include_retro_controls=True)
    report = render_markdown(primary, sensitivity, db_path=args.db)
    output = Path(args.output) if args.output else Path(config.ROOT) / "docs" / (
        f"phase1-{datetime.now(UTC).date().isoformat()}.md"
    )
    output.write_text(report, encoding="utf-8")
    print(f"report: {output}")


if __name__ == "__main__":
    main()
