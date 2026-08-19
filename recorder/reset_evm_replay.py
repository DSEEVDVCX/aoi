"""إرجاع عملات إعادةٍ عالقة إلى قائمة الانتظار — بانتقاءٍ لا بمحوٍ شامل.

`repair_evm_ledger.py --apply` أداةُ عطبٍ في الدفتر نفسه: تمحو شبكاتِ EVM كاملةً
(الأرصدة، المؤشّرات، كلّ صفوف التركّز) وتُلزم بالمجموعة كلّها في عمليّة واحدة،
فهي تُسقط لقطاتِ شبكةٍ سليمة لتُفرج عن عملاتِ شبكةٍ أخرى. وهذه الأداة للحالة
المقابلة: الدفتر والشِفرة سليمان، لكنّ حالة العملة كتبها **مسارٌ فُصل** — فبقيت
`error`/`negative` برقمٍ محسوب، والحالة النهائيّة تمنع إعادتها إلى المسار الجديد.

الحذف هو الإرجاع: `evm_replay_state` مشتقّةٌ بالكامل من سجلّات السلسلة، فمحوُ
صفّها يعيد العملة إلى الطابور من نشأتها. والافتراض عرضٌ فقط، ولا يُكتب شيء إلّا
بـ`--apply`.

وحالةٌ ثالثة أخفى من الاثنتين: **يتيمٌ** — صفوفُ تركّزٍ من إعادةٍ بلا صفِّ حالةٍ
أصلاً. لا حالةَ تُطابِق فلا `--status` يمسكه، فيبقى في الدفتر يُقرأ كأنّه مقيس.
يُعرَض في كلّ تشغيل، ويُحذف بـ`--orphans`.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import repair_evm_ledger  # noqa: E402 — مدقّقُ الشبكات واحد لا نسخة
from db import RecorderDB  # noqa: E402

# `done` ليست حالةً عالقة بل عملاً منجَزاً، ومحوُها يُسقط لقطاتٍ لا تُعاد إلّا
# بمشيٍ كامل. من أراد إعادةَ منجَزٍ فله `run_evm_replay.py --redo`، ومن أراد محوَ
# شبكةٍ لعطبٍ في الدفتر فله `repair_evm_ledger.py`.
REFUSED_STATUSES = ("done",)


def candidates(
    db: RecorderDB, networks: tuple[str, ...], statuses: tuple[str, ...],
    tokens: tuple[str, ...] = (),
) -> list[dict]:
    """صفوفُ الحالة المطابقة، أثقلَها نداءً أوّلاً (فهي الأجدر بالفحص قبل المحو)."""
    where = [
        f"network_id IN ({', '.join('?' for _ in networks)})",
        f"COALESCE(status, '') IN ({', '.join('?' for _ in statuses)})",
    ]
    params: list[str] = [*networks, *statuses]
    if tokens:
        where.append(f"token_address IN ({', '.join('?' for _ in tokens)})")
        params.extend(token.lower() for token in tokens)
    rows = db._conn.execute(
        f"""SELECT s.token_address, s.network_id, s.status, s.calls,
                   s.snapshots, s.transfers, s.from_block, s.last_error,
                   (SELECT COUNT(*) FROM chain_concentration c
                     WHERE c.token_address = s.token_address
                       AND c.network_id = s.network_id
                       AND c.is_replay = 1) AS rows_written
              FROM evm_replay_state s
             WHERE {' AND '.join(where)}
             ORDER BY COALESCE(s.calls, 0) DESC""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def orphans(
    db: RecorderDB, networks: tuple[str, ...], tokens: tuple[str, ...] = (),
) -> list[dict]:
    """صفوفُ إعادةٍ لا حالةَ لها — كتبها مسارٌ لم يُخلِّف سجلّاً يُنتقى به.

    `candidates` تنتقي من `evm_replay_state`، فعملةٌ كُتبت صفوفُها ثمّ اختفى صفُّ
    حالتها تصير غيرَ مرئيّةٍ لهذه الأداة كلّها: لا حالةَ تُطابَق فلا حذف. وهي
    أخطرُ الحالتين لا أهونهما — صفوفُها في الدفتر تُقرأ كأنّها مقيسة، ولا شيءَ
    يقول أيُّ مسارٍ كتبها ولا إلى أيّ كتلةٍ بلغ. ولذلك تُعرَض **دائماً**، ولا
    تُحذف إلّا بطلبٍ صريح (`--orphans`): إظهارُها إفادة، وحذفُها قرار.

    الشكلُ نفسُ شكل `candidates` كي يعمل `reset` و`_describe` بلا فرعٍ ثانٍ.
    """
    where = [
        f"c.network_id IN ({', '.join('?' for _ in networks)})",
        "c.is_replay = 1",
        "s.token_address IS NULL",
    ]
    params: list[str] = list(networks)
    if tokens:
        where.append(f"c.token_address IN ({', '.join('?' for _ in tokens)})")
        params.extend(token.lower() for token in tokens)
    rows = db._conn.execute(
        f"""SELECT c.token_address, c.network_id, NULL AS status, NULL AS calls,
                   NULL AS snapshots, NULL AS transfers, NULL AS from_block,
                   NULL AS last_error, COUNT(*) AS rows_written
              FROM chain_concentration c
              LEFT JOIN evm_replay_state s
                     ON s.token_address = c.token_address
                    AND s.network_id = c.network_id
             WHERE {' AND '.join(where)}
             GROUP BY c.token_address, c.network_id
             ORDER BY COUNT(*) DESC""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def reset(db: RecorderDB, rows: list[dict]) -> dict[str, int]:
    """محوٌ عملةً عملةً بمعاملةٍ لكلّ عملة.

    `reset_evm_replay_token` هي نفسها التي يستعملها `--redo`، فلا مسارَ محوٍ ثانٍ
    يُصان وحده. وعملةً عملةً لا دفعةً واحدة: العاملُ الحيّ قد يكون ممسكاً بالقاعدة،
    فمعاملةٌ صغيرة تنتظر أقلّ، وتعذُّرُ واحدةٍ لا يُلغي ما نجح قبلها.
    """
    out = {"tokens": 0, "rows_deleted": 0, "calls_freed": 0}
    for row in rows:
        db.reset_evm_replay_token(row["token_address"], row["network_id"])
        out["tokens"] += 1
        out["rows_deleted"] += int(row["rows_written"] or 0)
        out["calls_freed"] += int(row["calls"] or 0)
    return out


def _describe(rows: list[dict]) -> str:
    by_status: dict[str, list[dict]] = {}
    for row in rows:
        by_status.setdefault(str(row["status"] or ""), []).append(row)
    lines = []
    for status, group in sorted(by_status.items()):
        written = sum(int(r["rows_written"] or 0) for r in group)
        calls = sum(int(r["calls"] or 0) for r in group)
        lines.append(
            f"  {status or '(بلا حالة)'}: {len(group)} عملة · {calls} نداءً "
            f"سابقاً · {written} صفّاً سيُحذف"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--networks", nargs="+", required=True,
        help="شبكاتُ الإعادة المقصودة، مثل 8453",
    )
    parser.add_argument(
        "--status", nargs="+", default=["error"],
        help="الحالات التي تُرجَع إلى الطابور (الافتراض: error)",
    )
    parser.add_argument(
        "--token", nargs="*", default=[],
        help="عناوين بعينها؛ الافتراض كلُّ ما طابق الحالة",
    )
    parser.add_argument(
        "--max", type=int, default=0,
        help="سقفُ عملاتٍ تُرجَع في هذه العمليّة (0 = بلا سقف)",
    )
    parser.add_argument(
        "--orphans", action="store_true",
        help="أضِف صفوف الإعادة التي لا حالةَ لها (تُعرَض دائماً)",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        networks = repair_evm_ledger.validated_networks(args.networks)
    except ValueError as exc:
        print(str(exc))
        return 2
    statuses = tuple(dict.fromkeys(str(s) for s in args.status))
    refused = sorted(set(statuses) & set(REFUSED_STATUSES))
    if refused:
        print(
            f"حالاتٌ لا تُرجَع بهذه الأداة: {', '.join(refused)} — "
            "استعمل `run_evm_replay.py --redo` أو `repair_evm_ledger.py`"
        )
        return 2

    db = RecorderDB(config.DB_PATH, os.path.join(HERE, "schema.sql"))
    try:
        rows = candidates(db, networks, statuses, tuple(args.token))
        stray = orphans(db, networks, tuple(args.token))
        if stray and not args.orphans:
            # تُعرَض ولو لم تُطلَب: يتيمٌ صامتٌ يبقى في الدفتر إلى الأبد لأنّ
            # لا حالةَ له تُطابِق أيَّ `--status` يكتبه المشغّل.
            print(
                f"يتامى (غير مشمولين): {len(stray)} عملة · "
                f"{sum(int(r['rows_written'] or 0) for r in stray)} صفّاً — "
                "أضف --orphans لشملهم"
            )
        if args.orphans:
            rows = [*rows, *stray]
        if args.max > 0:
            rows = rows[: args.max]
        print(
            f"شبكات: {', '.join(networks)} · حالات: {', '.join(statuses)}"
            f"{' + يتامى' if args.orphans else ''} · مطابق: {len(rows)}"
        )
        if rows:
            print(_describe(rows))
        if not args.apply:
            # العرضُ هو الافتراض لأنّ الحذف يُنفَق نداءاتٍ لا تُستردّ: عملة Base
            # تُرجَع تعني مشياً كاملاً من نشأتها في الدورات القادمة.
            print("عرضٌ فقط. أضف --apply للتنفيذ.")
            return 0
        done = reset(db, rows)
        print(
            f"أُرجعت {done['tokens']} عملة · حُذف {done['rows_deleted']} صفّ "
            f"تركّز · أُهدر سابقاً {done['calls_freed']} نداءً"
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
