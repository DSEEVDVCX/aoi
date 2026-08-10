"""الحلقة الرئيسية لمسجّل البيانات التاريخي.

يقرأ الخام من fomo عبر FomoClient._get/_post مباشرة (قبل أي تعيين، لأن التعيين
يُسقط أثمن الحقول)، ويكتب إلى recorder.db. كل نداء upstream داخل try/except:
الفشل (502...) يُسجَّل في meta ويُتخطّى — الحلقة لا تموت أبداً.

read-only (FR-012): كل النداءات GET/POST قراءة فقط (feed، trending، verified،
leaderboard). لا نداء يكتب حالة حساب أو تداول.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
import traceback
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import config
import extract
from db import RecorderDB, utcnow_iso
from leaderboard_cache import LeaderboardCache


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


async def _fetch_trader_raw(client: Any, trader_id: str) -> Any:
    """خام GET /v2/users/{id} — ملفّ المتداول.

    نتجاوز `get_trader` لأنّها تُعيّن وتُسقط حقولاً؛ نريد المغلّف كاملاً
    للأرشيف. مقيس حيّاً: 26 مفتاحاً في `responseObject`.
    """
    from fomo_api.config import settings

    return await client._get(settings.upstream_trader_path.format(trader_id=trader_id))


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
            db.set_bars_state(addr, net, "error", 0, recorded_at)
            db.set_meta("last_error_bars", f"{recorded_at}: {type(exc).__name__}: {exc}")
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
            pick = rng.choice(eligible_by_network[network])
            eligible_by_network[network].remove(pick)
            picks.append(pick)
            existing[network] = existing.get(network, 0) + 1
    else:
        picks = rng.sample(pool, pick_count)
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
        price, admission_source = admission
        if db.add_signal_comparison_window(
            key[0], key[1], row["signal_type"], row["id"],
            config.WATCH_HOURS, recorded_at, price,
            config.CONTROL_DESIGN_VERSION, admission_source,
        ):
            added += 1
    return added


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
            db.set_meta("last_error_macro", f"{recorded_at}: {label}: {type(exc).__name__}: {exc}")
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
    due = db.social_fetch_due(limit=config.SOCIAL_PER_CYCLE, stale_before_iso=stale_before)

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
            db.set_social_state(addr, net, "error", 0, recorded_at)
            db.set_meta("last_error_social", f"{recorded_at}: {type(exc).__name__}: {exc}")
        if i + 1 < len(due):
            await sleep(config.SOCIAL_PACING_SECONDS)
    return stats


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

        for source, fetch_fn, extract_fn in (
            ("token_details", _fetch_token_details_raw, extract.extract_token_details_holders),
            ("hodlers_top", _fetch_hodlers_raw, extract.extract_platform_holders),
        ):
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
                db.set_meta(
                    "last_error_holders",
                    f"{recorded_at}: {source}: {type(exc).__name__}: {exc}",
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
                    db.set_meta(
                        "last_error_holders",
                        f"{recorded_at}: flow: {type(exc).__name__}: {exc}",
                    )
            await sleep(config.HOLDERS_PACING_SECONDS)

        if got:
            stats["holders_tokens"] += 1
        db.set_holders_state(addr, net, "ok" if got else "error", top10, recorded_at)
    return stats


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
    stats = {"filter_requested": 0, "filter_ticks": 0, "filter_errors": 0}
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
                for addr, net in batch:
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
        except Exception as exc:  # noqa: BLE001 — دفعة واحدة لا تُسقط الباقي
            stats["filter_errors"] += 1
            db.set_meta(
                "last_error_filter",
                f"{recorded_at}: {type(exc).__name__}: {exc}",
            )
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
    stats = {"traders_fetched": 0, "traders_rows": 0, "traders_errors": 0}
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

    for w in due:
        tid = w["trader_id"]
        status = "error"
        try:
            raw = await _fetch_trader_raw(client, tid)
            stats["traders_fetched"] += 1
            if raw is None:
                status = "empty"  # 404: حساب محذوف — لا نعيد المحاولة سريعاً
            else:
                row = extract.extract_trader(raw, tid, recorded_at)
                if row is None:
                    status = "empty"
                else:
                    db.upsert_trader(row)
                    stats["traders_rows"] += 1
                    status = "ok"
        except Exception as exc:  # noqa: BLE001 — متداول واحد لا يُسقط الباقي
            stats["traders_errors"] += 1
            db.set_meta(
                "last_error_traders",
                f"{recorded_at}: {type(exc).__name__}: {exc}",
            )
        db.set_trader_state(tid, status, recorded_at)
        await sleep(config.TRADERS_PACING_SECONDS)
    return stats


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
        "traders_rows": 0,
        "macro_rows": 0, "macro_no_data": 0, "errors": 0,
    }

    def _fail(where: str, exc: Exception) -> None:
        stats["errors"] += 1
        db.bump_counter("errors_total")
        db.set_meta(f"last_error_{where}", f"{recorded_at}: {type(exc).__name__}: {exc}")

    # 0) تحديث صدارة المتصدّرين (كل ساعة) + أرشفة الخام + تسجيل الفشل.
    try:
        await refresh_leaderboard(lb, db, now_mono, recorded_at)
    except Exception as exc:  # noqa: BLE001 — لا نُفشل الدورة
        _fail("leaderboard", exc)

    # 1) الـ feed الخام → signal_events + watchlist للمُشغّلات.
    try:
        raw_feed = await _fetch_feed_raw(client)
        if raw_feed is not None:
            with db.batch():  # تثبيت واحد للقطة + كل إشارات الدورة
                db.insert_snapshot("feed", raw_feed, recorded_at)
                events = extract.unwrap_feed(raw_feed)
                # ختم أحدث حدث في الـ feed. بدونه لا يمكن تمييز "السوق هادئ"
                # عن "feed المصدر متجمّد" — وقد شوهد متجمّداً 3 ساعات بينما
                # المسجّل يعمل بلا خطأ. الفرق حاسم لأي تحليل زمنيّ لاحق.
                newest = max(
                    (str(e.get("createdAt")) for e in events if e.get("createdAt")),
                    default=None,
                )
                if newest:
                    db.set_meta("last_feed_event_at", newest)
                for ev in events:
                    row = extract.extract_signal_event(
                        ev, recorded_at, lb.lookup, lb.lookups
                    )
                    if row is None:
                        continue
                    if db.insert_signal(row):
                        stats["signals"] += 1
                    # المُشغّلات فقط تُدخل المراقبة؛ sell سياق لا يُشغّل.
                    # السقف يخصّ المُشار إليها وحدها — الضابطة لا تزاحمها عليه.
                    if (
                        row["signal_type"] in config.TRIGGER_SIGNAL_TYPES
                        and db.active_watch_count(is_control=0) < config.WATCHLIST_CAP
                    ):
                        added = db.upsert_watch(
                            token_address=row["token_address"],
                            network_id=row["network_id"] or "",
                            source=row["signal_type"],
                            entry_signal_id=row["id"],
                            watch_hours=config.WATCH_HOURS,
                            now_iso=recorded_at,
                            admission_price_usd=row.get("price_usd"),
                        )
                        if added:
                            stats["watch_added"] += 1
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
        if filt["filter_errors"]:
            stats["errors"] += filt["filter_errors"]
    except Exception as exc:  # noqa: BLE001
        _fail("filter", exc)

    # 2.2) نوافذ إشارة من الكون نفسه وسعر السوق نفسه المستخدم للضابطة.
    try:
        stats["comparison_signal_added"] = admit_signal_comparison_windows(
            db, control_candidates, recorded_at
        )
    except Exception as exc:  # noqa: BLE001
        _fail("comparison_signal", exc)

    # 2.25) الضابطة تُقبل في الدورة نفسها التي قبلت إشارة مقارنة فقط. السماح
    # بملء 40 ضابطة بعد إشارة قديمة واحدة يعيد اختلال الزمن الذي نريد منعه.
    if stats["comparison_signal_added"]:
        try:
            stats["control_added"] = admit_control_sample(
                db, control_candidates, recorded_at
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
        db.set_meta("last_error_token_reload", f"{utcnow_iso()}: {type(exc).__name__}")
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
    db.set_meta("last_token_refresh_at", utcnow_iso())
    _log("token rotated → client rebuilt")  # بلا أي قيمة سرّية
    return new_client, disk_token


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
    try:
        while cycles is None or n < cycles:
            started = time.monotonic()
            # التقط التوكن المتجدّد على القرص قبل الدورة (يمنع 401 بعد الساعة).
            client, current_token = await _maybe_rotate_client(client, current_token, lb, db)
            try:
                stats = await run_cycle(client, db, lb, now_mono=started)
                _log(f"cycle {n}: {stats}")
            except Exception:  # noqa: BLE001 — درع أخير حول الدورة كلها
                _log("cycle crashed:\n" + traceback.format_exc())
                db.bump_counter("cycle_crashes")
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
    except Exception:
        pass


if __name__ == "__main__":
    import sys

    _cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        _cycles = int(sys.argv[1])
    asyncio.run(main_loop(cycles=_cycles))
