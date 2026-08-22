"""الحلقة الرئيسية لمسجّل البيانات التاريخي.

يقرأ الخام من fomo عبر FomoClient._get/_post مباشرة (قبل أي تعيين، لأن التعيين
يُسقط أثمن الحقول)، ويكتب إلى recorder.db. كل نداء upstream داخل try/except:
الفشل (502...) يُسجَّل في meta ويُتخطّى — الحلقة لا تموت أبداً.

read-only (FR-012): كل النداءات GET/POST قراءة فقط (feed، trending، verified،
leaderboard). لا نداء يكتب حالة حساب أو تداول.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import config
import extract
from db import RecorderDB, utcnow_iso
from leaderboard_cache import LeaderboardCache

_evm_admission_paused_runtime = False


def _fail_closed_evm_admission(networks: frozenset[str]) -> EVMAdmissionPolicy:
    global _evm_admission_paused_runtime

    _evm_admission_paused_runtime = True
    return EVMAdmissionPolicy(networks, -1, 0, 1, True)


@dataclass(frozen=True)
class EVMAdmissionPolicy:
    """قرار قبول حتميّ للعملات الجديدة؛ شبكات غير EVM لا تتأثر."""

    networks: frozenset[str]
    backlog: int
    numerator: int
    denominator: int
    paused: bool

    @property
    def percent(self) -> int:
        return 100 * self.numerator // self.denominator

    def allows(self, kind: str, token: str, network: str, key: str) -> bool:
        if str(network) not in self.networks:
            return True
        if self.numerator <= 0:
            return False
        if self.numerator >= self.denominator:
            return True
        # All admission paths must make the same decision for one token. The
        # unused kind/key parameters remain part of the call contract only.
        digest = hashlib.sha256(f"{network!s}:{token.lower()}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % self.denominator < self.numerator

    def score(self, kind: str, token: str, network: str) -> int:
        digest = hashlib.sha256(f"{network!s}:{token.lower()}".encode()).digest()
        return int.from_bytes(digest[:8], "big")


def evm_admission_policy(db: RecorderDB) -> EVMAdmissionPolicy:
    global _evm_admission_paused_runtime

    networks = frozenset(str(network) for network in config.EVM_NETWORKS)
    if not networks:
        return EVMAdmissionPolicy(networks, 0, 1, 1, False)
    marks = ", ".join("?" for _ in networks)
    backlog = int(db._conn.execute(
        "SELECT COUNT(*) FROM watchlist w LEFT JOIN evm_backfill_state b "
        "ON b.network_id=w.network_id AND lower(b.token_address)=lower(w.token_address) "
        f"WHERE w.active=1 AND w.network_id IN ({marks}) "
        "AND COALESCE(b.status, '') <> 'done'",
        tuple(sorted(networks)),
    ).fetchone()[0])
    paused = _evm_admission_paused_runtime or db.get_meta("evm_admission_paused") == "1"
    if paused and backlog < config.EVM_ADMISSION_RESUME_BELOW:
        paused = False
        _evm_admission_paused_runtime = False
        db.set_meta("evm_admission_paused", "0")
    elif not paused and backlog >= config.EVM_ADMISSION_PAUSE_AT:
        paused = True
        _evm_admission_paused_runtime = True
        db.set_meta("evm_admission_paused", "1")
    elif paused:
        _evm_admission_paused_runtime = True
    if paused:
        fraction = (0, 1)
    elif backlog < config.EVM_ADMISSION_FULL_BELOW:
        fraction = (1, 1)
    elif backlog < config.EVM_ADMISSION_REDUCED_BELOW:
        fraction = (3, 4)
    else:
        fraction = (1, 2)
    return EVMAdmissionPolicy(networks, backlog, *fraction, paused)


def _load_access_token() -> str:
    """يقرأ session_token (access_token) من CredentialStore على القرص.

    لا يطبع قيمة التوكن إطلاقاً (FR-013). يرفع خطأً واضحاً إن غاب الاعتماد.
    القرص هو مصدر الحقيقة: خادم الـ api (TokenRefresher) يكتب توكناً طازجاً هنا
    قبل انتهائه، فيلتقطه المسجّل كل دورة بلا أي تسجيل دخول يدوي.
    """
    from fomo_api.auth.credential_store import CredentialStore

    creds = CredentialStore(config.credential_state_path()).load()
    if creds is None or not creds.access_token:
        raise RuntimeError(
            "لا يوجد اعتماد صالح في ملف الحالة — شغّل خدمة الـ api أولاً لتوليده."
        )
    return creds.access_token


def _build_client(access_token: str) -> Any:
    """يُنشئ FomoClient من توكن معطى (لا يقرأ القرص، لا يطبع التوكن)."""
    from fomo_api.clients.fomo_client import FomoClient

    return FomoClient(session_token=access_token)


def _load_client() -> Any:
    """يحمّل session_token من القرص ويُنشئ FomoClient.

    لا يطبع قيمة التوكن إطلاقاً (FR-013). يرفع خطأً واضحاً إن غاب الاعتماد.
    """
    return _build_client(_load_access_token())


def _exc_note(exc: Exception) -> str:
    """اسمُ الخطأ ورسالتُه **ورمزُ حالته** — فالرسالة وحدها تكذب أحياناً.

    قِيس 2026-08-19T14:54Z: حجبت المنصّة الحساب بـ403 على كلّ مسارٍ من اثني
    عشر، فكتب المسجّل «UpstreamUnavailableError: fomo.family is currently
    unreachable» — وهي رسالةُ الصنف الافتراضيّة نفسها التي يكتبها الانقطاعُ
    الشبكيّ الحقيقيّ. فذهب التشخيصُ إلى DNS وping وTLS والمصدرُ يردّ في 20ms،
    والرمزُ 403 كان في `ApiError.details` طوال الوقت ولم يُطبَع لأنّ التنسيق
    يأخذ `str(exc)` وحدها. وطبقةُ EVM تُدرج الرمزَ أصلاً — يشهد عليها اختبارٌ
    يطلب `"503" in last_error_evm` — فهذا توحيدُ عُرفٍ قائم لا اختراعُ آخر.

    والقصُّ مقصود: `details["reason"]` تحمل نصَّ استثناءِ النقل كاملاً، وهذه
    الملاحظةُ تسكن صفّاً في `meta` يُقرأ في اللوحة لا سجلَّ تنقيبٍ مفتوحَ الطول.
    """
    text = f"{type(exc).__name__}: {exc}"
    details = getattr(exc, "details", None)
    if not isinstance(details, dict) or not details:
        return text
    pairs = ", ".join(f"{k}={str(v)[:120]}" for k, v in sorted(details.items()))
    return f"{text} ({pairs})"


def _note_shutout(
    db: RecorderDB, block: str, recorded_at: str, asked: int, produced: int
) -> int:
    """يرفع صوتَ الحصار: كتلةٌ تسأل ولا تُنتج شيئاً، دورةً بعد دورة.

    الدرسُ من 2026-08-19: مات `/v2/users/{id}` فردَّ 404 لكلّ معرّف، و404 و«حسابٌ
    محذوف» مسارٌ واحد — فكُتب `empty` لكلّ متداول، و`traders_rows: 0` في كلّ سطر
    سجلّ، و`errors: 0` كذلك. 21 ساعةً ولا كلمةَ خطأ واحدة، لأنّ «لا بيانات لهذا
    العنصر» جوابٌ **مشروع** لعنصرٍ واحد. الصفرُ الفرديُّ عاديّ؛ الصفرُ الجماعيُّ
    المتكرّرُ لا يكون إلّا عطلاً.

    فالعدّادُ في `meta` لا في الذاكرة: لو أُعيد تشغيلُ المسجّل كلَّ ساعة (وقد
    حدث) لبدأت الذاكرةُ من الصفر فبقي الحصارُ صامتاً كما كان. ويُصفَّر عند أوّل
    صفٍّ ينزل — فالتشخيصُ عن «الآن» لا عن الأمس.

    يُنادى فقط حيث يكون صفرُ الكتلة **مستحيلاً** لا نادراً: 50 متداولاً كلُّهم
    محذوفون ليس حدثاً (قِيس: 7 غيابات حقيقيّة من 500). أمّا الشموعُ فتغيب
    مشروعةً، والإشاراتُ تخلو في دقيقةٍ هادئة — فلا تُوصَل هذه بها.

    يعيد طولَ السلسلة بعد التحديث.
    """
    key = f"shutout_{block}"
    if produced > 0 or asked <= 0:
        if db.get_meta(key) not in (None, "0"):
            db.note_error(key, "0")
        return 0
    previous = db.get_meta(key)
    streak = (int(previous) if previous and previous.isdigit() else 0) + 1
    db.note_error(key, str(streak))
    if streak >= config.SHUTOUT_STREAK_ALERT:
        db.note_error(
            f"last_error_{block}",
            f"{recorded_at}: ShutoutSuspected: سُئل {asked} ولم ينزل صفٌّ واحد "
            f"في {streak} دورةً متتالية — الأرجحُ أنّ المسار نفسه تعطّل، "
            f"لا أنّ كلَّ العناصر خالية",
        )
    return streak


async def _fetch_feed_raw(client: Any) -> Any:
    """خام GET /feed — نتجاوز get_feed (تُعيّن وتُسقط topTraders/body الكامل)."""
    from fomo_api.config import settings

    params = {"feedTypes": list(config.FEED_TYPES), "limit": config.FEED_LIMIT}
    return await client._get(settings.upstream_feed_path, params)


async def _fetch_trending_raw(client: Any) -> Any:
    from fomo_api.config import settings

    return await client._post(settings.upstream_trending_tokens_path, {})


async def _fetch_verified_raw(client: Any) -> Any:
    from fomo_api.config import settings

    return await client._get(settings.upstream_verified_tokens_path)


async def _fetch_most_held_raw(client: Any) -> Any:
    """خام POST /proxy/mostHeld — قائمة اكتشاف ثالثة.

    مقيس حيّاً: `[200]`، 25 عنصراً، يتقاطع مع مستخرِج trending في 18 مفتاحاً
    ⇒ `extract_market_tick` يكفي بلا تعديل. 19 من 25 كانت مراقَبة عندنا و**6
    جديدة تماماً** — أي أنّه يرى عملات لا تراها القائمتان الأخريان.
    """
    from fomo_api.config import settings

    return await client._post(settings.upstream_most_held_path, {})


async def _fetch_filter_tokens_raw(client: Any, symbols: list[str]) -> Any:
    """خام POST /proxy/filterTokens — الجسم **مصفوفة** `"address:networkId"`.

    مقيس حيّاً: 150 عنواناً في نداء واحد ترجع 150/150 (326 KB)، والعنوان
    الميّت **يُحذف بصمت والدفعة تنجو** (5 من 6 رجعت، `[200]`) — فعملة تُشطب
    أثناء النافذة لا تُعمي بقيّة الدفعة.
    """
    from fomo_api.config import settings

    return await client._post(settings.upstream_filter_tokens_path, symbols)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


async def _fetch_traders_raw(client: Any, trader_ids: list[str]) -> Any:
    """خام GET /v2/users?userIds=…&userIds=… — رزمةُ ملفّات متداولين.

    نتجاوز `get_trader_profiles` لأنّها تُعيّن وتُسقط حقولاً؛ نريد المغلّف
    كاملاً للأرشيف. مقيس حيّاً: 26 مفتاحاً لكلّ مستخدم.

    والرزمةُ ليست تحسيناً اختياريّاً: `/v2/users/{id}` يردّ 404 «User not found»
    لكلّ معرّفٍ صحيحِ الشكل منذ 2026-08-19T14:53Z — ومعرّفاتٌ خرجت لحظتَها من
    200 على `/v2/leaderboard` تردّ 404 هي أيضاً، فلا هي معرّفاتُنا ولا حسابُنا.
    وهذه أوّلُ مرّةٍ يُجلَب فيها متداولٌ منذ ذلك الوقت (3,775 صفّاً آخرُها
    14:53:51Z، ثمّ 21 ساعةً من `empty` صامت).

    والحدُّ 100 لكلّ نداء يُصرّح به المصدرُ نفسه في خطأ التحقّق.
    """
    from fomo_api.config import settings

    return await client._get(
        settings.upstream_traders_batch_path, {"userIds": trader_ids}
    )


async def _fetch_bars_raw(
    client: Any, token_address: str, network_id: str, from_ts: int, to_ts: int,
    resolution: str | None = None,
) -> Any:
    """خام POST /proxy/getBarsNew.

    نتجاوز get_token_bars لأنّها تُعيّن؛ نريد المغلّف الخام لنستخرجه بأنفسنا.
    symbol = "address:networkId" و from/to إلزاميان — كلاهما مؤكَّد حيّاً
    (العنوان المجرّد → 502، وغياب from/to → 400).
    """
    from fomo_api.config import settings

    body = {
        "symbol": f"{token_address}:{network_id}",
        "resolution": resolution or config.BARS_RESOLUTION,
        "from": from_ts,
        "to": to_ts,
        "countBack": config.BARS_COUNT_BACK,
    }
    return await client._post(settings.upstream_get_bars_path, body)


async def run_bars_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """يسحب شموع شريحة من العملات المراقَبة (جدولة دوّارة).

    كل عملة على حدة داخل try: فشل واحدة لا يمنع البقيّة ولا يُسقط الدورة.
    نطلب دائماً النافذة الكاملة منذ (أول ظهور - سياق) لا منذ آخر شمعة: الشمعة
    الأخيرة تكون قيد التكوّن فتُراجَع، والسحب الكامل يشفي أي ثغرة سابقة.
    """
    stats = {"bars_tokens": 0, "bars_rows": 0, "bars_no_data": 0, "bars_errors": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (now_dt - timedelta(seconds=config.BARS_REFRESH_SECONDS)).isoformat()
    due = db.bars_fetch_due(
        limit=config.BARS_PER_CYCLE,
        stale_before_iso=stale_before,
        max_no_data_attempts=config.BARS_MAX_NO_DATA_ATTEMPTS,
    )
    to_ts = int(now_dt.timestamp())

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        try:
            first_seen = datetime.fromisoformat(w["first_seen_at"])
            from_dt = first_seen - timedelta(hours=config.BARS_PRE_SIGNAL_HOURS)
            # لا نتجاوز سقف النافذة (تفادي طلب مدى أوسع ممّا يعيده fomo أصلاً).
            earliest = now_dt - timedelta(hours=config.BARS_MAX_SPAN_HOURS)
            from_ts = int(max(from_dt, earliest).timestamp())

            raw = await _fetch_bars_raw(client, addr, net, from_ts, to_ts)
            status = extract.bars_status(raw) or "no_data"
            rows = extract.extract_bars(
                raw, addr, net, config.BARS_RESOLUTION, recorded_at
            )
            if rows:
                stats["bars_rows"] += db.insert_bars(rows)
                # حكم التشوّه يحتاج جار الشمعة الأخيرة — يُعاد بعد كل إدراج.
                db.recompute_bar_flags(addr, net, config.BARS_RESOLUTION)
                stats["bars_tokens"] += 1
            else:
                stats["bars_no_data"] += 1
            db.set_bars_state(addr, net, status if rows else "no_data", len(rows), recorded_at)
        except Exception as exc:  # noqa: BLE001 — عملة واحدة لا تُسقط الشريحة
            stats["bars_errors"] += 1
            try:
                db.set_bars_state(addr, net, "error", 0, recorded_at)
            except Exception:  # noqa: BLE001 — جدولةُ إعادة لا قياس؛ ولو رمت هي
                pass                        # لَما بُلِغ الختمُ أدناه أصلاً
            db.note_error("last_error_bars", f"{recorded_at}: {_exc_note(exc)}")
        if i + 1 < len(due):
            await sleep(config.BARS_PACING_SECONDS)
    return stats


def admit_control_sample(
    db: RecorderDB,
    candidates: Sequence[
        tuple[str, str]
        | tuple[str, str, float | None]
        | tuple[str, str, float | None, str]
    ],
    recorded_at: str,
    rng: random.Random | None = None,
    policy: EVMAdmissionPolicy | None = None,
) -> int:
    """يُدخل عملات ضابطة مختارة **عشوائياً** من نفس كون العملات. يعيد كم أُدخلت.

    شروط سلامة المقارنة (كلّها مقصودة):
    - **عشوائيّ لا حسب الترتيب**: الأخذ من رأس قائمة الرواج يختار الأعلى حجماً
      فيصير الفرق عن المُشار إليها فرقَ حجمٍ لا فرقَ إشارة.
    - **بلا نظر إلى المستقبل**: الاختيار يعتمد على ما هو معروف الآن فقط؛ لا
      يُستشار أداء لاحق (وإلّا كان تسرّباً صريحاً).
    - **يُستبعد كل ما أُشير إليه** ولو لم يدخل المراقبة — وإلّا لم يعد ضابطاً.
    - **بالتقسيط** (`CONTROL_PER_CYCLE`): أخذ الأربعين دفعةً واحدة يجعلها كلّها
      عيّنة من لحظة سوقية واحدة، فيختلط أثر الإشارة بأثر تلك اللحظة.
    """
    need = config.CONTROL_GROUP_SIZE - db.active_watch_count(is_control=1)
    if need <= 0 or not candidates:
        return 0

    known = db.known_tokens()
    signalled = db.signalled_tokens()
    normalized: dict[tuple[str, str], tuple[float | None, str | None]] = {}
    strict_comparison = any(len(candidate) == 4 for candidate in candidates)
    for candidate in candidates:
        if len(candidate) == 2:
            addr, net = candidate
            price_usd = None
            admission_source = None
            has_price = False
        elif len(candidate) == 3:
            addr, net, price_usd = candidate
            admission_source = None
            has_price = True
        else:
            addr, net, price_usd, admission_source = candidate
            has_price = True
        # Production candidates carry the current tick price. A missing price
        # is not a valid entry point; legacy callers without a price remain
        # supported for deterministic tests and offline tooling.
        if has_price:
            try:
                price_usd = float(price_usd) if price_usd is not None else None
            except (TypeError, ValueError):
                continue
            if price_usd is None or not math.isfinite(price_usd) or price_usd <= 0:
                continue
        normalized[(addr, net)] = (price_usd, admission_source)

    pool = sorted(
        key for key in normalized
        if key not in known and key[0] not in signalled
        and (
            policy is None
            or policy.allows("control", key[0], str(key[1] or ""), recorded_at)
        )
    )
    if not pool:
        return 0

    rng = rng or random.Random()
    pick_count = min(len(pool), config.CONTROL_PER_CYCLE, need)
    if strict_comparison:
        signal_networks = db._conn.execute(
            """SELECT network_id, admission_source, COUNT(*) AS n
                 FROM watch_windows
                WHERE design_version=? AND is_control=0
                GROUP BY network_id, admission_source""",
            (config.CONTROL_DESIGN_VERSION,),
        ).fetchall()
        weights = {
            (
                str(row["network_id"] or ""),
                str(row["admission_source"] or ""),
            ): int(row["n"])
            for row in signal_networks
        }
        if not weights:
            return 0
    else:
        signal_networks = db._conn.execute(
            """SELECT network_id, COUNT(*) AS n FROM watchlist
               WHERE active=1 AND is_control=0 GROUP BY network_id"""
        ).fetchall()
        weights = {
            (str(row["network_id"] or ""), ""): int(row["n"])
            for row in signal_networks
        }
    eligible_by_network = {}
    for key in pool:
        source = normalized[key][1] if strict_comparison else ""
        source = source or ""
        eligible_by_network.setdefault((str(key[1] or ""), source), []).append(key)

    if weights and any(group in weights for group in eligible_by_network):
        eligible_networks = [group for group in eligible_by_network if group in weights]
        total_weight = sum(weights[group] for group in eligible_networks)
        if strict_comparison:
            existing_rows = db._conn.execute(
                """SELECT network_id, admission_source, COUNT(*) AS n
                     FROM watch_windows
                    WHERE design_version=? AND is_control=1
                    GROUP BY network_id, admission_source""",
                (config.CONTROL_DESIGN_VERSION,),
            )
            existing = {
                (
                    str(row["network_id"] or ""),
                    str(row["admission_source"] or ""),
                ): int(row["n"])
                for row in existing_rows
            }
        else:
            existing = {
                (str(row["network_id"] or ""), ""): int(row["n"])
                for row in db._conn.execute(
                    """SELECT network_id, COUNT(*) AS n FROM watchlist
                       WHERE active=1 AND is_control=1 GROUP BY network_id"""
                )
            }
        picks = []
        for _ in range(pick_count):
            available = [network for network in eligible_networks
                         if eligible_by_network[network]]
            if not available:
                break
            target_total = sum(existing.values()) + 1
            network = max(
                available,
                key=lambda item: (
                    target_total * weights[item] / total_weight - existing.get(item, 0),
                    weights[item],
                ),
            )
            choices = eligible_by_network[network]
            pick = (
                min(choices, key=lambda item: policy.score(
                    "control", item[0], str(item[1] or "")
                ))
                if policy is not None else rng.choice(choices)
            )
            eligible_by_network[network].remove(pick)
            picks.append(pick)
            existing[network] = existing.get(network, 0) + 1
    else:
        picks = (
            sorted(pool, key=lambda item: policy.score(
                "control", item[0], str(item[1] or "")
            ))[:pick_count]
            if policy is not None else rng.sample(pool, pick_count)
        )
    added = 0
    for addr, net in picks:
        if db.admit_control(
            addr, net, config.CONTROL_WATCH_HOURS, recorded_at,
            admission_price_usd=normalized[(addr, net)][0],
            design_version=config.CONTROL_DESIGN_VERSION,
            admission_source=normalized[(addr, net)][1],
        ):
            added += 1
    return added


def admit_signal_comparison_windows(
    db: RecorderDB,
    candidates: Sequence[tuple[str, str, float | None, str]],
    recorded_at: str,
    policy: EVMAdmissionPolicy | None = None,
    admitted_signals: set[str] | None = None,
) -> int:
    """يدخل إشارات v3 فقط عندما تظهر في نفس قائمة المرشحين المستخدمة للضابطة."""
    candidate_prices: dict[tuple[str, str], tuple[float, str]] = {}
    for addr, net, price, admission_source in candidates:
        try:
            value = float(price) if price is not None else None
        except (TypeError, ValueError):
            continue
        if value is not None and math.isfinite(value) and value > 0:
            candidate_prices[(addr, str(net or ""))] = (value, admission_source)

    added = 0
    rows = db._conn.execute(
        """SELECT s.id, s.token_address, s.network_id, s.signal_type
             FROM signal_events s
            WHERE s.signal_type IN (?, ?)
              AND s.recorded_at = ?""",
        (*config.TRIGGER_SIGNAL_TYPES, recorded_at),
    ).fetchall()
    for row in rows:
        key = (row["token_address"], str(row["network_id"] or ""))
        admission = candidate_prices.get(key)
        if admission is None:
            continue
        if policy is not None:
            existing = db._conn.execute(
                "SELECT active FROM watchlist WHERE token_address=? AND network_id=?",
                key,
            ).fetchone()
            if admitted_signals is not None and existing is None and str(row["id"]) not in admitted_signals:
                continue
            if existing is None and not policy.allows(
                "signal", key[0], key[1], str(row["id"]),
            ):
                continue
        price, admission_source = admission
        if db.add_signal_comparison_window(
            key[0], key[1], row["signal_type"], row["id"],
            config.WATCH_HOURS, recorded_at, price,
            config.CONTROL_DESIGN_VERSION, admission_source,
        ):
            added += 1
    return added


AGE_OK = "ok"
AGE_TOO_YOUNG = "too_young"
AGE_UNKNOWN = "unknown"


def token_age_days(created_at: Any, now_iso: str) -> float | None:
    """عمرُ العملة بالأيّام لحظةَ `now_iso`، أو None إن تعذّر.

    `token_created_at` **ثوانٍ منذ المبدأ مخزَّنةً نصّاً** لا تاريخاً مقروءاً،
    فـ`julianday()` عليه يعيد NULL بصمت — ولهذا يُحسب هنا في بايثون لا في SQL.

    العمرُ السالب (تاريخُ إنشاءٍ في المستقبل) ليس عمراً: يعود None فيُعامَل
    معاملةَ المجهول لا معاملةَ القديم، وإلّا لصار انحرافُ ساعةٍ بوّابةً مفتوحة.
    """
    if created_at in (None, ""):
        return None
    try:
        created = int(float(created_at))
    except (TypeError, ValueError):
        return None
    if created <= 0:
        return None
    age = (int(datetime.fromisoformat(now_iso).timestamp()) - created) / 86400.0
    return age if age >= 0 else None


def age_verdict(created_at: Any, now_iso: str) -> str:
    """حكمُ بوّابة العمر على تاريخِ إنشاءٍ واحد. `MIN_TOKEN_AGE_DAYS=0` يُلغيها."""
    if not config.MIN_TOKEN_AGE_DAYS:
        return AGE_OK
    age = token_age_days(created_at, now_iso)
    if age is None:
        return AGE_UNKNOWN
    return AGE_OK if age >= config.MIN_TOKEN_AGE_DAYS else AGE_TOO_YOUNG


def stored_created_at(db: RecorderDB, addr: str, net: str) -> str | None:
    row = db._conn.execute(
        """SELECT token_created_at FROM token_static
            WHERE token_address=? AND network_id=?""",
        (addr, net),
    ).fetchone()
    if row is None:
        return None
    return row["token_created_at"] or None


async def resolve_ages(
    client: Any,
    db: RecorderDB,
    keys: Sequence[tuple[str, str]],
    recorded_at: str,
    stats: dict[str, int],
) -> None:
    """يجلب تاريخَ إنشاءِ المرشّحين المجهولين ويثبّته في `token_static`.

    الإشارةُ نفسها **لا تحمل عمرَ العملة**: حدثُ الـfeed فيه `tokenAddress` و
    `networkId` و`createdAt` الخاصّ بالمنشور وحده — لا كائنَ عملةٍ فيه إطلاقاً.
    فبوّابةُ العمر تحتاج مصدراً، و`filterTokens` يسأل عن عنوانٍ بالاسم بلا شرط
    شعبيّة ويحمل `token.createdAt`.

    الكلفةُ نداءٌ واحد في الدورة على الأكثر: المرشّحون الجدد **1.11 عملة في
    الدورة** (أقصى ما رُصد 4) والدفعة تحمل 150 — مقابل تسعة نداءات شموع تُجرى
    في الدورة نفسها. والنتيجةُ تُخزَّن فلا يُسأل عن العملة نفسها ثانيةً: إشارةٌ
    لاحقة على عملةٍ رُفضت تُحكَم من التاريخ المخزَّن بلا شبكة.

    الفشلُ يُميَّز عن الجهل: `age_lookup_failed` عدّادٌ وختمُ خطأ وسلسلةٌ في
    `meta`، لأنّ 502 من المنبع يجعل كلّ مرشّح «مجهولاً» فتتوقّف المراقبة كلّها —
    وهذا انقطاعٌ يجب أن يُسمع لا أن يُقرأ ترشيحاً عادياً.
    """
    if not keys:
        return
    symbols = [f"{addr}:{net}" for addr, net in keys]
    try:
        raw = await _fetch_filter_tokens_raw(client, symbols)
    except Exception as exc:  # noqa: BLE001 — الإشارة محفوظة أصلاً
        stats["age_lookup_failed"] += len(keys)
        db.note_error("last_error_age_lookup", f"{recorded_at}: {_exc_note(exc)}")
        db.bump_counter("age_lookup_failed_streak")
        db.bump_counter("age_lookup_failed_total", len(keys))
        return
    by_addr: dict[str, Any] = {}
    for item in extract.unwrap_token_list(raw):
        a = extract.token_list_address(item)
        if a:
            by_addr[a.lower()] = item
    with db.batch():
        for addr, net in keys:
            item = by_addr.get(addr.lower())
            if item is None:
                continue          # حُذف بصمت (عنوان ميّت) — يبقى مجهولاً
            if _write_filter_static(db, item, addr, net, recorded_at):
                stats["age_resolved"] += 1
    db.set_meta("age_lookup_last_ok_at", recorded_at)
    db.set_meta("age_lookup_failed_streak", "0")


def _opens_window(existing: Any) -> bool:
    """هل ستفتح `upsert_watch` نافذةً جديدة لهذا الصفّ؟

    شرطُ التحديث فيه `WHERE watchlist.active=0 OR watchlist.is_control=1`، فثلاثُ
    حالات تفتح نافذة: لا صفَّ أصلاً، صفٌّ منتهي، صفٌّ ضابط يُحوَّل إلى مُشار
    إليه. والنشطُ غيرُ الضابط وحده لا تُفتَح له نافذة — نافذتُه تجري بالفعل.
    """
    if existing is None:
        return True
    return not existing["active"] or bool(existing["is_control"])


async def record_feed(
    db: RecorderDB,
    raw_feed: Any,
    recorded_at: str,
    rank_lookup,
    rank_lookups,
    policy: EVMAdmissionPolicy,
    stats: dict[str, int],
    client: Any = None,
) -> set[str]:
    """يحفظ الإشارات أولاً، ثم يحاول المراقبة بلا أن يربط مصير الاثنين."""
    events = extract.unwrap_feed(raw_feed)
    rows = []
    inserted: set[str] = set()
    with db.batch():
        db.insert_snapshot("feed", raw_feed, recorded_at)
        newest = max(
            (str(event.get("createdAt")) for event in events if event.get("createdAt")),
            default=None,
        )
        if newest:
            db.set_meta("last_feed_event_at", newest)
        for event in events:
            row = extract.extract_signal_event(
                event, recorded_at, rank_lookup, rank_lookups,
            )
            if row is None:
                continue
            rows.append(row)
            if db.insert_signal(row):
                inserted.add(str(row["id"]))
                stats["signals"] += 1

    triggers = [r for r in rows if r["signal_type"] in config.TRIGGER_SIGNAL_TYPES]
    # حالةُ القائمة لكلّ مُشغّل مرّةً واحدة: يقرأها فرزُ المرشّحين وحلقةُ القبول.
    watch_state: dict[tuple[str, str], Any] = {}
    for row in triggers:
        key = (str(row["token_address"]), str(row["network_id"] or ""))
        if key not in watch_state:
            watch_state[key] = db._conn.execute(
                """SELECT active, is_control FROM watchlist
                    WHERE token_address=? AND network_id=?""",
                key,
            ).fetchone()

    # عمرُ المرشّحين الذين ستُفتَح لهم نافذة، قبل الحكم. المخزَّن يكفي غالباً
    # فلا نداءَ له؛ المجهول وحده يخرج للشبكة، ونداءٌ واحدٌ يحمل 150.
    if client is not None and config.MIN_TOKEN_AGE_DAYS:
        unknown = [
            key for key, existing in watch_state.items()
            if _opens_window(existing) and stored_created_at(db, *key) is None
        ]
        await resolve_ages(client, db, unknown, recorded_at, stats)

    admitted: set[str] = set()
    for row in triggers:
        token = str(row["token_address"])
        network = str(row["network_id"] or "")
        existing = watch_state[(token, network)]
        # البوّابةُ تحكم كلَّ نافذةٍ تُفتَح — الجديدةَ **وإعادةَ التنشيط**:
        # `upsert_watch` يُحيي صفّاً منتهياً (active=0) أو ضابطاً بنافذةٍ جديدة،
        # فلو حُصِرت في الجديد لدخلت الصغيرةُ من باب الإحياء.
        if _opens_window(existing):
            verdict = age_verdict(stored_created_at(db, token, network), recorded_at)
            if verdict != AGE_OK:
                stats["age_rejected"] += 1
                if verdict == AGE_UNKNOWN:
                    stats["age_unknown"] += 1
                continue
        if existing is None:
            if db.active_watch_count(is_control=0) >= config.WATCHLIST_CAP:
                continue
            if not policy.allows("signal", token, network, str(row["id"])):
                if network in policy.networks and str(row["id"]) in inserted:
                    stats["evm_admission_deferred"] += 1
                continue
        try:
            added = db.upsert_watch(
                token_address=token,
                network_id=network,
                source=row["signal_type"],
                entry_signal_id=row["id"],
                watch_hours=config.WATCH_HOURS,
                now_iso=recorded_at,
                admission_price_usd=row.get("price_usd"),
            )
            admitted.add(str(row["id"]))
            if added:
                stats["watch_added"] += 1
                watch_state[(token, network)] = {"active": 1, "is_control": 0}
        except Exception as exc:  # noqa: BLE001 - signal is already durable
            stats["errors"] += 1
            db.note_error("last_error_watch_admission", f"{recorded_at}: {_exc_note(exc)}")
    if stats["evm_admission_deferred"]:
        db.bump_counter("evm_admission_deferred_total", stats["evm_admission_deferred"])
    if stats["age_rejected"]:
        db.bump_counter("age_rejected_total", stats["age_rejected"])
    return admitted


async def _fetch_thesis_raw(client: Any, token_address: str, network_id: str) -> Any:
    """خام GET /feed/token/thesis — نتجاوز get_token_thesis_feed لأنّها تُعيّن."""
    from fomo_api.config import settings

    params = {
        "tokenAddress": token_address,
        "networkId": int(network_id) if str(network_id).isdigit() else network_id,
        "threshold": config.SOCIAL_THRESHOLD,
    }
    return await client._get(settings.upstream_feed_token_thesis_path, params)


async def _fetch_token_details_raw(client: Any, token_address: str, network_id: str) -> Any:
    """خام POST /proxy/tokenDetails — يحمل top10HoldersPercent وعدد الحائزين.

    `tokenId` **يجب** أن يكون "address:networkId" كـgetBarsNew؛ العنوان المجرّد
    يجعل الخادم يرمي 502 من Cloudflare (يبدو عطلاً وهو طلب مشوّه).
    """
    from fomo_api.config import settings

    body = {"tokenId": f"{token_address}:{network_id}" if network_id else token_address}
    return await client._post(settings.upstream_token_details_path, body)


async def _fetch_hodlers_raw(client: Any, token_address: str, network_id: str) -> Any:
    """خام GET /hodlers/top — تفصيل كبار الحائزين (يعطي top1 أيضاً).

    المعامل `tokens` سلسلة JSON لقائمة كائنات، وهو شكل المصدر لا عنوان مفرد.
    """
    from fomo_api.config import settings

    net: Any = int(network_id) if str(network_id).isdigit() else network_id
    params = {"tokens": json.dumps([{"address": token_address, "networkId": net}])}
    return await client._get(settings.upstream_hodlers_top_path, params)


async def refresh_leaderboard(
    lb: LeaderboardCache, db: RecorderDB, now_mono: float, recorded_at: str
) -> None:
    """يحدّث الصدارة عند الاستحقاق، يؤرشف الخام، ويسجّل الفشل صراحةً.

    بلا الأرشفة يضيع مسار كل متصدّر عبر الزمن إلى الأبد — الرتبة كانت تُقرأ
    وتُرمى كل ساعة. وبلا تسجيل الفشل يبقى ركود الخريطة (تحديث فاشل أو مغلّف
    فارغ ⇒ الخريطة القديمة) صامتاً إلى الأبد: `last_error_leaderboard` معروض
    في اللوحة كبقيّة المصادر.

    **خام كل مدّة في مصدر مستقلّ** (`leaderboard` / `leaderboard_24h` …): دمجها
    في مصدر واحد يخلط أربع قوائم مختلفة في أرشيف لا يُفكّ. `raw_by_period`
    تعيد المدد الطازجة (آخر محاولة) فحسب — أرشفة مغلّف قديم بتوقيت جديد تضع في
    الأرشيف صدارةً لم نجلبها قطّ. ومدّة فشلت وأخواتها نجحت لا تُفشل الدورة،
    لكنّها تُسجَّل كي لا يصمت العطل الجزئيّ.
    """
    was_stale = lb.is_stale(now_mono)
    refreshed = await lb.maybe_refresh(now_mono)
    if refreshed:
        for period, raw in lb.raw_by_period.items():
            if raw is None:
                continue
            source = "leaderboard" if period == "all" else f"leaderboard_{period}"
            db.insert_snapshot(source, raw, recorded_at)
        missing = lb.failed_periods()
        if missing:
            db.set_meta(
                "last_error_leaderboard",
                f"{recorded_at}: periods without a lookup: {', '.join(missing)}",
            )
    elif was_stale and not refreshed:
        db.set_meta(
            "last_error_leaderboard",
            f"{recorded_at}: refresh failed or empty — keeping previous lookup",
        )


async def run_macro_bars_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """يسحب شموع السوق الكلّي (SOL/WETH/WBTC) مرّة كل ساعة.

    مرجع النظام السوقي: عائد أي عملة يُقرأ بمعزل عن السوق فيبدو أثر الإشارة
    أثرَ يومٍ صاعد. لا تقودها watchlist — أصول ثابتة في config.MACRO_BARS.
    التخزين في token_bars بدقّة ساعية فلا يتصادم مع شموع المراقبة (5 دقائق)،
    والموسِّم لا يلمسها (لا إشارة ولا watch لها). الختم في meta يقود الإيقاع؛
    دورة ضمن الساعة تخرج فوراً بلا نداء شبكة.
    """
    stats = {"macro_rows": 0, "macro_errors": 0, "macro_no_data": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    last = db.get_meta("last_macro_bars_at")
    if last is not None:
        elapsed = (now_dt - datetime.fromisoformat(last)).total_seconds()
        if elapsed < config.MACRO_BARS_REFRESH_SECONDS:
            return stats

    to_ts = int(now_dt.timestamp())
    from_ts = to_ts - config.MACRO_BARS_SPAN_HOURS * 3600
    for i, (label, addr, net) in enumerate(config.MACRO_BARS):
        try:
            raw = await _fetch_bars_raw(
                client, addr, net, from_ts, to_ts, resolution=config.MACRO_BARS_RESOLUTION
            )
            rows = extract.extract_bars(
                raw, addr, net, config.MACRO_BARS_RESOLUTION, recorded_at
            )
            # «نجاح بلا شموع» ليس نجاحاً: لو كان دائماً (تهيئة خاطئة) صار صمتاً
            # أبدياً — نفس طراد no_data الموثّق في دورة الشموع.
            if not rows:
                stats["macro_no_data"] += 1
            stats["macro_rows"] += db.insert_bars(rows)
            if rows:
                db.recompute_bar_flags(addr, net, config.MACRO_BARS_RESOLUTION)
        except Exception as exc:  # noqa: BLE001 — أصل واحد لا يُسقط البقيّة
            stats["macro_errors"] += 1
            db.note_error("last_error_macro", f"{recorded_at}: {label}: {_exc_note(exc)}")
        if i + 1 < len(config.MACRO_BARS):
            await sleep(config.BARS_PACING_SECONDS)
    # الختم بصفوف مكتوبة فعلاً فقط: فشل كامل (انقطاع fomo) أو فراغ كامل
    # (ردود بلا شموع) يُعاد في الدورة القادمة كبقيّة المصادر — لا نُرجئه
    # ساعة كاملة، والفراغ الكليّ يُسجَّل خطأً لئلا يمرّ صامتاً.
    if stats["macro_rows"] == 0:
        if stats["macro_errors"] == 0:
            db.set_meta(
                "last_error_macro",
                f"{recorded_at}: all {len(config.MACRO_BARS)} assets returned no data",
            )
        return stats
    db.set_meta("last_macro_bars_at", recorded_at)
    return stats


async def run_social_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """يلتقط الطبقة الاجتماعية لشريحة من المراقَبات (جدولة دوّارة كالشموع).

    العملة بلا نقاش تُسجَّل بأصفار لا تُتخطّى: **الصمت إشارة**، وسلسلة الأصفار
    ثمّ الارتفاع المفاجئ هي بالضبط ما نريد التقاطه.
    """
    stats = {"social_tokens": 0, "social_items": 0, "social_errors": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (now_dt - timedelta(seconds=config.SOCIAL_REFRESH_SECONDS)).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.SOCIAL_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.social_fetch_due(
        limit=config.SOCIAL_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
    )

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        try:
            raw = await _fetch_thesis_raw(client, addr, net)
            row = extract.extract_social(raw, addr, net, recorded_at)
            db.insert_social(row)
            stats["social_tokens"] += 1
            stats["social_items"] += row["thesis_total"]
            db.set_social_state(
                addr, net, "ok" if row["thesis_sampled"] else "empty",
                row["thesis_total"], recorded_at,
            )
        except Exception as exc:  # noqa: BLE001 — عملة واحدة لا تُسقط الشريحة
            stats["social_errors"] += 1
            try:
                db.set_social_state(addr, net, "error", 0, recorded_at)
            except Exception:  # noqa: BLE001 — كما في bars: لا تحجب الختمَ أدناه
                pass
            db.note_error("last_error_social", f"{recorded_at}: {_exc_note(exc)}")
        if i + 1 < len(due):
            await sleep(config.SOCIAL_PACING_SECONDS)
    return stats


# مصدرا الحائزين ثابتاً وحدَه لا مضمَّناً في الحلقة، ليُعرَف طولهما فيُحرَس النوم
# بعد آخر نداء: التمهّل فاصل **بين** النداءات، ونوم بعد آخرها يقتطع من فسحة
# الدورة بلا مقابل (نفس حرس `if i + 1 < len(due)` في دورتَي الشموع والسوشيال).
_HOLDERS_SOURCES: tuple[tuple[str, Any, Any], ...] = (
    ("token_details", _fetch_token_details_raw, extract.extract_token_details_holders),
    ("hodlers_top", _fetch_hodlers_raw, extract.extract_platform_holders),
)


async def run_holders_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """يقيس تركيز الملكية وتموضع الحشد زمنياً من مصدرين متكاملين.

    `market_ticks.top10_holders_pct` ميت (صفر من 1,430,475): قوائم
    trending/verified لا تحمل المفتاح أصلاً. وفحص السلسلة يغطّي Solana وحدها
    (2,929 من 3,044) وصامت تماماً عن EVM (صفر من 2,378) — فالتركيز مجهول
    لكل عملة إيثيريوم عندنا. المصدران هنا يعملان على الشبكتين ويقيسان شيئين
    مختلفين (مؤكَّد حيّاً 2026-08-09 لا مفترَضاً):

    - `tokenDetails`: تركّز السلسلة — `top10HoldersPercent` + `holders` على
      السلسلة كلّها (شوهد 83.6% و90.1% و21.9%). يعمل على EVM وSolana معاً
      — وهو الإصلاح المباشر للعمود الميّت.
    - `hodlers/top`: تموضع الحشد — مستخدمو fomo الحائزون (276 من 947؛ 118 من
      14,371) بلا أي نسبة من المعروض، لكن مع تكلفة كل مركز وربحه غير المحقّق
      ومدّة حمله وعلَم `isDev`. «هل حاملو المنصّة تحت الماء؟» سؤال مختلف عن
      «هل الملكية مركَّزة؟». قياس أوّليّ: 50 من 50 حائزاً خاسراً في عملة،
      مقابل 12 من 49 في أخرى.

    كلٌّ يُخزَّن بصفّه (`source` داخل المفتاح الأساسي) فلا يطمس أحدهما قياس
    الآخر، ولا نفبرك قيمة غائبة (FR-007). فشل مصدر لا يُسقط الثاني.

    وردّ `tokenDetails` يُستخرَج **مرّتين**: تركّز الملكية إلى `token_holders`،
    وتدفّق الشراء/البيع إلى `token_flow` — **جلب واحد، مستخرِجان، جدولان**.
    الردّ يحمل (100% في 300 ردّ مؤرشف) انقسام الشراء/البيع وطبقة 5 دقائق
    كاملة، وكنّا نرميها كلّها. صفر نداء إضافي. فشل أحد المستخرِجَين لا يُسقط
    الآخر: التدفّق في `try` خاصّ به.
    """
    stats = {
        "holders_tokens": 0, "holders_details": 0,
        "holders_top": 0, "holders_errors": 0, "flow_rows": 0,
    }
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (now_dt - timedelta(seconds=config.HOLDERS_REFRESH_SECONDS)).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.HOLDERS_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.holders_fetch_due(
        limit=config.HOLDERS_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
    )

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        first_seen = w["first_seen_at"]
        sig = w.get("entry_signal_id")
        is_control = int(w.get("is_control") or 0)
        top10: float | None = None
        got = 0

        for j, (source, fetch_fn, extract_fn) in enumerate(_HOLDERS_SOURCES):
            raw: Any = None  # يبقى None لو رمى الجلب — يُقرأ في فرع التدفّق أدناه
            try:
                raw = await fetch_fn(client, addr, net)
                row = extract_fn(raw, addr, net, recorded_at, first_seen, sig, is_control)
                if row is not None:
                    db.insert_holders(row)
                    got += 1
                    if source == "token_details":
                        stats["holders_details"] += 1
                    else:
                        stats["holders_top"] += 1
                    if top10 is None:
                        top10 = row["top10_pct"]
            except Exception as exc:  # noqa: BLE001 — مصدر واحد لا يُسقط الباقي
                stats["holders_errors"] += 1
                db.note_error(
                    "last_error_holders",
                    f"{recorded_at}: {source}: {_exc_note(exc)}",
                )
            # التدفّق من **نفس** الردّ: مستخرِج ثانٍ على مغلّف بين أيدينا، بلا
            # نداء إضافي. `try` مستقلّ حتى لا يُسقط أحد الجدولين الآخر، و**خارج**
            # فرع الحيازة عمداً: مستخرِج الحيازة يعيد None حين تغيب نسب الملكية،
            # فلو عُلِّق التدفّق عليه لابتُلع معها — وهو حاضر 100% بينما هي لا.
            if source == "token_details" and raw is not None:
                try:
                    frow = extract.extract_token_flow(
                        raw, addr, net, recorded_at, first_seen, sig, is_control
                    )
                    if frow is not None:
                        db.insert_flow(frow)
                        stats["flow_rows"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["holders_errors"] += 1
                    db.note_error(
                        "last_error_holders",
                        f"{recorded_at}: flow: {_exc_note(exc)}",
                    )
            # فاصل **بين** النداءات لا بعد آخرها.
            if i + 1 < len(due) or j + 1 < len(_HOLDERS_SOURCES):
                await sleep(config.HOLDERS_PACING_SECONDS)

        if got:
            stats["holders_tokens"] += 1
        db.set_holders_state(addr, net, "ok" if got else "error", top10, recorded_at)
    return stats


def _write_filter_static(
    db: RecorderDB, item: Any, addr: str, net: str, recorded_at: str
) -> bool:
    """يسدّ ثقب العمر من عنصر filterTokens. يعيد True إن كتب شيئاً.

    كان تاريخ الإنشاء يأتي من القوائم العامّة وحدها، والقائمة العامّة شرطها
    **الشعبيّة لا العمر**: 406 عملةً ظهرت في trending/verified/most_held فلها
    صفّ ثوابت، و177 لم تظهر ولا مرّة فلا صفَّ لها إطلاقاً — فصلٌ تامّ 100%
    مقيس. أي أنّ «العمر المجهول» لم يكن فئة عملاتٍ حديثةٍ بل فئة عملاتٍ غير
    رائجة (39.8% منها كانت < يومين لحظة أوّل استرجاع، مقابل 42.4% في
    معروفات العمر — بلا فرق).

    والحلّ لا يكلّف نداءً واحداً: filterTokens يُسأل عن **مراقَباتنا بالذات**
    كلَّ دورة، وشكل عنصره **مطابقٌ** لشكل عنصر trending (المفاتيح العليا
    والمفاتيح داخل `token` متطابقة) فيحمل `token.createdAt` — تغطية مقيسة
    215 من 216 مراقَبة نشطة، وصفر لم يُعَد. كان العنصر بين أيدينا في هذه
    الحلقة نفسها ونحن نطرحه.
    """
    net = str(net or "")
    if not db.static_exists(addr, net):
        st = extract.extract_token_static(item, recorded_at)
        if st is None:
            return False
        db.upsert_static(st)
        return True
    # الصفّ قائم والعمر مفقود (المنبع أغفله): أوّل إجابةٍ تحمله تسدّه.
    tok = item.get("token") if isinstance(item.get("token"), Mapping) else {}
    created = tok.get("createdAt") or item.get("createdAt")
    if created in (None, ""):
        return False
    return db.set_static_created_at(addr, net, str(created))


async def run_filter_tokens_cycle(
    client: Any,
    db: RecorderDB,
    recorded_at: str,
    watched: set[tuple[str, str]],
    captured: set[tuple[str, str]],
    sleep=asyncio.sleep,
) -> dict[str, int]:
    """يقيس المراقَبات التي **لم تلتقطها** trending/verified في هذه الدورة.

    القائمتان العامّتان تقيسان ما هو رائج، ونحن نراقب ما أشارت إليه الإشارة —
    والمجموعتان تفترقان بسرعة. مقيس على القاعدة الحيّة: 53 من 189 مراقَبة نشطة
    بلا لقطة منذ ساعتين، و35 لم تُقَس ولا مرّة. `filterTokens` يطلب عناويننا
    بالاسم فيرجعها بلا اعتماد على شعبيّتها.

    قيود مقيسة حيّاً لا مفترضة:
    - الربط **بالعنوان لا بالترتيب**: كل عنصر يحمل `token.address`، والمصدر
      **يحذف الميّت بصمت** (5 من 6 رجعت) — فالفهرس ينزلق والترتيب يكذب.
    - **بلا `insert_snapshot`**: طلبنا مراقَباتنا بالذات فكل عنصر يصير صفّاً
      يحمل `raw_json` الخاص به؛ اللقطة تكرار محض (~94 MB/يوم بلا فائدة).
      بخلاف trending/verified حيث اللقطة تحفظ غير المراقَب أيضاً.
    - أعمدة العدّ/الفريد الـ18 تبقى `NULL` من هذا المصدر — هذا هو الصواب
      (FR-007)، ودمج المصادر في `features.market_features` هو ما يمنع هذا
      النقص من طمس قياس أغنى جاء من trending.
    """
    stats = {"filter_requested": 0, "filter_ticks": 0, "filter_errors": 0,
             "filter_static": 0}
    missing = sorted(watched - captured)
    if not missing:
        return stats

    for start in range(0, len(missing), config.FILTER_TOKENS_BATCH):
        batch = missing[start : start + config.FILTER_TOKENS_BATCH]
        symbols = [f"{addr}:{net}" for addr, net in batch]
        stats["filter_requested"] += len(symbols)
        try:
            raw = await _fetch_filter_tokens_raw(client, symbols)
            items = extract.unwrap_token_list(raw)
            # خريطة العنوان → عنصر. العنوان يعود بحالة أحرف قد تخالف المخزَّنة
            # (EVM checksummed)، فالمفتاح صغيرٌ كلّه على الطرفين.
            by_addr: dict[str, Any] = {}
            for item in items:
                a = extract.token_list_address(item)
                if a:
                    by_addr[a.lower()] = item
            with db.batch():
                for addr, _net in batch:
                    item = by_addr.get(addr.lower())
                    if item is None:
                        continue  # حُذف بصمت (عملة مشطوبة) — لا يكسر الدفعة
                    tick = extract.extract_market_tick(item, recorded_at, "filter")
                    if tick is None:
                        continue
                    if db.insert_tick(tick):
                        stats["filter_ticks"] += 1
                    # `dex_protocol` غائب تماماً من خام trending (0 من 3,000)
                    # وهذا مصدره الوحيد — نملأه حين يكون العمود فارغاً فقط.
                    proto = extract.filter_item_protocol(item)
                    if proto:
                        db.set_static_protocol(tick["token_address"],
                                               str(tick["network_id"] or ""), proto)
                    if _write_filter_static(
                        db, item, tick["token_address"],
                        str(tick["network_id"] or ""), recorded_at,
                    ):
                        stats["filter_static"] += 1
        except Exception as exc:  # noqa: BLE001 — دفعة واحدة لا تُسقط الباقي
            stats["filter_errors"] += 1
            db.note_error(
                "last_error_filter",
                f"{recorded_at}: {_exc_note(exc)}",
            )
        # فاصل **بين** الدفعات لا بعد آخرها. وفي الحالة الغالبة (≈53 عنواناً ⇒
        # دفعة واحدة) كان هذا النوم كلّه ضائعاً بلا نداء بعده.
        if start + config.FILTER_TOKENS_BATCH < len(missing):
            await sleep(config.FILTER_TOKENS_PACING_SECONDS)
    return stats


async def run_traders_cycle(
    client: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """يبني ملفّات المشترين الذين تتكرّر أسماؤهم في إشاراتنا.

    `signal_events.buyer_id` مخزَّن منذ اليوم الأوّل ولا جدول تجّار في القاعدة:
    5,572 معرّفاً مميّزاً، **3,202 منهم بـ≥3 أحداث**. فالسؤال «هل هذا المشتري
    ماهر أم يشتري كل شيء؟» بقي بلا جواب رغم أنّ الجواب على بُعد نداء واحد.

    الجدولة دوّارة كدورة الحائزين: غير المجلوب قطّ أوّلاً، ثم الأقدم جلباً،
    ثم الأكثر أحداثاً. `INSERT OR REPLACE` عمداً (بخلاف كل الجداول الأخرى):
    الملفّ **يتغيّر** — المتابعون ومدّة الحمل ليست ثوابت.
    """
    stats = {
        "traders_fetched": 0, "traders_rows": 0, "traders_errors": 0,
        "traders_missing": 0, "traders_malformed": 0,
    }
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.TRADERS_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.TRADERS_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.traders_fetch_due(
        limit=config.TRADERS_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        min_events=config.TRADERS_MIN_EVENTS,
    )

    def _seal() -> dict[str, int]:
        """ختمُ آخرِ الدورة: حرسُ الحصار ثمّ ختمُ النجاح. مخرجان يشتركان فيه.

        سُئل عددٌ ولم ينزل صفٌّ: مقبولٌ مرّةً، عطلٌ إن تكرّر — `_note_shutout`.
        ولا يُحاسب هذا على الأخطاء: الخطأُ يكتب سطرَه بنفسه، والحصارُ صامتٌ بطبعه.

        وختمُ النجاح شرطُه **نزولُ صفٍّ** (أو ألّا يُسأل أحد)، بخلاف قاعدة
        `chain_last_ok_at` «صفرُ أخطاءٍ ⇒ ختم». لأنّ الحصار هو بالضبط صفرُ أخطاءٍ
        مع صفرِ صفوف: ختمٌ هنا كان سيُعلن خطأَ الحصار «متعافياً» بعد دقيقة، فيموت
        الحرسُ بنفس الصحّةِ الكاذبة التي أوجدته. و«لم يُسأل أحد» نجاحٌ حقيقيّ لا
        خمود — لا شيء استحقّ الجلب فلا شيء فشل — وبلا ختمٍ له يبقى طابورٌ هادئ
        أحمرَ إلى الأبد على خطأٍ قد شُفي.
        """
        if not stats["traders_errors"]:
            streak = _note_shutout(
                db, "traders", recorded_at,
                stats["traders_fetched"], stats["traders_rows"],
            )
            if streak == 0:
                db.set_meta("traders_last_ok_at", recorded_at)
        return stats

    # معرّفٌ فاسدُ الشكل يُسقط الرزمةَ كلَّها (400 على العنصر الفاسد وحده)، فلا
    # يجوز أن يكلّف معرّفٌ واحدٌ رديء التسعةَ والتسعين الباقين. و`unsupported`
    # لا `error` لأنّه لن يصلح بإعادة المحاولة: استعلامُ الاستحقاق يستبعده نهائيّاً.
    wanted, malformed = [], []
    for w in due:
        (wanted if _UUID_RE.match(str(w["trader_id"] or "")) else malformed).append(
            str(w["trader_id"])
        )
    for tid in malformed:
        db.set_trader_state(tid, "unsupported", recorded_at)
        stats["traders_malformed"] += 1
    if not wanted:
        return _seal()

    cap = max(1, config.TRADERS_BATCH_MAX)
    for start in range(0, len(wanted), cap):
        chunk = wanted[start : start + cap]
        try:
            raw = await _fetch_traders_raw(client, chunk)
            stats["traders_fetched"] += len(chunk)
            rows = extract.extract_traders(raw, recorded_at) if raw is not None else {}
            for tid in chunk:
                row = rows.get(tid)
                if row is None:
                    # غائبٌ عن الردّ = لا مستخدمَ بهذا المعرّف. المصدرُ يحذف
                    # المجهولَ بصمتٍ ولا يخطئ به، فهذا جوابٌ لا فشل.
                    db.set_trader_state(tid, "empty", recorded_at)
                    stats["traders_missing"] += 1
                    continue
                db.upsert_trader(row)
                stats["traders_rows"] += 1
                db.set_trader_state(tid, "ok", recorded_at)
        except Exception as exc:  # noqa: BLE001 — رزمةٌ واحدة لا تُسقط الدورة
            stats["traders_errors"] += 1
            db.note_error("last_error_traders", f"{recorded_at}: {_exc_note(exc)}")
            # ولا حالةَ تُكتب لمن سقطت رزمتُه: الحالةُ تعني «سألنا وهذا الجواب»،
            # وكتابة `error` هنا تدفعه إلى مهلة الانتظار الطويلة على ذنبِ الشبكة.
        if start + cap < len(wanted):
            await sleep(config.TRADERS_PACING_SECONDS)
    return _seal()


async def run_cycle(
    client: Any,
    db: RecorderDB,
    lb: LeaderboardCache,
    now_mono: float | None = None,
) -> dict[str, int]:
    """دورة واحدة. يعيد عدّادات ملخّصة. يبتلع أخطاء كل مصدر على حدة."""
    now_mono = now_mono if now_mono is not None else time.monotonic()
    recorded_at = utcnow_iso()
    stats = {
        "signals": 0, "watch_added": 0, "comparison_signal_added": 0,
        "control_added": 0, "ticks": 0, "static": 0,
        "bars_tokens": 0, "bars_rows": 0, "social_tokens": 0, "social_items": 0,
        "holders_tokens": 0, "holders_details": 0, "holders_top": 0,
        "flow_rows": 0, "filter_requested": 0, "filter_ticks": 0,
        "filter_static": 0,
        "traders_rows": 0,
        "evm_admission_backlog": 0, "evm_admission_percent": 100,
        "evm_admission_paused": 0, "evm_admission_deferred": 0,
        "age_rejected": 0, "age_unknown": 0, "age_resolved": 0,
        "age_lookup_failed": 0,
        "macro_rows": 0, "macro_no_data": 0, "errors": 0,
    }

    try:
        admission_policy = evm_admission_policy(db)
        stats["evm_admission_backlog"] = admission_policy.backlog
        stats["evm_admission_percent"] = admission_policy.percent
        stats["evm_admission_paused"] = 1 if admission_policy.paused else 0
    except Exception as exc:  # noqa: BLE001 - admission must fail closed for EVM
        _fail_policy_networks = frozenset(str(n) for n in config.EVM_NETWORKS)
        admission_policy = _fail_closed_evm_admission(_fail_policy_networks)
        stats["evm_admission_backlog"] = -1
        stats["evm_admission_percent"] = 0
        stats["evm_admission_paused"] = 1
        db.note_error("last_error_evm_admission", f"{recorded_at}: {_exc_note(exc)}")

    def _fail(where: str, exc: Exception) -> None:
        stats["errors"] += 1
        # **معالج الخطأ يحتاج القاعدة التي هي المورد المتعطّل.** قِيس
        # 2026-08-17: `database is locked` في `insert_holders` رفع الاستثناء
        # هنا أيضاً، فأسقط `_fail` ثم أسقط درع الحلقة، فخرجت العمليّة بالرمز 1
        # وبقيت المهمّة `Ready` ثلاث ساعات صامتة. العدّ في الذاكرة يكفي لتُكمل
        # الدورة وتُبلّغ؛ فقدُ سطرٍ في `meta` أرخص من فقد المسجّل ساعات.
        try:
            db.bump_counter("errors_total")
        except Exception:  # noqa: BLE001 — عدّادٌ لا قياس
            pass
        note = f"{recorded_at}: {_exc_note(exc)}"
        if not db.note_error(f"last_error_{where}", note):
            # لا نصمت تماماً: السجلّ آخرُ ما يبقى حين تُقفل القاعدة، و`_log`
            # نفسها محميّة فلا تُسقط الدورة.
            _log(f"note_error failed for {where}: القاعدة لا تستجيب للكتابة")

    # 0) تحديث صدارة المتصدّرين (كل ساعة) + أرشفة الخام + تسجيل الفشل.
    try:
        await refresh_leaderboard(lb, db, now_mono, recorded_at)
    except Exception as exc:  # noqa: BLE001 — لا نُفشل الدورة
        _fail("leaderboard", exc)

    # 1) الـ feed الخام → signal_events + watchlist للمُشغّلات.
    admitted_signals: set[str] = set()
    try:
        raw_feed = await _fetch_feed_raw(client)
        if raw_feed is not None:
            admitted_signals = await record_feed(
                db, raw_feed, recorded_at, lb.lookup, lb.lookups,
                admission_policy, stats, client,
            )
    except Exception as exc:  # noqa: BLE001
        _fail("feed", exc)

    # 2) trending + verified + mostHeld الخام → snapshots + market_ticks + token_static.
    # `mostHeld` قائمة اكتشاف ثالثة: مقيس أنّها تعطي 25 عنصراً منها **6 لم نكن
    # نراها** في القائمتين الأخريين، وتتقاطع معهما في 18 مفتاحاً ⇒ نفس المستخرِج
    # يكفي، وتدخل تلقائياً في المرشّحين والضابطة وthe token_static بلا كود جديد.
    watched = {(w["token_address"], str(w["network_id"] or "")) for w in db.active_watches()}
    # المفاتيح الملقوطة في هذه الدورة — ما يتبقّى منها تسدّه دورة filterTokens.
    captured: set[tuple[str, str]] = set()
    # مرشّحو المجموعة الضابطة: كل عملة نراها في هذه الدورة ولم تدخل من قبل.
    # نجمعها هنا مجّاناً — البيانات في اليد أصلاً، فلا نداء شبكة إضافيّ.
    control_candidates: list[tuple[str, str, float | None, str]] = []
    for source, fetch in (
        ("trending", _fetch_trending_raw),
        ("verified", _fetch_verified_raw),
        ("most_held", _fetch_most_held_raw),
    ):
        try:
            raw = await fetch(client)
            if raw is None:
                continue
            with db.batch():  # ~65 tick في الدورة → تثبيت واحد بدل 65
                db.insert_snapshot(source, raw, recorded_at)
                items = extract.unwrap_token_list(raw)
                for item in items:
                    tick = extract.extract_market_tick(item, recorded_at, source)
                    if tick is None:
                        continue
                    key = (tick["token_address"], str(tick["network_id"] or ""))
                    control_candidates.append(
                        (key[0], key[1], tick.get("price_usd"), source)
                    )
                    # نسجّل tick لكل عملة مراقَبة (المصدر الأساسي للسلسلة الزمنية).
                    # نسجّل أيضاً الثوابت لكل عملة نراها لأول مرّة إن كانت مراقَبة.
                    if key in watched:
                        captured.add(key)
                        if db.insert_tick(tick):
                            stats["ticks"] += 1
                        if not db.static_exists(
                            tick["token_address"], str(tick["network_id"] or "")
                        ):
                            st = extract.extract_token_static(item, recorded_at)
                            if st is not None:
                                db.upsert_static(st)
                                stats["static"] += 1
        except Exception as exc:  # noqa: BLE001
            _fail(source, exc)

    # 2.3) سدّ فجوة القياس: المراقَبات التي لم تلتقطها أي قائمة في هذه الدورة.
    # بلا هذه الخطوة تتوقّف العملة عن القياس لحظة سقوطها من القوائم العامّة —
    # وهي لا تزال داخل نافذة الـ48 ساعة التي نزعم أنّنا نقيسها (53 من 189).
    try:
        filt = await run_filter_tokens_cycle(client, db, recorded_at, watched, captured)
        stats["filter_requested"] = filt["filter_requested"]
        stats["filter_ticks"] = filt["filter_ticks"]
        stats["filter_static"] = filt["filter_static"]
        if filt["filter_errors"]:
            stats["errors"] += filt["filter_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("filter", exc)

    # 2.2) نوافذ إشارة من الكون نفسه وسعر السوق نفسه المستخدم للضابطة.
    try:
        stats["comparison_signal_added"] = admit_signal_comparison_windows(
            db, control_candidates, recorded_at, policy=admission_policy,
            admitted_signals=admitted_signals,
        )
    except Exception as exc:  # noqa: BLE001
        _fail("comparison_signal", exc)

    # 2.25) الضابطة تُقبل في الدورة نفسها التي قبلت إشارة مقارنة فقط. السماح
    # بملء 40 ضابطة بعد إشارة قديمة واحدة يعيد اختلال الزمن الذي نريد منعه.
    if stats["comparison_signal_added"]:
        try:
            stats["control_added"] = admit_control_sample(
                db, control_candidates, recorded_at, policy=admission_policy,
            )
        except Exception as exc:  # noqa: BLE001 — الضابطة إضافة، لا تُسقط الدورة
            _fail("control", exc)

    # 2.42) تركيز الملكية من المصدرين — تركيز عالٍ = خطر تصريف، وهو مجهول اليوم
    # لكل عملة EVM عندنا.
    try:
        hold = await run_holders_cycle(client, db, recorded_at)
        for key in ("holders_tokens", "holders_details", "holders_top", "flow_rows"):
            stats[key] = hold[key]
        if hold["holders_errors"]:
            stats["errors"] += hold["holders_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("holders", exc)

    # 2.45) ملفّات المشترين المتكرّرين — «من اشترى؟» كان سؤالاً بلا جواب رغم أنّ
    # buyer_id مخزَّن في كل حدث منذ اليوم الأوّل.
    try:
        trd = await run_traders_cycle(client, db, recorded_at)
        stats["traders_rows"] = trd["traders_rows"]
        if trd["traders_errors"]:
            stats["errors"] += trd["traders_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("traders", exc)

    # 2.5) شموع OHLCV لشريحة من المراقَبات (مصدر الحقيقة السعرية للتوسيم).
    try:
        bars = await run_bars_cycle(client, db, recorded_at)
        stats["bars_tokens"] = bars["bars_tokens"]
        stats["bars_rows"] = bars["bars_rows"]
        if bars["bars_errors"]:
            stats["errors"] += bars["bars_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("bars", exc)

    # 2.75) الطبقة الاجتماعية لشريحة من المراقَبات.
    try:
        soc = await run_social_cycle(client, db, recorded_at)
        stats["social_tokens"] = soc["social_tokens"]
        stats["social_items"] = soc["social_items"]
        if soc["social_errors"]:
            stats["errors"] += soc["social_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("social", exc)

    # 2.9) شموع السوق الكلّي (مرجع النظام السوقي — مرّة كل ساعة).
    try:
        mac = await run_macro_bars_cycle(client, db, recorded_at)
        stats["macro_rows"] = mac["macro_rows"]
        stats["macro_no_data"] = mac["macro_no_data"]
        if mac["macro_errors"]:
            stats["errors"] += mac["macro_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("macro", exc)

    # 3) تنظيف watchlist: تعطيل ما تجاوز 48 ساعة.
    try:
        db.deactivate_expired(recorded_at)
        # حذف اللقطات القديمة — معطّل افتراضياً (0 = احتفاظ أبديّ).
        if config.SNAPSHOT_RETENTION_DAYS > 0:
            cutoff = (
                datetime.fromisoformat(recorded_at)
                - timedelta(days=config.SNAPSHOT_RETENTION_DAYS)
            ).isoformat()
            pruned = db.prune_snapshots(cutoff)
            if pruned:
                db.bump_counter("snapshots_pruned_total", pruned)
    except Exception as exc:  # noqa: BLE001
        _fail("cleanup", exc)

    db.set_meta("last_cycle_at", recorded_at)
    db.set_meta("evm_admission_last_state", json.dumps({
        "at": recorded_at,
        "backlog": stats["evm_admission_backlog"],
        "percent": stats["evm_admission_percent"],
        "paused": bool(stats["evm_admission_paused"]),
        "deferred": stats["evm_admission_deferred"],
    }, sort_keys=True))
    db.set_meta("last_cycle_stats", str(stats))
    # دورة نجحت كلياً (بلا أي خطأ مصدر) → ختم يُبطل أخطاء meta الأقدم منه في اللوحة.
    if stats["errors"] == 0:
        db.set_meta("last_ok_cycle_at", recorded_at)
    db.bump_counter("cycles_total")
    return stats


async def _maybe_rotate_client(
    client: Any, current_token: str, lb: LeaderboardCache, db: RecorderDB
) -> tuple[Any, str]:
    """يلتقط التوكن المتجدّد من القرص كل دورة.

    توكن fomo عمره 60 دقيقة؛ خادم الـ api يكتب توكناً طازجاً إلى القرص قبل انتهائه.
    نقرأ القرص، فإن تغيّر التوكن أعدنا بناء FomoClient (وأغلقنا القديم) ووجّهنا
    الكاش إلى العميل الجديد. فشل القراءة لا يُسقط الدورة — نُكمل بالعميل الحالي.
    لا يُطبع أي قيمة توكن إطلاقاً (FR-013).

    يعيد (client, token) المستعملَين للدورة القادمة.
    """
    try:
        disk_token = _load_access_token()
    except Exception as exc:  # noqa: BLE001 — قراءة القرص فشلت؛ نكمل بالحالي
        db.note_error("last_error_token_reload", f"{utcnow_iso()}: {type(exc).__name__}")
        return client, current_token
    if disk_token == current_token:
        return client, current_token
    # تدوّر التوكن: أنشئ عميلاً جديداً وأغلق القديم بنظافة.
    new_client = _build_client(disk_token)
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 — إغلاق العميل القديم لا يُسقط المسجّل
        pass
    lb.set_client(new_client)
    try:
        db.set_meta("last_token_refresh_at", utcnow_iso())
    except Exception:  # noqa: BLE001 — التدوير نجح فعلاً، لا نهدره لأجل ختم
        pass
    _log("token rotated → client rebuilt")  # بلا أي قيمة سرّية
    return new_client, disk_token


# مفاتيحُ الثمرة: صفوفٌ كُتبت فعلاً. و`filter_requested` ليست منها — هي عدُّ
# محاولاتٍ يرتفع 108→190 وقتَ الحجب بالضبط (مقيس 2026-08-19T14:55)، فلو حُسبت
# ثمرةً لأعمَت القاطعَ عن الحجب الذي وُضع له.
_PRODUCTIVE_KEYS = (
    "signals", "ticks", "filter_ticks", "bars_rows", "social_items",
    "holders_details", "holders_top", "flow_rows", "traders_rows", "macro_rows",
)


def _cycle_is_dead(stats: dict[str, int]) -> bool:
    """دورةٌ أخطأت ولم تكتب صفّاً واحداً — لا «دورةٌ ضعيفة» ولا «دورةٌ بأخطاء».

    الشرطان معاً مقصودان: أخطاءٌ بلا ثمرة تعني أنّ المصدرَ لم يُعطِ شيئاً، أمّا
    ثمرةٌ مع أخطاء فهي حالةُ 274 دورةٍ من 375 السويّة ولا يجوز أن تُبطئ الجمع.
    """
    return stats.get("errors", 0) > 0 and not any(
        stats.get(key, 0) for key in _PRODUCTIVE_KEYS
    )


def _breaker_wait(dead_streak: int) -> float:
    """انتظارُ ما بين مِجَسّين: يتضاعف من دورةٍ واحدة إلى السقف.

    مسقوفٌ مرّتين — بالثواني وبالأُسِّ نفسه: `2.0 ** 1024` يرفع OverflowError،
    وحجبٌ يطول أسبوعاً يبلغ ذلك الأُسّ، فينهار الدرعُ كلَّ دورةٍ إلى الأبد.
    ومقياسُ الأساس `CYCLE_SECONDS`، فاختبارٌ يصفّره يُلغي الانتظار من نفسه.
    """
    return min(
        float(config.UPSTREAM_BREAKER_MAX_SECONDS),
        config.CYCLE_SECONDS
        * 2.0 ** min(dead_streak - config.UPSTREAM_BREAKER_AFTER, 16),
    )


async def _upstream_alive(client: Any, reason: list[str] | None = None) -> bool:
    """مِجَسٌّ واحد: هل يردّ المصدر أصلاً؟ لا يرفع، ولا يكتب، ولا يُحصى.

    `limit=1` لأنّ المطلوب حالةُ الطريق لا حمولتُه — أمّا `feedTypes` فشرطٌ
    لازم: قِيس 2026-08-20T00:24Z بحسابٍ سليم أنّ `/feed?limit=1` وحده يردّ
    **400** «Invalid input: query.feedTypes - Required»، و`_get` يترجم كلَّ
    ≥400 إلى «غير متاح». فكان المِجَسُّ يقرأ الطريقَ ميتاً وهو حيّ، أي أنّ
    القاطعَ يُغلق ولا يُفتح أبداً ولو رُفع الحجبُ عن الحساب — وشهد السجلُّ:
    أحدَ عشرَ إغلاقاً ولا استئنافَ واحداً إلّا بإعادة تشغيل.

    ولذلك حُكمان لا واحد: النداءُ يحمل شرطَه، ثمّ **جوابُ التطبيق بذاته حياة**
    — فرمزٌ في 4xx غيرُ 403 و401 يعني أنّ الطلبَ وصل وفُحص، وذلك كلُّ ما
    يسأل عنه القاطع. فلو أضاف المصدرُ شرطاً آخر غداً لم يَقتُل الدرعُ الجمعَ
    مرّةً ثانية. و403 وحده حجبُ هويّة، و401 توكنٌ باطل، وكلاهما لا يُصلحه
    تشغيلُ الدورة.

    و`reason` مَخرَجٌ اختياريّ لا قيمةُ رجوع: «محجوب» و«منقطع» يتساويان في قرار
    الانتظار ويختلفان كلَّ الاختلاف في العلاج — 403 على الهويّة يُحَلّ بحساب،
    والانقطاع يُحَلّ بالصبر. فمن ردَّ الاستثناءَ نصّاً هنا كتبته الحلقةُ في
    `meta` فقرأته اللوحة، ومن جعله قيمةَ رجوعٍ كسر `is False` في الاختبار.
    """
    from fomo_api.config import settings

    params = {"feedTypes": list(config.FEED_TYPES), "limit": 1}
    try:
        await client._get(settings.upstream_feed_path, params)
        return True
    except Exception as exc:  # noqa: BLE001 — الفشلُ **هو** الجواب المطلوب
        details = getattr(exc, "details", None)
        status = details.get("upstream_status") if isinstance(details, dict) else None
        if isinstance(status, int) and 400 <= status < 500 and status not in (401, 403):
            return True                    # جوابُ التطبيق حياةٌ لا موت
        if reason is not None:
            reason.append(_exc_note(exc))
        return False


async def main_loop(cycles: int | None = None) -> None:
    """يشغّل الحلقة إلى ما لا نهاية (cycles=None) أو عدداً محدّداً (للتحقّق)."""
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    current_token = _load_access_token()
    client = _build_client(current_token)
    lb = LeaderboardCache(
        client,
        size=config.LEADERBOARD_SIZE,
        refresh_seconds=config.LEADERBOARD_REFRESH_SECONDS,
        periods=config.LEADERBOARD_PERIODS,
        pacing_seconds=config.LEADERBOARD_PACING_SECONDS,
    )
    db.set_meta("schema_version", "1")
    # يوثّق أنّ raw_json يُكتب مضغوطاً — أي قارئ لاحق يمرّ عبر db.decode_raw.
    db.set_meta("raw_encoding", "zlib")
    db.set_meta("started_at", utcnow_iso())
    n = 0
    dead_streak = 0
    try:
        while cycles is None or n < cycles:
            started = time.monotonic()
            try:
                # التقط التوكن المتجدّد على القرص قبل الدورة (يمنع 401 بعد
                # الساعة). **داخل** الدرع: كان خارجه وهو يكتب في القاعدة، فقفلٌ
                # هناك يخرج من الحلقة كلها بلا أي معالجة.
                client, current_token = await _maybe_rotate_client(
                    client, current_token, lb, db
                )
                # قاطعُ الحجب — **بعد** التدوير: التوكن يبقى طازجاً وإن لم
                # نَدُر دورةً واحدة، فحجبٌ طويل لا يورّث 401 عند انتهائه.
                # مِجَسٌّ واحد بدل دورةٍ كاملة، وانتظارٌ يتضاعف؛ وأوّلُ نجاحٍ
                # يُعيد كلَّ شيء في الحال فيبقى انقطاعُ الدقيقة دقيقةً واحدة.
                if dead_streak >= config.UPSTREAM_BREAKER_AFTER:
                    why: list[str] = []
                    if await _upstream_alive(client, why):
                        _log(f"upstream back after {dead_streak} dead cycles")
                        dead_streak = 0
                    else:
                        wait = _breaker_wait(dead_streak)
                        # سببُ الرفض في السطر نفسه: هذا السطرُ هو ما يُقرأ طولَ
                        # الحجب، وحجبُ 403 لا يُشبه انقطاعاً في شيءٍ إلا الانتظار.
                        cause = why[0] if why else "لا جواب"
                        _log(
                            f"upstream blocked ({dead_streak} dead cycles) — "
                            f"{cause} — skipping cycle, next probe in {wait:.0f}s"
                        )
                        db.note_error(
                            "last_error_upstream_blocked",
                            f"{utcnow_iso()}: مِجَسّ /feed رفض — {cause} — "
                            f"{dead_streak} دورة ميتة، انتظار {wait:.0f}ث",
                        )
                        dead_streak += 1
                        n += 1
                        await asyncio.sleep(wait)
                        continue
                stats = await run_cycle(client, db, lb, now_mono=started)
                # الجفافُ يُعَدّ هنا لا في الدرع: دورةٌ انهارت كلّياً يعالجها
                # الدرع، وهذه دورةٌ **نجحت** في الجريان وفشلت في الجمع.
                dead_streak = dead_streak + 1 if _cycle_is_dead(stats) else 0
                _log(f"cycle {n}: {stats}")
            except Exception:  # noqa: BLE001 — درع أخير حول الدورة كلها
                _log("cycle crashed:\n" + traceback.format_exc())
                # اتصالٌ واحد عمره عمر العملية: إن عَلِق بمعاملةٍ مفتوحة أو
                # بلقطة قراءةٍ سُبقت، بقيت كلّ دورةٍ تالية تنهار كما انهارت هذه
                # — 22 دقيقة و40 ثانية صامتة في 2026-08-19 حتى إعادةٍ يدويّة.
                # فالإنقاذ هنا: بعد الانهيار، وقبل عدّاده وقبل الدورة القادمة.
                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
                # والدرع لا يجوز أن يموت بيده: هذا السطر بعينه أخرج العمليّة
                # بالرمز 1 عند 2026-08-17T16:27 لأنّ القاعدة كانت مقفلة.
                try:
                    db.bump_counter("cycle_crashes")
                except Exception as meta_exc:  # noqa: BLE001
                    _log(f"cycle_crashes write failed: {type(meta_exc).__name__}")
            n += 1
            if cycles is not None and n >= cycles:
                break
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, config.CYCLE_SECONDS - elapsed))
    finally:
        await client.aclose()
        db.close()


def _log(msg: str) -> None:
    """يكتب سطراً للسجلّ مع ختم زمني. الفشل في الكتابة لا يُسقط المسجّل.

    يُدوّر الملف عند تجاوز LOG_MAX_BYTES (سطر/دقيقة يعني نموّاً أبدياً بلا ذلك)؛
    نحتفظ بنسخة واحدة `.1` فقط — السجلّ تشخيصيّ لا أرشيفيّ.
    """
    line = f"{utcnow_iso()} {msg}\n"
    try:
        import os

        if config.LOG_MAX_BYTES > 0 and os.path.getsize(config.LOG_PATH) > config.LOG_MAX_BYTES:
            os.replace(config.LOG_PATH, config.LOG_PATH + ".1")
    except OSError:
        pass  # الملف غير موجود بعد أو مقفل — الكتابة أدناه تتكفّل
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — الكتابةُ في السجلّ لا تُسقط ما تُسجّله
        pass


if __name__ == "__main__":
    import sys

    _cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        _cycles = int(sys.argv[1])
    asyncio.run(main_loop(cycles=_cycles))
