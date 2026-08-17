# -*- coding: utf-8 -*-
"""دورة طبقة السلسلة: تركّز الملكية مقيساً من البلوك تشين لا من FOMO.

منفصلة عن `recorder.py` عمداً ولسببين مقيسين:

1. **ميزانية الدورة ممتلئة.** دورة المسجّل 60 ثانية، والتمهّل «لُطفاً بالمصدر»
   كان يأكل 27.5ث منها. مصدر خارجيّ ثانٍ داخلها يجعل بطأه يؤخّر جمع FOMO نفسه.
2. **الانفصال هو ما يجعل إيقاع 5 دقائق ممكناً.** 73 عملة سولانا نشطة ÷ 5 دقائق
   = 15 نداءً في الدقيقة. الدورة كلّها تتحمّل 13 نداءً لكل شيء — أمّا هنا
   فالمقيس على المفتاح **224 نداءً في الدقيقة بلا فشل واحد** (توازٍ 3، وسيط
   216ms)، فحاجتنا 7% من الطاقة المريحة.

ولماذا هذه الطبقة أصلاً وعندنا `token_holders`؟ لأنّ FOMO يعطي `top10` وحده
وكل ~25 دقيقة (وسيط الفجوة المقيس 25.0 على 19,440 زوجاً)، فلا top1 — أي لا
جواب عن «حوت مفرد أم عشرة موزّعين؟» وهما خطران مختلفان — ولا إيقاع يلحق حركة
تصريفٍ تجري في دقائق. نداء السلسلة الواحد يعطي top1/5/10/20 معاً (مقيس 230ms).

**سولانا وحدها، وليس تقصيراً**: معيار ERC-20 لا يحمل قائمة حائزين على السلسلة،
فلا نداء عقدة يعطي أكبر الحائزين على EVM إطلاقاً — لا هنا ولا بمزوّد آخر بلا
مفهرس مدفوع لكل شبكة. سولانا = 45.2% من إشاراتنا (30,931 من 68,491).

قراءة فقط (FR-012)، ولا يُطبع المفتاح ولا يُسجَّل (FR-013).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import config
import extract
from db import RecorderDB
from solana_rpc import ChainKeyMissing


async def run_chain_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """دورة واحدة: أقدم المراقَبات المستحقّة، نداء واحد لكلٍّ، صفّ تركّز لكلٍّ.

    ثلاث حالات لا حالتان، وهذا مقصود:
      - `ok`    — قياس وصل وصفّ كُتب ⇒ التحديث التالي بعد `CHAIN_REFRESH_SECONDS`.
      - `empty` — المصدر ردّ بلا قياس (عنوان ليس عملةً، أو بلا حسابات) ⇒ إيقاع
        عاديّ لا سريع: إعادةٌ كل دقيقتين لعنوان لا قياس له أبداً تحرق الميزانية.
      - `error` — فشل نداء ⇒ إعادة سريعة (`CHAIN_ERROR_RETRY_SECONDS`)، فالمجهول
        ليس آمناً.

    `ChainKeyMissing` **يُرفع خارج الدورة** ولا يُعلَّم على العملات: العيب فينا لا
    فيها، ووسمُ الطابور كلّه `error` بسبب ملفّ مفاتيح غائب يفسد جدولة صحيحة.
    """
    stats = {
        "chain_due": 0, "chain_rows": 0, "chain_empty": 0,
        "chain_unsupported": 0, "chain_errors": 0,
    }
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.chain_fetch_due(
        limit=config.CHAIN_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        networks=config.CHAIN_NETWORKS,
    )
    stats["chain_due"] = len(due)

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        first_seen = w["first_seen_at"]
        sig = w.get("entry_signal_id")
        is_control = int(w.get("is_control") or 0)
        status = "error"
        top1: float | None = None

        try:
            if str(addr).lower() in config.CHAIN_UNSUPPORTED_TOKENS:
                # Some reference mints have millions of token accounts. The
                # Solana RPC rejects largest-account enumeration for them;
                # keep the token out of the retry loop instead of logging the
                # same permanent error every few minutes.
                status = "unsupported"
                stats["chain_unsupported"] += 1
            else:
                raw = await rpc.fetch_concentration_raw(addr)
                row = extract.extract_chain_concentration(
                    raw, addr, net, recorded_at, first_seen, sig, is_control
                )
                if row is None:
                    status = "empty"
                    stats["chain_empty"] += 1
                else:
                    db.insert_chain_concentration(row)
                    status = "ok"
                    top1 = row["top1_pct"]
                    stats["chain_rows"] += 1
        except ChainKeyMissing:
            raise  # عيب إعداد لا عيب عملة — لا يُوسَم عليها
        except Exception as exc:  # noqa: BLE001 — عملة واحدة لا تُسقط الدورة
            stats["chain_errors"] += 1
            # الرسالة مشطوبة من المفتاح داخل `solana_rpc` قبل أن تصل هنا
            # (FR-013)، وهذا الحقل تعرضه لوحة القيادة.
            db.set_meta(
                "last_error_chain",
                f"{recorded_at}: {addr}: {type(exc).__name__}: {exc}",
            )

        db.set_chain_state(addr, net, status, top1, recorded_at)
        # فاصل **بين** النداءات لا بعد آخرها (نفس حرس بقيّة الدورات).
        if i + 1 < len(due):
            await sleep(config.CHAIN_PACING_SECONDS)
    return stats


async def run_chain_auth_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep
) -> dict[str, int]:
    """الطبقة البطيئة: صلاحيات المِنت وقابليّة التعديل وحيازة المطوّر.

    إيقاع ساعيّ لا خمس‑دقائقيّ: صلاحية السكّ تُشطب مرّة واحدة في عمر العملة إن
    شُطبت، فسؤالها 12 مرّة في الساعة إهدارُ ميزانيةٍ نحتاجها للتركّز المتحرّك.
    وطابور مستقلّ (`chain_auth_state`) فلا يُخفي تحديثُ إحدى الطبقتين تأخّرَ
    الأخرى.

    نداءان لا واحد، والثاني **مشروط**: عنوان المطوّر لا يُعرف إلّا من ردّ الأصل،
    فلا يمكن ضمّه إلى نفس الدفعة. وإن فشل النداء الثاني وحده يُكتب الصفّ بلا
    `dev_holding_pct` — خسارة عمود لا خسارة قياس.
    """
    stats = {"auth_due": 0, "auth_rows": 0, "auth_empty": 0, "auth_errors": 0,
             "auth_dev": 0}
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_AUTH_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.CHAIN_AUTH_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.chain_auth_due(
        limit=config.CHAIN_AUTH_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        networks=config.CHAIN_NETWORKS,
    )
    stats["auth_due"] = len(due)

    for i, w in enumerate(due):
        addr = w["token_address"]
        net = str(w["network_id"] or "")
        first_seen = w["first_seen_at"]
        sig = w.get("entry_signal_id")
        is_control = int(w.get("is_control") or 0)
        status = "error"

        try:
            raw = await rpc.fetch_authority_raw(addr)
            owner = extract.pick_dev_owner(raw)
            if owner:
                await sleep(config.CHAIN_PACING_SECONDS)
                try:
                    raw = dict(raw)
                    raw["dev_owner"] = owner
                    raw["owner_accounts"] = await rpc.fetch_owner_token_balance_raw(
                        owner, addr
                    )
                    stats["auth_dev"] += 1
                except ChainKeyMissing:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # عمود واحد يسقط، والصفّ ينجو: `dev_owner` يبقى محفوظاً
                    # فنعرف لِمن كنّا نقيس حين نقرأ الخطأ.
                    raw["owner_accounts"] = None
                    raw["dev_error"] = f"{type(exc).__name__}: {exc}"
            row = extract.extract_chain_authority(
                raw, addr, net, recorded_at, first_seen, sig, is_control
            )
            if row is None:
                status = "empty"
                stats["auth_empty"] += 1
            else:
                db.insert_chain_authority(row)
                status = "ok"
                stats["auth_rows"] += 1
        except ChainKeyMissing:
            raise
        except Exception as exc:  # noqa: BLE001
            stats["auth_errors"] += 1
            db.set_meta(
                "last_error_chain_auth",
                f"{recorded_at}: {addr}: {type(exc).__name__}: {exc}",
            )

        db.set_chain_auth_state(addr, net, status, recorded_at)
        if i + 1 < len(due):
            await sleep(config.CHAIN_PACING_SECONDS)
    return stats
